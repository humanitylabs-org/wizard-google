"""Google transport and the fixed operation catalogue.

The broker never forwards a caller's URL. Every call goes to a fixed Google endpoint with
no proxies, no cookies and no redirects, and each operation is a typed function.
"""
import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from email.message import EmailMessage
from email.utils import getaddresses

TOKEN_URL = 'https://oauth2.googleapis.com/token'
REVOKE_URL = 'https://oauth2.googleapis.com/revoke'
AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
GMAIL = 'https://gmail.googleapis.com/gmail/v1/users/me'
CALENDAR = 'https://www.googleapis.com/calendar/v3'
DRIVE = 'https://www.googleapis.com/drive/v3'
PEOPLE = 'https://people.googleapis.com/v1'
YOUTUBE = 'https://www.googleapis.com/youtube/v3'
ALLOWED_HOSTS = {'oauth2.googleapis.com', 'gmail.googleapis.com', 'www.googleapis.com', 'people.googleapis.com'}
MAX_RESPONSE = 4_000_000
MAX_TEXT = 100_000

IDENTITY_SCOPES = ('openid', 'email', 'profile')
SCOPE = 'https://www.googleapis.com/auth/'
# Google reports the identity scopes by their long names.
SCOPE_ALIASES = {SCOPE + 'userinfo.email': 'email', SCOPE + 'userinfo.profile': 'profile'}

# One entry per thing an app or agent can be allowed to do. `api` is the Google API the owner's
# project must have enabled; `scope` is what Google must have been granted for the account.
OPERATIONS = {
    'gmail.search':   {'label': 'Search mail (sender, subject, preview)', 'group': 'Gmail', 'scope': SCOPE + 'gmail.readonly', 'api': 'gmail.googleapis.com', 'writes': False},
    'gmail.read':     {'label': 'Read mail', 'group': 'Gmail', 'scope': SCOPE + 'gmail.readonly', 'api': 'gmail.googleapis.com', 'writes': False},
    'gmail.send':     {'label': 'Send mail', 'group': 'Gmail', 'scope': SCOPE + 'gmail.send', 'api': 'gmail.googleapis.com', 'writes': True},
    'calendar.read':  {'label': 'See events', 'group': 'Calendar', 'scope': SCOPE + 'calendar.readonly', 'api': 'calendar-json.googleapis.com', 'writes': False},
    'calendar.write': {'label': 'Add events', 'group': 'Calendar', 'scope': SCOPE + 'calendar.events', 'api': 'calendar-json.googleapis.com', 'writes': True},
    'drive.search':   {'label': 'Find files (names only)', 'group': 'Drive', 'scope': SCOPE + 'drive.metadata.readonly', 'api': 'drive.googleapis.com', 'writes': False},
    'drive.read':     {'label': 'Read files', 'group': 'Drive', 'scope': SCOPE + 'drive.readonly', 'api': 'drive.googleapis.com', 'writes': False},
    'contacts.read':  {'label': 'Look up contacts', 'group': 'Contacts', 'scope': SCOPE + 'contacts.readonly', 'api': 'people.googleapis.com', 'writes': False},
    'youtube.read':   {'label': 'Look up videos', 'group': 'YouTube', 'scope': SCOPE + 'youtube.readonly', 'api': 'youtube.googleapis.com', 'writes': False},
}
APIS = {
    'gmail.googleapis.com': 'Gmail API',
    'calendar-json.googleapis.com': 'Google Calendar API',
    'drive.googleapis.com': 'Google Drive API',
    'docs.googleapis.com': 'Google Docs API',
    'sheets.googleapis.com': 'Google Sheets API',
    'slides.googleapis.com': 'Google Slides API',
    'people.googleapis.com': 'People API',
    'youtube.googleapis.com': 'YouTube Data API v3',
}

