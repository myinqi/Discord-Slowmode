import unittest

try:
    from bot.cogs.trya_dcs_chat import neutralize_mass_mentions, parse_web_chat_payload
except ModuleNotFoundError as exc:
    raise unittest.SkipTest(
        "discord.py is installed by requirements.txt and required for DCS chat tests"
    ) from exc


class TryaDcsWebChatTests(unittest.TestCase):
    def test_payload_extracts_reply_and_mention_ids(self):
        content, reply_to, mention_ids = parse_web_chat_payload({
            "content": "hello",
            "reply_to": "123456789012345678",
            "mention_ids": ["111", "111", "abc", "222"],
        })
        self.assertEqual(content, "hello")
        self.assertEqual(reply_to, "123456789012345678")
        self.assertEqual(mention_ids, ["111", "222"])

    def test_payload_rejects_non_numeric_reply(self):
        _, reply_to, _ = parse_web_chat_payload({"content": "hi", "reply_to": "nope"})
        self.assertIsNone(reply_to)

    def test_everyone_and_role_mentions_are_neutralized(self):
        cleaned = neutralize_mass_mentions("hello @everyone and <@&123456789012345678> @here")
        self.assertNotIn("@everyone", cleaned)
        self.assertNotIn("@here", cleaned)
        self.assertIn("@\u200beveryone", cleaned)
        self.assertIn("@\u200bhere", cleaned)
        self.assertIn("<@\u200b&123456789012345678>", cleaned)


if __name__ == "__main__":
    unittest.main()
