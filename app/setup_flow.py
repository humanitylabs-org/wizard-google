"""The owner's setup: which steps are left, where each one happens, and checking what they paste.

Google forces five short visits to the Cloud console (project, app name, publish, client, APIs).
There is no supported API to create a consent screen or an ordinary OAuth client, or to publish
the app (the IAP OAuth Admin API, the only one that came close, was shut down in March 2026). So the
app deep-links each page, hands over the values to paste, and checks the result itself.
"""
import json
import re
import urllib.parse

from google import APIS

CONSOLE = 'https://console.cloud.google.com'
CLIENT_ID = re.compile(r'^(\d{6,20})-[a-z0-9]{8,64}\.apps\.googleusercontent\.com$')
PROJECT_ID = re.compile(r'^[a-z][a-z0-9-]{4,28}[a-z0-9]$')
APP_NAME = 'Wizard'
CALLBACK_PATH = 'oauth/callback'


class SetupProblem(ValueError):
    def __init__(self, code, message, fix_url=None):
        super().__init__(code)
        self.code, self.message, self.fix_url = code, message, fix_url


def console(path, project=None, **query):
    if project:
        query['project'] = project
    return f'{CONSOLE}/{path}' + ('?' + urllib.parse.urlencode(query, safe=',') if query else '')


def enable_url(apis, project=None):
    return console('apis/enableflow', project, apiid=','.join(apis))


def client_url(client):
    if not client:
        return console('auth/clients')
    return console(f"auth/clients/{client['client_id']}", client.get('project_id'))


def parse_client(pasted=None, client_id=None, client_secret=None):
    """Accept the JSON Google offers to download, or the ID and secret typed separately."""
    redirect_uris = []
    project_id = None
    if pasted is not None:
        if not isinstance(pasted, str) or len(pasted) > 16384:
            raise SetupProblem('client_json_invalid', 'Paste the whole file Google downloaded (it starts with {"web":).')
        try:
            value = json.loads(pasted)
        except ValueError:
            raise SetupProblem('client_json_invalid', 'That is not the JSON file from Google. Paste the whole file, or upload it.') from None
        if not isinstance(value, dict):
            raise SetupProblem('client_json_invalid', 'That is not the JSON file from Google.')
        if 'installed' in value:
            raise SetupProblem('client_type_desktop', 'This client is a "Desktop app". Create a new client and choose "Web application".',
                               console('auth/clients/create'))
        web = value.get('web')
        if not isinstance(web, dict):
            raise SetupProblem('client_json_invalid', 'The file has no "web" client. Create a "Web application" client and download its JSON.',
                               console('auth/clients/create'))
        client_id, client_secret = web.get('client_id'), web.get('client_secret')
        project_id = web.get('project_id')
        redirect_uris = [u for u in web.get('redirect_uris', []) if isinstance(u, str)][:50]
        if web.get('token_uri') not in (None, 'https://oauth2.googleapis.com/token'):
            raise SetupProblem('client_json_invalid', 'The file names an unexpected token address; download it again from Google.')
    client_id = client_id.strip() if isinstance(client_id, str) else ''
    client_secret = client_secret.strip() if isinstance(client_secret, str) else ''
    match = CLIENT_ID.match(client_id)
    if not match:
        raise SetupProblem('client_id_invalid', 'The client ID should end in .apps.googleusercontent.com.')
    if not client_secret:
        raise SetupProblem('client_secret_missing',
                           'The secret is missing. Google shows it only once: open the client, add a new secret, and download the JSON again.',
                           console('auth/clients'))
    if len(client_secret) > 256 or not re.fullmatch(r'[\x21-\x7e]+', client_secret):
        raise SetupProblem('client_secret_invalid', 'The client secret does not look right.')
    if not isinstance(project_id, str) or not PROJECT_ID.match(project_id):
        project_id = None
    return {'client_id': client_id, 'client_secret': client_secret, 'project_id': project_id,
            'project_number': match.group(1), 'redirect_uris': redirect_uris}


def redirect_for(mode, origin, shell_path, relay_callback):
    """The exact return address the owner registers on the client."""
    if mode == 'relay':
        return relay_callback
    if not origin:
        return None
    return origin.rstrip('/') + shell_path + CALLBACK_PATH


def box_redirect_ok(origin):
    parts = urllib.parse.urlsplit(origin or '')
    if parts.scheme == 'https' and parts.hostname and '.' in parts.hostname:
        return True
    return parts.scheme == 'http' and parts.hostname in ('localhost', '127.0.0.1')


def authorized_domain(redirect_uri):
    """The domain Google wants under Branding > Authorized domains for this return address."""
    host = urllib.parse.urlsplit(redirect_uri or '').hostname or ''
    if host in ('localhost', '127.0.0.1') or '.' not in host:
        return None
    labels = host.split('.')
    # ts.net is a public suffix: each tailnet (tailXXXX.ts.net) is its own registrable domain.
    keep = 3 if host.endswith('.ts.net') else 2
    return '.'.join(labels[-keep:])


TESTING_REFRESH_LIMIT = 7 * 86400 + 600