# The services the owner switches on per account for their agent. Scopes follow gogcli's own
# `gog auth services` map (so every gog command in the service works), minus Gmail's
# delegation/forwarding scope. `commands` are the gog command roots the service unlocks.
SERVICES = {
    'gmail': {'label': 'Gmail', 'what': 'Search, read, draft, send and label mail',
              'scopes': [SCOPE + 'gmail.modify', SCOPE + 'gmail.settings.basic'],
              'commands': ['gmail'], 'apis': ['gmail.googleapis.com']},
    'calendar': {'label': 'Calendar', 'what': 'See, add and change events',
                 'scopes': [SCOPE + 'calendar'], 'commands': ['calendar'], 'apis': ['calendar-json.googleapis.com']},
    'drive': {'label': 'Drive, Docs and Sheets', 'what': 'Find, read, create and edit files, documents and spreadsheets',
              'scopes': [SCOPE + 'drive', SCOPE + 'documents', SCOPE + 'spreadsheets', SCOPE + 'presentations'],
              'commands': ['drive', 'docs', 'sheets', 'slides'],
              'apis': ['drive.googleapis.com', 'docs.googleapis.com', 'sheets.googleapis.com', 'slides.googleapis.com']},
    'contacts': {'label': 'Contacts', 'what': 'Look up, add and update contacts',
                 'scopes': [SCOPE + 'contacts', SCOPE + 'contacts.other.readonly'],
                 'commands': ['contacts', 'people'], 'apis': ['people.googleapis.com']},
    'youtube': {'label': 'YouTube', 'what': 'Search videos, read your playlists, subscriptions and comments',
                'scopes': [SCOPE + 'youtube.readonly'], 'commands': ['youtube'], 'apis': ['youtube.googleapis.com']},
}
DEFAULT_SERVICES = ('gmail', 'calendar', 'drive', 'contacts')

# A broader scope Google granted covers the narrower ones the typed operations ask for.
IMPLIED = {
    SCOPE + 'gmail.readonly': {'https://mail.google.com/', SCOPE + 'gmail.modify'},
    SCOPE + 'gmail.send': {'https://mail.google.com/', SCOPE + 'gmail.modify', SCOPE + 'gmail.compose'},
    SCOPE + 'calendar.readonly': {SCOPE + 'calendar', SCOPE + 'calendar.events'},
    SCOPE + 'calendar.events': {SCOPE + 'calendar'},
    SCOPE + 'drive.metadata.readonly': {SCOPE + 'drive', SCOPE + 'drive.readonly'},
    SCOPE + 'drive.readonly': {SCOPE + 'drive'},
    SCOPE + 'contacts.readonly': {SCOPE + 'contacts'},
    SCOPE + 'youtube.readonly': {SCOPE + 'youtube', SCOPE + 'youtube.force-ssl'},
}


def has_scope(granted, scope):
    granted = set(granted or ())
    return scope in granted or bool(IMPLIED.get(scope, set()) & granted)


def service_granted(granted, service):
    return all(has_scope(granted, s) for s in SERVICES[service]['scopes'])
# A cheap read per API, used to tell "API not enabled" apart from "not allowed".
API_PROBES = {
    'gmail.googleapis.com': ('gmail.search', GMAIL + '/profile'),
    'calendar-json.googleapis.com': ('calendar.read', CALENDAR + '/users/me/calendarList?maxResults=1'),
    'drive.googleapis.com': ('drive.search', DRIVE + '/about?fields=user(emailAddress)'),
    'people.googleapis.com': ('contacts.read', PEOPLE + '/people/me?personFields=names'),
    'youtube.googleapis.com': ('youtube.read', YOUTUBE + '/videos?part=id&id=dQw4w9WgXcQ'),
}


def normal_scopes(value):
    scopes = set()
    for item in (value or '').split():
        scopes.add(SCOPE_ALIASES.get(item, item))
    return scopes


class GoogleError(Exception):
    """A provider failure with a stable code the UI, MCP and apps can act on."""

    def __init__(self, code, status=502, message='', service=None):
        super().__init__(code)
        self.code, self.status, self.message, self.service = code, status, message or code, service


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise GoogleError('provider_redirect', 502, 'Google answered with a redirect.')


