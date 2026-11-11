"""Contract 1.5: events carry series_id, and calendar_update_event scope series moves every repeat (wizard-calendar#33)."""
import copy
import unittest

import mailcal

BASE = f'{mailcal.CALENDAR}/calendars/primary/events'


class FakeCalendar:
    """Just enough of Google Calendar: a weekly series (master + one listed instance) and a single event."""

    def __init__(self):
        self.items = {
            'wk': {'id': 'wk', 'summary': 'Standup', 'recurrence': ['RRULE:FREQ=WEEKLY;BYDAY=MO'],
                   'start': {'dateTime': '2026-11-02T10:00:00+01:00', 'timeZone': 'Europe/Berlin'},
                   'end': {'dateTime': '2026-11-02T10:30:00+01:00', 'timeZone': 'Europe/Berlin'}},
            'wk_20261109T090000Z': {'id': 'wk_20261109T090000Z', 'summary': 'Standup', 'recurringEventId': 'wk',
                                    'start': {'dateTime': '2026-11-09T10:00:00+01:00', 'timeZone': 'Europe/Berlin'},
                                    'end': {'dateTime': '2026-11-09T10:30:00+01:00', 'timeZone': 'Europe/Berlin'}},
            'one': {'id': 'one', 'summary': 'Dentist', 'start': {'dateTime': '2026-11-04T10:00:00+07:00'},
                    'end': {'dateTime': '2026-11-04T11:00:00+07:00'}},
        }
        self.calls = []

    def __call__(self, method, url, params=None, body=None):
        self.calls.append((method, url, params, copy.deepcopy(body)))
        eid = url.rsplit('/', 1)[-1]
        if eid not in self.items:
            raise AssertionError(f'unknown event {eid}')
        if method == 'GET':
            return copy.deepcopy(self.items[eid])
        if method == 'PATCH':
            self.items[eid].update(copy.deepcopy(body))
            return copy.deepcopy(self.items[eid])
        raise AssertionError(method)


class SeriesId(unittest.TestCase):
    def test_events_carry_their_series(self):
        fake = FakeCalendar()
        self.assertEqual(mailcal._event(fake.items['wk_20261109T090000Z'], 'primary')['series_id'], 'wk')
        self.assertEqual(mailcal._event(fake.items['wk'], 'primary')['series_id'], 'wk')
        self.assertIsNone(mailcal._event(fake.items['one'], 'primary')['series_id'])


class SeriesShift(unittest.TestCase):
    def setUp(self):
        self.fake = FakeCalendar()

    def update(self, by='owner', **args):
        return mailcal.calendar_update_event(self.fake, args, by=by)

    def test_moving_one_repeat_moves_the_series(self):
        out = self.update(id='wk_20261109T090000Z', scope='series', shift_start_minutes=90, shift_end_minutes=90)
        self.assertEqual((out['scope'], out['series_id'], out['updated']), ('series', 'wk', True))
        method, url, params, body = self.fake.calls[-1]
        self.assertEqual((method, url, params), ('PATCH', f'{BASE}/wk', {'sendUpdates': 'all'}))
        # Wall time in the series' own zone; the zone stays, so every Monday moves 10:00 -> 11:30 Berlin time.
        self.assertEqual(body['start'], {'dateTime': '2026-11-02T11:30:00+01:00', 'timeZone': 'Europe/Berlin'})
        self.assertEqual(body['end'], {'dateTime': '2026-11-02T12:00:00+01:00', 'timeZone': 'Europe/Berlin'})
        self.assertEqual(body['extendedProperties'], {'private': {'wizardBy': 'owner'}})
        self.assertNotIn('recurrence', body)  # the repeat rule is left alone
        self.assertEqual(out['event']['start']['date_time'], '2026-11-02T11:30:00+01:00')
        # Undo: the opposite shift on the series id puts it back.
        self.update(id='wk', scope='series', shift_start_minutes=-90, shift_end_minutes=-90)
        self.assertEqual(self.fake.items['wk']['start']['dateTime'], '2026-11-02T10:00:00+01:00')
        self.assertEqual(self.fake.items['wk']['end']['dateTime'], '2026-11-02T10:30:00+01:00')

    def test_resizing_moves_only_the_end_and_agents_are_marked(self):
        self.update(by='agent', id='wk', scope='series', shift_end_minutes=30)
        body = self.fake.calls[-1][3]
        self.assertEqual(body['start']['dateTime'], '2026-11-02T10:00:00+01:00')
        self.assertEqual(body['end']['dateTime'], '2026-11-02T11:00:00+01:00')
        self.assertEqual(body['extendedProperties'], {'private': {'wizardBy': 'agent'}})

    def test_refusals_change_nothing(self):
        before = copy.deepcopy(self.fake.items)
        cases = [
            dict(id='one', scope='series', shift_start_minutes=30, shift_end_minutes=30),        # not repeating
            dict(id='wk', scope='series', shift_start_minutes=-660, shift_end_minutes=-660),     # to Sunday
            dict(id='wk', scope='series', shift_start_minutes=840, shift_end_minutes=840),       # past midnight
            dict(id='wk', scope='series', shift_start_minutes=60),                               # ends before start
            dict(id='wk', scope='series'),                                                       # nothing to change
            dict(id='wk', scope='series', shift_start_minutes=2000),                             # too far
            dict(id='wk', scope='series', shift_start_minutes='30'),                             # not a number
            dict(id='wk', scope='series', start='2026-11-02T11:00:00+01:00', shift_end_minutes=5),
            dict(id='wk', scope='everything', title='x'),
            dict(id='wk', title='x', shift_start_minutes=30),                                    # shift without scope
        ]
        for args in cases:
            with self.subTest(args=args), self.assertRaises(mailcal.BadArgs):
                self.update(**args)
        self.assertEqual(self.fake.items, before)
        self.assertFalse([c for c in self.fake.calls if c[0] == 'PATCH'])

    def test_occurrence_scope_is_unchanged(self):
        self.update(id='wk_20261109T090000Z', title='Standup (just this one)')
        method, url, _, body = self.fake.calls[-1]
        self.assertEqual((method, url, body['summary']), ('PATCH', f'{BASE}/wk_20261109T090000Z', 'Standup (just this one)'))

    def test_schema_lists_the_new_arguments(self):
        tool = next(t for t in mailcal.mcp_tools() if t['name'] == 'calendar_update_event')
        self.assertEqual(tool['inputSchema']['properties']['scope']['enum'], ['occurrence', 'series'])
        self.assertIn('shift_start_minutes', tool['inputSchema']['properties'])


if __name__ == '__main__':
    unittest.main()
