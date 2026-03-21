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

        # Add message_id column to song_posts if missing
        async with self.db.execute("PRAGMA table_info(song_posts)") as cursor:
            sp_columns = [row[1] async for row in cursor]
        if "message_id" not in sp_columns:
            await self.db.execute("ALTER TABLE song_posts ADD COLUMN message_id INTEGER")
            await self.db.commit()

        # Create song_reactions table
        await self.db.executescript("""
            CREATE TABLE IF NOT EXISTS song_reactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                song_url TEXT,
                post_author_id INTEGER,
                reactor_user_id INTEGER NOT NULL,
                reactor_user_name TEXT,
                emoji TEXT NOT NULL,
                reacted_at REAL DEFAULT (unixepoch()),
                UNIQUE(message_id, reactor_user_id, emoji)
            );
        """)
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

    async def add_song_post(self, channel_id: int, user_id: int, user_name: str, url: str, posted_at: float, message_id: int = None):
        await self.db.execute(
            "INSERT OR IGNORE INTO song_posts (channel_id, user_id, user_name, url, posted_at, message_id) VALUES (?, ?, ?, ?, ?, ?)",
            (channel_id, user_id, user_name, url, posted_at, message_id),
        )
        await self.db.commit()

    async def add_song_posts_bulk(self, rows: list[tuple]):
        await self.db.executemany(
            "INSERT OR IGNORE INTO song_posts (channel_id, user_id, user_name, url, posted_at) VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        await self.db.commit()

    async def get_song_stats(self, channel_id: int = None) -> dict:
        """Return song counts grouped by year, month, week, day + averages + trend data."""
        where = "WHERE channel_id = ?" if channel_id else ""
        params = (channel_id,) if channel_id else ()

        stats = {
            "total": 0, "by_year": [], "by_month": [], "by_week": [], "by_day": [],
            "avg_per_year": 0.0, "avg_per_month": 0.0, "avg_per_week": 0.0, "avg_per_day": 0.0,
            "trend_labels": [], "trend_values": [],
        }

        # Total
        async with self.db.execute(
            f"SELECT COUNT(*) FROM song_posts {where}", params
        ) as cursor:
            row = await cursor.fetchone()
            stats["total"] = row[0]

        if stats["total"] == 0:
            return stats

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

        # Averages based on distinct time spans
        if stats["by_year"]:
            stats["avg_per_year"] = round(stats["total"] / len(stats["by_year"]), 1)
        if stats["by_month"]:
            # Count ALL distinct months, not just last 12
            async with self.db.execute(
                f"SELECT COUNT(DISTINCT strftime('%Y-%m', posted_at, 'unixepoch')) FROM song_posts {where}", params
            ) as cursor:
                n_months = (await cursor.fetchone())[0]
            stats["avg_per_month"] = round(stats["total"] / max(n_months, 1), 1)
        # Distinct weeks
        async with self.db.execute(
            f"SELECT COUNT(DISTINCT strftime('%Y-%W', posted_at, 'unixepoch')) FROM song_posts {where}", params
        ) as cursor:
            n_weeks = (await cursor.fetchone())[0]
        stats["avg_per_week"] = round(stats["total"] / max(n_weeks, 1), 1)
        # Distinct days
        async with self.db.execute(
            f"SELECT COUNT(DISTINCT strftime('%Y-%m-%d', posted_at, 'unixepoch')) FROM song_posts {where}", params
        ) as cursor:
            n_days = (await cursor.fetchone())[0]
        stats["avg_per_day"] = round(stats["total"] / max(n_days, 1), 1)

        # Trend data: monthly counts chronologically (last 24 months for chart)
        async with self.db.execute(
            f"SELECT strftime('%Y-%m', posted_at, 'unixepoch') as ym, COUNT(*) as cnt "
            f"FROM song_posts {where} GROUP BY ym ORDER BY ym ASC LIMIT 24", params
        ) as cursor:
            trend = [{"label": r[0], "count": r[1]} for r in await cursor.fetchall()]
            stats["trend_labels"] = [t["label"] for t in trend]
            stats["trend_values"] = [t["count"] for t in trend]

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

    async def get_user_song_stats(self, user_id: int) -> dict:
        """Comprehensive stats for a single user."""
        stats = {
            "total": 0,
            "per_channel": [],
            "first_post": None,
            "last_post": None,
            "avg_per_week": 0.0,
            "avg_per_month": 0.0,
            "by_month": [],
            "by_weekday": [],
            "top_days": [],
            "active_weeks": 0,
        }

        # Total
        async with self.db.execute(
            "SELECT COUNT(*) FROM song_posts WHERE user_id = ?", (user_id,)
        ) as cursor:
            stats["total"] = (await cursor.fetchone())[0]

        if stats["total"] == 0:
            return stats

        # Per channel
        async with self.db.execute(
            "SELECT channel_id, COUNT(*) as cnt FROM song_posts WHERE user_id = ? GROUP BY channel_id ORDER BY cnt DESC",
            (user_id,),
        ) as cursor:
            stats["per_channel"] = [{"channel_id": r[0], "count": r[1]} for r in await cursor.fetchall()]

        # First and last post
        async with self.db.execute(
            "SELECT MIN(posted_at), MAX(posted_at) FROM song_posts WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            stats["first_post"] = row[0]
            stats["last_post"] = row[1]

        # Averages: calculate from timespan
        if stats["first_post"] and stats["last_post"]:
            span_seconds = stats["last_post"] - stats["first_post"]
            span_weeks = max(span_seconds / 604800, 1)
            span_months = max(span_seconds / 2592000, 1)
            stats["avg_per_week"] = round(stats["total"] / span_weeks, 1)
            stats["avg_per_month"] = round(stats["total"] / span_months, 1)

        # By month (last 12)
        async with self.db.execute(
            "SELECT strftime('%Y-%m', posted_at, 'unixepoch') as ym, COUNT(*) as cnt "
            "FROM song_posts WHERE user_id = ? GROUP BY ym ORDER BY ym DESC LIMIT 12",
            (user_id,),
        ) as cursor:
            stats["by_month"] = [{"label": r[0], "count": r[1]} for r in await cursor.fetchall()]

        # By weekday (0=Sunday .. 6=Saturday in SQLite strftime %w)
        weekday_names = ["So", "Mo", "Di", "Mi", "Do", "Fr", "Sa"]
        async with self.db.execute(
            "SELECT strftime('%w', posted_at, 'unixepoch') as wd, COUNT(*) as cnt "
            "FROM song_posts WHERE user_id = ? GROUP BY wd ORDER BY wd",
            (user_id,),
        ) as cursor:
            stats["by_weekday"] = [{"label": weekday_names[int(r[0])], "day_num": int(r[0]), "count": r[1]} for r in await cursor.fetchall()]

        # Top posting days
        async with self.db.execute(
            "SELECT strftime('%Y-%m-%d', posted_at, 'unixepoch') as yd, COUNT(*) as cnt "
            "FROM song_posts WHERE user_id = ? GROUP BY yd ORDER BY cnt DESC LIMIT 5",
            (user_id,),
        ) as cursor:
            stats["top_days"] = [{"label": r[0], "count": r[1]} for r in await cursor.fetchall()]

        # Active weeks count
        async with self.db.execute(
            "SELECT COUNT(DISTINCT strftime('%Y-%W', posted_at, 'unixepoch')) "
            "FROM song_posts WHERE user_id = ?",
            (user_id,),
        ) as cursor:
            stats["active_weeks"] = (await cursor.fetchone())[0]

        return stats

    async def find_songs(self, user_id: int = None, limit: int = 1, random: bool = False) -> list[dict]:
        """Find songs, optionally filtered by user. Can return random results."""
        where = "WHERE user_id = ?" if user_id else ""
        params = (user_id,) if user_id else ()
        order = "ORDER BY RANDOM()" if random else "ORDER BY posted_at DESC"
        async with self.db.execute(
            f"SELECT channel_id, user_id, user_name, url, posted_at FROM song_posts {where} {order} LIMIT ?",
            (*params, limit),
        ) as cursor:
            return [
                {"channel_id": r[0], "user_id": r[1], "user_name": r[2], "url": r[3], "posted_at": r[4]}
                for r in await cursor.fetchall()
            ]

    async def get_all_users_ranking(self) -> list[dict]:
        """Leaderboard: all users ranked by total songs."""
        async with self.db.execute(
            "SELECT user_id, user_name, COUNT(*) as cnt FROM song_posts GROUP BY user_id ORDER BY cnt DESC"
        ) as cursor:
            return [{"user_id": r[0], "user_name": r[1], "count": r[2]} for r in await cursor.fetchall()]

    # --- Song Reactions ---

    async def add_song_reaction(self, message_id: int, channel_id: int, song_url: str,
                                 post_author_id: int, reactor_user_id: int,
                                 reactor_user_name: str, emoji: str):
        await self.db.execute(
            "INSERT OR IGNORE INTO song_reactions "
            "(message_id, channel_id, song_url, post_author_id, reactor_user_id, reactor_user_name, emoji) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (message_id, channel_id, song_url, post_author_id, reactor_user_id, reactor_user_name, emoji),
        )
        await self.db.commit()

    async def remove_song_reaction(self, message_id: int, reactor_user_id: int, emoji: str):
        await self.db.execute(
            "DELETE FROM song_reactions WHERE message_id = ? AND reactor_user_id = ? AND emoji = ?",
            (message_id, reactor_user_id, emoji),
        )
        await self.db.commit()

    async def get_song_post_by_message_id(self, message_id: int) -> dict | None:
        async with self.db.execute(
            "SELECT * FROM song_posts WHERE message_id = ?", (message_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_reaction_stats(self, channel_id: int = None, days: int = 0) -> dict:
        """Comprehensive reaction stats, optionally filtered by channel and time range."""
        conditions = []
        params = []
        if channel_id:
            conditions.append("channel_id = ?")
            params.append(channel_id)
        if days:
            conditions.append(f"reacted_at >= unixepoch('now', '-{int(days)} days')")

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params = tuple(params)

        stats = {
            "total_reactions": 0,
            "unique_reactors": 0,
            "unique_songs_reacted": 0,
            "top_emojis": [],
            "top_reactors": [],
            "top_songs": [],
            "most_reacted_authors": [],
            "avg_reactions_per_song": 0.0,
        }

        # Total reactions
        async with self.db.execute(
            f"SELECT COUNT(*) FROM song_reactions {where}", params
        ) as cursor:
            stats["total_reactions"] = (await cursor.fetchone())[0]

        if stats["total_reactions"] == 0:
            return stats

        # Unique reactors
        async with self.db.execute(
            f"SELECT COUNT(DISTINCT reactor_user_id) FROM song_reactions {where}", params
        ) as cursor:
            stats["unique_reactors"] = (await cursor.fetchone())[0]

        # Unique songs reacted
        async with self.db.execute(
            f"SELECT COUNT(DISTINCT message_id) FROM song_reactions {where}", params
        ) as cursor:
            stats["unique_songs_reacted"] = (await cursor.fetchone())[0]

        # Avg reactions per song
        if stats["unique_songs_reacted"] > 0:
            stats["avg_reactions_per_song"] = round(
                stats["total_reactions"] / stats["unique_songs_reacted"], 1
            )

        # Top emojis
        async with self.db.execute(
            f"SELECT emoji, COUNT(*) as cnt FROM song_reactions {where} GROUP BY emoji ORDER BY cnt DESC LIMIT 15",
            params,
        ) as cursor:
            stats["top_emojis"] = [{"emoji": r[0], "count": r[1]} for r in await cursor.fetchall()]

        # Top reactors
        async with self.db.execute(
            f"SELECT reactor_user_id, reactor_user_name, COUNT(DISTINCT message_id) as cnt "
            f"FROM song_reactions {where} GROUP BY reactor_user_id ORDER BY cnt DESC LIMIT 10",
            params,
        ) as cursor:
            stats["top_reactors"] = [
                {"user_id": r[0], "user_name": r[1], "count": r[2]}
                for r in await cursor.fetchall()
            ]

        # Top songs (most reactions)
        async with self.db.execute(
            f"SELECT message_id, song_url, post_author_id, COUNT(*) as cnt "
            f"FROM song_reactions {where} GROUP BY message_id ORDER BY cnt DESC LIMIT 10",
            params,
        ) as cursor:
            stats["top_songs"] = [
                {"message_id": r[0], "song_url": r[1], "post_author_id": r[2], "count": r[3]}
                for r in await cursor.fetchall()
            ]

        # Most reacted authors (whose songs get the most reactions)
        author_conditions = list(conditions) if conditions else []
        author_conditions.append("post_author_id IS NOT NULL")
        author_where = "WHERE " + " AND ".join(author_conditions)
        async with self.db.execute(
            f"SELECT post_author_id, COUNT(*) as cnt "
            f"FROM song_reactions {author_where} "
            f"GROUP BY post_author_id ORDER BY cnt DESC LIMIT 10",
            params,
        ) as cursor:
            stats["most_reacted_authors"] = [
                {"user_id": r[0], "count": r[1]} for r in await cursor.fetchall()
            ]

        return stats

    async def get_reactor_activity(self, channel_id: int = None, granularity: str = "daily",
                                    days: int = 30) -> dict:
        """Return reactor counts per day or week for a line chart."""
        conditions = []
        params = []
        if channel_id:
            conditions.append("channel_id = ?")
            params.append(channel_id)
        if days:
            conditions.append(f"reacted_at >= unixepoch('now', '-{int(days)} days')")

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        if granularity == "weekly":
            fmt = "'%Y-W%W'"
        else:
            fmt = "'%Y-%m-%d'"

        async with self.db.execute(
            f"SELECT strftime({fmt}, reacted_at, 'unixepoch') as period, "
            f"COUNT(DISTINCT reactor_user_id) as cnt "
            f"FROM song_reactions {where} GROUP BY period ORDER BY period ASC",
            tuple(params),
        ) as cursor:
            rows = [{"label": r[0], "count": r[1]} for r in await cursor.fetchall()]

        return {
            "labels": [r["label"] for r in rows],
            "counts": [r["count"] for r in rows],
        }

    async def get_top_songs_filtered(self, channel_id: int = None,
                                      date_from: str = None, date_to: str = None) -> list[dict]:
        """Top songs by reactions, optionally filtered by date range."""
        conditions = []
        params = []
        if channel_id:
            conditions.append("channel_id = ?")
            params.append(channel_id)
        if date_from:
            conditions.append("reacted_at >= unixepoch(?)")
            params.append(date_from)
        if date_to:
            conditions.append("reacted_at < unixepoch(?)")
            params.append(date_to)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        async with self.db.execute(
            f"SELECT message_id, song_url, post_author_id, COUNT(*) as cnt "
            f"FROM song_reactions {where} GROUP BY message_id ORDER BY cnt DESC LIMIT 10",
            tuple(params),
        ) as cursor:
            return [
                {"message_id": r[0], "song_url": r[1], "post_author_id": r[2], "count": r[3]}
                for r in await cursor.fetchall()
            ]

    async def get_reaction_channels(self) -> list[dict]:
        """Return channels that have reactions, with counts."""
        async with self.db.execute(
            "SELECT channel_id, COUNT(*) as cnt FROM song_reactions GROUP BY channel_id ORDER BY cnt DESC"
        ) as cursor:
            return [{"channel_id": r[0], "count": r[1]} for r in await cursor.fetchall()]
