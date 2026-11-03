import unittest

import mailcal


class EventTimes(unittest.TestCase):
    """Google Calendar answers 400 Bad Request to dateTimes without seconds; we always send HH:MM:SS."""

    def test_minutes_only_get_seconds(self):
        self.assertEqual(mailcal._time({'start': '2026-11-04T14:00', 'time_zone': 'Asia/Bangkok'}, 'start', True),
                         {'dateTime': '2026-11-04T14:00:00', 'timeZone': 'Asia/Bangkok'})
        self.assertEqual(mailcal._time({'start': '2026-11-04T14:00+07:00'}, 'start', True), {'dateTime': '2026-11-04T14:00:00+07:00'})
        self.assertEqual(mailcal._time({'start': '2026-11-04T14:00Z'}, 'start', True), {'dateTime': '2026-11-04T14:00:00Z'})

    def test_other_shapes_unchanged(self):
        self.assertEqual(mailcal._time({'start': '2026-11-04T14:00:30.5+07:00'}, 'start', True), {'dateTime': '2026-11-04T14:00:30.5+07:00'})
        self.assertEqual(mailcal._time({'start': '2026-11-04'}, 'start', True), {'date': '2026-11-04'})
        self.assertIsNone(mailcal._time({}, 'end', False))
        with self.assertRaises(mailcal.BadArgs):
            mailcal._time({'start': '2026-11-04T14:00'}, 'start', True)  # local time still needs a zone
        with self.assertRaises(mailcal.BadArgs):
            mailcal._time({'start': 'tomorrow'}, 'start', True)


if __name__ == '__main__':
    unittest.main()
