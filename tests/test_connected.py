"""The Google app on the Connected accounts sign-in (wizard-google#20; always on since 0.5.3).

Real HTTP server and handlers; Connected accounts is a fake HTTP server speaking its /service/google/v1 contract
(wizard-connectors docs/GOOGLE_TOOLS.md). No Google sign-in of this app's own exists in these tests.
"""
import hashlib
import json
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from test_http import Client, start

GRANT = 'FAKE-outgoing-grant-to-connected-accounts'


class FakeConnectedAccounts(BaseHTTPRequestHandler):
    calls = []
    level = 'draft_only'

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
        FakeConnectedAccounts.calls.append((self.command, tool, dict(self.headers), body))
        if self.headers.get('X-Wizard-Grant') != GRANT:
            return self.answer(403, {'error': {'code': 'google_grant_required', 'message': 'Only the Google app may use this.'}})
        if tool == 'status':
            return self.answer(200, {'accounts': [{'email': 'fake@example.invalid', 'status': 'active', 'services': ['gmail', 'drive', 'contacts']}],
                                     'blocker': None, 'mail_access': {'level': self.level}})
        via = {'source': 'google', 'via': 'connected-accounts', 'account': 'fake@example.invalid'}
        if tool == 'drive_search':
            return self.answer(200, {**via, 'files': [{'id': 'doc1', 'name': 'FAKE plan', 'mime_type': 'application/vnd.google-apps.document'}]})
        if tool == 'drive_read':
            return self.answer(200, {**via, 'file': {'id': body['file_id']}, 'text': 'FAKE doc text', 'untrusted': True, 'truncated': False})
        if tool == 'contacts_search':
            return self.answer(200, {**via, 'contacts': [{'name': 'FAKE Ada', 'emails': ['ada@example.invalid']}]})
        if tool == 'mail_draft':
            if self.level != 'draft_only':
                return self.answer(403, {'error': {'code': 'agent_access_denied', 'message': "The owner set agents' mail to Read-only."}})
            return self.answer(200, {**via, 'contract': 'wizard.mail-calendar/1', 'sent': False, 'draft': {'id': 'd1'}})
        if tool == 'mail_search':
            return self.answer(200, {**via, 'contract': 'wizard.mail-calendar/1', 'messages': []})
        return self.answer(404, {'error': {'code': 'not_found', 'message': 'Not found.'}})

    def do_GET(self):
        self.handle_one(None)

    def do_POST(self):
        self.handle_one(json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0')))))


class ConnectedAccountsSignIn(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.ca = ThreadingHTTPServer(('127.0.0.1', 0), FakeConnectedAccounts)
        threading.Thread(target=cls.ca.serve_forever, daemon=True).start()
        cls.grants_file = Path(cls.dir) / 'grants.json'
        cls.grants_file.write_text(json.dumps({
            'enforce': True, 'incoming': [], 'outgoing': {'connected-accounts': GRANT},
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
        FakeConnectedAccounts.calls.clear()
        FakeConnectedAccounts.level = 'draft_only'

    def call(self, name, args=None, headers=None):
        status, value, _ = Client(self.port).send('POST', '/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                                                    'params': {'name': name, 'arguments': args or {}}}, headers, browser=False)
        self.assertEqual(status, 200, value)
        return value['result']['structuredContent']

    def names(self):
        status, value, _ = Client(self.port).send('POST', '/mcp', {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}, browser=False)
        return {t['name'] for t in value['result']['tools']}

    def test_no_switch_left_and_an_old_off_value_is_ignored(self):
        # Approved at show-and-tell (wizard-google#20): no switch, and a saved 'off' from before changes nothing.
        who = {'Tailscale-User-Login': 'owner@example.com', 'X-Wizard-Switch': '1'}
        self.app.config.owner_logins = {'owner@example.com'}
        self.app.config.shell_auth = 'tailscale-serve'
        c = Client(self.port, 'box.tail1234.ts.net', 'https://box.tail1234.ts.net')
        self.app.config.trusted_hosts.add('box.tail1234.ts.net')
        status, doc, _ = c.send('GET', '/api/switches', headers=who, browser=False)
        self.assertEqual(status, 200, doc)
        self.assertNotIn('connected-accounts', [s['id'] for s in doc['switches']])
        Path(self.dir, 'data', 'switches.json').write_text(json.dumps({'connected-accounts': False}))
        try:
            self.assertTrue({'drive_search', 'drive_read', 'contacts_search'} <= self.names())
        finally:
            Path(self.dir, 'data', 'switches.json').write_text('{}')

    def test_lists_drive_reads_doc_finds_contact_and_drafts_without_own_sign_in(self):
        # The issue's done-when: only a Connected accounts sign-in, no Google app sign-in.
        self.assertTrue({'drive_search', 'drive_read', 'contacts_search', 'mail_draft'} <= self.names())
        status = self.call('google_status')
        self.assertTrue(status['connected'])
        self.assertEqual(status['connected_accounts']['accounts'][0]['email'], 'fake@example.invalid')
        self.assertEqual(self.call('drive_search', {'query': 'plan'})['files'][0]['id'], 'doc1')
        self.assertEqual(self.call('drive_read', {'file_id': 'doc1'})['text'], 'FAKE doc text')
        self.assertEqual(self.call('contacts_search', {'query': 'Ada'})['contacts'][0]['name'], 'FAKE Ada')
        draft = self.call('mail_draft', {'to': ['a@example.invalid'], 'subject': 'Hi', 'body': 'Hello'}, {'X-Wizard-Agent': 'helper'})
        self.assertEqual((draft['sent'], draft['draft']['id'], draft['via']), (False, 'd1', 'connected-accounts'))
        method, tool, headers, body = FakeConnectedAccounts.calls[-1]
        self.assertEqual((method, tool, headers['X-Wizard-Agent'], headers['X-Wizard-Grant']), ('POST', 'mail_draft', 'helper', GRANT))
        self.assertEqual(body['subject'], 'Hi')
        # The box-only HTTP mirror goes the same way.
        status, value, _ = Client(self.port).send('POST', '/v1/drive_search', {}, browser=False)
        self.assertEqual((status, value['files'][0]['id']), (200, 'doc1'))

    def test_owner_level_errors_and_sending_stay_where_they_were(self):
        FakeConnectedAccounts.level = 'read_only'
        err = self.call('mail_draft', {'to': ['a@example.invalid']})['error']
        self.assertEqual(err['code'], 'agent_access_denied')
        # Sending is never an agent's and never goes to Connected accounts.
        self.assertEqual(self.call('mail_send', {'draft_id': 'd1'})['error']['code'], 'agent_send_refused')
        self.assertNotIn('mail_send', [c[1] for c in FakeConnectedAccounts.calls])
        # Mail's own tap (its App Manager grant) keeps this app's own accounts.
        token = 'grant-token-for-wizard-mail-app'
        doc = json.loads(self.grants_file.read_text())
        self.grants_file.write_text(json.dumps({**doc, 'incoming': [{'from': 'wizard-mail', 'sha256': hashlib.sha256(token.encode()).hexdigest()}]}))
        try:
            before = len(FakeConnectedAccounts.calls)
            status, value, _ = Client(self.port).send('POST', '/v1/mail_search', {}, {'X-Wizard-Grant': token}, browser=False)
            self.assertEqual(value['error']['code'], 'not_connected')
            self.assertEqual(len(FakeConnectedAccounts.calls), before)
        finally:
            self.grants_file.write_text(json.dumps(doc))

    def test_not_linked_yet_is_a_plain_answer(self):
        doc = json.loads(self.grants_file.read_text())
        self.grants_file.write_text(json.dumps({**doc, 'outgoing': {}}))
        try:
            err = self.call('drive_search')['error']
            self.assertEqual(err['code'], 'connected_accounts_unavailable')
            self.assertIn('approve', err['message'])
            self.assertIn('error', self.call('google_status')['connected_accounts'])
        finally:
            self.grants_file.write_text(json.dumps(doc))


if __name__ == '__main__':
    unittest.main()
