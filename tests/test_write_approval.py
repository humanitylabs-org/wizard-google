"""One-shot agent write approvals; all provider traffic is labelled FakeGoogle."""
import copy
import json
import threading
from unittest.mock import patch

import google
from broker import Broker, Problem, WRITE_TTL
from store import Store
from test_broker import Base


MAIL = {'to': ['anna@example.com'], 'subject': 'Lunch', 'body': 'Thursday at noon?'}
EVENT = {'summary': 'Lunch', 'start': '2026-10-22T12:00:00Z', 'end': '2026-10-22T13:00:00Z',
         'attendees': ['anna@example.com'], 'description': 'Bring notes', 'location': 'Cafe'}


class WriteApproval(Base):
    def setUp(self):
        super().setUp()
        self.b = self.broker
        self.account = self.connect(ops=['gmail.send', 'calendar.write', 'gmail.search'])[0]['account']
        self.b.set_grant('agent:main', self.account, ['gmail.send', 'calendar.write', 'gmail.search'])

    def problem(self, code, fn, *args, **kw):
        with self.assertRaises(Problem) as caught:
            fn(*args, **kw)
        self.assertEqual(code, caught.exception.code)

    def propose(self, args=None, operation='gmail.send', caller='agent:main'):
        return self.b.call(caller, operation, copy.deepcopy(args or MAIL), self.account)['request']

    def approve(self, request, yes=True, **kw):
        return self.b.decide(request['id'], yes, payload_hash=request['payload_hash'], **kw)['request']

    def test_agent_send_without_approval_does_not_send_or_refresh(self):
        calls = len(self.fake.calls)
        r = self.propose()
        self.assertEqual('pending', r['status'])
        self.assertEqual('write', r['kind'])
        self.assertEqual(WRITE_TTL, r['expires'] - r['created'])
        self.assertEqual([], self.fake.sent)
        self.assertEqual(calls, len(self.fake.calls))
        self.assertEqual(r['id'], self.propose()['id'])  # retry while waiting dedupes exact packet
        self.assertEqual(r, self.b.caller_status('agent:main')['waiting'][0])
        self.assertEqual([], self.b.caller_status('agent:other')['write_requests'])

    def test_approve_executes_exactly_once_and_every_next_write_needs_a_new_card(self):
        r = self.propose()
        out = self.approve(r)
        self.assertEqual('executed', out['status'])
        self.assertEqual('sent1', out['result']['id'])
        self.assertEqual(1, len(self.fake.sent))
        self.assertNotIn('args', out)
        self.problem('request_executed', self.approve, r)
        again = self.propose()
        self.assertNotEqual(r['id'], again['id'])
        self.assertEqual(1, len(self.fake.sent))
        self.assertNotIn('Lunch', json.dumps(self.b.state['audit']))

    def test_payload_change_after_approval_cannot_reuse_approval(self):
        r = self.propose()
        self.approve(r)
        changed = self.propose(dict(MAIL, body='Different content', to=['elsewhere@example.com']))
        self.assertNotEqual(changed['payload_hash'], r['payload_hash'])
        self.problem('payload_changed', self.b.decide, changed['id'], True, payload_hash=r['payload_hash'])
        self.assertEqual('pending', self.b.state['requests'][changed['id']]['status'])
        self.assertEqual(1, len(self.fake.sent))

    def test_snapshot_and_caller_payload_are_detached_and_tampering_is_refused(self):
        args = copy.deepcopy(MAIL)
        r = self.b.call('agent:main', 'gmail.send', args, self.account)['request']
        args['to'][0] = 'evil@example.com'
        r['args']['body'] = 'not the original'
        self.assertEqual(MAIL, self.b.state['requests'][r['id']]['args'])
        self.b.state['requests'][r['id']]['args']['body'] = 'tampered storage'
        self.problem('payload_changed', self.approve, r)
        self.assertEqual([], self.fake.sent)

    def test_missing_wrong_hash_and_changed_account_fail_closed(self):
        r = self.propose()
        self.problem('payload_changed', self.b.decide, r['id'], True)
        self.problem('payload_changed', self.b.decide, r['id'], True, payload_hash='0' * 64)
        self.problem('payload_changed', self.approve, r, account='g-0000000000000000')
        self.assertEqual([], self.fake.sent)

    def test_expiry_at_boundary_is_durable_and_sends_nothing(self):
        r = self.propose()
        self.b.clock = lambda: r['expires']
        self.problem('request_expired', self.approve, r)
        self.assertEqual([], self.b.home('http://127.0.0.1:18812', '/')['requests'])
        self.assertNotIn('args', Store(self.dir).load()['requests'][r['id']])
        self.assertEqual([], self.fake.sent)

    def test_deny_cannot_be_reapproved_or_retried(self):
        r = self.propose()
        self.assertEqual('declined', self.approve(r, False)['status'])
        self.problem('request_declined', self.approve, r)
        self.assertEqual([], self.fake.sent)

    def test_calendar_writes_use_the_same_gate(self):
        r = self.propose(EVENT, 'calendar.write')
        self.assertFalse(any(m == 'POST' and '/calendar/' in u for m, u in self.fake.calls))
        self.assertEqual('executed', self.approve(r)['status'])
        self.problem('request_executed', self.approve, r)
        self.assertEqual(1, sum(m == 'POST' and '/calendar/' in u for m, u in self.fake.calls))

    def test_catalogue_write_flag_not_operation_name_enforces_gate(self):
        with patch.dict(google.OPERATIONS, {'future.write': dict(google.OPERATIONS['gmail.send'])}), \
             patch.dict(google.HANDLERS, {'future.write': google.HANDLERS['gmail.send']}), \
             patch.dict(google.WRITE_REVIEW, {'future.write': google.review_gmail}):
            self.b.set_grant('agent:main', self.account, ['future.write'])
            self.propose(operation='future.write')
            self.assertEqual([], self.fake.sent)

    def test_app_standing_grant_sends_without_a_card_and_agent_reads_unchanged(self):
        self.b.set_grant('app:mail', self.account, ['gmail.send'])
        self.assertEqual('sent1', self.b.call('app:mail', 'gmail.send', MAIL, self.account)['id'])
        self.assertEqual(1, len(self.fake.sent))
        self.assertEqual({}, self.b.state['requests'])
        self.assertEqual('m1', self.b.call('agent:main', 'gmail.search', {}, self.account)['messages'][0]['id'])

    def test_grant_changes_do_not_approve_a_write_and_revocation_blocks_execution(self):
        r = self.propose()
        self.b.set_grant('agent:main', self.account, ['gmail.send'])
        self.assertEqual('pending', self.b.state['requests'][r['id']]['status'])
        access = self.b.request_access('agent:main', ['gmail.send'], self.account)['request']
        self.assertNotEqual(r['id'], access['id'])
        self.b.decide(access['id'], True)
        self.assertEqual('pending', self.b.state['requests'][r['id']]['status'])
        self.b.set_grant('agent:main', self.account, [])
        out = self.approve(r)
        self.assertEqual('failed', out['status'])
        self.assertEqual('grant_required', out['error']['code'])
        self.assertEqual([], self.fake.sent)

    def test_disconnected_account_cannot_execute(self):
        r = self.propose()
        self.b.disconnect(self.account)
        self.assertEqual('failed', self.approve(r)['status'])
        self.assertEqual([], self.fake.sent)

    def test_concurrent_approvals_have_one_durable_claim(self):
        r = self.propose()
        started, release = threading.Event(), threading.Event()
        real = self.fake.request
        def slow(method, url, **kw):
            if url.endswith('/messages/send'):
                self.assertEqual('executing', Store(self.dir).load()['requests'][r['id']]['status'])
                started.set()
                self.assertTrue(release.wait(3))
            return real(method, url, **kw)
        self.fake.request = slow
        outcomes = []
        def approve():
            try:
                outcomes.append(self.approve(r)['status'])
            except Problem as p:
                outcomes.append(p.code)
        first = threading.Thread(target=approve)
        first.start()
        self.assertTrue(started.wait(3))
        second = threading.Thread(target=approve)
        second.start()
        release.set()
        first.join(3); second.join(3)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertCountEqual(['executed', 'request_executed'], outcomes)
        self.assertEqual(1, len(self.fake.sent))

    def test_persisted_pending_survives_restart_and_claim_is_not_replayed(self):
        r = self.propose()
        self.b = Broker(self.dir, transport=self.fake)
        self.assertEqual('pending', self.b.caller_status('agent:main')['write_requests'][0]['status'])
        self.approve(r)
        self.b = Broker(self.dir, transport=self.fake)
        self.problem('request_executed', self.approve, r)
        self.assertEqual(1, len(self.fake.sent))
        r2 = self.propose(dict(MAIL, subject='Interrupted'))
        state = self.b._copy()
        state['requests'][r2['id']]['status'] = 'executing'
        self.b._commit(state)
        self.b = Broker(self.dir, transport=self.fake)
        self.problem('request_unknown', self.approve, r2)
        self.assertEqual(1, len(self.fake.sent))

    def test_ambiguous_provider_failure_is_not_replayed(self):
        r = self.propose()
        real = self.fake.request
        def timeout_after_send(method, url, **kw):
            result = real(method, url, **kw)
            if url.endswith('/messages/send'):
                raise google.classify(503, {'error': {'message': 'Timeout after acceptance'}})
            return result
        self.fake.request = timeout_after_send
        self.assertEqual('unknown', self.approve(r)['status'])
        self.problem('request_unknown', self.approve, r)
        self.assertEqual(1, len(self.fake.sent))

    def test_invalid_payload_never_creates_an_approval(self):
        self.problem('invalid_arguments', self.propose, dict(MAIL, to=['bad\r\nBcc: evil@example.com']))
        self.assertEqual({}, self.b.state['requests'])
        self.assertEqual([], self.fake.sent)

    def test_unknown_fields_cannot_create_decoy_rows(self):
        for args in (dict(MAIL, Message='decoy'), dict(MAIL, To='anna@example.com'), dict(MAIL, approval_id='old')):
            self.problem('invalid_arguments', self.propose, args)
        self.problem('invalid_arguments', self.propose, dict(EVENT, Event='decoy'), 'calendar.write')
        self.assertEqual({}, self.b.state['requests'])

    def test_normalized_packet_shows_and_sends_bare_addresses_and_stripped_text(self):
        r = self.propose(dict(MAIL, to=['"anna@example.com" <actual@example.com>'], cc=['Friend <cc@example.com>'],
                              subject='  Lunch  ', body='  Hello!  '))
        self.assertEqual(['actual@example.com'], r['args']['to'])
        self.assertEqual(['cc@example.com'], r['args']['cc'])
        self.assertEqual('Lunch', r['args']['subject'])
        self.assertEqual('Hello!', r['args']['body'])
        receipt = self.approve(r)
        self.assertNotIn('to', receipt['result'])
        self.assertIn('To: actual@example.com', self.fake.sent[0][1])
        self.assertIn('Cc: cc@example.com', self.fake.sent[0][1])

    def test_non_ascii_and_control_addresses_fail_closed(self):
        for address in ('anna\u202e@example.com', 'anna\x01@example.com', 'naïve@example.com'):
            self.problem('invalid_arguments', self.propose, dict(MAIL, to=[address]))
        self.assertEqual({}, self.b.state['requests'])

    def test_calendar_review_materializes_defaults_and_receipt_drops_content(self):
        r = self.propose(dict(EVENT, summary='  Lunch  ', attendees=['Friend <anna@example.com>']), 'calendar.write')
        self.assertEqual('primary', r['args']['calendar_id'])
        self.assertEqual('Lunch', r['args']['summary'])
        self.assertEqual(['anna@example.com'], r['args']['attendees'])
        out = self.approve(r)
        self.assertNotIn('args', out)
        self.assertEqual({'id', 'link'}, set(out['result']['event']))

    def test_future_write_without_review_schema_is_refused_not_executed(self):
        with patch.dict(google.OPERATIONS, {'future.write': dict(google.OPERATIONS['gmail.send'])}), \
             patch.dict(google.HANDLERS, {'future.write': google.HANDLERS['gmail.send']}):
            self.b.set_grant('agent:main', self.account, ['future.write'])
            self.problem('invalid_arguments', self.propose, operation='future.write')
        self.assertEqual([], self.fake.sent)
