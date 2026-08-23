import hashlib
import tempfile
import time
import unittest
from pathlib import Path

try:
    from bot.database import Database
except ModuleNotFoundError as exc:
    if exc.name != "aiosqlite":
        raise
    raise unittest.SkipTest(
        "aiosqlite is installed by requirements.txt and required for database tests"
    ) from exc


DECLARATION = "Private non-commercial sharing declaration"


class TryaDcsDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.temp_dir.name) / "test.db"))
        await self.db.connect()

    async def asyncTearDown(self):
        await self.db.close()
        self.temp_dir.cleanup()

    async def _new_slot(self, user_id=100, replacement_song_id=None, playlist_source="submission"):
        return await self.db.add_trya_dcs_song(
            user_id=user_id,
            user_name=f"member-{user_id}",
            replacement_song_id=replacement_song_id,
            rights_version="1",
            rights_declaration=DECLARATION,
            playlist_source=playlist_source,
        )

    async def _finalize(self, song_id, suffix):
        now = time.time()
        return await self.db.finalize_trya_dcs_upload(
            song_id,
            original_sha256=hashlib.sha256(suffix.encode()).hexdigest(),
            original_filename=f"{suffix}.mp3",
            original_mime="audio/mpeg",
            original_size=1024,
            original_archive_filename=f"{suffix}-original.mp3",
            mp3_filename=f"{suffix}-work.mp3",
            duration=180.0,
            uploaded_at=now,
            rights_accepted_at=now,
            rights_version="1",
            rights_declaration=DECLARATION,
            rights_hash=hashlib.sha256(DECLARATION.encode()).hexdigest(),
            content_kind="original",
            suno_plan_status="paid",
            sharing_attested=1,
            official_download_attested=1,
            material_rights_attested=1,
            technical_processing_attested=1,
            private_playback_attested=1,
            evidence_json='{"test":true}',
        )

    async def test_stream_token_must_be_current_and_not_revoked(self):
        now = time.time()
        await self.db.issue_trya_dcs_stream_token(
            token_hash="current",
            discord_user_id=100,
            discord_guild_id=200,
            issued_at=now,
            expires_at=now + 600,
            remote_fingerprint="browser",
        )
        await self.db.issue_trya_dcs_stream_token(
            token_hash="expired",
            discord_user_id=101,
            discord_guild_id=200,
            issued_at=now - 1200,
            expires_at=now - 600,
        )

        self.assertIsNotNone(await self.db.get_trya_dcs_stream_token("current"))
        self.assertIsNone(await self.db.get_trya_dcs_stream_token("expired"))

        await self.db.revoke_trya_dcs_user_tokens(100)
        self.assertIsNone(await self.db.get_trya_dcs_stream_token("current"))

    async def test_owner_delete_rejects_another_member(self):
        song_id, token = await self._new_slot(user_id=100)

        self.assertIsNone(
            await self.db.delete_trya_dcs_song(song_id, user_id=999)
        )
        self.assertIsNotNone(await self.db.get_trya_dcs_song_by_token(token))

        removed = await self.db.delete_trya_dcs_song(song_id, user_id=100)
        self.assertEqual(removed["user_id"], 100)
        self.assertIsNone(await self.db.get_trya_dcs_song_by_token(token))

    async def test_optional_wlm_url_is_stored(self):
        song_id, _ = await self._new_slot(user_id=100)
        wlm_url = "https://www.welovemusic.ai/track/39a09dfb-bf72-4852-b813-49c3a02d3aaa"

        await self.db.update_trya_dcs_song(song_id, wlm_url=wlm_url)

        self.assertEqual((await self.db.get_trya_dcs_song(song_id))["wlm_url"], wlm_url)

    async def test_failed_upload_can_be_purged_with_consent_event(self):
        song_id, _ = await self._new_slot(user_id=100)
        await self._finalize(song_id, "failed")
        await self.db.update_trya_dcs_song(song_id, analysis_status="failed")

        removed = await self.db.purge_failed_trya_dcs_song(song_id)

        self.assertEqual(removed["id"], song_id)
        self.assertIsNone(await self.db.get_trya_dcs_song(song_id))
        async with self.db.db.execute(
            "SELECT COUNT(*) FROM trya_dcs_consent_events WHERE song_id = ?", (song_id,)
        ) as cursor:
            self.assertEqual((await cursor.fetchone())[0], 0)

    async def test_completed_upload_cannot_be_purged(self):
        song_id, _ = await self._new_slot(user_id=100)
        await self._finalize(song_id, "complete")

        self.assertIsNone(await self.db.purge_failed_trya_dcs_song(song_id))
        self.assertIsNotNone(await self.db.get_trya_dcs_song(song_id))

    async def test_invalid_playlist_source_is_rejected(self):
        with self.assertRaises(ValueError):
            await self._new_slot(user_id=100, playlist_source="admin")

    async def test_playlist_source_can_be_assigned_after_upload(self):
        song_id, _ = await self._new_slot(user_id=100)
        await self._finalize(song_id, "intro")

        await self.db.update_trya_dcs_song(song_id, playlist_source="intro")
        stored = await self.db.get_trya_dcs_song(song_id)

        self.assertEqual(stored["playlist_source"], "intro")

    async def test_intro_slot_is_created_without_counting_as_submission(self):
        submission_id, _ = await self._new_slot(user_id=100)
        await self._finalize(submission_id, "submission")
        intro_id, _ = await self._new_slot(user_id=100, playlist_source="intro")

        stored = await self.db.get_trya_dcs_song(intro_id)
        submissions = await self.db.get_trya_dcs_songs_by_user(
            100, playlist_source="submission"
        )
        intros = await self.db.get_trya_dcs_songs_by_user(
            100, include_pending=True, playlist_source="intro"
        )

        self.assertEqual(stored["playlist_source"], "intro")
        self.assertEqual([song["id"] for song in submissions], [submission_id])
        self.assertEqual([song["id"] for song in intros], [intro_id])

    async def test_pending_intro_does_not_supersede_pending_submission(self):
        submission_id, submission_token = await self._new_slot(user_id=100)
        await self.db.supersede_pending_trya_dcs_uploads(
            100, playlist_source="intro"
        )

        self.assertIsNotNone(await self.db.get_trya_dcs_song_by_token(submission_token))
        self.assertEqual(
            (await self.db.get_trya_dcs_song(submission_id))["playlist_source"],
            "submission",
        )

    async def test_replacement_retires_old_song_only_after_finalize(self):
        old_id, _ = await self._new_slot(user_id=100)
        await self._finalize(old_id, "old")

        replacement_id, _ = await self._new_slot(
            user_id=100, replacement_song_id=old_id
        )
        old_before = await self.db.get_trya_dcs_song(old_id)
        self.assertEqual(old_before["active"], 1)

        with self.assertRaises(ValueError):
            await self.db.finalize_trya_dcs_upload(
                replacement_id,
                original_sha256="incomplete",
            )
        old_after_failure = await self.db.get_trya_dcs_song(old_id)
        self.assertEqual(old_after_failure["active"], 1)

        replacement = await self._finalize(replacement_id, "replacement")
        old_after_success = await self.db.get_trya_dcs_song(old_id)
        self.assertEqual(replacement["active"], 1)
        self.assertEqual(old_after_success["active"], 0)
        self.assertEqual(
            old_after_success["remove_reason"], f"replaced_by:{replacement_id}"
        )

    async def test_playlist_snapshots_are_listed_newest_first(self):
        first_id = await self.db.save_trya_dcs_playlist_snapshot(
            created_by="admin",
            mode="manual",
            songs=[{
                "title": "First",
                "suno_url": "https://suno.com/song/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            }],
        )
        second_id = await self.db.save_trya_dcs_playlist_snapshot(
            created_by="scheduler",
            mode="scheduled",
            songs=[
                {
                    "title": "Second",
                    "suno_url": "https://suno.com/song/ffffffff-bbbb-cccc-dddd-eeeeeeeeeeee",
                },
                {"title": "No Suno"},
            ],
        )

        listed = await self.db.get_trya_dcs_playlist_snapshots()
        self.assertEqual([row["id"] for row in listed], [second_id, first_id])
        self.assertEqual(listed[0]["song_count"], 2)
        self.assertTrue(listed[0]["scheduled"])
        self.assertNotIn("songs", listed[0])

        snapshot = await self.db.get_trya_dcs_playlist_snapshot(second_id)
        self.assertEqual(snapshot["songs"][0]["title"], "Second")
        self.assertEqual(
            snapshot["urls"],
            ["https://suno.com/song/ffffffff-bbbb-cccc-dddd-eeeeeeeeeeee"],
        )


if __name__ == "__main__":
    unittest.main()
