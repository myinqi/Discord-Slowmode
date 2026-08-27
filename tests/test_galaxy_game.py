import hashlib
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from bot.database import Database, calculate_galaxy_listen_credits
except ModuleNotFoundError as exc:
    if exc.name != "aiosqlite":
        raise
    raise unittest.SkipTest("aiosqlite is required for Galaxy database tests") from exc


class GalaxyCreditCalculationTests(unittest.TestCase):
    def test_full_listen_is_maximum_and_every_forward_seek_deducts(self):
        complete = calculate_galaxy_listen_credits(
            duration_seconds=120,
            eligible_seconds=120,
            forward_seek_seconds=0,
            credits_per_minute=60,
        )
        tiny_seek = calculate_galaxy_listen_credits(
            duration_seconds=120,
            eligible_seconds=119,
            forward_seek_seconds=1,
            credits_per_minute=60,
        )
        large_seek = calculate_galaxy_listen_credits(
            duration_seconds=120,
            eligible_seconds=100,
            forward_seek_seconds=20,
            credits_per_minute=60,
        )
        self.assertEqual(complete, 150)
        self.assertLess(tiny_seek, complete)
        self.assertEqual(large_seek, 80)


class GalaxyDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.temp_dir.name) / "galaxy.db"))
        await self.db.connect()
        await self.db.galaxy_upsert_user(42, "Explorer")

    async def asyncTearDown(self):
        await self.db.close()
        self.temp_dir.cleanup()

    async def _listen(self, duration=40, *, token=b"expedition", message_id=99):
        token_hash = hashlib.sha256(token).hexdigest()
        expedition_id = await self.db.galaxy_create_expedition(
            token_hash=token_hash,
            discord_user_id=42,
            channel_id=10,
            time_range_days=7,
            song_limit=5,
            songs=[{"message_id": str(message_id), "channel_id": "10", "uuid": "song"}],
        )
        listen = await self.db.galaxy_start_listen(
            expedition_id=expedition_id,
            discord_user_id=42,
            message_id=message_id,
            channel_id=10,
            suno_uuid="song",
            duration_seconds=duration,
        )
        return token_hash, listen

    async def test_schema_seed_and_expedition(self):
        upgrades = await self.db.galaxy_get_upgrades()
        self.assertGreaterEqual(len(upgrades), 8)
        token_hash, _ = await self._listen()
        expedition = await self.db.galaxy_get_expedition(token_hash)
        self.assertEqual(expedition["discord_user_id"], 42)
        self.assertEqual(expedition["songs"][0]["message_id"], "99")

    async def test_heartbeat_seek_and_idempotent_completion(self):
        _, listen = await self._listen()
        await self.db.db.execute(
            "UPDATE galaxy_listens SET last_heartbeat_at = ? WHERE id = ?",
            (time.time() - 20, listen["id"]),
        )
        await self.db.db.commit()
        updated = await self.db.galaxy_heartbeat_listen(
            listen_id=listen["id"], discord_user_id=42,
            audio_position=20, paused=False, seeked=False,
        )
        self.assertGreaterEqual(updated["eligible_seconds"], 19)

        eligible_before_seek = updated["eligible_seconds"]
        await self.db.db.execute(
            "UPDATE galaxy_listens SET last_heartbeat_at = ? WHERE id = ?",
            (time.time() - 10, listen["id"]),
        )
        await self.db.db.commit()
        updated = await self.db.galaxy_heartbeat_listen(
            listen_id=listen["id"], discord_user_id=42,
            audio_position=38, paused=False, seeked=True,
        )
        self.assertEqual(updated["eligible_seconds"], eligible_before_seek)
        self.assertGreater(updated["seeked_seconds"], 0)

        await self.db.db.execute(
            "UPDATE galaxy_listens SET eligible_seconds = 35 WHERE id = ?",
            (listen["id"],),
        )
        await self.db.db.commit()
        first = await self.db.galaxy_complete_listen(
            listen_id=listen["id"], discord_user_id=42,
            minimum_seconds=30, minimum_percent=0.7,
            credits_per_minute=60, daily_cap=100,
            reaction_emoji_id=123,
        )
        second = await self.db.galaxy_complete_listen(
            listen_id=listen["id"], discord_user_id=42,
            minimum_seconds=30, minimum_percent=0.7,
            credits_per_minute=60, daily_cap=100,
            reaction_emoji_id=123,
        )
        self.assertTrue(first[0])
        self.assertEqual(second[1], first[1])
        profile = await self.db.galaxy_get_profile(42)
        self.assertEqual(profile["credits"], first[1])
        async with self.db.db.execute(
            "SELECT COUNT(*) FROM galaxy_credit_ledger WHERE reason = 'listen'"
        ) as cursor:
            self.assertEqual((await cursor.fetchone())[0], 1)
        async with self.db.db.execute(
            "SELECT COUNT(*) FROM galaxy_reaction_jobs"
        ) as cursor:
            self.assertEqual((await cursor.fetchone())[0], 1)
        galaxy_reactions = await self.db.get_galaxy_player_song_reactions(99)
        self.assertEqual(len(galaxy_reactions), 1)
        self.assertEqual(galaxy_reactions[0]["discord_display_name"], "Explorer")
        self.assertEqual(galaxy_reactions[0]["emoji_id"], 123)

    async def test_transactional_shop_and_loadout(self):
        self.assertFalse(await self.db.galaxy_set_loadout(42, "hull", "raven"))
        await self.db.db.execute(
            "UPDATE galaxy_users SET credits = 500 WHERE discord_user_id = 42"
        )
        await self.db.db.commit()
        bought, _ = await self.db.galaxy_buy_upgrade(42, "raven")
        self.assertTrue(bought)
        self.assertTrue(await self.db.galaxy_set_loadout(42, "hull", "raven"))
        profile = await self.db.galaxy_get_profile(42)
        self.assertEqual(profile["selected_hull"], "raven")
        self.assertIn("raven", profile["owned_upgrades"])

    async def test_user_navigation_preferences(self):
        profile = await self.db.galaxy_get_profile(42)
        self.assertEqual(profile["auto_navigation"], 1)
        self.assertEqual(profile["expedition_days"], 1)
        self.assertEqual(profile["skip_completed"], 1)
        self.assertEqual(profile["shop_collapsed"], 0)
        self.assertEqual(profile["volume_percent"], 80)
        profile = await self.db.galaxy_set_preferences(
            42, auto_navigation=False, expedition_days=14,
            skip_completed=False, shop_collapsed=True, volume_percent=35,
        )
        self.assertEqual(profile["auto_navigation"], 0)
        self.assertEqual(profile["expedition_days"], 14)
        self.assertEqual(profile["skip_completed"], 0)
        self.assertEqual(profile["shop_collapsed"], 1)
        self.assertEqual(profile["volume_percent"], 35)

    async def test_admin_can_configure_safe_shop_effects(self):
        self.assertTrue(await self.db.galaxy_update_upgrade(
            "nebula", name="Aurora", description="Custom hull", price=333,
            enabled=True, effect="cube", color="#12abef",
        ))
        upgrades = {row["id"]: row for row in await self.db.galaxy_get_upgrades()}
        self.assertEqual(upgrades["nebula"]["name"], "Aurora")
        self.assertEqual(upgrades["nebula"]["price"], 333)
        self.assertIn('"effect":"cube"', upgrades["nebula"]["config_json"])
        self.assertFalse(await self.db.galaxy_update_upgrade(
            "nebula", name="Bad", description="", price=1,
            enabled=True, effect="javascript", color="red",
        ))

        created = await self.db.galaxy_create_upgrade(
            category="trail", name="Matrix Wake", description="Green nebula",
            price=175, enabled=False, effect="nebula", color="#a4ec6c",
        )
        self.assertIsNotNone(created)
        self.assertEqual(created["id"], "matrix_wake")
        self.assertEqual(created["enabled"], 0)
        self.assertIn('"effect":"nebula"', created["config_json"])
        duplicate = await self.db.galaxy_create_upgrade(
            category="trail", name="Matrix Wake", description="Warp variant",
            price=225, enabled=True, effect="warp", color="#8b7cff",
        )
        self.assertEqual(duplicate["id"], "matrix_wake_2")
        self.assertIsNone(await self.db.galaxy_create_upgrade(
            category="scanner", name="Unsafe", description="", price=1,
            enabled=True, effect="warp", color="#ffffff",
        ))

    async def test_complete_no_seek_earns_more_than_forward_seek(self):
        _, complete_listen = await self._listen(
            duration=120, token=b"complete", message_id=101
        )
        _, seeked_listen = await self._listen(
            duration=120, token=b"seeked", message_id=102
        )
        await self.db.db.execute(
            "UPDATE galaxy_listens SET eligible_seconds = 120 WHERE id = ?",
            (complete_listen["id"],),
        )
        await self.db.db.execute(
            """UPDATE galaxy_listens
               SET eligible_seconds = 100, seeked_seconds = 20 WHERE id = ?""",
            (seeked_listen["id"],),
        )
        await self.db.db.commit()

        complete_result = await self.db.galaxy_complete_listen(
            listen_id=complete_listen["id"], discord_user_id=42,
            minimum_seconds=30, minimum_percent=0.7,
            credits_per_minute=60, daily_cap=1000,
            full_listen_bonus_percent=25,
            forward_seek_penalty_percent=100,
        )
        seeked_result = await self.db.galaxy_complete_listen(
            listen_id=seeked_listen["id"], discord_user_id=42,
            minimum_seconds=30, minimum_percent=0.7,
            credits_per_minute=60, daily_cap=1000,
            full_listen_bonus_percent=25,
            forward_seek_penalty_percent=100,
        )

        self.assertEqual(complete_result[1], 150)
        self.assertEqual(seeked_result[1], 80)
        self.assertGreater(complete_result[1], seeked_result[1])
        heard = await self.db.galaxy_get_fully_listened_message_ids(42, [101, 102])
        self.assertEqual(heard, {101})


