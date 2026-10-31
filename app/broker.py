"""The Google broker: the owner's own client, several accounts, and who may do what with them.

Only this app ever holds a Google token. Apps and agents get typed operations on the accounts
the owner granted them, and nothing else: no token, no raw URL, no other account.
"""
import base64
import copy
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
import urllib.parse

import google
import mailcal
import setup_flow
import gogrun
from quick import Quick, QuickProblem
from agent_access import AgentAccess, AccessRefused, Suggestions, SUGGESTION_TOOLS, DryRun, dry_call
from google import OPERATIONS, IDENTITY_SCOPES, SERVICES, GoogleError, BadArgs
from store import Store

FLOW_TTL = 600
SIGNIN_CACHE = 600
AUDIT_KEEP = 200
CLIENT = re.compile(r'^(app|agent):[a-z0-9][a-z0-9_-]{0,62}$')
ACCOUNT = re.compile(r'^g-[0-9a-f]{16}$')
ITEM = re.compile(r'^[a-z]{1,3}-[A-Za-z0-9_-]{8,40}$')


class Problem(Exception):
    def __init__(self, status, code, message, **extra):
        super().__init__(code)
        self.status, self.code, self.message, self.extra = status, code, message, extra

    def as_dict(self):
        return {'code': self.code, 'message': self.message, **self.extra}


def now():
    return int(time.time())


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


def new_id(prefix):
    return f'{prefix}-{secrets.token_urlsafe(12)}'


def b64json(segment):
    return json.loads(base64.urlsafe_b64decode(segment + '=' * (-len(segment) % 4)))


def client_label(client):
    kind, name = client.split(':', 1)
    if client == 'agent:main':
        return 'Main agent'
    return (name.replace('-', ' ').replace('_', ' ').capitalize()) + (' agent' if kind == 'agent' else '')


