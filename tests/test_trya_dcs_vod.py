import unittest

from bot.trya_dcs_vod import classify_vod_stop, safe_stop_target_out_time


class ClassifyVodStopTests(unittest.TestCase):
    def test_keep_recording_after_manual_stop(self):
        status, error = classify_vod_stop(interrupted=False, master_ok=True)
        self.assertEqual(status, "master_ready")
        self.assertEqual(error, "")

    def test_keep_recording_after_safe_stop_or_crash(self):
        status, error = classify_vod_stop(
            interrupted=True,
            master_ok=True,
            assembly_error="remux warned",
        )
        self.assertEqual(status, "interrupted")
        self.assertEqual(error, "")

    def test_fail_only_without_a_master_file(self):
        status, error = classify_vod_stop(
            interrupted=False,
            master_ok=False,
            recorder_error="Connection refused",
        )
        self.assertEqual(status, "failed")
        self.assertIn("Connection refused", error)


class SafeStopTimingTests(unittest.TestCase):
    def test_waits_for_probed_audio_beyond_stored_duration(self):
        target = safe_stop_target_out_time(100.0, 30.0, 180.0, 195.0, extra=2.0)
        self.assertEqual(target, 147.0)

    def test_without_ffmpeg_clock_there_is_no_target(self):
        self.assertIsNone(safe_stop_target_out_time(None, 10.0, 180.0, 180.0))


if __name__ == "__main__":
    unittest.main()