def classify(status, payload):
    """Map Google's error shapes (OAuth endpoint and API JSON errors) to our codes."""
    if not isinstance(payload, dict):
        payload = {}
    error = payload.get('error')
    if isinstance(error, str):  # OAuth token endpoint
        description = str(payload.get('error_description', ''))[:200]
        if error == 'invalid_grant':
            return GoogleError('reauth_required', 401, description or 'Google no longer accepts this sign-in.')
        if error == 'deleted_client':
            return GoogleError('client_deleted', 400, 'This client was deleted in Google Cloud (unused clients are removed after 6 months).')
        if error in ('invalid_client', 'unauthorized_client'):
            return GoogleError('client_invalid', 400, description or 'Google does not recognise this client ID and secret.')
        if error == 'redirect_uri_mismatch':
            return GoogleError('redirect_uri_mismatch', 400, description or 'The return address is not registered on the client.')
        return GoogleError('provider_rejected', 502, f'{error}: {description}'.strip(': '))
    error = error if isinstance(error, dict) else {}
    reasons = {str(e.get('reason', '')) for e in error.get('errors', []) if isinstance(e, dict)}
    service = None
    for detail in error.get('details', []) if isinstance(error.get('details'), list) else []:
        if isinstance(detail, dict) and detail.get('@type', '').endswith('google.rpc.ErrorInfo'):
            reasons.add(str(detail.get('reason', '')))
            meta = detail.get('metadata') if isinstance(detail.get('metadata'), dict) else {}
            if re.fullmatch(r'[a-z0-9-]+\.googleapis\.com', str(meta.get('service', ''))):
                service = meta['service']
    message = str(error.get('message', ''))[:300]
    if reasons & {'SERVICE_DISABLED', 'accessNotConfigured'}:
        return GoogleError('api_not_enabled', 409, message or 'This Google API is not enabled in your project.', service)
    if reasons & {'ACCESS_TOKEN_SCOPE_INSUFFICIENT', 'insufficientPermissions'}:
        return GoogleError('google_permission_missing', 403, message or 'Google has not been allowed to do this for the account.')
    if status == 401:
        return GoogleError('token_rejected', 401, message)
    if status == 404:
        return GoogleError('not_found', 404, message or 'Not found.')
    if status == 429 or reasons & {'rateLimitExceeded', 'userRateLimitExceeded', 'RATE_LIMIT_EXCEEDED'}:
        return GoogleError('rate_limited', 429, message or 'Google is rate limiting this account.')
    if status == 403:
        return GoogleError('provider_forbidden', 403, message)
    if 400 <= status < 500:
        return GoogleError('provider_rejected', 400, message)
    return GoogleError('provider_unavailable', 502, message or 'Google is unavailable.')


class Transport:
    def __init__(self, extra_hosts=()):
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        # The Wizard sign-in service (only when the owner uses Wizard's Google app).
        self.hosts = ALLOWED_HOSTS | {h for h in extra_hosts if h}

    def request(self, method, url, *, token=None, form=None, body=None, raw=False):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != 'https' or parts.hostname not in self.hosts or parts.username or parts.fragment:
            raise GoogleError('transport_policy', 500, 'Endpoint refused.')
        headers = {'Accept': 'application/json', 'User-Agent': 'wizard-google'}
        data = None
        if token:
            headers['Authorization'] = 'Bearer ' + token
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        elif body is not None:
            data = json.dumps(body).encode()
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self.opener.open(request, timeout=20) as response:
                content = response.read(MAX_RESPONSE + 1)
                kind = response.headers.get('Content-Type', '')
        except urllib.error.HTTPError as error:
            content = error.read(65536)
            error.close()
            try:
                payload = json.loads(content)
            except ValueError:
                payload = {}
            raise classify(error.code, payload) from None
        except GoogleError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError):
            raise GoogleError('provider_unavailable', 502, 'Could not reach Google.') from None
        if len(content) > MAX_RESPONSE:
            raise GoogleError('provider_too_large', 502, 'Google returned too much data.')
        if raw:
            return {'content': content, 'type': kind}
        if not content:
            return {}
        try:
            value = json.loads(content)
        except ValueError:
            raise GoogleError('provider_malformed', 502, 'Google returned a malformed response.') from None
        return value if isinstance(value, dict) else {'items': value}


