from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from gitlab.weekly_stats import week_bounds_local


class WeekBoundsLocalTest(unittest.TestCase):
    def test_week_bounds_use_rolling_last_seven_days(self) -> None:
        tz = ZoneInfo("Asia/Shanghai")
        now_local = datetime(2026, 5, 31, 15, 45, 30, 123456, tzinfo=tz)

        start_utc, end_utc = week_bounds_local("Asia/Shanghai", now=now_local)

        self.assertEqual(end_utc, now_local.astimezone(timezone.utc))
        self.assertEqual(
            start_utc,
            (now_local - timedelta(days=7)).astimezone(timezone.utc),
        )

    def test_week_bounds_treat_naive_now_as_local_timezone(self) -> None:
        now_local = datetime(2026, 5, 31, 15, 45, 30)

        start_utc, end_utc = week_bounds_local("Asia/Shanghai", now=now_local)

        expected_local = now_local.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertEqual(end_utc, expected_local.astimezone(timezone.utc))
        self.assertEqual(
            start_utc,
            (expected_local - timedelta(days=7)).astimezone(timezone.utc),
        )


if __name__ == "__main__":
    unittest.main()
