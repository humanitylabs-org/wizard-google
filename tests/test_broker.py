import os
import shutil
import stat
import tempfile
import time
import unittest
import urllib.parse
from pathlib import Path

from fakes import CLIENT_ID, CLIENT_JSON, SECRET, FakeGoogle, sign_in

import google
import setup_flow
from broker import Broker, Problem
from store import StoreError

S = google.SCOPE


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.fake = FakeGoogle()
        self.broker = Broker(self.dir, transport=self.fake, relay_callback='https://relay.example/wizard-google/auth/callback')
        self.broker.save_client(CLIENT_JSON, redirect_uri='http://127.0.0.1:18812/oauth/callback')

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def connect(self, sub='111', email='owner@gmail.com', ops=('gmail.search', 'gmail.read', 'calendar.read'), **kw):
        return sign_in(self.broker, self.fake, kw.pop('session', 's1'), sub, email, ops, **kw)


class ClientSetup(Base):
    def test_web_json_is_saved_and_probed_without_a_sign_in(self):
        summary = self.broker.client_summary()
        self.assertEqual(summary['client_id'], CLIENT_ID)
        self.assertEqual(summary['project_id'], 'wizard-test-123')
        self.assertEqual(summary['checked'], 'ok')  # invalid_grant on a made-up code = ID and secret are real
        self.assertNotIn(SECRET, str(summary))

    def test_wrong_secret_is_caught_at_paste_time(self):
        bad = CLIENT_JSON.replace(SECRET, 'GOCSPX-wrong')
        with self.assertRaises(Problem) as caught:
            Broker(tempfile.mkdtemp(), transport=self.fake).save_client(bad)
        self.assertEqual(caught.exception.code, 'client_invalid')
        self.assertIn('auth/clients/' + CLIENT_ID, caught.exception.extra['fix_url'])

    def test_desktop_client_and_missing_secret_explain_the_fix(self):
        with self.assertRaises(setup_flow.SetupProblem) as caught:
            setup_flow.parse_client('{"installed": {"client_id": "x"}}')
        self.assertEqual(caught.exception.code, 'client_type_desktop')
        with self.assertRaises(setup_flow.SetupProblem) as caught:
            setup_flow.parse_client(client_id=CLIENT_ID, client_secret='')
        self.assertEqual(caught.exception.code, 'client_secret_missing')
        with self.assertRaises(setup_flow.SetupProblem):
            setup_flow.parse_client(client_id='not-a-client', client_secret='x')
        typed = setup_flow.parse_client(client_id=CLIENT_ID, client_secret=SECRET)
        self.assertEqual(typed['project_number'], '123456789012')

    def test_switching_client_with_accounts_is_refused(self):
        self.connect()
        other_id = '999999999999-zzzzzzzzzzzzzzzz.apps.googleusercontent.com'
        self.fake.clients[other_id] = SECRET
        with self.assertRaises(Problem) as caught:
            self.broker.save_client(CLIENT_JSON.replace(CLIENT_ID, other_id))
        self.assertEqual(caught.exception.code, 'client_in_use')
        self.broker.save_client(CLIENT_JSON)  # same client again (e.g. a fresh secret) is fine