# ---------- argument checks ----------

class BadArgs(ValueError):
    pass


def text(args, name, maximum, required=True, default=''):
    value = args.get(name, default)
    if value in (None, '') and not required:
        return default
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise BadArgs(f'{name} must be text up to {maximum} characters.')
    return value.strip()


def number(args, name, default, low, high):
    value = args.get(name, default)
    if type(value) is not int or not low <= value <= high:
        raise BadArgs(f'{name} must be a whole number from {low} to {high}.')
    return value


def ident(args, name, pattern=r'[A-Za-z0-9_@.:-]{1,256}'):
    value = args.get(name)
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise BadArgs(f'{name} is not a valid id.')
    return value


def addresses(value, name):
    if value in (None, '', []):
        return []
    items = value if isinstance(value, list) else [value]
    found = []
    for item in items:
        if not isinstance(item, str) or '\r' in item or '\n' in item or len(item) > 320:
            raise BadArgs(f'{name} has an invalid address.')
        for _, address in getaddresses([item]):
            if not re.fullmatch(r'[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+', address):
                raise BadArgs(f'{name} has an invalid address.')
            found.append(address)
    if len(found) > 50:
        raise BadArgs(f'{name} has too many addresses.')
    return found


def q(value):
    return urllib.parse.quote(value, safe='')


# ---------- Gmail ----------

def _headers(message):
    found = {}
    for header in message.get('payload', {}).get('headers', []):
        name = str(header.get('name', '')).lower()
        if name in ('from', 'to', 'cc', 'subject', 'date', 'message-id') and name not in found:
            found[name] = str(header.get('value', ''))[:500]
    return found


def _body_text(part, budget=MAX_TEXT):
    """Plain text of a message: text/plain parts first, else HTML with tags dropped."""
    plain, html = [], []

    def walk(node):
        mime = node.get('mimeType', '')
        data = node.get('body', {}).get('data')
        if data and mime in ('text/plain', 'text/html'):
            try:
                decoded = base64.urlsafe_b64decode(data + '=' * (-len(data) % 4)).decode('utf-8', 'replace')
            except ValueError:
                decoded = ''
            (plain if mime == 'text/plain' else html).append(decoded)
        for child in node.get('parts', []) or []:
            walk(child)

    walk(part)
    if plain:
        content = '\n'.join(plain)
    else:
        content = re.sub(r'<(script|style)[^>]*>.*?</\1>', ' ', '\n'.join(html), flags=re.S | re.I)
        content = re.sub(r'<[^>]+>', ' ', content)
        content = re.sub(r'[ \t]+', ' ', content)
    return content[:budget], len(content) > budget


def gmail_search(call, args):
    query = text(args, 'query', 500, required=False, default='')
    limit = number(args, 'max', 10, 1, 25)
    listing = call('GET', f'{GMAIL}/messages?' + urllib.parse.urlencode({'q': query, 'maxResults': limit}))
    results = []
    for item in listing.get('messages', [])[:limit]:
        mid = str(item.get('id', ''))
        if not re.fullmatch(r'[A-Za-z0-9]{1,64}', mid):
            continue
        message = call('GET', f'{GMAIL}/messages/{mid}?format=metadata&metadataHeaders=From&metadataHeaders=To&metadataHeaders=Subject&metadataHeaders=Date')
        h = _headers(message)
        results.append({'id': mid, 'thread_id': message.get('threadId'), 'from': h.get('from', ''), 'to': h.get('to', ''),
                        'subject': h.get('subject', ''), 'date': h.get('date', ''), 'snippet': str(message.get('snippet', ''))[:300],
                        'labels': message.get('labelIds', [])[:20]})
    return {'messages': results, 'more': bool(listing.get('nextPageToken'))}


def gmail_read(call, args):
    mid = ident(args, 'id', r'[A-Za-z0-9]{1,64}')
    message = call('GET', f'{GMAIL}/messages/{mid}?format=full')
    h = _headers(message)
    body, truncated = _body_text(message.get('payload', {}))
    return {'id': mid, 'thread_id': message.get('threadId'), 'from': h.get('from', ''), 'to': h.get('to', ''), 'cc': h.get('cc', ''),
            'subject': h.get('subject', ''), 'date': h.get('date', ''), 'message_id': h.get('message-id', ''),
            'body': body, 'truncated': truncated}


