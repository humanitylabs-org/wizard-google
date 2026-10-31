"""Quick connect for Google: Gmail and Calendar through Composio's already-approved Google sign-in.

The owner pastes a Composio API key here (several keys allowed), taps Quick connect and signs in on
Composio's page. Composio keeps the Google tokens; this app keeps only the key (encrypted with the rest
of the app's state) and the connection ids. Agents and apps use the same mail/calendar tools as for an
own-client account: every request goes through Composio's proxy to the Gmail or Calendar API.
"""
import hashlib
import re
import secrets
import threading
import time

import composio
import google
import mailcal

TOOLKITS = {'gmail': 'gmail', 'calendar': 'googlecalendar'}
LABELS = {'gmail': 'Gmail', 'calendar': 'Calendar'}
WHAT = {'gmail': 'Search, read, draft and send mail', 'calendar': 'See, add, change and answer events'}
NOTE = ("Quick connect goes through Composio, a third party. They hold the sign-in keys, so technically they can access this "
        "account. Free for normal personal use; heavy use can cost money. We think that's an acceptable start: Google "
        "already reads your data. The most private setup is your own email on your Wizard, using a provider like "
        "Cloudflare only to send. You can switch any time.")


class QuickProblem(Exception):
    def __init__(self, status, code, message, **extra):
        super().__init__(code)
        self.status, self.code, self.message, self.extra = status, code, message, extra


def _problem(error):
    return QuickProblem(error.status if 400 <= error.status < 600 else 502, 'composio_' + error.code, error.message)


