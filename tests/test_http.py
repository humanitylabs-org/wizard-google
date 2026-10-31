"""Real HTTP server + real handlers; only Google is fake."""
import hashlib
import http.client
import json
import shutil
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path

from fakes import CLIENT_JSON, FakeGog, FakeGoogle

import server


def start(env):
    fake = FakeGoogle()
    config = server.Config({'GOOGLE_SIGNIN_SERVICE': 'off', **env})
    app = server.App(config, transport=fake)
    app.broker.gog = FakeGog()
    srv, app = server.serve(config, app)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, app, fake


class Client:
    def __init__(self, port, host=None, origin=None):
        self.port, self.host = port, host or f'127.0.0.1:{port}'
        self.origin = origin or f'http://{self.host}'
        self.cookie = self.csrf = None

    def send(self, method, path, body=None, headers=None, browser=True):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        h = {'Host': self.host}
        if browser:
            if self.cookie:
                h['Cookie'] = self.cookie
            if method == 'POST':
                h.update({'Origin': self.origin, 'Sec-Fetch-Site': 'same-origin', 'X-CSRF-Token': self.csrf or ''})
        h.update(headers or {})
        raw = None
        if body is not None:
            raw = json.dumps(body).encode()
            h['Content-Type'] = 'application/json'
        conn.request(method, path, raw, h)
        response = conn.getresponse()
        data = response.read()
        cookie = response.getheader('Set-Cookie')
        conn.close()
        try:
            value = json.loads(data)
        except ValueError:
            value = data.decode('utf-8', 'replace')
        return response.status, value, cookie

    def bootstrap(self, extra=None):
        status, value, cookie = self.send('POST', '/api/session', {}, {'X-Wizard-Request': 'bootstrap', **(extra or {})})
        assert status == 201, value
        self.cookie, self.csrf = cookie.split(';')[0], value['csrf']
        return cookie

    def mcp(self, name, args=None, headers=None):
        status, value, _ = self.send('POST', '/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {'name': name, 'arguments': args or {}}},
                                     headers, browser=False)
        assert status == 200, value
        return value['result']['structuredContent']