class Plan(Base):
    def test_seven_steps_five_in_google_with_deep_links(self):
        fresh = Broker(tempfile.mkdtemp(), transport=self.fake)
        plan = fresh.setup_plan('https://box.tail1234.ts.net', '/wizard-google/')
        self.assertEqual([s['id'] for s in plan['steps']], ['project', 'branding', 'publish', 'client', 'paste', 'apis', 'connect'])
        self.assertEqual((plan['count'], plan['google_steps']), (7, 5))
        self.assertEqual(plan['current'], 'project')
        self.assertEqual(plan['steps'][0]['url'], 'https://console.cloud.google.com/projectcreate')
        client_step = next(s for s in plan['steps'] if s['id'] == 'client')
        self.assertEqual(client_step['copy'][0]['value'], 'https://box.tail1234.ts.net/wizard-google/oauth/callback')
        self.assertEqual(plan['redirect']['authorized_domain'], 'tail1234.ts.net')
        self.assertTrue(plan['redirect']['box_ok'])

    def test_api_link_names_every_api_and_the_exact_project(self):
        apis = next(s for s in self.broker.setup_plan('http://127.0.0.1:18812', '/')['steps'] if s['id'] == 'apis')
        self.assertEqual(apis['url'], 'https://console.cloud.google.com/apis/enableflow?apiid=gmail.googleapis.com,calendar-json.googleapis.com,'
                                      'drive.googleapis.com,people.googleapis.com,youtube.googleapis.com&project=wizard-test-123')

    def test_links_carry_the_project_after_the_client_is_known(self):
        plan = self.broker.setup_plan('http://127.0.0.1:18812', '/')
        self.assertIn('project=wizard-test-123', plan['steps'][2]['url'])
        self.assertEqual(plan['current'], 'publish')

    def test_redirect_missing_from_client_is_flagged_with_fix(self):
        plan = self.broker.setup_plan('https://box.tail1234.ts.net', '/wizard-google/')
        codes = [w['code'] for w in plan['warnings']]
        self.assertIn('redirect_not_on_client', codes)

    def test_relay_mode_uses_one_fixed_return_address(self):
        self.broker.set_setup(redirect_mode='relay')
        plan = self.broker.setup_plan('https://box.tail1234.ts.net', '/wizard-google/')
        self.assertEqual(plan['redirect']['uri'], 'https://relay.example/wizard-google/auth/callback')
        started = self.broker.start('s1', 'https://box.tail1234.ts.net', '/wizard-google/')
        state = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(started['authorization_url']).query))['state']
        self.assertTrue(state.startswith('wg1.'))

    def test_testing_mode_is_detected_from_the_7_day_refresh_token(self):
        self.fake.testing = True
        self.connect()
        plan = self.broker.setup_plan('http://127.0.0.1:18812', '/')
        self.assertIn('testing_expiry', [w['code'] for w in plan['warnings']])
        self.assertEqual(plan['current'], 'publish')

    def test_published_app_completes_setup_after_connect_and_api_check(self):
        account = self.connect()[0]['account']
        self.assertEqual(self.broker.setup_plan('http://127.0.0.1:18812', '/')['current'], 'apis')
        self.broker.check_apis(account)
        plan = self.broker.setup_plan('http://127.0.0.1:18812', '/')
        self.assertTrue(plan['complete'], plan['current'])
        self.assertEqual(plan['warnings'][:1], [w for w in plan['warnings'] if w['code'] == 'redirect_not_on_client'][:1])

    def test_plain_http_box_address_is_not_offered_to_google(self):
        with self.assertRaises(Problem) as caught:
            self.broker.start('s1', 'http://wizard.lan:8080', '/wizard-google/')
        self.assertEqual(caught.exception.code, 'box_address_unusable')


