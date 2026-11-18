"""Calendar edits on the Connected accounts sign-in (wizard-connectors#29; its switch google-calendar-edits, off).

Real HTTP server and handlers; Connected accounts is a fake HTTP server speaking its /service/google/v1/calendar_*
contract (wizard-connectors docs/GOOGLE_TOOLS.md). This app has no Google sign-in of its own in these tests, and
Calendar > Settings > Agent access (agent_access.py, this app) decides what agents may do.
"""
import hashlib
import json
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from agent_access import AgentAccess
from test_http import Client, start

GRANT = 'FAKE-outgoing-grant-to-connected-accounts'
EVENTS = 'https://www.googleapis.com/calendar/v3/calendars/primary/events'


class FakeCalendarEdits(BaseHTTPRequestHandler):
    calls = []
    on = True  # Connected accounts' switch google-calendar-edits
    accounts = [{'email': 'fake@example.invalid', 'status': 'active', 'calendar_edits': True}]

    def log_message(self, *args):
        pass

    def answer(self, status, value):
        raw = json.dumps(value).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def handle_one(self, body):
        tool = self.path[len('/service/google/v1/'):]
        FakeCalendarEdits.calls.append((self.command, tool, dict(self.headers), body))
        if self.headers.get('X-Wizard-Grant') != GRANT:
            return self.answer(403, {'error': {'code': 'google_grant_required', 'message': 'Only the Google app may use this.'}})
        if not FakeCalendarEdits.on:
            return self.answer(409, {'error': {'code': 'service_off', 'message': 'Calendar edits for your wizard are off.'}})
        if tool == 'calendar_status':
            return self.answer(200, {'accounts': FakeCalendarEdits.accounts, 'blocker': None})
        if tool != 'calendar_http':
            return self.answer(404, {'error': {'code': 'not_found', 'message': 'Not found.'}})
        via = {'source': 'google', 'via': 'connected-accounts', 'account': body.get('account')}
        method, url = body['method'], body['url']
        if method == 'POST' and url == EVENTS:
            return self.answer(200, {**via, 'response': {'id': 'ev1', 'status': 'confirmed', **body['body']}})
        if method == 'PATCH' and url == EVENTS + '/ev1':
            return self.answer(200, {**via, 'response': {'id': 'ev1', 'summary': 'FAKE dentist', **body['body']}})
        if method == 'DELETE' and url == EVENTS + '/ev1':
            return self.answer(200, {**via, 'response': {}})
        if method == 'GET' and url == EVENTS:
            return self.answer(200, {**via, 'response': {'items': [{'id': 'ev1', 'summary': 'FAKE dentist',
                                                                    'start': {'dateTime': '2026-11-20T10:00:00+01:00'}}]}})
        return self.answer(400, {'error': {'code': 'invalid_arguments', 'message': 'Google could not do that.'}})

    def do_GET(self):
        self.handle_one(None)

    def do_POST(self):
        self.handle_one(json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0')))))


class ConnectedCalendarEdits(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.ca = ThreadingHTTPServer(('127.0.0.1', 0), FakeCalendarEdits)
        threading.Thread(target=cls.ca.serve_forever, daemon=True).start()
        cls.calendar_token = 'grant-token-for-wizard-calendar'
        cls.grants_file = Path(cls.dir) / 'grants.json'
        cls.grants_file.write_text(json.dumps({
            'enforce': True, 'incoming': [{'from': 'wizard-calendar', 'sha256': hashlib.sha256(cls.calendar_token.encode()).hexdigest()}],
            'outgoing': {'connected-accounts': GRANT},
            'provider_endpoints': {'connected-accounts': f'http://127.0.0.1:{cls.ca.server_address[1]}'}}))
        cls.srv, cls.app, cls.fake = start({'PORT': '0', 'DATA_DIR': cls.dir + '/data', 'WIZARD_GRANTS_FILE': str(cls.grants_file)})
        cls.port = cls.srv.server_address[1]
        config = cls.app.config
        config.port = cls.port
        config.local_hosts = {f'127.0.0.1:{cls.port}', f'localhost:{cls.port}'}
        config.trusted_hosts = set(config.local_hosts)
        config.trusted_origins = {f'http://{h}' for h in config.local_hosts}
        Path(cls.dir, 'data').mkdir(parents=True, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.ca.shutdown()
        shutil.rmtree(cls.dir, ignore_errors=True)

    def setUp(self):
        FakeCalendarEdits.calls.clear()
        FakeCalendarEdits.on = True
        FakeCalendarEdits.accounts = [{'email': 'fake@example.invalid', 'status': 'active', 'calendar_edits': True}]
        self.level('can_edit')

    def level(self, calendar):
        AgentAccess(Path(self.dir, 'data')).set(calendar=calendar)

    def call(self, name, args=None, headers=None):
        status, value, _ = Client(self.port).send('POST', '/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                                                    'params': {'name': name, 'arguments': args or {}}}, headers, browser=False)
        self.assertEqual(status, 200, value)
        return value['result']['structuredContent']

    def http_calls(self):
        return [(c[3]['method'], c[3]['url']) for c in FakeCalendarEdits.calls if c[1] == 'calendar_http']

    def test_can_edit_creates_and_moves_an_event_on_the_connected_accounts_sign_in(self):
        # The issue's done-when: only a Connected accounts sign-in, its switch on, Agent access allows it.
        made = self.call('calendar_create_event', {'title': 'Dentist', 'start': '2026-11-20T10:00:00+01:00',
                                                   'end': '2026-11-20T11:00:00+01:00'}, {'X-Wizard-Agent': 'helper'})
        self.assertEqual((made['created'], made['event']['id'], made['via'], made['account']),
                         (True, 'ev1', 'connected-accounts', 'fake@example.invalid'))
        self.assertTrue(made['event']['by_agent'])  # the same marker as on this app's own sign-in
        _, _, headers, body = FakeCalendarEdits.calls[-1]
        self.assertEqual((headers['X-Wizard-Agent'], headers['X-Wizard-Grant'], body['query']), ('helper', GRANT, {'sendUpdates': 'all'}))
        moved = self.call('calendar_update_event', {'id': 'ev1', 'start': '2026-11-20T14:00:00+01:00', 'end': '2026-11-20T15:00:00+01:00'})
        self.assertEqual((moved['updated'], moved['event']['start']['date_time']), (True, '2026-11-20T14:00:00+01:00'))
        listed = self.call('calendar_list_events', {})
        self.assertEqual(listed['events'][0]['id'], 'ev1')
        self.assertEqual(self.call('calendar_delete_event', {'id': 'ev1'})['deleted'], True)
        self.assertEqual(self.http_calls(), [('POST', EVENTS), ('PATCH', EVENTS + '/ev1'), ('GET', EVENTS), ('DELETE', EVENTS + '/ev1')])

    def test_refused_when_agent_access_does_not_allow_it(self):
        for level in ('read_only', 'no_access'):
            self.level(level)
            err = self.call('calendar_create_event', {'title': 'x', 'start': '2026-11-20', 'end': '2026-11-21'})['error']
            self.assertEqual(err['code'], 'agent_access_denied', level)
            self.assertIn('Calendar > Settings > Agent access', err['message'])
        self.assertEqual(self.call('calendar_list_events', {})['error']['code'], 'agent_access_denied')
        self.assertEqual(self.http_calls(), [])  # refused here: Connected accounts never saw a change

    def test_suggest_only_waits_for_the_owners_approve_in_calendar(self):
        self.level('suggest_only')
        made = self.call('calendar_update_event', {'id': 'ev1', 'start': '2026-11-20T14:00:00+01:00', 'end': '2026-11-20T15:00:00+01:00'})
        self.assertEqual((made['suggested'], made['applied'], made['account']), (True, False, 'fake@example.invalid'))
        self.assertEqual(self.http_calls(), [])
        status, done, _ = Client(self.port).send('POST', '/v1/calendar_suggestion_approve', {'id': made['suggestion']['id']},
                                                 {'X-Wizard-Grant': self.calendar_token}, browser=False)
        self.assertEqual((status, done['approved'], done['result']['via']), (200, True, 'connected-accounts'), done)
        _, _, headers, body = FakeCalendarEdits.calls[-1]
        self.assertEqual((headers.get('X-Wizard-Owner'), 'X-Wizard-Agent' in headers, body['method']), ('1', False, 'PATCH'))

    def test_switch_off_or_no_allow_keeps_the_old_answer(self):
        FakeCalendarEdits.on = False
        err = self.call('calendar_create_event', {'title': 'x', 'start': '2026-11-20', 'end': '2026-11-21'})['error']
        self.assertEqual(err['code'], 'not_connected')  # this app's own answer, as before
        FakeCalendarEdits.on = True
        FakeCalendarEdits.accounts = [{'email': 'fake@example.invalid', 'status': 'active', 'calendar_edits': False}]
        self.assertEqual(self.call('calendar_list_events', {})['error']['code'], 'not_connected')
        self.assertEqual(self.http_calls(), [])
        FakeCalendarEdits.accounts = [{'email': 'a@example.invalid', 'status': 'active', 'calendar_edits': True},
                                      {'email': 'b@example.invalid', 'status': 'active', 'calendar_edits': True}]
        err = self.call('calendar_list_events', {})['error']
        self.assertEqual((err['code'], err['accounts']), ('account_required', ['a@example.invalid', 'b@example.invalid']))
        self.assertEqual(self.call('calendar_list_events', {'account': 'B@example.invalid'})['account'], 'b@example.invalid')

    def test_not_linked_to_connected_accounts_keeps_the_old_answer(self):
        doc = json.loads(self.grants_file.read_text())
        self.grants_file.write_text(json.dumps({**doc, 'outgoing': {}}))
        try:
            self.assertEqual(self.call('calendar_list_events', {})['error']['code'], 'not_connected')
            self.assertEqual(FakeCalendarEdits.calls, [])
        finally:
            self.grants_file.write_text(json.dumps(doc))


if __name__ == '__main__':
    unittest.main()