def gmail_send(call, args):
    to = addresses(args.get('to'), 'to')
    if not to:
        raise BadArgs('to needs at least one address.')
    cc = addresses(args.get('cc'), 'cc')
    subject = text(args, 'subject', 300)
    if '\r' in subject or '\n' in subject:
        raise BadArgs('subject must be one line.')
    content = text(args, 'body', 50_000)
    message = EmailMessage()
    message['To'] = ', '.join(to)
    if cc:
        message['Cc'] = ', '.join(cc)
    message['Subject'] = subject
    message.set_content(content)
    payload = {'raw': base64.urlsafe_b64encode(message.as_bytes()).decode().rstrip('=')}
    thread = args.get('thread_id')
    if thread is not None:
        payload['threadId'] = ident(args, 'thread_id', r'[A-Za-z0-9]{1,64}')
    sent = call('POST', f'{GMAIL}/messages/send', body=payload)
    return {'sent': True, 'id': sent.get('id'), 'thread_id': sent.get('threadId'), 'to': to, 'cc': cc}


# ---------- Calendar ----------

RFC3339 = r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})'


def _event(item):
    def when(value):
        value = value if isinstance(value, dict) else {}
        return value.get('dateTime') or value.get('date')
    return {'id': item.get('id'), 'summary': str(item.get('summary', ''))[:300], 'start': when(item.get('start')), 'end': when(item.get('end')),
            'location': str(item.get('location', ''))[:300], 'description': str(item.get('description', ''))[:2000],
            'attendees': [str(a.get('email', '')) for a in item.get('attendees', []) or [] if isinstance(a, dict)][:50],
            'link': item.get('htmlLink')}


def calendar_events(call, args):
    calendar = args.get('calendar_id') or 'primary'
    calendar = ident({'c': calendar}, 'c', r'[A-Za-z0-9_@.#-]{1,256}')
    query = {'singleEvents': 'true', 'orderBy': 'startTime', 'maxResults': number(args, 'max', 20, 1, 100)}
    for name in ('time_min', 'time_max'):
        if args.get(name) is not None:
            if not isinstance(args[name], str) or not re.fullmatch(RFC3339, args[name]):
                raise BadArgs(f'{name} must be an RFC 3339 time, e.g. 2026-10-20T09:00:00Z.')
            query['timeMin' if name == 'time_min' else 'timeMax'] = args[name]
    if args.get('query'):
        query['q'] = text(args, 'query', 200)
    listing = call('GET', f'{CALENDAR}/calendars/{q(calendar)}/events?' + urllib.parse.urlencode(query))
    return {'calendar_id': calendar, 'events': [_event(i) for i in listing.get('items', []) if isinstance(i, dict)]}


def calendar_create(call, args):
    calendar = ident({'c': args.get('calendar_id') or 'primary'}, 'c', r'[A-Za-z0-9_@.#-]{1,256}')
    event = {'summary': text(args, 'summary', 300)}
    for name in ('start', 'end'):
        value = args.get(name)
        if isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            event[name] = {'date': value}
        elif isinstance(value, str) and re.fullmatch(RFC3339, value):
            event[name] = {'dateTime': value}
        else:
            raise BadArgs(f'{name} must be a date (2026-10-20) or an RFC 3339 time.')
    if args.get('description'):
        event['description'] = text(args, 'description', 8000)
    if args.get('location'):
        event['location'] = text(args, 'location', 300)
    guests = addresses(args.get('attendees'), 'attendees')
    if guests:
        event['attendees'] = [{'email': a} for a in guests]
    created = call('POST', f'{CALENDAR}/calendars/{q(calendar)}/events', body=event)
    return {'created': True, 'event': _event(created)}


# ---------- Drive ----------

