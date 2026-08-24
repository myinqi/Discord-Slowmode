import unittest

from bot.trya_dcs_vod import (
    _EMOJI_PLACEHOLDER,
    chat_text_with_emoji_slots,
    classify_vod_stop,
    ffmpeg_live_output_args,
    hls_url_from_rtmp,
    safe_stop_target_out_time,
)


class ChatEmojiSlotTests(unittest.TestCase):
    def test_custom_discord_emojis_become_overlay_slots(self):
        text, slots = chat_text_with_emoji_slots("hello <a:Dancing25:123456789012345678><:Dancing166:123456789012345679>")
        self.assertEqual(slots, [("Dancing25", "123456789012345678"), ("Dancing166", "123456789012345679")])
        self.assertEqual(text.count(_EMOJI_PLACEHOLDER), 2)
        self.assertNotIn(":Dancing25:", text)

    def test_plain_text_has_no_slots(self):
        text, slots = chat_text_with_emoji_slots("hello there")
        self.assertEqual(slots, [])
        self.assertEqual(text, "hello there")


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


class TeeOutputTests(unittest.TestCase):
    def test_rtmp_only_without_vod_path(self):
        self.assertEqual(
            ffmpeg_live_output_args("rtmp://mediamtx:1935/trya-dcs"),
            ["-f", "flv", "rtmp://mediamtx:1935/trya-dcs"],
        )

    def test_live_output_stays_rtmp_even_with_vod_path(self):
        args = ffmpeg_live_output_args(
            "rtmp://mediamtx:1935/trya-dcs",
            "/data/vods/id/master.partial.mp4",
        )
        self.assertEqual(args, ["-f", "flv", "rtmp://mediamtx:1935/trya-dcs"])

    def test_hls_url_follows_mediamtx_path(self):
        self.assertEqual(
            hls_url_from_rtmp("rtmp://mediamtx:1935/trya-dcs"),
            "http://mediamtx:8888/trya-dcs/index.m3u8",
        )


class SafeStopTimingTests(unittest.TestCase):
    def test_waits_for_probed_audio_beyond_stored_duration(self):
        target = safe_stop_target_out_time(100.0, 30.0, 180.0, 195.0, extra=2.0)
        self.assertEqual(target, 147.0)

    def test_without_ffmpeg_clock_there_is_no_target(self):
        self.assertIsNone(safe_stop_target_out_time(None, 10.0, 180.0, 180.0))


if __name__ == "__main__":
    unittest.main()

