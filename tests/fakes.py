"""A fake Google for tests. Labelled fake: no test here talks to Google."""
import base64
import json
import sys
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'app'))
import google  # noqa: E402
from google import GoogleError, classify  # noqa: E402

CLIENT_ID = '123456789012-abcdefghijklmnop.apps.googleusercontent.com'
SECRET = 'GOCSPX-fake-secret-for-tests'
CLIENT_JSON = json.dumps({'web': {
    'client_id': CLIENT_ID, 'project_id': 'wizard-test-123', 'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
    'token_uri': 'https://oauth2.googleapis.com/token', 'auth_provider_x509_cert_url': 'https://www.googleapis.com/oauth2/v1/certs',
    'client_secret': SECRET, 'redirect_uris': ['http://127.0.0.1:18812/oauth/callback']}})


def b64(value):
    return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b'=').decode()


def id_token(claims):
    return b64({'alg': 'RS256', 'kid': 'fake'}) + '.' + b64(claims) + '.' + 'c2ln'


class FakeGoogle:
    def __init__(self):
        self.codes = {}          # code -> (sub, email, scopes)
        self.refresh = {}        # refresh token -> {sub, scopes}
        self.access = {}         # access token -> {sub, scopes}
        self.disabled = set()    # API hosts not enabled in the project
        self.calls = []
        self.revoked = []
        self.rotate = False
        self.dead_refresh = set()
        self.counter = 0
        self.mail = {'m1': {'from': 'Anna <anna@example.com>', 'subject': 'Lunch', 'body': 'Thursday at noon?'}}
        self.sent = []
        self.clients = {CLIENT_ID: SECRET}
        self.testing = False  # Testing-mode apps get 7-day refresh tokens

    def issue(self, code, sub, email, scopes):
        self.codes[code] = (sub, email, scopes)

    def _token(self, sub, scopes):
        self.counter += 1
        token = f'at-{sub}-{self.counter}'
        self.access[token] = {'sub': sub, 'scopes': scopes}
        return token

    def request(self, method, url, *, token=None, form=None, body=None, raw=False):
        self.calls.append((method, url.split('?')[0]))
        parts = urllib.parse.urlsplit(url)
        if url == google.TOKEN_URL:
            if self.clients.get(form.get('client_id')) != form.get('client_secret'):
                raise classify(401, {'error': 'invalid_client', 'error_description': 'Unauthorized'})
            if form['grant_type'] == 'authorization_code':
                found = self.codes.pop(form['code'], None)
                if not found or not form.get('code_verifier'):
                    raise classify(400, {'error': 'invalid_grant', 'error_description': 'Malformed auth code.'})
                sub, email, scopes = found
                refresh = f'rt-{sub}-{self.counter}'
                self.refresh[refresh] = {'sub': sub, 'scopes': scopes}
                nonce = self.nonce_for(form)
                extra = {'refresh_token_expires_in': 604799} if self.testing else {}
                return {**extra, 'token_type': 'Bearer', 'access_token': self._token(sub, scopes), 'expires_in': 3599, 'refresh_token': refresh,
                        'scope': ' '.join(scopes), 'id_token': id_token({'iss': 'https://accounts.google.com', 'aud': CLIENT_ID, 'azp': CLIENT_ID,
                                                                          'sub': sub, 'email': email, 'email_verified': True, 'name': email.split('@')[0].title(),
                                                                          'exp': time.time() + 3600, 'iat': time.time(), 'nonce': nonce})}
            if form['grant_type'] == 'refresh_token':
                held = self.refresh.get(form['refresh_token'])
                if not held or form['refresh_token'] in self.dead_refresh:
                    raise classify(400, {'error': 'invalid_grant', 'error_description': 'Token has been expired or revoked.'})
                value = {'token_type': 'Bearer', 'access_token': self._token(held['sub'], held['scopes']), 'expires_in': 3599, 'scope': ' '.join(held['scopes'])}
                if self.rotate:
                    new = form['refresh_token'] + '-r'
                    self.refresh[new] = held
                    self.dead_refresh.add(form['refresh_token'])
                    value['refresh_token'] = new
                return value
        if url == google.REVOKE_URL:
            self.revoked.append(form['token'])
            return {}
        held = self.access.get(token)
        if not held:
            raise classify(401, {'error': {'code': 401, 'message': 'Invalid Credentials', 'status': 'UNAUTHENTICATED'}})
        if parts.hostname in self.disabled or (parts.hostname == 'www.googleapis.com' and parts.path.split('/')[1] in self.disabled):
            service = {'calendar': 'calendar-json.googleapis.com', 'drive': 'drive.googleapis.com', 'youtube': 'youtube.googleapis.com'}.get(parts.path.split('/')[1], parts.hostname)
            raise classify(403, {'error': {'code': 403, 'message': f'{service} has not been used in project 123456789012 before or it is disabled.',
                                           'errors': [{'reason': 'accessNotConfigured'}],
                                           'details': [{'@type': 'type.googleapis.com/google.rpc.ErrorInfo', 'reason': 'SERVICE_DISABLED',
                                                        'metadata': {'service': service, 'consumer': 'projects/123456789012'}}]}})
        if parts.path.startswith('/gmail/v1/users/me/messages/send'):
            raw_mail = base64.urlsafe_b64decode(body['raw'] + '==').decode()
            self.sent.append((held['sub'], raw_mail))
            return {'id': 'sent1', 'threadId': 't1'}
        if parts.path == '/gmail/v1/users/me/messages':
            return {'messages': [{'id': k} for k in self.mail]}
        if parts.path.startswith('/gmail/v1/users/me/messages/'):
            mid = parts.path.rsplit('/', 1)[1]
            m = self.mail[mid]
            data = base64.urlsafe_b64encode(m['body'].encode()).decode()
            return {'id': mid, 'threadId': 't-' + mid, 'snippet': m['body'][:20], 'labelIds': ['INBOX'],
                    'payload': {'mimeType': 'multipart/alternative', 'headers': [{'name': 'From', 'value': m['from']}, {'name': 'Subject', 'value': m['subject']}],
                                'parts': [{'mimeType': 'text/plain', 'body': {'data': data}}]}}
        if parts.path == '/gmail/v1/users/me/profile':
            return {'emailAddress': 'x'}
        if parts.path.startswith('/calendar/v3/calendars/'):
            if method == 'POST':
                return {'id': 'ev1', 'summary': body['summary'], 'start': body['start'], 'end': body['end']}
            return {'items': [{'id': 'ev0', 'summary': 'Standup', 'start': {'dateTime': '2026-10-21T09:00:00Z'}, 'end': {'dateTime': '2026-10-21T09:15:00Z'}}]}
        return {}

    def nonce_for(self, form):
        return self.last_nonce

    last_nonce = ''


def sign_in(broker, fake, session, sub, email, operations=(), account=None, origin='http://127.0.0.1:18812', shell='/', grant=None):
    """Drive the real start/finish code with the fake Google standing in for the browser trip."""
    started = broker.start(session, origin, shell, account, list(operations))
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(started['authorization_url']).query))
    fake.last_nonce = query['nonce']
    code = f'code-{sub}-{len(fake.codes)}-{time.time()}'
    fake.issue(code, sub, email, sorted(grant if grant is not None else set(query['scope'].split())))
    return broker.finish(query['state'], session, code), query
