"""Real wizard-google broker (from WG_ROOT) with its test fakes standing in for Composio/Google, served in the
wizard.mail-calendar/1 /v1 shape so a real Calendar can read it. An agent adds one event at Can edit first."""
import json, os, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
root = os.environ['WG_ROOT']
sys.path[:0] = [root + '/app', root + '/tests']
import broker as broker_mod
import test_quick as tq

fake, google = tq.FakeComposio(), tq.FakeGoogle()
b = broker_mod.Broker(tempfile.mkdtemp(), transport=google, composio_transport=fake, clock=tq.Clock())
q = b.quick
q.add_key(tq.GOOD)
def connect(service, account=None, session='s1'):
    q.start(session, service, account, 'https://box.example/wizard-google/quick/callback'); fake.activate()
    return q.status(session)['result']
first = connect('gmail'); connect('calendar', first['account'])
fake.events['ev0'].update({'summary': 'Standup', 'start': {'dateTime': '2026-11-04T09:00:00+07:00'}, 'end': {'dateTime': '2026-11-04T09:30:00+07:00'}})
b.access.set(calendar='can_edit')
made = b.mailcal_call('agent:main', 'calendar_create_event', {'title': 'Dentist', 'start': '2026-11-04T14:00:00+07:00', 'end': '2026-11-04T15:00:00+07:00'})
print('agent made', made['event'].get('id'), 'by_agent' in made['event'] and made['event']['by_agent'], flush=True)
GRANT = 'grant-google-0123456789'

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def reply(self, status, value):
        raw = json.dumps(value).encode(); self.send_response(status)
        self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def do_GET(self):
        self.reply(200, b.agent_status())
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'{}')
        tool = self.path.rsplit('/', 1)[1]
        caller = 'app:wizard-calendar' if self.headers.get('X-Wizard-Grant') == GRANT else 'agent:' + (self.headers.get('X-Wizard-Agent') or 'main')
        try:
            self.reply(200, b.mailcal_call(caller, tool, body))
        except Exception as e:
            self.reply(getattr(e, 'status', 500), {'error': {'code': getattr(e, 'code', 'x'), 'message': str(e)}})

srv = ThreadingHTTPServer(('127.0.0.1', int(os.environ['SHIM_PORT'])), H)
print('ready', flush=True)
srv.serve_forever()