class SignIn(Base):
    def test_pkce_offline_consent_and_account_record(self):
        result, query = self.connect()
        self.assertEqual(query['code_challenge_method'], 'S256')
        self.assertEqual(query['access_type'], 'offline')
        self.assertEqual(query['include_granted_scopes'], 'true')
        self.assertIn(S + 'gmail.readonly', query['scope'])
        self.assertEqual(result['email'], 'owner@gmail.com')
        account = self.broker.account_view(result['account'])
        self.assertEqual(account['status'], 'active')
        self.assertEqual(set(account['allowed']), {'gmail.search', 'gmail.read', 'calendar.read'})

    def test_partial_consent_keeps_the_account_and_reports_what_was_declined(self):
        result, _ = self.connect(grant={'openid', 'email', 'profile', S + 'gmail.readonly'})
        self.assertEqual(result['declined'], ['calendar.read'])
        self.assertNotIn('calendar.read', self.broker.account_view(result['account'])['allowed'])

    def test_state_is_one_use_and_bound_to_the_browser_session(self):
        started = self.broker.start('s1', 'http://127.0.0.1:18812', '/')
        state = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(started['authorization_url']).query))['state']
        with self.assertRaises(Problem):
            self.broker.finish(state, 'other-session', 'code')
        with self.assertRaises(Problem):
            self.broker.finish(state, 's1', 'code-unknown')  # first use fails at Google
        with self.assertRaises(Problem) as caught:
            self.broker.finish(state, 's1', 'code-unknown')  # and cannot be replayed
        self.assertEqual(caught.exception.code, 'invalid_state')

    def test_expired_flow_is_refused(self):
        clock = [time.time()]
        broker = Broker(self.dir, transport=self.fake, clock=lambda: clock[0])
        started = broker.start('s1', 'http://127.0.0.1:18812', '/')
        state = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(started['authorization_url']).query))['state']
        clock[0] += 601
        with self.assertRaises(Problem):
            broker.finish(state, 's1', 'x')

    def test_reconnect_must_be_the_same_google_account(self):
        result, _ = self.connect()
        with self.assertRaises(Problem) as caught:
            self.connect(sub='222', email='other@gmail.com', account=result['account'])
        self.assertEqual(caught.exception.code, 'wrong_account')

    def test_several_accounts(self):
        a, _ = self.connect()
        b, _ = self.connect(sub='222', email='work@example.com', ops=('drive.search',))
        self.assertNotEqual(a['account'], b['account'])
        self.assertEqual(len(self.broker.home('http://127.0.0.1:18812', '/')['accounts']), 2)
        self.assertEqual(self.broker.connection()['status'], 'connected')

    def test_consent_declined(self):
        started = self.broker.start('s1', 'http://127.0.0.1:18812', '/')
        state = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(started['authorization_url']).query))['state']
        with self.assertRaises(Problem) as caught:
            self.broker.finish(state, 's1', error='access_denied')
        self.assertEqual(caught.exception.code, 'consent_declined')
        self.assertEqual(self.broker.flow_status('s1')['status'], 'failed')


