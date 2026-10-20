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
import setup_flow
from google import OPERATIONS, IDENTITY_SCOPES, GoogleError, BadArgs
from store import Store

FLOW_TTL = 600
WRITE_TTL = 15 * 60
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
    def __init__(self, data_dir, *, transport=None, store_key=None, relay_callback='', clock=time.time):
        self.store = Store(data_dir, store_key)
        self.state = self.store.load()
        self.transport = transport or google.Transport()
        self.relay_callback = relay_callback
        self.clock = clock
        self.lock = threading.RLock()
        self.account_locks = {}
        self.flows = {}  # sha(state) -> pending sign-in; memory only, 10 minutes
        # A process may have died after submitting a write. Never replay a stranded claim.
        changed = False
        for request in self.state['requests'].values():
            if request.get('kind') == 'write' and request['status'] == 'executing':
                request.update(status='unknown', error={'code': 'outcome_unknown',
                               'message': 'Execution was interrupted. Check Google before proposing this again.'})
                request.pop('args', None)
                changed = True
        if changed:
            self._commit(self.state)

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
        return {'client_id': client['client_id'], 'project_id': client.get('project_id'), 'project_number': client.get('project_number'),
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
        mode = self.state['setup'].get('redirect_mode', 'box')
        return setup_flow.redirect_for(mode, origin, shell_path, self.relay_callback)

    def setup_plan(self, origin, shell_path):
        mode = self.state['setup'].get('redirect_mode', 'box')
        return setup_flow.plan(self.state, redirect_uri=self.redirect_uri(origin, shell_path), redirect_mode=mode,
                               box_ok=setup_flow.box_redirect_ok(origin), relay_callback=self.relay_callback)

    # ---------- sign-in ----------

    def start(self, session, origin, shell_path, account=None, operations=()):
        client = self.state.get('client')
        if not client:
            raise Problem(409, 'client_required', 'Finish setup first: Wizard needs your Google client.')
        mode = self.state['setup'].get('redirect_mode', 'box')
        if mode == 'box' and not setup_flow.box_redirect_ok(origin):
            raise Problem(409, 'box_address_unusable', 'Google needs an https address for this box. Open Wizard through its https address.')
        if mode == 'relay' and not (origin or '').startswith('https://'):
            raise Problem(409, 'box_address_unusable', 'The return page can only send you back to an https Wizard address.')
        redirect_uri = self.redirect_uri(origin, shell_path)
        unknown = [op for op in operations if op not in OPERATIONS]
        if unknown:
            raise Problem(400, 'unknown_operation', f'Unknown operation: {unknown[0]}')
        login_hint = None
        if account is not None:
            current = self.state['accounts'].get(account)
            if not current:
                raise Problem(404, 'account_not_found', 'Account not found.')
            login_hint = current.get('email') or None
        scopes = set(IDENTITY_SCOPES) | {OPERATIONS[op]['scope'] for op in operations}
        nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(64)
        state = secrets.token_urlsafe(32)
        if mode == 'relay':
            # The return page needs to know where to send the browser. It cannot use the code:
            # exchanging it needs this box's client secret and PKCE verifier.
            back = origin.rstrip('/') + shell_path + setup_flow.CALLBACK_PATH
            state = 'wg1.' + base64.urlsafe_b64encode(json.dumps({'n': state, 'r': back}, separators=(',', ':')).encode()).rstrip(b'=').decode()
        with self.lock:
            stamp = self.clock()
            self.flows = {k: v for k, v in self.flows.items() if v['expires'] > stamp and v['session'] != session}
            if len(self.flows) >= 32:
                raise Problem(429, 'signin_busy', 'Too many sign-ins in progress.')
            self.flows[sha(state)] = {'session': session, 'verifier': verifier, 'nonce': nonce, 'redirect_uri': redirect_uri,
                                      'account': account, 'scopes': sorted(scopes), 'expires': stamp + FLOW_TTL,
                                      'status': 'pending', 'result': None, 'started': stamp}
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
            value = self.transport.request('POST', google.TOKEN_URL, form={
                'grant_type': 'authorization_code', 'code': code, 'client_id': client['client_id'],
                'client_secret': client['client_secret'], 'redirect_uri': flow['redirect_uri'], 'code_verifier': flow['verifier']})
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
            state['accounts'][account] = {
                'id': account, 'sub': claims['sub'],
                'email': str(claims.get('email', ''))[:254] if claims.get('email_verified', True) else '',
                'name': str(claims.get('name', ''))[:120], 'picture': None,
                'status': 'active', 'scopes': sorted(granted),  # include_granted_scopes: Google reports the full set
                'access_token': value['access_token'], 'expires': self.clock() + lifetime, 'refresh_token': refresh,
                'refresh_expires': self.clock() + value['refresh_token_expires_in'] if isinstance(value.get('refresh_token_expires_in'), (int, float)) else None,
                'added': previous.get('added', stamp), 'signed_in': stamp, 'error': None, 'apis': previous.get('apis', {})}
            self._commit(state)
        missing = sorted(set(flow['scopes']) - granted - set(IDENTITY_SCOPES))
        return {'connected': True, 'account': account, 'email': state['accounts'][account]['email'],
                'declined': [op for op, spec in OPERATIONS.items() if spec['scope'] in missing]}

    # ---------- accounts ----------

    def account_view(self, account):
        a = self.state['accounts'][account]
        allowed = [op for op, spec in OPERATIONS.items() if spec['scope'] in a['scopes']]
        return {'id': a['id'], 'email': a['email'], 'name': a.get('name', ''), 'status': a['status'], 'error': a.get('error'),
                'allowed': allowed, 'apis': a.get('apis', {}), 'added': a.get('added'), 'signed_in': a.get('signed_in'),
                'refresh_expires': a.get('refresh_expires')}

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
            value = self.transport.request('POST', google.TOKEN_URL, form={
                'grant_type': 'refresh_token', 'refresh_token': a['refresh_token'],
                'client_id': client['client_id'], 'client_secret': client['client_secret']})
        except GoogleError as failure:
            if failure.code in ('reauth_required', 'client_invalid'):
                self._mark(account, 'reauth_required', failure.code)
                hint = None
                if failure.code == 'reauth_required' and not self.state['setup']['checked'].get('publish'):
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
                if OPERATIONS[operation]['scope'] not in scopes:
                    results[api] = {'status': 'not_allowed'}
                    continue
                try:
                    self.transport.request('GET', url, token=token)
                    results[api] = {'status': 'ok'}
                except GoogleError as failure:
                    if failure.code == 'api_not_enabled':
                        results[api] = {'status': 'not_enabled', 'fix_url': setup_flow.enable_url([api], project)}
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
        with self._account_lock(account), self.lock:
            if account not in self.state['accounts']:
                raise Problem(404, 'account_not_found', 'Account not found.')
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
                if request.get('kind') != 'write' and request['client'] == client and request['status'] == 'pending' and set(request['operations']) <= set(operations) \
                        and request.get('account') in (None, account):
                    request.update(status='approved', decided=now())
            self._commit(state)
        return {'grant': grant}

    def revoke_grant(self, grant_id):
        with self.lock:
            if grant_id not in self.state['grants']:
                raise Problem(404, 'grant_not_found', 'Grant not found.')
            account = self.state['grants'][grant_id]['account']
        with self._account_lock(account), self.lock:
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
            self._expire_writes()
            state = self._copy()
            pending = [r for r in state['requests'].values() if r['status'] == 'pending']
            same = next((r for r in pending if r.get('kind') != 'write' and r['client'] == client and r.get('account') == account and set(r['operations']) == set(operations)), None)
            if same:
                return {'request': same}
            if sum(r['client'] == client for r in pending) >= 5 or len(pending) >= 100:
                raise Problem(429, 'too_many_requests', 'This caller already has requests waiting for the owner.')
            request = {'id': new_id('rq'), 'client': client, 'account': account, 'operations': sorted(set(operations)),
                       'reason': reason, 'status': 'pending', 'created': now()}
            state['requests'][request['id']] = request
            self._trim_requests(state)
            self._commit(state)
        return {'request': request}

    def decide(self, request_id, approve, account=None, payload_hash=None):
        if account is not None and not isinstance(account, str):
            raise Problem(400, 'account_required', 'Choose a Google account, not a replacement payload.')
        with self.lock:
            request = self.state['requests'].get(request_id)
            is_write = request and request.get('kind') == 'write'
            target = account or (request or {}).get('account')
        if is_write:
            return self._decide_write(request_id, approve, account, payload_hash)
        with self._account_lock(target), self.lock:
            request = self.state['requests'].get(request_id)
            if not request or request['status'] != 'pending':
                raise Problem(404, 'request_not_found', 'This request is no longer waiting.')
            if approve:
                if target not in self.state['accounts']:
                    raise Problem(400, 'account_required', 'Choose which Google account to share.')
                current = next((g['operations'] for g in self.grants_for(request['client']) if g['account'] == target), [])
                self.set_grant(request['client'], target, sorted(set(current) | set(request['operations'])))
            state = self._copy()
            state['requests'][request_id].update(status='approved' if approve else 'declined', decided=now())
            self._commit(state)
            return {'request': copy.deepcopy(state['requests'][request_id])}

    # ---------- one-shot agent writes (never an MCP/consumer approval capability) ----------

    @staticmethod
    def _write_hash(client, account, operation, args):
        packet = {'client': client, 'account': account, 'operation': operation, 'args': args}
        return sha(json.dumps(packet, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False))

    @staticmethod
    def _trim_requests(state):
        # Never evict a waiting card or an in-flight claim to make room for history.
        terminal = sorted((r for r in state['requests'].values() if r['status'] not in ('pending', 'executing')),
                          key=lambda r: r['created'])
        for r in terminal[:max(0, len(state['requests']) - 100)]:
            del state['requests'][r['id']]

    def _expire_writes(self):
        # Caller holds self.lock. Expiry is durable and checked again when Approve is tapped.
        state = self._copy()
        changed = False
        for r in state['requests'].values():
            if r.get('kind') == 'write' and r['status'] == 'pending' and r['expires'] <= self.clock():
                r.update(status='expired', decided=int(self.clock()))
                r.pop('args', None)
                changed = True
        if changed:
            self._commit(state)

    def _request_write(self, client, operation, args, account):
        try:
            # Detach mutable caller data; only this frozen JSON is ever shown or executed.
            encoded = json.dumps(args, sort_keys=True, allow_nan=False)
            if len(encoded.encode()) > 64_000:
                raise ValueError
            args = json.loads(encoded)
            if operation not in google.WRITE_REVIEW:
                raise BadArgs('This write has no review schema yet; it cannot execute.')
            args = google.WRITE_REVIEW[operation](args)
            digest = self._write_hash(client, account, operation, args)
            # Typed handlers validate locally before the card exists; this never calls Google.
            google.HANDLERS[operation](lambda *a, **kw: {}, args)
        except Exception as bad:
            raise Problem(400, 'invalid_arguments', str(bad) or 'Invalid write payload.') from None
        with self.lock:
            self._expire_writes()
            state = self._copy()
            pending = [r for r in state['requests'].values() if r['status'] == 'pending']
            same = next((r for r in pending if r.get('kind') == 'write' and r['payload_hash'] == digest), None)
            if same:
                request = same
            else:
                if sum(r['client'] == client for r in pending) >= 5 or len(pending) >= 100:
                    raise Problem(429, 'too_many_requests', 'Requests are waiting for the owner.')
                stamp = int(self.clock())
                request = {'id': new_id('rq'), 'kind': 'write', 'client': client, 'account': account,
                           'operations': [operation], 'args': args, 'payload_hash': digest,
                           'status': 'pending', 'created': stamp, 'expires': stamp + WRITE_TTL}
                state['requests'][request['id']] = request
                self._trim_requests(state)
                self._audit(state, client, account, operation, 'approval_pending')
                self._commit(state)
            return {'status': 'pending', 'request': copy.deepcopy(request),
                    'tell_owner': 'Tap Approve or Deny in the Google app. Nothing has been sent.',
                    'app_link': '/wizard-os-home/?open=wizard-google'}

    def _decide_write(self, request_id, approve, account, payload_hash):
        with self.lock:
            request = self.state['requests'].get(request_id)
            if not request:
                raise Problem(404, 'request_not_found', 'This request is no longer available.')
            target = request['account']
        # Same lock order as disconnect/calls: account then state. Claim survives process death.
        with self._account_lock(target):
            with self.lock:
                self._expire_writes()
                request = self.state['requests'].get(request_id)
                if not request:
                    raise Problem(404, 'request_not_found', 'This request is no longer available.')
                if request['status'] != 'pending':
                    raise Problem(409, 'request_' + request['status'], 'This write is no longer waiting. Refresh the Google app.')
                if account is not None and account != target:
                    raise Problem(409, 'payload_changed', 'The account does not match the displayed request.')
                digest = self._write_hash(request['client'], target, request['operations'][0], request['args'])
                if not isinstance(payload_hash, str) or not hmac.compare_digest(payload_hash, request['payload_hash']) \
                        or not hmac.compare_digest(digest, request['payload_hash']):
                    raise Problem(409, 'payload_changed', 'The payload does not match the displayed request. Refresh and review again.')
                frozen = copy.deepcopy(request)
                state = self._copy()
                state['requests'][request_id].update(status='executing' if approve else 'declined', decided=int(self.clock()))
                # No stored message/event body once consumed. Keep hash and outcome as receipt.
                state['requests'][request_id].pop('args', None)
                self._audit(state, request['client'], target, request['operations'][0], 'approval_claimed' if approve else 'approval_denied')
                self._commit(state)  # fsync + replace BEFORE any provider submission
                decided = copy.deepcopy(state['requests'][request_id])
            if approve:
                try:
                    result = self._execute(frozen['client'], frozen['operations'][0], frozen['args'], target)
                except Problem as problem:
                    # Definitive local/provider rejection is failed; ambiguous network/5xx is unknown.
                    decided = self._finish_write(request_id, 'unknown' if problem.status >= 500 else 'failed', error=problem.as_dict())
                except Exception:
                    decided = self._finish_write(request_id, 'unknown', error={'code': 'outcome_unknown',
                                       'message': 'Check Google before proposing this again. The request will not be retried.'})
                else:
                    decided = self._finish_write(request_id, 'executed', result=result)
        return {'request': decided}

    def _finish_write(self, request_id, status, **receipt):
        if 'result' in receipt:
            result = receipt['result']
            # Keep identifiers as receipts, not another copy of message/event content.
            receipt['result'] = {k: result[k] for k in ('account', 'id', 'thread_id', 'sent', 'created') if k in result}
            if isinstance(result.get('event'), dict):
                receipt['result']['event'] = {k: result['event'][k] for k in ('id', 'link') if k in result['event']}
        with self.lock:
            state = self._copy()
            request = state['requests'][request_id]
            request.update(status=status, finished=int(self.clock()), **receipt)
            self._audit(state, request['client'], request['account'], request['operations'][0], status)
            self._commit(state)
            return copy.deepcopy(request)

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
        if spec['writes'] and client.startswith('agent:'):
            if spec['scope'] not in self.state['accounts'][account]['scopes']:
                raise Problem(403, 'google_permission_missing', 'Allow this in Google first.')
            return self._request_write(client, operation, args, account)
        return self._execute(client, operation, args, account)

    def _execute(self, client, operation, args, account):
        """Private executor: only standing app grants or a durably consumed owner approval."""
        spec = OPERATIONS[operation]
        with self._account_lock(account):
            if account not in self.state['accounts']:
                raise Problem(404, 'account_not_found', 'Account disconnected before execution.')
            if account not in self.allowed(client, operation):
                raise Problem(403, 'grant_required', 'The owner revoked access before execution.')
            if spec['scope'] not in self.state['accounts'][account]['scopes']:
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
        with self.lock:
            self._expire_writes()
        return {'setup': self.setup_plan(origin, shell_path), 'client': self.client_summary(),
                'accounts': [self.account_view(a) for a in self.state['accounts']],
                'clients': self.clients(),
                'requests': [{**r, 'label': client_label(r['client'])} for r in self.state['requests'].values() if r['status'] == 'pending'],
                'write_receipts': [{**r, 'label': client_label(r['client'])} for r in self.state['requests'].values()
                                   if r.get('kind') == 'write' and r['status'] != 'pending'][-5:][::-1],
                'operations': self.operations(), 'apis': google.APIS,
                'signin': self.flow_status(session) if session else {'status': 'none'},
                'audit': self.state['audit'][-30:][::-1]}

    def connection(self):
        """Read-only status in the shape of the other connection apps. Never a token."""
        accounts = list(self.state['accounts'].values())
        if not self.state['client']:
            status, error = 'not_connected', {'code': 'setup_required', 'message': 'Google setup is not finished.'}
        elif not accounts:
            status, error = 'not_connected', {'code': 'signed_out', 'message': 'No Google account connected.'}
        elif all(a['status'] != 'active' for a in accounts):
            status, error = 'expired', {'code': 'expired', 'message': 'Every Google account needs to be connected again.'}
        else:
            status, error = 'connected', None
        return {'contract': 'wizard.google-connection/1', 'provider': 'google', 'name': 'Google', 'status': status,
                'plan': None, 'account': ', '.join(a['email'] for a in accounts if a['email']) or None, 'usage': None,
                'accounts': [{'id': a['id'], 'email': a['email'], 'status': a['status']} for a in accounts],
                'error': error}

    def caller_status(self, client):
        with self.lock:
            self._expire_writes()
        grants = self.grants_for(client)
        return {'caller': client, 'setup_complete': bool(self.state['client']) and bool(self.state['accounts']),
                'accounts': [{'id': a['id'], 'email': a['email'], 'status': a['status'],
                              'operations': next((g['operations'] for g in grants if g['account'] == a['id']), [])}
                             for a in self.state['accounts'].values()],
                'operations': {op: spec['label'] for op, spec in OPERATIONS.items()},
                'waiting': [copy.deepcopy(r) for r in self.state['requests'].values() if r['client'] == client and r['status'] == 'pending'],
                'write_requests': [copy.deepcopy(r) for r in self.state['requests'].values() if r['client'] == client and r.get('kind') == 'write'][-20:]}
