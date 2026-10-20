"""HTTP front of the Google app: owner page + API, Google's return address, MCP for agents,
and the consumer API for other Wizard apps. One process, one container."""
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from broker import Broker, Problem  # noqa: E402
from google import OPERATIONS  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / 'web'
APP_ID = 'wizard-google'
VERSION = '0.1.0'
ASSETS = {'index.html', 'app.js', 'styles.css', 'boot.js', 'tokens.css', 'ui.css', 'appearance-client.js'}
FORWARDING = ('X-Forwarded-For', 'X-Forwarded-Host', 'Forwarded', 'X-Real-IP')
SESSION_TTL = 8 * 3600
MAX_BODY = 64_000


def csv_env(name):
    return {x.strip().lower() for x in os.getenv(name, '').split(',') if x.strip()}


class Config:
    """Read once at start; tests build their own."""

    def __init__(self, env=None):
        env = env if env is not None else os.environ
        get = lambda k, d='': env.get(k, d)
        self.port = int(get('PORT', '18812'))
        self.data_dir = get('DATA_DIR', str(ROOT / '.data'))
        self.store_key = get('GOOGLE_STORE_KEY') or None
        self.relay_callback = get('GOOGLE_RELAY_CALLBACK').strip()
        self.shell_auth = get('WIZARD_SHELL_AUTH')
        self.owner_logins = {x.strip().lower() for x in get('WIZARD_OWNER_LOGINS').split(',') if x.strip()}
        self.shell_path = '/' + get('WIZARD_SHELL_PATH', '/wizard-google/').strip('/') + '/'
        self.owner_token = get('WIZARD_OWNER_TOKEN')
        self.admin_token = get('WIZARD_ADMIN_TOKEN')
        self.mcp_box_only = get('WIZARD_MCP_BOX_ONLY', 'true').lower() == 'true'
        self.grants_file = get('WIZARD_GRANTS_FILE', '/run/wizard/grants.json')
        self.dev_mode = get('GOOGLE_DEV_MODE') == 'loopback-single-owner'
        self.local_hosts = {f'127.0.0.1:{self.port}', f'localhost:{self.port}', f'{APP_ID}:{self.port}'}
        self.trusted_hosts = self.local_hosts | {x.strip().lower() for x in get('WIZARD_TRUSTED_HOSTS').split(',') if x.strip()}
        self.trusted_origins = {f'http://{h}' for h in self.local_hosts} | {x.strip().lower().rstrip('/') for x in get('WIZARD_TRUSTED_ORIGINS').split(',') if x.strip()}
        self.frame_ancestors = get('WIZARD_FRAME_ANCESTORS', "'self'")


class Sessions:
    def __init__(self):
        self.items, self.lock = {}, threading.Lock()

    def create(self):
        cookie, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with self.lock:
            stamp = time.time()
            self.items = {k: v for k, v in self.items.items() if v['expires'] > stamp}
            self.items[sha(cookie)] = {'csrf': sha(csrf), 'expires': stamp + SESSION_TTL}
        return cookie, csrf

    def check(self, cookie, csrf=None):
        entry = self.items.get(sha(cookie or ''))
        if not cookie or not entry or entry['expires'] <= time.time():
            raise Problem(401, 'owner_session_required', 'Reload the Google app.')
        if csrf is not None and not hmac.compare_digest(entry['csrf'], sha(csrf)):
            raise Problem(403, 'csrf_rejected', 'Reload the Google app.')
        return sha(cookie)


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def read_cookie(header, name):
    for part in (header or '').split(';'):
        key, _, value = part.strip().partition('=')
        if key == name:
            return value
    return None


class WizardGrants:
    """PR #6 per-grant credentials: `X-Wizard-Grant` hashed and looked up in the App Manager's file."""

    def __init__(self, path):
        self.path = Path(path)

    def caller(self, token):
        if not token:
            return None
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return None
        digest = sha(token)
        for item in data.get('incoming', []) if isinstance(data, dict) else []:
            if isinstance(item, dict) and hmac.compare_digest(str(item.get('sha256', '')), digest) and re.fullmatch(r'[a-z0-9][a-z0-9-]{1,40}', str(item.get('from', ''))):
                return item['from']
        return None


