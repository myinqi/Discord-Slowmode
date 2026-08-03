import aiosqlite
import os
import random
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
                permissions TEXT DEFAULT '[]',
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

            CREATE TABLE IF NOT EXISTS user_activity (
                user_id INTEGER PRIMARY KEY,
                user_name TEXT,
                activity_type TEXT NOT NULL,
                summary TEXT,
                channel_id INTEGER,
                channel_name TEXT,
                timestamp REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS auto_translate_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at REAL NOT NULL,
                day TEXT NOT NULL,
                month TEXT NOT NULL,
                engine TEXT NOT NULL,
                target_lang TEXT NOT NULL,
                source_chars INTEGER NOT NULL DEFAULT 0,
                translated_chars INTEGER NOT NULL DEFAULT 0,
                token_count INTEGER NOT NULL DEFAULT 0,
                request_count INTEGER NOT NULL DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_auto_translate_usage_month
                ON auto_translate_usage(month, engine, target_lang);

            CREATE TABLE IF NOT EXISTS exp_radio_submission_bans (
                user_id INTEGER PRIMARY KEY,
                user_name TEXT NOT NULL,
                display_name TEXT NOT NULL,
                streams_remaining INTEGER NOT NULL DEFAULT 1,
                created_at REAL DEFAULT (unixepoch()),
                updated_at REAL DEFAULT (unixepoch()),
                created_by TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_exp_radio_submission_bans_remaining
                ON exp_radio_submission_bans(streams_remaining);
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

        # Add permissions column to web_users if missing
        async with self.db.execute("PRAGMA table_info(web_users)") as cursor:
            wu_columns = [row[1] async for row in cursor]
        if "permissions" not in wu_columns:
            await self.db.execute("ALTER TABLE web_users ADD COLUMN permissions TEXT DEFAULT '[]'")
            await self.db.commit()

        # Existing installations may have an explicit sidebar allow-list.
        # Add the new module once, then leave future visibility changes to
        # the normal Settings UI.
        migration_key = "migration_sidebar_submission_bans_v1"
        async with self.db.execute(
            "SELECT value FROM settings WHERE key = ?", (migration_key,)
        ) as cursor:
            sidebar_migrated = await cursor.fetchone()
        if not sidebar_migrated:
            async with self.db.execute(
                "SELECT value FROM settings WHERE key = 'sidebar_visible_items'"
            ) as cursor:
                sidebar_row = await cursor.fetchone()
            if sidebar_row and sidebar_row[0] and sidebar_row[0] != "__none__":
                visible_items = [item for item in sidebar_row[0].split(",") if item]
                if "submission_bans" not in visible_items:
                    visible_items.append("submission_bans")
                    await self.db.execute(
                        "UPDATE settings SET value = ? WHERE key = 'sidebar_visible_items'",
                        (",".join(visible_items),),
                    )
            await self.db.execute(
                "INSERT INTO settings (key, value) VALUES (?, 'done')",
                (migration_key,),
            )
            await self.db.commit()

        card_sidebar_migration = "migration_sidebar_card_collection_v1"
        async with self.db.execute(
            "SELECT value FROM settings WHERE key = ?", (card_sidebar_migration,)
        ) as cursor:
            card_sidebar_migrated = await cursor.fetchone()
        if not card_sidebar_migrated:
            async with self.db.execute(
                "SELECT value FROM settings WHERE key = 'sidebar_visible_items'"
            ) as cursor:
                sidebar_row = await cursor.fetchone()
            if sidebar_row and sidebar_row[0] and sidebar_row[0] != "__none__":
                visible_items = [item for item in sidebar_row[0].split(",") if item]
                if "card_collection" not in visible_items:
                    visible_items.append("card_collection")
                    await self.db.execute(
                        "UPDATE settings SET value = ? WHERE key = 'sidebar_visible_items'",
                        (",".join(visible_items),),
                    )
            await self.db.execute(
                "INSERT INTO settings (key, value) VALUES (?, 'done')",
                (card_sidebar_migration,),
            )
            await self.db.commit()

        # Add message_id column to song_posts if missing
        async with self.db.execute("PRAGMA table_info(song_posts)") as cursor:
            sp_columns = [row[1] async for row in cursor]
        if "message_id" not in sp_columns:
            await self.db.execute("ALTER TABLE song_posts ADD COLUMN message_id INTEGER")
            await self.db.commit()
        if "song_title" not in sp_columns:
            await self.db.execute("ALTER TABLE song_posts ADD COLUMN song_title TEXT")
            await self.db.commit()

        async with self.db.execute("PRAGMA table_info(auto_translate_usage)") as cursor:
            atu_columns = [row[1] async for row in cursor]
        if "day" not in atu_columns:
            await self.db.execute("ALTER TABLE auto_translate_usage ADD COLUMN day TEXT")
            await self.db.commit()
        if "token_count" not in atu_columns:
            await self.db.execute("ALTER TABLE auto_translate_usage ADD COLUMN token_count INTEGER NOT NULL DEFAULT 0")
            await self.db.commit()
        await self.db.execute("""
            CREATE INDEX IF NOT EXISTS idx_auto_translate_usage_day
                ON auto_translate_usage(day, engine)
        """)
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
                song_title TEXT,
                UNIQUE(message_id, reactor_user_id, emoji)
            );

            CREATE TABLE IF NOT EXISTS player_discord_connections (
                web_user_id INTEGER PRIMARY KEY,
                discord_user_id INTEGER NOT NULL UNIQUE,
                discord_username TEXT NOT NULL,
                discord_display_name TEXT NOT NULL,
                discord_avatar TEXT,
                connected_at REAL DEFAULT (unixepoch()),
                updated_at REAL DEFAULT (unixepoch()),
                FOREIGN KEY (web_user_id) REFERENCES web_users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS player_song_reactions (
                message_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                web_user_id INTEGER NOT NULL,
                discord_user_id INTEGER NOT NULL,
                discord_display_name TEXT NOT NULL,
                emoji TEXT NOT NULL,
                reacted_at REAL DEFAULT (unixepoch()),
                PRIMARY KEY (message_id, discord_user_id, emoji),
                FOREIGN KEY (web_user_id) REFERENCES web_users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS public_player_song_reactions (
                message_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                discord_user_id INTEGER NOT NULL,
                discord_display_name TEXT NOT NULL,
                emoji TEXT NOT NULL,
                reacted_at REAL DEFAULT (unixepoch()),
                PRIMARY KEY (message_id, discord_user_id, emoji)
            );

            CREATE TABLE IF NOT EXISTS player_reaction_threads (
                message_id INTEGER PRIMARY KEY,
                channel_id INTEGER NOT NULL,
                thread_id INTEGER NOT NULL,
                summary_message_id INTEGER,
                created_at REAL DEFAULT (unixepoch()),
                updated_at REAL DEFAULT (unixepoch())
            );

            CREATE INDEX IF NOT EXISTS idx_player_song_reactions_message
                ON player_song_reactions(message_id, reacted_at);
            CREATE INDEX IF NOT EXISTS idx_public_player_song_reactions_message
                ON public_player_song_reactions(message_id, reacted_at);
        """)
        await self.db.commit()

        # Add song_title column to song_reactions if missing
        async with self.db.execute("PRAGMA table_info(song_reactions)") as cursor:
            sr_columns = [row[1] async for row in cursor]
        if "song_title" not in sr_columns:
            await self.db.execute("ALTER TABLE song_reactions ADD COLUMN song_title TEXT")
            await self.db.commit()

        # Create party_playlist table
        await self.db.executescript("""
            CREATE TABLE IF NOT EXISTS party_playlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                user_name TEXT,
                url TEXT NOT NULL,
                song_title TEXT,
                submitted_at REAL DEFAULT (unixepoch()),
                heard INTEGER DEFAULT 0,
                image_url TEXT,
                duration_seconds REAL
            );
        """)
        await self.db.commit()

        # Historical playlists built by the Experimental Radio stream manager.
        await self.db.executescript("""
            CREATE TABLE IF NOT EXISTS exp_radio_playlist_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at REAL NOT NULL,
                source TEXT NOT NULL DEFAULT '',
                scheduled INTEGER NOT NULL DEFAULT 0,
                song_count INTEGER NOT NULL DEFAULT 0,
                urls_json TEXT NOT NULL DEFAULT '[]'
            );
            CREATE INDEX IF NOT EXISTS idx_exp_radio_playlist_snapshots_created
                ON exp_radio_playlist_snapshots(created_at DESC);
        """)
        await self.db.commit()

        # Preserve the currently available legacy snapshots before their
        # settings are overwritten by the next stream start.
        async with self.db.execute(
            "SELECT value FROM settings WHERE key = ?",
            ("migration_exp_radio_playlist_snapshots_v1",),
        ) as cursor:
            snapshots_migrated = await cursor.fetchone()
        if not snapshots_migrated:
            import json

            migrated_identities = set()
            for legacy_key in (
                "exp_radio_last_scheduled_playlist_snapshot",
                "exp_radio_last_playlist_snapshot",
            ):
                async with self.db.execute(
                    "SELECT value FROM settings WHERE key = ?", (legacy_key,)
                ) as cursor:
                    row = await cursor.fetchone()
                if not row or not row[0]:
                    continue
                try:
                    payload = json.loads(row[0])
                    urls = tuple(
                        str(url).strip()
                        for url in (payload.get("urls") or [])
                        if url and str(url).strip()
                    )
                    identity = (
                        int(payload.get("created_at") or 0),
                        str(payload.get("source") or ""),
                        bool(payload.get("scheduled")),
                        urls,
                    )
                except (TypeError, ValueError):
                    continue
                if not urls or identity in migrated_identities:
                    continue
                await self.db.execute(
                    """
                    INSERT INTO exp_radio_playlist_snapshots
                        (created_at, source, scheduled, song_count, urls_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        identity[0], identity[1], 1 if identity[2] else 0,
                        len(urls), json.dumps(list(urls)),
                    ),
                )
                migrated_identities.add(identity)
            await self.db.execute(
                "INSERT INTO settings (key, value) VALUES (?, '1')",
                ("migration_exp_radio_playlist_snapshots_v1",),
            )
            await self.db.commit()

        # Add image_url column to party_playlist if missing
        async with self.db.execute("PRAGMA table_info(party_playlist)") as cursor:
            pp_columns = [row[1] async for row in cursor]
        if "image_url" not in pp_columns:
            await self.db.execute("ALTER TABLE party_playlist ADD COLUMN image_url TEXT")
            await self.db.commit()
        if "duration_seconds" not in pp_columns:
            await self.db.execute("ALTER TABLE party_playlist ADD COLUMN duration_seconds REAL")
            await self.db.commit()

        # Create polls table
        await self.db.executescript("""
            CREATE TABLE IF NOT EXISTS polls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT,
                options TEXT NOT NULL DEFAULT '[]',
                image_filename TEXT,
                channel_id INTEGER,
                message_id INTEGER,
                created_at REAL DEFAULT (unixepoch()),
                active INTEGER DEFAULT 1,
                creator_id INTEGER,
                creator_name TEXT
            );

            CREATE TABLE IF NOT EXISTS quiz_questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                mode TEXT NOT NULL,
                question TEXT NOT NULL,
                answer_1 TEXT NOT NULL,
                answer_2 TEXT NOT NULL,
                answer_3 TEXT NOT NULL,
                answer_4 TEXT NOT NULL,
                answer_5 TEXT NOT NULL,
                correct_answer TEXT NOT NULL,
                created_at REAL DEFAULT (unixepoch()),
                updated_at REAL DEFAULT (unixepoch())
            );
            CREATE INDEX IF NOT EXISTS idx_quiz_questions_mode
                ON quiz_questions(mode);

            CREATE TABLE IF NOT EXISTS quiz_categories (
                key TEXT PRIMARY KEY,
                name TEXT NOT NULL UNIQUE,
                created_at REAL DEFAULT (unixepoch())
            );

            CREATE TABLE IF NOT EXISTS quiz_scores (
                user_id INTEGER PRIMARY KEY,
                user_name TEXT NOT NULL,
                points INTEGER NOT NULL DEFAULT 0,
                last_solved_at REAL DEFAULT (unixepoch())
            );
            CREATE INDEX IF NOT EXISTS idx_quiz_scores_points
                ON quiz_scores(points DESC, last_solved_at ASC);
        """)
        await self.db.commit()

        # Older databases restricted quiz modes to film/music with a CHECK.
        async with self.db.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'quiz_questions'"
        ) as cursor:
            quiz_table = await cursor.fetchone()
        quiz_sql = (quiz_table["sql"] if quiz_table else "") or ""
        if "CHECK" in quiz_sql.upper() and "MODE" in quiz_sql.upper():
            await self.db.executescript("""
                ALTER TABLE quiz_questions RENAME TO quiz_questions_legacy;
                CREATE TABLE quiz_questions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mode TEXT NOT NULL,
                    question TEXT NOT NULL,
                    answer_1 TEXT NOT NULL,
                    answer_2 TEXT NOT NULL,
                    answer_3 TEXT NOT NULL,
                    answer_4 TEXT NOT NULL,
                    answer_5 TEXT NOT NULL,
                    correct_answer TEXT NOT NULL,
                    created_at REAL DEFAULT (unixepoch()),
                    updated_at REAL DEFAULT (unixepoch())
                );
                INSERT INTO quiz_questions
                  (id, mode, question, answer_1, answer_2, answer_3,
                   answer_4, answer_5, correct_answer, created_at, updated_at)
                SELECT id, mode, question, answer_1, answer_2, answer_3,
                       answer_4, answer_5, correct_answer, created_at, updated_at
                FROM quiz_questions_legacy;
                DROP TABLE quiz_questions_legacy;
                CREATE INDEX IF NOT EXISTS idx_quiz_questions_mode
                    ON quiz_questions(mode);
            """)
            await self.db.commit()

        async with self.db.execute(
            "SELECT value FROM settings WHERE key = 'quiz_categories_seeded'"
        ) as cursor:
            categories_seeded = await cursor.fetchone()
        if not categories_seeded:
            await self.db.execute(
                "INSERT OR IGNORE INTO quiz_categories (key, name) VALUES ('film', 'Film')"
            )
            await self.db.execute(
                "INSERT OR IGNORE INTO quiz_categories (key, name) VALUES ('music', 'Music')"
            )
            await self.db.execute("""
                INSERT OR IGNORE INTO quiz_categories (key, name)
                SELECT DISTINCT mode,
                       UPPER(SUBSTR(mode, 1, 1)) || SUBSTR(mode, 2)
                FROM quiz_questions
                WHERE TRIM(mode) != ''
            """)
            await self.db.execute(
                "INSERT INTO settings (key, value) VALUES ('quiz_categories_seeded', '1')"
            )
            await self.db.commit()

        # Add creator_id column to polls if missing
        async with self.db.execute("PRAGMA table_info(polls)") as cursor:
            poll_columns = [row[1] async for row in cursor]
        if "creator_id" not in poll_columns:
            await self.db.execute("ALTER TABLE polls ADD COLUMN creator_id INTEGER")
            await self.db.commit()
        if "creator_name" not in poll_columns:
            await self.db.execute("ALTER TABLE polls ADD COLUMN creator_name TEXT")
            await self.db.commit()

        # Create radio_songs table
        await self.db.executescript("""
            CREATE TABLE IF NOT EXISTS radio_songs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                artist TEXT NOT NULL,
                suno_url TEXT,
                filename TEXT NOT NULL,
                original_filename TEXT,
                file_size INTEGER,
                duration REAL,
                bitrate INTEGER,
                uploaded_by_ip TEXT,
                uploaded_at REAL DEFAULT (unixepoch()),
                expires_at REAL,
                rights_declaration TEXT NOT NULL,
                rights_hash TEXT NOT NULL,
                rights_agreed_at REAL,
                position INTEGER DEFAULT 0,
                active INTEGER DEFAULT 1
            );
        """)
        await self.db.commit()

        # Create welcome_config table
        await self.db.executescript("""
            CREATE TABLE IF NOT EXISTS welcome_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                enabled INTEGER DEFAULT 0,
                channel_id INTEGER,
                message_text TEXT DEFAULT '🎉 Welcome {user} to our server!',
                dm_enabled INTEGER DEFAULT 0,
                dm_text TEXT DEFAULT 'Welcome to our server, {user}!',
                created_at REAL DEFAULT (unixepoch()),
                updated_at REAL DEFAULT (unixepoch())
            );
            INSERT OR IGNORE INTO welcome_config (id) VALUES (1);
        """)
        await self.db.commit()

        # Create image_categories and image_posts tables
        await self.db.executescript("""
            CREATE TABLE IF NOT EXISTS image_categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS image_posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT,
                category_id INTEGER NOT NULL,
                filename TEXT NOT NULL,
                uploaded_at REAL DEFAULT (unixepoch()),
                FOREIGN KEY (category_id) REFERENCES image_categories(id)
            );
        """)
        await self.db.commit()

        # Create suno_userlist table — per web user "todo list" of Suno creators
        await self.db.executescript("""
            CREATE TABLE IF NOT EXISTS suno_userlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_user_id INTEGER NOT NULL,
                profile_url TEXT NOT NULL,
                handle TEXT NOT NULL,
                display_name TEXT,
                avatar_url TEXT,
                priority TEXT NOT NULL DEFAULT 'medium',
                done INTEGER NOT NULL DEFAULT 0,
                paused INTEGER NOT NULL DEFAULT 0,
                last_song_url TEXT,
                last_song_title TEXT,
                pinned_song_url TEXT,
                pinned_song_title TEXT,
                latest_song_url TEXT,
                latest_song_title TEXT,
                last_fetched_at REAL,
                added_at REAL DEFAULT (unixepoch()),
                UNIQUE(owner_user_id, handle)
            );
            CREATE INDEX IF NOT EXISTS idx_suno_userlist_owner ON suno_userlist(owner_user_id);
        """)
        await self.db.commit()

        # Migrate suno_userlist: add pinned/latest song columns if missing
        async with self.db.execute("PRAGMA table_info(suno_userlist)") as cur:
            sul_cols = {row["name"] for row in await cur.fetchall()}
        if "pinned_song_url" not in sul_cols:
            await self.db.execute("ALTER TABLE suno_userlist ADD COLUMN pinned_song_url TEXT")
            await self.db.execute("ALTER TABLE suno_userlist ADD COLUMN pinned_song_title TEXT")
            await self.db.execute("ALTER TABLE suno_userlist ADD COLUMN latest_song_url TEXT")
            await self.db.execute("ALTER TABLE suno_userlist ADD COLUMN latest_song_title TEXT")
            await self.db.commit()

        # Create suno_playlists table for radio Suno playlist sources
        await self.db.executescript("""
            CREATE TABLE IF NOT EXISTS suno_playlists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL DEFAULT '',
                created_at REAL DEFAULT (unixepoch())
            );
        """)
        await self.db.commit()

        # --- LLM / Corax chat ---
        await self.db.executescript("""
            CREATE TABLE IF NOT EXISTS llm_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                enabled INTEGER DEFAULT 0,
                model TEXT DEFAULT 'qwen2.5:7b-instruct',
                tools_model TEXT DEFAULT '',
                persona TEXT DEFAULT '',
                retention_days INTEGER DEFAULT 30,
                rate_per_user_min INTEGER DEFAULT 3,
                rate_per_channel_min INTEGER DEFAULT 10,
                max_tokens INTEGER DEFAULT 512,
                tools_enabled TEXT DEFAULT '[]',
                default_result_limit INTEGER DEFAULT 10,
                updated_at REAL DEFAULT (unixepoch())
            );
            INSERT OR IGNORE INTO llm_config (id) VALUES (1);

            CREATE TABLE IF NOT EXISTS llm_allowed_channels (
                channel_id INTEGER PRIMARY KEY,
                channel_name TEXT,
                added_at REAL DEFAULT (unixepoch())
            );

            CREATE TABLE IF NOT EXISTS llm_allowed_roles (
                role_id INTEGER PRIMARY KEY,
                role_name TEXT,
                added_at REAL DEFAULT (unixepoch())
            );

            CREATE TABLE IF NOT EXISTS llm_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL DEFAULT (unixepoch()),
                user_id INTEGER,
                user_name TEXT,
                channel_id INTEGER,
                prompt TEXT,
                response TEXT,
                tools_used TEXT,
                error TEXT,
                latency_ms INTEGER,
                blocked INTEGER DEFAULT 0,
                block_reason TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_llm_audit_ts ON llm_audit_log(timestamp);
            CREATE INDEX IF NOT EXISTS idx_llm_audit_user ON llm_audit_log(user_id);

            CREATE TABLE IF NOT EXISTS reaction_roles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                role_name TEXT NOT NULL DEFAULT '',
                emoji TEXT NOT NULL,
                emoji_id INTEGER,
                content TEXT NOT NULL DEFAULT '',
                all_message_ids TEXT NOT NULL DEFAULT '',
                created_at REAL DEFAULT (unixepoch()),
                UNIQUE(message_id, emoji)
            );
            CREATE INDEX IF NOT EXISTS idx_reaction_roles_msg
                ON reaction_roles(message_id);
        """)
        await self.db.commit()

        # Migrate reaction_roles: add all_message_ids if missing.
        async with self.db.execute("PRAGMA table_info(reaction_roles)") as cur:
            rr_cols = {row["name"] for row in await cur.fetchall()}
        if "all_message_ids" not in rr_cols:
            await self.db.execute(
                "ALTER TABLE reaction_roles ADD COLUMN all_message_ids TEXT NOT NULL DEFAULT ''"
            )
            await self.db.commit()

        # Migrate older llm_config schemas (add tools_model if missing).
        async with self.db.execute("PRAGMA table_info(llm_config)") as cur:
            cols = {row["name"] for row in await cur.fetchall()}
        if "tools_model" not in cols:
            await self.db.execute(
                "ALTER TABLE llm_config ADD COLUMN tools_model TEXT DEFAULT ''"
            )
            await self.db.commit()

        # One-time migration: bump prior small-model defaults to qwen2.5:7b
        # and clear the now-unused tools model. Only touches rows that still
        # match the previous defaults exactly, so custom configs are preserved.
        async with self.db.execute(
            "SELECT value FROM settings WHERE key = 'llm_migration_v2'"
        ) as cur:
            done = await cur.fetchone()
        if not done:
            await self.db.execute(
                "UPDATE llm_config SET model = 'qwen2.5:7b-instruct' "
                "WHERE id = 1 AND (model IS NULL OR model = '' OR model = 'gemma3:4b')"
            )
            await self.db.execute(
                "UPDATE llm_config SET tools_model = '' "
                "WHERE id = 1 AND tools_model = 'qwen2.5:3b'"
            )
            await self.db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('llm_migration_v2', '1')"
            )
            await self.db.commit()

        # Create user_preferences table for per-user UI settings
        await self.db.executescript("""
            CREATE TABLE IF NOT EXISTS user_preferences (
                user_id INTEGER PRIMARY KEY,
                suno_player_split REAL DEFAULT 0.55,
                updated_at REAL DEFAULT (unixepoch())
            );
        """)
        # Create exp_radio_songs table
        await self.db.executescript("""
            CREATE TABLE IF NOT EXISTS exp_radio_songs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                user_name TEXT NOT NULL,
                suno_url TEXT NOT NULL,
                suno_uuid TEXT NOT NULL,
                mp3_filename TEXT,
                cover_url TEXT,
                video_url TEXT,
                hook_id TEXT,
                hook_share_url TEXT,
                hook_video_url TEXT,
                title TEXT,
                artist TEXT,
                duration REAL,
                lyrics TEXT,
                word_timestamps TEXT DEFAULT '[]',
                ass_filename TEXT,
                analysis_status TEXT DEFAULT 'pending',
                rights_declaration TEXT NOT NULL,
                rights_hash TEXT NOT NULL,
                rights_agreed_at REAL NOT NULL,
                submitted_at REAL DEFAULT (unixepoch()),
                expires_at REAL NOT NULL,
                active INTEGER DEFAULT 1
            );
            CREATE INDEX IF NOT EXISTS idx_exp_radio_user ON exp_radio_songs(user_id);
            CREATE INDEX IF NOT EXISTS idx_exp_radio_expires ON exp_radio_songs(expires_at);
        """)
        await self.db.commit()

        # Add upload_token to exp_radio_songs (migration for existing installs)
        try:
            await self.db.execute(
                "ALTER TABLE exp_radio_songs ADD COLUMN upload_token TEXT"
            )
            await self.db.commit()
        except Exception:
            pass  # column already exists

        # LLM-based lyric moderation columns (migration for existing installs).
        # moderation_status:
        #   NULL       — moderation was disabled when this song was processed
        #                (grandfathered; treated as passed)
        #   'pending'  — queued for moderation or LLM call failed/timed out
        #                (excluded from auto-stream; admin must approve manually)
        #   'passed'   — LLM cleared the lyrics
        #   'flagged'  — LLM raised a concern (see moderation_reason)
        #   'approved' — admin manually overrode a 'flagged' song
        for col, definition in [
            ("moderation_status", "TEXT"),
            ("moderation_reason", "TEXT"),
            ("moderation_at",     "REAL"),
        ]:
            try:
                await self.db.execute(
                    f"ALTER TABLE exp_radio_songs ADD COLUMN {col} {definition}"
                )
                await self.db.commit()
            except Exception:
                pass  # column already exists

        # Add playlist_source column (migration for existing installs).
        # 'submission' = user-submitted via /twitch-submit (default)
        # 'admin'      = added directly in admin UI
        try:
            await self.db.execute(
                "ALTER TABLE exp_radio_songs ADD COLUMN playlist_source TEXT NOT NULL DEFAULT 'submission'"
            )
            await self.db.commit()
        except Exception:
            pass  # column already exists

        # Optional per-song Suno Hook override for the stream video inset.
        for col, definition in [
            ("hook_id", "TEXT"),
            ("hook_share_url", "TEXT"),
            ("hook_video_url", "TEXT"),
        ]:
            try:
                await self.db.execute(
                    f"ALTER TABLE exp_radio_songs ADD COLUMN {col} {definition}"
                )
                await self.db.commit()
            except Exception:
                pass  # column already exists

        # Add DC-player filter columns (migration for existing installs)
        for col, definition in [
            ("dc_channel", "TEXT DEFAULT ''"),
            ("dc_limit",   "INTEGER DEFAULT 15"),
            ("dc_days",    "INTEGER DEFAULT 1"),
        ]:
            try:
                await self.db.execute(
                    f"ALTER TABLE user_preferences ADD COLUMN {col} {definition}"
                )
                await self.db.commit()
            except Exception:
                pass  # column already exists

        # Channel-moderation audit log: one row per (message, suno_url) that
        # has been LLM-screened. Used both for dedup and for the admin UI's
        # recent-verdicts table. Verdict is one of 'flagged' / 'passed' /
        # 'pending' / 'skipped' / 'error'.
        await self.db.executescript("""
            CREATE TABLE IF NOT EXISTS channel_moderation_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                channel_name TEXT,
                user_id INTEGER,
                user_name TEXT,
                suno_url TEXT NOT NULL,
                title TEXT,
                artist TEXT,
                verdict TEXT NOT NULL,
                reason TEXT,
                created_at REAL DEFAULT (unixepoch()),
                UNIQUE(message_id, suno_url)
            );
            CREATE INDEX IF NOT EXISTS idx_chmod_log_ts
                ON channel_moderation_log(created_at);
            CREATE INDEX IF NOT EXISTS idx_chmod_log_verdict
                ON channel_moderation_log(verdict);
        """)
        await self.db.commit()

        await self.db.executescript("""
            CREATE TABLE IF NOT EXISTS user_activity (
                user_id INTEGER PRIMARY KEY,
                user_name TEXT,
                activity_type TEXT NOT NULL,
                summary TEXT,
                channel_id INTEGER,
                channel_name TEXT,
                timestamp REAL NOT NULL
            );
        """)
        await self.db.commit()

        # Collectible cards. Stats and abilities are stored from the start so a
        # later deck/duel system can build on the collection without a migration.
        await self.db.executescript("""
            CREATE TABLE IF NOT EXISTS collectible_cards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                subtitle TEXT DEFAULT '',
                description TEXT DEFAULT '',
                quote TEXT DEFAULT '',
                rarity TEXT NOT NULL DEFAULT 'Common',
                draw_weight REAL NOT NULL DEFAULT 1.0,
                series TEXT DEFAULT '',
                card_number TEXT DEFAULT '',
                hero_type TEXT DEFAULT '',
                strength INTEGER NOT NULL DEFAULT 0,
                agility INTEGER NOT NULL DEFAULT 0,
                endurance INTEGER NOT NULL DEFAULT 0,
                charisma INTEGER NOT NULL DEFAULT 0,
                luck INTEGER NOT NULL DEFAULT 0,
                attack INTEGER NOT NULL DEFAULT 0,
                defense INTEGER NOT NULL DEFAULT 0,
                passive_name TEXT DEFAULT '',
                passive_text TEXT DEFAULT '',
                special_name TEXT DEFAULT '',
                special_text TEXT DEFAULT '',
                bonus_text TEXT DEFAULT '',
                image_filename TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at REAL DEFAULT (unixepoch()),
                updated_at REAL DEFAULT (unixepoch())
            );

            CREATE TABLE IF NOT EXISTS collectible_user_cards (
                user_id INTEGER NOT NULL,
                card_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL DEFAULT 1,
                first_obtained_at REAL DEFAULT (unixepoch()),
                last_obtained_at REAL DEFAULT (unixepoch()),
                PRIMARY KEY (user_id, card_id),
                FOREIGN KEY (card_id) REFERENCES collectible_cards(id)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS collectible_card_draws (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                user_name TEXT NOT NULL,
                card_id INTEGER NOT NULL,
                draw_date TEXT NOT NULL,
                drawn_at REAL DEFAULT (unixepoch()),
                UNIQUE (user_id, draw_date),
                FOREIGN KEY (card_id) REFERENCES collectible_cards(id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_collectible_cards_active
                ON collectible_cards(active);
            CREATE INDEX IF NOT EXISTS idx_collectible_user_cards_user
                ON collectible_user_cards(user_id);
            CREATE INDEX IF NOT EXISTS idx_collectible_draws_card
                ON collectible_card_draws(card_id);
        """)
        await self.db.commit()

    # --- Collectible Cards ---

    async def get_collectible_cards(self, *, include_inactive: bool = True) -> list[dict]:
        where = "" if include_inactive else "WHERE c.active = 1"
        async with self.db.execute(
            f"""
            SELECT c.*,
                   (SELECT COALESCE(SUM(uc.quantity), 0)
                    FROM collectible_user_cards uc
                    WHERE uc.card_id = c.id) AS total_owned,
                   (SELECT COUNT(*)
                    FROM collectible_user_cards uc
                    WHERE uc.card_id = c.id) AS unique_owners,
                   (SELECT COUNT(*)
                    FROM collectible_card_draws d
                    WHERE d.card_id = c.id) AS total_draws
            FROM collectible_cards c
            {where}
            ORDER BY c.active DESC, c.name COLLATE NOCASE
            """
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def get_collectible_card(self, card_id: int) -> Optional[dict]:
        async with self.db.execute(
            "SELECT * FROM collectible_cards WHERE id = ?", (int(card_id),)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def save_collectible_card(self, card_id: Optional[int] = None, **fields) -> int:
        allowed = {
            "name", "subtitle", "description", "quote", "rarity", "draw_weight",
            "series", "card_number", "hero_type", "strength", "agility",
            "endurance", "charisma", "luck", "attack", "defense",
            "passive_name", "passive_text", "special_name", "special_text",
            "bonus_text", "image_filename", "active",
        }
        clean = {key: value for key, value in fields.items() if key in allowed}
        if card_id:
            if not clean:
                return int(card_id)
            columns = ", ".join(f"{key} = ?" for key in clean)
            await self.db.execute(
                f"UPDATE collectible_cards SET {columns}, updated_at = unixepoch() WHERE id = ?",
                (*clean.values(), int(card_id)),
            )
            await self.db.commit()
            return int(card_id)

        cursor = await self.db.execute(
            f"INSERT INTO collectible_cards ({', '.join(clean)}) "
            f"VALUES ({', '.join('?' for _ in clean)})",
            tuple(clean.values()),
        )
        await self.db.commit()
        return int(cursor.lastrowid)

    async def delete_collectible_card(self, card_id: int) -> Optional[str]:
        card = await self.get_collectible_card(card_id)
        if not card:
            return None
        await self.db.execute("DELETE FROM collectible_cards WHERE id = ?", (int(card_id),))
        await self.db.commit()
        return card.get("image_filename")

    async def draw_collectible_card(
        self, *, user_id: int, user_name: str, draw_date: str
    ) -> tuple[Optional[dict], bool]:
        """Draw one weighted active card. Returns (card, already_drawn_today)."""
        async with self.db.execute(
            """
            SELECT c.*
            FROM collectible_card_draws d
            JOIN collectible_cards c ON c.id = d.card_id
            WHERE d.user_id = ? AND d.draw_date = ?
            """,
            (int(user_id), draw_date),
        ) as cursor:
            existing = await cursor.fetchone()
        if existing:
            return dict(existing), True

        async with self.db.execute(
            "SELECT * FROM collectible_cards WHERE active = 1 ORDER BY id"
        ) as cursor:
            cards = [dict(row) for row in await cursor.fetchall()]
        if not cards:
            return None, False

        weights = [max(0.0, float(card.get("draw_weight") or 0)) for card in cards]
        if not any(weights):
            weights = [1.0] * len(cards)
        card = random.choices(cards, weights=weights, k=1)[0]

        try:
            await self.db.execute(
                """
                INSERT INTO collectible_card_draws
                    (user_id, user_name, card_id, draw_date)
                VALUES (?, ?, ?, ?)
                """,
                (int(user_id), user_name, int(card["id"]), draw_date),
            )
            await self.db.execute(
                """
                INSERT INTO collectible_user_cards (user_id, card_id, quantity)
                VALUES (?, ?, 1)
                ON CONFLICT(user_id, card_id) DO UPDATE SET
                    quantity = quantity + 1,
                    last_obtained_at = unixepoch()
                """,
                (int(user_id), int(card["id"])),
            )
            await self.db.commit()
        except aiosqlite.IntegrityError:
            await self.db.rollback()
            async with self.db.execute(
                """
                SELECT c.*
                FROM collectible_card_draws d
                JOIN collectible_cards c ON c.id = d.card_id
                WHERE d.user_id = ? AND d.draw_date = ?
                """,
                (int(user_id), draw_date),
            ) as cursor:
                existing = await cursor.fetchone()
            return (dict(existing) if existing else None), True
        return card, False

    async def get_collectible_user_collection(self, user_id: int) -> list[dict]:
        async with self.db.execute(
            """
            SELECT c.*, uc.quantity, uc.first_obtained_at, uc.last_obtained_at
            FROM collectible_user_cards uc
            JOIN collectible_cards c ON c.id = uc.card_id
            WHERE uc.user_id = ?
            ORDER BY
                CASE c.rarity
                    WHEN 'Legendary' THEN 5 WHEN 'Epic' THEN 4 WHEN 'Rare' THEN 3
                    WHEN 'Uncommon' THEN 2 ELSE 1
                END DESC,
                c.name COLLATE NOCASE
            """,
            (int(user_id),),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def get_collectible_user_stats(self, user_id: int) -> dict:
        async with self.db.execute(
            """
            SELECT COUNT(*) AS unique_cards, COALESCE(SUM(quantity), 0) AS total_cards
            FROM collectible_user_cards WHERE user_id = ?
            """,
            (int(user_id),),
        ) as cursor:
            owned = await cursor.fetchone()
        async with self.db.execute(
            "SELECT COUNT(*) AS available_cards FROM collectible_cards WHERE active = 1"
        ) as cursor:
            available = await cursor.fetchone()
        return {
            "unique_cards": int(owned["unique_cards"] or 0),
            "total_cards": int(owned["total_cards"] or 0),
            "available_cards": int(available["available_cards"] or 0),
        }

    # --- Channel Moderation ---

    async def has_channel_moderation_check(self, message_id: int, suno_url: str) -> bool:
        async with self.db.execute(
            "SELECT 1 FROM channel_moderation_log "
            "WHERE message_id = ? AND suno_url = ? LIMIT 1",
            (message_id, suno_url),
        ) as cur:
            return await cur.fetchone() is not None

    async def add_channel_moderation_log(
        self, *, message_id: int, channel_id: int, channel_name: str,
        user_id: int, user_name: str, suno_url: str,
        title: str = "", artist: str = "",
        verdict: str = "pending", reason: str = "",
    ):
        await self.db.execute(
            "INSERT OR REPLACE INTO channel_moderation_log "
            "(message_id, channel_id, channel_name, user_id, user_name, "
            " suno_url, title, artist, verdict, reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (message_id, channel_id, channel_name, user_id, user_name,
             suno_url, title, artist, verdict, reason),
        )
        await self.db.commit()

    async def get_channel_moderation_log(
        self, limit: int = 100, verdict: str | None = None,
    ) -> list[dict]:
        if verdict:
            q = ("SELECT * FROM channel_moderation_log WHERE verdict = ? "
                 "ORDER BY created_at DESC LIMIT ?")
            args = (verdict, limit)
        else:
            q = ("SELECT * FROM channel_moderation_log "
                 "ORDER BY created_at DESC LIMIT ?")
            args = (limit,)
        async with self.db.execute(q, args) as cur:
            return [dict(r) for r in await cur.fetchall()]

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

    async def save_exp_radio_playlist_snapshot(
        self,
        *,
        created_at: float,
        source: str,
        scheduled: bool,
        urls: list[str],
    ) -> int:
        import json

        clean_urls = [str(url).strip() for url in urls if url and str(url).strip()]
        cursor = await self.db.execute(
            """
            INSERT INTO exp_radio_playlist_snapshots
                (created_at, source, scheduled, song_count, urls_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                float(created_at),
                str(source or ""),
                1 if scheduled else 0,
                len(clean_urls),
                json.dumps(clean_urls),
            ),
        )
        await self.db.commit()
        return int(cursor.lastrowid)

    async def get_exp_radio_playlist_snapshots(self, limit: int = 50) -> list[dict]:
        limit = max(1, min(int(limit), 200))
        async with self.db.execute(
            """
            SELECT id, created_at, source, scheduled, song_count
            FROM exp_radio_playlist_snapshots
            ORDER BY created_at DESC, id DESC
            LIMIT ?
            """,
            (limit,),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def get_exp_radio_playlist_snapshot(self, snapshot_id: int) -> Optional[dict]:
        import json

        async with self.db.execute(
            """
            SELECT id, created_at, source, scheduled, song_count, urls_json
            FROM exp_radio_playlist_snapshots
            WHERE id = ?
            """,
            (int(snapshot_id),),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None
        snapshot = dict(row)
        try:
            snapshot["urls"] = json.loads(snapshot.pop("urls_json") or "[]")
        except (TypeError, ValueError):
            snapshot["urls"] = []
        return snapshot

    async def add_auto_translate_usage(
        self,
        *,
        engine: str,
        target_lang: str,
        source_chars: int,
        translated_chars: int,
        token_count: int = 0,
    ) -> None:
        now = time.time()
        day = time.strftime("%Y-%m-%d", time.localtime(now))
        month = time.strftime("%Y-%m", time.localtime(now))
        await self.db.execute("""
            INSERT INTO auto_translate_usage
                (created_at, day, month, engine, target_lang, source_chars,
                 translated_chars, token_count, request_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
        """, (
            now,
            day,
            month,
            engine,
            target_lang,
            max(0, int(source_chars or 0)),
            max(0, int(translated_chars or 0)),
            max(0, int(token_count or 0)),
        ))
        await self.db.commit()

    async def get_auto_translate_daily_tokens(
        self,
        engine: str,
        day: str | None = None,
    ) -> int:
        if not day:
            day = time.strftime("%Y-%m-%d", time.localtime(time.time()))
        async with self.db.execute("""
            SELECT COALESCE(SUM(token_count), 0) AS tokens
            FROM auto_translate_usage
            WHERE day = ? AND engine = ?
        """, (day, engine)) as cur:
            row = await cur.fetchone()
            return int(row["tokens"] or 0) if row else 0

    async def get_auto_translate_monthly_usage(self, limit: int = 18) -> list[dict]:
        async with self.db.execute("""
            SELECT
                month,
                engine,
                target_lang,
                SUM(source_chars) AS source_chars,
                SUM(translated_chars) AS translated_chars,
                SUM(token_count) AS tokens,
                SUM(request_count) AS requests
            FROM auto_translate_usage
            GROUP BY month, engine, target_lang
            ORDER BY month DESC, engine ASC, target_lang ASC
            LIMIT ?
        """, (limit,)) as cur:
            return [dict(r) for r in await cur.fetchall()]

    # --- Welcome Config ---

    async def get_welcome_config(self) -> dict:
        async with self.db.execute(
            "SELECT * FROM welcome_config WHERE id = 1"
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return {
                    "enabled": bool(row["enabled"]),
                    "channel_id": row["channel_id"],
                    "message_text": row["message_text"],
                    "dm_enabled": bool(row["dm_enabled"]),
                    "dm_text": row["dm_text"],
                }
            return {
                "enabled": False,
                "channel_id": None,
                "message_text": "🎉 Welcome {user} to our server!",
                "dm_enabled": False,
                "dm_text": "Welcome to our server, {user}!",
            }

    async def set_welcome_config(
        self,
        enabled: bool = None,
        channel_id: int = None,
        message_text: str = None,
        dm_enabled: bool = None,
        dm_text: str = None,
    ):
        import time
        updates = []
        params = []
        if enabled is not None:
            updates.append("enabled = ?")
            params.append(1 if enabled else 0)
        if channel_id is not None:
            updates.append("channel_id = ?")
            params.append(channel_id)
        if message_text is not None:
            updates.append("message_text = ?")
            params.append(message_text)
        if dm_enabled is not None:
            updates.append("dm_enabled = ?")
            params.append(1 if dm_enabled else 0)
        if dm_text is not None:
            updates.append("dm_text = ?")
            params.append(dm_text)
        if updates:
            updates.append("updated_at = ?")
            params.append(time.time())
            sql = f"UPDATE welcome_config SET {', '.join(updates)} WHERE id = 1"
            await self.db.execute(sql, params)
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
            "SELECT id, username, is_admin, must_change_password, permissions, created_at FROM web_users"
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
            await self.db.rollback()
            return False

    async def update_web_user_password(self, user_id: int, password_hash: str):
        await self.db.execute(
            "UPDATE web_users SET password_hash = ?, must_change_password = 0 WHERE id = ?",
            (password_hash, user_id),
        )
        await self.db.commit()

    async def set_user_permissions(self, user_id: int, permissions: list[str]):
        import json
        await self.db.execute(
            "UPDATE web_users SET permissions = ? WHERE id = ?",
            (json.dumps(permissions), user_id),
        )
        await self.db.commit()

    async def get_user_permissions(self, user_id: int) -> list[str]:
        import json
        async with self.db.execute(
            "SELECT permissions FROM web_users WHERE id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if row and row[0]:
                try:
                    return json.loads(row[0])
                except (json.JSONDecodeError, TypeError):
                    return []
            return []

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

    async def get_latest_user_activity(self, user_id: int) -> Optional[dict]:
        async def table_exists(name: str) -> bool:
            async with self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (name,),
            ) as cursor:
                return await cursor.fetchone() is not None

        queries = []
        params = []

        if await table_exists("audit_log"):
            queries.append("""
                SELECT timestamp, 'Audit Log' AS type,
                       event_type || COALESCE(': ' || details, '') AS summary
                FROM audit_log
                WHERE user_id = ?
            """)
            params.append(user_id)

        if await table_exists("user_activity"):
            queries.append("""
                SELECT timestamp, activity_type AS type,
                       COALESCE(summary, activity_type) AS summary
                FROM user_activity
                WHERE user_id = ?
            """)
            params.append(user_id)

        if await table_exists("song_posts"):
            queries.append("""
                SELECT posted_at AS timestamp, 'Song Post' AS type,
                       COALESCE(song_title, url, 'Song posted') AS summary
                FROM song_posts
                WHERE user_id = ?
            """)
            params.append(user_id)

        if await table_exists("song_reactions"):
            queries.append("""
                SELECT reacted_at AS timestamp, 'Reaction' AS type,
                       'Reacted with ' || emoji AS summary
                FROM song_reactions
                WHERE reactor_user_id = ?
            """)
            params.append(user_id)

        if await table_exists("party_playlist"):
            queries.append("""
                SELECT submitted_at AS timestamp, 'Party Playlist' AS type,
                       COALESCE(song_title, url, 'Song submitted') AS summary
                FROM party_playlist
                WHERE user_id = ?
            """)
            params.append(user_id)

        if await table_exists("quiz_scores"):
            queries.append("""
                SELECT last_solved_at AS timestamp, 'Quiz' AS type,
                       'Solved a quiz question' AS summary
                FROM quiz_scores
                WHERE user_id = ?
            """)
            params.append(user_id)

        if await table_exists("channel_moderation_log"):
            queries.append("""
                SELECT created_at AS timestamp, 'Channel Moderation' AS type,
                       verdict || COALESCE(': ' || title, '') AS summary
                FROM channel_moderation_log
                WHERE user_id = ?
            """)
            params.append(user_id)

        if await table_exists("exp_radio_songs"):
            queries.append("""
                SELECT submitted_at AS timestamp, 'Experimental Radio' AS type,
                       COALESCE(title, suno_url, 'Song submitted') AS summary
                FROM exp_radio_songs
                WHERE user_id = ?
            """)
            params.append(user_id)

        if not queries:
            return None

        sql = (
            "SELECT timestamp, type, summary FROM ("
            + " UNION ALL ".join(queries)
            + ") WHERE timestamp IS NOT NULL ORDER BY timestamp DESC LIMIT 1"
        )
        async with self.db.execute(sql, tuple(params)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def record_user_activity(
        self,
        *,
        user_id: int,
        user_name: str,
        activity_type: str,
        summary: str = "",
        channel_id: int = None,
        channel_name: str = None,
        timestamp: float = None,
    ):
        ts = timestamp if timestamp is not None else time.time()
        await self.db.execute(
            "INSERT INTO user_activity "
            "(user_id, user_name, activity_type, summary, channel_id, channel_name, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "user_name = excluded.user_name, "
            "activity_type = excluded.activity_type, "
            "summary = excluded.summary, "
            "channel_id = excluded.channel_id, "
            "channel_name = excluded.channel_name, "
            "timestamp = excluded.timestamp",
            (user_id, user_name, activity_type, summary, channel_id, channel_name, ts),
        )
        await self.db.commit()

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

    async def add_song_post(self, channel_id: int, user_id: int, user_name: str, url: str, posted_at: float, message_id: int = None, song_title: str = None):
        await self.db.execute(
            "INSERT INTO song_posts (channel_id, user_id, user_name, url, posted_at, message_id, song_title) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(channel_id, url) DO UPDATE SET "
            "message_id = COALESCE(song_posts.message_id, excluded.message_id), "
            "song_title = COALESCE(song_posts.song_title, excluded.song_title)",
            (channel_id, user_id, user_name, url, posted_at, message_id, song_title),
        )
        await self.db.commit()

    async def get_player_songs(self, channel_id: int = None, limit: int = 200, offset: int = 0) -> list[dict]:
        """Get songs for the web player with reaction counts."""
        if channel_id:
            sql = """
                SELECT sp.id, sp.url, sp.song_title, sp.user_name, sp.posted_at, sp.channel_id,
                       sp.message_id, sp.user_id,
                       COUNT(sr.id) as reaction_count
                FROM song_posts sp
                LEFT JOIN song_reactions sr ON sr.message_id = sp.message_id AND sp.message_id IS NOT NULL
                WHERE sp.channel_id = ?
                GROUP BY sp.id
                ORDER BY sp.posted_at DESC
                LIMIT ? OFFSET ?
            """
            params = (channel_id, limit, offset)
        else:
            sql = """
                SELECT sp.id, sp.url, sp.song_title, sp.user_name, sp.posted_at, sp.channel_id,
                       sp.message_id, sp.user_id,
                       COUNT(sr.id) as reaction_count
                FROM song_posts sp
                LEFT JOIN song_reactions sr ON sr.message_id = sp.message_id AND sp.message_id IS NOT NULL
                GROUP BY sp.id
                ORDER BY sp.posted_at DESC
                LIMIT ? OFFSET ?
            """
            params = (limit, offset)
        async with self.db.execute(sql, params) as cursor:
            rows = await cursor.fetchall()
            result = []
            for r in rows:
                d = dict(r)
                # Convert large IDs to strings to avoid JavaScript precision loss
                for key in ("message_id", "user_id", "channel_id"):
                    if d.get(key) is not None:
                        d[key] = str(d[key])
                result.append(d)
            return result

    async def add_song_posts_bulk(self, rows: list[tuple]):
        await self.db.executemany(
            "INSERT INTO song_posts (channel_id, user_id, user_name, url, posted_at, message_id) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(channel_id, url) DO UPDATE SET message_id = COALESCE(song_posts.message_id, excluded.message_id)",
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
            "unique_reactions": 0,
            "total_reactions": 0,
            "reactions_by_month": [],
            "reactions_by_weekday": [],
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

        # Unique reactions received (distinct reactor-song pairs)
        async with self.db.execute(
            "SELECT COUNT(*) FROM "
            "(SELECT DISTINCT reactor_user_id, message_id FROM song_reactions WHERE post_author_id = ?)",
            (user_id,),
        ) as cursor:
            stats["unique_reactions"] = (await cursor.fetchone())[0]

        # Total reactions received
        async with self.db.execute(
            "SELECT COUNT(*) FROM song_reactions WHERE post_author_id = ?",
            (user_id,),
        ) as cursor:
            stats["total_reactions"] = (await cursor.fetchone())[0]

        # Reactions by month (unique reactor-song pairs, last 12 months)
        async with self.db.execute(
            "SELECT ym, COUNT(*) as cnt FROM "
            "(SELECT DISTINCT reactor_user_id, message_id, strftime('%Y-%m', reacted_at, 'unixepoch') as ym "
            "FROM song_reactions WHERE post_author_id = ?) GROUP BY ym ORDER BY ym DESC LIMIT 12",
            (user_id,),
        ) as cursor:
            stats["reactions_by_month"] = [{"label": r[0], "count": r[1]} for r in await cursor.fetchall()]

        # Reactions by weekday (unique reactor-song pairs)
        weekday_names = ["So", "Mo", "Di", "Mi", "Do", "Fr", "Sa"]
        async with self.db.execute(
            "SELECT wd, COUNT(*) as cnt FROM "
            "(SELECT DISTINCT reactor_user_id, message_id, strftime('%w', reacted_at, 'unixepoch') as wd "
            "FROM song_reactions WHERE post_author_id = ?) GROUP BY wd ORDER BY wd",
            (user_id,),
        ) as cursor:
            stats["reactions_by_weekday"] = [
                {"label": weekday_names[int(r[0])], "day_num": int(r[0]), "count": r[1]}
                for r in await cursor.fetchall()
            ]

        return stats

    async def find_songs(self, user_id: int = None, channel_id: int = None, limit: int = 1, random: bool = False) -> list[dict]:
        """Find songs, optionally filtered by user and/or channel. Can return random results."""
        clauses = []
        params: list = []
        if user_id is not None:
            clauses.append("user_id = ?")
            params.append(user_id)
        if channel_id is not None:
            clauses.append("channel_id = ?")
            params.append(channel_id)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
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
        """Leaderboard: all users ranked by weighted score (40% songs, 60% unique reactions)."""
        # Songs per user
        async with self.db.execute(
            "SELECT user_id, user_name, COUNT(*) as cnt FROM song_posts GROUP BY user_id"
        ) as cursor:
            users = {r[0]: {"user_id": r[0], "user_name": r[1], "song_count": r[2], "reaction_count": 0}
                     for r in await cursor.fetchall()}

        if not users:
            return []

        # Unique reactions received: count distinct (reactor, song) pairs per author
        async with self.db.execute(
            "SELECT post_author_id, COUNT(*) as cnt FROM "
            "(SELECT DISTINCT post_author_id, reactor_user_id, message_id FROM song_reactions "
            "WHERE post_author_id IS NOT NULL) GROUP BY post_author_id"
        ) as cursor:
            for r in await cursor.fetchall():
                if r[0] in users:
                    users[r[0]]["reaction_count"] = r[1]

        # Compute weighted score (normalized 0-100, then weighted)
        max_songs = max(u["song_count"] for u in users.values()) or 1
        max_reactions = max(u["reaction_count"] for u in users.values()) or 1

        for u in users.values():
            song_norm = u["song_count"] / max_songs * 100
            react_norm = u["reaction_count"] / max_reactions * 100
            u["score"] = round(0.4 * song_norm + 0.6 * react_norm, 1)

        ranking = sorted(users.values(), key=lambda x: x["score"], reverse=True)
        return ranking

    # --- Song Reactions ---

    async def add_song_reaction(self, message_id: int, channel_id: int, song_url: str,
                                 post_author_id: int, reactor_user_id: int,
                                 reactor_user_name: str, emoji: str, song_title: str = None,
                                 reacted_at: float = None):
        if reacted_at is not None:
            await self.db.execute(
                "INSERT INTO song_reactions "
                "(message_id, channel_id, song_url, post_author_id, reactor_user_id, reactor_user_name, emoji, song_title, reacted_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(message_id, reactor_user_id, emoji) DO UPDATE SET "
                "song_title = COALESCE(song_reactions.song_title, excluded.song_title), "
                "song_url = COALESCE(song_reactions.song_url, excluded.song_url), "
                "reacted_at = MIN(song_reactions.reacted_at, excluded.reacted_at)",
                (message_id, channel_id, song_url, post_author_id, reactor_user_id, reactor_user_name, emoji, song_title, reacted_at),
            )
        else:
            await self.db.execute(
                "INSERT INTO song_reactions "
                "(message_id, channel_id, song_url, post_author_id, reactor_user_id, reactor_user_name, emoji, song_title) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(message_id, reactor_user_id, emoji) DO UPDATE SET "
                "song_title = COALESCE(song_reactions.song_title, excluded.song_title), "
                "song_url = COALESCE(song_reactions.song_url, excluded.song_url)",
                (message_id, channel_id, song_url, post_author_id, reactor_user_id, reactor_user_name, emoji, song_title),
            )
        await self.db.commit()

    async def add_song_reactions_bulk(self, rows: list[tuple]):
        """Bulk insert reactions. Rows: (message_id, channel_id, song_url, post_author_id, reactor_user_id, reactor_user_name, emoji, song_title, reacted_at)"""
        await self.db.executemany(
            "INSERT INTO song_reactions "
            "(message_id, channel_id, song_url, post_author_id, reactor_user_id, reactor_user_name, emoji, song_title, reacted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(message_id, reactor_user_id, emoji) DO UPDATE SET "
            "song_title = COALESCE(song_reactions.song_title, excluded.song_title), "
            "song_url = COALESCE(song_reactions.song_url, excluded.song_url), "
            "reacted_at = MIN(song_reactions.reacted_at, excluded.reacted_at)",
            rows,
        )
        await self.db.commit()

    async def get_scanned_reaction_message_ids(self) -> set[int]:
        """Return set of message_ids that already have reactions in the DB."""
        async with self.db.execute(
            "SELECT DISTINCT message_id FROM song_reactions"
        ) as cursor:
            return {r[0] async for r in cursor}

    async def get_reactions_missing_titles(self) -> list[dict]:
        """Return distinct (message_id, channel_id) pairs where song_title is NULL."""
        async with self.db.execute(
            "SELECT DISTINCT message_id, channel_id FROM song_reactions WHERE song_title IS NULL"
        ) as cursor:
            return [{"message_id": r[0], "channel_id": r[1]} for r in await cursor.fetchall()]

    async def update_song_title(self, message_id: int, song_title: str):
        """Set song_title for all reactions on a given message."""
        await self.db.execute(
            "UPDATE song_reactions SET song_title = ? WHERE message_id = ? AND song_title IS NULL",
            (song_title, message_id),
        )
        await self.db.commit()

    async def remove_song_reaction(self, message_id: int, reactor_user_id: int, emoji: str):
        await self.db.execute(
            "DELETE FROM song_reactions WHERE message_id = ? AND reactor_user_id = ? AND emoji = ?",
            (message_id, reactor_user_id, emoji),
        )
        await self.db.commit()

    async def get_player_discord_connection(self, web_user_id: int) -> dict | None:
        async with self.db.execute(
            "SELECT * FROM player_discord_connections WHERE web_user_id = ?",
            (web_user_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def link_player_discord_account(
        self,
        web_user_id: int,
        discord_user_id: int,
        discord_username: str,
        discord_display_name: str,
        discord_avatar: str | None,
    ) -> bool:
        """Link a Discord identity to one web account.

        Returns False when the Discord account is already linked to another
        web user.
        """
        try:
            await self.db.execute(
                """
                INSERT INTO player_discord_connections
                    (web_user_id, discord_user_id, discord_username,
                     discord_display_name, discord_avatar)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(web_user_id) DO UPDATE SET
                    discord_user_id = excluded.discord_user_id,
                    discord_username = excluded.discord_username,
                    discord_display_name = excluded.discord_display_name,
                    discord_avatar = excluded.discord_avatar,
                    updated_at = unixepoch()
                """,
                (
                    web_user_id,
                    discord_user_id,
                    discord_username,
                    discord_display_name,
                    discord_avatar,
                ),
            )
            await self.db.commit()
            return True
        except aiosqlite.IntegrityError:
            await self.db.rollback()
            return False

    async def unlink_player_discord_account(self, web_user_id: int):
        await self.db.execute(
            "DELETE FROM player_discord_connections WHERE web_user_id = ?",
            (web_user_id,),
        )
        await self.db.commit()

    async def toggle_player_song_reaction(
        self,
        message_id: int,
        channel_id: int,
        web_user_id: int,
        discord_user_id: int,
        discord_display_name: str,
        emoji: str,
    ) -> bool:
        """Toggle a linked user's Player reaction and return True when added."""
        async with self.db.execute(
            """
            SELECT 1 FROM player_song_reactions
            WHERE message_id = ? AND discord_user_id = ? AND emoji = ?
            """,
            (message_id, discord_user_id, emoji),
        ) as cursor:
            exists = await cursor.fetchone()
        if exists:
            await self.db.execute(
                """
                DELETE FROM player_song_reactions
                WHERE message_id = ? AND discord_user_id = ? AND emoji = ?
                """,
                (message_id, discord_user_id, emoji),
            )
            await self.db.commit()
            return False

        await self.db.execute(
            """
            INSERT INTO player_song_reactions
                (message_id, channel_id, web_user_id, discord_user_id,
                 discord_display_name, emoji)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                channel_id,
                web_user_id,
                discord_user_id,
                discord_display_name,
                emoji,
            ),
        )
        await self.db.commit()
        return True

    async def get_player_song_reactions(self, message_id: int) -> list[dict]:
        async with self.db.execute(
            """
            SELECT discord_user_id, MAX(discord_display_name) AS discord_display_name,
                   emoji, MAX(reacted_at) AS reacted_at
            FROM (
                SELECT discord_user_id, discord_display_name, emoji, reacted_at
                FROM player_song_reactions
                WHERE message_id = ?
                UNION ALL
                SELECT discord_user_id, discord_display_name, emoji, reacted_at
                FROM public_player_song_reactions
                WHERE message_id = ?
            )
            GROUP BY discord_user_id, emoji
            ORDER BY reacted_at, discord_display_name COLLATE NOCASE
            """,
            (message_id, message_id),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def get_player_user_reactions(
        self, message_id: int, discord_user_id: int
    ) -> list[str]:
        async with self.db.execute(
            """
            SELECT emoji FROM player_song_reactions
            WHERE message_id = ? AND discord_user_id = ?
            ORDER BY reacted_at
            """,
            (message_id, discord_user_id),
        ) as cursor:
            return [row[0] for row in await cursor.fetchall()]

    async def toggle_public_player_song_reaction(
        self,
        message_id: int,
        channel_id: int,
        discord_user_id: int,
        discord_display_name: str,
        emoji: str,
    ) -> bool:
        """Toggle a public Player reaction and return True when added."""
        async with self.db.execute(
            """
            SELECT 1 FROM public_player_song_reactions
            WHERE message_id = ? AND discord_user_id = ? AND emoji = ?
            """,
            (message_id, discord_user_id, emoji),
        ) as cursor:
            exists = await cursor.fetchone()
        if exists:
            await self.db.execute(
                """
                DELETE FROM public_player_song_reactions
                WHERE message_id = ? AND discord_user_id = ? AND emoji = ?
                """,
                (message_id, discord_user_id, emoji),
            )
            await self.db.commit()
            return False

        await self.db.execute(
            """
            INSERT INTO public_player_song_reactions
                (message_id, channel_id, discord_user_id,
                 discord_display_name, emoji)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                message_id,
                channel_id,
                discord_user_id,
                discord_display_name,
                emoji,
            ),
        )
        await self.db.commit()
        return True

    async def get_public_player_user_reactions(
        self, message_id: int, discord_user_id: int
    ) -> list[str]:
        async with self.db.execute(
            """
            SELECT emoji FROM public_player_song_reactions
            WHERE message_id = ? AND discord_user_id = ?
            ORDER BY reacted_at
            """,
            (message_id, discord_user_id),
        ) as cursor:
            return [row[0] for row in await cursor.fetchall()]

    async def get_player_reaction_thread(self, message_id: int) -> dict | None:
        async with self.db.execute(
            "SELECT * FROM player_reaction_threads WHERE message_id = ?",
            (message_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def set_player_reaction_thread(
        self,
        message_id: int,
        channel_id: int,
        thread_id: int,
        summary_message_id: int | None,
    ):
        await self.db.execute(
            """
            INSERT INTO player_reaction_threads
                (message_id, channel_id, thread_id, summary_message_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(message_id) DO UPDATE SET
                channel_id = excluded.channel_id,
                thread_id = excluded.thread_id,
                summary_message_id = excluded.summary_message_id,
                updated_at = unixepoch()
            """,
            (message_id, channel_id, thread_id, summary_message_id),
        )
        await self.db.commit()

    async def delete_all_reactions(self) -> int:
        """Delete all rows from song_reactions. Returns number of deleted rows."""
        async with self.db.execute("SELECT COUNT(*) FROM song_reactions") as cursor:
            count = (await cursor.fetchone())[0]
        await self.db.execute("DELETE FROM song_reactions")
        await self.db.commit()
        return count

    async def delete_song_posts_by_message_id(self, message_id: int):
        """Remove song_posts entries for a deleted message."""
        await self.db.execute("DELETE FROM song_posts WHERE message_id = ?", (message_id,))
        await self.db.commit()

    async def get_song_posts_with_message_id(self) -> list[dict]:
        """Return all song_posts entries that have a message_id (for Discord verification)."""
        async with self.db.execute(
            "SELECT id, channel_id, message_id FROM song_posts WHERE message_id IS NOT NULL"
        ) as cursor:
            rows = await cursor.fetchall()
            return [dict(r) for r in rows]

    async def delete_all_songs(self) -> int:
        """Delete all rows from song_posts. Returns number of deleted rows."""
        async with self.db.execute("SELECT COUNT(*) FROM song_posts") as cursor:
            count = (await cursor.fetchone())[0]
        await self.db.execute("DELETE FROM song_posts")
        await self.db.commit()
        return count

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

        # --- All-time stats (only channel filter, no time filter) ---
        alltime_conditions = []
        alltime_params = []
        if channel_id:
            alltime_conditions.append("channel_id = ?")
            alltime_params.append(channel_id)
        alltime_where = ("WHERE " + " AND ".join(alltime_conditions)) if alltime_conditions else ""
        alltime_params = tuple(alltime_params)

        # Top emojis (all-time)
        async with self.db.execute(
            f"SELECT emoji, COUNT(*) as cnt FROM song_reactions {alltime_where} GROUP BY emoji ORDER BY cnt DESC LIMIT 15",
            alltime_params,
        ) as cursor:
            stats["top_emojis"] = [{"emoji": r[0], "count": r[1]} for r in await cursor.fetchall()]

        # Top reactors (all-time)
        async with self.db.execute(
            f"SELECT reactor_user_id, reactor_user_name, COUNT(DISTINCT message_id) as cnt "
            f"FROM song_reactions {alltime_where} GROUP BY reactor_user_id ORDER BY cnt DESC LIMIT 10",
            alltime_params,
        ) as cursor:
            stats["top_reactors"] = [
                {"user_id": r[0], "user_name": r[1], "count": r[2]}
                for r in await cursor.fetchall()
            ]

        # Most reacted authors (all-time)
        author_conditions = list(alltime_conditions)
        author_conditions.append("post_author_id IS NOT NULL")
        author_where = "WHERE " + " AND ".join(author_conditions)
        async with self.db.execute(
            f"SELECT post_author_id, COUNT(*) as cnt "
            f"FROM song_reactions {author_where} "
            f"GROUP BY post_author_id ORDER BY cnt DESC LIMIT 10",
            alltime_params,
        ) as cursor:
            stats["most_reacted_authors"] = [
                {"user_id": r[0], "count": r[1]} for r in await cursor.fetchall()
            ]

        return stats

    async def get_unseen_songs(self, channel_id: int, user_id: int) -> list[dict]:
        """Return songs from last 2 days in channel that user hasn't reacted to, oldest first."""
        async with self.db.execute(
            """
            SELECT sp.id, sp.url, sp.user_id, sp.posted_at,
                   (SELECT COUNT(DISTINCT sr2.reactor_user_id) FROM song_reactions sr2
                    WHERE sr2.message_id = sp.message_id OR sr2.song_url = sp.url) as unique_cnt,
                   (SELECT COUNT(*) FROM song_reactions sr3
                    WHERE sr3.message_id = sp.message_id OR sr3.song_url = sp.url) as total_cnt,
                   COALESCE(
                       (SELECT MAX(sr4.song_title) FROM song_reactions sr4
                        WHERE sr4.message_id = sp.message_id OR sr4.song_url = sp.url),
                       sp.song_title
                   ) as title,
                   sp.message_id, sp.channel_id
            FROM song_posts sp
            WHERE sp.channel_id = ?
              AND sp.posted_at >= unixepoch('now', '-2 days')
              AND sp.user_id != ?
              AND NOT EXISTS (
                  SELECT 1 FROM song_reactions sr
                  WHERE sr.reactor_user_id = ?
                  AND (sr.message_id = sp.message_id OR sr.song_url = sp.url)
              )
            ORDER BY sp.posted_at ASC
            """,
            (channel_id, user_id, user_id),
        ) as cursor:
            return [
                {
                    "song_post_id": r[0], "song_url": r[1], "post_author_id": r[2],
                    "posted_at": r[3], "unique_count": r[4], "total_count": r[5],
                    "song_title": r[6], "message_id": r[7], "channel_id": r[8],
                }
                for r in await cursor.fetchall()
            ]

    async def get_user_top_emojis(self, user_id: int, limit: int = 4) -> list[str]:
        """Return the user's most frequently used reaction emojis."""
        async with self.db.execute(
            "SELECT emoji, COUNT(*) as cnt FROM song_reactions "
            "WHERE reactor_user_id = ? GROUP BY emoji ORDER BY cnt DESC LIMIT ?",
            (user_id, limit),
        ) as cursor:
            return [r[0] for r in await cursor.fetchall()]

    async def get_top_songs(self, channel_id: int = None, days: int = 0) -> list[dict]:
        """Return top songs ranked by unique reactions. Filters by song posting date (not reaction date)."""
        conditions = []
        params = []
        if channel_id:
            conditions.append("sp.channel_id = ?")
            params.append(channel_id)
        if days:
            conditions.append(f"sp.posted_at >= unixepoch('now', '-{int(days)} days')")

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        async with self.db.execute(
            f"SELECT sr.message_id, sr.song_url, sr.post_author_id, "
            f"COUNT(DISTINCT sr.reactor_user_id) as unique_cnt, COUNT(*) as total_cnt, "
            f"MAX(sr.song_title) as title "
            f"FROM song_reactions sr "
            f"JOIN song_posts sp ON sp.message_id = sr.message_id "
            f"{where} GROUP BY sr.message_id ORDER BY unique_cnt DESC LIMIT 10",
            tuple(params),
        ) as cursor:
            return [
                {
                    "message_id": r[0], "song_url": r[1], "post_author_id": r[2],
                    "unique_count": r[3], "total_count": r[4], "song_title": r[5],
                }
                for r in await cursor.fetchall()
            ]

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

    # --- Image Posting ---

    async def get_image_categories(self) -> list[dict]:
        async with self.db.execute("SELECT * FROM image_categories ORDER BY name") as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def add_image_category(self, name: str) -> int:
        cursor = await self.db.execute(
            "INSERT INTO image_categories (name) VALUES (?)", (name,)
        )
        await self.db.commit()
        return cursor.lastrowid

    async def delete_image_category(self, category_id: int):
        await self.db.execute("DELETE FROM image_posts WHERE category_id = ?", (category_id,))
        await self.db.execute("DELETE FROM image_categories WHERE id = ?", (category_id,))
        await self.db.commit()

    async def add_image_post(self, title: str, description: str, category_id: int, filename: str) -> int:
        cursor = await self.db.execute(
            "INSERT INTO image_posts (title, description, category_id, filename) VALUES (?, ?, ?, ?)",
            (title, description, category_id, filename),
        )
        await self.db.commit()
        return cursor.lastrowid

    async def get_image_posts(self, category_id: int = None) -> list[dict]:
        if category_id:
            query = """
                SELECT ip.*, ic.name as category_name
                FROM image_posts ip JOIN image_categories ic ON ip.category_id = ic.id
                WHERE ip.category_id = ? ORDER BY ip.uploaded_at DESC
            """
            params = (category_id,)
        else:
            query = """
                SELECT ip.*, ic.name as category_name
                FROM image_posts ip JOIN image_categories ic ON ip.category_id = ic.id
                ORDER BY ip.uploaded_at DESC
            """
            params = ()
        async with self.db.execute(query, params) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def get_image_post(self, image_id: int) -> dict | None:
        async with self.db.execute(
            "SELECT ip.*, ic.name as category_name FROM image_posts ip "
            "JOIN image_categories ic ON ip.category_id = ic.id WHERE ip.id = ?",
            (image_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_image_post_by_title(self, title: str, category_id: int) -> dict | None:
        async with self.db.execute(
            "SELECT ip.*, ic.name as category_name FROM image_posts ip "
            "JOIN image_categories ic ON ip.category_id = ic.id "
            "WHERE ip.title = ? AND ip.category_id = ?",
            (title, category_id),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_random_image_post(self, category_id: int) -> dict | None:
        async with self.db.execute(
            "SELECT ip.*, ic.name as category_name FROM image_posts ip "
            "JOIN image_categories ic ON ip.category_id = ic.id "
            "WHERE ip.category_id = ? ORDER BY RANDOM() LIMIT 1",
            (category_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def delete_image_post(self, image_id: int) -> str | None:
        """Delete an image post. Returns the filename for cleanup."""
        async with self.db.execute("SELECT filename FROM image_posts WHERE id = ?", (image_id,)) as cursor:
            row = await cursor.fetchone()
        if row:
            await self.db.execute("DELETE FROM image_posts WHERE id = ?", (image_id,))
            await self.db.commit()
            return row["filename"]
        return None

    async def get_image_category_by_name(self, name: str) -> dict | None:
        async with self.db.execute(
            "SELECT * FROM image_categories WHERE name = ? COLLATE NOCASE", (name,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    # --- Party Playlist ---

    async def party_submit_song(self, user_id: int, user_name: str, url: str, song_title: str = None, image_url: str = None) -> int:
        cursor = await self.db.execute(
            "INSERT INTO party_playlist (user_id, user_name, url, song_title, image_url) VALUES (?, ?, ?, ?, ?)",
            (user_id, user_name, url, song_title, image_url),
        )
        await self.db.commit()
        return cursor.lastrowid

    async def party_get_user_songs(self, user_id: int) -> list[dict]:
        async with self.db.execute(
            "SELECT * FROM party_playlist WHERE user_id = ? ORDER BY submitted_at", (user_id,)
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def party_get_user_song_count(self, user_id: int) -> int:
        async with self.db.execute(
            "SELECT COUNT(*) FROM party_playlist WHERE user_id = ?", (user_id,)
        ) as cursor:
            return (await cursor.fetchone())[0]

    async def party_remove_song(self, song_id: int, user_id: int) -> bool:
        """Remove a song from the playlist. Returns True if deleted."""
        async with self.db.execute(
            "SELECT id FROM party_playlist WHERE id = ? AND user_id = ?", (song_id, user_id)
        ) as cursor:
            row = await cursor.fetchone()
        if row:
            await self.db.execute("DELETE FROM party_playlist WHERE id = ?", (song_id,))
            await self.db.commit()
            return True
        return False

    async def party_get_all_songs(self) -> list[dict]:
        async with self.db.execute(
            "SELECT * FROM party_playlist ORDER BY submitted_at"
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def party_get_unheard_songs(self) -> list[dict]:
        async with self.db.execute(
            "SELECT * FROM party_playlist WHERE heard = 0 ORDER BY RANDOM()"
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def party_mark_heard(self, song_id: int):
        await self.db.execute("UPDATE party_playlist SET heard = 1 WHERE id = ?", (song_id,))
        await self.db.commit()

    async def party_mark_unheard(self, song_id: int):
        await self.db.execute("UPDATE party_playlist SET heard = 0 WHERE id = ?", (song_id,))
        await self.db.commit()

    async def party_reset(self) -> int:
        """Delete all songs from the party playlist. Returns count."""
        async with self.db.execute("SELECT COUNT(*) FROM party_playlist") as cursor:
            count = (await cursor.fetchone())[0]
        await self.db.execute("DELETE FROM party_playlist")
        await self.db.commit()
        return count

    # --- Polls ---

    async def create_poll(
        self,
        title: str,
        description: str,
        options: str,
        image_filename: str = None,
        creator_id: int = None,
        creator_name: str = None,
    ) -> int:
        cursor = await self.db.execute(
            "INSERT INTO polls "
            "(title, description, options, image_filename, creator_id, creator_name) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (title, description, options, image_filename, creator_id, creator_name),
        )
        await self.db.commit()
        return cursor.lastrowid

    async def get_poll(self, poll_id: int) -> Optional[dict]:
        async with self.db.execute("SELECT * FROM polls WHERE id = ?", (poll_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_all_polls(self) -> list[dict]:
        async with self.db.execute("SELECT * FROM polls ORDER BY created_at DESC") as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def update_poll(self, poll_id: int, title: str, description: str, options: str, image_filename: str = None):
        if image_filename is not None:
            await self.db.execute(
                "UPDATE polls SET title = ?, description = ?, options = ?, image_filename = ? WHERE id = ?",
                (title, description, options, image_filename, poll_id),
            )
        else:
            await self.db.execute(
                "UPDATE polls SET title = ?, description = ?, options = ? WHERE id = ?",
                (title, description, options, poll_id),
            )
        await self.db.commit()

    async def update_poll_message(self, poll_id: int, channel_id: int, message_id: int):
        await self.db.execute(
            "UPDATE polls SET channel_id = ?, message_id = ?, active = 1 WHERE id = ?",
            (channel_id, message_id, poll_id),
        )
        await self.db.commit()

    async def close_poll(self, poll_id: int):
        await self.db.execute("UPDATE polls SET active = 0 WHERE id = ?", (poll_id,))
        await self.db.commit()

    async def delete_poll(self, poll_id: int) -> Optional[str]:
        """Delete a poll. Returns image_filename if any."""
        async with self.db.execute("SELECT image_filename FROM polls WHERE id = ?", (poll_id,)) as cursor:
            row = await cursor.fetchone()
            filename = row["image_filename"] if row else None
        await self.db.execute("DELETE FROM polls WHERE id = ?", (poll_id,))
        await self.db.commit()
        return filename

    # --- Quiz ---

    async def get_quiz_categories(self) -> list[dict]:
        async with self.db.execute(
            "SELECT key, name, created_at FROM quiz_categories ORDER BY name COLLATE NOCASE"
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def get_quiz_category(self, key: str) -> Optional[dict]:
        async with self.db.execute(
            "SELECT key, name, created_at FROM quiz_categories WHERE key = ?",
            (key,),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def create_quiz_category(self, key: str, name: str) -> None:
        await self.db.execute(
            "INSERT INTO quiz_categories (key, name) VALUES (?, ?)",
            (key, name),
        )
        await self.db.commit()

    async def delete_quiz_category(self, key: str) -> bool:
        async with self.db.execute(
            "SELECT COUNT(*) AS count FROM quiz_questions WHERE mode = ?",
            (key,),
        ) as cursor:
            row = await cursor.fetchone()
        if row and row["count"]:
            return False
        await self.db.execute("DELETE FROM quiz_categories WHERE key = ?", (key,))
        await self.db.commit()
        return True

    async def create_quiz_question(
        self,
        mode: str,
        question: str,
        answers: list[str],
        correct_answer: str,
    ) -> int:
        cursor = await self.db.execute(
            "INSERT INTO quiz_questions "
            "(mode, question, answer_1, answer_2, answer_3, answer_4, answer_5, correct_answer) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (mode, question, answers[0], answers[1], answers[2], answers[3], answers[4], correct_answer),
        )
        await self.db.commit()
        return cursor.lastrowid

    async def create_quiz_questions_bulk(self, questions: list[tuple[str, str, list[str], str]]) -> int:
        if not questions:
            return 0
        rows = [
            (mode, question, answers[0], answers[1], answers[2], answers[3], answers[4], correct_answer)
            for mode, question, answers, correct_answer in questions
        ]
        await self.db.executemany(
            "INSERT INTO quiz_questions "
            "(mode, question, answer_1, answer_2, answer_3, answer_4, answer_5, correct_answer) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        await self.db.commit()
        return len(rows)

    async def get_quiz_question(self, question_id: int) -> Optional[dict]:
        async with self.db.execute(
            "SELECT * FROM quiz_questions WHERE id = ?", (question_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_quiz_questions(self, mode: str | None = None) -> list[dict]:
        if mode:
            sql = "SELECT * FROM quiz_questions WHERE mode = ? ORDER BY updated_at DESC, id DESC"
            args = (mode,)
        else:
            sql = "SELECT * FROM quiz_questions ORDER BY mode, updated_at DESC, id DESC"
            args = ()
        async with self.db.execute(sql, args) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def get_random_quiz_question(self, mode: str | None = None) -> Optional[dict]:
        if mode and mode != "mixed":
            sql = "SELECT * FROM quiz_questions WHERE mode = ? ORDER BY RANDOM() LIMIT 1"
            args = (mode,)
        else:
            sql = "SELECT * FROM quiz_questions ORDER BY RANDOM() LIMIT 1"
            args = ()
        async with self.db.execute(sql, args) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def update_quiz_question(
        self,
        question_id: int,
        mode: str,
        question: str,
        answers: list[str],
        correct_answer: str,
    ):
        await self.db.execute(
            "UPDATE quiz_questions SET "
            "mode = ?, question = ?, answer_1 = ?, answer_2 = ?, answer_3 = ?, "
            "answer_4 = ?, answer_5 = ?, correct_answer = ?, updated_at = unixepoch() "
            "WHERE id = ?",
            (mode, question, answers[0], answers[1], answers[2], answers[3], answers[4], correct_answer, question_id),
        )
        await self.db.commit()

    async def delete_quiz_question(self, question_id: int):
        await self.db.execute("DELETE FROM quiz_questions WHERE id = ?", (question_id,))
        await self.db.commit()

    async def increment_quiz_score(self, user_id: int, user_name: str) -> int:
        await self.db.execute(
            "INSERT INTO quiz_scores (user_id, user_name, points, last_solved_at) "
            "VALUES (?, ?, 1, unixepoch()) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "user_name = excluded.user_name, "
            "points = points + 1, "
            "last_solved_at = excluded.last_solved_at",
            (user_id, user_name),
        )
        await self.db.commit()
        async with self.db.execute(
            "SELECT points FROM quiz_scores WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return int(row["points"]) if row else 0

    async def get_quiz_highscore(self, limit: int = 10) -> list[dict]:
        async with self.db.execute(
            "SELECT user_id, user_name, points, last_solved_at "
            "FROM quiz_scores "
            "ORDER BY points DESC, last_solved_at ASC "
            "LIMIT ?",
            (limit,),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    # --- Radio Songs ---

    async def add_radio_song(
        self, title: str, artist: str, suno_url: str, filename: str,
        original_filename: str, file_size: int, duration: float, bitrate: int,
        uploaded_by_ip: str, rights_declaration: str, rights_hash: str,
    ) -> int:
        import time
        expires_at = time.time() + 14 * 86400
        # Set position to max+1
        async with self.db.execute("SELECT COALESCE(MAX(position), 0) FROM radio_songs") as cursor:
            max_pos = (await cursor.fetchone())[0]
        cursor = await self.db.execute(
            """INSERT INTO radio_songs
               (title, artist, suno_url, filename, original_filename, file_size,
                duration, bitrate, uploaded_by_ip, expires_at,
                rights_declaration, rights_hash, rights_agreed_at, position)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (title, artist, suno_url, filename, original_filename, file_size,
             duration, bitrate, uploaded_by_ip, expires_at,
             rights_declaration, rights_hash, time.time(), max_pos + 1),
        )
        await self.db.commit()
        return cursor.lastrowid

    async def get_radio_song(self, song_id: int) -> Optional[dict]:
        async with self.db.execute("SELECT * FROM radio_songs WHERE id = ?", (song_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_all_radio_songs(self, active_only: bool = True) -> list[dict]:
        if active_only:
            sql = "SELECT * FROM radio_songs WHERE active = 1 ORDER BY position ASC"
        else:
            sql = "SELECT * FROM radio_songs ORDER BY position ASC"
        async with self.db.execute(sql) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def delete_radio_song(self, song_id: int) -> Optional[str]:
        """Delete a radio song. Returns filename for file cleanup."""
        async with self.db.execute("SELECT filename FROM radio_songs WHERE id = ?", (song_id,)) as cursor:
            row = await cursor.fetchone()
            filename = row["filename"] if row else None
        await self.db.execute("DELETE FROM radio_songs WHERE id = ?", (song_id,))
        await self.db.commit()
        return filename

    async def reorder_radio_songs(self, song_ids: list[int]):
        """Set position based on order of IDs provided."""
        for i, sid in enumerate(song_ids):
            await self.db.execute(
                "UPDATE radio_songs SET position = ? WHERE id = ?", (i, sid)
            )
        await self.db.commit()

    async def move_radio_song(self, song_id: int, direction: str):
        """Move a song up or down in the playlist."""
        songs = await self.get_all_radio_songs()
        idx = next((i for i, s in enumerate(songs) if s["id"] == song_id), None)
        if idx is None:
            return
        if direction == "up" and idx > 0:
            swap_id = songs[idx - 1]["id"]
            swap_pos = songs[idx - 1]["position"]
            await self.db.execute("UPDATE radio_songs SET position = ? WHERE id = ?", (swap_pos, song_id))
            await self.db.execute("UPDATE radio_songs SET position = ? WHERE id = ?", (songs[idx]["position"], swap_id))
        elif direction == "down" and idx < len(songs) - 1:
            swap_id = songs[idx + 1]["id"]
            swap_pos = songs[idx + 1]["position"]
            await self.db.execute("UPDATE radio_songs SET position = ? WHERE id = ?", (swap_pos, song_id))
            await self.db.execute("UPDATE radio_songs SET position = ? WHERE id = ?", (songs[idx]["position"], swap_id))
        await self.db.commit()

    async def cleanup_expired_radio_songs(self) -> tuple[list[str], list[dict]]:
        """Delete expired songs. Returns (filenames, song_details) for cleanup + notification."""
        import time
        now = time.time()
        async with self.db.execute(
            "SELECT id, title, artist, filename, suno_url FROM radio_songs WHERE expires_at IS NOT NULL AND expires_at < ?",
            (now,),
        ) as cursor:
            rows = [dict(r) for r in await cursor.fetchall()]
        filenames = [r["filename"] for r in rows]
        if rows:
            await self.db.execute(
                "DELETE FROM radio_songs WHERE expires_at IS NOT NULL AND expires_at < ?", (now,)
            )
            await self.db.commit()
        return filenames, rows

    async def count_radio_uploads_by_ip(self, ip: str, hours: int = 1) -> int:
        """Count uploads from an IP in the last N hours for rate limiting."""
        import time
        since = time.time() - hours * 3600
        async with self.db.execute(
            "SELECT COUNT(*) FROM radio_songs WHERE uploaded_by_ip = ? AND uploaded_at > ?",
            (ip, since),
        ) as cursor:
            return (await cursor.fetchone())[0]

    async def count_active_radio_songs_by_artist(self, artist: str) -> int:
        """Count active radio songs for a given artist (case-insensitive)."""
        import time
        async with self.db.execute(
            "SELECT COUNT(*) FROM radio_songs "
            "WHERE active = 1 AND LOWER(artist) = LOWER(?) "
            "AND (expires_at IS NULL OR expires_at > ?)",
            (artist, time.time()),
        ) as cursor:
            return (await cursor.fetchone())[0]

    # --- Suno Playlists (Radio) ---

    async def add_suno_playlist(self, url: str, description: str) -> int:
        cursor = await self.db.execute(
            "INSERT INTO suno_playlists (url, description) VALUES (?, ?)",
            (url, description),
        )
        await self.db.commit()
        return cursor.lastrowid

    async def get_all_suno_playlists(self) -> list[dict]:
        async with self.db.execute("SELECT * FROM suno_playlists ORDER BY created_at DESC") as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def get_suno_playlist(self, playlist_id: int) -> Optional[dict]:
        async with self.db.execute("SELECT * FROM suno_playlists WHERE id = ?", (playlist_id,)) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def delete_suno_playlist(self, playlist_id: int):
        await self.db.execute("DELETE FROM suno_playlists WHERE id = ?", (playlist_id,))
        await self.db.commit()

    # --- LLM Config ---

    async def get_llm_config(self) -> dict:
        async with self.db.execute("SELECT * FROM llm_config WHERE id = 1") as cur:
            row = await cur.fetchone()
            return dict(row) if row else {}

    async def update_llm_config(self, **fields):
        if not fields:
            return
        allowed = {
            "enabled", "model", "tools_model", "persona", "retention_days",
            "rate_per_user_min", "rate_per_channel_min",
            "max_tokens", "tools_enabled", "default_result_limit",
        }
        sets, params = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            sets.append(f"{k} = ?")
            params.append(v)
        if not sets:
            return
        sets.append("updated_at = unixepoch()")
        params.append(1)
        await self.db.execute(
            f"UPDATE llm_config SET {', '.join(sets)} WHERE id = ?", params
        )
        await self.db.commit()

    async def get_llm_allowed_channels(self) -> list:
        async with self.db.execute(
            "SELECT channel_id, channel_name FROM llm_allowed_channels ORDER BY channel_name"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def set_llm_allowed_channels(self, channels: list):
        # channels: list of (channel_id, channel_name)
        await self.db.execute("DELETE FROM llm_allowed_channels")
        for cid, cname in channels:
            await self.db.execute(
                "INSERT OR REPLACE INTO llm_allowed_channels (channel_id, channel_name) VALUES (?, ?)",
                (cid, cname),
            )
        await self.db.commit()

    async def is_llm_channel_allowed(self, channel_id: int) -> bool:
        async with self.db.execute(
            "SELECT 1 FROM llm_allowed_channels WHERE channel_id = ?", (channel_id,)
        ) as cur:
            return await cur.fetchone() is not None

    async def get_llm_allowed_roles(self) -> list:
        async with self.db.execute(
            "SELECT role_id, role_name FROM llm_allowed_roles ORDER BY role_name"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def set_llm_allowed_roles(self, roles: list):
        await self.db.execute("DELETE FROM llm_allowed_roles")
        for rid, rname in roles:
            await self.db.execute(
                "INSERT OR REPLACE INTO llm_allowed_roles (role_id, role_name) VALUES (?, ?)",
                (rid, rname),
            )
        await self.db.commit()

    async def get_llm_allowed_role_ids(self) -> set:
        async with self.db.execute("SELECT role_id FROM llm_allowed_roles") as cur:
            return {r["role_id"] for r in await cur.fetchall()}

    async def log_llm_interaction(
        self,
        user_id: int = None,
        user_name: str = None,
        channel_id: int = None,
        prompt: str = None,
        response: str = None,
        tools_used: str = None,
        error: str = None,
        latency_ms: int = None,
        blocked: bool = False,
        block_reason: str = None,
    ):
        await self.db.execute(
            """INSERT INTO llm_audit_log
               (user_id, user_name, channel_id, prompt, response, tools_used,
                error, latency_ms, blocked, block_reason)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, user_name, channel_id, prompt, response, tools_used,
             error, latency_ms, 1 if blocked else 0, block_reason),
        )
        await self.db.commit()

    async def get_llm_audit_log(self, limit: int = 200) -> list:
        async with self.db.execute(
            "SELECT * FROM llm_audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def count_llm_user_recent(self, user_id: int, since_ts: float) -> int:
        async with self.db.execute(
            "SELECT COUNT(*) c FROM llm_audit_log WHERE user_id = ? AND timestamp >= ? AND blocked = 0",
            (user_id, since_ts),
        ) as cur:
            row = await cur.fetchone()
            return row["c"] if row else 0

    async def count_llm_channel_recent(self, channel_id: int, since_ts: float) -> int:
        async with self.db.execute(
            "SELECT COUNT(*) c FROM llm_audit_log WHERE channel_id = ? AND timestamp >= ? AND blocked = 0",
            (channel_id, since_ts),
        ) as cur:
            row = await cur.fetchone()
            return row["c"] if row else 0

    async def search_songs_by_artist(self, artist: str, channel_ids: list = None,
                                     days: int = None, limit: int = 10,
                                     order: str = "recent") -> list[dict]:
        """Search songs where title/user_name contains the artist substring.
        order: 'recent' | 'reactions'
        """
        cond = ["(LOWER(sp.song_title) LIKE ? OR LOWER(sp.user_name) LIKE ?)"]
        pat = f"%{artist.lower()}%"
        params = [pat, pat]
        if channel_ids:
            placeholders = ",".join("?" for _ in channel_ids)
            cond.append(f"sp.channel_id IN ({placeholders})")
            params.extend(channel_ids)
        if days and days > 0:
            cond.append("sp.posted_at >= ?")
            params.append(time.time() - days * 86400)
        order_sql = "COUNT(sr.id) DESC, sp.posted_at DESC" if order == "reactions" \
                    else "sp.posted_at DESC"
        sql = f"""
            SELECT sp.id, sp.url, sp.song_title, sp.user_name, sp.posted_at,
                   sp.channel_id, sp.message_id,
                   COUNT(sr.id) AS reaction_count
            FROM song_posts sp
            LEFT JOIN song_reactions sr
              ON sr.message_id = sp.message_id AND sp.message_id IS NOT NULL
            WHERE {' AND '.join(cond)}
            GROUP BY sp.id
            ORDER BY {order_sql}
            LIMIT ?
        """
        params.append(max(1, min(25, int(limit))))
        async with self.db.execute(sql, params) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def get_songs_by_user_id(self, user_id: int, channel_ids: list = None,
                                   days: int = None, limit: int = 10,
                                   order: str = "recent") -> list[dict]:
        """Songs posted by a specific Discord user."""
        cond = ["sp.user_id = ?"]
        params = [int(user_id)]
        if channel_ids:
            placeholders = ",".join("?" for _ in channel_ids)
            cond.append(f"sp.channel_id IN ({placeholders})")
            params.extend(channel_ids)
        if days and days > 0:
            cond.append("sp.posted_at >= ?")
            params.append(time.time() - days * 86400)
        order_sql = "COUNT(sr.id) DESC, sp.posted_at DESC" if order == "reactions" \
                    else "sp.posted_at DESC"
        sql = f"""
            SELECT sp.id, sp.url, sp.song_title, sp.user_name, sp.posted_at,
                   sp.channel_id, sp.message_id,
                   COUNT(sr.id) AS reaction_count
            FROM song_posts sp
            LEFT JOIN song_reactions sr
              ON sr.message_id = sp.message_id AND sp.message_id IS NOT NULL
            WHERE {' AND '.join(cond)}
            GROUP BY sp.id
            ORDER BY {order_sql}
            LIMIT ?
        """
        params.append(max(1, min(25, int(limit))))
        async with self.db.execute(sql, params) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def get_recent_songs(self, channel_ids: list = None, days: int = 7,
                               limit: int = 10) -> list[dict]:
        cond = []
        params = []
        if channel_ids:
            placeholders = ",".join("?" for _ in channel_ids)
            cond.append(f"sp.channel_id IN ({placeholders})")
            params.extend(channel_ids)
        if days and days > 0:
            cond.append("sp.posted_at >= ?")
            params.append(time.time() - days * 86400)
        where = ("WHERE " + " AND ".join(cond)) if cond else ""
        sql = f"""
            SELECT sp.id, sp.url, sp.song_title, sp.user_name, sp.posted_at,
                   sp.channel_id, sp.message_id,
                   COUNT(sr.id) AS reaction_count
            FROM song_posts sp
            LEFT JOIN song_reactions sr
              ON sr.message_id = sp.message_id AND sp.message_id IS NOT NULL
            {where}
            GROUP BY sp.id
            ORDER BY sp.posted_at DESC
            LIMIT ?
        """
        params.append(max(1, min(25, int(limit))))
        async with self.db.execute(sql, params) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def get_top_reacted_songs(self, channel_ids: list = None,
                                    days: int = None, limit: int = 10) -> list[dict]:
        """Songs ranked by reactions. When `days` is given, the window
        applies to the reactions themselves (reacted_at) — so 'top this
        week' means 'songs that collected the most reactions this week',
        regardless of when they were originally posted."""
        params: list = []
        join_cond = "sr.message_id = sp.message_id AND sp.message_id IS NOT NULL"
        if days and days > 0:
            join_cond += " AND sr.reacted_at >= ?"
            params.append(time.time() - days * 86400)

        where_parts: list[str] = []
        if channel_ids:
            placeholders = ",".join("?" for _ in channel_ids)
            where_parts.append(f"sp.channel_id IN ({placeholders})")
            params.extend(channel_ids)
        where = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

        sql = f"""
            SELECT sp.id, sp.url, sp.song_title, sp.user_name, sp.posted_at,
                   sp.channel_id, sp.message_id,
                   COUNT(sr.id) AS reaction_count
            FROM song_posts sp
            LEFT JOIN song_reactions sr
              ON {join_cond}
            {where}
            GROUP BY sp.id
            HAVING reaction_count > 0
            ORDER BY reaction_count DESC, sp.posted_at DESC
            LIMIT ?
        """
        params.append(max(1, min(25, int(limit))))
        async with self.db.execute(sql, params) as cur:
            return [dict(r) for r in await cur.fetchall()]

    # --- Suno Userlist (per web user) ---

    async def suno_userlist_add(
        self,
        owner_user_id: int,
        profile_url: str,
        handle: str,
        display_name: str | None = None,
        avatar_url: str | None = None,
        last_song_url: str | None = None,
        last_song_title: str | None = None,
        pinned_song_url: str | None = None,
        pinned_song_title: str | None = None,
        latest_song_url: str | None = None,
        latest_song_title: str | None = None,
        priority: str = "medium",
    ) -> int | None:
        try:
            cur = await self.db.execute(
                "INSERT INTO suno_userlist "
                "(owner_user_id, profile_url, handle, display_name, avatar_url, "
                " priority, last_song_url, last_song_title, "
                " pinned_song_url, pinned_song_title, latest_song_url, latest_song_title, "
                " last_fetched_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (owner_user_id, profile_url, handle, display_name, avatar_url,
                 priority, last_song_url, last_song_title,
                 pinned_song_url, pinned_song_title, latest_song_url, latest_song_title,
                 time.time()),
            )
            await self.db.commit()
            return cur.lastrowid
        except aiosqlite.IntegrityError:
            return None

    async def suno_userlist_list(self, owner_user_id: int) -> list[dict]:
        async with self.db.execute(
            "SELECT * FROM suno_userlist WHERE owner_user_id = ? "
            "ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 "
            "  WHEN 'low' THEN 2 ELSE 3 END, "
            "  done ASC, LOWER(COALESCE(display_name, handle)) ASC",
            (owner_user_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def suno_userlist_get(self, owner_user_id: int, entry_id: int) -> dict | None:
        async with self.db.execute(
            "SELECT * FROM suno_userlist WHERE owner_user_id = ? AND id = ?",
            (owner_user_id, entry_id),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def suno_userlist_delete(self, owner_user_id: int, entry_id: int) -> bool:
        cur = await self.db.execute(
            "DELETE FROM suno_userlist WHERE owner_user_id = ? AND id = ?",
            (owner_user_id, entry_id),
        )
        await self.db.commit()
        return (cur.rowcount or 0) > 0

    async def suno_userlist_set_priority(
        self, owner_user_id: int, entry_id: int, priority: str
    ) -> bool:
        if priority not in ("high", "medium", "low"):
            return False
        cur = await self.db.execute(
            "UPDATE suno_userlist SET priority = ? WHERE owner_user_id = ? AND id = ?",
            (priority, owner_user_id, entry_id),
        )
        await self.db.commit()
        return (cur.rowcount or 0) > 0

    async def suno_userlist_set_done(
        self, owner_user_id: int, entry_id: int, done: bool
    ) -> bool:
        if done:
            cur = await self.db.execute(
                "UPDATE suno_userlist SET done = 1, "
                "  last_song_url = COALESCE(latest_song_url, last_song_url), "
                "  last_song_title = COALESCE(latest_song_title, last_song_title) "
                "WHERE owner_user_id = ? AND id = ?",
                (owner_user_id, entry_id),
            )
        else:
            cur = await self.db.execute(
                "UPDATE suno_userlist SET done = 0 WHERE owner_user_id = ? AND id = ?",
                (owner_user_id, entry_id),
            )
        await self.db.commit()
        return (cur.rowcount or 0) > 0

    async def suno_userlist_reset_done(self, owner_user_id: int) -> int:
        """Reset all entries of the given owner to done=0. Returns affected row count."""
        cur = await self.db.execute(
            "UPDATE suno_userlist SET done = 0 WHERE owner_user_id = ? AND done = 1",
            (owner_user_id,),
        )
        await self.db.commit()
        return cur.rowcount or 0

    async def suno_userlist_set_paused(
        self, owner_user_id: int, entry_id: int, paused: bool
    ) -> bool:
        cur = await self.db.execute(
            "UPDATE suno_userlist SET paused = ? WHERE owner_user_id = ? AND id = ?",
            (1 if paused else 0, owner_user_id, entry_id),
        )
        await self.db.commit()
        return (cur.rowcount or 0) > 0

    async def suno_userlist_update_url(
        self,
        owner_user_id: int,
        entry_id: int,
        profile_url: str,
        handle: str,
        display_name: str | None,
        avatar_url: str | None,
        last_song_url: str | None,
        last_song_title: str | None,
        pinned_song_url: str | None = None,
        pinned_song_title: str | None = None,
        latest_song_url: str | None = None,
        latest_song_title: str | None = None,
    ) -> bool:
        try:
            cur = await self.db.execute(
                "UPDATE suno_userlist SET profile_url = ?, handle = ?, display_name = ?, "
                "  avatar_url = ?, last_song_url = ?, last_song_title = ?, "
                "  pinned_song_url = ?, pinned_song_title = ?, latest_song_url = ?, latest_song_title = ?, "
                "  last_fetched_at = ? "
                "WHERE owner_user_id = ? AND id = ?",
                (profile_url, handle, display_name, avatar_url,
                 last_song_url, last_song_title,
                 pinned_song_url, pinned_song_title, latest_song_url, latest_song_title,
                 time.time(),
                 owner_user_id, entry_id),
            )
            await self.db.commit()
            return (cur.rowcount or 0) > 0
        except aiosqlite.IntegrityError:
            return False

    async def suno_userlist_update_meta(
        self,
        owner_user_id: int,
        entry_id: int,
        display_name: str | None,
        avatar_url: str | None,
        last_song_url: str | None,
        last_song_title: str | None,
        pinned_song_url: str | None = None,
        pinned_song_title: str | None = None,
        latest_song_url: str | None = None,
        latest_song_title: str | None = None,
        done: bool | None = None,
    ) -> bool:
        done_value = None if done is None else (1 if done else 0)
        cur = await self.db.execute(
            "UPDATE suno_userlist SET display_name = ?, avatar_url = ?, "
            "  last_song_url = ?, last_song_title = ?, "
            "  pinned_song_url = ?, pinned_song_title = ?, latest_song_url = ?, latest_song_title = ?, "
            "  done = CASE WHEN ? IS NULL THEN done ELSE ? END, "
            "  last_fetched_at = ? "
            "WHERE owner_user_id = ? AND id = ?",
            (display_name, avatar_url, last_song_url, last_song_title,
             pinned_song_url, pinned_song_title, latest_song_url, latest_song_title,
             done_value, done_value,
             time.time(),
             owner_user_id, entry_id),
        )
        await self.db.commit()
        return (cur.rowcount or 0) > 0

    async def purge_llm_audit_log(self, retention_days: int) -> int:
        cutoff = time.time() - retention_days * 86400
        cur = await self.db.execute(
            "DELETE FROM llm_audit_log WHERE timestamp < ?", (cutoff,)
        )
        await self.db.commit()
        return cur.rowcount or 0

    # --- Reaction Roles ---

    async def add_reaction_role(
        self,
        *,
        channel_id: int,
        message_id: int,
        role_id: int,
        role_name: str,
        emoji: str,
        emoji_id: int | None,
        content: str,
        all_message_ids: str = "",
    ) -> int:
        cur = await self.db.execute(
            "INSERT INTO reaction_roles "
            "  (channel_id, message_id, role_id, role_name, emoji, emoji_id, content, all_message_ids) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (channel_id, message_id, role_id, role_name, emoji, emoji_id, content, all_message_ids),
        )
        await self.db.commit()
        return cur.lastrowid

    async def get_all_reaction_roles(self) -> list[dict]:
        async with self.db.execute(
            "SELECT * FROM reaction_roles ORDER BY created_at DESC"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def get_reaction_role(
        self, message_id: int, emoji: str
    ) -> Optional[dict]:
        async with self.db.execute(
            "SELECT * FROM reaction_roles WHERE message_id = ? AND emoji = ?",
            (message_id, emoji),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def delete_reaction_role(self, entry_id: int) -> Optional[dict]:
        """Delete a reaction-role entry; returns the deleted row (or None)."""
        async with self.db.execute(
            "SELECT * FROM reaction_roles WHERE id = ?", (entry_id,)
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            data = dict(row)
        await self.db.execute(
            "DELETE FROM reaction_roles WHERE id = ?", (entry_id,)
        )
        await self.db.commit()
        return data

    # --- User Preferences (per-web-user UI settings) ---

    _PREF_COLUMNS = {"suno_player_split", "dc_channel", "dc_limit", "dc_days"}

    async def get_user_preference(self, user_id: int, key: str, default=None):
        """Get a user preference value. Returns default if not set."""
        if key not in self._PREF_COLUMNS:
            return default
        async with self.db.execute(
            f"SELECT {key} FROM user_preferences WHERE user_id = ?",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
            return row[key] if row and row[key] is not None else default

    async def get_all_user_preferences(self, user_id: int) -> dict:
        """Return all preferences for a user as a dict."""
        async with self.db.execute(
            "SELECT suno_player_split, dc_channel, dc_limit, dc_days "
            "FROM user_preferences WHERE user_id = ?",
            (user_id,),
        ) as cur:
            row = await cur.fetchone()
        if row:
            return dict(row)
        return {"suno_player_split": 0.55, "dc_channel": "", "dc_limit": 15, "dc_days": 1}

    async def set_user_preference(self, user_id: int, key: str, value):
        """Set a user preference value."""
        if key not in self._PREF_COLUMNS:
            return
        await self.db.execute(
            f"INSERT INTO user_preferences (user_id, {key}, updated_at) "
            f"VALUES (?, ?, unixepoch()) "
            f"ON CONFLICT(user_id) DO UPDATE SET {key} = excluded.{key}, updated_at = excluded.updated_at",
            (user_id, value),
        )
        await self.db.commit()

    # ── Experimental Radio ──────────────────────────────────────────────────

    async def add_exp_radio_song(
        self, user_id: int, user_name: str, suno_url: str, suno_uuid: str,
        rights_declaration: str, rights_hash: str, expiry_days: int = 14,
    ) -> tuple[int, str]:
        import secrets
        upload_token = secrets.token_urlsafe(32)
        expiry_days = 7 if int(expiry_days or 14) == 7 else 14
        expires_at = time.time() + expiry_days * 86400
        cursor = await self.db.execute(
            """INSERT INTO exp_radio_songs
               (user_id, user_name, suno_url, suno_uuid,
                rights_declaration, rights_hash, rights_agreed_at, expires_at, upload_token)
               VALUES (?, ?, ?, ?, ?, ?, unixepoch(), ?, ?)""",
            (user_id, user_name, suno_url, suno_uuid,
             rights_declaration, rights_hash, expires_at, upload_token),
        )
        await self.db.commit()
        return cursor.lastrowid, upload_token

    async def get_exp_radio_song(self, song_id: int) -> Optional[dict]:
        async with self.db.execute(
            "SELECT * FROM exp_radio_songs WHERE id = ?", (song_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_exp_radio_song_by_token(self, token: str) -> Optional[dict]:
        async with self.db.execute(
            "SELECT * FROM exp_radio_songs WHERE upload_token = ? AND active = 1", (token,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_exp_radio_songs_by_user(self, user_id: int) -> list[dict]:
        async with self.db.execute(
            "SELECT * FROM exp_radio_songs WHERE user_id = ? AND active = 1 ORDER BY submitted_at DESC",
            (user_id,),
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]

    async def count_exp_radio_songs_by_user(self, user_id: int) -> int:
        async with self.db.execute(
            "SELECT COUNT(*) FROM exp_radio_songs "
            "WHERE user_id = ? AND active = 1 AND analysis_status != 'failed'",
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def get_exp_radio_submission_ban(self, user_id: int) -> Optional[dict]:
        async with self.db.execute(
            "SELECT * FROM exp_radio_submission_bans "
            "WHERE user_id = ? AND streams_remaining > 0",
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_exp_radio_submission_bans(self) -> list[dict]:
        async with self.db.execute(
            "SELECT * FROM exp_radio_submission_bans "
            "WHERE streams_remaining > 0 "
            "ORDER BY streams_remaining DESC, display_name COLLATE NOCASE"
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def set_exp_radio_submission_ban(
        self,
        user_id: int,
        user_name: str,
        display_name: str,
        streams_remaining: int,
        created_by: str = "",
    ) -> None:
        streams_remaining = max(1, int(streams_remaining))
        await self.db.execute(
            """
            INSERT INTO exp_radio_submission_bans
                (user_id, user_name, display_name, streams_remaining, created_by)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                user_name = excluded.user_name,
                display_name = excluded.display_name,
                streams_remaining = excluded.streams_remaining,
                updated_at = unixepoch(),
                created_by = excluded.created_by
            """,
            (user_id, user_name, display_name, streams_remaining, created_by),
        )
        await self.db.commit()

    async def remove_exp_radio_submission_ban(self, user_id: int) -> Optional[dict]:
        async with self.db.execute(
            "SELECT * FROM exp_radio_submission_bans WHERE user_id = ?",
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None
        data = dict(row)
        await self.db.execute(
            "DELETE FROM exp_radio_submission_bans WHERE user_id = ?",
            (user_id,),
        )
        await self.db.commit()
        return data

    async def advance_exp_radio_submission_bans(self) -> list[dict]:
        """Consume one blocked stream and return the affected rows."""
        async with self.db.execute(
            "SELECT * FROM exp_radio_submission_bans WHERE streams_remaining > 0"
        ) as cursor:
            affected = [dict(row) for row in await cursor.fetchall()]
        if not affected:
            return []
        await self.db.execute(
            "UPDATE exp_radio_submission_bans "
            "SET streams_remaining = streams_remaining - 1, updated_at = unixepoch() "
            "WHERE streams_remaining > 0"
        )
        await self.db.execute(
            "DELETE FROM exp_radio_submission_bans WHERE streams_remaining <= 0"
        )
        await self.db.commit()
        for row in affected:
            row["streams_remaining_after"] = max(0, int(row["streams_remaining"]) - 1)
        return affected

    async def update_exp_radio_song(self, song_id: int, **fields):
        """Generic field update for exp_radio_songs."""
        allowed = {
            "mp3_filename", "cover_url", "video_url", "title", "artist",
            "hook_id", "hook_share_url", "hook_video_url",
            "duration", "lyrics", "word_timestamps", "ass_filename",
            "analysis_status", "active", "playlist_source",
            "moderation_status", "moderation_reason", "moderation_at",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        sql = "UPDATE exp_radio_songs SET " + ", ".join(f"{k}=?" for k in updates)
        sql += " WHERE id = ?"
        await self.db.execute(sql, (*updates.values(), song_id))
        await self.db.commit()

    async def get_all_exp_radio_songs(
        self, active_only: bool = True, source: str | None = None
    ) -> list[dict]:
        """Return songs, optionally filtered by playlist_source ('submission'|'admin').
        source=None returns all sources."""
        conditions = ["active = 1"] if active_only else []
        params: list = []
        if source is not None:
            conditions.append("playlist_source = ?")
            params.append(source)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        sql = f"SELECT * FROM exp_radio_songs {where} ORDER BY submitted_at ASC"
        async with self.db.execute(sql, params) as cursor:
            return [dict(r) for r in await cursor.fetchall()]

    async def add_admin_exp_radio_song(self, suno_url: str, suno_uuid: str) -> int:
        """Insert an admin-playlist song (no user, no rights, no expiry).
        Returns the new song ID."""
        cursor = await self.db.execute(
            """
            INSERT INTO exp_radio_songs
               (user_id, user_name, suno_url, suno_uuid,
                rights_declaration, rights_hash, rights_agreed_at,
                expires_at, playlist_source)
            VALUES (0, 'admin', ?, ?, '', '', unixepoch(), 9999999999, 'admin')
            """,
            (suno_url, suno_uuid),
        )
        await self.db.commit()
        return cursor.lastrowid

    async def add_intro_outro_song(self, suno_url: str, suno_uuid: str, source: str) -> int:
        """Insert an intro or outro song. source must be 'intro' or 'outro'."""
        cursor = await self.db.execute(
            """
            INSERT INTO exp_radio_songs
               (user_id, user_name, suno_url, suno_uuid,
                rights_declaration, rights_hash, rights_agreed_at,
                expires_at, playlist_source)
            VALUES (0, 'admin', ?, ?, '', '', unixepoch(), 9999999999, ?)
            """,
            (suno_url, suno_uuid, source),
        )
        await self.db.commit()
        return cursor.lastrowid

    async def delete_exp_radio_song(self, song_id: int) -> Optional[dict]:
        """Soft-delete by setting active=0. Returns song data for file cleanup."""
        async with self.db.execute(
            "SELECT * FROM exp_radio_songs WHERE id = ?", (song_id,)
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return None
            data = dict(row)
        await self.db.execute(
            "UPDATE exp_radio_songs SET active = 0 WHERE id = ?", (song_id,)
        )
        await self.db.commit()
        return data

    async def delete_all_exp_radio_songs(self, source: str) -> int:
        """Soft-delete active songs for exactly one exp-radio playlist source."""
        if source not in {"submission", "admin", "intro", "outro"}:
            raise ValueError("delete_all_exp_radio_songs requires a valid playlist source")
        conditions = ["active = 1"]
        params: list = [source]
        conditions.append("playlist_source = ?")
        where = " AND ".join(conditions)
        async with self.db.execute(
            f"SELECT COUNT(*) FROM exp_radio_songs WHERE {where}",
            params,
        ) as cursor:
            row = await cursor.fetchone()
            count = row[0] if row else 0
        await self.db.execute(
            f"UPDATE exp_radio_songs SET active = 0 WHERE {where}",
            params,
        )
        await self.db.commit()
        return count

    async def expire_old_exp_radio_songs(self) -> list[dict]:
        """Mark expired songs inactive. Returns list of song dicts for file cleanup."""
        async with self.db.execute(
            "SELECT * FROM exp_radio_songs WHERE active = 1 AND expires_at < unixepoch()"
        ) as cursor:
            rows = [dict(r) for r in await cursor.fetchall()]
        if rows:
            ids = [r["id"] for r in rows]
            await self.db.execute(
                f"UPDATE exp_radio_songs SET active = 0 WHERE id IN ({','.join('?'*len(ids))})",
                ids,
            )
            await self.db.commit()
        return rows

    async def get_exp_radio_consent_csv_rows(self) -> list[dict]:
        """All consent records for CSV export."""
        async with self.db.execute(
            """SELECT id, user_id, user_name, suno_url, title, artist,
                      analysis_status, rights_hash, rights_agreed_at,
                      submitted_at, expires_at, active
               FROM exp_radio_songs ORDER BY submitted_at DESC"""
        ) as cursor:
            return [dict(r) for r in await cursor.fetchall()]

    # -----------------------------------------------------------------------
    # Relic Hunt — table creation
    # -----------------------------------------------------------------------
    async def ensure_relic_tables(self):
        """Create all Relic Hunt tables if they don't exist yet."""
        await self.db.executescript("""
            CREATE TABLE IF NOT EXISTS relic_items (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                rarity TEXT NOT NULL DEFAULT 'common',
                enabled INTEGER NOT NULL DEFAULT 1,
                drop_weight REAL NOT NULL DEFAULT 1,
                min_points INTEGER NOT NULL DEFAULT 0,
                max_points INTEGER NOT NULL DEFAULT 0,
                min_xp INTEGER NOT NULL DEFAULT 0,
                max_xp INTEGER NOT NULL DEFAULT 0,
                flavor_text TEXT,
                announce_globally INTEGER NOT NULL DEFAULT 0,
                can_be_used_in_ritual INTEGER NOT NULL DEFAULT 0,
                ritual_energy INTEGER NOT NULL DEFAULT 0,
                icon TEXT,
                category TEXT,
                seasonal_tag TEXT,
                required_event TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS relic_users (
                twitch_user_id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                points INTEGER NOT NULL DEFAULT 0,
                xp INTEGER NOT NULL DEFAULT 0,
                shinies INTEGER NOT NULL DEFAULT 0,
                level INTEGER NOT NULL DEFAULT 1,
                last_raven_at REAL,
                last_daily_at REAL,
                last_ritual_at REAL,
                commands_used INTEGER NOT NULL DEFAULT 0,
                legendary_finds INTEGER NOT NULL DEFAULT 0,
                mythic_finds INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS relic_user_items (
                twitch_user_id TEXT NOT NULL,
                item_id TEXT NOT NULL,
                amount INTEGER NOT NULL DEFAULT 0,
                first_found_at REAL,
                last_found_at REAL,
                PRIMARY KEY (twitch_user_id, item_id)
            );
            CREATE TABLE IF NOT EXISTS relic_hunt_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                twitch_user_id TEXT NOT NULL,
                username TEXT NOT NULL,
                item_id TEXT,
                item_name TEXT,
                rarity TEXT,
                points_awarded INTEGER NOT NULL DEFAULT 0,
                xp_awarded INTEGER NOT NULL DEFAULT 0,
                result_type TEXT NOT NULL,
                message TEXT,
                created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS relic_events (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                config_json TEXT NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS relic_active_events (
                event_id TEXT PRIMARY KEY,
                started_at REAL NOT NULL,
                ends_at REAL NOT NULL,
                started_by TEXT
            );
            CREATE TABLE IF NOT EXISTS relic_ritual_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                energy INTEGER NOT NULL DEFAULT 0,
                goal INTEGER NOT NULL DEFAULT 500,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS relic_ranks (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                icon TEXT,
                min_points INTEGER NOT NULL DEFAULT 0,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS relic_combine_recipes (
                id TEXT PRIMARY KEY,
                ingredient_a_id TEXT NOT NULL,
                ingredient_b_id TEXT NOT NULL,
                result_item_id TEXT NOT NULL,
                bonus_points INTEGER NOT NULL DEFAULT 0,
                priority INTEGER NOT NULL DEFAULT 100,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS relic_phrase_puzzle (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                phrase TEXT NOT NULL DEFAULT '',
                revealed_mask TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 0,
                loop_queue INTEGER NOT NULL DEFAULT 0,
                current_phrase_id INTEGER,
                letter_find_chance REAL NOT NULL DEFAULT 0.05,
                winner_xp_reward INTEGER NOT NULL DEFAULT 500,
                solved_by_user_id TEXT,
                solved_by_username TEXT,
                solved_at REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS relic_phrase_guesses (
                twitch_user_id TEXT PRIMARY KEY,
                username TEXT NOT NULL,
                last_guess_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS relic_phrase_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phrase TEXT NOT NULL,
                position INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 1,
                used_at REAL,
                solved_by_user_id TEXT,
                solved_by_username TEXT,
                solved_at REAL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS relic_custom_commands (
                command TEXT PRIMARY KEY,
                response TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS relic_village_areas (
                area_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                resource_type TEXT NOT NULL,
                level INTEGER NOT NULL DEFAULT 0,
                progress INTEGER NOT NULL DEFAULT 0,
                max_level INTEGER NOT NULL DEFAULT 5,
                updated_at REAL NOT NULL
            );
        """)
        await self.db.commit()

        async with self.db.execute("PRAGMA table_info(relic_users)") as cur:
            user_columns = [row["name"] for row in await cur.fetchall()]
        if "shinies" not in user_columns:
            await self.db.execute(
                "ALTER TABLE relic_users ADD COLUMN shinies INTEGER NOT NULL DEFAULT 0"
            )
            await self.db.commit()

        async with self.db.execute("PRAGMA table_info(relic_phrase_puzzle)") as cur:
            phrase_columns = [row["name"] for row in await cur.fetchall()]
        if "loop_queue" not in phrase_columns:
            await self.db.execute(
                "ALTER TABLE relic_phrase_puzzle ADD COLUMN loop_queue INTEGER NOT NULL DEFAULT 0"
            )
        if "current_phrase_id" not in phrase_columns:
            await self.db.execute(
                "ALTER TABLE relic_phrase_puzzle ADD COLUMN current_phrase_id INTEGER"
            )
        await self.db.commit()

        await self._migrate_phrase_to_queue()
        await self.relic_seed_village_areas()

    # -----------------------------------------------------------------------
    # Relic Hunt — ranks
    # -----------------------------------------------------------------------
    async def relic_get_all_ranks(self, active_only: bool = False) -> list[dict]:
        where = "WHERE enabled = 1" if active_only else ""
        async with self.db.execute(
            f"SELECT * FROM relic_ranks {where} ORDER BY min_points ASC, name ASC"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def relic_get_rank(self, rank_id: str) -> Optional[dict]:
        async with self.db.execute(
            "SELECT * FROM relic_ranks WHERE id = ?", (rank_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def relic_upsert_rank(self, rank: dict) -> None:
        now = time.time()
        await self.db.execute("""
            INSERT INTO relic_ranks
              (id, name, icon, min_points, enabled, created_at, updated_at)
            VALUES
              (:id,:name,:icon,:min_points,:enabled,:now,:now)
            ON CONFLICT(id) DO UPDATE SET
              name=excluded.name,
              icon=excluded.icon,
              min_points=excluded.min_points,
              enabled=excluded.enabled,
              updated_at=excluded.updated_at
        """, {
            "id": rank["id"],
            "name": rank["name"],
            "icon": rank.get("icon", ""),
            "min_points": int(rank.get("min_points") or 0),
            "enabled": 1 if rank.get("enabled", 1) else 0,
            "now": now,
        })
        await self.db.commit()

    async def relic_delete_rank(self, rank_id: str) -> None:
        await self.db.execute("DELETE FROM relic_ranks WHERE id = ?", (rank_id,))
        await self.db.commit()

    # -----------------------------------------------------------------------
    # Relic Hunt — custom chat commands
    # -----------------------------------------------------------------------
    @staticmethod
    def _normalize_relic_custom_command(command: str) -> str:
        return (command or "").strip().lstrip("!").lower()

    async def relic_get_all_custom_commands(self) -> list[dict]:
        async with self.db.execute(
            "SELECT * FROM relic_custom_commands ORDER BY command ASC"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def relic_get_custom_command(self, command: str) -> Optional[dict]:
        command = self._normalize_relic_custom_command(command)
        if not command:
            return None
        async with self.db.execute(
            "SELECT * FROM relic_custom_commands WHERE command = ?",
            (command,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def relic_upsert_custom_command(
        self, command: str, response: str, enabled: bool = True
    ) -> None:
        command = self._normalize_relic_custom_command(command)
        response = (response or "").strip()
        if not command or not response:
            return
        now = time.time()
        await self.db.execute("""
            INSERT INTO relic_custom_commands
              (command, response, enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(command) DO UPDATE SET
              response=excluded.response,
              enabled=excluded.enabled,
              updated_at=excluded.updated_at
        """, (command, response, 1 if enabled else 0, now, now))
        await self.db.commit()

    async def relic_toggle_custom_command(self, command: str) -> None:
        command = self._normalize_relic_custom_command(command)
        if not command:
            return
        await self.db.execute(
            "UPDATE relic_custom_commands "
            "SET enabled = CASE enabled WHEN 1 THEN 0 ELSE 1 END, updated_at = ? "
            "WHERE command = ?",
            (time.time(), command),
        )
        await self.db.commit()

    async def relic_delete_custom_command(self, command: str) -> None:
        command = self._normalize_relic_custom_command(command)
        if not command:
            return
        await self.db.execute(
            "DELETE FROM relic_custom_commands WHERE command = ?",
            (command,),
        )
        await self.db.commit()

    # -----------------------------------------------------------------------
    # Relic Hunt — combine recipes
    # -----------------------------------------------------------------------
    async def relic_get_all_combine_recipes(self, active_only: bool = False) -> list[dict]:
        where = "WHERE r.enabled = 1" if active_only else ""
        async with self.db.execute(f"""
            SELECT r.*,
                   a.name AS ingredient_a_name, a.icon AS ingredient_a_icon,
                   a.rarity AS ingredient_a_rarity,
                   b.name AS ingredient_b_name, b.icon AS ingredient_b_icon,
                   b.rarity AS ingredient_b_rarity,
                   result.name AS result_item_name, result.icon AS result_item_icon,
                   result.rarity AS result_item_rarity
            FROM relic_combine_recipes r
            LEFT JOIN relic_items a ON a.id = r.ingredient_a_id
            LEFT JOIN relic_items b ON b.id = r.ingredient_b_id
            LEFT JOIN relic_items result ON result.id = r.result_item_id
            {where}
            ORDER BY r.priority ASC, r.id ASC
        """) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def relic_get_combine_recipe(self, recipe_id: str) -> Optional[dict]:
        async with self.db.execute(
            "SELECT * FROM relic_combine_recipes WHERE id = ?", (recipe_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def relic_upsert_combine_recipe(self, recipe: dict) -> None:
        now = time.time()
        await self.db.execute("""
            INSERT INTO relic_combine_recipes
              (id, ingredient_a_id, ingredient_b_id, result_item_id,
               bonus_points, priority, enabled, created_at, updated_at)
            VALUES
              (:id,:ingredient_a_id,:ingredient_b_id,:result_item_id,
               :bonus_points,:priority,:enabled,:now,:now)
            ON CONFLICT(id) DO UPDATE SET
              ingredient_a_id=excluded.ingredient_a_id,
              ingredient_b_id=excluded.ingredient_b_id,
              result_item_id=excluded.result_item_id,
              bonus_points=excluded.bonus_points,
              priority=excluded.priority,
              enabled=excluded.enabled,
              updated_at=excluded.updated_at
        """, {
            "id": recipe["id"],
            "ingredient_a_id": recipe["ingredient_a_id"],
            "ingredient_b_id": recipe["ingredient_b_id"],
            "result_item_id": recipe["result_item_id"],
            "bonus_points": max(0, int(recipe.get("bonus_points") or 0)),
            "priority": max(0, int(recipe.get("priority") or 100)),
            "enabled": 1 if recipe.get("enabled", 1) else 0,
            "now": now,
        })
        await self.db.commit()

    async def relic_insert_combine_recipe_if_missing(self, recipe: dict) -> bool:
        """Insert a default recipe without overwriting an existing admin version."""
        now = time.time()
        cursor = await self.db.execute("""
            INSERT OR IGNORE INTO relic_combine_recipes
              (id, ingredient_a_id, ingredient_b_id, result_item_id,
               bonus_points, priority, enabled, created_at, updated_at)
            VALUES
              (:id,:ingredient_a_id,:ingredient_b_id,:result_item_id,
               :bonus_points,:priority,:enabled,:now,:now)
        """, {
            "id": recipe["id"],
            "ingredient_a_id": recipe["ingredient_a_id"],
            "ingredient_b_id": recipe["ingredient_b_id"],
            "result_item_id": recipe["result_item_id"],
            "bonus_points": max(0, int(recipe.get("bonus_points") or 0)),
            "priority": max(0, int(recipe.get("priority") or 100)),
            "enabled": 1 if recipe.get("enabled", 1) else 0,
            "now": now,
        })
        await self.db.commit()
        return cursor.rowcount > 0

    async def relic_delete_combine_recipe(self, recipe_id: str) -> None:
        await self.db.execute(
            "DELETE FROM relic_combine_recipes WHERE id = ?", (recipe_id,)
        )
        await self.db.commit()

    async def relic_apply_combine_recipe(
        self,
        twitch_user_id: str,
        username: str,
        recipe: dict,
        message: str,
    ) -> bool:
        """Atomically consume two ingredients, grant the result and log it."""
        ingredient_a = recipe["ingredient_a_id"]
        ingredient_b = recipe["ingredient_b_id"]
        result_item = recipe["result_item_id"]
        required_a = 2 if ingredient_a == ingredient_b else 1
        required_b = 0 if ingredient_a == ingredient_b else 1
        now = time.time()

        await self.db.execute("BEGIN IMMEDIATE")
        try:
            async with self.db.execute("""
                SELECT item_id, amount FROM relic_user_items
                WHERE twitch_user_id = ? AND item_id IN (?, ?)
            """, (twitch_user_id, ingredient_a, ingredient_b)) as cur:
                amounts = {row["item_id"]: row["amount"] for row in await cur.fetchall()}

            if amounts.get(ingredient_a, 0) < required_a:
                await self.db.rollback()
                return False
            if required_b and amounts.get(ingredient_b, 0) < required_b:
                await self.db.rollback()
                return False

            await self.db.execute("""
                UPDATE relic_user_items SET amount = amount - ?
                WHERE twitch_user_id = ? AND item_id = ?
            """, (required_a, twitch_user_id, ingredient_a))
            if required_b:
                await self.db.execute("""
                    UPDATE relic_user_items SET amount = amount - 1
                    WHERE twitch_user_id = ? AND item_id = ?
                """, (twitch_user_id, ingredient_b))

            await self.db.execute("""
                INSERT INTO relic_user_items
                  (twitch_user_id, item_id, amount, first_found_at, last_found_at)
                VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(twitch_user_id, item_id) DO UPDATE SET
                  amount = amount + 1, last_found_at = excluded.last_found_at
            """, (twitch_user_id, result_item, now, now))
            await self.db.execute("""
                UPDATE relic_users SET points = points + ?, updated_at = ?
                WHERE twitch_user_id = ?
            """, (recipe["bonus_points"], now, twitch_user_id))
            await self.db.execute("""
                INSERT INTO relic_hunt_log
                  (twitch_user_id, username, item_id, item_name, rarity,
                   points_awarded, xp_awarded, result_type, message, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 0, 'combine', ?, ?)
            """, (
                twitch_user_id,
                username,
                result_item,
                recipe["activity_text"],
                recipe.get("result_item_rarity") or "common",
                recipe["bonus_points"],
                message,
                now,
            ))
            await self.db.commit()
            return True
        except Exception:
            await self.db.rollback()
            raise

    # -----------------------------------------------------------------------
    # Relic Hunt — phrase puzzle
    # -----------------------------------------------------------------------
    async def _phrase_mask(self, phrase: str) -> str:
        return "".join("0" if char.isalpha() else "1" for char in phrase)

    async def _migrate_phrase_to_queue(self) -> None:
        puzzle = await self.relic_get_phrase_puzzle()
        if not puzzle.get("phrase"):
            return
        async with self.db.execute(
            "SELECT COUNT(*) AS count FROM relic_phrase_queue"
        ) as cur:
            row = await cur.fetchone()
        if row and row["count"]:
            return
        now = time.time()
        cursor = await self.db.execute("""
            INSERT INTO relic_phrase_queue
              (phrase, position, active, used_at, solved_by_user_id,
               solved_by_username, solved_at, created_at, updated_at)
            VALUES (?, 1, 1, ?, ?, ?, ?, ?, ?)
        """, (
            puzzle["phrase"],
            now if puzzle.get("phrase") else None,
            puzzle.get("solved_by_user_id"),
            puzzle.get("solved_by_username"),
            puzzle.get("solved_at"),
            now,
            now,
        ))
        await self.db.execute(
            "UPDATE relic_phrase_puzzle SET current_phrase_id = ? WHERE id = 1",
            (cursor.lastrowid,),
        )
        await self.db.commit()

    async def relic_get_phrase_puzzle(self) -> dict:
        async with self.db.execute(
            "SELECT * FROM relic_phrase_puzzle WHERE id = 1"
        ) as cur:
            row = await cur.fetchone()
            if row:
                return dict(row)
        return {
            "id": 1,
            "phrase": "",
            "revealed_mask": "",
            "enabled": 0,
            "letter_find_chance": 0.05,
            "winner_xp_reward": 500,
            "loop_queue": 0,
            "current_phrase_id": None,
            "solved_by_user_id": None,
            "solved_by_username": None,
            "solved_at": None,
            "created_at": time.time(),
            "updated_at": time.time(),
        }

    async def relic_save_phrase_puzzle(
        self,
        enabled: bool,
        loop_queue: bool,
        letter_find_chance: float,
        winner_xp_reward: int,
    ) -> None:
        """Save puzzle settings without changing phrase progress."""
        now = time.time()
        current = await self.relic_get_phrase_puzzle()
        await self.db.execute("""
            INSERT INTO relic_phrase_puzzle
              (id, phrase, revealed_mask, enabled, loop_queue,
               current_phrase_id, letter_find_chance,
               winner_xp_reward, solved_by_user_id, solved_by_username,
               solved_at, created_at, updated_at)
            VALUES
              (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              enabled=excluded.enabled,
              loop_queue=excluded.loop_queue,
              letter_find_chance=excluded.letter_find_chance,
              winner_xp_reward=excluded.winner_xp_reward,
              updated_at=excluded.updated_at
        """, (
            current.get("phrase", ""),
            current.get("revealed_mask", ""),
            1 if enabled else 0,
            1 if loop_queue else 0,
            current.get("current_phrase_id"),
            min(1.0, max(0.0, float(letter_find_chance))),
            max(0, int(winner_xp_reward)),
            current.get("solved_by_user_id"),
            current.get("solved_by_username"),
            current.get("solved_at"),
            current.get("created_at") or now,
            now,
        ))
        await self.db.commit()

    async def relic_reset_phrase_progress(self) -> None:
        puzzle = await self.relic_get_phrase_puzzle()
        phrase = puzzle.get("phrase", "")
        mask = await self._phrase_mask(phrase)
        await self.db.execute("""
            UPDATE relic_phrase_puzzle
            SET revealed_mask = ?, solved_by_user_id = NULL,
                solved_by_username = NULL, solved_at = NULL, updated_at = ?
            WHERE id = 1
        """, (mask, time.time()))
        await self.db.execute("DELETE FROM relic_phrase_guesses")
        await self.db.commit()

    async def relic_get_phrase_queue(self) -> list[dict]:
        async with self.db.execute("""
            SELECT * FROM relic_phrase_queue
            ORDER BY active DESC, position ASC, id ASC
        """) as cur:
            return [dict(row) for row in await cur.fetchall()]

    async def relic_add_phrase_to_queue(self, phrase: str) -> int:
        async with self.db.execute(
            "SELECT COALESCE(MAX(position), 0) AS max_position FROM relic_phrase_queue"
        ) as cur:
            row = await cur.fetchone()
        now = time.time()
        cursor = await self.db.execute("""
            INSERT INTO relic_phrase_queue
              (phrase, position, active, created_at, updated_at)
            VALUES (?, ?, 1, ?, ?)
        """, (phrase, int(row["max_position"] or 0) + 1, now, now))
        await self.db.commit()
        puzzle = await self.relic_get_phrase_puzzle()
        if not puzzle.get("phrase") and not puzzle.get("current_phrase_id"):
            await self.relic_activate_phrase(cursor.lastrowid, preserve_enabled=True)
        return cursor.lastrowid

    async def relic_delete_phrase_from_queue(self, phrase_id: int) -> None:
        puzzle = await self.relic_get_phrase_puzzle()
        await self.db.execute("DELETE FROM relic_phrase_queue WHERE id = ?", (phrase_id,))
        await self.db.commit()
        if puzzle.get("current_phrase_id") == phrase_id:
            await self.relic_activate_next_phrase()

    async def relic_activate_phrase(
        self,
        phrase_id: int,
        preserve_enabled: bool = True,
    ) -> bool:
        async with self.db.execute(
            "SELECT * FROM relic_phrase_queue WHERE id = ? AND active = 1",
            (phrase_id,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return False
        entry = dict(row)
        puzzle = await self.relic_get_phrase_puzzle()
        now = time.time()
        await self.db.execute("""
            INSERT INTO relic_phrase_puzzle
              (id, phrase, revealed_mask, enabled, loop_queue,
               current_phrase_id, letter_find_chance, winner_xp_reward,
               solved_by_user_id, solved_by_username, solved_at,
               created_at, updated_at)
            VALUES
              (1, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
              phrase=excluded.phrase,
              revealed_mask=excluded.revealed_mask,
              current_phrase_id=excluded.current_phrase_id,
              enabled=excluded.enabled,
              solved_by_user_id=NULL,
              solved_by_username=NULL,
              solved_at=NULL,
              updated_at=excluded.updated_at
        """, (
            entry["phrase"],
            await self._phrase_mask(entry["phrase"]),
            puzzle.get("enabled", 0) if preserve_enabled else 1,
            puzzle.get("loop_queue", 0),
            phrase_id,
            puzzle.get("letter_find_chance", 0.05),
            puzzle.get("winner_xp_reward", 500),
            puzzle.get("created_at") or now,
            now,
        ))
        await self.db.execute("""
            UPDATE relic_phrase_queue
            SET used_at = COALESCE(used_at, ?),
                solved_by_user_id = NULL,
                solved_by_username = NULL,
                solved_at = NULL,
                updated_at = ?
            WHERE id = ?
        """, (now, now, phrase_id))
        await self.db.execute("DELETE FROM relic_phrase_guesses")
        await self.db.commit()
        return True

    async def relic_activate_next_phrase(self) -> Optional[dict]:
        puzzle = await self.relic_get_phrase_puzzle()
        current_id = puzzle.get("current_phrase_id") or 0
        async with self.db.execute("""
            SELECT * FROM relic_phrase_queue
            WHERE active = 1
              AND solved_at IS NULL
              AND id != ?
            ORDER BY
              CASE WHEN used_at IS NULL THEN 0 ELSE 1 END,
              position ASC,
              id ASC
            LIMIT 1
        """, (current_id,)) as cur:
            row = await cur.fetchone()
        if not row and puzzle.get("loop_queue"):
            async with self.db.execute("""
                SELECT * FROM relic_phrase_queue
                WHERE active = 1 AND id != ?
                ORDER BY position ASC, id ASC
                LIMIT 1
            """, (current_id,)) as cur:
                row = await cur.fetchone()
        if not row and puzzle.get("loop_queue"):
            async with self.db.execute("""
                SELECT * FROM relic_phrase_queue
                WHERE active = 1
                ORDER BY position ASC, id ASC
                LIMIT 1
            """) as cur:
                row = await cur.fetchone()
        if not row:
            await self.db.execute("""
                UPDATE relic_phrase_puzzle
                SET phrase = '', revealed_mask = '', current_phrase_id = NULL,
                    enabled = 0, solved_by_user_id = NULL,
                    solved_by_username = NULL, solved_at = NULL, updated_at = ?
                WHERE id = 1
            """, (time.time(),))
            await self.db.execute("DELETE FROM relic_phrase_guesses")
            await self.db.commit()
            return None
        entry = dict(row)
        await self.relic_activate_phrase(entry["id"], preserve_enabled=True)
        return entry

    async def relic_mark_current_phrase_solved(
        self,
        twitch_user_id: str,
        username: str,
        solved_at: float,
    ) -> None:
        puzzle = await self.relic_get_phrase_puzzle()
        if not puzzle.get("current_phrase_id"):
            return
        await self.db.execute("""
            UPDATE relic_phrase_queue
            SET solved_by_user_id = ?, solved_by_username = ?,
                solved_at = ?, updated_at = ?
            WHERE id = ?
        """, (
            twitch_user_id,
            username,
            solved_at,
            solved_at,
            puzzle["current_phrase_id"],
        ))
        await self.db.commit()

    async def relic_reveal_random_phrase_letter(self) -> Optional[dict]:
        """Reveal one still-hidden letter occurrence and return its new state."""
        await self.db.execute("BEGIN IMMEDIATE")
        try:
            async with self.db.execute(
                "SELECT * FROM relic_phrase_puzzle WHERE id = 1"
            ) as cur:
                row = await cur.fetchone()
            if not row:
                await self.db.rollback()
                return None

            puzzle = dict(row)
            phrase = puzzle.get("phrase", "")
            mask = list(puzzle.get("revealed_mask", ""))
            if (
                not puzzle.get("enabled")
                or puzzle.get("solved_at")
                or not phrase
                or len(mask) != len(phrase)
            ):
                await self.db.rollback()
                return None

            hidden = [
                index for index, char in enumerate(phrase)
                if char.isalpha() and mask[index] != "1"
            ]
            if not hidden:
                await self.db.rollback()
                return None

            index = random.choice(hidden)
            mask[index] = "1"
            revealed_mask = "".join(mask)
            await self.db.execute("""
                UPDATE relic_phrase_puzzle
                SET revealed_mask = ?, updated_at = ?
                WHERE id = 1
            """, (revealed_mask, time.time()))
            await self.db.commit()
            return {
                **puzzle,
                "revealed_mask": revealed_mask,
                "revealed_index": index,
                "revealed_letter": phrase[index],
            }
        except Exception:
            await self.db.rollback()
            raise

    async def relic_try_solve_phrase(
        self,
        twitch_user_id: str,
        username: str,
        normalized_guess: str,
        cooldown_seconds: int = 3600,
    ) -> dict:
        """Atomically check a solution and enforce the per-user cooldown."""
        now = time.time()
        await self.db.execute("BEGIN IMMEDIATE")
        try:
            async with self.db.execute(
                "SELECT * FROM relic_phrase_puzzle WHERE id = 1"
            ) as cur:
                row = await cur.fetchone()
            if not row:
                await self.db.rollback()
                return {"status": "inactive"}
            puzzle = dict(row)
            if puzzle.get("solved_at"):
                await self.db.rollback()
                return {"status": "solved", "puzzle": puzzle}
            if not puzzle.get("enabled") or not puzzle.get("phrase"):
                await self.db.rollback()
                return {"status": "inactive"}

            async with self.db.execute("""
                SELECT last_guess_at FROM relic_phrase_guesses
                WHERE twitch_user_id = ?
            """, (twitch_user_id,)) as cur:
                guess_row = await cur.fetchone()
            if guess_row:
                remaining = cooldown_seconds - (now - guess_row["last_guess_at"])
                if remaining > 0:
                    await self.db.rollback()
                    return {"status": "cooldown", "remaining": remaining}

            normalized_phrase = " ".join(
                puzzle["phrase"].casefold().split()
            )
            if normalized_guess != normalized_phrase:
                await self.db.execute("""
                    INSERT INTO relic_phrase_guesses
                      (twitch_user_id, username, last_guess_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(twitch_user_id) DO UPDATE SET
                      username=excluded.username,
                      last_guess_at=excluded.last_guess_at
                """, (twitch_user_id, username, now))
                await self.db.commit()
                return {"status": "wrong"}

            solved_mask = "1" * len(puzzle["phrase"])
            cursor = await self.db.execute("""
                UPDATE relic_phrase_puzzle
                SET solved_by_user_id = ?, solved_by_username = ?,
                    solved_at = ?, revealed_mask = ?, updated_at = ?
                WHERE id = 1 AND solved_at IS NULL
            """, (twitch_user_id, username, now, solved_mask, now))
            if cursor.rowcount <= 0:
                await self.db.rollback()
                return {"status": "solved", "puzzle": puzzle}
            await self.db.commit()
            return {"status": "correct", "puzzle": puzzle}
        except Exception:
            await self.db.rollback()
            raise

    # -----------------------------------------------------------------------
    # Relic Hunt — items
    # -----------------------------------------------------------------------
    async def relic_get_all_items(self) -> list[dict]:
        async with self.db.execute(
            "SELECT * FROM relic_items ORDER BY rarity, name"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def relic_get_item(self, item_id: str) -> Optional[dict]:
        async with self.db.execute(
            "SELECT * FROM relic_items WHERE id = ?", (item_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def relic_upsert_item(self, item: dict) -> None:
        now = time.time()
        await self.db.execute("""
            INSERT INTO relic_items
              (id, name, rarity, enabled, drop_weight,
               min_points, max_points, min_xp, max_xp,
               flavor_text, announce_globally, can_be_used_in_ritual,
               ritual_energy, icon, category, seasonal_tag,
               required_event, created_at, updated_at)
            VALUES
              (:id,:name,:rarity,:enabled,:drop_weight,
               :min_points,:max_points,:min_xp,:max_xp,
               :flavor_text,:announce_globally,:can_be_used_in_ritual,
               :ritual_energy,:icon,:category,:seasonal_tag,
               :required_event,:now,:now)
            ON CONFLICT(id) DO UPDATE SET
              name=excluded.name, rarity=excluded.rarity,
              enabled=excluded.enabled, drop_weight=excluded.drop_weight,
              min_points=excluded.min_points, max_points=excluded.max_points,
              min_xp=excluded.min_xp, max_xp=excluded.max_xp,
              flavor_text=excluded.flavor_text,
              announce_globally=excluded.announce_globally,
              can_be_used_in_ritual=excluded.can_be_used_in_ritual,
              ritual_energy=excluded.ritual_energy,
              icon=excluded.icon, category=excluded.category,
              seasonal_tag=excluded.seasonal_tag,
              required_event=excluded.required_event,
              updated_at=excluded.updated_at
        """, {**item, "now": now})
        await self.db.commit()

    async def relic_delete_item(self, item_id: str) -> None:
        await self.db.execute("DELETE FROM relic_items WHERE id = ?", (item_id,))
        await self.db.commit()

    async def relic_get_eligible_items(self, active_event_ids: list) -> list[dict]:
        """Items that are enabled, have drop_weight > 0, and whose requiredEvent
        is NULL or matches an active event."""
        async with self.db.execute(
            "SELECT * FROM relic_items WHERE enabled = 1 AND drop_weight > 0"
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        return [
            r for r in rows
            if not r.get("required_event")
            or r["required_event"] in active_event_ids
        ]

    # -----------------------------------------------------------------------
    # Relic Hunt — users
    # -----------------------------------------------------------------------
    async def relic_get_user(self, twitch_user_id: str) -> Optional[dict]:
        async with self.db.execute(
            "SELECT * FROM relic_users WHERE twitch_user_id = ?", (twitch_user_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def relic_upsert_user(self, user: dict) -> None:
        now = time.time()
        await self.db.execute("""
            INSERT INTO relic_users
              (twitch_user_id, username, points, xp, shinies, level,
               last_raven_at, last_daily_at, last_ritual_at,
               commands_used, legendary_finds, mythic_finds,
               created_at, updated_at)
            VALUES
              (:twitch_user_id,:username,:points,:xp,:shinies,:level,
               :last_raven_at,:last_daily_at,:last_ritual_at,
               :commands_used,:legendary_finds,:mythic_finds,
               :now,:now)
            ON CONFLICT(twitch_user_id) DO UPDATE SET
              username=excluded.username,
              points=excluded.points, xp=excluded.xp,
              shinies=excluded.shinies, level=excluded.level,
              last_raven_at=excluded.last_raven_at,
              last_daily_at=excluded.last_daily_at,
              last_ritual_at=excluded.last_ritual_at,
              commands_used=excluded.commands_used,
              legendary_finds=excluded.legendary_finds,
              mythic_finds=excluded.mythic_finds,
              updated_at=excluded.updated_at
        """, {**user, "shinies": int(user.get("shinies") or 0), "now": now})
        await self.db.commit()

    async def relic_add_shinies(self, twitch_user_id: str, amount: int) -> None:
        await self.db.execute(
            "UPDATE relic_users SET shinies = MAX(0, shinies + ?), updated_at = ? "
            "WHERE twitch_user_id = ?",
            (int(amount), time.time(), twitch_user_id),
        )
        await self.db.commit()

    async def relic_try_spend_shinies(self, twitch_user_id: str, amount: int) -> bool:
        amount = max(0, int(amount))
        if amount <= 0:
            return True
        await self.db.execute("BEGIN IMMEDIATE")
        try:
            async with self.db.execute(
                "SELECT shinies FROM relic_users WHERE twitch_user_id = ?",
                (twitch_user_id,),
            ) as cur:
                row = await cur.fetchone()
            if not row or int(row["shinies"] or 0) < amount:
                await self.db.rollback()
                return False
            await self.db.execute(
                "UPDATE relic_users SET shinies = shinies - ?, updated_at = ? "
                "WHERE twitch_user_id = ?",
                (amount, time.time(), twitch_user_id),
            )
            await self.db.commit()
            return True
        except Exception:
            await self.db.rollback()
            raise

    async def relic_get_leaderboard(self, limit: int = 10) -> list[dict]:
        async with self.db.execute(
            "SELECT * FROM relic_users ORDER BY points DESC LIMIT ?", (limit,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def relic_get_all_users(self) -> list[dict]:
        async with self.db.execute(
            "SELECT * FROM relic_users ORDER BY points DESC"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def relic_delete_user(self, twitch_user_id: str) -> None:
        await self.db.execute(
            "DELETE FROM relic_users WHERE twitch_user_id = ?", (twitch_user_id,)
        )
        await self.db.execute(
            "DELETE FROM relic_user_items WHERE twitch_user_id = ?", (twitch_user_id,)
        )
        await self.db.commit()

    # -----------------------------------------------------------------------
    # Relic Hunt — Hrafnathorp village
    # -----------------------------------------------------------------------
    async def relic_seed_village_areas(self) -> None:
        defaults = [
            ("culture", "Culture", "points"),
            ("education", "Education", "xp"),
            ("trade", "Trade", "items"),
            ("treasury", "Treasury", "shinies"),
        ]
        now = time.time()
        for area_id, name, resource_type in defaults:
            await self.db.execute("""
                INSERT OR IGNORE INTO relic_village_areas
                  (area_id, name, resource_type, level, progress, max_level, updated_at)
                VALUES (?, ?, ?, 0, 0, 5, ?)
            """, (area_id, name, resource_type, now))
        await self.db.commit()

    async def relic_get_village_areas(self) -> list[dict]:
        await self.relic_seed_village_areas()
        order = {"culture": 1, "education": 2, "trade": 3, "treasury": 4}
        async with self.db.execute(
            "SELECT * FROM relic_village_areas"
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        return sorted(rows, key=lambda r: order.get(r["area_id"], 99))

    async def relic_get_village_area(self, area_id: str) -> Optional[dict]:
        await self.relic_seed_village_areas()
        async with self.db.execute(
            "SELECT * FROM relic_village_areas WHERE area_id = ?",
            (area_id,),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def relic_add_village_progress(self, area_id: str, amount: int = 1) -> Optional[dict]:
        await self.db.execute("BEGIN IMMEDIATE")
        try:
            async with self.db.execute(
                "SELECT * FROM relic_village_areas WHERE area_id = ?",
                (area_id,),
            ) as cur:
                row = await cur.fetchone()
            if not row:
                await self.db.rollback()
                return None
            area = dict(row)
            level = int(area["level"] or 0)
            max_level = int(area["max_level"] or 5)
            progress = int(area["progress"] or 0)
            if level >= max_level:
                await self.db.rollback()
                area["leveled_up"] = False
                area["is_maxed"] = True
                return area
            progress += max(0, int(amount))
            leveled_up = False
            while progress >= 100 and level < max_level:
                progress -= 100
                level += 1
                leveled_up = True
            if level >= max_level:
                progress = 0
            await self.db.execute("""
                UPDATE relic_village_areas
                SET level = ?, progress = ?, updated_at = ?
                WHERE area_id = ?
            """, (level, progress, time.time(), area_id))
            await self.db.commit()
            area.update({
                "level": level,
                "progress": progress,
                "leveled_up": leveled_up,
                "is_maxed": level >= max_level,
            })
            return area
        except Exception:
            await self.db.rollback()
            raise

    async def relic_reset_village(self) -> None:
        await self.db.execute(
            "UPDATE relic_village_areas SET level = 0, progress = 0, updated_at = ?",
            (time.time(),),
        )
        await self.db.commit()

    async def relic_get_active_users_since(self, cutoff: float) -> list[dict]:
        async with self.db.execute(
            "SELECT * FROM relic_users WHERE COALESCE(last_raven_at, 0) >= ? "
            "ORDER BY last_raven_at DESC",
            (cutoff,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    # -----------------------------------------------------------------------
    # Relic Hunt — user inventory
    # -----------------------------------------------------------------------
    async def relic_get_inventory(self, twitch_user_id: str) -> list[dict]:
        async with self.db.execute("""
            SELECT ui.*, i.name, i.rarity, i.icon, i.can_be_used_in_ritual,
                   i.ritual_energy, i.flavor_text
            FROM relic_user_items ui
            JOIN relic_items i ON i.id = ui.item_id
            WHERE ui.twitch_user_id = ? AND ui.amount > 0
            ORDER BY i.rarity DESC, ui.amount DESC
        """, (twitch_user_id,)) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def relic_add_item_to_user(self, twitch_user_id: str, item_id: str) -> None:
        now = time.time()
        await self.db.execute("""
            INSERT INTO relic_user_items (twitch_user_id, item_id, amount, first_found_at, last_found_at)
            VALUES (?, ?, 1, ?, ?)
            ON CONFLICT(twitch_user_id, item_id) DO UPDATE SET
              amount = amount + 1, last_found_at = excluded.last_found_at
        """, (twitch_user_id, item_id, now, now))
        await self.db.commit()

    async def relic_consume_ritual_item(self, twitch_user_id: str) -> Optional[dict]:
        """Remove 1 of the lowest-rarity ritual-eligible item from the user.
        Returns the item dict if consumed, None if user has no ritual items."""
        inv = await self.relic_get_inventory(twitch_user_id)
        rarity_order = ["common", "uncommon", "rare", "epic", "legendary", "mythic"]
        eligible = [i for i in inv if i.get("can_be_used_in_ritual") and i["amount"] > 0]
        if not eligible:
            return None
        eligible.sort(key=lambda i: rarity_order.index(i.get("rarity", "common")
                                                        if i.get("rarity") in rarity_order else "common"))
        item = eligible[0]
        await self.db.execute("""
            UPDATE relic_user_items SET amount = amount - 1
            WHERE twitch_user_id = ? AND item_id = ?
        """, (twitch_user_id, item["item_id"]))
        await self.db.commit()
        return item

    # -----------------------------------------------------------------------
    # Relic Hunt — hunt log
    # -----------------------------------------------------------------------
    async def relic_log_hunt(self, entry: dict) -> None:
        await self.db.execute("""
            INSERT INTO relic_hunt_log
              (twitch_user_id, username, item_id, item_name, rarity,
               points_awarded, xp_awarded, result_type, message, created_at)
            VALUES
              (:twitch_user_id,:username,:item_id,:item_name,:rarity,
               :points_awarded,:xp_awarded,:result_type,:message,:created_at)
        """, entry)
        await self.db.commit()

    async def relic_get_recent_log(self, limit: int = 50) -> list[dict]:
        async with self.db.execute(
            "SELECT * FROM relic_hunt_log ORDER BY created_at DESC LIMIT ?", (limit,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    # -----------------------------------------------------------------------
    # Relic Hunt — events
    # -----------------------------------------------------------------------
    async def relic_get_all_events(self) -> list[dict]:
        async with self.db.execute("SELECT * FROM relic_events ORDER BY name") as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def relic_upsert_event(self, event: dict) -> None:
        now = time.time()
        await self.db.execute("""
            INSERT INTO relic_events (id, name, enabled, config_json, created_at, updated_at)
            VALUES (:id,:name,:enabled,:config_json,:now,:now)
            ON CONFLICT(id) DO UPDATE SET
              name=excluded.name, enabled=excluded.enabled,
              config_json=excluded.config_json, updated_at=excluded.updated_at
        """, {**event, "now": now})
        await self.db.commit()

    async def relic_delete_event(self, event_id: str) -> None:
        await self.db.execute("DELETE FROM relic_events WHERE id = ?", (event_id,))
        await self.db.execute("DELETE FROM relic_active_events WHERE event_id = ?", (event_id,))
        await self.db.commit()

    async def relic_get_active_events(self) -> list[dict]:
        now = time.time()
        async with self.db.execute(
            "SELECT * FROM relic_active_events WHERE ends_at > ?", (now,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def relic_start_event(self, event_id: str, duration_seconds: float,
                                started_by: str = "") -> None:
        now = time.time()
        await self.db.execute("""
            INSERT INTO relic_active_events (event_id, started_at, ends_at, started_by)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET
              started_at=excluded.started_at, ends_at=excluded.ends_at,
              started_by=excluded.started_by
        """, (event_id, now, now + duration_seconds, started_by))
        await self.db.commit()

    async def relic_stop_event(self, event_id: str) -> None:
        await self.db.execute(
            "DELETE FROM relic_active_events WHERE event_id = ?", (event_id,)
        )
        await self.db.commit()

    async def relic_expire_events(self) -> list[dict]:
        """Delete and return active events whose time has elapsed."""
        now = time.time()
        async with self.db.execute(
            "SELECT * FROM relic_active_events WHERE ends_at <= ?", (now,)
        ) as cur:
            expired = [dict(r) for r in await cur.fetchall()]
        if expired:
            await self.db.execute(
                "DELETE FROM relic_active_events WHERE ends_at <= ?", (now,)
            )
            await self.db.commit()
        return expired

    # -----------------------------------------------------------------------
    # Relic Hunt — ritual state
    # -----------------------------------------------------------------------
    async def relic_get_ritual(self) -> dict:
        async with self.db.execute(
            "SELECT * FROM relic_ritual_state WHERE id = 1"
        ) as cur:
            row = await cur.fetchone()
            if row:
                return dict(row)
        return {"id": 1, "energy": 0, "goal": 500, "updated_at": time.time()}

    async def relic_update_ritual(self, energy: int, goal: Optional[int] = None) -> None:
        now = time.time()
        await self.db.execute("""
            INSERT INTO relic_ritual_state (id, energy, goal, updated_at)
            VALUES (1, ?, COALESCE(?, 500), ?)
            ON CONFLICT(id) DO UPDATE SET
              energy=excluded.energy,
              goal=CASE WHEN excluded.goal IS NOT NULL THEN excluded.goal ELSE goal END,
              updated_at=excluded.updated_at
        """, (energy, goal, now))
        await self.db.commit()

    # -----------------------------------------------------------------------
    # Relic Hunt — settings helpers
    # -----------------------------------------------------------------------
    async def relic_get_setting(self, key: str) -> Optional[str]:
        return await self.get_setting(f"relic_{key}")

    async def relic_set_setting(self, key: str, value: str) -> None:
        await self.set_setting(f"relic_{key}", value)

    # =======================================================================
    # RPG — table creation + seed defaults
    # =======================================================================
    async def ensure_rpg_tables(self) -> None:
        """Create all RPG tables if they don't exist yet and seed defaults."""
        await self.db.executescript("""
            CREATE TABLE IF NOT EXISTS rpg_adventures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                intro_text TEXT DEFAULT '',
                llm_system_prompt TEXT DEFAULT '',
                start_scene_key TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at REAL DEFAULT (unixepoch())
            );

            CREATE TABLE IF NOT EXISTS rpg_scenes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                adventure_id INTEGER NOT NULL,
                scene_key TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                narration TEXT NOT NULL DEFAULT '',
                scene_type TEXT NOT NULL DEFAULT 'story',
                data_json TEXT NOT NULL DEFAULT '{}',
                UNIQUE (adventure_id, scene_key)
            );
            CREATE INDEX IF NOT EXISTS idx_rpg_scenes_adv ON rpg_scenes(adventure_id);

            CREATE TABLE IF NOT EXISTS rpg_classes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                class_key TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                base_hp INTEGER NOT NULL DEFAULT 20,
                base_attack INTEGER NOT NULL DEFAULT 5,
                base_defense INTEGER NOT NULL DEFAULT 5,
                base_agility INTEGER NOT NULL DEFAULT 5,
                base_mana INTEGER NOT NULL DEFAULT 10,
                abilities_json TEXT NOT NULL DEFAULT '[]'
            );

            CREATE TABLE IF NOT EXISTS rpg_enemies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                enemy_key TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                hp INTEGER NOT NULL DEFAULT 15,
                attack INTEGER NOT NULL DEFAULT 4,
                defense INTEGER NOT NULL DEFAULT 3,
                agility INTEGER NOT NULL DEFAULT 4,
                abilities_json TEXT NOT NULL DEFAULT '[]',
                loot_json TEXT NOT NULL DEFAULT '[]',
                xp_reward INTEGER NOT NULL DEFAULT 10
            );

            CREATE TABLE IF NOT EXISTS rpg_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_key TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                item_type TEXT NOT NULL DEFAULT 'misc',
                effect_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS rpg_characters (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL UNIQUE,
                user_name TEXT NOT NULL,
                name TEXT NOT NULL,
                class_key TEXT NOT NULL,
                level INTEGER NOT NULL DEFAULT 1,
                xp INTEGER NOT NULL DEFAULT 0,
                hp INTEGER NOT NULL,
                max_hp INTEGER NOT NULL,
                mana INTEGER NOT NULL DEFAULT 0,
                max_mana INTEGER NOT NULL DEFAULT 0,
                attack INTEGER NOT NULL,
                defense INTEGER NOT NULL,
                agility INTEGER NOT NULL,
                stats_json TEXT NOT NULL DEFAULT '{}',
                inventory_json TEXT NOT NULL DEFAULT '[]',
                status_json TEXT NOT NULL DEFAULT '[]',
                cooldowns_json TEXT NOT NULL DEFAULT '{}',
                party_id INTEGER,
                created_at REAL DEFAULT (unixepoch()),
                updated_at REAL DEFAULT (unixepoch())
            );
            CREATE INDEX IF NOT EXISTS idx_rpg_char_party ON rpg_characters(party_id);

            CREATE TABLE IF NOT EXISTS rpg_parties (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                leader_user_id INTEGER NOT NULL,
                channel_id INTEGER,
                adventure_id INTEGER,
                current_scene_key TEXT,
                state TEXT NOT NULL DEFAULT 'idle',
                state_json TEXT NOT NULL DEFAULT '{}',
                created_at REAL DEFAULT (unixepoch()),
                updated_at REAL DEFAULT (unixepoch())
            );

            CREATE TABLE IF NOT EXISTS rpg_combat_state (
                party_id INTEGER PRIMARY KEY,
                round INTEGER NOT NULL DEFAULT 1,
                turn_index INTEGER NOT NULL DEFAULT 0,
                initiative_json TEXT NOT NULL DEFAULT '[]',
                enemies_json TEXT NOT NULL DEFAULT '[]',
                scene_key TEXT,
                next_scene_key TEXT,
                updated_at REAL DEFAULT (unixepoch())
            );

            CREATE TABLE IF NOT EXISTS rpg_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                party_id INTEGER NOT NULL,
                ts REAL DEFAULT (unixepoch()),
                kind TEXT NOT NULL,
                content TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_rpg_log_party ON rpg_log(party_id, ts);
        """)
        await self.db.commit()
        await self._seed_rpg_defaults()

    async def _seed_rpg_defaults(self) -> None:
        """Seed the four starter classes if the classes table is empty."""
        import json as _json
        async with self.db.execute("SELECT COUNT(*) FROM rpg_classes") as cur:
            count = (await cur.fetchone())[0]
        if count == 0:
            classes = [
                {
                    "class_key": "warrior",
                    "name": "Warrior",
                    "description": "Stalwart melee fighter with heavy armor and brute force.",
                    "base_hp": 30, "base_attack": 7, "base_defense": 6,
                    "base_agility": 4, "base_mana": 6,
                    "abilities": [
                        {"key": "power_strike", "name": "Power Strike",
                         "description": "A devastating overhead blow.",
                         "mana_cost": 2, "cooldown": 0, "target": "enemy",
                         "effect": {"type": "damage", "bonus_dice": "2d6", "stat": "attack"}},
                        {"key": "shield_wall", "name": "Shield Wall",
                         "description": "Brace for incoming attacks, +5 defense for 2 rounds.",
                         "mana_cost": 3, "cooldown": 3, "target": "self",
                         "effect": {"type": "buff", "stat": "defense", "amount": 5, "duration": 2}},
                        {"key": "cleave", "name": "Cleave",
                         "description": "Sweep your blade through up to two enemies.",
                         "mana_cost": 4, "cooldown": 2, "target": "all_enemies",
                         "effect": {"type": "damage", "bonus_dice": "1d8", "stat": "attack", "max_targets": 2}},
                    ],
                },
                {
                    "class_key": "mage",
                    "name": "Mage",
                    "description": "Arcane scholar wielding elemental magic.",
                    "base_hp": 18, "base_attack": 4, "base_defense": 3,
                    "base_agility": 5, "base_mana": 20,
                    "abilities": [
                        {"key": "fireball", "name": "Fireball",
                         "description": "Hurl a roaring ball of flame.",
                         "mana_cost": 5, "cooldown": 0, "target": "enemy",
                         "effect": {"type": "damage", "bonus_dice": "3d6"}},
                        {"key": "frost_nova", "name": "Frost Nova",
                         "description": "Burst of ice; chance to stun all enemies for 1 round.",
                         "mana_cost": 7, "cooldown": 3, "target": "all_enemies",
                         "effect": {"type": "damage", "bonus_dice": "1d8",
                                    "status": {"name": "stun", "chance": 0.5, "duration": 1}}},
                        {"key": "arcane_shield", "name": "Arcane Shield",
                         "description": "A shimmering ward absorbs the next 8 damage.",
                         "mana_cost": 4, "cooldown": 3, "target": "self",
                         "effect": {"type": "shield", "amount": 8, "duration": 3}},
                    ],
                },
                {
                    "class_key": "rogue",
                    "name": "Rogue",
                    "description": "Swift duelist who strikes from the shadows.",
                    "base_hp": 22, "base_attack": 6, "base_defense": 4,
                    "base_agility": 9, "base_mana": 8,
                    "abilities": [
                        {"key": "backstab", "name": "Backstab",
                         "description": "A precise strike for massive damage.",
                         "mana_cost": 3, "cooldown": 1, "target": "enemy",
                         "effect": {"type": "damage", "bonus_dice": "3d6", "stat": "attack", "crit_bonus": 0.2}},
                        {"key": "poison_strike", "name": "Poison Strike",
                         "description": "Coats blade with venom; poisons target for 3 rounds.",
                         "mana_cost": 2, "cooldown": 0, "target": "enemy",
                         "effect": {"type": "damage", "bonus_dice": "1d4", "stat": "attack",
                                    "status": {"name": "poison", "chance": 1.0, "duration": 3, "damage": 3}}},
                        {"key": "smoke_bomb", "name": "Smoke Bomb",
                         "description": "Party gains +3 evade (defense) for 2 rounds.",
                         "mana_cost": 4, "cooldown": 3, "target": "party",
                         "effect": {"type": "buff", "stat": "defense", "amount": 3, "duration": 2}},
                    ],
                },
                {
                    "class_key": "cleric",
                    "name": "Cleric",
                    "description": "Devout healer channeling divine power.",
                    "base_hp": 24, "base_attack": 5, "base_defense": 6,
                    "base_agility": 4, "base_mana": 15,
                    "abilities": [
                        {"key": "heal", "name": "Heal",
                         "description": "Restore 2d6 HP to an ally (or self).",
                         "mana_cost": 4, "cooldown": 0, "target": "ally",
                         "effect": {"type": "heal", "dice": "2d6"}},
                        {"key": "bless", "name": "Bless",
                         "description": "Grant the party +2 attack for 2 rounds.",
                         "mana_cost": 3, "cooldown": 3, "target": "party",
                         "effect": {"type": "buff", "stat": "attack", "amount": 2, "duration": 2}},
                        {"key": "smite", "name": "Smite",
                         "description": "Channel holy fury to deal 2d8 radiant damage.",
                         "mana_cost": 5, "cooldown": 1, "target": "enemy",
                         "effect": {"type": "damage", "bonus_dice": "2d8"}},
                    ],
                },
            ]
            for c in classes:
                await self.db.execute(
                    """INSERT INTO rpg_classes
                       (class_key, name, description, base_hp, base_attack,
                        base_defense, base_agility, base_mana, abilities_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (c["class_key"], c["name"], c["description"], c["base_hp"],
                     c["base_attack"], c["base_defense"], c["base_agility"],
                     c["base_mana"], _json.dumps(c["abilities"])),
                )

        async with self.db.execute("SELECT COUNT(*) FROM rpg_enemies") as cur:
            ecount = (await cur.fetchone())[0]
        if ecount == 0:
            enemies = [
                ("goblin", "Goblin Scout", "A wiry green raider with a rusty dagger.",
                 14, 4, 3, 6, [], [{"item_key": "gold", "amount": 5, "chance": 1.0}], 8),
                ("wolf", "Dire Wolf", "A massive wolf with bloody fangs.",
                 18, 5, 3, 7, [], [], 10),
                ("skeleton", "Skeleton Warrior", "Rattling bones held together by dark magic.",
                 16, 5, 4, 4, [], [{"item_key": "gold", "amount": 8, "chance": 1.0}], 12),
                ("bandit", "Highway Bandit", "A cutthroat looking for easy coin.",
                 20, 6, 4, 5, [], [{"item_key": "gold", "amount": 12, "chance": 1.0}], 14),
                ("ogre", "Hill Ogre", "Hulking brute with a tree-trunk club.",
                 40, 8, 5, 3, [], [{"item_key": "gold", "amount": 25, "chance": 1.0}], 30),
            ]
            for k, n, d, hp, atk, df, ag, ab, lo, xp in enemies:
                await self.db.execute(
                    """INSERT INTO rpg_enemies (enemy_key, name, description, hp, attack,
                       defense, agility, abilities_json, loot_json, xp_reward)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (k, n, d, hp, atk, df, ag, _json.dumps(ab), _json.dumps(lo), xp),
                )

        async with self.db.execute("SELECT COUNT(*) FROM rpg_items") as cur:
            icount = (await cur.fetchone())[0]
        if icount == 0:
            items = [
                ("gold", "Gold Coins", "Shiny currency.", "currency", {}),
                ("potion_heal_small", "Small Healing Potion",
                 "Restores 2d6 HP when consumed.", "consumable",
                 {"type": "heal", "dice": "2d6"}),
                ("potion_mana_small", "Small Mana Potion",
                 "Restores 2d6 mana when consumed.", "consumable",
                 {"type": "restore_mana", "dice": "2d6"}),
                ("antidote", "Antidote", "Cures poison.", "consumable",
                 {"type": "cure", "status": "poison"}),
            ]
            for k, n, d, t, ef in items:
                await self.db.execute(
                    """INSERT INTO rpg_items (item_key, name, description, item_type, effect_json)
                       VALUES (?, ?, ?, ?, ?)""",
                    (k, n, d, t, _json.dumps(ef)),
                )
        await self.db.commit()

    # =======================================================================
    # RPG — Adventures
    # =======================================================================
    async def rpg_list_adventures(self) -> list[dict]:
        async with self.db.execute(
            "SELECT * FROM rpg_adventures ORDER BY created_at DESC"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def rpg_get_adventure(self, adventure_id: int) -> Optional[dict]:
        async with self.db.execute(
            "SELECT * FROM rpg_adventures WHERE id = ?", (adventure_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def rpg_create_adventure(self, name: str, description: str,
                                   intro_text: str, llm_system_prompt: str,
                                   start_scene_key: str = "") -> int:
        cur = await self.db.execute(
            """INSERT INTO rpg_adventures
               (name, description, intro_text, llm_system_prompt, start_scene_key)
               VALUES (?, ?, ?, ?, ?)""",
            (name, description, intro_text, llm_system_prompt, start_scene_key or None),
        )
        await self.db.commit()
        return cur.lastrowid

    async def rpg_update_adventure(self, adventure_id: int, *, name: str,
                                   description: str, intro_text: str,
                                   llm_system_prompt: str, start_scene_key: str,
                                   is_active: int) -> None:
        await self.db.execute(
            """UPDATE rpg_adventures
               SET name = ?, description = ?, intro_text = ?,
                   llm_system_prompt = ?, start_scene_key = ?, is_active = ?
               WHERE id = ?""",
            (name, description, intro_text, llm_system_prompt,
             start_scene_key or None, is_active, adventure_id),
        )
        await self.db.commit()

    async def rpg_delete_adventure(self, adventure_id: int) -> None:
        await self.db.execute("DELETE FROM rpg_scenes WHERE adventure_id = ?", (adventure_id,))
        await self.db.execute("DELETE FROM rpg_adventures WHERE id = ?", (adventure_id,))
        await self.db.commit()

    async def rpg_import_adventure(self, adv: dict, scenes: list[dict]) -> int:
        """Atomically create an adventure plus all its scenes.

        On any error the transaction is rolled back. Returns the new adventure id.
        """
        try:
            cur = await self.db.execute(
                """INSERT INTO rpg_adventures
                   (name, description, intro_text, llm_system_prompt,
                    start_scene_key, is_active)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    adv["name"],
                    adv.get("description", ""),
                    adv.get("intro_text", ""),
                    adv.get("llm_system_prompt", ""),
                    adv.get("start_scene_key") or None,
                    1 if adv.get("is_active", True) else 0,
                ),
            )
            adv_id = cur.lastrowid
            for s in scenes:
                await self.db.execute(
                    """INSERT INTO rpg_scenes
                       (adventure_id, scene_key, title, narration, scene_type, data_json)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        adv_id,
                        s["scene_key"],
                        s.get("title", ""),
                        s.get("narration", ""),
                        s.get("scene_type", "story"),
                        s.get("data_json", "{}"),
                    ),
                )
            await self.db.commit()
            return adv_id
        except Exception:
            await self.db.rollback()
            raise

    # =======================================================================
    # RPG — Scenes
    # =======================================================================
    async def rpg_list_scenes(self, adventure_id: int) -> list[dict]:
        async with self.db.execute(
            "SELECT * FROM rpg_scenes WHERE adventure_id = ? ORDER BY scene_key",
            (adventure_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def rpg_get_scene(self, scene_id: int) -> Optional[dict]:
        async with self.db.execute(
            "SELECT * FROM rpg_scenes WHERE id = ?", (scene_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def rpg_get_scene_by_key(self, adventure_id: int,
                                   scene_key: str) -> Optional[dict]:
        async with self.db.execute(
            "SELECT * FROM rpg_scenes WHERE adventure_id = ? AND scene_key = ?",
            (adventure_id, scene_key),
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def rpg_create_scene(self, adventure_id: int, scene_key: str, title: str,
                               narration: str, scene_type: str, data_json: str) -> int:
        cur = await self.db.execute(
            """INSERT INTO rpg_scenes
               (adventure_id, scene_key, title, narration, scene_type, data_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (adventure_id, scene_key, title, narration, scene_type, data_json),
        )
        await self.db.commit()
        return cur.lastrowid

    async def rpg_update_scene(self, scene_id: int, *, scene_key: str, title: str,
                               narration: str, scene_type: str, data_json: str) -> None:
        await self.db.execute(
            """UPDATE rpg_scenes
               SET scene_key = ?, title = ?, narration = ?,
                   scene_type = ?, data_json = ?
               WHERE id = ?""",
            (scene_key, title, narration, scene_type, data_json, scene_id),
        )
        await self.db.commit()

    async def rpg_delete_scene(self, scene_id: int) -> None:
        await self.db.execute("DELETE FROM rpg_scenes WHERE id = ?", (scene_id,))
        await self.db.commit()

    # =======================================================================
    # RPG — Classes
    # =======================================================================
    async def rpg_list_classes(self) -> list[dict]:
        async with self.db.execute(
            "SELECT * FROM rpg_classes ORDER BY name"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def rpg_get_class(self, class_key: str) -> Optional[dict]:
        async with self.db.execute(
            "SELECT * FROM rpg_classes WHERE class_key = ?", (class_key,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def rpg_upsert_class(self, *, class_key: str, name: str, description: str,
                               base_hp: int, base_attack: int, base_defense: int,
                               base_agility: int, base_mana: int,
                               abilities_json: str) -> None:
        await self.db.execute(
            """INSERT INTO rpg_classes
               (class_key, name, description, base_hp, base_attack,
                base_defense, base_agility, base_mana, abilities_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(class_key) DO UPDATE SET
                 name = excluded.name,
                 description = excluded.description,
                 base_hp = excluded.base_hp,
                 base_attack = excluded.base_attack,
                 base_defense = excluded.base_defense,
                 base_agility = excluded.base_agility,
                 base_mana = excluded.base_mana,
                 abilities_json = excluded.abilities_json""",
            (class_key, name, description, base_hp, base_attack,
             base_defense, base_agility, base_mana, abilities_json),
        )
        await self.db.commit()

    async def rpg_delete_class(self, class_key: str) -> None:
        await self.db.execute(
            "DELETE FROM rpg_classes WHERE class_key = ?", (class_key,)
        )
        await self.db.commit()

    # =======================================================================
    # RPG — Enemies
    # =======================================================================
    async def rpg_list_enemies(self) -> list[dict]:
        async with self.db.execute(
            "SELECT * FROM rpg_enemies ORDER BY name"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def rpg_get_enemy(self, enemy_key: str) -> Optional[dict]:
        async with self.db.execute(
            "SELECT * FROM rpg_enemies WHERE enemy_key = ?", (enemy_key,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def rpg_upsert_enemy(self, *, enemy_key: str, name: str, description: str,
                               hp: int, attack: int, defense: int, agility: int,
                               abilities_json: str, loot_json: str,
                               xp_reward: int) -> None:
        await self.db.execute(
            """INSERT INTO rpg_enemies
               (enemy_key, name, description, hp, attack, defense, agility,
                abilities_json, loot_json, xp_reward)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(enemy_key) DO UPDATE SET
                 name = excluded.name,
                 description = excluded.description,
                 hp = excluded.hp,
                 attack = excluded.attack,
                 defense = excluded.defense,
                 agility = excluded.agility,
                 abilities_json = excluded.abilities_json,
                 loot_json = excluded.loot_json,
                 xp_reward = excluded.xp_reward""",
            (enemy_key, name, description, hp, attack, defense, agility,
             abilities_json, loot_json, xp_reward),
        )
        await self.db.commit()

    async def rpg_delete_enemy(self, enemy_key: str) -> None:
        await self.db.execute(
            "DELETE FROM rpg_enemies WHERE enemy_key = ?", (enemy_key,)
        )
        await self.db.commit()

    # =======================================================================
    # RPG — Items
    # =======================================================================
    async def rpg_list_items(self) -> list[dict]:
        async with self.db.execute(
            "SELECT * FROM rpg_items ORDER BY name"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def rpg_get_item(self, item_key: str) -> Optional[dict]:
        async with self.db.execute(
            "SELECT * FROM rpg_items WHERE item_key = ?", (item_key,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def rpg_upsert_item(self, *, item_key: str, name: str, description: str,
                              item_type: str, effect_json: str) -> None:
        await self.db.execute(
            """INSERT INTO rpg_items (item_key, name, description, item_type, effect_json)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(item_key) DO UPDATE SET
                 name = excluded.name,
                 description = excluded.description,
                 item_type = excluded.item_type,
                 effect_json = excluded.effect_json""",
            (item_key, name, description, item_type, effect_json),
        )
        await self.db.commit()

    async def rpg_delete_item(self, item_key: str) -> None:
        await self.db.execute(
            "DELETE FROM rpg_items WHERE item_key = ?", (item_key,)
        )
        await self.db.commit()

    # =======================================================================
    # RPG — Characters
    # =======================================================================
    async def rpg_get_character_by_user(self, user_id: int) -> Optional[dict]:
        async with self.db.execute(
            "SELECT * FROM rpg_characters WHERE user_id = ?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def rpg_get_character(self, character_id: int) -> Optional[dict]:
        async with self.db.execute(
            "SELECT * FROM rpg_characters WHERE id = ?", (character_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def rpg_list_characters(self) -> list[dict]:
        async with self.db.execute(
            "SELECT * FROM rpg_characters ORDER BY level DESC, name"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def rpg_create_character(self, *, user_id: int, user_name: str, name: str,
                                   class_key: str, max_hp: int, max_mana: int,
                                   attack: int, defense: int, agility: int) -> int:
        cur = await self.db.execute(
            """INSERT INTO rpg_characters
               (user_id, user_name, name, class_key, level, xp,
                hp, max_hp, mana, max_mana,
                attack, defense, agility)
               VALUES (?, ?, ?, ?, 1, 0, ?, ?, ?, ?, ?, ?, ?)""",
            (user_id, user_name, name, class_key, max_hp, max_hp,
             max_mana, max_mana, attack, defense, agility),
        )
        await self.db.commit()
        return cur.lastrowid

    async def rpg_update_character(self, character_id: int, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [time.time(), character_id]
        await self.db.execute(
            f"UPDATE rpg_characters SET {cols}, updated_at = ? WHERE id = ?",
            vals,
        )
        await self.db.commit()

    async def rpg_delete_character(self, character_id: int) -> None:
        await self.db.execute(
            "DELETE FROM rpg_characters WHERE id = ?", (character_id,)
        )
        await self.db.commit()

    async def rpg_get_party_members(self, party_id: int) -> list[dict]:
        async with self.db.execute(
            "SELECT * FROM rpg_characters WHERE party_id = ? ORDER BY id",
            (party_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    # =======================================================================
    # RPG — Parties
    # =======================================================================
    async def rpg_list_parties(self) -> list[dict]:
        async with self.db.execute(
            "SELECT * FROM rpg_parties ORDER BY created_at DESC"
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def rpg_get_party(self, party_id: int) -> Optional[dict]:
        async with self.db.execute(
            "SELECT * FROM rpg_parties WHERE id = ?", (party_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def rpg_create_party(self, *, name: str, leader_user_id: int,
                               channel_id: Optional[int] = None) -> int:
        cur = await self.db.execute(
            """INSERT INTO rpg_parties (name, leader_user_id, channel_id)
               VALUES (?, ?, ?)""",
            (name, leader_user_id, channel_id),
        )
        await self.db.commit()
        return cur.lastrowid

    async def rpg_update_party(self, party_id: int, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [time.time(), party_id]
        await self.db.execute(
            f"UPDATE rpg_parties SET {cols}, updated_at = ? WHERE id = ?",
            vals,
        )
        await self.db.commit()

    async def rpg_delete_party(self, party_id: int) -> None:
        await self.db.execute(
            "UPDATE rpg_characters SET party_id = NULL WHERE party_id = ?",
            (party_id,),
        )
        await self.db.execute(
            "DELETE FROM rpg_combat_state WHERE party_id = ?", (party_id,)
        )
        await self.db.execute("DELETE FROM rpg_parties WHERE id = ?", (party_id,))
        await self.db.commit()

    # =======================================================================
    # RPG — Combat state
    # =======================================================================
    async def rpg_get_combat(self, party_id: int) -> Optional[dict]:
        async with self.db.execute(
            "SELECT * FROM rpg_combat_state WHERE party_id = ?", (party_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def rpg_set_combat(self, party_id: int, *, round: int, turn_index: int,
                             initiative_json: str, enemies_json: str,
                             scene_key: Optional[str] = None,
                             next_scene_key: Optional[str] = None) -> None:
        await self.db.execute(
            """INSERT INTO rpg_combat_state
               (party_id, round, turn_index, initiative_json, enemies_json,
                scene_key, next_scene_key, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(party_id) DO UPDATE SET
                 round = excluded.round,
                 turn_index = excluded.turn_index,
                 initiative_json = excluded.initiative_json,
                 enemies_json = excluded.enemies_json,
                 scene_key = excluded.scene_key,
                 next_scene_key = excluded.next_scene_key,
                 updated_at = excluded.updated_at""",
            (party_id, round, turn_index, initiative_json, enemies_json,
             scene_key, next_scene_key, time.time()),
        )
        await self.db.commit()

    async def rpg_clear_combat(self, party_id: int) -> None:
        await self.db.execute(
            "DELETE FROM rpg_combat_state WHERE party_id = ?", (party_id,)
        )
        await self.db.commit()

    # =======================================================================
    # RPG — Log
    # =======================================================================
    async def rpg_log_event(self, party_id: int, kind: str, content: str) -> None:
        await self.db.execute(
            "INSERT INTO rpg_log (party_id, kind, content) VALUES (?, ?, ?)",
            (party_id, kind, content),
        )
        await self.db.commit()

    async def rpg_recent_log(self, party_id: int, limit: int = 20) -> list[dict]:
        async with self.db.execute(
            "SELECT * FROM rpg_log WHERE party_id = ? ORDER BY id DESC LIMIT ?",
            (party_id, limit),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        rows.reverse()
        return rows
