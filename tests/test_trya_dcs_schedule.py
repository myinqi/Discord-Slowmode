import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from bot import trya_dcs_schedule as dcs_manager


BERLIN = ZoneInfo("Europe/Berlin")


class FakeSettings:
    def __init__(self, **settings):
        self._settings = settings

    async def get_setting(self, key, default=None):
        return self._settings.get(key, default)


class TryaDcsScheduleLockTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._previous_live = dcs_manager.dcs_stream_is_live
        dcs_manager.dcs_stream_is_live = False

    async def asyncTearDown(self):
        dcs_manager.dcs_stream_is_live = self._previous_live

    async def test_unlocked_when_scheduler_disabled(self):
        db = FakeSettings(
            trya_dcs_schedule_enabled="off",
            trya_dcs_schedule_days="0",
            trya_dcs_schedule_time="20:00",
        )
        now = datetime(2026, 8, 24, 19, 15, tzinfo=BERLIN)
        locked, reason = await dcs_manager.is_submissions_locked(db, now=now)
        self.assertFalse(locked)
        self.assertEqual(reason, "")

    async def test_locked_when_publisher_is_live(self):
        dcs_manager.dcs_stream_is_live = True
        db = FakeSettings(trya_dcs_schedule_enabled="off")
        locked, reason = await dcs_manager.is_submissions_locked(db)
        self.assertTrue(locked)
        self.assertEqual(reason, "stream_live")

    async def test_locked_sixty_minutes_before_scheduled_start(self):
        db = FakeSettings(
            trya_dcs_schedule_enabled="on",
            trya_dcs_schedule_days="0",
            trya_dcs_schedule_time="20:00",
        )
        now = datetime(2026, 8, 24, 19, 0, tzinfo=BERLIN)
        locked, reason = await dcs_manager.is_submissions_locked(db, now=now)
        self.assertTrue(locked)
        self.assertEqual(reason, "pre_start_60min")

    async def test_unlocked_sixty_one_minutes_before_scheduled_start(self):
        db = FakeSettings(
            trya_dcs_schedule_enabled="on",
            trya_dcs_schedule_days="0",
            trya_dcs_schedule_time="20:00",
        )
        now = datetime(2026, 8, 24, 18, 59, tzinfo=BERLIN)
        locked, reason = await dcs_manager.is_submissions_locked(db, now=now)
        self.assertFalse(locked)
        self.assertEqual(reason, "")

    async def test_unlocked_on_unscheduled_weekday(self):
        db = FakeSettings(
            trya_dcs_schedule_enabled="on",
            trya_dcs_schedule_days="0",
            trya_dcs_schedule_time="20:00",
        )
        now = datetime(2026, 8, 25, 19, 15, tzinfo=BERLIN)
        locked, reason = await dcs_manager.is_submissions_locked(db, now=now)
        self.assertFalse(locked)
        self.assertEqual(reason, "")

    async def test_unlocked_at_scheduled_start_if_not_live(self):
        db = FakeSettings(
            trya_dcs_schedule_enabled="on",
            trya_dcs_schedule_days="0",
            trya_dcs_schedule_time="20:00",
        )
        now = datetime(2026, 8, 24, 20, 0, tzinfo=BERLIN)
        locked, reason = await dcs_manager.is_submissions_locked(db, now=now)
        self.assertFalse(locked)
        self.assertEqual(reason, "")


if __name__ == "__main__":
    unittest.main()