class Broker:
    def __init__(self, data_dir, *, transport=None, store_key=None, relay_callback='', signin_service='', clock=time.time,
                 gog=None, composio_transport=None):
        self.store = Store(data_dir, store_key)
        self.state = self.store.load()
        # The owner's choice of what agents may do with mail (Mail > Settings > Agent access). Draft only by default.
        self.access = AgentAccess(data_dir)
        # Calendar changes agents suggested at Suggest only, waiting for the owner's Approve / Dismiss in Calendar.
        self.suggestions = Suggestions(data_dir)
        # Wizard's Google app: a stateless sign-in service holds the shared client's secret and the
        # fixed return page. Sign-ins, tokens and every Google call stay on this box.
        self.signin_service = (signin_service or '').rstrip('/')
        host = urllib.parse.urlsplit(self.signin_service).hostname if self.signin_service else None
        self.transport = transport or google.Transport(extra_hosts=[host] if host else [])
        self.relay_callback = relay_callback
        self.gog = gog or gogrun
        self._signin = {'at': 0, 'value': None}
        self.clock = clock
        self.lock = threading.RLock()
        self.account_locks = {}
        self.flows = {}  # sha(state) -> pending sign-in; memory only, 10 minutes
        # Quick connect (Composio): Gmail and Calendar without a Google Cloud project.
        self.quick = Quick(self, transport=composio_transport, clock=clock)

    # ---------- state ----------

    def _commit(self, state):
        self.store.save(state)
        self.state = state

    def _copy(self):
        return copy.deepcopy(self.state)

    def _account_lock(self, account):
        with self.lock:
            return self.account_locks.setdefault(account, threading.RLock())

    def _audit(self, state, client, account, operation, outcome):
        state['audit'] = (state.get('audit', []) + [{'at': now(), 'client': client, 'account': account,
                                                      'operation': operation, 'outcome': outcome}])[-AUDIT_KEEP:]

    # ---------- owner client ----------

    def client_summary(self):
        client = self.state.get('client')
        if not client:
            return None
        if client.get('mode') == 'wizard':
            return {'mode': 'wizard', 'client_id': client['client_id'], 'project_id': None, 'project_number': None,
                    'secret_hint': None, 'redirect_uris': [client['redirect_uri']], 'checked': 'ok', 'console_url': None}
        return {'mode': 'own', 'client_id': client['client_id'], 'project_id': client.get('project_id'), 'project_number': client.get('project_number'),
                'secret_hint': client['client_secret'][:7] + '…', 'redirect_uris': client.get('redirect_uris', []),
                'checked': client.get('checked'), 'console_url': setup_flow.client_url(client)}

    def probe_client(self, client, redirect_uri):
        """Ask Google whether the ID and secret are real, without anyone signing in.

        A made-up code is refused either way; the kind of refusal says what is wrong: invalid_client
        (unknown client or wrong secret) or invalid_grant (client fine, code bad). Google does not check
        the return address here, so that is only proven by the first real sign-in."""
        try:
            self.transport.request('POST', google.TOKEN_URL, form={
                'grant_type': 'authorization_code', 'code': 'wizard-setup-check', 'client_id': client['client_id'],
                'client_secret': client['client_secret'], 'redirect_uri': redirect_uri or 'https://localhost/'})
        except GoogleError as error:
            if error.code == 'reauth_required':  # invalid_grant: the client authenticated; only the code was bad
                return {'result': 'ok'}
            if error.code in ('client_invalid', 'client_deleted'):
                raise Problem(400, 'client_invalid', 'Google does not accept this client ID and secret. Download the JSON again, or add a new secret.',
                              fix_url=setup_flow.client_url(client)) from None
            return {'result': 'unknown', 'detail': error.code}
        return {'result': 'unknown'}

    def save_client(self, pasted=None, client_id=None, client_secret=None, redirect_uri=None):
        try:
            client = setup_flow.parse_client(pasted, client_id, client_secret)
        except setup_flow.SetupProblem as problem:
            raise Problem(400, problem.code, problem.message, fix_url=problem.fix_url) from None
        previous = self.state.get('client')
        if previous and self.state['accounts'] and (previous.get('mode') == 'wizard' or previous['client_id'] != client['client_id']):
            # Tokens belong to the client that issued them; a different client cannot refresh them.
            raise Problem(409, 'client_in_use', 'Disconnect your Google accounts before switching to a different client.')
        check = self.probe_client(client, redirect_uri)
        client.update(added=now(), checked=check['result'])
        with self.lock:
            state = self._copy()
            previous = state.get('client')
            if previous and previous['client_id'] != client['client_id'] and state['accounts']:
                # Tokens belong to the client that issued them; a different client cannot refresh them.
                raise Problem(409, 'client_in_use', 'Disconnect your Google accounts before switching to a different client.')
            state['client'] = client
            self._commit(state)
        return {'client': self.client_summary(), 'check': check}

    def forget_client(self):
        with self.lock:
            if self.state['accounts']:
                raise Problem(409, 'client_in_use', 'Disconnect your Google accounts first.')
            state = self._copy()
            state['client'] = None
            self._commit(state)
        return {'forgotten': True}

    # ---------- Wizard's Google app (shared client, no Google Cloud setup) ----------

    def wizard_signin(self, fresh=False):
        """The shared client the sign-in service offers, or None when it is not available.

        Only public values cross: the client ID and the fixed return page. The secret stays with the service."""
        if not self.signin_service:
            return None
        stamp = self.clock()
        if not fresh and stamp - self._signin['at'] < (SIGNIN_CACHE if self._signin['value'] else 60):
            return self._signin['value']
        value = None
        try:
            answer = self.transport.request('GET', self.signin_service + '/v1/client')
            client_id, redirect = answer.get('client_id'), answer.get('redirect_uri')
            service_host = urllib.parse.urlsplit(self.signin_service).hostname
            if (isinstance(client_id, str) and setup_flow.CLIENT_ID.match(client_id) and isinstance(redirect, str)
                    and redirect.startswith('https://') and urllib.parse.urlsplit(redirect).hostname == service_host):
                value = {'client_id': client_id, 'redirect_uri': redirect, 'name': str(answer.get('name') or 'Wizard')[:60],
                         'verified': answer.get('verified') is True}
        except GoogleError:
            value = None
        self._signin = {'at': stamp, 'value': value}
        return value

    def use_wizard_client(self):
        """Switch this box to Wizard's Google app. Only before any account is connected: tokens belong to their client."""
        offered = self.wizard_signin(fresh=True)
        if not offered:
            raise Problem(503, 'wizard_signin_unavailable', 'Quick sign-in is not available right now. Use your own Google client instead, or try again later.')
        with self.lock:
            current = self.state.get('client')
            if current and current.get('mode') == 'wizard' and current['client_id'] == offered['client_id']:
                return {'client': self.client_summary()}
            if current and self.state['accounts']:
                raise Problem(409, 'client_in_use', 'Disconnect your Google accounts before changing how Wizard signs in.')
            state = self._copy()
            state['client'] = {'mode': 'wizard', 'client_id': offered['client_id'], 'redirect_uri': offered['redirect_uri'],
                               'service': self.signin_service, 'added': now()}
            self._commit(state)
        return {'client': self.client_summary()}

    def _mode(self):
        client = self.state.get('client')
        return (client or {}).get('mode', 'own') if client else None

    def _token_request(self, form):
        """POST to Google's token endpoint with the right client authentication.

        Own client: the secret is on this box. Wizard's Google app: the sign-in service adds the shared
        secret and passes Google's answer straight back; it keeps nothing."""
        client = self.state['client']
        form = {**form, 'client_id': client['client_id']}
        if client.get('mode') == 'wizard':
            return self.transport.request('POST', client.get('service', self.signin_service) + '/v1/token', form=form)
        return self.transport.request('POST', google.TOKEN_URL, form={**form, 'client_secret': client['client_secret']})

    def set_setup(self, step=None, done=None, redirect_mode=None):
        with self.lock:
            state = self._copy()
            if step is not None:
                if step not in ('project', 'branding', 'publish', 'apis') or not isinstance(done, bool):
                    raise Problem(400, 'invalid_step', 'Unknown setup step.')
                state['setup']['checked'][step] = done
            if redirect_mode is not None:
                if redirect_mode not in ('box', 'relay') or (redirect_mode == 'relay' and not self.relay_callback):
                    raise Problem(400, 'invalid_redirect_mode', 'Unknown return address mode.')
                state['setup']['redirect_mode'] = redirect_mode
            self._commit(state)
        return {'setup': self.state['setup']}

    def redirect_uri(self, origin, shell_path):
        client = self.state.get('client')
        if client and client.get('mode') == 'wizard':
            return client['redirect_uri']
        mode = self.state['setup'].get('redirect_mode', 'box')
        return setup_flow.redirect_for(mode, origin, shell_path, self.relay_callback)

    def setup_plan(self, origin, shell_path):
        client = self.state.get('client')
        offered = self.wizard_signin() if not client else None
        if (client and client.get('mode') == 'wizard') or (not client and offered):
            connected = any(a.get('status') == 'active' for a in self.state['accounts'].values())
            step = {'id': 'connect', 'n': 1, 'where': 'wizard', 'how': 'auto', 'title': 'Connect your Google account',
                    'detail': ['Choose your Google account and press Allow.'], 'done': connected}
            return {'mode': 'wizard', 'steps': [step], 'count': 1, 'google_steps': 0, 'current': None if connected else 'connect',
                    'complete': connected, 'warnings': [], 'wizard_available': True,
                    'redirect': {'mode': 'wizard', 'uri': (client or offered)['redirect_uri'], 'box_ok': setup_flow.box_redirect_ok(origin),
                                 'relay': None, 'authorized_domain': None}}
        mode = self.state['setup'].get('redirect_mode', 'box')
        plan = setup_flow.plan(self.state, redirect_uri=self.redirect_uri(origin, shell_path), redirect_mode=mode,
                               box_ok=setup_flow.box_redirect_ok(origin), relay_callback=self.relay_callback)
        plan['mode'] = 'own'
        plan['wizard_available'] = bool(offered) if not client else bool(self.signin_service)
        return plan

    # ---------- sign-in ----------

    def start(self, session, origin, shell_path, account=None, operations=(), services=None):
        client = self.state.get('client')
        if not client and self.wizard_signin():
            self.use_wizard_client()
            client = self.state.get('client')
        if not client:
            raise Problem(409, 'client_required', 'Finish setup first: Wizard needs your Google client.')
        wizard = client.get('mode') == 'wizard'
        mode = 'wizard' if wizard else self.state['setup'].get('redirect_mode', 'box')
        if mode == 'box' and not setup_flow.box_redirect_ok(origin):
            raise Problem(409, 'box_address_unusable', 'Google needs an https address for this box. Open Wizard through its https address.')
        if mode in ('relay', 'wizard') and not (origin or '').startswith('https://'):
            raise Problem(409, 'box_address_unusable', 'Open Wizard through its https address to sign in to Google.')
        redirect_uri = self.redirect_uri(origin, shell_path)
        unknown = [op for op in operations if op not in OPERATIONS]
        if unknown:
            raise Problem(400, 'unknown_operation', f'Unknown operation: {unknown[0]}')
        if services is None:
            services = [] if operations else list(google.DEFAULT_SERVICES)
        if not isinstance(services, list) or any(s not in SERVICES for s in services):
            raise Problem(400, 'unknown_service', 'Unknown Google service.')
        login_hint = None
        if account is not None:
            current = self.state['accounts'].get(account)
            if not current:
                raise Problem(404, 'account_not_found', 'Account not found.')
            login_hint = current.get('email') or None
        scopes = set(IDENTITY_SCOPES) | {OPERATIONS[op]['scope'] for op in operations}
        for service in services:
            scopes |= set(SERVICES[service]['scopes'])
        nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(64)
        state = secrets.token_urlsafe(32)
        if mode in ('relay', 'wizard'):
            # The return page needs to know where to send the browser. It cannot use the code:
            # exchanging it needs this box's PKCE verifier (and, for an own client, its secret).
            back = origin.rstrip('/') + shell_path + setup_flow.CALLBACK_PATH
            state = 'wg1.' + base64.urlsafe_b64encode(json.dumps({'n': state, 'r': back}, separators=(',', ':')).encode()).rstrip(b'=').decode()
        with self.lock:
            stamp = self.clock()
            self.flows = {k: v for k, v in self.flows.items() if v['expires'] > stamp and v['session'] != session}
            if len(self.flows) >= 32:
                raise Problem(429, 'signin_busy', 'Too many sign-ins in progress.')
            self.flows[sha(state)] = {'session': session, 'verifier': verifier, 'nonce': nonce, 'redirect_uri': redirect_uri,
                                      'account': account, 'scopes': sorted(scopes), 'services': sorted(set(services)),
                                      'expires': stamp + FLOW_TTL, 'status': 'pending', 'result': None, 'started': stamp}
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        params = {'client_id': client['client_id'], 'redirect_uri': redirect_uri, 'response_type': 'code',
                  'scope': ' '.join(sorted(scopes)), 'state': state, 'nonce': nonce, 'access_type': 'offline',
                  'include_granted_scopes': 'true', 'prompt': 'consent' if account else 'consent select_account',
                  'code_challenge': challenge, 'code_challenge_method': 'S256'}
        if login_hint:
            params['login_hint'] = login_hint
        return {'authorization_url': google.AUTH_URL + '?' + urllib.parse.urlencode(params), 'expires_in': FLOW_TTL,
                'redirect_uri': redirect_uri}

    def flow_status(self, session):
        with self.lock:
            mine = [f for f in self.flows.values() if f['session'] == session]
        if not mine:
            return {'status': 'none'}
        latest = max(mine, key=lambda f: f['started'])
        return {'status': latest['status'], 'result': latest['result']}

    def cancel(self, session):
        with self.lock:
            for flow in self.flows.values():
                if flow['session'] == session and flow['status'] == 'pending':
                    flow['status'], flow['result'] = 'cancelled', {'code': 'cancelled'}
        return {'cancelled': True}

    def _claims(self, id_token, client_id, nonce):
        """ID token received straight from Google's token endpoint over TLS (OpenID Connect Core 3.1.3.7):
        the TLS server check stands in for the signature; every claim is still checked."""
        try:
            header, payload, _ = id_token.split('.')
            claims = b64json(payload)
            if b64json(header).get('alg') != 'RS256':
                raise ValueError
        except (ValueError, AttributeError):
            raise Problem(502, 'identity_invalid', 'Google returned an unreadable identity.') from None
        stamp = self.clock()
        audience = claims.get('aud')
        if (claims.get('iss') not in ('https://accounts.google.com', 'accounts.google.com')
                or (audience != client_id and not (isinstance(audience, list) and client_id in audience))
                or claims.get('azp', client_id) != client_id
                or not isinstance(claims.get('exp'), (int, float)) or claims['exp'] < stamp - 60
                or not hmac.compare_digest(str(claims.get('nonce', '')), nonce)
                or not isinstance(claims.get('sub'), str) or not 1 <= len(claims['sub']) <= 255):
            raise Problem(502, 'identity_invalid', 'Google returned an identity for a different sign-in.')
        return claims

    def finish(self, state, session, code=None, error=None):
        if not isinstance(state, str) or len(state) > 1024:
            raise Problem(400, 'invalid_state', 'This sign-in link is not valid. Start again from Wizard.')
        with self.lock:
            flow = self.flows.get(sha(state))
            if not flow or flow['status'] != 'pending' or flow['expires'] <= self.clock() or not hmac.compare_digest(flow['session'], session):
                raise Problem(400, 'invalid_state', 'This sign-in expired or was started in another browser. Start again from Wizard.')
            flow['status'] = 'exchanging'  # one use, whatever happens next
        try:
            result = self._finish(flow, code, error)
            flow['status'], flow['result'] = 'connected', result
            return result
        except Problem as problem:
            flow['status'], flow['result'] = 'failed', problem.as_dict()
            raise

    def _finish(self, flow, code, error):
        if error:
            raise Problem(400, 'consent_declined', 'Google sign-in was cancelled.')
        if not isinstance(code, str) or not 1 <= len(code) <= 4096:
            raise Problem(400, 'invalid_code', 'Google did not return a sign-in code.')
        client = self.state['client']
        if not client:
            raise Problem(409, 'client_required', 'Setup was reset during sign-in.')
        try:
            value = self._token_request({'grant_type': 'authorization_code', 'code': code, 'redirect_uri': flow['redirect_uri'],
                                         'code_verifier': flow['verifier']})
        except GoogleError as failure:
            raise Problem(failure.status, failure.code, failure.message) from None
        if str(value.get('token_type', '')).lower() != 'bearer' or not isinstance(value.get('access_token'), str):
            raise Problem(502, 'invalid_provider_grant', 'Google returned an unexpected answer.')
        claims = self._claims(value.get('id_token', ''), client['client_id'], flow['nonce'])
        account = 'g-' + sha(claims['sub'])[:16]
        granted = google.normal_scopes(value.get('scope'))
        lifetime = value.get('expires_in')
        if not isinstance(lifetime, (int, float)) or not 0 < lifetime <= 86400:
            raise Problem(502, 'invalid_provider_grant', 'Google returned an unexpected answer.')
        # Lock order everywhere: account lock first, then the state lock.
        with self._account_lock(account), self.lock:
            if flow['account'] and flow['account'] != account:
                raise Problem(409, 'wrong_account', 'You signed in with a different Google account. Choose the same account to reconnect it.')
            state = self._copy()
            previous = state['accounts'].get(account, {})
            refresh = value.get('refresh_token') or previous.get('refresh_token')
            if not refresh:
                raise Problem(502, 'no_refresh_token', 'Google did not allow offline access. Remove Wizard from your Google account permissions and connect again.',
                              fix_url='https://myaccount.google.com/connections')
            stamp = now()
            # Services the owner ticked on this sign-in come on if Google granted them; earlier choices stay.
            services = set(previous.get('services', [])) | {sv for sv in flow.get('services', []) if google.service_granted(granted, sv)}
            state['accounts'][account] = {
                'id': account, 'sub': claims['sub'],
                'email': str(claims.get('email', ''))[:254] if claims.get('email_verified', True) else '',
                'name': str(claims.get('name', ''))[:120], 'picture': None,
                'status': 'active', 'scopes': sorted(granted),  # include_granted_scopes: Google reports the full set
                'access_token': value['access_token'], 'expires': self.clock() + lifetime, 'refresh_token': refresh,
                'refresh_expires': self.clock() + value['refresh_token_expires_in'] if isinstance(value.get('refresh_token_expires_in'), (int, float)) else None,
                'added': previous.get('added', stamp), 'signed_in': stamp, 'error': None, 'apis': previous.get('apis', {}),
                'services': sorted(services), 'label': previous.get('label', '')}
            self._commit(state)
        missing = sorted(set(flow['scopes']) - granted - set(IDENTITY_SCOPES))
        return {'connected': True, 'account': account, 'email': state['accounts'][account]['email'],
                'declined': [op for op, spec in OPERATIONS.items() if spec['scope'] in missing],
                'declined_services': [sv for sv in flow.get('services', []) if not google.service_granted(granted, sv)]}

    # ---------- accounts ----------

    def account_view(self, account):
        a = self.state['accounts'][account]
        allowed = [op for op, spec in OPERATIONS.items() if google.has_scope(a['scopes'], spec['scope'])]
        on = set(a.get('services', []))
        services = [{'id': sv, 'label': spec['label'], 'what': spec['what'], 'granted': google.service_granted(a['scopes'], sv),
                     'on': sv in on and google.service_granted(a['scopes'], sv)} for sv, spec in SERVICES.items()]
        return {'id': a['id'], 'email': a['email'], 'name': a.get('name', ''), 'label': a.get('label', ''), 'status': a['status'],
                'error': a.get('error'), 'allowed': allowed, 'services': services, 'apis': a.get('apis', {}), 'added': a.get('added'),
                'signed_in': a.get('signed_in'), 'refresh_expires': a.get('refresh_expires')}

    def find_account(self, value):
        if not isinstance(value, str):
            raise Problem(400, 'invalid_account', 'Name the account by its email or id.')
        value = value.strip().lower()
        for a in self.state['accounts'].values():
            if a['id'] == value or (a['email'] and a['email'].lower() == value):
                return a['id']
        raise Problem(404, 'account_not_found', 'No connected Google account matches that.')

    def disconnect(self, account):
        with self._account_lock(account), self.lock:
            current = self.state['accounts'].get(account)
            if not current:
                raise Problem(404, 'account_not_found', 'Account not found.')
            token = current.get('refresh_token') or current.get('access_token')
            state = self._copy()
            del state['accounts'][account]
            state['grants'] = {k: g for k, g in state['grants'].items() if g['account'] != account}
            # Cancel sign-ins already on their way back, so none can bring this account back.
            for flow in self.flows.values():
                if flow['status'] == 'pending':
                    flow['status'], flow['result'] = 'cancelled', {'code': 'cancelled'}
            self._commit(state)
        revoked = False
        if token:
            try:
                self.transport.request('POST', google.REVOKE_URL, form={'token': token})
                revoked = True
            except GoogleError:
                pass
        return {'disconnected': True, 'revoked_at_google': revoked,
                'manual_revoke_url': None if revoked else 'https://myaccount.google.com/connections'}

    def _token(self, account):
        """A fresh access token for the account. Caller holds the account lock."""
        a = self.state['accounts'].get(account)
        if not a or a['status'] != 'active':
            raise Problem(409, 'reauth_required', 'This Google account needs to be connected again.', account=account)
        if a['expires'] > self.clock() + 60:
            return a['access_token']
        client = self.state['client']
        try:
            value = self._token_request({'grant_type': 'refresh_token', 'refresh_token': a['refresh_token']})
        except GoogleError as failure:
            if failure.code in ('reauth_required', 'client_invalid'):
                self._mark(account, 'reauth_required', failure.code)
                hint = None
                if failure.code == 'reauth_required' and client.get('mode') != 'wizard' and not self.state['setup']['checked'].get('publish'):
                    hint = 'If your Google app is still in Testing, Google ends sign-ins after 7 days. Publish it, then reconnect.'
                raise Problem(409, 'reauth_required', hint or 'Google ended this sign-in. Connect the account again.', account=account,
                              fix_url=setup_flow.console('auth/audience', client.get('project_id')) if hint else None) from None
            raise Problem(failure.status, failure.code, failure.message) from None
        if not isinstance(value.get('access_token'), str) or not isinstance(value.get('expires_in'), (int, float)):
            raise Problem(502, 'invalid_provider_grant', 'Google returned an unexpected answer.')
        with self.lock:
            state = self._copy()
            target = state['accounts'][account]
            target.update(access_token=value['access_token'], expires=self.clock() + value['expires_in'], error=None)
            if value.get('scope'):
                target['scopes'] = sorted(google.normal_scopes(value['scope']))
            if value.get('refresh_token'):
                # Persist a rotated refresh token before anything else uses the new access token.
                target['refresh_token'] = value['refresh_token']
            self._commit(state)
        return value['access_token']

    def _mark(self, account, status, error):
        with self.lock:
            state = self._copy()
            if account in state['accounts']:
                state['accounts'][account].update(status=status, error=error, access_token='')
                self._commit(state)

    def check_apis(self, account):
        """Try one cheap read per API so the owner sees "turn on the Calendar API" before an app trips on it."""
        if account not in self.state['accounts']:
            raise Problem(404, 'account_not_found', 'Account not found.')
        project = (self.state['client'] or {}).get('project_id')
        results = {}
        with self._account_lock(account):
            token = self._token(account)
            scopes = set(self.state['accounts'][account]['scopes'])
            for api, (operation, url) in google.API_PROBES.items():
                if not google.has_scope(scopes, OPERATIONS[operation]['scope']):
                    results[api] = {'status': 'not_allowed'}
                    continue
                try:
                    self.transport.request('GET', url, token=token)
                    results[api] = {'status': 'ok'}
                except GoogleError as failure:
                    if failure.code == 'api_not_enabled':
                        results[api] = {'status': 'not_enabled', 'fix_url': None if self._mode() == 'wizard' else setup_flow.enable_url([api], project)}
                    elif failure.code == 'google_permission_missing':
                        results[api] = {'status': 'not_allowed'}
                    else:
                        results[api] = {'status': 'error', 'code': failure.code}
        with self.lock:
            state = self._copy()
            if account in state['accounts']:
                state['accounts'][account]['apis'] = {k: v['status'] for k, v in results.items()}
                self._commit(state)
        return {'account': account, 'apis': results}

    # ---------- grants ----------

    def set_grant(self, client, account, operations):
        if not isinstance(client, str) or not CLIENT.match(client):
            raise Problem(400, 'invalid_client', 'Name an app (app:<id>) or an agent (agent:<id>).')
        if account not in self.state['accounts']:
            raise Problem(404, 'account_not_found', 'Account not found.')
        if not isinstance(operations, list) or any(op not in OPERATIONS for op in operations):
            raise Problem(400, 'unknown_operation', 'Unknown operation.')
        with self.lock:
            state = self._copy()
            existing = next((g for g in state['grants'].values() if g['client'] == client and g['account'] == account), None)
            if not operations:
                if existing:
                    del state['grants'][existing['id']]
                self._commit(state)
                return {'grant': None}
            grant = existing or {'id': new_id('gr'), 'client': client, 'account': account, 'created': now()}
            grant.update(operations=sorted(set(operations)), updated=now())
            state['grants'][grant['id']] = grant
            for request in state['requests'].values():
                if request['client'] == client and request['status'] == 'pending' and set(request['operations']) <= set(operations) \
                        and request.get('account') in (None, account):
                    request.update(status='approved', decided=now())
            self._commit(state)
        return {'grant': grant}

    def revoke_grant(self, grant_id):
        with self.lock:
            if grant_id not in self.state['grants']:
                raise Problem(404, 'grant_not_found', 'Grant not found.')
            state = self._copy()
            del state['grants'][grant_id]
            self._commit(state)
        return {'revoked': True}

    def grants_for(self, client):
        return [g for g in self.state['grants'].values() if g['client'] == client]

    # ---------- access requests (apps and agents ask, the owner decides) ----------

    def request_access(self, client, operations, account=None, reason=''):
        if not isinstance(operations, list) or not operations or any(op not in OPERATIONS for op in operations):
            raise Problem(400, 'unknown_operation', 'Ask for one or more known operations.')
        if account is not None:
            account = self.find_account(account)
        reason = reason.strip()[:200] if isinstance(reason, str) else ''
        with self.lock:
            state = self._copy()
            pending = [r for r in state['requests'].values() if r['status'] == 'pending']
            same = next((r for r in pending if r['client'] == client and r.get('account') == account and set(r['operations']) == set(operations)), None)
            if same:
                return {'request': same}
            if sum(r['client'] == client for r in pending) >= 5:
                raise Problem(429, 'too_many_requests', 'This caller already has requests waiting for the owner.')
            request = {'id': new_id('rq'), 'client': client, 'account': account, 'operations': sorted(set(operations)),
                       'reason': reason, 'status': 'pending', 'created': now()}
            state['requests'][request['id']] = request
            state['requests'] = dict(sorted(state['requests'].items(), key=lambda kv: kv[1]['created'])[-100:])
            self._commit(state)
        return {'request': request}

    def decide(self, request_id, approve, account=None):
        with self.lock:
            request = self.state['requests'].get(request_id)
            if not request or request['status'] != 'pending':
                raise Problem(404, 'request_not_found', 'This request is no longer waiting.')
            if approve:
                target = account or request.get('account')
                if target not in self.state['accounts']:
                    raise Problem(400, 'account_required', 'Choose which Google account to share.')
                current = next((g['operations'] for g in self.grants_for(request['client']) if g['account'] == target), [])
                self.set_grant(request['client'], target, sorted(set(current) | set(request['operations'])))
            state = self._copy()
            state['requests'][request_id].update(status='approved' if approve else 'declined', decided=now())
            self._commit(state)
        return {'request': self.state['requests'][request_id]}

    # ---------- app access keys (apps installed before Wizard grants exist) ----------

    def create_key(self, client):
        if not isinstance(client, str) or not CLIENT.match(client) or not client.startswith('app:'):
            raise Problem(400, 'invalid_client', 'Keys are for apps (app:<id>).')
        secret = 'wgk_' + secrets.token_urlsafe(32)
        key = {'id': new_id('ky'), 'client': client, 'hash': sha(secret), 'created': now()}
        with self.lock:
            state = self._copy()
            state['keys'][key['id']] = key
            self._commit(state)
        return {'id': key['id'], 'client': client, 'key': secret, 'shown_once': True}

    def revoke_key(self, key_id):
        with self.lock:
            if key_id not in self.state['keys']:
                raise Problem(404, 'key_not_found', 'Key not found.')
            state = self._copy()
            del state['keys'][key_id]
            self._commit(state)
        return {'revoked': True}

    def client_for_key(self, secret):
        digest = sha(secret)
        for key in self.state['keys'].values():
            if hmac.compare_digest(key['hash'], digest):
                return key['client']
        return None

    # ---------- calls ----------

    def allowed(self, client, operation):
        return sorted(g['account'] for g in self.grants_for(client) if operation in g['operations'])

    def call(self, client, operation, args=None, account=None):
        """Run one operation for an app or agent, on an account the owner granted it."""
        if operation not in OPERATIONS:
            raise Problem(400, 'unknown_operation', 'Unknown operation.')
        args = args if isinstance(args, dict) else {}
        if account is None:
            granted = self.allowed(client, operation)
            if len(granted) != 1:
                raise Problem(403 if not granted else 400, 'grant_required' if not granted else 'account_required',
                              'The owner has not allowed this yet. Ask with google_request_access.' if not granted
                              else 'Several accounts are allowed; name one.',
                              accounts=[self.state['accounts'][a]['email'] for a in granted])
            account = granted[0]
        else:
            account = self.find_account(account)
        grant = next((g for g in self.grants_for(client) if g['account'] == account), None)
        if not grant or operation not in grant['operations']:
            self._record(client, account, operation, 'denied')
            raise Problem(403, 'grant_required', 'The owner has not allowed this for that account. Ask with google_request_access.')
        spec = OPERATIONS[operation]
        with self._account_lock(account):
            if not google.has_scope(self.state['accounts'][account]['scopes'], spec['scope']):
                raise Problem(403, 'google_permission_missing', 'Google has not been allowed this for the account yet. The owner can add it in the Google app.')
            token = self._token(account)
            retried = False

            def request(method, url, **kwargs):
                nonlocal token, retried
                try:
                    return self.transport.request(method, url, token=token, **kwargs)
                except GoogleError as failure:
                    if failure.code == 'token_rejected' and not retried:
                        retried = True
                        self._expire(account)
                        token = self._token(account)
                        return self.transport.request(method, url, token=token, **kwargs)
                    raise

            try:
                result = google.HANDLERS[operation](request, args)
            except BadArgs as bad:
                raise Problem(400, 'invalid_arguments', str(bad)) from None
            except GoogleError as failure:
                self._record(client, account, operation, failure.code)
                extra = {}
                if failure.code == 'api_not_enabled':
                    extra['fix_url'] = setup_flow.enable_url([failure.service or spec['api']], (self.state['client'] or {}).get('project_id'))
                raise Problem(failure.status, failure.code, failure.message, **extra) from None
        self._record(client, account, operation, 'ok')
        return {'account': self.state['accounts'][account]['email'], 'operation': operation, **result}

    def _expire(self, account):
        with self.lock:
            self.state['accounts'][account]['expires'] = 0

    def _record(self, client, account, operation, outcome):
        with self.lock:
            state = self._copy()
            self._audit(state, client, account, operation, outcome)
            self._commit(state)

    # ---------- service switches (what the owner's agents may use, per account) ----------

    def set_service(self, account, service, on):
        if service not in SERVICES or not isinstance(on, bool):
            raise Problem(400, 'unknown_service', 'Unknown Google service.')
        with self.lock:
            current = self.state['accounts'].get(account)
            if not current:
                raise Problem(404, 'account_not_found', 'Account not found.')
            if on and not google.service_granted(current['scopes'], service):
                # Google has not been asked for this yet: the page sends the owner to Google for just this service.
                raise Problem(409, 'consent_needed', f'Google needs your OK for {SERVICES[service]["label"]} first.', service=service, account=account)
            state = self._copy()
            services = set(state['accounts'][account].get('services', []))
            services.add(service) if on else services.discard(service)
            state['accounts'][account]['services'] = sorted(services)
            self._commit(state)
        return {'account': self.account_view(account)}

    def set_label(self, account, label):
        if not isinstance(label, str) or len(label.strip()) > 40:
            raise Problem(400, 'invalid_label', 'Use a short name, like Work or Personal.')
        with self.lock:
            if account not in self.state['accounts']:
                raise Problem(404, 'account_not_found', 'Account not found.')
            state = self._copy()
            state['accounts'][account]['label'] = label.strip()
            self._commit(state)
        return {'account': self.account_view(account)}

    def _services_on(self, account):
        a = self.state['accounts'][account]
        return [sv for sv in a.get('services', []) if sv in SERVICES and google.service_granted(a['scopes'], sv)]

    def _roots(self, account):
        return {root for sv in self._services_on(account) for root in SERVICES[sv]['commands']}

    def _pick_account(self, value, root=None):
        """The account an agent means: named by email/label/id, or the only one with that service on."""
        accounts = self.state['accounts']
        if value not in (None, ''):
            if not isinstance(value, str):
                raise Problem(400, 'invalid_account', 'Name the account by its email.')
            wanted = value.strip().lower()
            for a in accounts.values():
                if wanted in (a['id'], (a.get('email') or '').lower(), (a.get('label') or '').lower()):
                    return a['id']
            raise Problem(404, 'account_not_found', 'No connected Google account matches that.', accounts=[a['email'] for a in accounts.values()])
        candidates = [a for a in accounts if a in self.state['accounts'] and (root is None or root in self._roots(a))]
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            raise Problem(403, 'service_off', 'No connected Google account has this switched on. The owner can switch it on in the Google app.',
                          app_link='/wizard-os-home/?open=wizard-google')
        raise Problem(400, 'account_required', 'Several accounts can do this; name one with "account".',
                      accounts=[accounts[a]['email'] for a in candidates])

    def run_gog(self, caller, args, account=None):
        """Run one gog command for an agent on one account, limited to the services the owner switched on."""
        if not self.state.get('client') or not self.state['accounts']:
            raise Problem(409, 'not_connected', 'No Google account is connected yet. The owner connects one in the Google app.',
                          app_link='/wizard-os-home/?open=wizard-google')
        root = None
        if isinstance(args, list) and args and isinstance(args[0], str):
            root = self.gog.ALIASES.get(args[0], args[0])
        account = self._pick_account(account, root)
        roots = self._roots(account)
        try:
            self.gog.check_args(args, roots)
        except self.gog.RunProblem as problem:
            self._record(caller, account, f'gog:{root or "?"}', problem.code)
            raise Problem(403 if problem.code == 'service_off' else 400, problem.code, problem.message) from None
        try:
            extra = self.access.gog_flags(caller, root)  # gmail for agents: gog itself refuses sending (and more below Draft only)
        except AccessRefused as refused:
            self._record(caller, account, f'gog:{root}', refused.code)
            raise Problem(refused.status, refused.code, refused.message) from None
        kw = {'extra': extra} if extra else {}
        # Only the token fetch holds the account lock; gog itself runs unlocked so calls can overlap.
        with self._account_lock(account):
            token = self._token(account)
        try:
            result = self.gog.run(args, token, roots, **kw)
            if result['exit_code'] == 4:  # gog: auth_required. One retry with a fresh token.
                with self._account_lock(account):
                    self._expire(account)
                    token = self._token(account)
                result = self.gog.run(args, token, roots, **kw)
        except self.gog.RunProblem as problem:
            self._record(caller, account, f'gog:{root}', problem.code)
            raise Problem(502 if problem.code in ('gog_missing', 'timeout') else 400, problem.code, problem.message) from None
        self._record(caller, account, f'gog:{" ".join(a for a in args[:2] if not a.startswith("-"))}', 'ok' if result['exit_code'] == 0 else f'exit {result["exit_code"]}')
        return {'account': self.state['accounts'][account]['email'], **result}

    def gog_help(self, command=''):
        roots = set()
        for account in self.state['accounts']:
            roots |= self._roots(account)
        try:
            return self.gog.help_for(command, roots)
        except self.gog.RunProblem as problem:
            raise Problem(400, problem.code, problem.message) from None

    def agent_status(self):
        """What the owner's agents can use. Never a token."""
        client = self.state.get('client')
        return {'connected': (bool(client) and any(a['status'] == 'active' for a in self.state['accounts'].values()))
                or any(a['status'] == 'active' for a in self.quick.section()['accounts'].values()),
                'sign_in': (client or {}).get('mode', 'own') if client else None,
                'mail_access': self.access.agent_view(),
                'calendar_access': self.access.calendar_agent_view(),
                'accounts': [{'email': a['email'], 'label': a.get('label', ''), 'status': a['status'],
                              'services': self._services_on(a['id']),
                              'commands': sorted(self._roots(a['id'])), 'via': 'own-client'} for a in self.state['accounts'].values()]
                            + [{'email': a['email'], 'label': a.get('label', ''), 'status': a['status'], 'services': self.quick.services_on(a),
                                'commands': [], 'via': 'composio'} for a in self.quick.section()['accounts'].values()],
                'services': {sv: spec['label'] for sv, spec in SERVICES.items()},
                'mail_calendar': {'contract': mailcal.CONTRACT,
                                  'tools': self.access.visible_tools('agent:main', sorted(mailcal.TOOLS) + ['calendar_suggestions']),
                                  'note': 'mail_* and calendar_* work on every account with Gmail/Calendar on. google_run (gog) needs an own-client account.'},
                'app_link': '/wizard-os-home/?open=wizard-google'}

    # ---------- views ----------

    def operations(self):
        return [{'id': op, **{k: v for k, v in spec.items() if k != 'scope'}} for op, spec in OPERATIONS.items()]

    def clients(self):
        known = {g['client'] for g in self.state['grants'].values()} | {r['client'] for r in self.state['requests'].values()} \
            | {k['client'] for k in self.state['keys'].values()} | {'agent:main'}
        return [{'id': c, 'label': client_label(c), 'kind': c.split(':')[0],
                 'grants': [{'id': g['id'], 'account': g['account'], 'operations': g['operations']} for g in self.grants_for(c)],
                 'keys': [{'id': k['id'], 'created': k['created']} for k in self.state['keys'].values() if k['client'] == c]}
                for c in sorted(known, key=lambda c: (c != 'agent:main', c))]

    def home(self, origin, shell_path, session=None):
        return {'setup': self.setup_plan(origin, shell_path), 'client': self.client_summary(),
                'accounts': [self.account_view(a) for a in self.state['accounts']],
                'clients': self.clients(),
                'requests': [{**r, 'label': client_label(r['client'])} for r in self.state['requests'].values() if r['status'] == 'pending'],
                'operations': self.operations(), 'apis': google.APIS,
                'services': [{'id': sv, 'label': spec['label'], 'what': spec['what'], 'default': sv in google.DEFAULT_SERVICES}
                             for sv, spec in SERVICES.items()],
                'sign_in': self._sign_in_view(),
                'signin': self.flow_status(session) if session else {'status': 'none'},
                'quick': self.quick.view(),
                'audit': self.state['audit'][-30:][::-1]}

    def mail_settings(self):
        """Owner only (Mail > Settings): the accounts with how to disconnect each, and the agents' mail level."""
        accounts = [{'id': a['id'], 'email': a['email'], 'label': a.get('label', ''), 'status': a['status'], 'via': 'own-client',
                     'services': self._services_on(a['id']), 'disconnect': f"api/accounts/{a['id']}/disconnect"}
                    for a in self.state['accounts'].values()]
        accounts += [{'id': a['id'], 'email': a['email'], 'label': a.get('label', ''), 'status': a['status'], 'via': 'composio',
                      'services': self.quick.services_on(a), 'disconnect': f"api/quick/accounts/{a['id']}/disconnect"}
                     for a in self.quick.section()['accounts'].values()]
        return {'source': 'google', 'accounts': accounts, 'agent_access': self.access.view()}

    def calendar_settings(self):
        """Owner only (Calendar > Settings): the same accounts and levels as mail_settings."""
        return self.mail_settings()

    def set_agent_access(self, body):
        """Owner only: {mail: <level>} and/or {calendar: <level>} (an older Mail sends a bare mail level)."""
        body = body if isinstance(body, dict) else {'mail': body}
        try:
            view = self.access.set_from(body)
        except AccessRefused as refused:
            raise Problem(refused.status, refused.code, refused.message) from None
        self._record('owner', '-', 'agent_access', ','.join(f'{k}={body[k]}' for k in ('mail', 'calendar') if body.get(k)))
        return {'agent_access': view}

    # ---------- calendar suggestions (agents at Suggest only) ----------

    def _suggestion_tool(self, caller, tool, args):
        try:
            self.access.check(caller, tool)  # approve / dismiss: the owner's tap only; listing: agents at Suggest only
            if tool == 'calendar_suggestions':
                return {'suggestions': self.suggestions.list(), 'level': self.access.calendar_level()}
            item = self.suggestions.get(args.get('id'))
        except AccessRefused as refused:
            self._record(caller, '-', tool, refused.code)
            raise Problem(refused.status, refused.code, refused.message) from None
        if tool == 'calendar_suggestion_dismiss':
            self.suggestions.drop(item['id'])
            self._record(caller, '-', tool, 'ok')
            return {'dismissed': True, 'id': item['id']}
        # Approve: the owner's change, applied as the owner (the caller is the front end's grant, so no new suggestion).
        result = self.mailcal_call(caller, item['tool'], item['args'])
        self.suggestions.drop(item['id'])
        self._record(caller, '-', tool, 'ok')
        return {'approved': True, 'id': item['id'], 'result': result}

    def _suggest(self, caller, tool, args, handler, account, record):
        """An agent's calendar change at Suggest only: check it as if it ran, then keep it for the owner. Nothing changes."""
        try:
            handler(dry_call, args)
        except DryRun:
            pass
        except BadArgs as bad:
            raise Problem(400, 'invalid_arguments', str(bad)) from None
        try:
            item = self.suggestions.add(caller, tool, args, record.get('email') or account)
        except AccessRefused as refused:
            raise Problem(refused.status, refused.code, refused.message) from None
        self._record(caller, account, tool, 'suggested')
        return {'contract': mailcal.CONTRACT, 'source': 'google', 'account': record.get('email') or account,
                'suggested': True, 'applied': False, 'suggestion': item,
                'next': 'Nothing changed yet. The owner sees this in Calendar and approves or dismisses it.'}

    def _sign_in_view(self):
        mode = self._mode()
        offered = self.wizard_signin() if mode != 'own' else None
        return {'mode': mode, 'wizard_available': bool(offered), 'verified': bool(offered and offered.get('verified'))}

    def connection(self):
        """Read-only status in the shape of the other connection apps. Never a token."""
        accounts = list(self.state['accounts'].values()) + list(self.quick.section()['accounts'].values())
        if not accounts:
            status, error = 'not_connected', {'code': 'signed_out', 'message': 'No Google account connected.'}
        elif all(a['status'] != 'active' for a in accounts):
            status, error = 'expired', {'code': 'expired', 'message': 'Every Google account needs to be connected again.'}
        else:
            status, error = 'connected', None
        return {'contract': 'wizard.google-connection/1', 'provider': 'google', 'name': 'Google', 'status': status,
                'plan': None, 'account': ', '.join(a['email'] for a in accounts if a['email']) or None, 'usage': None,
                'accounts': [{'id': a['id'], 'email': a['email'], 'label': a.get('label', ''), 'status': a['status'],
                              'via': 'composio' if a['id'].startswith('q-') else 'own-client',
                              'services': self.quick.services_on(a) if a['id'].startswith('q-') else self._services_on(a['id'])}
                             for a in accounts],
                'error': error}

    def caller_status(self, client):
        grants = self.grants_for(client)
        return {'caller': client, 'setup_complete': bool(self.state['client']) and bool(self.state['accounts']),
                'accounts': [{'id': a['id'], 'email': a['email'], 'status': a['status'],
                              'operations': next((g['operations'] for g in grants if g['account'] == a['id']), [])}
                             for a in self.state['accounts'].values()],
                'operations': {op: spec['label'] for op, spec in OPERATIONS.items()},
                'waiting': [r for r in self.state['requests'].values() if r['client'] == client and r['status'] == 'pending']}

    # ---------- mail and calendar (contract wizard.mail-calendar/1; own client or Quick connect) ----------

    def mail_accounts(self, service):
        own = [(a['id'], a) for a in self.state['accounts'].values() if a['status'] == 'active' and service in self._services_on(a['id'])]
        quick = [(a['id'], a) for a in self.quick.section()['accounts'].values() if a['status'] == 'active' and service in self.quick.services_on(a)]
        return own + quick

    def _pick_mail_account(self, value, service):
        candidates = self.mail_accounts(service)
        everyone = list(self.state['accounts'].values()) + list(self.quick.section()['accounts'].values())
        if value not in (None, ''):
            if not isinstance(value, str):
                raise Problem(400, 'invalid_account', 'Name the account by its email or label.')
            wanted = value.strip().lower()
            named = [a for a in everyone if wanted in (a['id'], (a.get('email') or '').lower(), (a.get('label') or '').lower())]
            if not named:
                raise Problem(404, 'account_not_found', 'No connected Google account matches that.', accounts=[a['email'] for a in everyone])
            match = next((pair for pair in candidates if pair[0] in {a['id'] for a in named}), None)
            if not match:
                raise Problem(403, 'service_off', f'{SERVICES[service]["label"]} is not switched on for that account. The owner can switch it on in the Google app.',
                              app_link='/wizard-os-home/?open=wizard-google')
            return match
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            if not everyone:
                raise Problem(409, 'not_connected', 'No Google account is connected yet. The owner connects one in the Google app.',
                              app_link='/wizard-os-home/?open=wizard-google')
            raise Problem(403, 'service_off', f'No connected Google account has {SERVICES[service]["label"]} switched on.',
                          app_link='/wizard-os-home/?open=wizard-google')
        raise Problem(400, 'account_required', 'Several accounts can do this; name one with "account".',
                      accounts=[a['email'] for _, a in candidates])

    def _own_call(self, account):
        def call(method, url, query=None, body=None):
            if query:
                url += ('&' if '?' in url else '?') + urllib.parse.urlencode(query, doseq=True)
            with self._account_lock(account):
                token = self._token(account)
            try:
                return self.transport.request(method, url, token=token, body=body)
            except GoogleError as failure:
                if failure.code != 'token_rejected':
                    raise
                with self._account_lock(account):
                    self._expire(account)
                    token = self._token(account)
                return self.transport.request(method, url, token=token, body=body)
        return call

    def mailcal_call(self, caller, tool, args):
        """One mail/calendar tool on one account. Mail and event text in the result is untrusted data."""
        args = args if isinstance(args, dict) else {}
        if tool in SUGGESTION_TOOLS:
            return self._suggestion_tool(caller, tool, args)
        if tool not in mailcal.TOOLS:
            raise Problem(404, 'unknown_tool', 'Unknown tool.')
        service, writes, handler = mailcal.TOOLS[tool]
        try:
            action = self.access.check(caller, tool)  # agents: the owner's levels; sending is never an agent's
        except AccessRefused as refused:
            self._record(caller, '-', tool, refused.code)
            raise Problem(refused.status, refused.code, refused.message) from None
        account, record = self._pick_mail_account(args.get('account'), service)
        if action == 'suggest':
            return self._suggest(caller, tool, args, handler, account, record)
        via = 'composio' if account.startswith('q-') else 'own-client'
        try:
            call = self.quick.caller_for(account, service) if via == 'composio' else self._own_call(account)
            result = handler(call, args)
        except QuickProblem as problem:
            raise Problem(problem.status, problem.code, problem.message, **problem.extra) from None
        except BadArgs as bad:
            raise Problem(400, 'invalid_arguments', str(bad)) from None
        except GoogleError as failure:
            self._record(caller, account, tool, failure.code)
            extra = {}
            if failure.code == 'api_not_enabled' and via == 'own-client':
                extra['fix_url'] = setup_flow.enable_url([failure.service or SERVICES[service]['apis'][0]], (self.state['client'] or {}).get('project_id'))
            raise Problem(failure.status, failure.code, failure.message, **extra) from None
        self._record(caller, account, tool, 'ok')
        return {'contract': mailcal.CONTRACT, 'source': 'google', 'via': via, 'account': record.get('email') or account, **result}