DRIVE_FIELDS = 'id,name,mimeType,modifiedTime,size,webViewLink,owners(emailAddress)'
EXPORTS = {'application/vnd.google-apps.document': 'text/plain', 'application/vnd.google-apps.spreadsheet': 'text/csv',
           'application/vnd.google-apps.presentation': 'text/plain'}


def _file(item):
    return {'id': item.get('id'), 'name': str(item.get('name', ''))[:300], 'mime_type': item.get('mimeType'),
            'modified': item.get('modifiedTime'), 'size': item.get('size'), 'link': item.get('webViewLink')}


def drive_search(call, args):
    name = text(args, 'query', 200, required=False, default='')
    clause = 'trashed = false'
    if name:
        clause += " and name contains '" + name.replace('\\', '\\\\').replace("'", "\\'") + "'"
    query = {'q': clause, 'pageSize': number(args, 'max', 20, 1, 100), 'fields': f'files({DRIVE_FIELDS}),nextPageToken', 'orderBy': 'modifiedTime desc'}
    listing = call('GET', f'{DRIVE}/files?' + urllib.parse.urlencode(query))
    return {'files': [_file(i) for i in listing.get('files', []) if isinstance(i, dict)], 'more': bool(listing.get('nextPageToken'))}


def drive_read(call, args):
    fid = ident(args, 'file_id', r'[A-Za-z0-9_-]{10,200}')
    meta = call('GET', f'{DRIVE}/files/{fid}?fields={DRIVE_FIELDS}')
    mime = str(meta.get('mimeType', ''))
    result = {'file': _file(meta), 'text': None, 'truncated': False}
    if mime in EXPORTS:
        content = call('GET', f'{DRIVE}/files/{fid}/export?' + urllib.parse.urlencode({'mimeType': EXPORTS[mime]}), raw=True)['content']
    elif mime.startswith('text/') or mime in ('application/json', 'application/xml'):
        content = call('GET', f'{DRIVE}/files/{fid}?alt=media', raw=True)['content']
    else:
        result['note'] = 'Only Google Docs, Sheets, Slides and text files are read as text.'
        return result
    decoded = content.decode('utf-8', 'replace')
    result['text'], result['truncated'] = decoded[:MAX_TEXT], len(decoded) > MAX_TEXT
    return result


# ---------- Contacts, YouTube ----------

def contacts_search(call, args):
    query = {'query': text(args, 'query', 200), 'readMask': 'names,emailAddresses,phoneNumbers,organizations', 'pageSize': number(args, 'max', 10, 1, 30)}
    found = call('GET', f'{PEOPLE}/people:searchContacts?' + urllib.parse.urlencode(query))
    people = []
    for result in found.get('results', []):
        person = result.get('person', {}) if isinstance(result, dict) else {}
        people.append({'name': next((n.get('displayName') for n in person.get('names', []) if n.get('displayName')), ''),
                       'emails': [e.get('value') for e in person.get('emailAddresses', []) if e.get('value')][:5],
                       'phones': [p.get('value') for p in person.get('phoneNumbers', []) if p.get('value')][:5],
                       'organization': next((o.get('name') for o in person.get('organizations', []) if o.get('name')), '')})
    return {'people': people}


def youtube_video(call, args):
    vid = ident(args, 'video_id', r'[A-Za-z0-9_-]{11}')
    found = call('GET', f'{YOUTUBE}/videos?' + urllib.parse.urlencode({'part': 'snippet,contentDetails', 'id': vid, 'maxResults': 1}))
    items = [{'id': i.get('id'), 'title': str(i.get('snippet', {}).get('title', ''))[:300], 'channel': i.get('snippet', {}).get('channelTitle'),
              'duration': i.get('contentDetails', {}).get('duration')} for i in found.get('items', []) if i.get('id') == vid]
    return {'videos': items}


HANDLERS = {
    'gmail.search': gmail_search, 'gmail.read': gmail_read, 'gmail.send': gmail_send,
    'calendar.read': calendar_events, 'calendar.write': calendar_create,
    'drive.search': drive_search, 'drive.read': drive_read,
    'contacts.read': contacts_search, 'youtube.read': youtube_video,
}
assert set(HANDLERS) == set(OPERATIONS)
