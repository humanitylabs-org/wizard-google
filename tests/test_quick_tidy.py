"""Synthetic regression coverage for Quick connect's account tidy operation."""
import copy
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from quick import Quick


class TidyPreservationTests(unittest.TestCase):
    def setUp(self):
        self.state = {'quick': {'accounts': {}}}
        self.saved = []
        self.client = Mock()
        self.keys = Mock()
        self.keys.client.return_value = self.client
        self.broker = SimpleNamespace(state=self.state, lock=threading.RLock())
        self.broker._copy = lambda: copy.deepcopy(self.broker.state)
        def commit(state):
            self.saved.append(copy.deepcopy(state))
            self.broker.state = state
        self.broker._commit = commit
        self.quick = Quick.__new__(Quick)
        self.quick.broker = self.broker
        self.quick.keys = lambda: self.keys

    def account(self, name, service, *, key='key-one', user: str | None = 'user-one', added=1, link=None):
        self.state['quick']['accounts'][name] = {
            'id': name, 'email': 'owner@example.com', 'key_id': key, 'user_id': user,
            'links': {service: link or 'connection-' + name}, 'services': [service], 'added': added,
        }

    def test_same_email_with_different_key_keeps_both_accounts_and_connections(self):
        self.account('old', 'calendar')
        self.account('new', 'gmail', key='key-two', added=2)
        self.quick.tidy()
        self.assertEqual(set(self.broker.state['quick']['accounts']), {'old', 'new'})
        self.client.remove.assert_not_called()
        self.assertEqual(self.saved, [])

    def test_same_email_with_different_user_keeps_both_accounts_and_connections(self):
        self.account('old', 'calendar')
        self.account('new', 'gmail', user='user-two', added=2)
        self.quick.tidy()
        self.assertEqual(set(self.broker.state['quick']['accounts']), {'old', 'new'})
        self.client.remove.assert_not_called()

    def test_missing_identity_does_not_authorize_coalescing(self):
        self.account('old', 'calendar', user=None)
        self.account('new', 'gmail', user=None, added=2)
        self.quick.tidy()
        self.assertEqual(set(self.broker.state['quick']['accounts']), {'old', 'new'})
        self.client.remove.assert_not_called()

    def test_disjoint_services_are_persisted_even_without_a_remote_delete(self):
        self.account('old', 'calendar')
        self.account('new', 'gmail', added=2)
        self.quick.tidy()
        self.assertEqual(len(self.saved), 1)
        kept = self.saved[0]['quick']['accounts']
        self.assertEqual(set(kept), {'new'})
        self.assertEqual(kept['new']['links'], {'gmail': 'connection-new', 'calendar': 'connection-old'})
        self.assertEqual(kept['new']['services'], ['calendar', 'gmail'])
        self.client.remove.assert_not_called()

    def test_duplicate_reference_never_deletes_the_connection_being_kept(self):
        self.account('old', 'gmail', link='shared-connection')
        self.account('new', 'gmail', link='shared-connection', added=2)
        self.quick.tidy()
        self.assertEqual(set(self.broker.state['quick']['accounts']), {'new'})
        self.assertEqual(len(self.saved), 1)
        self.client.remove.assert_not_called()

    def test_failed_persistence_preserves_live_state_and_remote_connections(self):
        self.account('old', 'gmail')
        self.account('new', 'gmail', added=2)
        before = copy.deepcopy(self.broker.state)
        self.broker._commit = Mock(side_effect=OSError('synthetic disk failure'))
        self.quick.tidy()
        self.assertEqual(self.broker.state, before)
        self.client.remove.assert_not_called()

    def test_inflight_caller_marks_current_account_after_tidy_replaces_state(self):
        import google
        self.account('old', 'gmail')
        self.account('new', 'gmail', added=2)
        self.state['quick']['accounts']['new']['status'] = 'active'
        caller = self.quick._caller(self.state['quick']['accounts']['new'], 'gmail')
        self.quick.tidy()
        self.client.proxy.return_value = (401, {'error': {'message': 'Invalid Credentials'}})
        with self.assertRaises(google.GoogleError):
            caller('GET', 'https://gmail.googleapis.com/gmail/v1/users/me/messages')
        self.assertEqual(self.broker.state['quick']['accounts']['new']['status'], 'reauth_required')
        self.assertEqual(self.saved[-1]['quick']['accounts']['new']['status'], 'reauth_required')

    def test_owner_edits_resolve_account_after_acquiring_lock(self):
        for operation in ('label', 'service'):
            with self.subTest(operation=operation):
                self.account('new', 'gmail')
                self.state['quick']['accounts']['new'].update(label='', status='active')
                broker = self.broker
                class ReplacingLock:
                    def __enter__(self):
                        broker.state = copy.deepcopy(broker.state)
                    def __exit__(self, *args):
                        return False
                broker.lock = ReplacingLock()
                if operation == 'label':
                    self.quick.set_label('new', 'Work')
                    self.assertEqual(broker.state['quick']['accounts']['new']['label'], 'Work')
                    self.assertEqual(self.saved[-1]['quick']['accounts']['new']['label'], 'Work')
                else:
                    self.quick.set_service('new', 'gmail', False)
                    self.assertEqual(broker.state['quick']['accounts']['new']['services'], [])
                    self.assertEqual(self.saved[-1]['quick']['accounts']['new']['services'], [])

    def test_old_connection_failure_does_not_mark_reconnected_account(self):
        import google
        self.account('new', 'gmail')
        self.state['quick']['accounts']['new']['status'] = 'active'
        caller = self.quick._caller(self.state['quick']['accounts']['new'], 'gmail')
        replacement = copy.deepcopy(self.broker.state)
        replacement['quick']['accounts']['new']['links']['gmail'] = 'reconnected'
        self.broker.state = replacement
        self.client.proxy.return_value = (401, {'error': {'message': 'Invalid Credentials'}})
        with self.assertRaises(google.GoogleError):
            caller('GET', 'https://gmail.googleapis.com/gmail/v1/users/me/messages')
        self.assertEqual(self.broker.state['quick']['accounts']['new']['status'], 'active')

    def test_inflight_key_counter_stays_attached_after_tidy_replaces_state(self):
        import composio
        self.account('old', 'gmail')
        self.account('new', 'gmail', added=2)
        self.state['quick']['keys'] = [{'id': 'key-one', 'calls': {}}]
        def live_keys():
            keys = composio.Keys(self.quick.section(), lambda: self.broker._commit(self.broker.state))
            keys.client = lambda key: self.client
            return keys
        self.quick.keys = live_keys
        caller = self.quick._caller(self.state['quick']['accounts']['new'], 'gmail')
        self.quick.tidy()
        self.client.proxy.return_value = (200, {})
        caller('GET', 'https://gmail.googleapis.com/gmail/v1/users/me/messages')
        self.assertEqual(sum(self.broker.state['quick']['keys'][0]['calls'].values()), 1)
        self.assertEqual(sum(self.saved[-1]['quick']['keys'][0]['calls'].values()), 1)

    def test_temporary_profile_caller_without_account_id_preserves_provider_error(self):
        import google
        import mailcal
        temporary = {'key_id': 'key-one', 'user_id': 'user-one',
                     'links': {'gmail': 'temporary-connection'}}
        self.client.proxy.return_value = (401, {'error': {'message': 'Invalid Credentials'}})
        caller = self.quick._caller(temporary, 'gmail')
        with self.assertRaises(google.GoogleError) as caught:
            mailcal.profile_email(caller, 'gmail')
        self.assertEqual(caught.exception.code, 'reauth_required')
        self.assertEqual(self.saved, [])
        self.assertEqual(self.broker.state, self.state)

    def test_another_retained_account_prevents_remote_connection_delete(self):
        self.account('old', 'gmail', link='shared-connection')
        self.account('new', 'gmail', added=2)
        self.account('other', 'gmail', link='shared-connection', user='other-user')
        self.quick.tidy()
        self.assertEqual(set(self.broker.state['quick']['accounts']), {'new', 'other'})
        self.client.remove.assert_not_called()

    def test_replaced_duplicate_is_deleted_only_after_commit_without_revoke(self):
        self.account('old', 'gmail')
        self.account('new', 'gmail', added=2)
        def remove(connection, *, revoke):
            self.assertEqual(len(self.saved), 1)
            self.assertEqual(connection, 'connection-old')
            self.assertIs(revoke, False)
        self.client.remove.side_effect = remove
        self.assertEqual(self.quick.tidy(), 1)
        self.client.remove.assert_called_once_with('connection-old', revoke=False)
        self.assertEqual(self.quick.tidy(), 0)


if __name__ == '__main__':
    unittest.main()