class _GalaxyMember:
    id = 42
    display_name = "Explorer"
    display_avatar = None


class _GalaxyChannel:
    id = 10
    name = "showcase"


class _GalaxyGuild:
    id = 123
    emojis = []
    text_channels = [_GalaxyChannel()]

    def get_member(self, user_id):
        return _GalaxyMember() if int(user_id) == 42 else None

    def get_channel(self, channel_id):
        return _GalaxyChannel() if int(channel_id) == 10 else None


class _GalaxyBot:
    user = None

    def is_ready(self):
        return True

    def get_guild(self, guild_id):
        return _GalaxyGuild() if int(guild_id) == 123 else None


class GalaxyRouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from config import Config
        from web.app import create_app

        self.old_guild_id = Config.GUILD_ID
        Config.GUILD_ID = 123
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db = Database(str(Path(self.temp_dir.name) / "routes.db"))
        await self.db.connect()
        await self.db.galaxy_upsert_user(42, "Explorer")
        await self.db.set_setting("galaxy_enabled", "on")
        await self.db.set_setting("galaxy_allowed_channel_ids", "10")
        await self.db.db.execute(
            """INSERT INTO monitored_channels
               (channel_id, channel_name, cooldown_minutes) VALUES (?, ?, ?)""",
            (10, "showcase", 0),
        )
        await self.db.db.execute(
            """INSERT INTO song_posts
               (channel_id, user_id, user_name, url, posted_at, message_id,
                song_title, suno_uuid)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                10, 7, "Artist", "https://suno.com/song/39a09dfb-bf72-4852-b813-49c3a02d3aaa",
                time.time(), 99, "Galaxy Song", "39a09dfb-bf72-4852-b813-49c3a02d3aaa",
            ),
        )
        await self.db.db.commit()
        self.app = create_app(self.db, _GalaxyBot())
        self.app.secret_key = "test"
        self.client = self.app.test_client()
        async with self.client.session_transaction() as session:
            session["galaxy_discord"] = {
                "discord_user_id": "42", "display_name": "Explorer", "avatar_url": "",
            }
            session["galaxy_csrf"] = "csrf"

    async def asyncTearDown(self):
        from config import Config
        Config.GUILD_ID = self.old_guild_id
        await self.db.close()
        self.temp_dir.cleanup()

    async def test_config_and_expedition_are_membership_and_channel_bounded(self):
        config = await self.client.get("/galaxy/api/config")
        self.assertEqual(config.status_code, 200)
        payload = await config.get_json()
        self.assertEqual(payload["channels"][0]["id"], "10")

        channels = await self.client.get("/galaxy/api/channels")
        self.assertEqual(channels.status_code, 200)
        self.assertEqual((await channels.get_json())["channels"][0]["name"], "showcase")

        profile = await self.client.get("/galaxy/api/profile")
        self.assertEqual(profile.status_code, 200)
        self.assertEqual((await profile.get_json())["profile"]["discord_user_id"], 42)

        response = await self.client.post(
            "/galaxy/api/expeditions",
            json={"channel_id": "10", "days": 7, "limit": 5},
            headers={"X-CSRF-Token": "csrf"},
        )
        self.assertEqual(response.status_code, 200)
        expedition = await response.get_json()
        self.assertEqual(expedition["songs"][0]["title"], "Galaxy Song")
        self.assertEqual(expedition["songs"][0]["message_id"], "99")
        self.assertEqual(
            expedition["songs"][0]["url"],
            "https://suno.com/song/39a09dfb-bf72-4852-b813-49c3a02d3aaa",
        )
        restored = await self.client.get(
            f"/galaxy/api/expeditions/{expedition['token']}"
        )
        self.assertEqual(restored.status_code, 200)
        self.assertEqual((await restored.get_json())["songs"][0]["message_id"], "99")

        rejected = await self.client.post(
            "/galaxy/api/expeditions",
            json={"channel_id": "11", "days": 7, "limit": 5},
            headers={"X-CSRF-Token": "csrf"},
        )
        self.assertEqual(rejected.status_code, 403)

    async def test_missing_song_title_is_resolved_and_persisted(self):
        missing_uuid = "49a09dfb-bf72-4852-b813-49c3a02d3aab"
        await self.db.db.execute(
            """INSERT INTO song_posts
               (channel_id, user_id, user_name, url, posted_at, message_id,
                song_title, suno_uuid)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                10, 8, "Legacy Artist", f"https://suno.com/song/{missing_uuid}",
                time.time() + 1, 100, None, missing_uuid,
            ),
        )
        await self.db.db.commit()

        async def resolve_missing(songs, max_concurrent=5):
            for song in songs:
                song["title"] = "Resolved Galaxy Song"
            return songs

        with patch("bot.suno_meta.enrich_songs", side_effect=resolve_missing):
            response = await self.client.post(
                "/galaxy/api/expeditions",
                json={"channel_id": "10", "days": 7, "limit": 5},
                headers={"X-CSRF-Token": "csrf"},
            )
        self.assertEqual(response.status_code, 200)
        expedition = await response.get_json()
        resolved = next(song for song in expedition["songs"] if song["message_id"] == "100")
        self.assertEqual(resolved["title"], "Resolved Galaxy Song")
        async with self.db.db.execute(
            "SELECT song_title FROM song_posts WHERE message_id = 100"
        ) as cursor:
            self.assertEqual((await cursor.fetchone())[0], "Resolved Galaxy Song")

    async def test_galaxy_page_uses_custom_favicon(self):
        response = await self.client.get("/galaxy")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Galaxy_emoji.png", await response.get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