# ---------- MCP ----------

ACCOUNT_ARG = {'type': 'string', 'description': 'Email of the Google account. Optional when only one account is allowed.'}
TOOL_OPS = {
    'google_gmail_search': ('gmail.search', 'Search the owner\'s Gmail. Returns sender, subject, date and a short preview per message.',
                            {'query': {'type': 'string', 'description': 'Gmail search, e.g. "from:anna newer_than:7d"'}, 'max': {'type': 'integer', 'minimum': 1, 'maximum': 25}}, []),
    'google_gmail_read': ('gmail.read', 'Read one Gmail message as plain text.', {'id': {'type': 'string'}}, ['id']),
    'google_gmail_send': ('gmail.send', 'Send an email from the owner\'s Gmail. Only when the owner asked for this exact email.',
                          {'to': {'type': 'array', 'items': {'type': 'string'}}, 'cc': {'type': 'array', 'items': {'type': 'string'}},
                           'subject': {'type': 'string'}, 'body': {'type': 'string'}, 'thread_id': {'type': 'string'}}, ['to', 'subject', 'body']),
    'google_calendar_events': ('calendar.read', 'List calendar events in a time range (RFC 3339 times).',
                               {'time_min': {'type': 'string'}, 'time_max': {'type': 'string'}, 'query': {'type': 'string'},
                                'calendar_id': {'type': 'string'}, 'max': {'type': 'integer', 'minimum': 1, 'maximum': 100}}, []),
    'google_calendar_create': ('calendar.write', 'Add an event to the owner\'s calendar.',
                               {'summary': {'type': 'string'}, 'start': {'type': 'string'}, 'end': {'type': 'string'}, 'description': {'type': 'string'},
                                'location': {'type': 'string'}, 'attendees': {'type': 'array', 'items': {'type': 'string'}}, 'calendar_id': {'type': 'string'}},
                               ['summary', 'start', 'end']),
    'google_drive_search': ('drive.search', 'Find Drive files by name. Returns names, types and links only.',
                            {'query': {'type': 'string'}, 'max': {'type': 'integer', 'minimum': 1, 'maximum': 100}}, []),
    'google_drive_read': ('drive.read', 'Read a Drive file as text (Docs, Sheets as CSV, Slides, text files).', {'file_id': {'type': 'string'}}, ['file_id']),
    'google_contacts_search': ('contacts.read', 'Look up the owner\'s contacts by name or email.', {'query': {'type': 'string'}, 'max': {'type': 'integer', 'minimum': 1, 'maximum': 30}}, ['query']),
    'google_youtube_video': ('youtube.read', 'Look up a YouTube video by its 11-character id.', {'video_id': {'type': 'string'}}, ['video_id']),
}


def tools():
    listed = [
        {'name': 'google_status', 'description': 'Google accounts on this box and what you may do with each. Start here. Never returns credentials.',
         'annotations': {'readOnlyHint': True}, 'inputSchema': {'type': 'object', 'properties': {}}},
        {'name': 'google_setup_guide', 'description': 'The owner\'s Google setup steps, which are done, and the link and values for the next one. Use it to walk the owner through setup.',
         'annotations': {'readOnlyHint': True}, 'inputSchema': {'type': 'object', 'properties': {}}},
        {'name': 'google_request_access', 'description': 'Ask the owner to allow operations on a Google account. The owner decides in the Google app; this never grants anything itself.',
         'inputSchema': {'type': 'object', 'required': ['operations'], 'properties': {
             'operations': {'type': 'array', 'items': {'type': 'string', 'enum': list(OPERATIONS)}},
             'account': ACCOUNT_ARG, 'reason': {'type': 'string', 'description': 'One short sentence the owner will see.'}}}},
    ]
    for name, (operation, description, props, required) in TOOL_OPS.items():
        listed.append({'name': name, 'description': description, 'annotations': {'readOnlyHint': not OPERATIONS[operation]['writes']},
                       'inputSchema': {'type': 'object', 'required': required, 'properties': {'account': ACCOUNT_ARG, **props}}})
    return listed