class Quick:
    """Lives inside the Broker; `state['quick']` is part of the app's encrypted store."""

    def __init__(self, broker, transport=None, clock=time.time):
        self.broker, self.clock = broker, clock
        self.transport = transport  # Composio HTTP (tests pass a fake)
        self.links = composio.Links(clock)
        self.lock = threading.RLock()

    # ---------- state ----------
    def section(self):
        quick = self.broker.state.setdefault('quick', {})
        quick.setdefault('keys', [])
        quick.setdefault('accounts', {})
        if not quick.get('box'):
            quick['box'] = secrets.token_hex(8)
        return quick

    def _save(self):
        with self.broker.lock:
            self.broker._commit(self.broker.state)

    def keys(self):
        return composio.Keys(self.section(), self._save, self.transport, self.clock)

    # ---------- keys ----------
    def add_key(self, key, label=''):
        try:
            return {'key': self.keys().add(key, label), 'quick': self.view()}
        except composio.ComposioError as error:
            raise _problem(error) from None

    def remove_key(self, key_id):
        users = [a['email'] or a['id'] for a in self.section()['accounts'].values() if a['key_id'] == key_id]
        if users:
            raise QuickProblem(409, 'key_in_use', f'Disconnect {", ".join(users)} first: they were connected with this key.')
        try:
            self.keys().remove(key_id)
        except composio.ComposioError as error:
            raise _problem(error) from None
        return {'quick': self.view()}

    # ---------- connect ----------
    def start(self, session, service, account=None, callback_base=None):
        if service not in TOOLKITS:
            raise QuickProblem(400, 'unknown_service', 'Quick connect covers Gmail and Calendar.')
        keys = self.keys()
        existing = self.section()['accounts'].get(account) if account else None
        if account and not existing:
            raise QuickProblem(404, 'account_not_found', 'Account not found.')
        item = None
        try:
            item = keys.pick(prefer=existing['key_id'] if existing else None)
            if existing and item['id'] != existing['key_id']:
                item = keys.get(existing['key_id'])  # same Composio project as the account's other connection
            user_id = existing['user_id'] if existing else f"wizard-{self.section()['box']}-{secrets.token_hex(4)}"
            attempt = self.links.add(session=session, service=service, key_id=item['id'], user_id=user_id, account=account)
            callback = f'{callback_base}?attempt={attempt}' if callback_base else None
            link = composio.Client(item['key'], self.transport).start_link(user_id, TOOLKITS[service], callback)
        except composio.ComposioError as error:
            if error.next_key_may_help and item:
                keys.mark(item['id'], error.code, error.message)
            raise _problem(error) from None
        self.links.items[attempt]['connected_account_id'] = link['connected_account_id']
        return {'attempt': attempt, 'redirect_url': link['redirect_url']}

    def status(self, session=None, attempt_id=None):
        """Poll Composio for the pending link; on ACTIVE, name the account and save it."""
        attempt = self.links.get(attempt_id) if attempt_id else self.links.latest(session)
        if not attempt or (session is not None and attempt.get('session') != session):
            return {'status': 'none'}
        if attempt['state'] != 'pending':
            return {'status': attempt['state'], 'result': attempt['result']}
        try:
            client = self.keys().client(attempt['key_id'])
            found = client.account(attempt['connected_account_id'])
        except composio.ComposioError as error:
            if error.code == 'unavailable':
                return {'status': 'pending'}
            attempt.update(state='failed', result={'code': 'composio_' + error.code, 'message': error.message})
            return {'status': 'failed', 'result': attempt['result']}
        if found['status'] in composio.ENDED:
            attempt.update(state='failed', result={'code': 'connect_failed', 'message': 'The sign-in did not finish. Try Quick connect again.'})
            return {'status': 'failed', 'result': attempt['result']}
        if found['status'] not in composio.DONE:
            return {'status': 'pending'}
        with self.lock:
            if attempt['state'] != 'pending':
                return {'status': attempt['state'], 'result': attempt['result']}
            attempt['state'] = 'saving'
        try:
            result = self._save_account(attempt, found)
        except QuickProblem as problem:
            attempt.update(state='failed', result={'code': problem.code, 'message': problem.message})
            return {'status': 'failed', 'result': attempt['result']}
        attempt.update(state='connected', result=result)
        return {'status': 'connected', 'result': result}

    def _save_account(self, attempt, found):
        service, ca = attempt['service'], attempt['connected_account_id']
        temporary = {'user_id': attempt['user_id'], 'key_id': attempt['key_id'], 'links': {service: ca}}
        try:
            email = mailcal.profile_email(self._caller(temporary, service), service).strip().lower()[:254]
        except google.GoogleError:
            email = ''
        with self.broker.lock:
            accounts = self.section()['accounts']
            target = accounts.get(attempt['account']) if attempt['account'] else None
            if not target and email:
                target = next((a for a in accounts.values() if a['email'] == email and a['key_id'] == attempt['key_id']), None)
            if target and target.get('email') and email and target['email'] != email:
                raise QuickProblem(409, 'wrong_account', f'You signed in as {email}, not {target["email"]}. Choose the same account.')
            if target and target['user_id'] != attempt['user_id']:
                target = None  # a different Composio user: keep it as its own account
            if not target:
                account_id = 'q-' + hashlib.sha256(f"{attempt['user_id']}:{email}".encode()).hexdigest()[:16]
                target = accounts.setdefault(account_id, {'id': account_id, 'email': email, 'label': '', 'user_id': attempt['user_id'],
                                                          'key_id': attempt['key_id'], 'links': {}, 'services': [],
                                                          'added': int(self.clock()), 'status': 'active'})
            target['links'][service] = ca
            target['services'] = sorted(set(target['services']) | {service})
            target['status'], target['error'] = 'active', None
            target['email'] = target['email'] or email
            target['scopes'] = found.get('scopes', [])
            self.broker._commit(self.broker.state)
        return {'account': target['id'], 'email': target['email'], 'service': service}

    # ---------- accounts ----------
    def account(self, account_id):
        a = self.section()['accounts'].get(account_id)
        if not a:
            raise QuickProblem(404, 'account_not_found', 'Account not found.')
        return a

    def set_service(self, account_id, service, on):
        a = self.account(account_id)
        if service not in TOOLKITS or not isinstance(on, bool):
            raise QuickProblem(400, 'unknown_service', 'Quick connect covers Gmail and Calendar.')
        if on and service not in a['links']:
            raise QuickProblem(409, 'consent_needed', f'Connect {LABELS[service]} first.', service=service, account=account_id)
        with self.broker.lock:
            services = set(a['services'])
            services.add(service) if on else services.discard(service)
            a['services'] = sorted(services)
            self.broker._commit(self.broker.state)
        return {'account': self.account_view(a)}

    def set_label(self, account_id, label):
        a = self.account(account_id)
        if not isinstance(label, str) or len(label.strip()) > 40:
            raise QuickProblem(400, 'invalid_label', 'Use a short name, like Work or Personal.')
        with self.broker.lock:
            a['label'] = label.strip()
            self.broker._commit(self.broker.state)
        return {'account': self.account_view(a)}

    def disconnect(self, account_id):
        a = self.account(account_id)
        removed_everywhere = True
        try:
            client = self.keys().client(a['key_id'])
            for ca in a['links'].values():
                client.remove(ca)
        except composio.ComposioError:
            removed_everywhere = False
        with self.broker.lock:
            self.section()['accounts'].pop(account_id, None)
            self.broker._commit(self.broker.state)
        return {'disconnected': True, 'removed_at_composio': removed_everywhere,
                'manual_url': None if removed_everywhere else 'https://myaccount.google.com/connections'}

    # ---------- calls ----------
    def _caller(self, a, service):
        """`call(method, url, query, body)` for mailcal, signed by Composio, counted per key."""
        keys = self.keys()
        try:
            client = keys.client(a['key_id'])
        except composio.ComposioError:
            raise google.GoogleError('reauth_required', 409, 'The Composio key for this account was removed. Add it again.') from None
        ca = a['links'].get(service)
        if not ca:
            raise google.GoogleError('service_off', 403, f'{LABELS[service]} is not connected for this account.')

        def call(method, url, query=None, body=None):
            try:
                status, data = client.proxy(a['user_id'], ca, method, url, query, body)
            except composio.ComposioError as error:
                if error.code in ('key_invalid', 'key_forbidden', 'quota'):
                    keys.mark(a['key_id'], error.code, error.message)
                if error.code in ('not_found', 'rejected') and self._gone(client, ca):
                    self._mark(a, 'reauth_required')
                    raise google.GoogleError('reauth_required', 409, 'Composio no longer has this sign-in. Quick connect again.') from None
                raise google.GoogleError('provider_unavailable' if error.code == 'unavailable' else 'composio_' + error.code,
                                         error.status, error.message) from None
            finally:
                keys.count(a['key_id'])
            if status >= 400:
                failure = google.classify(status, data if isinstance(data, dict) else {})
                if failure.code == 'token_rejected':
                    self._mark(a, 'reauth_required')
                    failure = google.GoogleError('reauth_required', 409, 'Google ended this sign-in. Quick connect again.')
                raise failure
            return data if isinstance(data, dict) else {}
        return call

    def _gone(self, client, ca):
        try:
            return client.account(ca)['status'] in composio.ENDED
        except composio.ComposioError as error:
            return error.code == 'not_found'

    def _mark(self, a, status):
        with self.broker.lock:
            a['status'] = status
            self.broker._commit(self.broker.state)

    def caller_for(self, account_id, service):
        return self._caller(self.account(account_id), service)

    # ---------- views ----------
    def account_view(self, a):
        return {'id': a['id'], 'email': a['email'], 'label': a.get('label', ''), 'status': a['status'], 'via': 'composio',
                'key': a['key_id'],
                'services': [{'id': sv, 'label': LABELS[sv], 'what': WHAT[sv], 'linked': sv in a['links'], 'on': sv in a['services'] and sv in a['links']}
                             for sv in TOOLKITS]}

    def services_on(self, a):
        return [sv for sv in a['services'] if sv in a['links']]

    def view(self):
        keys = self.keys()
        if self.section()['keys']:
            keys.recheck()
        return {'signup_url': composio.SIGNUP_URL, 'key_help': composio.KEY_HELP, 'note': NOTE, 'free_calls': composio.MANAGED_FREE_CALLS,
                'keys': keys.public(), 'ready': any(k['status'] in ('ok', 'unchecked') for k in self.section()['keys']),
                'accounts': [self.account_view(a) for a in self.section()['accounts'].values()],
                'services': [{'id': sv, 'label': LABELS[sv], 'what': WHAT[sv]} for sv in TOOLKITS]}


def valid_attempt(value):
    return isinstance(value, str) and re.fullmatch(r'cl-[A-Za-z0-9_-]{8,40}', value) is not None