class Grants(Base):
    def setUp(self):
        super().setUp()
        self.personal = self.connect()[0]['account']
        self.work = self.connect(sub='222', email='work@example.com', ops=('gmail.search', 'gmail.send'))[0]['account']

    def test_nothing_is_allowed_by_default(self):
        with self.assertRaises(Problem) as caught:
            self.broker.call('agent:main', 'gmail.search', {})
        self.assertEqual(caught.exception.code, 'grant_required')

    def test_grant_is_per_caller_per_account_per_operation(self):
        self.broker.set_grant('agent:main', self.personal, ['gmail.search'])
        found = self.broker.call('agent:main', 'gmail.search', {'query': 'lunch'})
        self.assertEqual(found['account'], 'owner@gmail.com')
        self.assertEqual(found['messages'][0]['subject'], 'Lunch')
        for caller, op, account in (('agent:main', 'gmail.read', None), ('agent:main', 'gmail.search', 'work@example.com'),
                                    ('app:music', 'gmail.search', None), ('agent:research', 'gmail.search', None)):
            with self.assertRaises(Problem, msg=(caller, op, account)) as caught:
                self.broker.call(caller, op, {'id': 'm1'}, account)
            self.assertEqual(caught.exception.code, 'grant_required')

    def test_two_allowed_accounts_must_be_named(self):
        self.broker.set_grant('agent:main', self.personal, ['gmail.search'])
        self.broker.set_grant('agent:main', self.work, ['gmail.search'])
        with self.assertRaises(Problem) as caught:
            self.broker.call('agent:main', 'gmail.search', {})
        self.assertEqual(caught.exception.code, 'account_required')
        self.assertEqual(self.broker.call('agent:main', 'gmail.search', {}, 'work@example.com')['account'], 'work@example.com')

    def test_grant_without_google_permission_says_so(self):
        self.broker.set_grant('app:planner', self.work, ['calendar.read'])
        with self.assertRaises(Problem) as caught:
            self.broker.call('app:planner', 'calendar.read', {})
        self.assertEqual(caught.exception.code, 'google_permission_missing')

    def test_send_builds_a_clean_message(self):
        self.broker.set_grant('agent:main', self.work, ['gmail.send'])
        proposed = self.broker.call('agent:main', 'gmail.send', {'to': ['anna@example.com'], 'subject': 'Hi', 'body': 'See you.'})['request']
        self.assertEqual([], self.fake.sent)
        self.broker.decide(proposed['id'], True, payload_hash=proposed['payload_hash'])
        sub, raw = self.fake.sent[0]
        self.assertEqual(sub, '222')
        self.assertIn('To: anna@example.com', raw)
        with self.assertRaises(Problem):
            self.broker.call('agent:main', 'gmail.send', {'to': ['anna@example.com\r\nBcc: x@y.z'], 'subject': 'Hi', 'body': 'x'})
        with self.assertRaises(Problem):
            self.broker.call('agent:main', 'gmail.send', {'to': ['anna@example.com'], 'subject': 'Hi\nBcc: x@y.z', 'body': 'x'})

    def test_access_request_then_owner_approval(self):
        asked = self.broker.request_access('app:music', ['youtube.read'], reason='Match songs to videos')['request']
        again = self.broker.request_access('app:music', ['youtube.read'], reason='Match songs to videos')['request']
        self.assertEqual(asked['id'], again['id'])
        self.assertEqual(self.broker.home('http://127.0.0.1:18812', '/')['requests'][0]['label'], 'Music')
        with self.assertRaises(Problem):
            self.broker.decide(asked['id'], True)  # no account named by the app: the owner picks one
        self.broker.decide(asked['id'], True, self.personal)
        self.assertEqual(self.broker.grants_for('app:music')[0]['operations'], ['youtube.read'])
        self.assertEqual(self.broker.state['requests'][asked['id']]['status'], 'approved')

    def test_revoke_grant(self):
        grant = self.broker.set_grant('agent:main', self.personal, ['gmail.search'])['grant']
        self.broker.revoke_grant(grant['id'])
        with self.assertRaises(Problem):
            self.broker.call('agent:main', 'gmail.search', {})

    def test_app_keys_are_stored_hashed_and_revocable(self):
        made = self.broker.create_key('app:music')
        self.assertEqual(self.broker.client_for_key(made['key']), 'app:music')
        self.assertNotIn(made['key'], str(self.broker.state))
        self.broker.revoke_key(made['id'])
        self.assertIsNone(self.broker.client_for_key(made['key']))
        with self.assertRaises(Problem):
            self.broker.create_key('agent:main')

    def test_audit_has_no_content(self):
        self.broker.set_grant('agent:main', self.personal, ['gmail.read'])
        self.broker.call('agent:main', 'gmail.read', {'id': 'm1'})
        entry = self.broker.state['audit'][-1]
        self.assertEqual(entry['outcome'], 'ok')
        self.assertNotIn('Thursday', str(self.broker.state['audit']))


class Tokens(Base):
    def setUp(self):
        super().setUp()
        self.account = self.connect()[0]['account']
        self.broker.set_grant('agent:main', self.account, ['gmail.search', 'calendar.read'])

    def expire(self):
        self.broker.state['accounts'][self.account]['expires'] = 0

    def test_refresh_and_rotation_are_persisted(self):
        self.fake.rotate = True
        before = self.broker.state['accounts'][self.account]['refresh_token']
        self.expire()
        self.broker.call('agent:main', 'gmail.search', {})
        after = Broker(self.dir, transport=self.fake).state['accounts'][self.account]['refresh_token']
        self.assertEqual(after, before + '-r')

    def test_dead_refresh_token_asks_for_reconnect_and_hints_at_testing_mode(self):
        self.fake.dead_refresh.add(self.broker.state['accounts'][self.account]['refresh_token'])
        self.expire()
        with self.assertRaises(Problem) as caught:
            self.broker.call('agent:main', 'gmail.search', {})
        self.assertEqual(caught.exception.code, 'reauth_required')
        self.assertIn('7 days', caught.exception.message)
        self.assertIn('auth/audience', caught.exception.extra['fix_url'])
        self.assertEqual(self.broker.account_view(self.account)['status'], 'reauth_required')
        self.assertEqual(self.broker.connection()['status'], 'expired')

    def test_api_not_enabled_links_straight_to_the_enable_page(self):
        self.fake.disabled.add('calendar')
        with self.assertRaises(Problem) as caught:
            self.broker.call('agent:main', 'calendar.read', {})
        self.assertEqual(caught.exception.code, 'api_not_enabled')
        self.assertEqual(caught.exception.extra['fix_url'],
                         'https://console.cloud.google.com/apis/enableflow?apiid=calendar-json.googleapis.com&project=wizard-test-123')
        checked = self.broker.check_apis(self.account)['apis']
        self.assertEqual(checked['calendar-json.googleapis.com']['status'], 'not_enabled')
        self.assertEqual(checked['gmail.googleapis.com']['status'], 'ok')
        self.assertEqual(checked['drive.googleapis.com']['status'], 'not_allowed')

    def test_disconnect_revokes_removes_grants_and_cancels_pending_sign_ins(self):
        started = self.broker.start('s2', 'http://127.0.0.1:18812', '/')
        state = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(started['authorization_url']).query))['state']
        out = self.broker.disconnect(self.account)
        self.assertTrue(out['revoked_at_google'])
        self.assertEqual(self.broker.grants_for('agent:main'), [])
        with self.assertRaises(Problem):
            self.broker.finish(state, 's2', 'late-code')


