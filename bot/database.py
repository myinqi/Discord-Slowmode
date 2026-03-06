import aiosqlite
import os
import time
from typing import Optional


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.db: Optional[aiosqlite.Connection] = None

    async def connect(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self.db = await aiosqlite.connect(self.db_path)
        self.db.row_factory = aiosqlite.Row
        await self.db.execute("PRAGMA journal_mode=WAL")
        await self.db.execute("PRAGMA foreign_keys=ON")
        await self._create_tables()

    async def close(self):
        if self.db:
            await self.db.close()

    async def _create_tables(self):
        await self.db.executescript("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS web_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                is_admin INTEGER DEFAULT 0,
                must_change_password INTEGER DEFAULT 1,
                created_at REAL DEFAULT (unixepoch())
            );

            CREATE TABLE IF NOT EXISTS monitored_channels (
                channel_id INTEGER PRIMARY KEY,
                channel_name TEXT NOT NULL,
                cooldown_minutes INTEGER DEFAULT 0,
                enabled INTEGER DEFAULT 1,
                added_at REAL DEFAULT (unixepoch())
            );

            CREATE TABLE IF NOT EXISTS exempt_roles (
                role_id INTEGER PRIMARY KEY,
                role_name TEXT NOT NULL,
                added_at REAL DEFAULT (unixepoch())
            );

            CREATE TABLE IF NOT EXISTS command_roles (
                role_id INTEGER PRIMARY KEY,
                role_name TEXT NOT NULL,
                added_at REAL DEFAULT (unixepoch())
            );

            CREATE TABLE IF NOT EXISTS cooldown_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                timestamp REAL NOT NULL,
                UNIQUE(user_id, channel_id)
            );

            CREATE TABLE IF NOT EXISTS listening_party_config (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                input_channel_id INTEGER NOT NULL,
                output_channel_id INTEGER NOT NULL,
                time_range_hours INTEGER DEFAULT 24,
                UNIQUE(input_channel_id)
            );

            CREATE TABLE IF NOT EXISTS playlist_search_config (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER NOT NULL UNIQUE
            );

            CREATE TABLE IF NOT EXISTS song_posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                user_name TEXT,
                url TEXT NOT NULL,
                posted_at REAL NOT NULL,
                UNIQUE(channel_id, url)
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL DEFAULT (unixepoch()),
                event_type TEXT NOT NULL,
                user_id INTEGER,
                user_name TEXT,
                channel_id INTEGER,
                channel_name TEXT,
                details TEXT,
                actor TEXT
            );
        """)
        await self.db.commit()
        await self._run_migrations()

    async def _run_migrations(self):
        async with self.db.execute("PRAGMA table_info(monitored_channels)") as cursor:
            columns = [row[1] async for row in cursor]
        if "cooldown_hours" in columns and "cooldown_minutes" not in columns:
            await self.db.execute(
                "ALTER TABLE monitored_channels RENAME COLUMN cooldown_hours TO cooldown_minutes"
            )
            await self.db.commit()

    # --- Settings ---

    async def get_setting(self, key: str, default: str = "") -> str:
        async with self.db.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ) as cursor:
            row = await cursor.fetchone()
            return row["value"] if row else default

    async def set_setting(self, key: str, value: str):
        await self.db.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        await self.db.commit()

    # --- Web Users ---

    async def get_web_user(self, username: str) -> Optional[dict]:
        async with self.db.execute(
            "SELECT * FROM web_users WHERE username = ?", (username,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_web_user_by_id(self, user_id: int) -> Optional[dict]:
        async with self.db.execute(
            "SELECT * FROM web_users WHERE id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_all_web_users(self) -> list[dict]:
        async with self.db.execute(
            "SELECT id, username, is_admin, must_change_password, created_at FROM web_users"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def create_web_user(
        self, username: str, password_hash: str, is_admin: int = 0
    ) -> bool:
        try:
            await self.db.execute(
                "INSERT INTO web_users (username, password_hash, is_admin, must_change_password) "
                "VALUES (?, ?, ?, 1)",
                (username, password_hash, is_admin),
            )
            await self.db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def update_web_user_password(self, user_id: int, password_hash: str):
        await self.db.execute(
            "UPDATE web_users SET password_hash = ?, must_change_password = 0 WHERE id = ?",
            (password_hash, user_id),
        )
        await self.db.commit()

    async def delete_web_user(self, user_id: int):
        await self.db.execute("DELETE FROM web_users WHERE id = ?", (user_id,))
        await self.db.commit()

    # --- Monitored Channels ---

    async def get_monitored_channels(self) -> list[dict]:
        async with self.db.execute("SELECT * FROM monitored_channels") as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_monitored_channel(self, channel_id: int) -> Optional[dict]:
        async with self.db.execute(
            "SELECT * FROM monitored_channels WHERE channel_id = ?", (channel_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def add_monitored_channel(
        self, channel_id: int, channel_name: str, cooldown_minutes: int = 0
    ):
        await self.db.execute(
            "INSERT INTO monitored_channels (channel_id, channel_name, cooldown_minutes) "
            "VALUES (?, ?, ?) ON CONFLICT(channel_id) DO UPDATE SET "
            "channel_name = excluded.channel_name, cooldown_minutes = excluded.cooldown_minutes",
            (channel_id, channel_name, cooldown_minutes),
        )
        await self.db.commit()

    async def update_channel_cooldown(self, channel_id: int, cooldown_minutes: int):
        await self.db.execute(
            "UPDATE monitored_channels SET cooldown_minutes = ? WHERE channel_id = ?",
            (cooldown_minutes, channel_id),
        )
        await self.db.commit()

    async def toggle_channel(self, channel_id: int, enabled: bool):
        await self.db.execute(
            "UPDATE monitored_channels SET enabled = ? WHERE channel_id = ?",
            (1 if enabled else 0, channel_id),
        )
        await self.db.commit()

    async def remove_monitored_channel(self, channel_id: int):
        await self.db.execute(
            "DELETE FROM monitored_channels WHERE channel_id = ?", (channel_id,)
        )
        await self.db.execute(
            "DELETE FROM cooldown_records WHERE channel_id = ?", (channel_id,)
        )
        await self.db.commit()

    # --- Exempt Roles ---

    async def get_exempt_roles(self) -> list[dict]:
        async with self.db.execute("SELECT * FROM exempt_roles") as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def add_exempt_role(self, role_id: int, role_name: str):
        await self.db.execute(
            "INSERT INTO exempt_roles (role_id, role_name) VALUES (?, ?) "
            "ON CONFLICT(role_id) DO UPDATE SET role_name = excluded.role_name",
            (role_id, role_name),
        )
        await self.db.commit()

    async def remove_exempt_role(self, role_id: int):
        await self.db.execute(
            "DELETE FROM exempt_roles WHERE role_id = ?", (role_id,)
        )
        await self.db.commit()

    # --- Command Roles ---

    async def get_command_roles(self) -> list[dict]:
        async with self.db.execute("SELECT * FROM command_roles") as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def add_command_role(self, role_id: int, role_name: str):
        await self.db.execute(
            "INSERT INTO command_roles (role_id, role_name) VALUES (?, ?) "
            "ON CONFLICT(role_id) DO UPDATE SET role_name = excluded.role_name",
            (role_id, role_name),
        )
        await self.db.commit()

    async def remove_command_role(self, role_id: int):
        await self.db.execute(
            "DELETE FROM command_roles WHERE role_id = ?", (role_id,)
        )
        await self.db.commit()

    # --- Cooldown Records ---

    async def get_active_cooldowns(self, channel_id: int, cooldown_minutes: int) -> list[dict]:
        cutoff = time.time() - (cooldown_minutes * 60)
        async with self.db.execute(
            "SELECT * FROM cooldown_records WHERE channel_id = ? AND timestamp > ? ORDER BY timestamp DESC",
            (channel_id, cutoff),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_cooldown_record(
        self, user_id: int, channel_id: int
    ) -> Optional[dict]:
        async with self.db.execute(
            "SELECT * FROM cooldown_records WHERE user_id = ? AND channel_id = ?",
            (user_id, channel_id),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def set_cooldown_record(self, user_id: int, channel_id: int):
        now = time.time()
        await self.db.execute(
            "INSERT INTO cooldown_records (user_id, channel_id, timestamp) "
            "VALUES (?, ?, ?) ON CONFLICT(user_id, channel_id) DO UPDATE SET timestamp = ?",
            (user_id, channel_id, now, now),
        )
        await self.db.commit()

    async def clear_cooldown_record(self, user_id: int, channel_id: int):
        await self.db.execute(
            "DELETE FROM cooldown_records WHERE user_id = ? AND channel_id = ?",
            (user_id, channel_id),
        )
        await self.db.commit()

    async def clear_all_cooldowns(self, channel_id: Optional[int] = None):
        if channel_id:
            await self.db.execute(
                "DELETE FROM cooldown_records WHERE channel_id = ?", (channel_id,)
            )
        else:
            await self.db.execute("DELETE FROM cooldown_records")
        await self.db.commit()

    # --- Audit Log ---

    async def add_audit_log(
        self,
        event_type: str,
        user_id: int = None,
        user_name: str = None,
        channel_id: int = None,
        channel_name: str = None,
        details: str = None,
        actor: str = None,
    ):
        await self.db.execute(
            "INSERT INTO audit_log (event_type, user_id, user_name, channel_id, channel_name, details, actor) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (event_type, user_id, user_name, channel_id, channel_name, details, actor),
        )
        await self.db.commit()

    async def get_audit_logs(self, limit: int = 100, offset: int = 0) -> list[dict]:
        async with self.db.execute(
            "SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_audit_log_count(self) -> int:
        async with self.db.execute("SELECT COUNT(*) as cnt FROM audit_log") as cursor:
            row = await cursor.fetchone()
            return row["cnt"]

    # --- Listening Party Config ---

    async def get_listening_party_configs(self) -> list[dict]:
        async with self.db.execute("SELECT * FROM listening_party_config") as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_listening_party_config(self, config_id: int) -> Optional[dict]:
        async with self.db.execute(
            "SELECT * FROM listening_party_config WHERE id = ?", (config_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def add_listening_party_config(
        self, input_channel_id: int, output_channel_id: int, time_range_hours: int = 24
    ):
        await self.db.execute(
            "INSERT INTO listening_party_config (input_channel_id, output_channel_id, time_range_hours) "
            "VALUES (?, ?, ?) ON CONFLICT(input_channel_id) DO UPDATE SET "
            "output_channel_id = excluded.output_channel_id, time_range_hours = excluded.time_range_hours",
            (input_channel_id, output_channel_id, time_range_hours),
        )
        await self.db.commit()

    async def update_listening_party_config(
        self, config_id: int, output_channel_id: int, time_range_hours: int
    ):
        await self.db.execute(
            "UPDATE listening_party_config SET output_channel_id = ?, time_range_hours = ? WHERE id = ?",
            (output_channel_id, time_range_hours, config_id),
        )
        await self.db.commit()

    async def remove_listening_party_config(self, config_id: int):
        await self.db.execute(
            "DELETE FROM listening_party_config WHERE id = ?", (config_id,)
        )
        await self.db.commit()

    # --- Playlist Search Config ---

    async def get_playlist_search_channels(self) -> list[dict]:
        async with self.db.execute("SELECT * FROM playlist_search_config") as cursor:
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def add_playlist_search_channel(self, channel_id: int):
        await self.db.execute(
            "INSERT OR IGNORE INTO playlist_search_config (channel_id) VALUES (?)",
            (channel_id,),
        )
        await self.db.commit()

    async def remove_playlist_search_channel(self, config_id: int):
        await self.db.execute(
            "DELETE FROM playlist_search_config WHERE id = ?", (config_id,)
        )
        await self.db.commit()

    # --- Song Posts (Statistics) ---

    async def add_song_post(self, channel_id: int, user_id: int, user_name: str, url: str, posted_at: float):
        await self.db.execute(
            "INSERT OR IGNORE INTO song_posts (channel_id, user_id, user_name, url, posted_at) VALUES (?, ?, ?, ?, ?)",
            (channel_id, user_id, user_name, url, posted_at),
        )
        await self.db.commit()

    async def add_song_posts_bulk(self, rows: list[tuple]):
        await self.db.executemany(
            "INSERT OR IGNORE INTO song_posts (channel_id, user_id, user_name, url, posted_at) VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        await self.db.commit()

    async def get_song_stats(self, channel_id: int = None) -> dict:
        """Return song counts grouped by year, month, week, day."""
        where = "WHERE channel_id = ?" if channel_id else ""
        params = (channel_id,) if channel_id else ()

        stats = {"total": 0, "by_year": [], "by_month": [], "by_week": [], "by_day": []}

        # Total
        async with self.db.execute(
            f"SELECT COUNT(*) FROM song_posts {where}", params
        ) as cursor:
            row = await cursor.fetchone()
            stats["total"] = row[0]

        # By year
        async with self.db.execute(
            f"SELECT strftime('%Y', posted_at, 'unixepoch') as yr, COUNT(*) as cnt "
            f"FROM song_posts {where} GROUP BY yr ORDER BY yr DESC", params
        ) as cursor:
            stats["by_year"] = [{"label": r[0], "count": r[1]} for r in await cursor.fetchall()]

        # By month (last 12)
        async with self.db.execute(
            f"SELECT strftime('%Y-%m', posted_at, 'unixepoch') as ym, COUNT(*) as cnt "
            f"FROM song_posts {where} GROUP BY ym ORDER BY ym DESC LIMIT 12", params
        ) as cursor:
            stats["by_month"] = [{"label": r[0], "count": r[1]} for r in await cursor.fetchall()]

        # By week (last 12)
        async with self.db.execute(
            f"SELECT strftime('%Y-W%W', posted_at, 'unixepoch') as yw, COUNT(*) as cnt "
            f"FROM song_posts {where} GROUP BY yw ORDER BY yw DESC LIMIT 12", params
        ) as cursor:
            stats["by_week"] = [{"label": r[0], "count": r[1]} for r in await cursor.fetchall()]

        # By day (last 30)
        async with self.db.execute(
            f"SELECT strftime('%Y-%m-%d', posted_at, 'unixepoch') as yd, COUNT(*) as cnt "
            f"FROM song_posts {where} GROUP BY yd ORDER BY yd DESC LIMIT 30", params
        ) as cursor:
            stats["by_day"] = [{"label": r[0], "count": r[1]} for r in await cursor.fetchall()]

        return stats

    async def get_song_stats_all_channels(self) -> list[dict]:
        """Return total song count per channel."""
        async with self.db.execute(
            "SELECT channel_id, COUNT(*) as cnt FROM song_posts GROUP BY channel_id ORDER BY cnt DESC"
        ) as cursor:
            return [{"channel_id": r[0], "count": r[1]} for r in await cursor.fetchall()]

    async def get_song_post_count(self, channel_id: int) -> int:
        async with self.db.execute(
            "SELECT COUNT(*) FROM song_posts WHERE channel_id = ?", (channel_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return row[0]
