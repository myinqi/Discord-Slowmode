import unittest

from bot.trya_dcs_web_chat import (
    forget_web_chat_fingerprint,
    neutralize_mass_mentions,
    parse_web_chat_payload,
    register_web_chat_fingerprint,
)


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

    def test_identical_web_posts_are_deduped_within_the_window(self):
        store = {}
        self.assertTrue(register_web_chat_fingerprint(1, "hello", now=10.0, store=store))
        self.assertFalse(register_web_chat_fingerprint(1, "hello", now=11.0, store=store))
        self.assertTrue(register_web_chat_fingerprint(1, "hello", now=14.0, store=store))
        self.assertTrue(register_web_chat_fingerprint(1, "hello", reply_to="99", now=11.0, store=store))
        self.assertTrue(register_web_chat_fingerprint(2, "hello", now=11.0, store=store))

    def test_failed_web_post_fingerprint_can_be_retried(self):
        store = {}
        self.assertTrue(register_web_chat_fingerprint(7, "retry me", now=1.0, store=store))
        forget_web_chat_fingerprint(7, "retry me", store=store)
        self.assertTrue(register_web_chat_fingerprint(7, "retry me", now=1.5, store=store))


if __name__ == "__main__":
    unittest.main()