def rpc(app, message, caller):
    if not isinstance(message, dict) or message.get('jsonrpc') != '2.0' or not isinstance(message.get('method'), str):
        return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Invalid Request'}}
    if 'id' not in message:
        return None
    rid, method, params = message['id'], message['method'], message.get('params') or {}
    if method == 'initialize':
        return {'jsonrpc': '2.0', 'id': rid, 'result': {'protocolVersion': '2025-06-18', 'capabilities': {'tools': {}}, 'serverInfo': {'name': APP_ID, 'version': VERSION}}}
    if method == 'ping':
        return {'jsonrpc': '2.0', 'id': rid, 'result': {}}
    if method == 'tools/list':
        return {'jsonrpc': '2.0', 'id': rid, 'result': {'tools': tools()}}
    if method != 'tools/call' or not isinstance(params, dict):
        return {'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32601, 'message': 'Method not found'}}
    name, args = params.get('name'), params.get('arguments') or {}
    try:
        if not isinstance(args, dict):
            raise Problem(400, 'invalid_arguments', 'Arguments must be an object.')
        broker = app.broker
        if name == 'google_status':
            value = broker.caller_status(caller)
            value['setup'] = {k: v for k, v in broker.setup_plan(app.last_origin(), app.config.shell_path).items() if k in ('count', 'current', 'complete')}
            value['app_link'] = '/wizard-os-home/?open=wizard-google'
        elif name == 'google_setup_guide':
            value = broker.setup_plan(app.last_origin(), app.config.shell_path)
            value['app_link'] = '/wizard-os-home/?open=wizard-google'
        elif name == 'google_request_access':
            value = broker.request_access(caller, args.get('operations'), args.get('account'), args.get('reason', ''))
            value['tell_owner'] = 'Open the Google app and choose Allow on the request.'
        elif name in TOOL_OPS:
            account = args.get('account')
            value = broker.call(caller, TOOL_OPS[name][0], {k: v for k, v in args.items() if k != 'account'}, account)
        else:
            return {'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32602, 'message': 'Unknown tool'}}
        return {'jsonrpc': '2.0', 'id': rid, 'result': {'content': [{'type': 'text', 'text': json.dumps(value)}], 'structuredContent': value, 'isError': False}}
    except Problem as problem:
        error = problem.as_dict()
        return {'jsonrpc': '2.0', 'id': rid, 'result': {'content': [{'type': 'text', 'text': json.dumps({'error': error})}], 'structuredContent': {'error': error}, 'isError': True}}


# ---------- HTTP ----------

class App:
    def __init__(self, config, broker=None, transport=None):
        self.config = config
        self.broker = broker or Broker(config.data_dir, transport=transport, store_key=config.store_key, relay_callback=config.relay_callback)
        self.sessions = Sessions()
        self.grants = WizardGrants(config.grants_file)
        self.origins = []

    def origin_for(self, host):
        for scheme in ('https', 'http'):
            if f'{scheme}://{host}' in self.config.trusted_origins:
                return f'{scheme}://{host}'
        return None

    def shell_path_for(self, host):
        return '/' if host in self.config.local_hosts else self.config.shell_path

    def remember_origin(self, origin):
        if origin and origin not in self.origins[-1:]:
            self.origins = (self.origins + [origin])[-4:]

    def last_origin(self):
        if self.origins:
            return self.origins[-1]
        https = sorted(o for o in self.config.trusted_origins if o.startswith('https://'))
        return https[0] if https else None


def handler_for(app):
    config = app.config

    class Handler(BaseHTTPRequestHandler):
        server_version = APP_ID

        # ----- responses -----
        def headers_common(self, content_type):
            self.send_header('Content-Type', content_type)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self'; "
                             f"form-action 'self'; base-uri 'none'; frame-ancestors {config.frame_ancestors}")

        def send_json(self, status, value, extra=None):
            raw = json.dumps(value).encode()
            self.send_response(status)
            self.headers_common('application/json')
            for key, val in (extra or {}).items():
                self.send_header(key, val)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def send_page(self, status, title, message, link):
            esc = lambda s: s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')
            raw = (f'<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                   f'<meta name="color-scheme" content="dark"><title>{esc(title)}</title><link rel="stylesheet" href="../tokens.css"><link rel="stylesheet" href="../ui.css">'
                   f'<body><main class="wz-empty"><strong>{esc(title)}</strong><p>{esc(message)}</p><p><a href="{esc(link)}">Back to Google in Wizard</a></p></main></body></html>').encode()
            self.send_response(status)
            self.headers_common('text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def fail(self, problem):
            self.send_json(problem.status, {'error': problem.as_dict()})

        def log_message(self, *args):
            pass  # no request logs: callback URLs carry sign-in codes

        # ----- guards -----
        def host(self):
            host = (self.headers.get('Host') or '').lower()
            if host not in config.trusted_hosts:
                raise Problem(403, 'unknown_host', 'Unknown host.')
            return host

        def owner(self):
            host = self.host()
            if self.headers.get('Authorization'):
                raise Problem(403, 'owner_bearer_refused', 'Bearer credentials cannot open the owner page.')
            if config.dev_mode and host in config.local_hosts:
                return host
            if config.shell_auth == 'tailscale-serve' and host not in config.local_hosts:
                logins = self.headers.get_all('Tailscale-User-Login') or []
                if config.owner_logins and len(logins) == 1 and logins[0].strip().lower() in config.owner_logins:
                    return host
                raise Problem(401, 'owner_login_required', 'Open this app from your Wizard as its owner.')
            if config.owner_token:
                supplied = self.headers.get('X-Wizard-Owner-Token', '')
                if supplied and hmac.compare_digest(supplied, config.owner_token):
                    return host
                raise Problem(401, 'owner_token_required', 'Owner gateway token required.')
            raise Problem(503, 'owner_auth_unconfigured', 'Set WIZARD_OWNER_LOGINS (Tailscale) or WIZARD_OWNER_TOKEN.')

        def same_origin_write(self, host):
            origin = (self.headers.get('Origin') or '').lower()
            if origin not in config.trusted_origins or origin != app.origin_for(host) or (self.headers.get('Sec-Fetch-Site') or 'same-origin').lower() != 'same-origin':
                raise Problem(403, 'origin_rejected', 'Open this app from your Wizard.')
            return origin

        def session(self, csrf=False):
            return app.sessions.check(read_cookie(self.headers.get('Cookie'), 'wg_session'), self.headers.get('X-CSRF-Token', '') if csrf else None)

        def mcp_caller(self):
            if self.headers.get('Origin') or self.headers.get('Cookie') or (self.headers.get('Sec-Fetch-Site') or ''):
                raise Problem(403, 'mcp_browser_refused', 'Browsers cannot use MCP.')
            if any(self.headers.get(x) for x in FORWARDING) or any(k.lower().startswith(('tailscale-', 'cf-')) for k in self.headers.keys()):
                raise Problem(403, 'mcp_forwarding_refused', 'Forwarded MCP requests are refused.')
            host = (self.headers.get('Host') or '').lower()
            supplied = self.headers.get('Authorization', '')
            if config.admin_token and supplied and hmac.compare_digest(supplied, 'Bearer ' + config.admin_token):
                pass
            elif not (config.mcp_box_only and host in config.local_hosts and not supplied):
                raise Problem(401, 'mcp_not_box_local', 'MCP is for agents on this box.')
            # The Agent Service gateway names the agent it calls for; without a name it is the main agent.
            agent = (self.headers.get('X-Wizard-Agent') or 'main').strip().lower()
            if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,62}', agent):
                raise Problem(400, 'invalid_agent', 'Invalid agent name.')
            return 'agent:' + agent

        def consumer_caller(self):
            if self.headers.get('Cookie') or self.headers.get('Origin'):
                raise Problem(403, 'consumer_boundary', 'Browser credentials cannot call the app API.')
            self.host()
            via_grant = app.grants.caller(self.headers.get('X-Wizard-Grant', ''))
            if via_grant:
                return 'app:' + via_grant
            supplied = self.headers.get('Authorization', '')
            if supplied.startswith('Bearer wgk_'):
                client = app.broker.client_for_key(supplied[7:])
                if client:
                    return client
            raise Problem(401, 'caller_unknown', 'Send X-Wizard-Grant, or an access key the owner created in the Google app.')

        def body(self):
            if (self.headers.get('Content-Type') or '').split(';')[0].strip().lower() != 'application/json':
                raise Problem(415, 'content_type', 'Use application/json.')
            try:
                length = int(self.headers.get('Content-Length', ''))
            except ValueError:
                raise Problem(400, 'invalid_length', 'Invalid Content-Length.') from None
            if not 1 <= length <= MAX_BODY:
                raise Problem(413, 'body_size', 'Request body is empty or too large.')
            try:
                value = json.loads(self.rfile.read(length))
            except ValueError:
                raise Problem(400, 'invalid_json', 'Invalid JSON.') from None
            if not isinstance(value, dict):
                raise Problem(400, 'invalid_body', 'Expected a JSON object.')
            return value

        # ----- routes -----
        def do_GET(self):
            try:
                url = urllib.parse.urlsplit(self.path)
                path = url.path
                query = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
                if path == '/healthz':
                    self.host()
                    return self.send_json(200, {'ok': True, 'app': APP_ID, 'version': VERSION})
                if path == '/api/connection':
                    self.host()
                    if any(self.headers.get(x) for x in FORWARDING) or any(k.lower().startswith(('tailscale-', 'cf-')) for k in self.headers.keys()):
                        raise Problem(403, 'box_only', 'Box-only route.')
                    return self.send_json(200, app.broker.connection())
                if path == '/oauth/callback':
                    host = self.owner()
                    back = '../'
                    try:
                        session = self.session()
                        result = app.broker.finish(query.get('state'), session, query.get('code'), query.get('error'))
                    except Problem as problem:
                        return self.send_page(400, 'Not connected', problem.message, back)
                    note = ' Google did not allow everything you ticked; you can add it later.' if result.get('declined') else ''
                    return self.send_page(200, 'Connected', f"{result['email'] or 'Your account'} is connected. Close this tab and return to Wizard.{note}", back)
                if path.startswith('/consumer/v1/'):
                    caller = self.consumer_caller()
                    if path == '/consumer/v1/status':
                        return self.send_json(200, app.broker.caller_status(caller))
                    raise Problem(404, 'not_found', 'Not found.')
                if path.startswith('/api/'):
                    host = self.owner()
                    session = self.session()
                    origin = app.origin_for(host)
                    if path == '/api/home':
                        app.remember_origin(origin)
                        return self.send_json(200, app.broker.home(origin, app.shell_path_for(host), session))
                    if path == '/api/oauth/status':
                        return self.send_json(200, app.broker.flow_status(session))
                    raise Problem(404, 'not_found', 'Not found.')
                self.host()
                asset = 'index.html' if path in ('', '/') else path.lstrip('/')
                if asset not in ASSETS:
                    raise Problem(404, 'not_found', 'Not found.')
                raw = (WEB / asset).read_bytes()
                kind = mimetypes.guess_type(asset)[0] or 'application/octet-stream'
                self.send_response(200)
                self.headers_common(kind + ('; charset=utf-8' if kind.startswith('text/') or kind.endswith('javascript') else ''))
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except Problem as problem:
                self.fail(problem)
            except OSError:
                self.fail(Problem(404, 'not_found', 'Not found.'))

        def do_POST(self):
            try:
                path = urllib.parse.urlsplit(self.path).path
                if path == '/mcp':
                    caller = self.mcp_caller()
                    self.host()
                    output = rpc(app, self.body(), caller)
                    return self.send_json(202 if output is None else 200, output or {})
                if path.startswith('/consumer/v1/'):
                    caller = self.consumer_caller()
                    body = self.body()
                    if path == '/consumer/v1/call':
                        return self.send_json(200, app.broker.call(caller, body.get('operation'), body.get('args'), body.get('account')))
                    if path == '/consumer/v1/request':
                        return self.send_json(201, app.broker.request_access(caller, body.get('operations'), body.get('account'), body.get('reason', '')))
                    raise Problem(404, 'not_found', 'Not found.')
                host = self.owner()
                origin = self.same_origin_write(host)
                if path == '/api/session':
                    if self.headers.get('X-Wizard-Request') != 'bootstrap':
                        raise Problem(403, 'bootstrap_refused', 'Session bootstrap refused.')
                    cookie, csrf = app.sessions.create()
                    secure = '; Secure' if origin.startswith('https://') else ''
                    return self.send_json(201, {'csrf': csrf}, {'Set-Cookie': f'wg_session={cookie}; Path={app.shell_path_for(host)}; HttpOnly; SameSite=Lax{secure}'})
                session = self.session(csrf=True)
                body = self.body()
                broker, shell = app.broker, app.shell_path_for(host)
                redirect = broker.redirect_uri(origin, shell)
                routes = {
                    '/api/setup/client': lambda: broker.save_client(body.get('json'), body.get('client_id'), body.get('client_secret'), redirect),
                    '/api/setup/client/forget': broker.forget_client,
                    '/api/setup/step': lambda: broker.set_setup(step=body.get('step'), done=body.get('done')),
                    '/api/setup/redirect': lambda: broker.set_setup(redirect_mode=body.get('mode')),
                    '/api/oauth/start': lambda: broker.start(session, origin, shell, body.get('account'), body.get('operations') or []),
                    '/api/oauth/cancel': lambda: broker.cancel(session),
                    '/api/grants': lambda: broker.set_grant(body.get('client'), body.get('account'), body.get('operations')),
                    '/api/keys': lambda: broker.create_key(body.get('client')),
                }
                if path in routes:
                    return self.send_json(200, routes[path]())
                match = re.fullmatch(r'/api/(accounts|grants|keys|requests)/([A-Za-z0-9_-]{4,64})/(disconnect|check|revoke|approve|decline)', path)
                if match:
                    kind, item, action = match.groups()
                    actions = {('accounts', 'disconnect'): lambda: broker.disconnect(item),
                               ('accounts', 'check'): lambda: broker.check_apis(item),
                               ('grants', 'revoke'): lambda: broker.revoke_grant(item),
                               ('keys', 'revoke'): lambda: broker.revoke_key(item),
                               ('requests', 'approve'): lambda: broker.decide(item, True, body.get('account')),
                               ('requests', 'decline'): lambda: broker.decide(item, False)}
                    if (kind, action) in actions:
                        return self.send_json(200, actions[(kind, action)]())
                raise Problem(404, 'not_found', 'Not found.')
            except Problem as problem:
                self.fail(problem)

    return Handler


def wait_for_storage(path, timeout=90):
    # A fresh Runtipi bind directory can become writable a moment after compose up.
    deadline = time.monotonic() + timeout
    while True:
        try:
            Path(path).mkdir(parents=True, exist_ok=True, mode=0o700)
            probe = Path(path) / '.write-check'
            probe.write_text('ok')
            probe.unlink()
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.25)


def serve(config, app=None):
    app = app or App(config)
    server = ThreadingHTTPServer((os.getenv('BIND', '0.0.0.0'), config.port), handler_for(app))
    server.daemon_threads = True
    return server, app


if __name__ == '__main__':
    config = Config()
    wait_for_storage(config.data_dir)
    server, _ = serve(config)
    server.serve_forever()
