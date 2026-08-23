import unittest

from bot.trya_dcs_transcript import parse_dcs_transcript


class TryaDcsTranscriptParseTests(unittest.TestCase):
    def test_json_words_are_normalized(self):
        words = parse_dcs_transcript(
            '[{"word":"Hello","start":0.5,"end":0.9},{"word":"there","start":0.9,"end":1.2}]'
        )
        self.assertEqual(words[0]["word"], "Hello")
        self.assertEqual(words[1]["start"], 0.9)

    def test_timestamped_lines_are_split_into_words(self):
        text = (
            "[0:18] Music to hear, why hear'st thou music sadly?\n"
            "[0:29] Sweets were sweets\n"
            "[1:07] in singleness"
        )
        words = parse_dcs_transcript(text, duration=200)
        self.assertEqual(words[0]["word"], "Music")
        self.assertAlmostEqual(words[0]["start"], 18.0, places=2)
        starts = [item["start"] for item in words if item["word"] == "Sweets"]
        self.assertTrue(starts)
        self.assertAlmostEqual(starts[0], 29.0, places=2)
        self.assertTrue(any(item["word"] == "singleness" for item in words))

    def test_invalid_json_still_explains_json_error(self):
        with self.assertRaisesRegex(ValueError, "not valid JSON"):
            parse_dcs_transcript('[{"word":"Hello" "start":1,"end":2}]')


if __name__ == "__main__":
    unittest.main()