class LoopbackOwner(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.grants_file = Path(cls.dir) / 'grants.json'
        cls.srv, cls.app, cls.fake = start({'PORT': '0', 'DATA_DIR': cls.dir + '/data', 'GOOGLE_DEV_MODE': 'loopback-single-owner',
                                            'WIZARD_GRANTS_FILE': str(cls.grants_file)})
        cls.port = cls.srv.server_address[1]
        # The server learns its port only after binding; rebuild the host lists for tests.
        config = cls.app.config
        config.port = cls.port
        config.local_hosts = {f'127.0.0.1:{cls.port}', f'localhost:{cls.port}'}
        config.trusted_hosts = set(config.local_hosts)
        config.trusted_origins = {f'http://{h}' for h in config.local_hosts}
        cls.owner = Client(cls.port)
        cls.owner.bootstrap()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        shutil.rmtree(cls.dir, ignore_errors=True)

    def connect(self, sub, email, ops):
        status, value, _ = self.owner.send('POST', '/api/oauth/start', {'operations': ops})
        self.assertEqual(status, 200, value)
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(value['authorization_url']).query))
        self.assertEqual(query['redirect_uri'], f'http://127.0.0.1:{self.port}/oauth/callback')
        self.fake.last_nonce = query['nonce']
        self.fake.issue('code-' + sub, sub, email, sorted(query['scope'].split()))
        status, page, _ = self.owner.send('GET', '/oauth/callback?' + urllib.parse.urlencode({'state': query['state'], 'code': 'code-' + sub}))
        self.assertEqual(status, 200)
        self.assertIn('is connected', page)
        return self.owner.send('GET', '/api/oauth/status')[1]['result']['account']

    def test_1_setup_paste_connect_grant_and_agent_call(self):
        status, home, _ = self.owner.send('GET', '/api/home')
        self.assertEqual(status, 200)
        self.assertEqual(home['setup']['current'], 'project')
        status, saved, _ = self.owner.send('POST', '/api/setup/client', {'json': CLIENT_JSON})
        self.assertEqual(status, 200, saved)
        self.assertEqual(saved['check']['result'], 'ok')
        account = self.connect('111', 'owner@gmail.com', ['gmail.search', 'gmail.read'])

        # The agent sees the account and the services switched on for it, never a token.
        agent = Client(self.port)
        seen = agent.mcp('google_status')
        self.assertEqual(seen['accounts'][0]['email'], 'owner@gmail.com')
        self.assertEqual(seen['accounts'][0]['services'], [])  # the 0.1-style sign-in asked only for typed operations
        self.assertEqual(agent.mcp('google_run', {'args': ['gmail', 'search', 'lunch']})['error']['code'], 'service_off')
        # The owner switches Gmail on: Google has not been asked for it yet, so the page is told to ask.
        status, value, _ = self.owner.send('POST', f'/api/accounts/{account}/service', {'service': 'gmail', 'on': True})
        self.assertEqual((status, value['error']['code']), (409, 'consent_needed'))
        status, value, _ = self.owner.send('POST', '/api/oauth/start', {'account': account, 'services': ['gmail']})
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(value['authorization_url']).query))
        self.assertIn('https://www.googleapis.com/auth/gmail.modify', query['scope'].split())
        self.fake.last_nonce = query['nonce']
        self.fake.issue('code-111b', '111', 'owner@gmail.com', sorted(query['scope'].split()))
        status, page, _ = self.owner.send('GET', '/oauth/callback?' + urllib.parse.urlencode({'state': query['state'], 'code': 'code-111b'}))
        self.assertEqual(status, 200, page)
        status, home, _ = self.owner.send('GET', '/api/home')
        gmail = next(sv for sv in home['accounts'][0]['services'] if sv['id'] == 'gmail')
        self.assertEqual((gmail['granted'], gmail['on']), (True, True))
        found = agent.mcp('google_run', {'args': ['gmail', 'search', 'lunch']})
        self.assertEqual((found['account'], found['exit_code']), ('owner@gmail.com', 0))
        self.assertEqual(self.app.broker.gog.runs[-1]['roots'], ['gmail'])
        # Switching it off stops the agent at once.
        status, _, _ = self.owner.send('POST', f'/api/accounts/{account}/service', {'service': 'gmail', 'on': False})
        self.assertEqual(status, 200)
        self.assertEqual(agent.mcp('google_run', {'args': ['gmail', 'search', 'lunch']})['error']['code'], 'service_off')
        self.assertEqual(agent.mcp('google_help')['commands'], [])
        # No response anywhere carries a token or the client secret.
        everything = json.dumps([home, seen, found, self.owner.send('GET', '/api/home')[1]])
        for secret in ('fake-secret-for-tests', 'rt-111', 'at-111'):
            self.assertNotIn(secret, everything)

    def test_2_apps_use_wizard_grants_or_an_access_key(self):
        token = 'grant-token-for-music-app'
        self.grants_file.write_text(json.dumps({'incoming': [{'from': 'music', 'sha256': hashlib.sha256(token.encode()).hexdigest()}], 'enforce': False}))
        app = Client(self.port)
        status, value, _ = app.send('GET', '/consumer/v1/status', headers={'X-Wizard-Grant': token}, browser=False)
        self.assertEqual((status, value['caller']), (200, 'app:music'))
        status, value, _ = app.send('POST', '/consumer/v1/call', {'operation': 'gmail.search'}, {'X-Wizard-Grant': token}, browser=False)
        self.assertEqual((status, value['error']['code']), (403, 'grant_required'))
        status, value, _ = app.send('GET', '/consumer/v1/status', headers={'X-Wizard-Grant': 'forged'}, browser=False)
        self.assertEqual(status, 401)
        status, made, _ = self.owner.send('POST', '/api/keys', {'client': 'app:planner'})
        status, value, _ = app.send('GET', '/consumer/v1/status', headers={'Authorization': 'Bearer ' + made['key']}, browser=False)
        self.assertEqual(value['caller'], 'app:planner')
        # Owner browser credentials never work on the app API.
        status, _, _ = app.send('GET', '/consumer/v1/status', headers={'Cookie': self.owner.cookie, 'Authorization': 'Bearer ' + made['key']}, browser=False)
        self.assertEqual(status, 403)

    def test_3_mcp_is_box_only(self):
        c = Client(self.port)
        for headers in ({'Origin': c.origin}, {'X-Forwarded-For': '1.2.3.4'}, {'Tailscale-User-Login': 'x@y'}, {'Cookie': 'wg_session=x'}):
            status, value, _ = c.send('POST', '/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}, headers, browser=False)
            self.assertEqual(status, 403, headers)
        status, value, _ = c.send('POST', '/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}, browser=False)
        names = {t['name'] for t in value['result']['tools']}
        self.assertEqual(names, {'google_status', 'google_run', 'google_help', 'mail_search', 'mail_get', 'mail_draft', 'mail_send',
                                 'calendar_list_events', 'calendar_create_event', 'calendar_update_event', 'calendar_delete_event',
                                 'calendar_respond'})

    def test_4_owner_writes_need_csrf_and_same_origin(self):
        status, _, _ = self.owner.send('POST', '/api/setup/step', {'step': 'publish', 'done': True}, {'X-CSRF-Token': 'wrong'})
        self.assertEqual(status, 403)
        status, _, _ = self.owner.send('POST', '/api/setup/step', {'step': 'publish', 'done': True}, {'Origin': 'http://evil.example'})
        self.assertEqual(status, 403)
        status, _, _ = self.owner.send('POST', '/api/setup/step', {'step': 'publish', 'done': True}, {'Sec-Fetch-Site': 'cross-site'})
        self.assertEqual(status, 403)
        status, _, _ = self.owner.send('POST', '/api/setup/step', {'step': 'publish', 'done': True})
        self.assertEqual(status, 200)
        status, _, _ = self.owner.send('GET', '/api/home', headers={'Host': 'evil.example'})
        self.assertEqual(status, 403)

    def test_5_callback_with_bad_state_fails_closed(self):
        status, page, _ = self.owner.send('GET', '/oauth/callback?state=nope&code=x')
        self.assertEqual(status, 400)
        self.assertIn('Not connected', page)
        stranger = Client(self.port)
        status, _, _ = stranger.send('GET', '/oauth/callback?state=nope&code=x')
        self.assertEqual(status, 400)

    def test_6_connection_contract_and_assets(self):
        status, value, _ = Client(self.port).send('GET', '/api/connection', browser=False)
        self.assertEqual((status, value['contract'], value['provider']), (200, 'wizard.google-connection/1', 'google'))
        status, _, _ = Client(self.port).send('GET', '/api/connection', headers={'X-Forwarded-For': '1.1.1.1'}, browser=False)
        self.assertEqual(status, 403)
        for asset in ('/', '/app.js', '/styles.css', '/sheet.js', '/icon.png', '/standalone.css'):
            self.assertEqual(Client(self.port).send('GET', asset, browser=False)[0], 200, asset)
        self.assertEqual(Client(self.port).send('GET', '/../app/server.py', browser=False)[0], 404)


class ShellOwner(unittest.TestCase):
    """Inside the Wizard shell: Tailscale Serve names the signed-in user; only the configured owner gets in."""

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.srv, cls.app, cls.fake = start({'PORT': '0', 'DATA_DIR': cls.dir, 'WIZARD_SHELL_AUTH': 'tailscale-serve',
                                            'WIZARD_OWNER_LOGINS': 'owner@example.com', 'WIZARD_TRUSTED_HOSTS': 'box.tail1234.ts.net',
                                            'WIZARD_TRUSTED_ORIGINS': 'https://box.tail1234.ts.net'})
        cls.port = cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_owner_login_required_and_redirect_uses_the_shell_address(self):
        stranger = Client(self.port, 'box.tail1234.ts.net', 'https://box.tail1234.ts.net')
        status, _, _ = stranger.send('POST', '/api/session', {}, {'X-Wizard-Request': 'bootstrap', 'Tailscale-User-Login': 'guest@example.com'})
        self.assertEqual(status, 401)
        owner = Client(self.port, 'box.tail1234.ts.net', 'https://box.tail1234.ts.net')
        cookie = owner.bootstrap({'Tailscale-User-Login': 'owner@example.com'})
        self.assertIn('Path=/wizard-google/', cookie)
        self.assertIn('Secure', cookie)
        status, home, _ = owner.send('GET', '/api/home', headers={'Tailscale-User-Login': 'owner@example.com'})
        self.assertEqual(status, 200)
        client_step = next(s for s in home['setup']['steps'] if s['id'] == 'client')
        self.assertEqual(client_step['copy'][0]['value'], 'https://box.tail1234.ts.net/wizard-google/oauth/callback')
        # Two identity headers are ambiguous and refused.
        status, _, _ = owner.send('GET', '/api/home', headers={'Tailscale-User-Login': 'owner@example.com'})
        conn = http.client.HTTPConnection('127.0.0.1', self.port)
        conn.putrequest('GET', '/api/home', skip_host=True)
        conn.putheader('Host', 'box.tail1234.ts.net')
        conn.putheader('Tailscale-User-Login', 'owner@example.com')
        conn.putheader('Tailscale-User-Login', 'guest@example.com')
        conn.putheader('Cookie', owner.cookie)
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 401)
        conn.close()


if __name__ == '__main__':
    unittest.main()