class Concurrency(Base):
    def test_disconnect_during_a_slow_call_does_not_deadlock(self):
        import threading
        account = self.connect()[0]['account']
        self.broker.set_grant('agent:main', account, ['gmail.search'])
        self.broker.state['accounts'][account]['expires'] = 0  # the call must refresh (takes the state lock inside)
        gate, original = threading.Event(), self.fake.request

        def slow(method, url, **kw):
            if url == google.TOKEN_URL and kw.get('form', {}).get('grant_type') == 'refresh_token':
                gate.wait(2)
            return original(method, url, **kw)
        self.fake.request = slow
        def run_call():
            try:
                self.broker.call('agent:main', 'gmail.search', {})
            except Problem:
                pass  # either order is fine; hanging is not
        caller = threading.Thread(target=run_call)
        caller.start()
        remover = threading.Thread(target=self.broker.disconnect, args=(account,))
        remover.start()
        gate.set()
        caller.join(5)
        remover.join(5)
        self.assertFalse(caller.is_alive() or remover.is_alive(), 'deadlock')
        self.assertNotIn(account, self.broker.state['accounts'])


class Custody(Base):
    def test_files_are_private_and_encrypted(self):
        self.connect()
        mode = lambda p: stat.S_IMODE(os.stat(p).st_mode)
        self.assertEqual(mode(self.dir), 0o700)
        self.assertEqual(mode(os.path.join(self.dir, 'google.enc')), 0o600)
        self.assertEqual(mode(os.path.join(self.dir, 'store.key')), 0o600)
        raw = Path(self.dir, 'google.enc').read_bytes()
        for secret in (SECRET.encode(), b'rt-111', b'at-111', b'owner@gmail.com'):
            self.assertNotIn(secret, raw)

    def test_loosened_permissions_are_restored_on_start(self):
        os.chmod(self.dir, 0o777)
        os.chmod(os.path.join(self.dir, 'store.key'), 0o666)
        Broker(self.dir, transport=self.fake)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.dir, 'store.key')).st_mode), 0o600)

    def test_moving_the_data_directory_keeps_everything_without_reconnect(self):
        account = self.connect()[0]['account']
        self.broker.set_grant('agent:main', account, ['gmail.search'])
        self.broker.state['accounts'][account]['expires'] = 0
        self.broker.store.save(self.broker.state)
        new_box = tempfile.mkdtemp()
        shutil.copytree(self.dir, new_box, dirs_exist_ok=True)  # what an App Manager backup/restore does
        moved = Broker(new_box, transport=self.fake)
        self.assertEqual(moved.client_summary()['client_id'], CLIENT_ID)
        self.assertEqual(moved.call('agent:main', 'gmail.search', {})['account'], 'owner@gmail.com')  # refreshes on the new box

    def test_missing_key_never_overwrites_existing_sign_ins(self):
        self.connect()
        os.unlink(os.path.join(self.dir, 'store.key'))
        with self.assertRaises(StoreError):
            Broker(self.dir, transport=self.fake)


if __name__ == '__main__':
    unittest.main()
