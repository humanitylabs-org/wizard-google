"""0.2: Wizard's Google app (shared client via the sign-in service), service switches, and gog for agents.
Only Google, the sign-in service and gog are fake; the broker, store and argument checks are real."""
import json
import os
import shutil
import tempfile
import unittest
import urllib.parse

from fakes import CLIENT_JSON, SERVICE, SHARED_ID, SHARED_SECRET, FakeGog, FakeGoogle, FakeSigninService

import google
import gogrun
from broker import Broker, Problem

ORIGIN, SHELL = 'https://box.tail1234.ts.net', '/wizard-google/'


def finish_via_return_page(broker, fake, started, session, sub, email, grant=None):
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(started['authorization_url']).query))
    fake.last_nonce = query['nonce']
    code = f'code-{sub}-{len(fake.codes)}'
    fake.issue(code, sub, email, sorted(grant if grant is not None else query['scope'].split()))
    return broker.finish(query['state'], session, code), query


class WizardSignIn(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.google = FakeGoogle()
        self.service = FakeSigninService(self.google)
        self.gog = FakeGog()
        self.broker = Broker(self.dir, transport=self.service, signin_service=SERVICE, gog=self.gog)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def connect(self, sub='111', email='alex@gmail.com', services=None, account=None, session='s1', grant=None):
        started = self.broker.start(session, ORIGIN, SHELL, account, [], services)
        return finish_via_return_page(self.broker, self.google, started, session, sub, email, grant)

    def test_one_step_setup_when_the_service_is_available(self):
        home = self.broker.home(ORIGIN, SHELL)
        self.assertEqual(home['sign_in'], {'mode': None, 'wizard_available': True, 'verified': False})
        self.assertEqual((home['setup']['count'], home['setup']['current'], home['setup']['google_steps']), (1, 'connect', 0))
        self.assertEqual([s['id'] for s in home['services']], ['gmail', 'calendar', 'drive', 'contacts', 'youtube'])

    def test_sign_in_uses_the_shared_client_and_fixed_return_page_and_never_the_secret(self):
        result, query = self.connect()
        self.assertEqual(query['client_id'], SHARED_ID)
        self.assertEqual(query['redirect_uri'], SERVICE + '/callback')
        state = json.loads(__import__('base64').urlsafe_b64decode(query['state'][4:] + '=='))
        self.assertEqual(state['r'], ORIGIN + SHELL + 'oauth/callback')
        self.assertEqual(query['code_challenge_method'], 'S256')
        for scope in google.SERVICES['gmail']['scopes'] + google.SERVICES['drive']['scopes']:
            self.assertIn(scope, query['scope'].split())
        self.assertNotIn(google.SCOPE + 'youtube.readonly', query['scope'])  # YouTube is off by default
        self.assertEqual(result['email'], 'alex@gmail.com')
        sent = self.service.forms[-1]
        self.assertEqual(sent['grant_type'], 'authorization_code')
        self.assertNotIn('client_secret', sent)
        on = {s['id'] for s in self.broker.account_view(result['account'])['services'] if s['on']}
        self.assertEqual(on, {'gmail', 'calendar', 'drive', 'contacts'})
        # Nothing the box stores or shows holds the shared secret.
        self.assertNotIn(SHARED_SECRET, json.dumps(self.broker.state))
        self.assertNotIn(SHARED_SECRET, json.dumps(self.broker.home(ORIGIN, SHELL)))

    def test_refresh_goes_through_the_service(self):
        result, _ = self.connect()
        self.broker._expire(result['account'])
        self.broker.run_gog('agent:main', ['gmail', 'search', 'is:unread'])
        self.assertEqual(self.service.forms[-1]['grant_type'], 'refresh_token')
        self.assertNotIn('client_secret', self.service.forms[-1])

    def test_work_and_personal_accounts_side_by_side(self):
        personal = self.connect()[0]['account']
        work = self.connect(sub='222', email='alex@studio.example', services=['gmail', 'calendar'])[0]['account']
        self.broker.set_label(personal, 'Personal')
        self.broker.set_label(work, 'Work')
        with self.assertRaises(Problem) as caught:
            self.broker.run_gog('agent:main', ['gmail', 'search', 'is:unread'])
        self.assertEqual(caught.exception.code, 'account_required')
        self.assertEqual(set(caught.exception.extra['accounts']), {'alex@gmail.com', 'alex@studio.example'})
        done = self.broker.run_gog('agent:main', ['gmail', 'search', 'is:unread'], 'work')
        self.assertEqual(done['account'], 'alex@studio.example')
        # Drive is on only for the personal account, so it needs no account name.
        self.assertEqual(self.broker.run_gog('agent:main', ['drive', 'search', 'budget'])['account'], 'alex@gmail.com')
        status = self.broker.agent_status()
        self.assertEqual({a['label'] for a in status['accounts']}, {'Personal', 'Work'})
        self.assertNotIn('refresh_token', json.dumps(status))

    def test_switches_limit_what_gog_may_run(self):
        account = self.connect()[0]['account']
        self.broker.set_service(account, 'gmail', False)
        with self.assertRaises(Problem) as caught:
            self.broker.run_gog('agent:main', ['gmail', 'search', 'x'])
        self.assertEqual(caught.exception.code, 'service_off')
        ran = self.broker.run_gog('agent:main', ['calendar', 'events'])
        self.assertEqual(ran['exit_code'], 0)
        self.assertEqual(self.gog.runs[-1]['roots'], sorted(['calendar', 'contacts', 'people', 'docs', 'drive', 'sheets', 'slides']))
        self.assertTrue(self.gog.runs[-1]['token'].startswith('at-'))
        for bad, code in ((['auth', 'list'], 'command_refused'), (['calendar', 'events', '--access-token=x'], 'flag_refused'),
                          (['drive', 'upload', '/data/store.key'], 'path_refused'), ('calendar events', 'invalid_arguments')):
            with self.assertRaises(Problem) as caught:
                self.broker.run_gog('agent:main', bad, 'alex@gmail.com')
            self.assertEqual(caught.exception.code, code, bad)
        audit = json.dumps(self.broker.state['audit'])
        self.assertIn('gog:calendar events', audit)
        self.assertNotIn('store.key', audit.replace('gog:drive', ''))  # outcome only, never arguments

    def test_switching_on_a_new_service_asks_google_for_just_that(self):
        account = self.connect()[0]['account']
        with self.assertRaises(Problem) as caught:
            self.broker.set_service(account, 'youtube', True)
        self.assertEqual(caught.exception.code, 'consent_needed')
        result, query = self.connect(services=['gmail', 'youtube'], account=account)
        self.assertEqual(query['login_hint'], 'alex@gmail.com')
        self.assertIn(google.SCOPE + 'youtube.readonly', query['scope'])
        on = {s['id'] for s in self.broker.account_view(account)['services'] if s['on']}
        self.assertEqual(on, {'gmail', 'calendar', 'drive', 'contacts', 'youtube'})

    def test_declined_service_stays_off(self):
        everything_but_drive = {'openid', 'email', 'profile', *google.SERVICES['gmail']['scopes'], *google.SERVICES['calendar']['scopes']}
        result, _ = self.connect(grant=everything_but_drive)
        self.assertEqual(set(result['declined_services']), {'drive', 'contacts'})
        on = {s['id'] for s in self.broker.account_view(result['account'])['services'] if s['on']}
        self.assertEqual(on, {'gmail', 'calendar'})

    def test_broad_service_scopes_cover_the_typed_app_operations(self):
        account = self.connect()[0]['account']
        allowed = set(self.broker.account_view(account)['allowed'])
        self.assertTrue({'gmail.search', 'gmail.read', 'gmail.send', 'calendar.read', 'calendar.write', 'drive.search', 'drive.read', 'contacts.read'} <= allowed)
        self.broker.set_grant('app:planner', account, ['gmail.search'])
        self.assertEqual(self.broker.call('app:planner', 'gmail.search', {})['messages'][0]['subject'], 'Lunch')

    def test_mode_cannot_change_under_connected_accounts(self):
        self.connect()
        with self.assertRaises(Problem) as caught:
            self.broker.save_client(CLIENT_JSON, redirect_uri=ORIGIN + SHELL + 'oauth/callback')
        self.assertEqual(caught.exception.code, 'client_in_use')

    def test_unavailable_service_falls_back_to_own_client_setup(self):
        self.service.available = False
        broker = Broker(tempfile.mkdtemp(), transport=self.service, signin_service=SERVICE, gog=self.gog)
        plan = broker.setup_plan(ORIGIN, SHELL)
        self.assertEqual((plan['mode'], plan['count'], plan['wizard_available']), ('own', 7, False))
        with self.assertRaises(Problem) as caught:
            broker.start('s1', ORIGIN, SHELL)
        self.assertEqual(caught.exception.code, 'client_required')

    def test_gog_auth_failure_retries_once_with_a_fresh_token(self):
        self.connect()
        self.gog.exit_codes = [4, 0]
        result = self.broker.run_gog('agent:main', ['gmail', 'labels', 'list'])
        self.assertEqual(result['exit_code'], 0)
        self.assertNotEqual(self.gog.runs[0]['token'], self.gog.runs[1]['token'])


@unittest.skipUnless(os.path.exists(gogrun.GOG) or os.getenv('GOG_TEST_BIN'), 'gog binary not installed here')
class RealGog(unittest.TestCase):
    """The pinned gog binary itself: our flags, its own command gate, and a refused token (no Google account needed)."""
    def test_real_binary(self):
        binary = os.getenv('GOG_TEST_BIN') or gogrun.GOG
        out = gogrun.run(['gmail', 'labels', 'list'], 'ya29.not-a-real-token', {'gmail'}, binary=binary)
        self.assertEqual(out['exit_code'], 4)  # auth_required: gog reached Google with the token we gave it
        self.assertNotIn('ya29.not-a-real-token', json.dumps(out))
        help_ = gogrun.help_for('gmail search', {'gmail'}, binary=binary)
        self.assertIn('schema', help_)


if __name__ == '__main__':
    unittest.main()