def plan(state, *, redirect_uri, redirect_mode, box_ok, relay_callback):
    """Ordered steps for the setup screen and for the agent's walkthrough. Counted, never hidden."""
    client = state.get('client')
    project = client.get('project_id') if client else None
    checked = state.get('setup', {}).get('checked', {})
    accounts = list(state.get('accounts', {}).values())
    connected = any(a.get('status') == 'active' for a in accounts)
    # Google tells us the publishing status indirectly: Testing apps get 7-day refresh tokens.
    testing = any(a.get('refresh_expires') and a['refresh_expires'] - a.get('signed_in', 0) <= TESTING_REFRESH_LIMIT for a in accounts)
    published = bool(checked.get('publish')) or (connected and not testing)
    apis_ok = any(a.get('apis') and all(v != 'not_enabled' for v in a['apis'].values()) for a in accounts)
    domain = authorized_domain(redirect_uri)
    steps = [
        {'id': 'project', 'where': 'google', 'how': 'link',
         'title': 'Create a Google Cloud project',
         'detail': ['Sign in with the Google account that should own the setup.',
                    'If Google asks, accept the Cloud terms and pick your country.',
                    'Name it Wizard and press Create. Already have one for Wizard? Tick this step.'],
         'url': console('projectcreate'), 'copy': [{'label': 'Project name', 'value': APP_NAME}],
         'done': bool(checked.get('project') or client)},
        {'id': 'branding', 'where': 'google', 'how': 'manual',
         'title': 'Name your Google app',
         'detail': ['Press "Get started".', 'App name: Wizard. Support email: your own address.',
                    'Audience: External (Internal only if every account is in your Google Workspace).',
                    'Contact email: your own address. Agree, then Create.']
                   + ([f'Then open Branding and add {domain} under Authorized domains.'] if domain else []),
         'url': console('auth/overview', project),
         'copy': [{'label': 'App name', 'value': APP_NAME}] + ([{'label': 'Authorized domain', 'value': domain}] if domain else []),
         'done': bool(checked.get('branding') or client)},
        {'id': 'publish', 'where': 'google', 'how': 'manual',
         'title': 'Publish the app so sign-ins last',
         'detail': ['Under Audience press "Publish app", then Confirm.',
                    'While an External app is in Testing, Google ends every sign-in after 7 days.',
                    'Publishing does not make anything public. Google will warn you that the app is not verified; that is expected for your own app.',
                    'Internal (Workspace) apps skip this step.'],
         'url': console('auth/audience', project), 'done': published, 'detected': testing and not checked.get('publish') and 'testing'},
        {'id': 'client', 'where': 'google', 'how': 'manual',
         'title': 'Create the sign-in client',
         'detail': ['Application type: Web application. Name: Wizard.',
                    'Under "Authorized redirect URIs" press Add URI and paste the address below.',
                    'Press Create, then "Download JSON". Google shows the secret only once.'],
         'url': console('auth/clients/create', project),
         'copy': [{'label': 'Authorized redirect URI', 'value': redirect_uri or ''}],
         'done': bool(client)},
        {'id': 'paste', 'where': 'wizard', 'how': 'auto',
         'title': 'Give the client to Wizard',
         'detail': ['Upload the downloaded file or paste its contents. Wizard checks the ID and secret with Google straight away.'],
         'done': bool(client)},
        {'id': 'apis', 'where': 'google', 'how': 'link',
         'title': 'Turn on Gmail, Calendar, Drive, Contacts and YouTube',
         'detail': ['One page, already pointed at your project: press Enable.',
                    'Only the APIs you use matter; turning them on costs nothing.'],
         'url': enable_url(list(APIS), project), 'done': bool(checked.get('apis') or apis_ok)},
        {'id': 'connect', 'where': 'wizard', 'how': 'auto',
         'title': 'Connect your Google account',
         'detail': ['Google will say it has not verified the app. Choose Advanced, then "Go to Wizard (unsafe)". It is your own app.',
                    'Tick what Wizard may use. You can add more later.'],
         'done': connected},
    ]
    for number, step in enumerate(steps, 1):
        step['n'] = number
    current = next((s['id'] for s in steps if not s['done']), None)
    warnings = []
    if client and redirect_uri and client.get('redirect_uris') and redirect_uri not in client['redirect_uris']:
        warnings.append({'code': 'redirect_not_on_client', 'message': 'The client you pasted does not list this return address. Add it to the client in Google, then sign in.',
                         'value': redirect_uri, 'fix_url': client_url(client)})
    if redirect_mode == 'box' and not box_ok:
        warnings.append({'code': 'box_address_unusable', 'message': 'Google needs an https address. Open Wizard through its https address'
                         + (', or use the Wizard return page.' if relay_callback else '.'), 'fix_url': None})
    if testing and not checked.get('publish'):
        warnings.append({'code': 'testing_expiry', 'message': 'Your Google app is still in Testing, so Google will end these sign-ins after 7 days. Publish it, then reconnect.',
                         'fix_url': console('auth/audience', project)})
    return {'steps': steps, 'count': len(steps), 'google_steps': sum(s['where'] == 'google' for s in steps),
            'current': current, 'complete': current is None, 'warnings': warnings,
            'redirect': {'mode': redirect_mode, 'uri': redirect_uri, 'box_ok': box_ok, 'relay': relay_callback or None,
                         'authorized_domain': domain}}
