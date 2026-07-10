import asyncio
import os
import re
import functools
import math
import time
import bcrypt
import aiohttp
from quart import Quart, render_template, request, redirect, url_for, session, flash
from bot.database import Database

SUNO_URL_PATTERN = re.compile(r'https://suno\.com/(?:s|song)/[\w-]+')
YOUTUBE_URL_RE   = re.compile(
    r'(?:https?://)?(?:www\.)?(?:youtu\.be/|youtube\.com/(?:watch\?v=|embed/|v/|shorts/))'
    r'([A-Za-z0-9_-]{11})'
)
ELEVENMUSIC_TRACK_RE = re.compile(
    r'(?:https?://)?(?:www\.)?elevenmusic\.io/tracks/([A-Fa-f0-9]{24})'
)


def _decode_suno_json_string(value: str) -> str:
    import html as _html
    value = re.sub(r'\\\\u([0-9a-fA-F]{4})',
                   lambda m: chr(int(m.group(1), 16)), value)
    value = re.sub(r'\\u([0-9a-fA-F]{4})',
                   lambda m: chr(int(m.group(1), 16)), value)
    return _html.unescape(
        value.replace(r'\"', '"').replace(r"\/", "/").strip()
    )


def _valid_suno_display_name(name: str | None) -> bool:
    return bool(
        name
        and len(name) > 1
        and not re.match(r"^v\d", name)
        and name not in ("Cover", "Remix")
    )


def _extract_suno_clip_owner_display_name(page: str, song_id: str | None = None) -> str | None:
    id_part = re.escape(song_id) if song_id else r"[a-f0-9-]{8,36}"
    patterns = [
        rf'\\"id\\":\\"{id_part}\\".*?\\"user_id\\":\\"[^"\\]+\\".*?\\"display_name\\":\\"((?:(?!\\").)*)\\"',
        rf'"id"\s*:\s*"{id_part}".*?"user_id"\s*:\s*"[^"]+".*?"display_name"\s*:\s*"((?:[^"\\]|\\.)*)"',
    ]
    for pat in patterns:
        m = re.search(pat, page, re.S)
        if not m:
            continue
        name = _decode_suno_json_string(m.group(1))
        if _valid_suno_display_name(name):
            return name
    return None


async def _rpg_import_enemies(db, raw_json: str, _json) -> tuple[int, list[str]]:
    """Validate + bulk-upsert enemies from a JSON payload.

    Accepts either a JSON list or an object with `{"enemies":[...]}`.
    Returns (count_imported, errors).
    """
    errors: list[str] = []
    if not raw_json:
        return 0, ["JSON input is required."]
    try:
        payload = _json.loads(raw_json)
    except _json.JSONDecodeError as exc:
        return 0, [f"Invalid JSON: {exc.msg} at line {exc.lineno}."]
    if isinstance(payload, dict) and "enemies" in payload:
        items = payload["enemies"]
    elif isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict) and "enemy_key" in payload:
        items = [payload]
    else:
        return 0, ["JSON must be a list, an enemy object, or {\"enemies\":[...]}."]

    count = 0
    for idx, raw in enumerate(items, start=1):
        if not isinstance(raw, dict):
            errors.append(f"Enemy {idx}: must be an object.")
            continue
        key = str(raw.get("enemy_key") or "").strip()
        name = str(raw.get("name") or "").strip()
        if not key or not name:
            errors.append(f"Enemy {idx}: 'enemy_key' and 'name' are required.")
            continue
        ab_raw = raw.get("abilities", raw.get("abilities_json", []))
        loot_raw = raw.get("loot", raw.get("loot_json", []))
        try:
            abilities_json = (ab_raw if isinstance(ab_raw, str)
                              else _json.dumps(ab_raw))
            loot_json = (loot_raw if isinstance(loot_raw, str)
                         else _json.dumps(loot_raw))
            _json.loads(abilities_json)
            _json.loads(loot_json)
        except (TypeError, ValueError, _json.JSONDecodeError) as exc:
            errors.append(f"Enemy {idx} ({key}): invalid abilities/loot JSON ({exc}).")
            continue
        try:
            await db.rpg_upsert_enemy(
                enemy_key=key,
                name=name,
                description=str(raw.get("description") or ""),
                hp=int(raw.get("hp", 15)),
                attack=int(raw.get("attack", 4)),
                defense=int(raw.get("defense", 3)),
                agility=int(raw.get("agility", 4)),
                abilities_json=abilities_json,
                loot_json=loot_json,
                xp_reward=int(raw.get("xp_reward", 10)),
            )
            count += 1
        except Exception as exc:
            errors.append(f"Enemy {idx} ({key}): DB error — {exc}")
    return count, errors


async def _rpg_import_items(db, raw_json: str, _json) -> tuple[int, list[str]]:
    """Validate + bulk-upsert items from a JSON payload.

    Accepts either a JSON list or an object with `{"items":[...]}`.
    """
    errors: list[str] = []
    if not raw_json:
        return 0, ["JSON input is required."]
    try:
        payload = _json.loads(raw_json)
    except _json.JSONDecodeError as exc:
        return 0, [f"Invalid JSON: {exc.msg} at line {exc.lineno}."]
    if isinstance(payload, dict) and "items" in payload:
        items = payload["items"]
    elif isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict) and "item_key" in payload:
        items = [payload]
    else:
        return 0, ["JSON must be a list, an item object, or {\"items\":[...]}."]

    count = 0
    for idx, raw in enumerate(items, start=1):
        if not isinstance(raw, dict):
            errors.append(f"Item {idx}: must be an object.")
            continue
        key = str(raw.get("item_key") or "").strip()
        name = str(raw.get("name") or "").strip()
        if not key or not name:
            errors.append(f"Item {idx}: 'item_key' and 'name' are required.")
            continue
        eff_raw = raw.get("effect", raw.get("effect_json", {}))
        try:
            effect_json = (eff_raw if isinstance(eff_raw, str)
                           else _json.dumps(eff_raw))
            _json.loads(effect_json)
        except (TypeError, ValueError, _json.JSONDecodeError) as exc:
            errors.append(f"Item {idx} ({key}): invalid effect JSON ({exc}).")
            continue
        try:
            await db.rpg_upsert_item(
                item_key=key,
                name=name,
                description=str(raw.get("description") or ""),
                item_type=str(raw.get("item_type") or "misc"),
                effect_json=effect_json,
            )
            count += 1
        except Exception as exc:
            errors.append(f"Item {idx} ({key}): DB error — {exc}")
    return count, errors


def create_app(db: Database, bot=None) -> Quart:
    app = Quart(
        __name__,
        template_folder="templates",
        static_folder="static",
    )
    # Allow larger uploads (PiP videos, radio backgrounds). Default is 16 MB.
    app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024   # 50 MB
    app.config["BODY_TIMEOUT"]       = 120                # 2 min for slow uplinks
    # Session cookies: Secure flag so browsers only send them over HTTPS;
    # SameSite=Lax prevents CSRF while keeping normal navigation working.
    app.config["SESSION_COOKIE_SECURE"]   = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.db = db
    app.bot = bot
    app.scan_status = {"running": False, "progress": "", "result": ""}
    app.title_scan_status = {"running": False, "progress": "", "result": ""}
    app.reaction_scan_status = {"running": False, "progress": "", "result": ""}
    app.cleanup_status = {"running": False, "progress": "", "result": ""}

    @app.template_filter("timestamp_to_date")
    def timestamp_to_date(ts):
        from datetime import datetime, timezone
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%d.%m.%Y")

    # --- Auth helpers ---

    def login_required(f):
        @functools.wraps(f)
        async def decorated(*args, **kwargs):
            if "user_id" not in session:
                return redirect(url_for("login"))
            user = await db.get_web_user_by_id(session["user_id"])
            if not user:
                session.clear()
                return redirect(url_for("login"))
            if user["must_change_password"] and request.endpoint != "change_password":
                return redirect(url_for("change_password"))
            return await f(*args, **kwargs)
        return decorated

    ALL_PERMISSIONS = [
        ('dashboard', 'Dashboard'),
        ('channels', 'Channels'),
        ('roles', 'Roles'),
        ('users', 'Users'),
        ('welcome', 'Welcome'),
        ('party_playlist', 'Party Playlist'),
        ('playlist_search', 'Playlist Search'),
        ('player', 'Suno Player'),
        ('song_stats', 'Song Stats'),
        ('user_stats', 'User Stats'),
        ('reaction_stats', 'Reaction Stats'),
        ('reaction_roles', 'Reaction Roles'),
        ('image_posting', 'Image Posting'),
        ('polls', 'Polls'),
        ('quiz', 'Quiz'),
        ('radio', 'Twitch Radio'),
        ('exp_radio', 'Experimental Radio'),
        ('auto_translate', 'Auto Translate'),
        ('channel_moderation', 'Channel Moderation'),
        ('executioner', 'Executioner'),
        ('suno_analyzer', 'Suno Analyzer'),
        ('suno_promotion', 'Suno Promotion'),
        ('suno_info', 'Suno Info'),
        ('audit', 'Audit Log'),
        ('settings', 'Settings'),
        ('llm', 'Corax Chat (LLM)'),
        ('relic_hunt', "Raven's Nest"),
        ('rpg', 'RPG Adventures'),
    ]

    def admin_required(f):
        @functools.wraps(f)
        async def decorated(*args, **kwargs):
            if "user_id" not in session:
                return redirect(url_for("login"))
            user = await db.get_web_user_by_id(session["user_id"])
            if not user:
                session.clear()
                return redirect(url_for("login"))
            if not user.get("is_admin"):
                await flash("Admin access required.", "error")
                return redirect(url_for("dashboard"))
            return await f(*args, **kwargs)
        return decorated

    def permission_required(perm):
        def decorator(f):
            @functools.wraps(f)
            async def decorated(*args, **kwargs):
                if "user_id" not in session:
                    return redirect(url_for("login"))
                user = await db.get_web_user_by_id(session["user_id"])
                if not user:
                    session.clear()
                    return redirect(url_for("login"))
                if user["must_change_password"] and request.endpoint != "change_password":
                    return redirect(url_for("change_password"))
                # admins always have access
                if user.get("is_admin"):
                    return await f(*args, **kwargs)
                import json
                perms = []
                try:
                    perms = json.loads(user.get("permissions") or "[]")
                except (json.JSONDecodeError, TypeError):
                    pass
                if perm not in perms:
                    await flash("You don't have permission to access this page.", "error")
                    return redirect(url_for("dashboard"))
                return await f(*args, **kwargs)
            return decorated
        return decorator

    def hash_password(password: str) -> str:
        return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    def check_password(password: str, hashed: str) -> bool:
        return bcrypt.checkpw(password.encode(), hashed.encode())

    def get_guild():
        if bot and bot.is_ready():
            from config import Config
            return bot.get_guild(Config.GUILD_ID)
        return None

    async def get_guild_members(guild):
        if not guild:
            return []
        members = list(getattr(guild, "members", []) or [])
        if len(members) <= 1:
            try:
                members = [m async for m in guild.fetch_members(limit=None)]
            except Exception:
                pass
        return sorted(
            [m for m in members if not getattr(m, "bot", False)],
            key=lambda m: ((m.display_name or m.name or "").casefold(), m.id),
        )

    def find_latest_cached_message(bot_obj, user_id: int):
        latest = None
        for message in getattr(bot_obj, "cached_messages", []) or []:
            if getattr(getattr(message, "author", None), "id", None) != user_id:
                continue
            if latest is None or message.created_at > latest.created_at:
                latest = message
        if not latest:
            return None
        channel = getattr(latest, "channel", None)
        return {
            "type": "Message",
            "timestamp": latest.created_at.timestamp(),
            "summary": f"Message in #{getattr(channel, 'name', 'unknown-channel')}",
        }

    # --- Routes ---

    @app.route("/login", methods=["GET", "POST"])
    async def login():
        if request.method == "POST":
            form = await request.form
            username = form.get("username", "").strip()
            password = form.get("password", "")
            user = await db.get_web_user(username)
            if user and check_password(password, user["password_hash"]):
                session["user_id"] = user["id"]
                session["username"] = user["username"]
                session["is_admin"] = bool(user.get("is_admin"))
                import json
                try:
                    session["permissions"] = json.loads(user.get("permissions") or "[]")
                except (json.JSONDecodeError, TypeError):
                    session["permissions"] = []
                if user["must_change_password"]:
                    return redirect(url_for("change_password"))
                return redirect(url_for("dashboard"))
            await flash("Invalid username or password.", "error")
        return await render_template("login.html")

    @app.route("/logout")
    async def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/change-password", methods=["GET", "POST"])
    @login_required
    async def change_password():
        if request.method == "POST":
            form = await request.form
            current = form.get("current_password", "")
            new_pw = form.get("new_password", "")
            confirm = form.get("confirm_password", "")
            user = await db.get_web_user_by_id(session["user_id"])

            if not check_password(current, user["password_hash"]):
                await flash("Current password is incorrect.", "error")
            elif len(new_pw) < 6:
                await flash("New password must be at least 6 characters.", "error")
            elif new_pw != confirm:
                await flash("Passwords do not match.", "error")
            else:
                await db.update_web_user_password(user["id"], hash_password(new_pw))
                await flash("Password changed successfully.", "success")
                return redirect(url_for("dashboard"))
        return await render_template("change_password.html")

    @app.route("/")
    @login_required
    async def dashboard():
        channels = await db.get_monitored_channels()
        exempt_roles = await db.get_exempt_roles()
        command_roles = await db.get_command_roles()
        bot_name = await db.get_setting("bot_name") or "Slowmode Bot"
        guild_id = await db.get_setting("guild_id") or ""
        log_count = await db.get_audit_log_count()

        bot_connected = bot is not None and bot.is_ready()
        guild_name = None
        if bot_connected:
            guild = get_guild()
            if guild:
                guild_name = guild.name

        return await render_template(
            "dashboard.html",
            channels=channels,
            exempt_roles=exempt_roles,
            command_roles=command_roles,
            bot_name=bot_name,
            guild_id=guild_id,
            guild_name=guild_name,
            bot_connected=bot_connected,
            log_count=log_count,
        )

    @app.route("/settings", methods=["GET", "POST"])
    @permission_required('settings')
    async def settings():
        if request.method == "POST":
            form = await request.form
            action = form.get("action", "")

            if action == "lp_add":
                input_channel_id = form.get("input_channel_id", "").strip()
                output_channel_id = form.get("output_channel_id", "").strip()
                time_range = int(form.get("time_range_hours", "24"))
                if not input_channel_id.isdigit() or not output_channel_id.isdigit():
                    await flash("Invalid channel ID.", "error")
                else:
                    input_channel_id = int(input_channel_id)
                    output_channel_id = int(output_channel_id)
                    monitored = await db.get_monitored_channel(input_channel_id)
                    if not monitored:
                        await flash("Input channel must be a monitored channel.", "error")
                    elif input_channel_id == output_channel_id:
                        await flash("Input and output channel must be different.", "error")
                    else:
                        await db.add_listening_party_config(input_channel_id, output_channel_id, time_range)
                        await db.add_audit_log(
                            event_type="listening_party_added",
                            channel_id=input_channel_id,
                            details=f"Listening party config added: input={input_channel_id}, output={output_channel_id}, range={time_range}h",
                            actor=session.get("username", "unknown"),
                        )
                        await flash("Listening party config added.", "success")
                return redirect(url_for("settings"))

            elif action == "lp_update":
                config_id = int(form.get("config_id", "0"))
                output_channel_id = int(form.get("output_channel_id", "0"))
                time_range = int(form.get("time_range_hours", "24"))
                await db.update_listening_party_config(config_id, output_channel_id, time_range)
                await db.add_audit_log(
                    event_type="listening_party_updated",
                    details=f"Config {config_id} updated: output={output_channel_id}, range={time_range}h",
                    actor=session.get("username", "unknown"),
                )
                await flash("Config updated.", "success")
                return redirect(url_for("settings"))

            elif action == "lp_remove":
                config_id = int(form.get("config_id", "0"))
                await db.remove_listening_party_config(config_id)
                await db.add_audit_log(
                    event_type="listening_party_removed",
                    details=f"Config {config_id} removed",
                    actor=session.get("username", "unknown"),
                )
                await flash("Config removed.", "success")
                return redirect(url_for("settings"))

            bot_name = form.get("bot_name", "").strip()
            guild_id = form.get("guild_id", "").strip()
            new_channel = form.get("new_command_channel", "").strip()

            party_max_songs = form.get("party_max_songs", "2").strip()
            party_voice_channel = form.get("party_voice_channel", "").strip()
            player_url = form.get("player_url", "").strip()

            if bot_name:
                await db.set_setting("bot_name", bot_name)
            if guild_id:
                await db.set_setting("guild_id", guild_id)
            await db.set_setting("new_command_channel", new_channel)
            await db.set_setting("party_max_songs", party_max_songs)
            await db.set_setting("party_voice_channel", party_voice_channel)
            await db.set_setting("player_url", player_url)

            await db.add_audit_log(
                event_type="settings_changed",
                details=f"Bot name: {bot_name}, Guild ID: {guild_id}, /new channel: {new_channel}, party_max_songs: {party_max_songs}, party_voice_channel: {party_voice_channel}, player_url: {player_url}",
                actor=session.get("username", "unknown"),
            )
            await flash("Settings saved.", "success")
            return redirect(url_for("settings"))

        bot_name = await db.get_setting("bot_name") or "Slowmode Bot"
        guild_id = await db.get_setting("guild_id") or ""
        new_command_channel = await db.get_setting("new_command_channel") or ""
        party_max_songs = await db.get_setting("party_max_songs") or "2"
        party_voice_channel = await db.get_setting("party_voice_channel") or ""
        player_url = await db.get_setting("player_url") or ""
        monitored = await db.get_monitored_channels()
        guild = get_guild()
        channel_options = []
        for ch in monitored:
            ch_name = f"channel-{ch['channel_id']}"
            if guild:
                gch = guild.get_channel(ch["channel_id"])
                if gch:
                    ch_name = gch.name
            channel_options.append({"id": ch["channel_id"], "name": ch_name})
        # All text + voice channels for party post channel selection
        all_text_channels = []
        if guild:
            for ch in sorted(guild.text_channels, key=lambda c: c.position):
                all_text_channels.append({"id": ch.id, "name": ch.name})
            for ch in sorted(guild.voice_channels, key=lambda c: c.position):
                all_text_channels.append({"id": ch.id, "name": f"🔊 {ch.name}"})
        # Enrich monitored channels with names for LP config form
        for ch in monitored:
            ch["channel_name"] = f"channel-{ch['channel_id']}"
            if guild:
                gch = guild.get_channel(ch["channel_id"])
                if gch:
                    ch["channel_name"] = gch.name
        # Listening party configs
        lp_configs = await db.get_listening_party_configs()
        available_output_channels = []
        if guild:
            for ch in guild.text_channels:
                available_output_channels.append({"id": ch.id, "name": ch.name})
        for cfg in lp_configs:
            cfg["input_name"] = f"channel-{cfg['input_channel_id']}"
            cfg["output_name"] = f"channel-{cfg['output_channel_id']}"
            if guild:
                inch = guild.get_channel(cfg["input_channel_id"])
                if inch:
                    cfg["input_name"] = inch.name
                outch = guild.get_channel(cfg["output_channel_id"])
                if outch:
                    cfg["output_name"] = outch.name

        return await render_template("settings.html", bot_name=bot_name, guild_id=guild_id,
                                     new_command_channel=new_command_channel, channel_options=channel_options,
                                     party_max_songs=party_max_songs, party_voice_channel=party_voice_channel,
                                     player_url=player_url,
                                     all_text_channels=all_text_channels,
                                     monitored_channels=monitored,
                                     available_output_channels=available_output_channels,
                                     lp_configs=lp_configs)

    @app.route("/welcome", methods=["GET", "POST"])
    @permission_required('welcome')
    async def welcome():
        if request.method == "POST":
            form = await request.form
            enabled = form.get("enabled") == "1"
            dm_enabled = form.get("dm_enabled") == "1"
            channel_id = form.get("channel_id", "").strip()
            message_text = form.get("message_text", "").strip()
            dm_text = form.get("dm_text", "").strip()

            # Convert channel_id to int or None
            channel_id_int = int(channel_id) if channel_id.isdigit() else None

            await db.set_welcome_config(
                enabled=enabled,
                channel_id=channel_id_int,
                message_text=message_text if message_text else None,
                dm_enabled=dm_enabled,
                dm_text=dm_text if dm_text else None,
            )

            await db.add_audit_log(
                event_type="welcome_config_updated",
                details=f"enabled={enabled}, channel={channel_id_int}, dm_enabled={dm_enabled}",
                actor=session.get("username", "unknown"),
            )
            await flash("Welcome configuration saved.", "success")
            return redirect(url_for("welcome"))

        config = await db.get_welcome_config()
        guild = get_guild()
        channels = []
        if guild:
            for ch in sorted(guild.text_channels, key=lambda c: c.position):
                channels.append({"id": ch.id, "name": ch.name})

        return await render_template("welcome.html", config=config, channels=channels)

    @app.route("/channels", methods=["GET", "POST"])
    @permission_required('channels')
    async def channels():
        if request.method == "POST":
            form = await request.form
            action = form.get("action")

            if action == "add":
                channel_id = form.get("channel_id", "").strip()
                cooldown = int(form.get("cooldown_minutes", "0"))

                if not channel_id.isdigit():
                    await flash("Invalid channel ID.", "error")
                else:
                    channel_id = int(channel_id)
                    channel_name = f"channel-{channel_id}"

                    guild = get_guild()
                    if guild:
                        ch = guild.get_channel(channel_id)
                        if ch:
                            channel_name = ch.name

                    await db.add_monitored_channel(channel_id, channel_name, cooldown)
                    await db.add_audit_log(
                        event_type="channel_added",
                        channel_id=channel_id,
                        channel_name=channel_name,
                        details=f"Added with {cooldown}min cooldown",
                        actor=session.get("username", "unknown"),
                    )
                    await flash(f"Channel #{channel_name} added.", "success")

            elif action == "update":
                channel_id = int(form.get("channel_id", "0"))
                cooldown = int(form.get("cooldown_minutes", "0"))
                await db.update_channel_cooldown(channel_id, cooldown)
                await db.add_audit_log(
                    event_type="channel_updated",
                    channel_id=channel_id,
                    details=f"Cooldown updated to {cooldown}min",
                    actor=session.get("username", "unknown"),
                )
                await flash("Channel updated.", "success")

            elif action == "toggle":
                channel_id = int(form.get("channel_id", "0"))
                enabled = form.get("enabled") == "1"
                await db.toggle_channel(channel_id, enabled)
                await flash("Channel toggled.", "success")

            elif action == "remove":
                channel_id = int(form.get("channel_id", "0"))
                await db.remove_monitored_channel(channel_id)
                await db.add_audit_log(
                    event_type="channel_removed",
                    channel_id=channel_id,
                    details="Channel removed",
                    actor=session.get("username", "unknown"),
                )
                await flash("Channel removed.", "success")

            elif action == "reset_user_cooldown":
                channel_id = int(form.get("channel_id", "0"))
                user_id = int(form.get("user_id", "0"))
                await db.clear_cooldown_record(user_id, channel_id)
                await db.add_audit_log(
                    event_type="cooldown_reset",
                    user_id=user_id,
                    channel_id=channel_id,
                    details=f"Cooldown manually reset via web interface",
                    actor=session.get("username", "unknown"),
                )
                await flash("User cooldown reset.", "success")

            return redirect(url_for("channels"))

        channel_list = await db.get_monitored_channels()

        guild = get_guild()
        available_channels = []
        if guild:
            monitored_ids = {c["channel_id"] for c in channel_list}
            for ch in guild.text_channels:
                if ch.id not in monitored_ids:
                    available_channels.append({"id": ch.id, "name": ch.name})

        channel_cooldowns = {}
        for ch in channel_list:
            if ch["cooldown_minutes"] > 0:
                records = await db.get_active_cooldowns(ch["channel_id"], ch["cooldown_minutes"])
                users = []
                for r in records:
                    elapsed = time.time() - r["timestamp"]
                    remaining = (ch["cooldown_minutes"] * 60) - elapsed
                    if remaining > 0:
                        user_name = f"User {r['user_id']}"
                        if guild:
                            member = guild.get_member(r["user_id"])
                            if member:
                                user_name = str(member)
                        hours = remaining / 3600
                        if hours >= 1:
                            time_str = f"{math.ceil(hours)}h remaining"
                        else:
                            time_str = f"{math.ceil(remaining / 60)}min remaining"
                        users.append({
                            "user_id": r["user_id"],
                            "user_name": user_name,
                            "time_remaining": time_str,
                        })
                channel_cooldowns[ch["channel_id"]] = users

        return await render_template(
            "channels.html",
            channels=channel_list,
            available_channels=available_channels,
            channel_cooldowns=channel_cooldowns,
        )

    @app.route("/roles", methods=["GET", "POST"])
    @permission_required('roles')
    async def roles():
        if request.method == "POST":
            form = await request.form
            action = form.get("action")
            role_type = form.get("role_type", "exempt")

            if action == "add":
                role_id = form.get("role_id", "").strip()
                if not role_id.isdigit():
                    await flash("Invalid role ID.", "error")
                else:
                    role_id = int(role_id)
                    role_name = f"role-{role_id}"

                    guild = get_guild()
                    if guild:
                        r = guild.get_role(role_id)
                        if r:
                            role_name = r.name

                    if role_type == "exempt":
                        await db.add_exempt_role(role_id, role_name)
                    else:
                        await db.add_command_role(role_id, role_name)

                    await db.add_audit_log(
                        event_type=f"{role_type}_role_added",
                        details=f"Role {role_name} ({role_id}) added as {role_type}",
                        actor=session.get("username", "unknown"),
                    )
                    await flash(f"Role {role_name} added.", "success")

            elif action == "remove":
                role_id = int(form.get("role_id", "0"))
                if role_type == "exempt":
                    await db.remove_exempt_role(role_id)
                else:
                    await db.remove_command_role(role_id)
                await db.add_audit_log(
                    event_type=f"{role_type}_role_removed",
                    details=f"Role {role_id} removed from {role_type}",
                    actor=session.get("username", "unknown"),
                )
                await flash("Role removed.", "success")

            return redirect(url_for("roles"))

        exempt_roles = await db.get_exempt_roles()
        command_roles = await db.get_command_roles()

        guild = get_guild()
        available_roles = []
        if guild:
            exempt_ids = {r["role_id"] for r in exempt_roles}
            command_ids = {r["role_id"] for r in command_roles}
            for r in guild.roles:
                if r.id != guild.default_role.id:
                    available_roles.append({
                        "id": r.id,
                        "name": r.name,
                        "is_exempt": r.id in exempt_ids,
                        "is_command": r.id in command_ids,
                    })

        return await render_template(
            "roles.html",
            exempt_roles=exempt_roles,
            command_roles=command_roles,
            available_roles=available_roles,
        )

    @app.route("/users", methods=["GET", "POST"])
    @permission_required('users')
    async def users():
        if request.method == "POST":
            form = await request.form
            action = form.get("action")

            if action == "add":
                username = form.get("username", "").strip()
                password = form.get("password", "").strip()
                if len(username) < 3:
                    await flash("Username must be at least 3 characters.", "error")
                elif len(password) < 6:
                    await flash("Password must be at least 6 characters.", "error")
                else:
                    success = await db.create_web_user(username, hash_password(password))
                    if success:
                        await db.add_audit_log(
                            event_type="user_created",
                            details=f"Web user '{username}' created",
                            actor=session.get("username", "unknown"),
                        )
                        await flash(f"User '{username}' created.", "success")
                    else:
                        await flash("Username already exists.", "error")

            elif action == "delete":
                user_id = int(form.get("user_id", "0"))
                if user_id == session.get("user_id"):
                    await flash("You cannot delete yourself.", "error")
                else:
                    target = await db.get_web_user_by_id(user_id)
                    if target:
                        await db.delete_web_user(user_id)
                        await db.add_audit_log(
                            event_type="user_deleted",
                            details=f"Web user '{target['username']}' deleted",
                            actor=session.get("username", "unknown"),
                        )
                        await flash("User deleted.", "success")

            elif action == "reset_password":
                user_id = int(form.get("user_id", "0"))
                new_pw = form.get("new_password", "").strip()
                if len(new_pw) < 6:
                    await flash("Password must be at least 6 characters.", "error")
                else:
                    await db.update_web_user_password(user_id, hash_password(new_pw))
                    # Force password change on next login
                    await db.db.execute(
                        "UPDATE web_users SET must_change_password = 1 WHERE id = ?",
                        (user_id,),
                    )
                    await db.db.commit()
                    await flash("Password reset. User must change it on next login.", "success")

            elif action == "set_permissions":
                user_id = int(form.get("user_id", "0"))
                target = await db.get_web_user_by_id(user_id)
                if target and not target.get("is_admin"):
                    import json
                    selected = [k for k, _ in ALL_PERMISSIONS if form.get(f"perm_{k}")]
                    await db.set_user_permissions(user_id, selected)
                    await db.add_audit_log(
                        event_type="permissions_updated",
                        details=f"Permissions for '{target['username']}' set to: {', '.join(selected) or 'none'}",
                        actor=session.get("username", "unknown"),
                    )
                    await flash(f"Permissions updated for '{target['username']}'.", "success")

            return redirect(url_for("users"))

        user_list = await db.get_all_web_users()
        import json
        for u in user_list:
            try:
                u["perms"] = json.loads(u.get("permissions") or "[]")
            except (json.JSONDecodeError, TypeError):
                u["perms"] = []
        return await render_template("users.html", users=user_list, current_user_id=session.get("user_id"), all_permissions=ALL_PERMISSIONS)

    @app.route("/executioner", methods=["GET", "POST"])
    @permission_required('executioner')
    async def executioner():
        guild = get_guild()
        selected_user_id = request.args.get("user_id", type=int)

        if request.method == "POST":
            form = await request.form
            raw_user_id = (form.get("user_id") or "").strip()
            reason = (form.get("reason") or "").strip()

            if not raw_user_id.isdigit():
                await flash("Please select a user.", "error")
                return redirect(url_for("executioner"))
            if not reason:
                await flash("A reason is required.", "error")
                return redirect(url_for("executioner", user_id=raw_user_id))
            if not guild:
                await flash("The bot is not connected to the configured server.", "error")
                return redirect(url_for("executioner", user_id=raw_user_id))

            user_id = int(raw_user_id)
            member = guild.get_member(user_id)
            if not member:
                try:
                    member = await guild.fetch_member(user_id)
                except Exception:
                    member = None
            if not member:
                await flash("The selected user is not a current server member.", "error")
                return redirect(url_for("executioner"))

            bot_member = guild.me
            if not bot_member or not bot_member.guild_permissions.kick_members:
                await flash("The bot does not have permission to kick members.", "error")
                return redirect(url_for("executioner", user_id=user_id))
            if member == guild.owner:
                await flash("The server owner cannot be kicked.", "error")
                return redirect(url_for("executioner", user_id=user_id))
            if member.top_role >= bot_member.top_role:
                await flash("The bot cannot kick this user because of the role hierarchy.", "error")
                return redirect(url_for("executioner", user_id=user_id))

            actor = session.get("username", "unknown")
            audit_reason = f"{reason} (requested by {actor} via Admin UI)"
            try:
                await member.kick(reason=audit_reason[:512])
            except Exception as exc:
                await flash(f"Kick failed: {exc}", "error")
                return redirect(url_for("executioner", user_id=user_id))

            await db.add_audit_log(
                event_type="user_kicked",
                user_id=user_id,
                user_name=str(member),
                details=f"Reason: {reason}",
                actor=actor,
            )
            await flash(f"{member} was kicked.", "success")
            return redirect(url_for("executioner"))

        members = await get_guild_members(guild)
        latest_activity = None
        if selected_user_id:
            latest_activity = await db.get_latest_user_activity(selected_user_id)
            cached_message = find_latest_cached_message(bot, selected_user_id) if bot else None
            if cached_message and (
                not latest_activity
                or cached_message["timestamp"] > float(latest_activity.get("timestamp") or 0)
            ):
                latest_activity = cached_message

        return await render_template(
            "executioner.html",
            members=members,
            selected_user_id=selected_user_id,
            latest_activity=latest_activity,
            bot_connected=bool(guild),
        )

    @app.route("/audit")
    @permission_required('audit')
    async def audit():
        page = int(request.args.get("page", 1))
        per_page = 50
        offset = (page - 1) * per_page
        logs = await db.get_audit_logs(limit=per_page, offset=offset)
        total = await db.get_audit_log_count()
        total_pages = max(1, (total + per_page - 1) // per_page)

        return await render_template(
            "audit.html",
            logs=logs,
            page=page,
            total_pages=total_pages,
            total=total,
        )

    # --- Channel Moderation ---

    @app.route("/channel-moderation", methods=["GET", "POST"])
    @permission_required('channel_moderation')
    async def channel_moderation():
        if request.method == "POST":
            form = await request.form
            action = form.get("action", "")
            if action == "save_settings":
                enabled = "on" if form.get("enabled") else "off"
                report_ch = (form.get("report_channel_id") or "").strip()
                await db.set_setting("channel_moderation_enabled", enabled)
                await db.set_setting("channel_moderation_report_channel", report_ch)
                await flash("Channel moderation settings saved.", "success")
            elif action == "clear_log":
                await db.db.execute("DELETE FROM channel_moderation_log")
                await db.db.commit()
                await flash("Moderation log cleared.", "success")
            return redirect(url_for("channel_moderation"))

        enabled = await db.get_setting("channel_moderation_enabled") or "off"
        report_ch_id = await db.get_setting("channel_moderation_report_channel") or ""
        verdict_filter = (request.args.get("verdict") or "").strip().lower()
        if verdict_filter not in ("flagged", "passed", "pending", "skipped", "error"):
            verdict_filter = ""
        log_rows = await db.get_channel_moderation_log(
            limit=200, verdict=verdict_filter or None,
        )
        from datetime import datetime as _dt
        for r in log_rows:
            try:
                r["created_pretty"] = _dt.fromtimestamp(float(r["created_at"])).strftime("%d.%m. %H:%M")
            except Exception:
                r["created_pretty"] = ""

        # Channel pickers: monitored channels (source list, read-only display)
        # + all text channels for the report-channel dropdown.
        monitored = await db.get_monitored_channels()
        guild = get_guild()
        text_channels = []
        monitored_resolved = []
        if guild:
            import discord as _discord
            for ch in guild.channels:
                if isinstance(ch, _discord.TextChannel):
                    text_channels.append({"id": ch.id, "name": ch.name})
            text_channels.sort(key=lambda c: c["name"].lower())
            for m in monitored:
                ch = guild.get_channel(m["channel_id"])
                monitored_resolved.append({
                    "id": m["channel_id"],
                    "name": ch.name if ch else m.get("channel_name") or str(m["channel_id"]),
                    "enabled": bool(m.get("enabled")),
                })

        return await render_template(
            "channel_moderation.html",
            enabled=enabled,
            report_ch_id=report_ch_id,
            log_rows=log_rows,
            verdict_filter=verdict_filter,
            text_channels=text_channels,
            monitored=monitored_resolved,
        )

    # --- Quiz ---

    @app.route("/quiz", methods=["GET", "POST"])
    @permission_required('quiz')
    async def quiz_admin():
        def _category_key(value):
            value = re.sub(r"[^a-z0-9]+", "_", (value or "").strip().lower())
            return value.strip("_")

        categories = await db.get_quiz_categories()
        category_keys = {category["key"] for category in categories}

        def _clean_quiz_form(form):
            mode = (form.get("mode") or "").strip().lower()
            question = (form.get("question") or "").strip()
            answers = [
                (form.get(f"answer_{idx}") or "").strip()
                for idx in range(1, 6)
            ]
            correct_answer = (form.get("correct_answer") or "").strip()
            return mode, question, answers, correct_answer

        if request.method == "POST":
            form = await request.form
            action = form.get("action", "")

            if action == "add_category":
                name = (form.get("category_name") or "").strip()
                key = _category_key(form.get("category_key") or name)
                if not name or not key:
                    await flash("Category name is required.", "error")
                elif key == "mixed":
                    await flash("'mixed' is reserved for all categories.", "error")
                elif key in category_keys:
                    await flash("A category with this key already exists.", "error")
                else:
                    try:
                        await db.create_quiz_category(key, name)
                        await flash(f"Category '{name}' created.", "success")
                    except Exception:
                        await flash("Category name or key already exists.", "error")
                return redirect(url_for("quiz_admin"))

            if action == "delete_category":
                key = (form.get("category_key") or "").strip().lower()
                category = await db.get_quiz_category(key)
                if not category:
                    await flash("Category not found.", "error")
                elif not await db.delete_quiz_category(key):
                    await flash(
                        "This category still contains questions and cannot be deleted.",
                        "error",
                    )
                else:
                    if (await db.get_setting("quiz_mode") or "") == key:
                        await db.set_setting("quiz_mode", "mixed")
                    await flash(f"Category '{category['name']}' deleted.", "success")
                return redirect(url_for("quiz_admin"))

            if action == "save_settings":
                mode = (form.get("quiz_mode") or "mixed").strip().lower()
                if mode != "mixed" and mode not in category_keys:
                    mode = "mixed"
                channel_id = (form.get("quiz_channel_id") or "").strip()
                await db.set_setting("quiz_mode", mode)
                await db.set_setting("quiz_channel_id", channel_id)
                await flash("Quiz settings saved.", "success")
                return redirect(url_for("quiz_admin"))

            if action in ("create", "edit"):
                mode, question, answers, correct_answer = _clean_quiz_form(form)
                if mode not in category_keys:
                    await flash("Please select a valid quiz category.", "error")
                    return redirect(url_for("quiz_admin"))
                if not question:
                    await flash("Question is required.", "error")
                    return redirect(url_for("quiz_admin"))
                if any(not answer for answer in answers):
                    await flash("All five answers are required.", "error")
                    return redirect(url_for("quiz_admin"))
                if not correct_answer:
                    await flash("Correct answer is required.", "error")
                    return redirect(url_for("quiz_admin"))
                if correct_answer not in answers:
                    await flash("Correct answer must match one of the five answers exactly.", "error")
                    return redirect(url_for("quiz_admin"))

                if action == "create":
                    question_id = await db.create_quiz_question(mode, question, answers, correct_answer)
                    await db.add_audit_log(
                        event_type="quiz_question_created",
                        details=f"Quiz question #{question_id} created for mode={mode}",
                        actor=session.get("username", "unknown"),
                    )
                    await flash("Quiz question created.", "success")
                else:
                    question_id = int(form.get("question_id") or 0)
                    await db.update_quiz_question(question_id, mode, question, answers, correct_answer)
                    await db.add_audit_log(
                        event_type="quiz_question_updated",
                        details=f"Quiz question #{question_id} updated for mode={mode}",
                        actor=session.get("username", "unknown"),
                    )
                    await flash("Quiz question updated.", "success")
                return redirect(url_for("quiz_admin"))

            if action == "import_json":
                import json as _json
                raw_json = (form.get("quiz_json") or "").strip()
                if not raw_json:
                    await flash("JSON input is required.", "error")
                    return redirect(url_for("quiz_admin"))

                try:
                    payload = _json.loads(raw_json)
                except _json.JSONDecodeError as exc:
                    await flash(f"Invalid JSON: {exc.msg} at line {exc.lineno}.", "error")
                    return redirect(url_for("quiz_admin"))

                items = payload.get("questions") if isinstance(payload, dict) else payload
                if not isinstance(items, list):
                    await flash("JSON must be an array or an object with a questions array.", "error")
                    return redirect(url_for("quiz_admin"))

                prepared = []
                errors = []
                for idx, item in enumerate(items, start=1):
                    if not isinstance(item, dict):
                        errors.append(f"Item {idx}: expected an object.")
                        continue
                    mode = str(
                        item.get("category") or item.get("mode") or ""
                    ).strip().lower()
                    if mode not in category_keys:
                        errors.append(
                            f"Item {idx}: unknown category '{mode or '(empty)'}'."
                        )
                        continue
                    question = str(item.get("question") or "").strip()
                    answers = item.get("answers")
                    if not question:
                        errors.append(f"Item {idx}: question is required.")
                        continue
                    if not isinstance(answers, list) or len(answers) != 5:
                        errors.append(f"Item {idx}: answers must contain exactly 5 entries.")
                        continue
                    answers = [str(answer).strip() for answer in answers]
                    if any(not answer for answer in answers):
                        errors.append(f"Item {idx}: all answers must be non-empty.")
                        continue

                    correct_answer = str(
                        item.get("correct_answer")
                        or item.get("correctAnswer")
                        or ""
                    ).strip()
                    correct_index = item.get("correct_index", item.get("correctIndex"))
                    if not correct_answer and correct_index is not None:
                        try:
                            correct_idx = int(correct_index)
                            if 1 <= correct_idx <= 5:
                                correct_answer = answers[correct_idx - 1]
                        except (TypeError, ValueError):
                            pass
                    if not correct_answer:
                        errors.append(f"Item {idx}: correct_answer or correct_index is required.")
                        continue
                    if correct_answer not in answers:
                        errors.append(f"Item {idx}: correct answer must match one of the answers exactly.")
                        continue
                    prepared.append((mode, question, answers, correct_answer))

                if errors:
                    preview = " ".join(errors[:5])
                    if len(errors) > 5:
                        preview += f" ... and {len(errors) - 5} more."
                    await flash(preview, "error")
                    return redirect(url_for("quiz_admin"))

                if not prepared:
                    await flash("No quiz questions found in the JSON input.", "error")
                    return redirect(url_for("quiz_admin"))

                imported_count = await db.create_quiz_questions_bulk(prepared)

                await db.add_audit_log(
                    event_type="quiz_questions_imported",
                    details=f"Imported {imported_count} quiz question(s) from JSON",
                    actor=session.get("username", "unknown"),
                )
                await flash(f"Imported {imported_count} quiz question(s).", "success")
                return redirect(url_for("quiz_admin"))

            if action == "delete":
                question_id = int(form.get("question_id") or 0)
                await db.delete_quiz_question(question_id)
                await db.add_audit_log(
                    event_type="quiz_question_deleted",
                    details=f"Quiz question #{question_id} deleted",
                    actor=session.get("username", "unknown"),
                )
                await flash("Quiz question deleted.", "success")
                return redirect(url_for("quiz_admin"))

        categories = await db.get_quiz_categories()
        category_keys = {category["key"] for category in categories}
        category_names = {
            category["key"]: category["name"] for category in categories
        }
        quiz_mode = await db.get_setting("quiz_mode") or "mixed"
        if quiz_mode != "mixed" and quiz_mode not in category_keys:
            quiz_mode = "mixed"
        quiz_channel_id = await db.get_setting("quiz_channel_id") or ""
        questions = await db.get_quiz_questions()
        for question in questions:
            question["answers"] = [
                question["answer_1"],
                question["answer_2"],
                question["answer_3"],
                question["answer_4"],
                question["answer_5"],
            ]
            question["category_name"] = category_names.get(
                question["mode"], question["mode"]
            )

        guild = get_guild()
        text_channels = []
        if guild:
            for ch in sorted(guild.text_channels, key=lambda c: c.position):
                text_channels.append({"id": ch.id, "name": ch.name})

        return await render_template(
            "quiz.html",
            quiz_mode=quiz_mode,
            quiz_channel_id=quiz_channel_id,
            questions=questions,
            categories=categories,
            text_channels=text_channels,
        )

    # --- LLM / Corax chat ---

    @app.route("/llm", methods=["GET", "POST"])
    @permission_required('llm')
    async def llm():
        import json as _json
        from bot.llm import DEFAULT_PERSONA, AVAILABLE_TOOL_NAMES

        if request.method == "POST":
            form = await request.form
            action = form.get("action", "save")

            if action == "purge_audit":
                cfg = await db.get_llm_config()
                days = int((cfg or {}).get("retention_days") or 30)
                deleted = await db.purge_llm_audit_log(days)
                await flash(f"{deleted} audit entries purged.", "success")
                return redirect(url_for("llm"))

            if action == "reset_persona":
                await db.update_llm_config(persona=DEFAULT_PERSONA)
                await flash("Persona auf Default zurückgesetzt.", "success")
                return redirect(url_for("llm"))

            # save main config
            def _i(key, default):
                try:
                    return int(form.get(key, default))
                except Exception:
                    return default

            tools_selected = [
                name for name in AVAILABLE_TOOL_NAMES
                if form.get(f"tool_{name}")
            ]
            await db.update_llm_config(
                enabled=1 if form.get("enabled") else 0,
                model=(form.get("model") or "qwen2.5:7b-instruct").strip()[:64],
                tools_model=(form.get("tools_model") or "").strip()[:64],
                persona=(form.get("persona") or "").strip()[:16000],
                retention_days=max(1, min(365, _i("retention_days", 30))),
                rate_per_user_min=max(1, min(60, _i("rate_per_user_min", 3))),
                rate_per_channel_min=max(1, min(500, _i("rate_per_channel_min", 10))),
                max_tokens=max(64, min(2048, _i("max_tokens", 512))),
                default_result_limit=max(1, min(25, _i("default_result_limit", 10))),
                tools_enabled=_json.dumps(tools_selected),
            )

            # channels
            channel_ids = form.getlist("channels")
            guild = get_guild()
            ch_entries = []
            for cid in channel_ids:
                try:
                    cid_i = int(cid)
                except Exception:
                    continue
                cname = f"channel-{cid_i}"
                if guild:
                    ch = guild.get_channel(cid_i)
                    if ch:
                        cname = ch.name
                ch_entries.append((cid_i, cname))
            await db.set_llm_allowed_channels(ch_entries)

            # roles
            role_ids = form.getlist("roles")
            role_entries = []
            for rid in role_ids:
                try:
                    rid_i = int(rid)
                except Exception:
                    continue
                rname = f"role-{rid_i}"
                if guild:
                    r = guild.get_role(rid_i)
                    if r:
                        rname = r.name
                role_entries.append((rid_i, rname))
            await db.set_llm_allowed_roles(role_entries)

            await db.add_audit_log(
                event_type="llm_config_updated",
                actor=session.get("username", "unknown"),
                details=(
                    f"enabled={bool(form.get('enabled'))}, "
                    f"channels={len(ch_entries)}, roles={len(role_entries)}, "
                    f"tools={tools_selected}"
                ),
            )
            await flash("Corax-Einstellungen gespeichert.", "success")
            return redirect(url_for("llm"))

        cfg = await db.get_llm_config()
        try:
            tools_enabled = set(_json.loads(cfg.get("tools_enabled") or "[]"))
        except Exception:
            tools_enabled = set()
        allowed_channels = await db.get_llm_allowed_channels()
        allowed_role_ids = await db.get_llm_allowed_role_ids()
        allowed_channel_ids = {c["channel_id"] for c in allowed_channels}

        guild = get_guild()
        guild_channels = []
        guild_roles = []
        if guild:
            for ch in sorted(guild.text_channels, key=lambda c: c.name.lower()):
                guild_channels.append({"id": ch.id, "name": ch.name})
            for r in sorted(guild.roles, key=lambda r: r.name.lower()):
                if r.is_default():
                    continue
                guild_roles.append({"id": r.id, "name": r.name})

        # fall back to stored names for any channels/roles not visible in guild
        known_ids = {c["id"] for c in guild_channels}
        for c in allowed_channels:
            if c["channel_id"] not in known_ids:
                guild_channels.append({"id": c["channel_id"], "name": c.get("channel_name") or f"channel-{c['channel_id']}"})
        known_role_ids = {r["id"] for r in guild_roles}
        for r in await db.get_llm_allowed_roles():
            if r["role_id"] not in known_role_ids:
                guild_roles.append({"id": r["role_id"], "name": r.get("role_name") or f"role-{r['role_id']}"})

        audit_log = await db.get_llm_audit_log(limit=100)

        return await render_template(
            "llm.html",
            cfg=cfg,
            default_persona=DEFAULT_PERSONA,
            tools_all=sorted(AVAILABLE_TOOL_NAMES),
            tools_enabled=tools_enabled,
            guild_channels=guild_channels,
            guild_roles=guild_roles,
            allowed_channel_ids=allowed_channel_ids,
            allowed_role_ids=allowed_role_ids,
            audit_log=audit_log,
        )

    async def _get_player_channels():
        guild = get_guild()
        channels = []
        monitored = await db.get_monitored_channels()
        for ch in monitored:
            ch_name = f"channel-{ch['channel_id']}"
            if guild:
                gch = guild.get_channel(ch["channel_id"])
                if gch:
                    ch_name = gch.name
            channels.append({"id": ch["channel_id"], "name": ch_name})
        return channels

    @app.route("/player")
    @permission_required('player')
    async def player():
        return await render_template("player.html", channels=await _get_player_channels())

    # --- Public player (no login required) ---
    @app.route("/public/player")
    async def player_public():
        return await render_template("player_public.html", channels=await _get_player_channels())

    @app.route("/public/api/player-songs")
    async def api_player_songs_public():
        from quart import jsonify
        channel_id = request.args.get("channel_id", "").strip()
        limit = min(int(request.args.get("limit", "200")), 500)
        try:
            offset = max(0, int(request.args.get("offset", "0")))
        except (TypeError, ValueError):
            offset = 0
        ch_id = int(channel_id) if channel_id.isdigit() else None
        songs = await db.get_player_songs(channel_id=ch_id, limit=limit, offset=offset)
        return jsonify(songs)

    async def _resolve_suno_short_url(short_id: str):
        """Resolve a Suno short ID to a full song UUID. Returns uuid str or None."""
        import aiohttp as _aiohttp, re as _re
        print(f"[suno-resolve] Resolving short_id={short_id}", flush=True)
        try:
            async with _aiohttp.ClientSession() as sess:
                async with sess.get(
                    f"https://suno.com/s/{short_id}",
                    allow_redirects=True,
                    timeout=_aiohttp.ClientTimeout(total=10),
                ) as resp:
                    final_url = str(resp.url)
                    print(f"[suno-resolve] HTTP {resp.status} final_url={final_url}", flush=True)
                    # Direct song redirect
                    m = _re.search(r'/song/([a-f0-9-]{36})', final_url)
                    if m:
                        print(f"[suno-resolve] Resolved via URL song match: {m.group(1)}", flush=True)
                        return m.group(1)
                    html = await resp.text()
                    print(f"[suno-resolve] HTML length={len(html)}, snippet={html[:200]!r}", flush=True)
                    # Hook redirect — Suno hooks are client-side rendered;
                    # the parent song UUID is NOT in the initial HTML.
                    # We can only succeed if Suno ever embeds a /song/ link.
                    hook_m = _re.search(r'/hook/([a-f0-9-]{36})', final_url)
                    if hook_m:
                        hook_uuid = hook_m.group(1)
                        print(f"[suno-resolve] Hook detected: {hook_uuid} — searching for /song/ link", flush=True)
                        parent_m = _re.search(r'/song/([a-f0-9-]{36})', html)
                        if parent_m:
                            print(f"[suno-resolve] Parent song via /song/ in HTML: {parent_m.group(1)}", flush=True)
                            return parent_m.group(1)
                        print(f"[suno-resolve] Hook: no /song/ link found — cannot resolve", flush=True)
                        return None
                    # CDN audio URL in HTML
                    m = _re.search(r'cdn[12]\.suno\.ai/([a-f0-9-]{36})\.mp3', html)
                    if m:
                        print(f"[suno-resolve] Resolved via CDN URL in HTML: {m.group(1)}", flush=True)
                        return m.group(1)
                    m = _re.search(r'"audio_url"\s*:\s*"[^"]*?([a-f0-9-]{36})\.mp3"', html)
                    if m:
                        print(f"[suno-resolve] Resolved via audio_url in HTML: {m.group(1)}", flush=True)
                        return m.group(1)
                    print(f"[suno-resolve] Could not resolve {short_id} — no patterns matched", flush=True)
        except Exception as e:
            print(f"[suno-resolve] Exception for {short_id}: {e}", flush=True)
        return None

    async def _resolve_suno_hook_uuid(hook_uuid: str):
        """Fetch a Suno hook page and return the parent song UUID, or None."""
        import aiohttp as _aiohttp, re as _re
        print(f"[suno-hook] Resolving hook_uuid={hook_uuid}", flush=True)
        try:
            headers = {"User-Agent": "Mozilla/5.0 (compatible; Bot/1.0)"}
            async with _aiohttp.ClientSession(headers=headers) as sess:
                async with sess.get(
                    f"https://suno.com/hook/{hook_uuid}",
                    allow_redirects=True,
                    timeout=_aiohttp.ClientTimeout(total=10),
                ) as resp:
                    print(f"[suno-hook] HTTP {resp.status} for {hook_uuid}", flush=True)
                    if resp.status != 200:
                        return None
                    html = await resp.text()
                    print(f"[suno-hook] HTML length={len(html)} — searching for /song/ link", flush=True)
                    m = _re.search(r'/song/([a-f0-9-]{36})', html)
                    if m:
                        print(f"[suno-hook] Found parent via /song/: {m.group(1)}", flush=True)
                        return m.group(1)
                    print(f"[suno-hook] No /song/ link in hook HTML — cannot resolve {hook_uuid}", flush=True)
        except Exception as e:
            print(f"[suno-hook] Exception for {hook_uuid}: {e}", flush=True)
        return None

    @app.route("/public/api/suno-resolve/<short_id>")
    async def api_suno_resolve_public(short_id):
        from quart import jsonify
        uuid = await _resolve_suno_short_url(short_id)
        if uuid:
            return jsonify({"uuid": uuid})
        return jsonify({"uuid": None}), 404

    @app.route("/public/api/suno-hook/<hook_uuid>")
    async def api_suno_hook_resolve_public(hook_uuid):
        from quart import jsonify
        uuid = await _resolve_suno_hook_uuid(hook_uuid)
        if uuid:
            return jsonify({"uuid": uuid})
        return jsonify({"uuid": None}), 404

    @app.route("/public/api/suno-lyrics/<uuid>")
    async def api_suno_lyrics_public(uuid):
        from quart import jsonify
        return jsonify(await _fetch_suno_meta(uuid))

    @app.route("/public/api/verify-songs", methods=["POST"])
    async def api_verify_songs_public():
        """Public mirror — verify messages using DB-stored channel_id (never client-provided)."""
        from quart import jsonify
        import discord as _discord
        data = await request.get_json(silent=True) or {}
        items = data.get("items") or []
        # Hard cap batch size to limit DoS potential
        if len(items) > 100:
            items = items[:100]
        if not bot or not bot.is_ready():
            return jsonify({"missing": [], "ok": False, "reason": "bot_not_ready"})
        guild = get_guild()
        if not guild:
            return jsonify({"missing": [], "ok": False, "reason": "no_guild"})

        async def check(item):
            try:
                mid = int(item.get("message_id"))
            except (TypeError, ValueError):
                return None
            # Look up channel_id from DB — never trust client input for deletion decisions
            row = await db.get_song_post_by_message_id(mid)
            if not row:
                return None  # unknown to us, don't mark as missing
            cid = row.get("channel_id")
            if cid is None:
                return None
            ch = guild.get_channel(int(cid))
            if ch is None:
                return mid  # channel gone
            try:
                await ch.fetch_message(mid)
                return None
            except _discord.NotFound:
                return mid
            except Exception:
                return None

        sem = asyncio.Semaphore(5)
        async def bounded(i):
            async with sem:
                return await check(i)
        results = await asyncio.gather(*[bounded(i) for i in items])
        missing = [r for r in results if r is not None]
        for mid in missing:
            try:
                await db.delete_song_posts_by_message_id(mid)
            except Exception:
                pass
        return jsonify({"missing": [str(m) for m in missing], "ok": True})

    @app.route("/api/player-songs")
    @login_required
    async def api_player_songs():
        from quart import jsonify
        channel_id = request.args.get("channel_id", "").strip()
        limit = min(int(request.args.get("limit", "200")), 500)
        offset = max(int(request.args.get("offset", "0")), 0)
        ch_id = int(channel_id) if channel_id.isdigit() else None
        songs = await db.get_player_songs(channel_id=ch_id, limit=limit, offset=offset)
        return jsonify(songs)

    @app.route("/api/player-react", methods=["POST"])
    @login_required
    async def api_player_react():
        from quart import jsonify
        data = await request.get_json()
        if not data:
            return jsonify({"error": "No data"}), 400
        message_id = data.get("message_id")
        channel_id = data.get("channel_id")
        song_url = data.get("song_url", "")
        post_author_id = data.get("post_author_id")
        emoji = data.get("emoji", "")
        song_title = data.get("song_title")
        if not message_id or not emoji:
            return jsonify({"error": "Missing message_id or emoji"}), 400
        reactor_user_id = session.get("user_id", 0)
        reactor_user_name = session.get("username", "web-user")
        await db.add_song_reaction(
            message_id=int(message_id),
            channel_id=int(channel_id or 0),
            song_url=song_url,
            post_author_id=int(post_author_id) if post_author_id else None,
            reactor_user_id=int(reactor_user_id),
            reactor_user_name=reactor_user_name,
            emoji=emoji,
            song_title=song_title,
        )
        # Also add the reaction on the actual Discord message via the bot
        discord_ok = False
        if bot and bot.is_ready() and channel_id:
            try:
                guild = get_guild()
                if guild:
                    ch = guild.get_channel(int(channel_id))
                    if ch:
                        msg = await ch.fetch_message(int(message_id))
                        await msg.add_reaction(emoji)
                        discord_ok = True
            except Exception as e:
                print(f"[player-react] Failed to add Discord reaction: {e}")
        return jsonify({"ok": True, "discord": discord_ok})

    @app.route("/api/suno-resolve/<short_id>")
    @login_required
    async def api_suno_resolve(short_id):
        """Server-side proxy to resolve Suno short URLs to full UUIDs."""
        from quart import jsonify
        uuid = await _resolve_suno_short_url(short_id)
        if uuid:
            return jsonify({"uuid": uuid})
        return jsonify({"uuid": None, "error": "Could not resolve"}), 404

    @app.route("/api/suno-hook/<hook_uuid>")
    @login_required
    async def api_suno_hook_resolve(hook_uuid):
        """Resolve a Suno hook UUID to its parent song UUID."""
        from quart import jsonify
        uuid = await _resolve_suno_hook_uuid(hook_uuid)
        if uuid:
            return jsonify({"uuid": uuid})
        return jsonify({"uuid": None, "error": "Parent song not found"}), 404

    _RSC_REF_RE = re.compile(r'^\$[0-9a-f]+$')

    async def _fetch_suno_meta(uuid):
        """Shared helper to fetch song metadata from Suno embed page."""
        import aiohttp, re, html as _html
        lyrics = title = image_url = artist = video_url = handle = None
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(
                    f"https://suno.com/embed/{uuid}",
                    timeout=aiohttp.ClientTimeout(total=10),
                    headers={"User-Agent": "Mozilla/5.0"},
                ) as resp:
                    html = await resp.text()

                    # Title + Artist from <title>: "Song Title by Artist | Suno"
                    m = re.search(r'<title>(.+?)\s*\|\s*Suno</title>', html)
                    if m:
                        raw = m.group(1).strip()
                        parts = raw.rsplit(' by ', 1)
                        if len(parts) == 2:
                            title = parts[0].strip()
                            artist = parts[1].strip()
                        else:
                            title = raw

                    # Fallback title from og:title
                    if not title:
                        m = re.search(r'<meta\s+property="og:title"\s+content="([^"]*)"', html)
                        if m:
                            title = m.group(1).strip()

                    # Image from og:image
                    m = re.search(r'<meta\s+property="og:image"\s+content="([^"]*)"', html)
                    if m:
                        image_url = m.group(1).strip()
                    if not image_url:
                        m = re.search(r'"image_url"\s*:\s*"([^"]*)"', html)
                        if m:
                            image_url = m.group(1)

                    # Artist fallback from display_name in JSON
                    if not artist:
                        artist = _extract_suno_clip_owner_display_name(html, uuid)
                    if not artist:
                        matches = re.findall(r'display_name\\":\\"([^"\\]+)\\"', html)
                        for dn in reversed(matches):
                            dn = _decode_suno_json_string(dn)
                            if _valid_suno_display_name(dn):
                                artist = dn
                                break

                    # Lyrics from prompt field
                    def _valid_lyrics(s):
                        if not s or len(s.strip()) < 10:
                            return False
                        if re.match(r'^\$\w+$', s.strip()):
                            return False
                        return True

                    idx = html.find(uuid)
                    if idx > -1:
                        chunk = html[max(0, idx-500):idx+5000]
                        m = re.search(r'"prompt"\s*:\s*"((?:[^"\\]|\\.)*)"', chunk)
                        if m:
                            candidate = m.group(1).replace("\\n", "\n").replace('\\"', '"')
                            if _valid_lyrics(candidate):
                                lyrics = candidate
                    if not lyrics:
                        m = re.search(r'prompt\\":\\"((?:[^\\]|\\.)*?)\\"', html)
                        if m:
                            candidate = m.group(1).replace("\\\\n", "\n").replace('\\\\"', '"').replace("\\n", "\n")
                            if _valid_lyrics(candidate):
                                lyrics = candidate

                    # RSC-reference lyrics: longer prompts (or those containing
                    # emojis / non-ASCII chars that Next.js splits out) are stored
                    # in a separate flight chunk like `3d:T1aef,<content>` and
                    # the song JSON only carries `"prompt":"$3d"`. Resolve that.
                    if not lyrics or _RSC_REF_RE.match(lyrics or ""):
                        try:
                            import json as _json
                            chunks = re.findall(
                                r'self\.__next_f\.push\(\[1,"(.*?)"\]\)',
                                html, re.DOTALL,
                            )
                            if chunks:
                                # Each chunk is a JS string literal — decode via JSON.
                                decoded = []
                                for c in chunks:
                                    try:
                                        decoded.append(_json.loads('"' + c + '"'))
                                    except Exception:
                                        decoded.append(
                                            c.replace('\\n', '\n')
                                             .replace('\\"', '"')
                                             .replace('\\\\', '\\')
                                        )
                                full = "".join(decoded)
                                mref = re.search(
                                    r'"prompt":"\$([0-9a-f]+)"', full
                                )
                                if mref:
                                    ref = mref.group(1)
                                    # RSC text chunks carry their exact length
                                    # in hex right after the `T`, e.g.
                                    # `3d:T1aef,<bytes of content>`. The hex
                                    # value is the **UTF-8 byte length** of
                                    # the payload, so we have to slice on a
                                    # bytes view — slicing on the decoded
                                    # str would over-capture (and bleed into
                                    # the next chunk) for emoji / special
                                    # characters.
                                    full_bytes = full.encode("utf-8")
                                    tpat_b = re.compile(
                                        rb'(?:^|\n)' + re.escape(ref).encode() +
                                        rb':T([0-9a-f]+),'
                                    )
                                    tfnd = tpat_b.search(full_bytes)
                                    if tfnd:
                                        length = int(tfnd.group(1), 16)
                                        b_start = tfnd.end()
                                        candidate = full_bytes[b_start:b_start + length] \
                                            .decode("utf-8", errors="replace").rstrip()
                                        if _valid_lyrics(candidate):
                                            lyrics = candidate
                        except Exception:
                            pass

                    # Video cover (user-uploaded video cover, typically 9:16)
                    m = re.search(r'"video_cover_url"\s*:\s*"([^"]+)"', html)
                    if not m:
                        m = re.search(r'video_cover_url\\":\\"([^"\\]+)\\"', html)
                    if m:
                        video_url = m.group(1).replace("\\/", "/")

                    # Handle (artist URL slug) — first occurrence is the song owner
                    m = re.search(r'handle\\":\\"([^"\\]+)\\"', html)
                    if not m:
                        m = re.search(r'"handle"\s*:\s*"([^"]+)"', html)
                    if m:
                        handle = m.group(1)
        except Exception:
            pass
        if title:
            title = _html.unescape(title)
        if artist:
            artist = _html.unescape(artist)
        return {"lyrics": lyrics, "title": title, "image_url": image_url, "artist": artist, "video_url": video_url, "handle": handle}

    async def _fetch_elevenmusic_meta(track_id: str):
        """Fetch public ElevenMusic track metadata from the rendered track page."""
        import html as _html
        import json as _json

        url = f"https://elevenmusic.io/tracks/{track_id}"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "Chrome/126 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        result = {
            "uuid": track_id,
            "provider": "elevenmusic",
            "source_url": url,
            "title": track_id[:8],
            "artist": "",
            "image_url": "",
            "video_url": None,
            "audio_url": f"https://media.elevenmusic.io/tracks/{track_id}/hls/master.m3u8",
            "lyrics": "",
            "prompt": "",
            "tags": [],
            "model": "ElevenMusic",
            "type": "elevenmusic",
            "plays": None,
            "likes": None,
            "created_at": "",
            "duration": None,
            "handle": None,
        }

        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(
                    url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=12),
                ) as resp:
                    if resp.status != 200:
                        return result
                    page = await resp.text()
        except Exception:
            return result

        def _first(pattern):
            m = re.search(pattern, page, re.S)
            if not m:
                return ""
            return _html.unescape(m.group(1).replace(r"\/", "/").strip())

        og_title = _first(r'<meta\s+property="og:title"\s+content="([^"]+)"')
        if og_title:
            og_title = re.sub(r"\s*\|\s*ElevenMusic\s*$", "", og_title).strip()
            parts = og_title.rsplit(" - ", 1)
            if len(parts) == 2:
                result["title"], result["artist"] = parts[0].strip(), parts[1].strip()
            else:
                result["title"] = og_title

        image_url = _first(r'<meta\s+property="og:image"\s+content="([^"]+)"')
        if image_url:
            result["image_url"] = image_url

        audio_url = _first(r'<meta\s+property="og:audio"\s+content="([^"]+)"')
        if audio_url:
            result["audio_url"] = audio_url

        description = _first(r'<meta\s+property="og:description"\s+content="([^"]*)"')
        if description:
            result["prompt"] = description

        for raw in re.findall(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', page, re.S):
            try:
                data = _json.loads(_html.unescape(raw))
            except Exception:
                continue
            if isinstance(data, dict) and data.get("@type") == "MusicRecording":
                result["title"] = data.get("name") or result["title"]
                artist_data = data.get("byArtist") or {}
                if isinstance(artist_data, dict):
                    result["artist"] = artist_data.get("name") or result["artist"]
                if isinstance(data.get("image"), str):
                    result["image_url"] = data["image"]
                genres = data.get("genre")
                if isinstance(genres, list):
                    result["tags"] = [str(g) for g in genres if g]
                elif isinstance(genres, str) and genres:
                    result["tags"] = [genres]
                dur = data.get("duration") or ""
                m = re.match(r"PT(?:(\d+)M)?(?:(\d+)S)?", dur)
                if m:
                    result["duration"] = int(m.group(1) or 0) * 60 + int(m.group(2) or 0)
                break

        if result["duration"] is None:
            m = re.search(r'"duration_ms"\s*:\s*(\d+)', page)
            if m:
                result["duration"] = int(m.group(1)) / 1000

        if not result["artist"]:
            artist = _first(r'"display_name"\s*:\s*"([^"\\]+)"')
            if artist:
                result["artist"] = artist

        title = _first(r'"title"\s*:\s*"([^"\\]+)"')
        if title and result["title"] == track_id[:8]:
            result["title"] = title

        animated = re.search(
            r'"track"\s*:\s*\{.*?"id"\s*:\s*"' + re.escape(track_id) +
            r'".*?"cover"\s*:\s*\{.*?"animated"\s*:\s*\{.*?"url"\s*:\s*"([^"\\]+)"',
            page,
            re.S,
        )
        if animated:
            result["video_url"] = _html.unescape(animated.group(1).replace(r"\/", "/"))

        lyric_lines = []
        for line in re.findall(r'<p[^>]*class="[^"]*text-lg[^"]*"[^>]*>(.*?)</p>', page, re.S):
            clean = re.sub(r"<[^>]+>", "", line)
            clean = _html.unescape(clean).strip()
            if clean and clean not in lyric_lines:
                lyric_lines.append(clean)
        if lyric_lines:
            result["lyrics"] = "\n".join(lyric_lines)

        return result

    @app.route("/api/suno-lyrics/<uuid>")
    @login_required
    async def api_suno_lyrics(uuid):
        from quart import jsonify
        return jsonify(await _fetch_suno_meta(uuid))

    @app.route("/api/verify-songs", methods=["POST"])
    @login_required
    async def api_verify_songs():
        """Verify messages using DB-stored channel_id (never client-provided)."""
        from quart import jsonify
        import discord as _discord
        data = await request.get_json(silent=True) or {}
        items = data.get("items") or []
        if len(items) > 500:
            items = items[:500]
        if not bot or not bot.is_ready():
            return jsonify({"missing": [], "ok": False, "reason": "bot_not_ready"})
        guild = get_guild()
        if not guild:
            return jsonify({"missing": [], "ok": False, "reason": "no_guild"})

        async def check(item):
            try:
                mid = int(item.get("message_id"))
            except (TypeError, ValueError):
                return None
            row = await db.get_song_post_by_message_id(mid)
            if not row:
                return None
            cid = row.get("channel_id")
            if cid is None:
                return None
            ch = guild.get_channel(int(cid))
            if ch is None:
                return mid  # channel gone
            try:
                await ch.fetch_message(mid)
                return None
            except _discord.NotFound:
                return mid
            except Exception:
                return None

        # Limit concurrency to avoid rate-limit bursts
        sem = asyncio.Semaphore(5)
        async def bounded(i):
            async with sem:
                return await check(i)
        results = await asyncio.gather(*[bounded(i) for i in items])
        missing = [r for r in results if r is not None]
        for mid in missing:
            try:
                await db.delete_song_posts_by_message_id(mid)
            except Exception:
                pass
        return jsonify({"missing": [str(m) for m in missing], "ok": True})

    @app.route("/listening-party", methods=["GET", "POST"])
    @permission_required('party_playlist')
    async def listening_party():
        if request.method == "POST":
            form = await request.form
            action = form.get("action")

            if action == "add":
                input_channel_id = form.get("input_channel_id", "").strip()
                output_channel_id = form.get("output_channel_id", "").strip()
                time_range = int(form.get("time_range_hours", "24"))

                if not input_channel_id.isdigit() or not output_channel_id.isdigit():
                    await flash("Invalid channel ID.", "error")
                else:
                    input_channel_id = int(input_channel_id)
                    output_channel_id = int(output_channel_id)

                    monitored = await db.get_monitored_channel(input_channel_id)
                    if not monitored:
                        await flash("Input channel must be a monitored channel.", "error")
                    elif input_channel_id == output_channel_id:
                        await flash("Input and output channel must be different.", "error")
                    else:
                        await db.add_listening_party_config(input_channel_id, output_channel_id, time_range)
                        await db.add_audit_log(
                            event_type="listening_party_added",
                            channel_id=input_channel_id,
                            details=f"Listening party config added: input={input_channel_id}, output={output_channel_id}, range={time_range}h",
                            actor=session.get("username", "unknown"),
                        )
                        await flash("Listening party config added.", "success")

            elif action == "update":
                config_id = int(form.get("config_id", "0"))
                output_channel_id = int(form.get("output_channel_id", "0"))
                time_range = int(form.get("time_range_hours", "24"))
                await db.update_listening_party_config(config_id, output_channel_id, time_range)
                await db.add_audit_log(
                    event_type="listening_party_updated",
                    details=f"Config {config_id} updated: output={output_channel_id}, range={time_range}h",
                    actor=session.get("username", "unknown"),
                )
                await flash("Config updated.", "success")

            elif action == "remove":
                config_id = int(form.get("config_id", "0"))
                await db.remove_listening_party_config(config_id)
                await db.add_audit_log(
                    event_type="listening_party_removed",
                    details=f"Config {config_id} removed",
                    actor=session.get("username", "unknown"),
                )
                await flash("Config removed.", "success")

            return redirect(url_for("listening_party"))

        configs = await db.get_listening_party_configs()

        guild = get_guild()
        monitored_channels = await db.get_monitored_channels()
        available_output_channels = []
        if guild:
            for ch in guild.text_channels:
                available_output_channels.append({"id": ch.id, "name": ch.name})

        # Resolve channel names
        for cfg in configs:
            cfg["input_name"] = f"channel-{cfg['input_channel_id']}"
            cfg["output_name"] = f"channel-{cfg['output_channel_id']}"
            if guild:
                inch = guild.get_channel(cfg["input_channel_id"])
                if inch:
                    cfg["input_name"] = inch.name
                outch = guild.get_channel(cfg["output_channel_id"])
                if outch:
                    cfg["output_name"] = outch.name

        return await render_template(
            "listening_party.html",
            configs=configs,
            monitored_channels=monitored_channels,
            available_output_channels=available_output_channels,
        )

    @app.route("/playlist-search", methods=["GET", "POST"])
    @permission_required('playlist_search')
    async def playlist_search():
        if request.method == "POST":
            form = await request.form
            action = form.get("action")

            if action == "add":
                channel_id = form.get("channel_id", "").strip()
                if not channel_id.isdigit():
                    await flash("Invalid channel ID.", "error")
                else:
                    channel_id = int(channel_id)
                    await db.add_playlist_search_channel(channel_id)
                    channel_name = f"channel-{channel_id}"
                    guild = get_guild()
                    if guild:
                        ch = guild.get_channel(channel_id)
                        if ch:
                            channel_name = ch.name
                    await db.add_audit_log(
                        event_type="playlist_search_added",
                        channel_id=channel_id,
                        channel_name=channel_name,
                        details="Playlist search channel added",
                        actor=session.get("username", "unknown"),
                    )
                    await flash(f"Channel #{channel_name} added for playlist search.", "success")

            elif action == "remove":
                config_id = int(form.get("config_id", "0"))
                await db.remove_playlist_search_channel(config_id)
                await db.add_audit_log(
                    event_type="playlist_search_removed",
                    details=f"Playlist search config {config_id} removed",
                    actor=session.get("username", "unknown"),
                )
                await flash("Channel removed.", "success")

            return redirect(url_for("playlist_search"))

        configs = await db.get_playlist_search_channels()

        guild = get_guild()
        available_channels = []
        if guild:
            existing_ids = {c["channel_id"] for c in configs}
            for ch in guild.text_channels:
                if ch.id not in existing_ids:
                    available_channels.append({"id": ch.id, "name": ch.name})

        # Resolve channel names
        for cfg in configs:
            cfg["channel_name"] = f"channel-{cfg['channel_id']}"
            if guild:
                ch = guild.get_channel(cfg["channel_id"])
                if ch:
                    cfg["channel_name"] = ch.name

        return await render_template(
            "playlist_search.html",
            configs=configs,
            available_channels=available_channels,
        )

    async def _run_scan(actor: str):
        """Background task to scan all monitored channels for Suno URLs."""
        app.scan_status["running"] = True
        app.scan_status["progress"] = "Starting scan..."
        app.scan_status["result"] = ""
        try:
            guild = get_guild()
            if not guild:
                app.scan_status["result"] = "Bot not connected to guild."
                return

            channels = await db.get_monitored_channels()
            total_found = 0

            for i, ch_cfg in enumerate(channels):
                channel = guild.get_channel(ch_cfg["channel_id"])
                if not channel:
                    continue

                app.scan_status["progress"] = f"Scanning #{channel.name} ({i+1}/{len(channels)})..."
                rows = []
                msg_count = 0
                try:
                    async for message in channel.history(limit=None):
                        if message.author.bot:
                            continue
                        msg_count += 1
                        if msg_count % 500 == 0:
                            app.scan_status["progress"] = f"Scanning #{channel.name} ({i+1}/{len(channels)})... {msg_count} messages"
                        urls = SUNO_URL_PATTERN.findall(message.content)
                        for url in urls:
                            rows.append((
                                channel.id,
                                message.author.id,
                                str(message.author),
                                url,
                                message.created_at.timestamp(),
                                message.id,
                            ))
                except Exception as e:
                    app.scan_status["progress"] = f"Error scanning #{channel.name}: {e}"
                    continue

                if rows:
                    await db.add_song_posts_bulk(rows)
                    total_found += len(rows)

            app.scan_status["result"] = f"Scan complete. {total_found} song(s) found across {len(channels)} channel(s)."
            await db.add_audit_log(
                event_type="song_scan",
                details=f"History scan: {total_found} songs found",
                actor=actor,
            )
        except Exception as e:
            app.scan_status["result"] = f"Scan failed: {e}"
        finally:
            app.scan_status["running"] = False
            app.scan_status["progress"] = ""

    async def _run_orphan_cleanup():
        """Background task: verify each song_post message exists on Discord, delete orphans."""
        import discord as _discord
        app.cleanup_status["running"] = True
        app.cleanup_status["progress"] = "Loading song posts..."
        app.cleanup_status["result"] = ""
        try:
            guild = get_guild()
            if not guild:
                app.cleanup_status["result"] = "Bot not connected to guild."
                return
            posts = await db.get_song_posts_with_message_id()
            total = len(posts)
            if not total:
                app.cleanup_status["result"] = "No song posts with message IDs to verify."
                return
            # Group by channel for efficiency
            by_channel: dict[int, list[int]] = {}
            for p in posts:
                by_channel.setdefault(int(p["channel_id"]), []).append(int(p["message_id"]))
            removed = 0
            checked = 0
            errors = 0
            for ch_id, msg_ids in by_channel.items():
                channel = guild.get_channel(ch_id)
                if channel is None:
                    # Channel gone — remove all its posts
                    for mid in msg_ids:
                        await db.delete_song_posts_by_message_id(mid)
                        removed += 1
                        checked += 1
                    continue
                for mid in msg_ids:
                    checked += 1
                    try:
                        await channel.fetch_message(mid)
                    except _discord.NotFound:
                        await db.delete_song_posts_by_message_id(mid)
                        removed += 1
                    except _discord.Forbidden:
                        errors += 1
                    except Exception:
                        errors += 1
                    if checked % 25 == 0:
                        app.cleanup_status["progress"] = (
                            f"Verified {checked}/{total} — removed {removed} orphans"
                        )
                    # Small sleep to avoid rate limit bursts
                    await asyncio.sleep(0.05)
            app.cleanup_status["result"] = (
                f"Done. Verified {checked} song posts, removed {removed} orphans"
                + (f", {errors} errors" if errors else "") + "."
            )
            await db.add_audit_log(
                event_type="song_cleanup",
                details=f"Orphan cleanup: {removed}/{checked} removed",
                actor="system",
            )
        except Exception as e:
            app.cleanup_status["result"] = f"Cleanup failed: {e}"
        finally:
            app.cleanup_status["running"] = False
            app.cleanup_status["progress"] = ""

    @app.route("/song-stats", methods=["GET", "POST"])
    @permission_required('song_stats')
    async def song_stats():
        if request.method == "POST":
            form = await request.form
            action = form.get("action")

            if action == "scan":
                if app.scan_status["running"]:
                    await flash("A scan is already in progress.", "error")
                elif bot and bot.is_ready():
                    actor = session.get("username", "unknown")
                    asyncio.get_event_loop().create_task(_run_scan(actor))
                    await flash("Scan started in the background. Refresh this page to see progress.", "success")
                else:
                    await flash("Bot is not ready.", "error")
            elif action == "cleanup_orphans":
                if app.cleanup_status["running"]:
                    await flash("A cleanup is already in progress.", "error")
                elif bot and bot.is_ready():
                    asyncio.get_event_loop().create_task(_run_orphan_cleanup())
                    await flash("Orphan cleanup started. Refresh this page to see progress.", "success")
                else:
                    await flash("Bot is not ready.", "error")
            elif action == "delete_all_songs":
                count = await db.delete_all_songs()
                print(f"[song-stats] Deleted all {count} song posts")

            return redirect(url_for("song_stats"))

        # GET: gather stats
        import traceback
        try:
            guild = get_guild()
            monitored = await db.get_monitored_channels()

            # Per-channel stats overview
            channel_totals = await db.get_song_stats_all_channels()
            channel_map = {}
            for ct in channel_totals:
                channel_map[ct["channel_id"]] = ct["count"]

            channel_list = []
            for ch in monitored:
                ch_name = f"channel-{ch['channel_id']}"
                if guild:
                    gch = guild.get_channel(ch["channel_id"])
                    if gch:
                        ch_name = gch.name
                channel_list.append({
                    "channel_id": ch["channel_id"],
                    "channel_name": ch_name,
                    "count": channel_map.get(ch["channel_id"], 0),
                })

            # Selected channel filter
            filter_channel = request.args.get("channel", type=int)
            stats = await db.get_song_stats(channel_id=filter_channel)

            return await render_template(
                "song_stats.html",
                channel_list=channel_list,
                stats=stats,
                filter_channel=filter_channel,
                scan_status=app.scan_status,
                cleanup_status=app.cleanup_status,
            )
        except Exception as e:
            traceback.print_exc()
            return f"<pre>Error: {e}\n\n{traceback.format_exc()}</pre>", 500

    @app.route("/user-stats", methods=["GET", "POST"])
    @permission_required('user_stats')
    async def user_stats():
        if request.method == "POST":
            form = await request.form
            action = form.get("action")
            if action == "delete_all_songs":
                count = await db.delete_all_songs()
                print(f"[user-stats] Deleted all {count} song posts")
            return redirect(url_for("user_stats"))

        import traceback
        try:
            guild = get_guild()
            selected_user_id = request.args.get("user_id", type=int)

            # Leaderboard
            ranking = await db.get_all_users_ranking()

            # Resolve display names from guild
            for entry in ranking:
                entry["display_name"] = entry["user_name"] or f"User {entry['user_id']}"
                if guild:
                    member = guild.get_member(entry["user_id"])
                    if member:
                        entry["display_name"] = member.display_name

            # User detail stats
            user_detail = None
            user_display_name = None
            if selected_user_id:
                user_detail = await db.get_user_song_stats(selected_user_id)
                user_display_name = f"User {selected_user_id}"
                if guild:
                    member = guild.get_member(selected_user_id)
                    if member:
                        user_display_name = member.display_name

                # Resolve channel names in per_channel
                if user_detail and user_detail["per_channel"]:
                    for pc in user_detail["per_channel"]:
                        pc["channel_name"] = f"channel-{pc['channel_id']}"
                        if guild:
                            ch = guild.get_channel(pc["channel_id"])
                            if ch:
                                pc["channel_name"] = ch.name

            return await render_template(
                "user_stats.html",
                ranking=ranking,
                user_detail=user_detail,
                user_display_name=user_display_name,
                selected_user_id=selected_user_id,
            )
        except Exception as e:
            traceback.print_exc()
            return f"<pre>Error: {e}\n\n{traceback.format_exc()}</pre>", 500

    import re as _re
    _CUSTOM_EMOJI_RE = _re.compile(r'<a?:(\w+):\d+>')

    @app.template_filter('format_emoji')
    def format_emoji_filter(emoji_str):
        m = _CUSTOM_EMOJI_RE.match(emoji_str)
        if m:
            return f":{m.group(1)}:"
        return emoji_str

    async def _run_title_scan():
        """Background task to fetch song titles from Discord embeds for reactions missing them."""
        import discord as _discord
        app.title_scan_status["running"] = True
        app.title_scan_status["progress"] = "Starting title scan..."
        app.title_scan_status["result"] = ""
        try:
            guild = get_guild()
            if not guild:
                app.title_scan_status["result"] = "Bot not connected to guild."
                return

            missing = await db.get_reactions_missing_titles()
            total = len(missing)
            updated = 0
            skipped = 0

            for i, item in enumerate(missing):
                if (i + 1) % 10 == 0 or i == 0:
                    app.title_scan_status["progress"] = f"Fetching titles... {i+1}/{total} messages"

                channel = guild.get_channel(item["channel_id"])
                if not channel:
                    skipped += 1
                    continue

                try:
                    message = await channel.fetch_message(item["message_id"])
                    title = None
                    for embed in message.embeds:
                        if embed.title:
                            title = embed.title
                            break
                    if title:
                        await db.update_song_title(item["message_id"], title)
                        updated += 1
                    else:
                        skipped += 1
                except (_discord.NotFound, _discord.Forbidden):
                    skipped += 1
                except Exception:
                    skipped += 1

            app.title_scan_status["result"] = f"Done. {updated} titles updated, {skipped} skipped (of {total} messages)."
        except Exception as e:
            app.title_scan_status["result"] = f"Title scan failed: {e}"
        finally:
            app.title_scan_status["running"] = False
            app.title_scan_status["progress"] = ""

    async def _run_reaction_scan():
        """Background task to backfill reactions from Discord message history."""
        import discord as _discord
        app.reaction_scan_status["running"] = True
        app.reaction_scan_status["progress"] = "Loading already-scanned messages..."
        app.reaction_scan_status["result"] = ""
        try:
            guild = get_guild()
            if not guild:
                app.reaction_scan_status["result"] = "Bot not connected to guild."
                return

            # Pre-load message IDs that already have reactions in DB — skip them entirely
            scanned_ids = await db.get_scanned_reaction_message_ids()

            channels = await db.get_monitored_channels()
            total_added = 0
            total_messages = 0
            skipped = 0

            for i, ch_cfg in enumerate(channels):
                channel = guild.get_channel(ch_cfg["channel_id"])
                if not channel:
                    continue

                app.reaction_scan_status["progress"] = f"Scanning #{channel.name} ({i+1}/{len(channels)})..."
                msg_count = 0

                try:
                    batch = []
                    async for message in channel.history(limit=None):
                        if message.author.bot:
                            continue
                        msg_count += 1
                        if msg_count % 500 == 0:
                            app.reaction_scan_status["progress"] = (
                                f"Scanning #{channel.name} ({i+1}/{len(channels)})... "
                                f"{msg_count} msgs, {total_added} reactions, {skipped} skipped"
                            )

                        urls = SUNO_URL_PATTERN.findall(message.content)
                        if not urls:
                            continue

                        if not message.reactions:
                            continue

                        # Skip messages already fully scanned
                        if message.id in scanned_ids:
                            skipped += 1
                            continue

                        total_messages += 1
                        # Extract title from embed
                        song_title = None
                        for embed in message.embeds:
                            if embed.title:
                                song_title = embed.title
                                break

                        # Fetch all reactions on this message
                        for reaction in message.reactions:
                            emoji_str = str(reaction.emoji)
                            try:
                                async for user in reaction.users():
                                    if user.bot:
                                        continue
                                    for url in urls:
                                        batch.append((
                                            message.id, channel.id, url,
                                            message.author.id, user.id,
                                            str(user), emoji_str, song_title,
                                            message.created_at.timestamp(),
                                        ))
                            except (_discord.Forbidden, _discord.HTTPException):
                                pass

                        # Flush batch when large enough
                        if len(batch) >= 500:
                            await db.add_song_reactions_bulk(batch)
                            total_added += len(batch)
                            batch = []

                    # Flush remaining batch
                    if batch:
                        await db.add_song_reactions_bulk(batch)
                        total_added += len(batch)
                        batch = []

                except Exception as e:
                    app.reaction_scan_status["progress"] = f"Error scanning #{channel.name}: {e}"
                    continue

            app.reaction_scan_status["result"] = (
                f"Done. {total_added} reactions added, {skipped} messages skipped (already scanned), "
                f"{total_messages} new song messages processed in {len(channels)} channel(s)."
            )
            await db.add_audit_log(
                event_type="reaction_scan",
                details=f"Reaction scan: {total_added} reactions from {total_messages} messages, {skipped} skipped",
                actor="system",
            )
        except Exception as e:
            app.reaction_scan_status["result"] = f"Reaction scan failed: {e}"
        finally:
            app.reaction_scan_status["running"] = False
            app.reaction_scan_status["progress"] = ""

    @app.route("/reaction-stats", methods=["GET", "POST"])
    @permission_required('reaction_stats')
    async def reaction_stats():
        import traceback

        if request.method == "POST":
            form = await request.form
            action = form.get("action")
            if action == "refresh_titles":
                if app.title_scan_status["running"]:
                    pass  # already running
                elif bot and bot.is_ready():
                    import asyncio
                    asyncio.get_event_loop().create_task(_run_title_scan())
                return redirect(url_for("reaction_stats"))
            elif action == "scan_reactions":
                if app.reaction_scan_status["running"]:
                    pass  # already running
                elif bot and bot.is_ready():
                    import asyncio
                    asyncio.get_event_loop().create_task(_run_reaction_scan())
                return redirect(url_for("reaction_stats"))
            elif action == "delete_all_reactions":
                count = await db.delete_all_reactions()
                print(f"[reaction-stats] Deleted all {count} reactions")
                return redirect(url_for("reaction_stats"))

        try:
            guild = get_guild()
            filter_channel = request.args.get("channel", type=int)

            # Time filter
            chart_range = request.args.get("range", default="30", type=str)
            range_days = {"1": 1, "7": 7, "30": 30, "90": 90, "all": 0}.get(chart_range, 30)

            # All stats use the same time filter
            stats = await db.get_reaction_stats(channel_id=filter_channel, days=range_days)

            # Most Reacted Songs — uses same central time filter
            top_songs = await db.get_top_songs(channel_id=filter_channel, days=range_days)

            # Channel list with reaction counts
            reaction_channels = await db.get_reaction_channels()
            channel_list = []
            for rc in reaction_channels:
                ch_name = f"channel-{rc['channel_id']}"
                if guild:
                    ch = guild.get_channel(rc["channel_id"])
                    if ch:
                        ch_name = ch.name
                channel_list.append({
                    "channel_id": rc["channel_id"],
                    "channel_name": ch_name,
                    "count": rc["count"],
                })

            # Resolve display names for top reactors, most reacted authors, and top songs
            if guild:
                for entry in stats.get("top_reactors", []):
                    member = guild.get_member(entry["user_id"])
                    entry["display_name"] = member.display_name if member else (entry["user_name"] or f"User {entry['user_id']}")
                for entry in stats.get("most_reacted_authors", []):
                    member = guild.get_member(entry["user_id"])
                    entry["display_name"] = member.display_name if member else f"User {entry['user_id']}"
                for entry in top_songs:
                    if entry.get("post_author_id"):
                        member = guild.get_member(entry["post_author_id"])
                        entry["author_name"] = member.display_name if member else f"User {entry['post_author_id']}"
                    else:
                        entry["author_name"] = "Unknown"
            else:
                for entry in stats.get("top_reactors", []):
                    entry["display_name"] = entry["user_name"] or f"User {entry['user_id']}"
                for entry in stats.get("most_reacted_authors", []):
                    entry["display_name"] = f"User {entry['user_id']}"
                for entry in top_songs:
                    entry["author_name"] = f"User {entry.get('post_author_id', '?')}"

            return await render_template(
                "reaction_stats.html",
                stats=stats,
                channel_list=channel_list,
                filter_channel=filter_channel,
                chart_range=chart_range,
                title_scan_status=app.title_scan_status,
                reaction_scan_status=app.reaction_scan_status,
                top_songs=top_songs,
            )
        except Exception as e:
            traceback.print_exc()
            return f"<pre>Error: {e}\n\n{traceback.format_exc()}</pre>", 500

    # --- Reaction Roles ---

    @app.route("/reaction-roles", methods=["GET", "POST"])
    @permission_required('reaction_roles')
    async def reaction_roles():
        guild = get_guild()

        if request.method == "POST":
            form = await request.form
            action = form.get("action", "")

            if action == "create":
                if not bot or not bot.is_ready() or not guild:
                    await flash("Discord-Bot ist nicht verbunden.", "error")
                    return redirect(url_for("reaction_roles"))

                channel_id_raw = form.get("channel_id", "").strip()
                role_id_raw    = form.get("role_id", "").strip()
                emoji_tag      = form.get("emoji", "").strip()
                content        = form.get("content", "").strip()

                if not (channel_id_raw.isdigit() and role_id_raw.isdigit()
                        and emoji_tag and content):
                    await flash("Bitte alle Felder ausfüllen.", "error")
                    return redirect(url_for("reaction_roles"))

                channel_id = int(channel_id_raw)
                role_id    = int(role_id_raw)
                channel    = guild.get_channel(channel_id) or guild.get_thread(channel_id)
                role       = guild.get_role(role_id)
                if not channel or not role:
                    await flash("Channel oder Rolle nicht gefunden.", "error")
                    return redirect(url_for("reaction_roles"))

                # Resolve emoji from <:name:id> tag back to a guild emoji we own
                import re as _re
                m = _re.match(r"<a?:([A-Za-z0-9_]+):(\d+)>", emoji_tag)
                if not m:
                    await flash("Ungültiges Emoji.", "error")
                    return redirect(url_for("reaction_roles"))
                emoji_name = m.group(1)
                emoji_id   = int(m.group(2))
                emoji_obj  = next((e for e in guild.emojis if e.id == emoji_id), None)
                if not emoji_obj:
                    await flash("Emoji ist nicht (mehr) auf diesem Server verfügbar.", "error")
                    return redirect(url_for("reaction_roles"))

                # --- Resolve #channel-name → <#id> mentions ---
                channel_map = {}
                if guild:
                    for ch in guild.text_channels:
                        channel_map[ch.name.lower()] = ch.id
                    for ch in guild.voice_channels:
                        channel_map[ch.name.lower()] = ch.id
                    for ch in guild.forums:
                        channel_map[ch.name.lower()] = ch.id

                def resolve_channel_mentions(text):
                    import re as _re2
                    def _repl(m):
                        name = m.group(1).lower()
                        cid = channel_map.get(name)
                        return f"<#{cid}>" if cid else m.group(0)
                    return _re2.sub(r'#([a-zA-Z0-9_-]+)', _repl, text)

                # --- Resolve :emoji_name: → <:name:id> custom emoji mentions ---
                emoji_map = {}
                if guild:
                    for e in guild.emojis:
                        emoji_map[e.name.lower()] = str(e)

                def resolve_emoji_mentions(text):
                    import re as _re3
                    def _repl(m):
                        name = m.group(1).lower()
                        tag = emoji_map.get(name)
                        return tag if tag else m.group(0)
                    return _re3.sub(r':([A-Za-z0-9_]+):', _repl, text)

                content = resolve_channel_mentions(content)
                content = resolve_emoji_mentions(content)

                # --- Split content into ≤2000 char chunks at paragraph boundaries ---
                def split_message(text, limit=2000):
                    if len(text) <= limit:
                        return [text]
                    chunks = []
                    while text:
                        if len(text) <= limit:
                            chunks.append(text)
                            break
                        # Find last double-newline within limit
                        cut = text.rfind('\n\n', 0, limit)
                        if cut <= 0:
                            # Fall back to single newline
                            cut = text.rfind('\n', 0, limit)
                        if cut <= 0:
                            # Hard cut at limit
                            cut = limit
                        chunks.append(text[:cut].rstrip())
                        text = text[cut:].lstrip('\n')
                    return chunks

                chunks = split_message(content)

                # Post all chunks; react only on the last one
                sent_messages = []
                try:
                    for i, chunk in enumerate(chunks):
                        msg = await channel.send(chunk)
                        sent_messages.append(msg)
                    await msg.add_reaction(emoji_obj)
                except Exception as ex:
                    # Cleanup already-sent messages on failure
                    for m in sent_messages:
                        try:
                            await m.delete()
                        except Exception:
                            pass
                    await flash(f"Konnte Beitrag nicht senden: {ex}", "error")
                    return redirect(url_for("reaction_roles"))

                # Store: message_id = last message (the one with the reaction)
                # all_message_ids = comma-separated list of ALL message IDs for cleanup
                stored_emoji = str(emoji_obj)
                all_ids = ",".join(str(m.id) for m in sent_messages)
                try:
                    await db.add_reaction_role(
                        channel_id=channel_id,
                        message_id=msg.id,
                        role_id=role_id,
                        role_name=role.name,
                        emoji=stored_emoji,
                        emoji_id=emoji_id,
                        content=content,
                        all_message_ids=all_ids,
                    )
                except Exception as ex:
                    for m in sent_messages:
                        try:
                            await m.delete()
                        except Exception:
                            pass
                    await flash(f"Konnte Eintrag nicht speichern: {ex}", "error")
                    return redirect(url_for("reaction_roles"))

                await db.add_audit_log(
                    event_type="reaction_role_created",
                    details=f"#{channel.name} message {msg.id} → {role.name} via {emoji_name}",
                    actor=session.get("username", "unknown"),
                )
                await flash("Reaction-Role erstellt.", "success")
                return redirect(url_for("reaction_roles"))

            elif action == "delete":
                entry_id_raw = form.get("entry_id", "").strip()
                if not entry_id_raw.isdigit():
                    return redirect(url_for("reaction_roles"))
                entry = await db.delete_reaction_role(int(entry_id_raw))
                if entry and bot and bot.is_ready() and guild:
                    ch = guild.get_channel(entry["channel_id"]) or guild.get_thread(entry["channel_id"])
                    if ch:
                        # Delete ALL messages that belong to this reaction-role
                        ids_to_delete = []
                        all_ids_str = entry.get("all_message_ids", "")
                        if all_ids_str:
                            ids_to_delete = [int(x) for x in all_ids_str.split(",") if x.strip().isdigit()]
                        if not ids_to_delete:
                            ids_to_delete = [entry["message_id"]]
                        for mid in ids_to_delete:
                            try:
                                m = await ch.fetch_message(mid)
                                await m.delete()
                            except Exception:
                                pass
                if entry:
                    await db.add_audit_log(
                        event_type="reaction_role_deleted",
                        details=f"message {entry['message_id']} → {entry['role_name']}",
                        actor=session.get("username", "unknown"),
                    )
                    await flash("Reaction-Role gelöscht.", "success")
                return redirect(url_for("reaction_roles"))

            return redirect(url_for("reaction_roles"))

        # --- GET ---
        text_channels   = []
        available_roles = []
        guild_emojis    = []
        if guild:
            for ch in sorted(guild.text_channels, key=lambda c: c.position):
                text_channels.append({"id": ch.id, "name": ch.name})
            for r in guild.roles:
                if r.id != guild.default_role.id and not r.managed:
                    available_roles.append({"id": r.id, "name": r.name})
            for e in guild.emojis:
                if e.available:
                    # str(e) → "<:name:id>" / "<a:name:id>"
                    guild_emojis.append({"name": e.name, "tag": str(e)})

        rows = await db.get_all_reaction_roles()
        # Decorate with channel name & jump URL for the UI
        entries = []
        for r in rows:
            ch_name = None
            jump_url = None
            if guild:
                ch = guild.get_channel(r["channel_id"]) or guild.get_thread(r["channel_id"])
                if ch:
                    ch_name = ch.name
                    jump_url = f"https://discord.com/channels/{guild.id}/{r['channel_id']}/{r['message_id']}"
            entries.append({
                **r,
                "channel_name": ch_name,
                "jump_url": jump_url,
                # For display: just the emoji tag as-is (Discord renders custom emojis in plain text fine).
                "emoji_display": r["emoji"],
            })

        return await render_template(
            "reaction_roles.html",
            text_channels=text_channels,
            available_roles=available_roles,
            guild_emojis=guild_emojis,
            entries=entries,
            bot_ready=bool(bot and bot.is_ready() and guild),
        )

    # --- Image Posting ---

    UPLOAD_DIR = os.path.join(os.path.dirname(db.db_path), "uploads")
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}

    @app.route("/uploads/<filename>")
    @login_required
    async def uploaded_image(filename):
        from quart import send_from_directory
        return await send_from_directory(UPLOAD_DIR, filename)

    @app.route("/image-posting", methods=["GET", "POST"])
    @permission_required('image_posting')
    async def image_posting():
        if request.method == "POST":
            form = await request.form
            action = form.get("action")

            if action == "add_category":
                name = form.get("category_name", "").strip()
                if name:
                    try:
                        await db.add_image_category(name)
                    except Exception:
                        await flash("Category already exists.", "error")
                return redirect(url_for("image_posting"))

            elif action == "delete_category":
                cat_id = int(form.get("category_id", 0))
                # Delete associated files
                images = await db.get_image_posts(category_id=cat_id)
                for img in images:
                    filepath = os.path.join(UPLOAD_DIR, img["filename"])
                    if os.path.exists(filepath):
                        os.remove(filepath)
                await db.delete_image_category(cat_id)
                return redirect(url_for("image_posting"))

            elif action == "upload_image":
                files = await request.files
                image_file = files.get("image")
                title = form.get("title", "").strip()[:30]
                description = form.get("description", "").strip()[:400]
                category_id = int(form.get("category_id", 0))

                if not image_file or not title or not category_id:
                    await flash("Title, category and image are required.", "error")
                    return redirect(url_for("image_posting"))

                ext = image_file.filename.rsplit(".", 1)[-1].lower() if "." in image_file.filename else ""
                if ext not in ALLOWED_EXTENSIONS:
                    await flash(f"Invalid file type. Allowed: {', '.join(ALLOWED_EXTENSIONS)}", "error")
                    return redirect(url_for("image_posting"))

                import uuid
                filename = f"{uuid.uuid4().hex}.{ext}"
                filepath = os.path.join(UPLOAD_DIR, filename)
                await image_file.save(filepath)

                await db.add_image_post(title, description, category_id, filename)
                return redirect(url_for("image_posting", category=category_id))

            elif action == "delete_image":
                image_id = int(form.get("image_id", 0))
                filename = await db.delete_image_post(image_id)
                if filename:
                    filepath = os.path.join(UPLOAD_DIR, filename)
                    if os.path.exists(filepath):
                        os.remove(filepath)
                return redirect(request.referrer or url_for("image_posting"))

        # GET
        import traceback
        try:
            categories = await db.get_image_categories()
            # Add image count per category
            all_images = await db.get_image_posts()
            cat_counts = {}
            for img in all_images:
                cat_counts[img["category_id"]] = cat_counts.get(img["category_id"], 0) + 1
            for cat in categories:
                cat["count"] = cat_counts.get(cat["id"], 0)

            filter_category = request.args.get("category", type=int)
            filter_category_name = None
            if filter_category:
                for cat in categories:
                    if cat["id"] == filter_category:
                        filter_category_name = cat["name"]
                        break

            images = await db.get_image_posts(category_id=filter_category)

            return await render_template(
                "image_posting.html",
                categories=categories,
                images=images,
                filter_category=filter_category,
                filter_category_name=filter_category_name,
            )
        except Exception as e:
            traceback.print_exc()
            return f"<pre>Error: {e}\n\n{traceback.format_exc()}</pre>", 500

    # --- Polls ---

    NUMBER_EMOJIS = ["1\u20e3", "2\u20e3", "3\u20e3", "4\u20e3", "5\u20e3", "6\u20e3", "7\u20e3", "8\u20e3", "9\u20e3", "\U0001F51F"]

    @app.route("/polls", methods=["GET", "POST"])
    @permission_required('polls')
    async def polls():
        import json as _json
        if request.method == "POST":
            form = await request.form
            action = form.get("action", "")

            if action == "create":
                title = form.get("title", "").strip()
                description = form.get("description", "").strip()
                raw_options = form.get("options", "").strip()
                options_list = [o.strip() for o in raw_options.split("\n") if o.strip()]
                if not title:
                    await flash("Title is required.", "error")
                    return redirect(url_for("polls"))
                if len(options_list) < 2:
                    await flash("At least 2 options are required.", "error")
                    return redirect(url_for("polls"))
                if len(options_list) > 10:
                    await flash("Maximum 10 options allowed.", "error")
                    return redirect(url_for("polls"))

                image_filename = None
                files = await request.files
                image_file = files.get("image")
                if image_file and image_file.filename:
                    ext = image_file.filename.rsplit(".", 1)[-1].lower()
                    if ext in ALLOWED_EXTENSIONS:
                        import uuid
                        image_filename = f"poll_{uuid.uuid4().hex}.{ext}"
                        filepath = os.path.join(UPLOAD_DIR, image_filename)
                        await image_file.save(filepath)

                poll_id = await db.create_poll(
                    title,
                    description,
                    _json.dumps(options_list),
                    image_filename,
                    creator_id=session.get("user_id"),
                    creator_name=session.get("username", "unknown"),
                )
                await flash(f"Poll #{poll_id} created.", "success")
                return redirect(url_for("polls"))

            elif action == "edit":
                poll_id = int(form.get("poll_id", 0))
                title = form.get("title", "").strip()
                description = form.get("description", "").strip()
                raw_options = form.get("options", "").strip()
                options_list = [o.strip() for o in raw_options.split("\n") if o.strip()]
                if not title or len(options_list) < 2:
                    await flash("Title and at least 2 options are required.", "error")
                    return redirect(url_for("polls"))
                if len(options_list) > 10:
                    await flash("Maximum 10 options allowed.", "error")
                    return redirect(url_for("polls"))

                image_filename = None
                files = await request.files
                image_file = files.get("image")
                if image_file and image_file.filename:
                    ext = image_file.filename.rsplit(".", 1)[-1].lower()
                    if ext in ALLOWED_EXTENSIONS:
                        import uuid
                        image_filename = f"poll_{uuid.uuid4().hex}.{ext}"
                        filepath = os.path.join(UPLOAD_DIR, image_filename)
                        await image_file.save(filepath)
                        # Remove old image
                        old_poll = await db.get_poll(poll_id)
                        if old_poll and old_poll.get("image_filename"):
                            old_path = os.path.join(UPLOAD_DIR, old_poll["image_filename"])
                            if os.path.exists(old_path):
                                os.remove(old_path)

                await db.update_poll(poll_id, title, description, _json.dumps(options_list), image_filename)
                await flash("Poll updated.", "success")
                return redirect(url_for("polls"))

            elif action == "delete":
                poll_id = int(form.get("poll_id", 0))
                filename = await db.delete_poll(poll_id)
                if filename:
                    filepath = os.path.join(UPLOAD_DIR, filename)
                    if os.path.exists(filepath):
                        os.remove(filepath)
                await flash("Poll deleted.", "success")
                return redirect(url_for("polls"))

            elif action == "post":
                poll_id = int(form.get("poll_id", 0))
                channel_id_str = form.get("channel_id", "").strip()
                poll = await db.get_poll(poll_id)
                if not poll:
                    await flash("Poll not found.", "error")
                    return redirect(url_for("polls"))
                if not channel_id_str:
                    await flash("No channel selected.", "error")
                    return redirect(url_for("polls"))
                guild = get_guild()
                if not guild:
                    await flash("Bot is not connected.", "error")
                    return redirect(url_for("polls"))
                import discord
                ch_id = int(channel_id_str)
                channel = guild.get_channel(ch_id) or guild.get_thread(ch_id)
                if not channel:
                    await flash("Channel not found.", "error")
                    return redirect(url_for("polls"))

                options_list = _json.loads(poll["options"])
                options_text = "\n".join(f"{NUMBER_EMOJIS[i]}  {opt}" for i, opt in enumerate(options_list))
                embed = discord.Embed(
                    title=f"\U0001F4CA {poll['title']}",
                    description=f"{poll['description']}\n\n{options_text}" if poll["description"] else options_text,
                    color=discord.Color.blue(),
                )
                file = None
                if poll.get("image_filename"):
                    filepath = os.path.join(UPLOAD_DIR, poll["image_filename"])
                    if os.path.exists(filepath):
                        file = discord.File(filepath, filename=poll["image_filename"])
                        embed.set_image(url=f"attachment://{poll['image_filename']}")
                bot_name = await db.get_setting("bot_name") or "Slowmode Bot"
                embed.set_footer(text=f"{bot_name} — Poll")
                try:
                    send_kwargs = {"embed": embed}
                    if file:
                        send_kwargs["file"] = file
                    if isinstance(channel, discord.ForumChannel):
                        thread, msg = await channel.create_thread(
                            name=f"\U0001F4CA {poll['title']}",
                            **send_kwargs,
                        )
                    else:
                        msg = await channel.send(**send_kwargs)
                    for i in range(len(options_list)):
                        await msg.add_reaction(NUMBER_EMOJIS[i])
                    await db.update_poll_message(poll_id, channel.id, msg.id)
                    await flash(f"Poll posted to #{channel.name}.", "success")
                except Exception as post_err:
                    await flash(f"Failed to post: {post_err}", "error")
                return redirect(url_for("polls"))

            elif action == "close":
                poll_id = int(form.get("poll_id", 0))
                await db.close_poll(poll_id)
                await flash("Poll closed.", "success")
                return redirect(url_for("polls"))

        # GET
        all_polls = await db.get_all_polls()
        web_users = await db.get_all_web_users()
        web_user_names = {u["id"]: u["username"] for u in web_users}
        guild = get_guild()
        discord_creator_names = {}
        for p in all_polls:
            p["options_list"] = _json.loads(p["options"])
            creator_id = p.get("creator_id")
            stored_creator_name = (p.get("creator_name") or "").strip()
            if stored_creator_name:
                p["creator_name"] = stored_creator_name
            elif creator_id in web_user_names:
                p["creator_name"] = web_user_names[creator_id]
            elif creator_id:
                if creator_id not in discord_creator_names:
                    member = guild.get_member(creator_id) if guild else None
                    if not member and guild:
                        try:
                            member = await guild.fetch_member(creator_id)
                        except Exception:
                            member = None
                    if not member and bot:
                        user = bot.get_user(creator_id)
                        if not user:
                            try:
                                user = await bot.fetch_user(creator_id)
                            except Exception:
                                user = None
                        member = user
                    discord_creator_names[creator_id] = (
                        str(member) if member else f"Discord user {creator_id}"
                    )
                p["creator_name"] = discord_creator_names[creator_id]
            else:
                p["creator_name"] = "Unknown"
        text_channels = []
        if guild:
            for ch in sorted(guild.text_channels, key=lambda c: c.position):
                text_channels.append({"id": ch.id, "name": ch.name})
            import discord as _disc
            for ch in sorted([c for c in guild.channels if isinstance(c, _disc.ForumChannel)], key=lambda c: c.position):
                text_channels.append({"id": ch.id, "name": f"\U0001F4AC {ch.name}"})
                for thread in sorted(ch.threads, key=lambda t: t.created_at or t.id, reverse=True):
                    if not thread.archived:
                        text_channels.append({"id": thread.id, "name": f"  \u2514 {thread.name}"})
        return await render_template("polls.html", polls=all_polls, text_channels=text_channels)

    # --- Shared helpers ---

    async def _fetch_youtube_info(url: str) -> tuple[str | None, str | None, str | None]:
        """Fetch title, author and thumbnail from a YouTube URL via oEmbed.
        Returns (title, author, thumbnail_url)."""
        try:
            oembed = f"https://www.youtube.com/oembed?url={url}&format=json"
            async with aiohttp.ClientSession() as sess:
                async with sess.get(oembed, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return None, None, None
                    data = await resp.json()
                    title  = data.get("title")
                    author = data.get("author_name")
                    m = YOUTUBE_URL_RE.search(url)
                    thumb = f"https://img.youtube.com/vi/{m.group(1)}/hqdefault.jpg" if m else None
                    return title, author, thumb
        except Exception:
            return None, None, None

    async def _fetch_suno_info(url: str) -> tuple[str | None, str | None, str | None]:
        """Fetch song title, artist and image from a Suno URL. Returns (title, artist, image_url)."""
        import html as _html
        try:
            song_id = None
            m_id = re.search(r'suno\.com/(?:s|song)/([A-Za-z0-9_-]+)', url or "")
            if m_id:
                song_id = m_id.group(1)
            async with aiohttp.ClientSession() as sess:
                async with sess.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return None, None, None
                    page_html = await resp.text()
                    # Extract og:image
                    image_url = None
                    img_match = re.search(r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', page_html)
                    if not img_match:
                        img_match = re.search(r'<meta\s+content=["\']([^"\']+)["\']\s+property=["\']og:image["\']', page_html)
                    if img_match:
                        image_url = img_match.group(1).strip()
                    artist = _extract_suno_clip_owner_display_name(page_html, song_id)
                    # <title> format: "Song Title by Artist Name | Suno"
                    match = re.search(r'<title>([^<]+)</title>', page_html)
                    if match:
                        raw = _html.unescape(match.group(1).strip())
                        raw = re.sub(r'\s*[|\-\u2013]\s*Suno$', '', raw).strip()
                        by_match = re.search(r'^(.+?)\s+by\s+(.+)$', raw)
                        if by_match:
                            title = by_match.group(1).strip()
                            return title, artist or by_match.group(2).strip(), image_url
                        return raw, artist, image_url
        except Exception:
            pass
        return None, None, None

    # --- Radio ---

    RADIO_UPLOAD_DIR = os.path.join(os.path.dirname(db.db_path), "radio")
    os.makedirs(RADIO_UPLOAD_DIR, exist_ok=True)

    EXP_RADIO_DIR = os.path.join(os.path.dirname(db.db_path), "radio", "exp_radio")
    for _sub in ("mp3", "ass", "assets"):
        os.makedirs(os.path.join(EXP_RADIO_DIR, _sub), exist_ok=True)

    RIGHTS_DECLARATION_TEXT = (
        "I hereby confirm that I am the creator or rights holder of this audio track "
        "and grant a non-exclusive streaming license for a period of 14 days from the "
        "date of upload. This license covers live streaming on Twitch and the storage of "
        "VODs (Video on Demand) that include this track. After 14 days, the file will be "
        "automatically deleted from the server."
    )

    CONTENT_GUIDELINES_TEXT = (
        "By uploading, you also confirm that your track does not contain any of the following: "
        "extremist, radical, or politically motivated content (whether left-wing or right-wing); "
        "glorification or incitement of violence, hatred, or discrimination against individuals or groups; "
        "graphic, cruel, disturbing, or otherwise harmful material; "
        "content that violates applicable laws, Twitch Community Guidelines, or basic standards of decency. "
        "Submissions that violate these guidelines will be removed without notice and may result in a permanent upload ban."
    )

    MAX_UPLOAD_SIZE_MB = 20
    MAX_DURATION_SEC = 360
    MAX_BITRATE_KBPS = 320
    MAX_UPLOADS_PER_IP = 3

    async def _validate_mp3(filepath: str) -> dict:
        """Validate an MP3 file. Returns dict with info or 'error' key."""
        import asyncio, mimetypes, json as _json
        # MIME type check
        mime, _ = mimetypes.guess_type(filepath)
        if mime not in ("audio/mpeg", "audio/mp3"):
            return {"error": "Invalid file type. Only MP3 files are allowed."}
        # File size
        size = os.path.getsize(filepath)
        if size > MAX_UPLOAD_SIZE_MB * 1024 * 1024:
            return {"error": f"File too large. Maximum {MAX_UPLOAD_SIZE_MB}MB."}
        # ffprobe
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_format", "-show_streams", filepath,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0:
                return {"error": "Not a valid audio file."}
            info = _json.loads(stdout)
            fmt = info.get("format", {})
            duration = float(fmt.get("duration", 0))
            bitrate = int(fmt.get("bit_rate", 0)) // 1000
            # Check for audio stream
            has_audio = any(s.get("codec_type") == "audio" for s in info.get("streams", []))
            if not has_audio:
                return {"error": "No audio stream found in file."}
            if duration > MAX_DURATION_SEC:
                return {"error": f"Track too long. Maximum {MAX_DURATION_SEC // 60} minutes."}
            if duration < 5:
                return {"error": "Track too short. Minimum 5 seconds."}
            return {"duration": round(duration, 1), "bitrate": min(bitrate, MAX_BITRATE_KBPS), "size": size}
        except FileNotFoundError:
            return {"error": "Audio validation unavailable (ffprobe not found)."}
        except Exception as e:
            return {"error": f"Validation failed: {e}"}

    @app.route("/radio/upload", methods=["GET", "POST"])
    async def radio_upload():
        # Check if uploads are enabled
        upload_enabled = await db.get_setting("radio_upload_enabled")
        if upload_enabled == "0":
            return await render_template("radio_upload.html", closed=True)

        if request.method == "POST":
            import hashlib, uuid, time, json as _json
            form = await request.form
            suno_url = form.get("suno_url", "").strip()
            rights_agreed = form.get("rights_agreed")

            if not rights_agreed:
                await flash("You must agree to the streaming rights declaration.", "error")
                return redirect(url_for("radio_upload"))

            if not suno_url:
                await flash("Please provide the Suno URL.", "error")
                return redirect(url_for("radio_upload"))

            # Rate limiting
            client_ip = request.headers.get("X-Forwarded-For", request.remote_addr) or "unknown"
            upload_count = await db.count_radio_uploads_by_ip(client_ip)
            if upload_count >= MAX_UPLOADS_PER_IP:
                await flash("Upload limit reached. Please try again later.", "error")
                return redirect(url_for("radio_upload"))

            # Get file
            files = await request.files
            mp3_file = files.get("mp3_file")
            if not mp3_file or not mp3_file.filename:
                await flash("Please select an MP3 file.", "error")
                return redirect(url_for("radio_upload"))

            if not mp3_file.filename.lower().endswith(".mp3"):
                await flash("Only .mp3 files are accepted.", "error")
                return redirect(url_for("radio_upload"))

            # Honeypot check
            if form.get("website", ""):
                return redirect(url_for("radio_upload"))

            # Save temp file for validation
            original_filename = mp3_file.filename
            unique_name = f"radio_{uuid.uuid4().hex}.mp3"
            filepath = os.path.join(RADIO_UPLOAD_DIR, unique_name)
            await mp3_file.save(filepath)

            # Validate
            result = await _validate_mp3(filepath)
            if "error" in result:
                os.remove(filepath)
                await flash(result["error"], "error")
                return redirect(url_for("radio_upload"))

            # Strip cover art / non-audio streams to prevent concat stalls
            stripped_path = filepath + ".stripped.mp3"
            try:
                strip_proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-y", "-i", filepath, "-vn", "-acodec", "copy", stripped_path,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                await strip_proc.communicate()
                if strip_proc.returncode == 0 and os.path.exists(stripped_path):
                    os.replace(stripped_path, filepath)
                else:
                    # If stripping fails, keep the original
                    if os.path.exists(stripped_path):
                        os.remove(stripped_path)
            except Exception:
                if os.path.exists(stripped_path):
                    os.remove(stripped_path)

            # Fetch Suno metadata
            title, artist, _ = await _fetch_suno_info(suno_url)
            if not title:
                os.remove(filepath)
                await flash("Could not fetch song info from the Suno URL.", "error")
                return redirect(url_for("radio_upload"))
            artist = artist or "Unknown Artist"

            # Artist limit: max 3 active songs per artist
            artist_count = await db.count_active_radio_songs_by_artist(artist)
            if artist_count >= 3:
                os.remove(filepath)
                await flash(f"Artist '{artist}' already has {artist_count} songs in the playlist (max 3).", "error")
                return redirect(url_for("radio_upload"))

            # Generate rights hash
            rights_hash = hashlib.sha256(
                f"{RIGHTS_DECLARATION_TEXT}|{time.time()}|{client_ip}|{original_filename}|{suno_url}".encode()
            ).hexdigest()

            song_id = await db.add_radio_song(
                title=title, artist=artist, suno_url=suno_url,
                filename=unique_name, original_filename=original_filename,
                file_size=result["size"], duration=result["duration"],
                bitrate=result["bitrate"], uploaded_by_ip=client_ip,
                rights_declaration=RIGHTS_DECLARATION_TEXT, rights_hash=rights_hash,
            )
            await flash(f"'{title}' by {artist} uploaded successfully! (#{song_id})", "success")
            return redirect(url_for("radio_upload"))

        return await render_template("radio_upload.html", closed=False, rights_text=RIGHTS_DECLARATION_TEXT, content_guidelines=CONTENT_GUIDELINES_TEXT)

    @app.route("/radio", methods=["GET", "POST"])
    @permission_required('radio')
    async def radio_admin():
        import json as _json
        if request.method == "POST":
            form = await request.form
            action = form.get("action", "")

            if action == "delete_song":
                song_id = int(form.get("song_id", 0))
                filename = await db.delete_radio_song(song_id)
                if filename:
                    filepath = os.path.join(RADIO_UPLOAD_DIR, filename)
                    if os.path.exists(filepath):
                        os.remove(filepath)
                await flash("Song deleted.", "success")

            elif action == "move_up":
                await db.move_radio_song(int(form.get("song_id", 0)), "up")

            elif action == "move_down":
                await db.move_radio_song(int(form.get("song_id", 0)), "down")

            elif action == "save_config":
                twitch_key = form.get("twitch_key", "").strip()
                stream_url = form.get("stream_url", "").strip()
                upload_enabled = "1" if form.get("upload_enabled") else "0"
                shuffle = "1" if form.get("shuffle") else "0"
                post_ch1 = form.get("post_channel_1_id", "").strip()
                post_ch2 = form.get("post_channel_2_id", "").strip()
                expiry_ch = form.get("expiry_channel_id", "").strip()
                tw_client_id = form.get("twitch_client_id", "").strip()
                tw_client_secret = form.get("twitch_client_secret", "").strip()
                tw_refresh_token = form.get("twitch_refresh_token", "").strip()
                tw_broadcaster = form.get("twitch_broadcaster_login", "").strip()

                # Only update key if not masked placeholder
                if twitch_key and not twitch_key.startswith("****"):
                    await db.set_setting("radio_twitch_key", twitch_key)
                if stream_url:
                    await db.set_setting("radio_stream_url", stream_url)
                await db.set_setting("radio_upload_enabled", upload_enabled)
                await db.set_setting("radio_shuffle", shuffle)
                await db.set_setting("radio_post_channel_1_id", post_ch1)
                await db.set_setting("radio_post_channel_2_id", post_ch2)
                await db.set_setting("radio_expiry_channel_id", expiry_ch)
                if tw_client_id:
                    await db.set_setting("radio_twitch_client_id", tw_client_id)
                if tw_client_secret and not tw_client_secret.startswith("****"):
                    await db.set_setting("radio_twitch_client_secret", tw_client_secret)
                if tw_refresh_token and not tw_refresh_token.startswith("****"):
                    await db.set_setting("radio_twitch_refresh_token", tw_refresh_token)
                    # Reset cached IDs so they get re-resolved on next start
                    await db.set_setting("radio_twitch_bot_login", "")
                    await db.set_setting("radio_twitch_bot_user_id", "")
                if tw_broadcaster:
                    # Accept "name", "#name", or full "https://twitch.tv/name" —
                    # always store just the bare login.
                    bn = tw_broadcaster.strip().rstrip("/").lstrip("#").lower()
                    if "twitch.tv/" in bn:
                        bn = bn.split("twitch.tv/", 1)[1].split("/")[0]
                    await db.set_setting("radio_twitch_broadcaster_login", bn)

                # Background upload
                files = await request.files
                bg_file = files.get("background")
                if bg_file and bg_file.filename:
                    ext = bg_file.filename.rsplit(".", 1)[-1].lower()
                    allowed_bg = {"png", "jpg", "jpeg", "gif", "webp", "mp4", "webm"}
                    if ext in allowed_bg:
                        import uuid
                        bg_type = "video" if ext in ("mp4", "webm") else "image"
                        bg_name = f"radio_bg_{uuid.uuid4().hex}.{ext}"
                        bg_path = os.path.join(RADIO_UPLOAD_DIR, bg_name)
                        await bg_file.save(bg_path)
                        # Remove old background
                        old_bg = await db.get_setting("radio_background_filename")
                        if old_bg:
                            old_path = os.path.join(RADIO_UPLOAD_DIR, old_bg)
                            if os.path.exists(old_path):
                                os.remove(old_path)
                        await db.set_setting("radio_background_filename", bg_name)
                        await db.set_setting("radio_background_type", bg_type)

                await flash("Configuration saved.", "success")

            elif action == "post_upload_url":
                ch_id = form.get("post_channel_id_select", "")
                guild = get_guild()
                if guild and ch_id:
                    channel = guild.get_channel(int(ch_id)) or guild.get_thread(int(ch_id))
                    if channel:
                        import discord
                        base_url = str(request.host_url).rstrip("/")
                        embed = discord.Embed(
                            title="\U0001F3B5 Song Upload",
                            description=f"Submit your track for the stream!\n\n**[Upload here]({base_url}/radio/upload)**",
                            color=discord.Color.green(),
                        )
                        await channel.send(embed=embed)
                        await flash(f"Upload link posted to #{channel.name}.", "success")

            elif action == "post_stream_url":
                ch_id = form.get("post_channel_id_select", "")
                stream_url = await db.get_setting("radio_stream_url") or ""
                guild = get_guild()
                if guild and ch_id and stream_url:
                    channel = guild.get_channel(int(ch_id)) or guild.get_thread(int(ch_id))
                    if channel:
                        import discord
                        # Resolve current playlist name
                        _src = await db.get_setting("radio_source_mode") or "submissions"
                        if _src == "suno_playlist":
                            _pl_id = await db.get_setting("radio_active_suno_playlist")
                            if _pl_id:
                                _pl = await db.get_suno_playlist(int(_pl_id))
                                _pl_name = _pl["description"] if _pl and _pl.get("description") else "Suno Playlist"
                            else:
                                _pl_name = "Suno Playlist"
                        else:
                            _pl_name = "Submissions Playlist"
                        embed = discord.Embed(
                            title="\U0001F4FA Live Stream",
                            description=f"Watch the stream now!\n\n\U0001F4CB Playlist: **{_pl_name}**\n\n**[Tune in]({stream_url})**",
                            color=discord.Color.purple(),
                        )
                        await channel.send(embed=embed)
                        await flash(f"Stream link posted to #{channel.name}.", "success")

            elif action == "save_post_channels":
                ch1 = form.get("channel_1_id", "").strip()
                ch2 = form.get("channel_2_id", "").strip()
                if ch1:
                    await db.set_setting("radio_post_channel_1_id", ch1)
                if ch2:
                    await db.set_setting("radio_post_channel_2_id", ch2)
                from quart import jsonify
                return jsonify({"ok": True})

            elif action == "save_lyrics_config":
                lyrics_width = form.get("lyrics_width", "80")
                if lyrics_width not in ("80", "60", "40"):
                    lyrics_width = "80"
                await db.set_setting("radio_lyrics_width", lyrics_width)
                await flash("Lyrics config saved.", "success")

            elif action == "save_song_pip_config":
                spip_enabled  = "on" if form.get("song_pip_enabled") == "on" else "off"
                spip_format   = form.get("song_pip_format", "9:16")
                spip_scale    = form.get("song_pip_scale", "20")
                spip_position = form.get("song_pip_position", "top-right")
                await db.set_setting("radio_song_pip_enabled",  spip_enabled)
                await db.set_setting("radio_song_pip_format",   spip_format)
                await db.set_setting("radio_song_pip_scale",    spip_scale)
                await db.set_setting("radio_song_pip_position", spip_position)
                if stream_manager.is_running:
                    await stream_manager.reload_pip()
                    await flash("Song Video PiP config saved & applied to running stream.", "success")
                else:
                    await flash("Song Video PiP config saved.", "success")

            elif action == "save_pip_config":
                pip_mode = form.get("pip_mode", "off")
                pip_format = form.get("pip_format", "16:9")
                pip_scale = form.get("pip_scale", "25")
                pip_position = form.get("pip_position", "center-right")
                pip_rtmp_key = form.get("pip_rtmp_key", "").strip()
                await db.set_setting("radio_pip_mode", pip_mode)
                await db.set_setting("radio_pip_format", pip_format)
                await db.set_setting("radio_pip_scale", pip_scale)
                await db.set_setting("radio_pip_position", pip_position)
                await db.set_setting("radio_pip_rtmp_key", pip_rtmp_key)
                # PiP file upload
                files = await request.files
                pip_file = files.get("pip_file")
                if pip_file and pip_file.filename:
                    ext = pip_file.filename.rsplit(".", 1)[-1].lower()
                    allowed = {"png", "jpg", "jpeg", "gif", "webp", "mp4", "webm"}
                    if ext in allowed:
                        import uuid
                        pip_type = "video" if ext in ("mp4", "webm") else "image"
                        pip_name = f"radio_pip_{uuid.uuid4().hex}.{ext}"
                        pip_path = os.path.join(RADIO_UPLOAD_DIR, pip_name)
                        await pip_file.save(pip_path)
                        old_pip = await db.get_setting("radio_pip_filename")
                        if old_pip:
                            old_path = os.path.join(RADIO_UPLOAD_DIR, old_pip)
                            if os.path.exists(old_path):
                                os.remove(old_path)
                        await db.set_setting("radio_pip_filename", pip_name)
                        await db.set_setting("radio_pip_file_type", pip_type)
                # Hot-reload PiP on running stream
                if stream_manager.is_running:
                    await stream_manager.reload_pip()
                    await flash("PiP configuration saved & applied to running stream.", "success")
                else:
                    await flash("PiP configuration saved.", "success")

            elif action == "set_source_mode":
                mode = form.get("source_mode", "submissions")
                await db.set_setting("radio_source_mode", mode)
                await flash(f"Radio source set to: {mode}", "success")

            elif action == "add_suno_playlist":
                pl_url = form.get("suno_playlist_url", "").strip()
                pl_desc = form.get("suno_playlist_desc", "").strip()
                if pl_url:
                    try:
                        await db.add_suno_playlist(pl_url, pl_desc)
                        await flash("Suno playlist added.", "success")
                    except Exception as e:
                        await flash(f"Could not add playlist: {e}", "error")

            elif action == "delete_suno_playlist":
                pl_id = int(form.get("playlist_id", 0))
                await db.delete_suno_playlist(pl_id)
                # If this was the active playlist, clear the setting
                active = await db.get_setting("radio_active_suno_playlist")
                if active and int(active) == pl_id:
                    await db.set_setting("radio_active_suno_playlist", "")
                await flash("Suno playlist deleted.", "success")

            elif action == "select_suno_playlist":
                pl_id = form.get("playlist_id", "").strip()
                await db.set_setting("radio_active_suno_playlist", pl_id)
                await flash("Active Suno playlist updated.", "success")

            return redirect(url_for("radio_admin"))

        # GET
        songs = await db.get_all_radio_songs(active_only=False)
        twitch_key = await db.get_setting("radio_twitch_key") or ""
        masked_key = f"****{twitch_key[-4:]}" if len(twitch_key) > 4 else ""
        stream_url = await db.get_setting("radio_stream_url") or ""
        upload_enabled = await db.get_setting("radio_upload_enabled") or "1"
        bg_filename = await db.get_setting("radio_background_filename") or ""
        bg_type = await db.get_setting("radio_background_type") or "image"
        shuffle = await db.get_setting("radio_shuffle") or "0"

        guild = get_guild()
        text_channels = []
        if guild:
            for ch in sorted(guild.text_channels, key=lambda c: c.position):
                text_channels.append({"id": ch.id, "name": ch.name})

        post_channel_1_id = await db.get_setting("radio_post_channel_1_id") or ""
        post_channel_2_id = await db.get_setting("radio_post_channel_2_id") or ""
        expiry_channel_id = await db.get_setting("radio_expiry_channel_id") or ""
        # Twitch chat-bot (modern Helix flow)
        tw_client_id     = await db.get_setting("radio_twitch_client_id") or ""
        _tw_secret       = await db.get_setting("radio_twitch_client_secret") or ""
        _tw_refresh      = await db.get_setting("radio_twitch_refresh_token") or ""
        tw_secret_masked = f"****{_tw_secret[-4:]}" if len(_tw_secret) > 4 else ""
        tw_refresh_masked = f"****{_tw_refresh[-4:]}" if len(_tw_refresh) > 4 else ""
        tw_broadcaster_login = await db.get_setting("radio_twitch_broadcaster_login") or ""
        tw_bot_login         = await db.get_setting("radio_twitch_bot_login") or ""

        source_mode = await db.get_setting("radio_source_mode") or "submissions"
        suno_playlists = await db.get_all_suno_playlists()
        active_suno_playlist = await db.get_setting("radio_active_suno_playlist") or ""

        pip_mode = await db.get_setting("radio_pip_mode") or "off"
        pip_format = await db.get_setting("radio_pip_format") or "16:9"
        pip_scale = await db.get_setting("radio_pip_scale") or "25"
        pip_position = await db.get_setting("radio_pip_position") or "center-right"
        pip_filename = await db.get_setting("radio_pip_filename") or ""
        pip_file_type = await db.get_setting("radio_pip_file_type") or "image"
        pip_rtmp_key = await db.get_setting("radio_pip_rtmp_key") or ""
        song_pip_enabled  = await db.get_setting("radio_song_pip_enabled")  or "off"
        song_pip_format   = await db.get_setting("radio_song_pip_format")   or "9:16"
        song_pip_scale    = await db.get_setting("radio_song_pip_scale")    or "30"
        song_pip_position = await db.get_setting("radio_song_pip_position") or "top-right"
        lyrics_width = await db.get_setting("radio_lyrics_width") or "80"

        return await render_template(
            "radio.html",
            songs=songs, masked_key=masked_key, stream_url=stream_url,
            upload_enabled=upload_enabled, bg_filename=bg_filename, bg_type=bg_type,
            text_channels=text_channels,
            post_channel_1_id=post_channel_1_id,
            post_channel_2_id=post_channel_2_id,
            expiry_channel_id=expiry_channel_id,
            shuffle=shuffle,
            tw_client_id=tw_client_id,
            tw_secret_masked=tw_secret_masked,
            tw_refresh_masked=tw_refresh_masked,
            tw_broadcaster_login=tw_broadcaster_login,
            tw_bot_login=tw_bot_login,
            source_mode=source_mode,
            suno_playlists=suno_playlists,
            active_suno_playlist=active_suno_playlist,
            pip_mode=pip_mode, pip_format=pip_format, pip_scale=pip_scale,
            pip_position=pip_position, pip_filename=pip_filename,
            pip_file_type=pip_file_type, pip_rtmp_key=pip_rtmp_key,
            song_pip_enabled=song_pip_enabled, song_pip_format=song_pip_format,
            song_pip_scale=song_pip_scale, song_pip_position=song_pip_position,
            lyrics_width=lyrics_width,
        )

    @app.route("/radio/files/<filename>")
    async def radio_file(filename):
        from quart import send_from_directory
        return await send_from_directory(RADIO_UPLOAD_DIR, filename)

    @app.route("/radio/export-rights")
    @permission_required('radio')
    async def radio_export_rights():
        import csv, io
        from datetime import datetime, timezone
        from quart import Response
        songs = await db.get_all_radio_songs(active_only=False)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "ID", "Title", "Artist", "Suno URL", "Original Filename",
            "Uploaded At (UTC)", "Expires At (UTC)", "Uploader IP",
            "Rights Declaration", "Rights Hash (SHA256)", "Rights Agreed At (UTC)",
        ])
        for s in songs:
            fmt = lambda ts: datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if ts else ""
            writer.writerow([
                s["id"], s["title"], s["artist"], s.get("suno_url", ""),
                s.get("original_filename", ""),
                fmt(s.get("uploaded_at")), fmt(s.get("expires_at")),
                s.get("uploaded_by_ip", ""),
                s.get("rights_declaration", ""), s.get("rights_hash", ""),
                fmt(s.get("rights_agreed_at")),
            ])
        csv_data = output.getvalue()
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        return Response(
            csv_data,
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename=streaming_rights_{timestamp}.csv"},
        )

    from bot.stream_manager import StreamManager
    stream_manager = StreamManager(db, RADIO_UPLOAD_DIR)

    @app.route("/radio/stream/status")
    @permission_required('radio')
    async def radio_stream_status():
        from quart import jsonify
        return jsonify(await stream_manager.get_status())

    @app.route("/admin/twitch-radio/test-connection", methods=["POST"])
    @permission_required('radio')
    async def radio_twitch_test():
        """One-shot health-check for the Twitch chat-bot credentials."""
        from quart import jsonify
        from bot.twitch_bot import TwitchBot
        bot = TwitchBot(db)
        result = await bot.diagnose()
        return jsonify(result)

    @app.route("/radio/stream/<action>", methods=["POST"])
    @permission_required('radio')
    async def radio_stream_action(action):
        from quart import jsonify
        if action == "start":
            result = await stream_manager.start()
        elif action == "stop":
            result = await stream_manager.stop()
        elif action == "next":
            result = await stream_manager.skip_next()
        elif action == "prev":
            result = await stream_manager.skip_prev()
        elif action == "reload":
            result = await stream_manager.reload_playlist()
        elif action == "pip-reload":
            result = await stream_manager.reload_pip()
        else:
            result = {"error": "Unknown action."}
        return jsonify(result)

    # --- Auto-cleanup task ---

    async def _radio_cleanup_loop():
        """Periodically delete expired radio songs (every hour).
        Posts a notification to the configured Discord channel when songs expire."""
        while True:
            try:
                await asyncio.sleep(3600)
                filenames, expired_songs = await db.cleanup_expired_radio_songs()
                for fn in filenames:
                    filepath = os.path.join(RADIO_UPLOAD_DIR, fn)
                    if os.path.exists(filepath):
                        os.remove(filepath)
                if expired_songs:
                    print(f"[radio] Cleaned up {len(expired_songs)} expired songs.")
                    # Post notification to Discord
                    channel_id_str = await db.get_setting("radio_expiry_channel_id")
                    if channel_id_str and bot and bot.is_ready():
                        guild = get_guild()
                        if guild:
                            ch = guild.get_channel(int(channel_id_str))
                            if ch:
                                try:
                                    lines = []
                                    for s in expired_songs:
                                        title = s.get("title", "Unknown")
                                        artist = s.get("artist", "Unknown")
                                        suno_url = s.get("suno_url", "")
                                        if suno_url:
                                            lines.append(f"- **{title}** - {artist}\n  {suno_url}")
                                        else:
                                            lines.append(f"- **{title}** - {artist}")
                                    msg = (
                                        f"🗑️ **{len(expired_songs)} Song{'s' if len(expired_songs) != 1 else ''} "
                                        f"expired and removed from the radio playlist:**\n\n"
                                        + "\n".join(lines)
                                    )
                                    await ch.send(msg)
                                except Exception as notify_err:
                                    print(f"[radio] Expiry notification error: {notify_err}")
            except Exception as e:
                print(f"[radio] Cleanup error: {e}")

    async def _exp_radio_cleanup_loop():
        """Periodically soft-delete expired exp_radio songs (every hour).
        Also marks stale pending songs (no MP3 uploaded within 30 min) as failed.
        Notifies users via the configured Discord channel."""
        while True:
            try:
                await asyncio.sleep(3600)
                # Mark stale pending songs (submitted >30 min ago, still no MP3) as failed
                await db.db.execute(
                    "UPDATE exp_radio_songs SET analysis_status = 'failed' "
                    "WHERE analysis_status = 'pending' AND mp3_filename IS NULL "
                    "AND submitted_at < unixepoch() - 1800"
                )
                await db.db.commit()
                expired = await db.expire_old_exp_radio_songs()
                if not expired:
                    continue
                print(f"[exp-radio] Expired {len(expired)} song(s).", flush=True)
                # File cleanup — but skip songs still referenced by the
                # currently-running stream's in-memory playlist (FFmpeg would
                # crash on its next loop iteration if we removed those).
                in_use_mp3 = set()
                if exp_stream_manager.is_running:
                    in_use_mp3 = {
                        s.get("mp3_filename")
                        for s in (exp_stream_manager.playlist or [])
                        if s.get("mp3_filename")
                    }
                mp3_dir   = os.path.join(EXP_RADIO_DIR, "mp3")
                ass_dir   = os.path.join(EXP_RADIO_DIR, "ass")
                cover_dir = os.path.join(EXP_RADIO_DIR, "cover_cache")
                removed_files = 0
                for s in expired:
                    mp3_fn = s.get("mp3_filename")
                    if mp3_fn and mp3_fn in in_use_mp3:
                        print(
                            f"[exp-radio] Keeping files for #{s['id']} ({s.get('title')!r}) "
                            f"— still in active stream playlist.", flush=True,
                        )
                        continue
                    targets = []
                    if mp3_fn:
                        targets.append(os.path.join(mp3_dir, mp3_fn))
                    ass_fn = s.get("ass_filename")
                    if ass_fn:
                        targets.append(os.path.join(ass_dir, ass_fn))
                    uuid = s.get("suno_uuid")
                    if uuid:
                        for ext in (".jpg", ".mp4"):
                            targets.append(os.path.join(cover_dir, f"{uuid}{ext}"))
                    for p in targets:
                        if os.path.exists(p):
                            try:
                                os.remove(p)
                                removed_files += 1
                            except Exception as e:
                                print(f"[exp-radio] Could not remove {p}: {e}", flush=True)
                if removed_files:
                    print(f"[exp-radio] Removed {removed_files} expired file(s) from disk.", flush=True)
                channel_id_str = (
                    await db.get_setting("exp_radio_expiry_channel_id")
                    or await db.get_setting("radio_expiry_channel_id")
                )
                if channel_id_str and bot and bot.is_ready():
                    guild = get_guild()
                    if guild:
                        ch = guild.get_channel(int(channel_id_str))
                        if ch:
                            try:
                                lines = []
                                for s in expired:
                                    t = s.get("title") or "Untitled"
                                    a = s.get("artist") or s.get("user_name") or ""
                                    u = s.get("suno_url") or ""
                                    lines.append(f"- **{t}** by {a}  {u}")
                                msg = (
                                    f"🗑️ **{len(expired)} Experimental Radio song(s) expired:**\n"
                                    + "\n".join(lines)
                                )
                                await ch.send(msg)
                            except Exception as e:
                                print(f"[exp-radio] Expiry notify error: {e}", flush=True)
            except Exception as e:
                print(f"[exp-radio] Cleanup error: {e}", flush=True)

    async def _post_exp_stream_announcement(ch_id: str, stream_url: str) -> tuple[bool, str]:
        """Send the “📺 Live Stream” embed to the given Discord channel id.

        Returns (True, channel_name) on success, (False, error_message) on
        failure. Called both from the manual “Post Stream Link” buttons on
        the admin page and from the auto-start scheduler.
        """
        if not ch_id or not stream_url:
            return False, "missing channel id or stream URL"
        guild = get_guild()
        if not guild:
            return False, "Discord guild unavailable"
        try:
            ch_int = int(ch_id)
        except Exception:
            return False, f"invalid channel id {ch_id!r}"
        channel = guild.get_channel(ch_int) or guild.get_thread(ch_int)
        if not channel:
            return False, f"channel {ch_id} not found"
        try:
            import discord
            embed = discord.Embed(
                title="\U0001F4FA Live Stream",
                description=f"Watch the stream now!\n\n**[Tune in]({stream_url})**",
                color=discord.Color.purple(),
            )
            await channel.send(embed=embed)
            return True, channel.name
        except Exception as e:
            return False, f"send failed: {e}"

    async def _exp_radio_schedule_loop():
        """Auto-start the exp_radio stream on configured weekdays + time.

        Wakes every 30s, checks (now.weekday, now.hour:minute) against the
        admin-saved schedule. Starts the stream with `fresh_cache=True` so a
        scheduled session always begins with a freshly downloaded cover
        cache and rebuilt audio/ASS intermediates. We track the last fired
        unix-minute on `app.exp_schedule_last_fired` to avoid double-firing
        within the same minute.
        """
        from bot.exp_stream_manager import log_event
        from datetime import datetime
        app.exp_schedule_last_fired = 0
        while True:
            try:
                enabled = await db.get_setting("exp_radio_schedule_enabled") or "off"
                if enabled != "on":
                    await asyncio.sleep(30)
                    continue
                days_csv = await db.get_setting("exp_radio_schedule_days") or ""
                days = {int(d) for d in days_csv.split(",") if d.strip().isdigit()}
                hhmm = (await db.get_setting("exp_radio_schedule_time") or "").strip()
                if not days or not hhmm or ":" not in hhmm:
                    await asyncio.sleep(30)
                    continue
                try:
                    h_str, m_str = hhmm.split(":", 1)
                    target_h, target_m = int(h_str), int(m_str)
                except Exception:
                    await asyncio.sleep(30)
                    continue
                now = datetime.now()  # server-local time
                cur_min_key = int(now.timestamp() // 60)
                # Match: today's weekday is in the set AND hh:mm matches AND
                # we haven't fired in this exact minute yet.
                if (now.weekday() in days
                        and now.hour == target_h
                        and now.minute == target_m
                        and cur_min_key != app.exp_schedule_last_fired):
                    app.exp_schedule_last_fired = cur_min_key
                    if exp_stream_manager.is_running:
                        log_event(
                            "Scheduler: stream already running \u2014 skipping auto-start.",
                            prefix="[exp-schedule]",
                        )
                    else:
                        twitch_key = await db.get_setting("exp_radio_twitch_key") or ""
                        if not twitch_key:
                            log_event(
                                "Scheduler: no Twitch stream key configured \u2014 cannot auto-start.",
                                level="error", prefix="[exp-schedule]",
                            )
                        else:
                            log_event(
                                f"Scheduler: triggering auto-start (weekday={now.weekday()}, "
                                f"time={now.strftime('%H:%M')}) with fresh cache.",
                                prefix="[exp-schedule]",
                            )
                            try:
                                result = await exp_stream_manager.start(
                                    twitch_key, fresh_cache=True, scheduled=True,
                                )
                                if result.get("ok"):
                                    log_event(
                                        f"Scheduler: start accepted with {result.get('song_count')} song(s); waiting for FFmpeg to go live.",
                                        prefix="[exp-schedule]",
                                    )
                                    live_ok = await exp_stream_manager.wait_until_live(timeout=900)
                                    if not live_ok:
                                        log_event(
                                            "Scheduler: stream did not become live within 15 minutes — skipping Discord announcement.",
                                            level="error", prefix="[exp-schedule]",
                                        )
                                        continue
                                    log_event(
                                        "Scheduler: stream is live; posting Discord announcement.",
                                        prefix="[exp-schedule]",
                                    )
                                    # Auto-post stream URL to configured
                                    # Discord channels (same embed as the
                                    # manual “Post Stream Link” buttons).
                                    stream_url = await db.get_setting("exp_radio_stream_url") or ""
                                    if stream_url:
                                        for slot in ("1", "2", "3"):
                                            ch_id = await db.get_setting(f"exp_radio_post_channel_{slot}_id") or ""
                                            if not ch_id:
                                                continue
                                            ok, info = await _post_exp_stream_announcement(ch_id, stream_url)
                                            if ok:
                                                log_event(
                                                    f"Scheduler: announced in #{info}.",
                                                    prefix="[exp-schedule]",
                                                )
                                            else:
                                                log_event(
                                                    f"Scheduler: announcement to channel {ch_id} failed \u2014 {info}",
                                                    level="error", prefix="[exp-schedule]",
                                                )
                                    else:
                                        log_event(
                                            "Scheduler: no stream URL configured \u2014 skipping Discord announcement.",
                                            prefix="[exp-schedule]",
                                        )
                                else:
                                    log_event(
                                        f"Scheduler: start failed \u2014 {result.get('error')}",
                                        level="error", prefix="[exp-schedule]",
                                    )
                            except Exception as e:
                                log_event(
                                    f"Scheduler: start exception: {e}",
                                    level="error", prefix="[exp-schedule]",
                                )
            except Exception as e:
                print(f"[exp-radio] Scheduler loop error: {e}", flush=True)
            await asyncio.sleep(30)

    @app.before_serving
    async def start_cleanup_task():
        app.radio_cleanup_task = asyncio.create_task(_radio_cleanup_loop())
        app.exp_radio_cleanup_task = asyncio.create_task(_exp_radio_cleanup_loop())
        app.exp_radio_schedule_task = asyncio.create_task(_exp_radio_schedule_loop())
        asyncio.create_task(_relic_hunt_autostart())

    # ── Experimental Radio ─────────────────────────────────────────────────────

    from bot.exp_stream_manager import ExpStreamManager
    exp_stream_manager = ExpStreamManager(db, EXP_RADIO_DIR)
    if bot is not None:
        bot.exp_stream_manager = exp_stream_manager

    from bot.relic_hunt import RelicHunt
    from bot.twitch_bot import TwitchBot as _TwitchBot
    from bot.live_log import log_event as _rh_log
    relic_hunt = RelicHunt(db)

    async def _relic_hunt_autostart():
        """Auto-start the Relic Hunt IRC listener on app boot if the game is enabled
        and the exp_radio_twitch credentials are configured."""
        await asyncio.sleep(3)  # brief delay so DB is fully ready
        try:
            await db.ensure_relic_tables()
            enabled = (await db.relic_get_setting("enabled")) != "false"
            if not enabled:
                return
            client_id   = await db.get_setting("exp_radio_twitch_client_id")
            refresh_tok = await db.get_setting("exp_radio_twitch_refresh_token")
            broadcaster = await db.get_setting("exp_radio_twitch_broadcaster_login")
            if not (client_id and refresh_tok and broadcaster):
                _rh_log("Twitch credentials not configured, skipping auto-start", "error", "[relic-hunt]")
                return
            bot = _TwitchBot(db, key_prefix="exp_radio_twitch")
            ok, msg = await bot.start()
            if not ok:
                _rh_log(f"Auto-start failed: {msg}", "error", "[relic-hunt]")
                return
            await relic_hunt.start(bot)
            _rh_log("Auto-started successfully", "info", "[relic-hunt]")
        except Exception as e:
            _rh_log(f"Auto-start error: {e}", "error", "[relic-hunt]")

    @app.route("/exp-radio", methods=["GET", "POST"])
    @permission_required('exp_radio')
    async def exp_radio_admin():
        import csv, io, uuid as _uuid
        from datetime import datetime, timezone
        from quart import jsonify, send_file

        if request.method == "POST":
            form = await request.form
            files = await request.files
            action = form.get("action", "")

            if action == "delete_song":
                song_id = int(form.get("song_id", 0))
                await db.delete_exp_radio_song(song_id)
                await flash("Song removed.", "success")

            elif action == "add_intro_song":
                suno_url = (form.get("suno_url") or "").strip()
                if not suno_url:
                    await flash("Please enter a Suno URL.", "error")
                else:
                    from bot.exp_radio_worker import process_intro_outro_song
                    async def _bg_intro(url=suno_url):
                        ok, msg = await process_intro_outro_song(db, url, "intro", EXP_RADIO_DIR)
                        from bot.exp_stream_manager import log_event
                        log_event(msg, "info" if ok else "error", "[intro]")
                    asyncio.create_task(_bg_intro())
                    await flash("Intro song queued for download and Whisper analysis.", "success")

            elif action == "add_outro_song":
                suno_url = (form.get("suno_url") or "").strip()
                if not suno_url:
                    await flash("Please enter a Suno URL.", "error")
                else:
                    from bot.exp_radio_worker import process_intro_outro_song
                    async def _bg_outro(url=suno_url):
                        ok, msg = await process_intro_outro_song(db, url, "outro", EXP_RADIO_DIR)
                        from bot.exp_stream_manager import log_event
                        log_event(msg, "info" if ok else "error", "[outro]")
                    asyncio.create_task(_bg_outro())
                    await flash("Outro song queued for download and Whisper analysis.", "success")

            elif action == "add_admin_song":
                suno_url = (form.get("suno_url") or "").strip()
                if not suno_url:
                    await flash("Please enter a Suno URL.", "error")
                else:
                    from bot.exp_radio_worker import process_admin_song
                    async def _bg_add(url=suno_url):
                        ok, msg = await process_admin_song(db, url, EXP_RADIO_DIR)
                        from bot.exp_stream_manager import log_event
                        log_event(msg, "info" if ok else "error", "[admin-pl]")
                    asyncio.create_task(_bg_add())
                    await flash(
                        "Song queued for download and Whisper analysis. "
                        "Check the Live Log for progress.", "success"
                    )

            elif action == "add_submission_song":
                suno_url = (form.get("suno_url") or "").strip()
                user_ref = (form.get("submission_user_ref") or "").strip()
                if not suno_url:
                    await flash("Please enter a Suno URL.", "error")
                else:
                    submitter_user_id = 0
                    submitter_user_name = "admin-ui"
                    if user_ref:
                        user = None
                        if user_ref.isdigit() and bot is not None:
                            submitter_user_id = int(user_ref)
                            try:
                                user = bot.get_user(submitter_user_id) or await bot.fetch_user(submitter_user_id)
                            except Exception:
                                user = None
                        elif bot is not None:
                            guild = get_guild()
                            query = user_ref.lower().lstrip("@").strip()
                            if guild:
                                members = list(getattr(guild, "members", []) or [])
                                def _names(member):
                                    return [
                                        str(getattr(member, "name", "") or ""),
                                        str(getattr(member, "display_name", "") or ""),
                                        str(getattr(member, "global_name", "") or ""),
                                    ]
                                exact = [
                                    m for m in members
                                    if any(n.lower() == query for n in _names(m) if n)
                                ]
                                matches = exact or [
                                    m for m in members
                                    if any(query in n.lower() for n in _names(m) if n)
                                ]
                                if len(matches) == 1:
                                    user = matches[0]
                                elif len(matches) > 1:
                                    names = ", ".join(str(m) for m in matches[:5])
                                    await flash(f"Discord user name is ambiguous. Use the numeric user ID. Matches: {names}", "error")
                                    return redirect(request.url)
                        if user is None:
                            await flash("Discord user not found. Use the numeric user ID or leave the field empty.", "error")
                            return redirect(request.url)
                        submitter_user_id = int(user.id)
                        submitter_user_name = str(user)
                    from bot.exp_radio_worker import process_admin_submission_song
                    async def _bg_add_submission(
                        url=suno_url,
                        uid=submitter_user_id,
                        uname=submitter_user_name,
                    ):
                        ok, msg = await process_admin_submission_song(
                            db, url, EXP_RADIO_DIR, bot=bot,
                            submitter_user_id=uid,
                            submitter_user_name=uname,
                        )
                        from bot.exp_stream_manager import log_event
                        log_event(msg, "info" if ok else "error", "[submit-admin]")
                    asyncio.create_task(_bg_add_submission())
                    await flash(
                        "Song queued for the regular submission playlist. "
                        "It will run through the normal Whisper and moderation pipeline.",
                        "success",
                    )

            elif action == "delete_all_songs":
                import bot.exp_stream_manager as _esm
                if _esm.stream_is_live:
                    await flash("Cannot clear the playlist while the stream is live.", "error")
                else:
                    count = await db.delete_all_exp_radio_songs(source="submission")
                    await flash(f"Playlist cleared — {count} song(s) removed.", "success")

            elif action == "delete_all_admin_songs":
                import bot.exp_stream_manager as _esm
                if _esm.stream_is_live:
                    await flash("Cannot clear the admin playlist while the stream is live.", "error")
                else:
                    count = await db.delete_all_exp_radio_songs(source="admin")
                    await flash(f"Admin playlist cleared — {count} song(s) removed.", "success")

            elif action == "reanalyze_whisper":
                import bot.exp_stream_manager as _esm
                if _esm.stream_is_live:
                    await flash("Cannot run Whisper while the stream is live.", "error")
                else:
                    from bot.exp_radio_worker import process_exp_song
                    songs_all = await db.get_all_exp_radio_songs(active_only=True)
                    queued = 0
                    for s in songs_all:
                        if not s.get("mp3_filename"):
                            continue  # MP3 never finished uploading — skip
                        # Drop stale ASS so the player won't pick it up mid-rebuild
                        await db.update_exp_radio_song(
                            s["id"], analysis_status="processing", ass_filename=None,
                        )
                        asyncio.create_task(
                            process_exp_song(db, s["id"], EXP_RADIO_DIR, bot=bot)
                        )
                        queued += 1
                    await flash(
                        f"Queued {queued} song(s) for re-analysis. "
                        "Whisper runs in the background — refresh the page to see status updates.",
                        "success",
                    )

            elif action == "rescrape_metadata_one":
                from bot.exp_radio_worker import scrape_suno
                from bot.exp_stream_manager import log_event
                sid = int(form.get("song_id", "0") or 0)
                s = await db.get_exp_radio_song(sid) if sid else None
                if not s or not s.get("suno_uuid"):
                    await flash("Song not found.", "error")
                else:
                    uuid = s["suno_uuid"]
                    log_event(f"Refreshing metadata for #{sid} (uuid={uuid})…", prefix="[meta]")
                    meta = await scrape_suno(uuid)
                    fields = {}
                    if meta.get("title"):     fields["title"]     = meta["title"]
                    if meta.get("artist"):    fields["artist"]    = meta["artist"]
                    if meta.get("video_url"): fields["video_url"] = meta["video_url"]
                    if meta.get("cover_url"): fields["cover_url"] = meta["cover_url"]
                    if fields:
                        await db.update_exp_radio_song(sid, **fields)
                        for ext in (".jpg", ".mp4"):
                            cached = os.path.join(EXP_RADIO_DIR, "cover_cache", f"{uuid}{ext}")
                            if os.path.exists(cached):
                                try: os.remove(cached)
                                except Exception: pass
                        log_event(
                            f"Refreshed metadata for #{sid} ({fields.get('title') or s.get('title')!r}) "
                            f"— updated: {', '.join(sorted(fields.keys()))}",
                            prefix="[meta]",
                        )
                        await flash(f"Refreshed metadata for “{fields.get('title') or s.get('title')}”.", "success")
                    else:
                        log_event(
                            f"No usable metadata returned for #{sid} (uuid={uuid})",
                            level="error", prefix="[meta]",
                        )
                        await flash("Nothing to update — Suno returned no usable metadata.", "warning")

            elif action == "renormalize_cover_one":
                sid = int(form.get("song_id", "0") or 0)
                s = await db.get_exp_radio_song(sid) if sid else None
                if not s:
                    await flash("Song not found.", "error")
                else:
                    ok, msg = await exp_stream_manager.renormalize_cover(s)
                    await flash(
                        f"{'✅' if ok else '❌'} {s.get('title') or sid}: {msg}",
                        "success" if ok else "error",
                    )

            elif action == "renormalize_cover_all":
                from bot.exp_stream_manager import log_event
                songs_all = await db.get_all_exp_radio_songs(active_only=True)
                log_event(
                    f"Bulk cover normalize started for {len(songs_all)} song(s)\u2026",
                    prefix="[cover]",
                )
                # Run in the background so the HTTP request returns immediately
                # (each individual normalization spawns ffprobe + ffmpeg and
                # would otherwise block the request for tens of seconds).
                async def _bulk_renorm(songs):
                    ok_n = fail_n = skip_n = 0
                    for s in songs:
                        title = s.get("title") or s.get("id")
                        try:
                            ok, msg = await exp_stream_manager.renormalize_cover(s)
                        except Exception as e:
                            log_event(
                                f"  #{s.get('id')} ({title!r}) error: {e}",
                                level="error", prefix="[cover]",
                            )
                            fail_n += 1
                            continue
                        if ok:
                            log_event(
                                f"  #{s.get('id')} ({title!r}): {msg}",
                                prefix="[cover]",
                            )
                            ok_n += 1
                        else:
                            # 'No usable cover URL' / 'failed' — distinguish
                            level = "error" if "fail" in msg.lower() else "info"
                            log_event(
                                f"  #{s.get('id')} ({title!r}): {msg}",
                                level=level, prefix="[cover]",
                            )
                            if level == "error":
                                fail_n += 1
                            else:
                                skip_n += 1
                    log_event(
                        f"Bulk cover normalize done: {ok_n} ok, {fail_n} failed, "
                        f"{skip_n} skipped (no cached cover).",
                        prefix="[cover]",
                    )
                asyncio.create_task(_bulk_renorm(songs_all))
                await flash(
                    f"Queued cover normalization for {len(songs_all)} song(s) \u2014 "
                    "watch the Live Log for progress.",
                    "success",
                )

            elif action == "approve_moderation_one":
                sid = int(form.get("song_id", "0") or 0)
                s = await db.get_exp_radio_song(sid) if sid else None
                if not s:
                    await flash("Song not found.", "error")
                else:
                    await db.update_exp_radio_song(
                        sid,
                        moderation_status="approved",
                        moderation_at=time.time(),
                    )
                    await flash(
                        f"✅ Approved “{s.get('title') or sid}” for the stream playlist.",
                        "success",
                    )

            elif action == "approve_admin_whisper_bypass":
                from bot.exp_stream_manager import log_event
                sid = int(form.get("song_id", "0") or 0)
                s = await db.get_exp_radio_song(sid) if sid else None
                if not s:
                    await flash("Song not found.", "error")
                elif s.get("playlist_source") != "admin":
                    await flash("This bypass is only available for admin playlist songs.", "error")
                elif not s.get("mp3_filename"):
                    await flash("Cannot approve yet: MP3 download is not complete.", "error")
                else:
                    await db.update_exp_radio_song(
                        sid,
                        analysis_status="done",
                        word_timestamps="[]",
                        ass_filename=None,
                        moderation_status="approved",
                        moderation_reason="Admin playlist: Whisper transcript bypassed.",
                        moderation_at=time.time(),
                    )
                    log_event(
                        f"Admin bypassed Whisper transcript for #{sid} "
                        f"({s.get('title') or s.get('suno_url') or sid!r}); marked stream-ready.",
                        prefix="[admin-pl]",
                    )
                    await flash(
                        f"✅ Marked “{s.get('title') or sid}” as ready without Whisper transcript.",
                        "success",
                    )

            elif action == "remoderate_one":
                import bot.exp_stream_manager as _esm
                if _esm.stream_is_live:
                    await flash("Cannot run LLM moderation while the stream is live.", "error")
                else:
                    from bot.exp_moderation import moderate_lyrics
                    from bot.llm import OllamaClient
                    from config import Config
                    sid = int(form.get("song_id", "0") or 0)
                    s = await db.get_exp_radio_song(sid) if sid else None
                    if not s or not s.get("lyrics"):
                        await flash("Song not found or has no lyrics yet.", "error")
                    else:
                        from bot.exp_stream_manager import log_event
                        async def _run_remoderation(song_id, snap):
                            title_s = snap.get("title") or f"#{song_id}"
                            try:
                                client = OllamaClient(
                                    base_url=Config.OLLAMA_URL,
                                    model=Config.LLM_MODEL,
                                    timeout=Config.LLM_REQUEST_TIMEOUT,
                                )
                                log_event(
                                    f"Re-moderation start for #{song_id} ({title_s!r})",
                                    prefix="[mod]",
                                )
                                verdict = await moderate_lyrics(
                                    client,
                                    lyrics=snap.get("lyrics") or "",
                                    title=snap.get("title") or "",
                                    artist=snap.get("artist") or "",
                                )
                                await db.update_exp_radio_song(
                                    song_id,
                                    moderation_status=verdict["status"],
                                    moderation_reason=verdict.get("reason") or "",
                                    moderation_at=time.time(),
                                )
                                level = "error" if verdict["status"] in ("flagged", "pending") else "info"
                                summary = (
                                    f"Re-moderation #{song_id} → {verdict['status']}"
                                    f"{' (translated)' if verdict.get('translated') else ''}"
                                )
                                if verdict.get("reason"):
                                    summary += f": {verdict['reason']}"
                                log_event(summary, level=level, prefix="[mod]")
                            except Exception as e:
                                log_event(
                                    f"Re-moderation error #{song_id}: {e}",
                                    level="error", prefix="[mod]",
                                )
                                await db.update_exp_radio_song(
                                    song_id,
                                    moderation_status="pending",
                                    moderation_reason=f"Re-moderation error: {e!s}",
                                    moderation_at=time.time(),
                                )
                        await db.update_exp_radio_song(
                            sid,
                            moderation_status="pending",
                            moderation_reason="Re-moderation in progress…",
                            moderation_at=time.time(),
                        )
                        asyncio.create_task(_run_remoderation(sid, s))
                        await flash(
                            f"Queued “{s.get('title') or sid}” for LLM re-moderation. "
                            "Refresh the page in a few seconds.",
                            "success",
                        )

            elif action == "reanalyze_whisper_one":
                import bot.exp_stream_manager as _esm
                if _esm.stream_is_live:
                    await flash("Cannot run Whisper while the stream is live.", "error")
                else:
                    from bot.exp_radio_worker import process_exp_song
                    sid = int(form.get("song_id", "0") or 0)
                    s = await db.get_exp_radio_song(sid) if sid else None
                    if not s or not s.get("mp3_filename"):
                        await flash("Song not found or MP3 missing.", "error")
                    else:
                        await db.update_exp_radio_song(
                            sid, analysis_status="processing", ass_filename=None,
                        )
                        asyncio.create_task(
                            process_exp_song(db, sid, EXP_RADIO_DIR, bot=bot)
                        )
                        await flash(
                            f"Queued “{s.get('title') or sid}” for Whisper re-analysis. "
                            "Refresh the page for status.",
                            "success",
                        )

            elif action == "rescrape_metadata":
                from bot.exp_radio_worker import scrape_suno
                from bot.exp_stream_manager import log_event
                songs_all = await db.get_all_exp_radio_songs(active_only=True)
                log_event(
                    f"Bulk metadata refresh started for {len(songs_all)} song(s)…",
                    prefix="[meta]",
                )
                updated = 0
                for s in songs_all:
                    uuid = s.get("suno_uuid")
                    if not uuid:
                        continue
                    meta = await scrape_suno(uuid)
                    real_uuid = meta.get("real_uuid") or uuid
                    fields = {}
                    if meta.get("title"):     fields["title"]     = meta["title"]
                    if meta.get("artist"):    fields["artist"]    = meta["artist"]
                    if meta.get("video_url"): fields["video_url"] = meta["video_url"]
                    if meta.get("cover_url"): fields["cover_url"] = meta["cover_url"]
                    if fields:
                        await db.update_exp_radio_song(s["id"], **fields)
                        updated += 1
                        log_event(
                            f"  #{s['id']} ({fields.get('title') or s.get('title')!r}) updated: "
                            f"{', '.join(sorted(fields.keys()))}",
                            prefix="[meta]",
                        )
                        # Invalidate cached media so next stream run downloads fresh
                        for ext in (".jpg", ".mp4"):
                            cached = os.path.join(EXP_RADIO_DIR, "cover_cache", f"{uuid}{ext}")
                            if os.path.exists(cached):
                                try: os.remove(cached)
                                except Exception: pass
                log_event(f"Bulk metadata refresh done: {updated}/{len(songs_all)} updated.", prefix="[meta]")
                await flash(f"Refreshed metadata for {updated} song(s).", "success")

            elif action == "save_stream_key":
                key = form.get("exp_twitch_key", "").strip()
                await db.set_setting("exp_radio_twitch_key", key)
                await flash("Stream key saved.", "success")

            elif action == "save_exp_settings":
                ch1 = form.get("exp_post_channel_1_id", "").strip()
                ch2 = form.get("exp_post_channel_2_id", "").strip()
                ch3 = form.get("exp_post_channel_3_id", "").strip()
                expiry_ch = form.get("exp_expiry_channel_id", "").strip()
                announcement_ch = form.get("exp_announcement_channel_id", "").strip()
                announcement_msg = (form.get("exp_announcement_message") or "").strip()
                stream_url_v = form.get("exp_stream_url", "").strip()
                moderation_en = "on" if form.get("exp_moderation_enabled") else "off"
                loop_mode_v = form.get("exp_loop_mode", "reshuffle").strip()
                if loop_mode_v not in ("stop", "reshuffle"):
                    loop_mode_v = "reshuffle"
                # Auto-start scheduler
                sched_en = "on" if form.get("exp_schedule_enabled") else "off"
                # Day checkboxes named exp_schedule_day_0..6 (Mon=0 .. Sun=6)
                sched_days = ",".join(
                    str(i) for i in range(7) if form.get(f"exp_schedule_day_{i}")
                )
                sched_time = (form.get("exp_schedule_time") or "").strip()
                # Validate HH:MM
                import re as _re
                if not _re.match(r"^\d{1,2}:\d{2}$", sched_time):
                    sched_time = ""
                await db.set_setting("exp_radio_post_channel_1_id", ch1)
                await db.set_setting("exp_radio_post_channel_2_id", ch2)
                await db.set_setting("exp_radio_post_channel_3_id", ch3)
                await db.set_setting("exp_radio_expiry_channel_id", expiry_ch)
                await db.set_setting("exp_radio_announcement_channel_id", announcement_ch)
                await db.set_setting("exp_radio_announcement_message", announcement_msg)
                progress_overlay_en = "on" if form.get("exp_progress_overlay") else "off"
                try:
                    max_per_user_v = max(1, min(20, int(form.get("exp_max_per_user", "4") or "4")))
                except (ValueError, TypeError):
                    max_per_user_v = 4
                try:
                    expiry_days_v = int(form.get("exp_expiry_days", "14") or "14")
                except (ValueError, TypeError):
                    expiry_days_v = 14
                if expiry_days_v not in (7, 14):
                    expiry_days_v = 14
                try:
                    video_bitrate_v = int(form.get("exp_video_bitrate_kbps", "2500") or "2500")
                except (ValueError, TypeError):
                    video_bitrate_v = 2500
                if video_bitrate_v not in (1800, 2000, 2500):
                    video_bitrate_v = 2500
                await db.set_setting("exp_radio_max_per_user", str(max_per_user_v))
                await db.set_setting("exp_radio_expiry_days", str(expiry_days_v))
                await db.set_setting("exp_radio_video_bitrate_kbps", str(video_bitrate_v))
                await db.set_setting("exp_radio_moderation_enabled", moderation_en)
                await db.set_setting("exp_radio_loop_mode", loop_mode_v)
                await db.set_setting("exp_radio_schedule_enabled", sched_en)
                await db.set_setting("exp_radio_schedule_days", sched_days)
                await db.set_setting("exp_radio_schedule_time", sched_time)
                await db.set_setting("exp_radio_progress_overlay", progress_overlay_en)
                active_pl_v = form.get("exp_active_playlist", "submission")
                if active_pl_v not in ("submission", "admin", "both"):
                    active_pl_v = "submission"
                await db.set_setting("exp_radio_active_playlist", active_pl_v)
                intro_en = "on" if form.get("exp_intro_enabled") else "off"
                outro_en = "on" if form.get("exp_outro_enabled") else "off"
                intro_selection = (form.get("exp_intro_selection") or "random").strip()
                outro_selection = (form.get("exp_outro_selection") or "random").strip()
                await db.set_setting("exp_radio_intro_enabled", intro_en)
                await db.set_setting("exp_radio_outro_enabled", outro_en)
                await db.set_setting("exp_radio_intro_selection", intro_selection)
                await db.set_setting("exp_radio_outro_selection", outro_selection)
                if stream_url_v:
                    await db.set_setting("exp_radio_stream_url", stream_url_v)
                await flash("Settings saved.", "success")

            elif action == "save_exp_twitch_settings":
                exp_tw_cid = form.get("exp_twitch_client_id", "").strip()
                exp_tw_sec = form.get("exp_twitch_client_secret", "").strip()
                exp_tw_rt  = form.get("exp_twitch_refresh_token", "").strip()
                exp_tw_bc  = form.get("exp_twitch_broadcaster_login", "").strip()
                chat_en    = "on" if form.get("exp_twitch_chat_enabled") else "off"
                if exp_tw_cid:
                    await db.set_setting("exp_radio_twitch_client_id", exp_tw_cid)
                if exp_tw_sec and not exp_tw_sec.startswith("****"):
                    await db.set_setting("exp_radio_twitch_client_secret", exp_tw_sec)
                if exp_tw_rt and not exp_tw_rt.startswith("****"):
                    await db.set_setting("exp_radio_twitch_refresh_token", exp_tw_rt)
                    await db.set_setting("exp_radio_twitch_bot_login", "")
                    await db.set_setting("exp_radio_twitch_bot_user_id", "")
                if exp_tw_bc:
                    bn = exp_tw_bc.strip().rstrip("/").lstrip("#").lower()
                    if "twitch.tv/" in bn:
                        bn = bn.split("twitch.tv/", 1)[1].split("/")[0]
                    await db.set_setting("exp_radio_twitch_broadcaster_login", bn)
                await db.set_setting("exp_radio_twitch_chat_enabled", chat_en)
                await flash("Twitch Chat Bot settings saved.", "success")

            elif action == "post_exp_stream_url":
                ch_id = form.get("post_channel_id_select", "")
                exp_stream_url_v = await db.get_setting("exp_radio_stream_url") or ""
                if not exp_stream_url_v:
                    await flash("No stream URL configured. Open Settings to add one.", "danger")
                else:
                    ok, name_or_err = await _post_exp_stream_announcement(ch_id, exp_stream_url_v)
                    if ok:
                        await flash(f"Stream link posted to #{name_or_err}.", "success")
                    else:
                        await flash(f"Could not post: {name_or_err}", "danger")

            elif action == "post_exp_announcement":
                ch_id = await db.get_setting("exp_radio_announcement_channel_id") or ""
                message_text = (await db.get_setting("exp_radio_announcement_message") or "").strip()
                if not ch_id:
                    await flash("No announcement channel configured. Open Settings to add one.", "danger")
                elif not message_text:
                    await flash("No announcement message configured. Open Settings to add one.", "danger")
                else:
                    guild = get_guild()
                    channel = None
                    if guild:
                        try:
                            ch_int = int(ch_id)
                            channel = guild.get_channel(ch_int) or guild.get_thread(ch_int)
                        except Exception:
                            channel = None
                    if not channel:
                        await flash("Announcement channel not found.", "danger")
                    else:
                        try:
                            await channel.send(message_text)
                            await flash(f"Announcement posted to #{channel.name}.", "success")
                        except Exception as e:
                            await flash(f"Could not post announcement: {e}", "danger")

            elif action == "upload_background":
                bg_file = files.get("bg_file")
                if bg_file and bg_file.filename:
                    ext = bg_file.filename.rsplit(".", 1)[-1].lower()
                    if ext in ("jpg", "jpeg", "png", "mp4", "webm"):
                        bg_type = "video" if ext in ("mp4", "webm") else "image"
                        fn = f"exp_bg_{_uuid.uuid4().hex}.{ext}"
                        await bg_file.save(os.path.join(EXP_RADIO_DIR, "assets", fn))
                        old = await db.get_setting("exp_radio_bg_filename")
                        if old:
                            old_path = os.path.join(EXP_RADIO_DIR, "assets", old)
                            if os.path.exists(old_path):
                                os.remove(old_path)
                        await db.set_setting("exp_radio_bg_filename", fn)
                        await db.set_setting("exp_radio_bg_type", bg_type)
                        await flash("Background uploaded.", "success")
                    else:
                        await flash("Unsupported file type.", "danger")

            elif action == "upload_loop_video":
                import json as _jl
                lv_file = files.get("lv_file")
                if lv_file and lv_file.filename:
                    ext = lv_file.filename.rsplit(".", 1)[-1].lower()
                    if ext in ("mp4", "webm"):
                        fn = f"exp_loop_{_uuid.uuid4().hex}.{ext}"
                        label = form.get("lv_label", "").strip() or lv_file.filename
                        await lv_file.save(os.path.join(EXP_RADIO_DIR, "assets", fn))
                        raw = await db.get_setting("exp_radio_loop_videos") or "[]"
                        vids = _jl.loads(raw) if raw else []
                        vids.append({"filename": fn, "label": label})
                        await db.set_setting("exp_radio_loop_videos", _jl.dumps(vids))
                        # If this is the first video, auto-select it
                        if len(vids) == 1:
                            await db.set_setting("exp_radio_loop_selection", fn)
                        await flash("Loop video uploaded.", "success")
                    else:
                        await flash("Only MP4/WebM supported.", "danger")

            elif action == "save_exp_loop_source":
                obs_overlay_enabled_v = "on" if form.get("exp_obs_overlay_enabled") else "off"
                loop_rtmp_key_v = (form.get("exp_loop_rtmp_key") or "").strip()
                try:
                    obs_overlay_fps_v = int(form.get("exp_obs_overlay_fps", "20") or "20")
                except (TypeError, ValueError):
                    obs_overlay_fps_v = 20
                if obs_overlay_fps_v not in (15, 20, 24):
                    obs_overlay_fps_v = 20
                if obs_overlay_enabled_v == "on" and not loop_rtmp_key_v:
                    import secrets as _secrets
                    loop_rtmp_key_v = _secrets.token_urlsafe(18)
                await db.set_setting("exp_radio_obs_overlay_enabled", obs_overlay_enabled_v)
                await db.set_setting("exp_radio_obs_overlay_fps", str(obs_overlay_fps_v))
                await db.set_setting("exp_radio_loop_source", "local")
                await db.set_setting("exp_radio_loop_rtmp_key", loop_rtmp_key_v)
                await flash("OBS overlay settings saved.", "success")

            elif action == "delete_loop_video":
                import json as _jl
                del_fn = form.get("loop_filename", "")
                if del_fn:
                    raw = await db.get_setting("exp_radio_loop_videos") or "[]"
                    vids = _jl.loads(raw) if raw else []
                    vids = [v for v in vids if v.get("filename") != del_fn]
                    await db.set_setting("exp_radio_loop_videos", _jl.dumps(vids))
                    del_path = os.path.join(EXP_RADIO_DIR, "assets", del_fn)
                    if os.path.exists(del_path):
                        os.remove(del_path)
                    # If the deleted video was selected, fall back to shuffle
                    sel = await db.get_setting("exp_radio_loop_selection") or "shuffle"
                    if sel == del_fn:
                        await db.set_setting("exp_radio_loop_selection", "shuffle")
                    await flash("Loop video removed.", "success")

            elif action == "set_loop_selection":
                sel = form.get("loop_selection", "shuffle")
                await db.set_setting("exp_radio_loop_selection", sel)
                loop_selection_labels = {
                    "shuffle": "Shuffle",
                    "concat_all": "Concatenate all videos",
                    "concat_all_random": "Concatenate all videos in random order",
                }
                await flash(f"Loop video selection: {loop_selection_labels.get(sel, sel)}", "success")

            return redirect(request.url)

        songs = await db.get_all_exp_radio_songs(active_only=True, source="submission")
        # Enrich each song with parsed analysis info for the admin UI:
        # word_count, coverage span and a transcript preview reconstructed
        # from the stored word_timestamps JSON.
        import json as _json
        for s in songs:
            wt_raw = s.get("word_timestamps")
            s["word_count"] = 0
            s["transcript_preview"] = ""
            s["transcript_span"] = None
            if wt_raw:
                try:
                    wt = _json.loads(wt_raw) if isinstance(wt_raw, str) else wt_raw
                    if isinstance(wt, list) and wt:
                        s["word_count"] = len(wt)
                        s["transcript_span"] = (
                            float(wt[0].get("start", 0)),
                            float(wt[-1].get("end", 0)),
                        )
                        # Reconstructed transcript with [mm:ss] markers every ~10s
                        parts, last_mark = [], -10.0
                        for w in wt:
                            st = float(w.get("start", 0))
                            if st - last_mark >= 10:
                                m, sec = divmod(int(st), 60)
                                parts.append(f"\n[{m}:{sec:02d}] ")
                                last_mark = st
                            parts.append(w.get("word", ""))
                            parts.append(" ")
                        s["transcript_preview"] = "".join(parts).strip()
                except Exception:
                    pass
        status = await exp_stream_manager.get_status()
        masked_key = "*" * 20 if await db.get_setting("exp_radio_twitch_key") else ""
        bg_filename  = await db.get_setting("exp_radio_bg_filename") or ""
        import json as _jlv
        _loop_raw = await db.get_setting("exp_radio_loop_videos") or "[]"
        loop_videos = _jlv.loads(_loop_raw) if _loop_raw else []
        # Auto-migrate legacy single-video setting
        if not loop_videos:
            _old_lv = await db.get_setting("exp_radio_loop_filename") or ""
            if _old_lv:
                loop_videos = [{"filename": _old_lv, "label": _old_lv}]
                await db.set_setting("exp_radio_loop_videos", _jlv.dumps(loop_videos))
                await db.set_setting("exp_radio_loop_selection", _old_lv)
                await db.set_setting("exp_radio_loop_filename", "")
        loop_selection = await db.get_setting("exp_radio_loop_selection") or "shuffle"
        exp_stream_url = await db.get_setting("exp_radio_stream_url") or ""
        exp_post_channel_1_id = await db.get_setting("exp_radio_post_channel_1_id") or ""
        exp_post_channel_2_id = await db.get_setting("exp_radio_post_channel_2_id") or ""
        exp_post_channel_3_id = await db.get_setting("exp_radio_post_channel_3_id") or ""
        exp_expiry_channel_id = await db.get_setting("exp_radio_expiry_channel_id") or ""
        exp_announcement_channel_id = await db.get_setting("exp_radio_announcement_channel_id") or ""
        exp_announcement_message = await db.get_setting("exp_radio_announcement_message") or ""
        exp_twitch_chat_enabled = await db.get_setting("exp_radio_twitch_chat_enabled") or "off"
        exp_moderation_enabled  = await db.get_setting("exp_radio_moderation_enabled") or "off"
        exp_loop_mode           = await db.get_setting("exp_radio_loop_mode") or "reshuffle"
        exp_progress_overlay    = await db.get_setting("exp_radio_progress_overlay") or "off"
        exp_max_per_user        = int(await db.get_setting("exp_radio_max_per_user") or "4")
        exp_expiry_days         = int(await db.get_setting("exp_radio_expiry_days") or "14")
        if exp_expiry_days not in (7, 14):
            exp_expiry_days = 14
        try:
            exp_video_bitrate_kbps = int(await db.get_setting("exp_radio_video_bitrate_kbps") or "2500")
        except (TypeError, ValueError):
            exp_video_bitrate_kbps = 2500
        if exp_video_bitrate_kbps not in (1800, 2000, 2500):
            exp_video_bitrate_kbps = 2500
        exp_loop_source        = await db.get_setting("exp_radio_loop_source") or "local"
        if exp_loop_source not in ("local", "rtmp"):
            exp_loop_source = "local"
        exp_obs_overlay_enabled = await db.get_setting("exp_radio_obs_overlay_enabled") or "off"
        if exp_loop_source == "rtmp":
            exp_obs_overlay_enabled = "on"
        try:
            exp_obs_overlay_fps = int(await db.get_setting("exp_radio_obs_overlay_fps") or "20")
        except (TypeError, ValueError):
            exp_obs_overlay_fps = 20
        if exp_obs_overlay_fps not in (15, 20, 24):
            exp_obs_overlay_fps = 20
        exp_loop_rtmp_key      = await db.get_setting("exp_radio_loop_rtmp_key") or ""
        exp_schedule_enabled    = await db.get_setting("exp_radio_schedule_enabled") or "off"
        exp_schedule_days       = await db.get_setting("exp_radio_schedule_days") or ""
        exp_schedule_time       = await db.get_setting("exp_radio_schedule_time") or ""
        exp_schedule_days_set   = set(d for d in exp_schedule_days.split(",") if d)
        exp_active_playlist     = await db.get_setting("exp_radio_active_playlist") or "submission"
        exp_intro_enabled       = await db.get_setting("exp_radio_intro_enabled") or "off"
        exp_outro_enabled       = await db.get_setting("exp_radio_outro_enabled") or "off"
        exp_intro_selection     = await db.get_setting("exp_radio_intro_selection") or "random"
        exp_outro_selection     = await db.get_setting("exp_radio_outro_selection") or "random"
        exp_tw_client_id = await db.get_setting("exp_radio_twitch_client_id") or ""
        _exp_tw_secret   = await db.get_setting("exp_radio_twitch_client_secret") or ""
        _exp_tw_refresh  = await db.get_setting("exp_radio_twitch_refresh_token") or ""
        exp_tw_secret_masked  = f"****{_exp_tw_secret[-4:]}"  if len(_exp_tw_secret)  > 4 else ""
        exp_tw_refresh_masked = f"****{_exp_tw_refresh[-4:]}" if len(_exp_tw_refresh) > 4 else ""
        exp_tw_broadcaster = await db.get_setting("exp_radio_twitch_broadcaster_login") or ""
        exp_tw_bot_login   = await db.get_setting("exp_radio_twitch_bot_login") or ""
        # Quick scope check for UI badge (uses cached access token if available)
        exp_tw_scopes_ok = False
        try:
            import aiohttp as _ah
            _rt = await db.get_setting("exp_radio_twitch_refresh_token") or ""
            _cid = await db.get_setting("exp_radio_twitch_client_id") or ""
            _cs  = await db.get_setting("exp_radio_twitch_client_secret") or ""
            if _rt and _cid and _cs:
                async with _ah.ClientSession() as _hs:
                    async with _hs.post(
                        "https://id.twitch.tv/oauth2/token",
                        data={"client_id": _cid, "client_secret": _cs,
                              "grant_type": "refresh_token", "refresh_token": _rt},
                        timeout=_ah.ClientTimeout(total=8),
                    ) as _hr:
                        _hd = await _hr.json()
                        _tok = _hd.get("access_token", "")
                    if _tok:
                        async with _hs.get(
                            "https://id.twitch.tv/oauth2/validate",
                            headers={"Authorization": f"OAuth {_tok}"},
                            timeout=_ah.ClientTimeout(total=8),
                        ) as _hr:
                            _hv = await _hr.json()
                            exp_tw_scopes_ok = "chat:read" in (_hv.get("scopes") or [])
        except Exception:
            pass
        exp_guild = get_guild()
        exp_text_channels = []
        if exp_guild:
            for _ch in sorted(exp_guild.text_channels, key=lambda c: c.position):
                exp_text_channels.append({"id": _ch.id, "name": _ch.name})
        admin_songs = await db.get_all_exp_radio_songs(active_only=False, source="admin")
        intro_songs = await db.get_all_exp_radio_songs(active_only=False, source="intro")
        outro_songs = await db.get_all_exp_radio_songs(active_only=False, source="outro")
        active_intro_songs = [s for s in intro_songs if s.get("active") and s.get("analysis_status") == "done" and s.get("mp3_filename")]
        active_outro_songs = [s for s in outro_songs if s.get("active") and s.get("analysis_status") == "done" and s.get("mp3_filename")]
        return await render_template(
            "exp_radio.html",
            songs=songs, status=status,
            admin_songs=admin_songs,
            intro_songs=intro_songs,
            outro_songs=outro_songs,
            exp_active_playlist=exp_active_playlist,
            exp_intro_enabled=exp_intro_enabled,
            exp_outro_enabled=exp_outro_enabled,
            exp_intro_selection=exp_intro_selection,
            exp_outro_selection=exp_outro_selection,
            active_intro_songs=active_intro_songs,
            active_outro_songs=active_outro_songs,
            masked_key=masked_key,
            bg_filename=bg_filename,
            loop_videos=loop_videos, loop_selection=loop_selection,
            exp_stream_url=exp_stream_url,
            exp_post_channel_1_id=exp_post_channel_1_id,
            exp_post_channel_2_id=exp_post_channel_2_id,
            exp_post_channel_3_id=exp_post_channel_3_id,
            exp_expiry_channel_id=exp_expiry_channel_id,
            exp_announcement_channel_id=exp_announcement_channel_id,
            exp_announcement_message=exp_announcement_message,
            exp_twitch_chat_enabled=exp_twitch_chat_enabled,
            exp_moderation_enabled=exp_moderation_enabled,
            exp_loop_mode=exp_loop_mode,
            exp_loop_source=exp_loop_source,
            exp_obs_overlay_enabled=exp_obs_overlay_enabled,
            exp_obs_overlay_fps=exp_obs_overlay_fps,
            exp_loop_rtmp_key=exp_loop_rtmp_key,
            exp_progress_overlay=exp_progress_overlay,
            exp_max_per_user=exp_max_per_user,
            exp_expiry_days=exp_expiry_days,
            exp_video_bitrate_kbps=exp_video_bitrate_kbps,
            exp_schedule_enabled=exp_schedule_enabled,
            exp_schedule_time=exp_schedule_time,
            exp_schedule_days_set=exp_schedule_days_set,
            exp_tw_client_id=exp_tw_client_id,
            exp_tw_secret_masked=exp_tw_secret_masked,
            exp_tw_refresh_masked=exp_tw_refresh_masked,
            exp_tw_broadcaster=exp_tw_broadcaster,
            exp_tw_bot_login=exp_tw_bot_login,
            exp_tw_scopes_ok=exp_tw_scopes_ok,
            text_channels=exp_text_channels,
        )

    @app.route("/exp-radio/upload/<token>/resolve")
    async def exp_radio_upload_resolve(token: str):
        """Resolve the real Suno UUID (full UUID) for the upload page JS."""
        from quart import jsonify
        import aiohttp, re as _re
        song = await db.get_exp_radio_song_by_token(token)
        if not song:
            return jsonify({"error": "Invalid token"}), 403
        suno_uuid = song["suno_uuid"]
        # Try to get the real full UUID by scraping the Suno song page
        is_full_uuid = bool(_re.match(
            r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$', suno_uuid
        ))
        if is_full_uuid:
            return jsonify({"real_uuid": suno_uuid})
        # Short ID — scrape the song page to get the real UUID
        url = f"https://suno.com/s/{suno_uuid}"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(url, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return jsonify({"error": f"Suno returned {resp.status}"}), 502
                    html = await resp.text()
            m = _re.search(
                r'"id"\s*:\s*"([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})"',
                html,
            )
            if not m:
                m = _re.search(
                    r'song/([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})',
                    html,
                )
            real_uuid = m.group(1) if m else None
            if not real_uuid:
                return jsonify({"error": "Could not resolve real UUID from Suno page"}), 502
            return jsonify({"real_uuid": real_uuid})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/exp-radio/upload/<token>")
    async def exp_radio_upload_page(token: str):
        """Public page (no login) — browser fetches MP3 from Suno CDN and POSTs it here."""
        from bot.exp_stream_manager import is_submissions_locked as _is_locked
        song = await db.get_exp_radio_song_by_token(token)
        if not song:
            return await render_template("exp_radio_upload.html", error="Link invalid or expired.")
        if song.get("mp3_filename"):
            return await render_template("exp_radio_upload.html", done=True,
                                         title=song.get("title") or "Your song")
        _locked, _lock_reason = await _is_locked(db)
        if _locked:
            return await render_template("exp_radio_upload.html", stream_live=True)
        expiry_days = 14
        try:
            expiry_days = max(1, round((float(song.get("expires_at") or 0) - time.time()) / 86400))
        except Exception:
            pass
        return await render_template("exp_radio_upload.html", song=song, token=token, expiry_days=expiry_days)

    @app.route("/exp-radio/upload/<token>", methods=["POST"])
    async def exp_radio_upload_receive(token: str):
        """Receive the MP3 posted by the browser upload page."""
        from quart import jsonify
        import bot.exp_stream_manager as _esm
        song = await db.get_exp_radio_song_by_token(token)
        if not song:
            return jsonify({"ok": False, "error": "Invalid token"}), 403
        if song.get("mp3_filename"):
            return jsonify({"ok": True, "already": True})

        # Reject uploads while the stream is live or within 60 min of
        # a scheduled start — Whisper + LLM compete with FFmpeg for CPU/RAM.
        from bot.exp_stream_manager import is_submissions_locked as _is_locked
        _locked, _lock_reason = await _is_locked(db)
        if _locked:
            return jsonify({
                "ok": False,
                "error": "Submissions are currently closed (stream live or starting soon). Please try again later.",
            }), 503

        files = await request.files
        mp3_file = files.get("mp3")
        if not mp3_file:
            return jsonify({"ok": False, "error": "No file received"}), 400

        mp3_dir = os.path.join(EXP_RADIO_DIR, "mp3")
        os.makedirs(mp3_dir, exist_ok=True)
        mp3_filename = f"{song['suno_uuid']}.mp3"
        mp3_path     = os.path.join(mp3_dir, mp3_filename)

        # Detect upload extension. Suno is migrating some songs from .mp3 to
        # .m4a (Opus codec). The browser tries .mp3 first and falls back to
        # .m4a, sending the filename with the right extension.
        upload_name = (mp3_file.filename or "").lower()
        is_m4a = upload_name.endswith(".m4a")
        if is_m4a:
            # Save the m4a temporarily, then transcode to mp3 so the rest of
            # the pipeline (FFmpeg concat demuxer, Whisper, cover normalize)
            # keeps working unchanged.
            tmp_path = os.path.join(mp3_dir, f"{song['suno_uuid']}.m4a")
            await mp3_file.save(tmp_path)
            try:
                _proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-y", "-i", tmp_path,
                    "-vn", "-acodec", "libmp3lame", "-b:a", "192k",
                    mp3_path,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, _err = await _proc.communicate()
                if _proc.returncode != 0:
                    try: os.remove(tmp_path)
                    except OSError: pass
                    return jsonify({
                        "ok": False,
                        "error": f"M4A→MP3 transcode failed: {(_err or b'').decode('utf-8', 'replace')[:200]}",
                    }), 500
            finally:
                try: os.remove(tmp_path)
                except OSError: pass
        else:
            await mp3_file.save(mp3_path)

        await db.update_exp_radio_song(song["id"], mp3_filename=mp3_filename)

        from bot.exp_radio_worker import process_exp_song
        asyncio.create_task(
            process_exp_song(db, song["id"], EXP_RADIO_DIR, bot=bot)
        )
        return jsonify({"ok": True})

    @app.route("/exp-radio/stream/<action>", methods=["POST"])
    @permission_required('exp_radio')
    async def exp_radio_stream_action(action):
        from quart import jsonify
        if action in ("start", "start_legacy"):
            twitch_key = await db.get_setting("exp_radio_twitch_key") or ""
            if not twitch_key:
                return jsonify({"ok": False, "error": "No Twitch stream key configured."}), 400
            result = await exp_stream_manager.start(twitch_key, legacy_pipeline=(action == "start_legacy"))
        elif action == "stop":
            result = await exp_stream_manager.stop()
        elif action == "safe_stop":
            result = await exp_stream_manager.safe_stop()
        else:
            return jsonify({"ok": False, "error": "Unknown action"}), 400
        return jsonify(result)

    @app.route("/exp-radio/stream/status")
    @permission_required('exp_radio')
    async def exp_radio_stream_status():
        from quart import jsonify
        return jsonify(await exp_stream_manager.get_status())

    @app.route("/exp-radio/cover-preview/<int:song_id>")
    @permission_required('exp_radio')
    async def exp_radio_cover_preview(song_id):
        """Serve the locally cached cover MP4 for admin preview.

        Triggers an on-demand download (and normalization) via the stream
        manager if the file hasn't been cached yet, so an admin can verify
        what will actually be streamed without starting the full stream.
        """
        from quart import send_file, abort
        s = await db.get_exp_radio_song(song_id)
        if not s:
            return abort(404)
        uuid = s.get("suno_uuid") or ""
        if not uuid:
            return abort(404)
        path = os.path.join(EXP_RADIO_DIR, "cover_cache", f"{uuid}.mp4")
        if not os.path.exists(path):
            # Lazily download + normalize through the stream manager so the
            # preview matches what the stream pipeline will actually use.
            if not s.get("video_url"):
                return abort(404)
            await exp_stream_manager._get_video(s)
            if not os.path.exists(path):
                return abort(404)
        return await send_file(path, mimetype="video/mp4")

    @app.route("/exp-radio/stream/log")
    @permission_required('exp_radio')
    async def exp_radio_stream_log():
        """Return live-log entries for the admin UI panel.

        Query params:
          since   – unix timestamp (float). If given, only return entries
                    strictly newer than this. Used for incremental polling.
          window  – fallback time window in seconds (default 300 = 5 min).
        """
        from quart import jsonify, request
        try:
            since = float(request.args.get("since", "0") or 0)
        except ValueError:
            since = 0.0
        try:
            window = float(request.args.get("window", "300") or 300)
        except ValueError:
            window = 300.0
        return jsonify({
            "running": exp_stream_manager.is_running,
            "entries": exp_stream_manager.get_log(since_ts=since, max_age_secs=window),
        })

    # ── Twitch Bot OAuth re-authorization ──────────────────────────────────────
    _TWITCH_BOT_SCOPES = "user:bot user:write:chat chat:read"
    _TWITCH_OAUTH_STATE_KEY = "twitch_oauth_state"

    @app.route("/exp-radio/twitch-exchange-code", methods=["POST"])
    @permission_required('exp_radio')
    async def exp_radio_twitch_exchange_code():
        """Exchange an authorization code (obtained via manual OAuth flow with
        redirect_uri=https://localhost) for a refresh token and save it."""
        import aiohttp as _aio
        form = await request.form
        code = (form.get("twitch_code") or "").strip()
        if not code:
            await flash("No code provided.", "error")
            return redirect(url_for("exp_radio_admin"))
        client_id     = await db.get_setting("exp_radio_twitch_client_id") or ""
        client_secret = await db.get_setting("exp_radio_twitch_client_secret") or ""
        if not (client_id and client_secret):
            await flash("Client ID / Secret not configured.", "error")
            return redirect(url_for("exp_radio_admin"))
        try:
            async with _aio.ClientSession() as _s:
                async with _s.post(
                    "https://id.twitch.tv/oauth2/token",
                    data={
                        "client_id":     client_id,
                        "client_secret": client_secret,
                        "code":          code,
                        "grant_type":    "authorization_code",
                        "redirect_uri":  "https://localhost",
                    },
                    timeout=_aio.ClientTimeout(total=15),
                ) as _r:
                    _d = await _r.json()
                    if _r.status != 200 or "refresh_token" not in _d:
                        await flash(f"Token exchange failed: {_d.get('message', _d)}", "error")
                        return redirect(url_for("exp_radio_admin"))
                    access_token  = _d["access_token"]
                    refresh_token = _d["refresh_token"]
                async with _s.get(
                    "https://id.twitch.tv/oauth2/validate",
                    headers={"Authorization": f"OAuth {access_token}"},
                    timeout=_aio.ClientTimeout(total=10),
                ) as _r:
                    _v = await _r.json()
                    bot_login  = _v.get("login", "")
                    new_scopes = _v.get("scopes", [])
            await db.set_setting("exp_radio_twitch_refresh_token", refresh_token)
            await db.set_setting("exp_radio_twitch_bot_login", bot_login)
            await db.set_setting("exp_radio_twitch_bot_user_id", "")
            scope_ok = "chat:read" in new_scopes
            await flash(
                f"✅ Token saved for {bot_login} | scopes: {new_scopes}"
                + (" — chat:read ✓ Relic Hunt ready!" if scope_ok else " — ⚠️ chat:read still missing"),
                "success" if scope_ok else "error",
            )
            if scope_ok:
                await relic_hunt.stop()
                asyncio.create_task(_relic_hunt_autostart())
        except Exception as _e:
            await flash(f"Code exchange error: {_e}", "error")
        return redirect(url_for("exp_radio_admin"))

    @app.route("/exp-radio/twitch-oauth-start")
    @permission_required('exp_radio')
    async def exp_radio_twitch_oauth_start():
        import secrets as _sec
        client_id = await db.get_setting("exp_radio_twitch_client_id")
        if not client_id:
            await flash("Client ID not configured — save it first.", "error")
            return redirect(url_for("exp_radio_admin"))
        state = _sec.token_urlsafe(16)
        session[_TWITCH_OAUTH_STATE_KEY] = state
        redirect_uri = request.url_root.rstrip("/") + url_for("exp_radio_twitch_oauth_callback")
        params = (
            f"client_id={client_id}"
            f"&redirect_uri={redirect_uri}"
            f"&response_type=code"
            f"&scope={_TWITCH_BOT_SCOPES.replace(' ', '+')}"
            f"&state={state}"
        )
        return redirect(f"https://id.twitch.tv/oauth2/authorize?{params}")

    @app.route("/exp-radio/twitch-oauth-callback")
    @permission_required('exp_radio')
    async def exp_radio_twitch_oauth_callback():
        import aiohttp as _aio
        code      = request.args.get("code", "")
        state_got = request.args.get("state", "")
        error     = request.args.get("error_description") or request.args.get("error", "")
        if error:
            await flash(f"Twitch authorization denied: {error}", "error")
            return redirect(url_for("exp_radio_admin"))
        state_exp = session.pop(_TWITCH_OAUTH_STATE_KEY, None)
        if not state_exp or state_got != state_exp:
            await flash("OAuth state mismatch — possible CSRF. Try again.", "error")
            return redirect(url_for("exp_radio_admin"))
        client_id     = await db.get_setting("exp_radio_twitch_client_id") or ""
        client_secret = await db.get_setting("exp_radio_twitch_client_secret") or ""
        redirect_uri  = request.url_root.rstrip("/") + url_for("exp_radio_twitch_oauth_callback")
        try:
            async with _aio.ClientSession() as _s:
                async with _s.post(
                    "https://id.twitch.tv/oauth2/token",
                    data={
                        "client_id":     client_id,
                        "client_secret": client_secret,
                        "code":          code,
                        "grant_type":    "authorization_code",
                        "redirect_uri":  redirect_uri,
                    },
                    timeout=_aio.ClientTimeout(total=15),
                ) as _r:
                    _d = await _r.json()
                    if _r.status != 200 or "refresh_token" not in _d:
                        await flash(f"Token exchange failed: {_d.get('message', _d)}", "error")
                        return redirect(url_for("exp_radio_admin"))
                    access_token  = _d["access_token"]
                    refresh_token = _d["refresh_token"]
                # Resolve bot login from the new token
                async with _aio.ClientSession() as _s:
                    async with _s.get(
                        "https://id.twitch.tv/oauth2/validate",
                        headers={"Authorization": f"OAuth {access_token}"},
                        timeout=_aio.ClientTimeout(total=10),
                    ) as _r:
                        _v = await _r.json()
                        bot_login = _v.get("login", "")
                        new_scopes = _v.get("scopes", [])
            await db.set_setting("exp_radio_twitch_refresh_token", refresh_token)
            await db.set_setting("exp_radio_twitch_bot_login", bot_login)
            await db.set_setting("exp_radio_twitch_bot_user_id", "")
            scope_ok = "chat:read" in new_scopes
            msg = (
                f"✅ Bot re-authorized as {bot_login} with scopes: {new_scopes}. "
                + ("chat:read present — Relic Hunt will work!" if scope_ok
                   else "⚠️ chat:read still missing — check the app's Twitch scopes.")
            )
            await flash(msg, "success" if scope_ok else "error")
            # Auto-restart the relic hunt listener with the new token
            if scope_ok:
                await relic_hunt.stop()
                asyncio.create_task(_relic_hunt_autostart())
        except Exception as _e:
            await flash(f"OAuth callback error: {_e}", "error")
        return redirect(url_for("exp_radio_admin"))

    @app.route("/exp-radio/consent-csv")
    @permission_required('exp_radio')
    async def exp_radio_consent_csv():
        import csv, io
        from datetime import datetime, timezone
        rows = await db.get_exp_radio_consent_csv_rows()
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "ID", "User ID", "Username", "Suno URL", "Title", "Artist",
            "Status", "Rights Hash", "Agreed At", "Submitted At", "Expires At", "Active",
        ])
        for r in rows:
            def _dt(ts):
                if not ts:
                    return ""
                return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            writer.writerow([
                r["id"], r["user_id"], r["user_name"], r["suno_url"],
                r["title"] or "", r["artist"] or "",
                r["analysis_status"], r["rights_hash"],
                _dt(r["rights_agreed_at"]), _dt(r["submitted_at"]), _dt(r["expires_at"]),
                "Yes" if r["active"] else "No",
            ])
        buf.seek(0)
        from quart import Response
        return Response(
            buf.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=exp_radio_consent.csv"},
        )

    # --- Raven's Nest: Relic Hunt ---
    @app.route("/relic-hunt", methods=["GET", "POST"])
    @permission_required('relic_hunt')
    async def relic_hunt_admin():
        import json as _json
        from quart import jsonify

        if request.method == "POST":
            form = await request.form
            action = form.get("action", "")

            if action == "save_settings":
                _bool_keys = {"announce_level_ups", "announce_rank_ups",
                              "mods_bypass_cooldowns", "auto_event_enabled"}
                _interval_keys = {"auto_event_min_interval_minutes",
                                  "auto_event_max_interval_minutes"}
                for key in (
                    "enabled", "command_prefix", "timezone",
                    "raven_cooldown_seconds", "ritual_cooldown_seconds",
                    "leaderboard_size", "announce_level_ups", "announce_rank_ups",
                    "mods_bypass_cooldowns", "subscriber_cooldown_multiplier",
                    "vip_cooldown_multiplier", "ritual_reward_points",
                    "ritual_reward_xp", "ritual_legendary_chance",
                    "ritual_active_window_minutes",
                    "access_mode",
                    "auto_event_enabled",
                    "auto_event_min_interval_minutes",
                    "auto_event_max_interval_minutes",
                ):
                    if key in _bool_keys:
                        val = "true" if form.get(key) else "false"
                    elif key == "access_mode":
                        val = form.get(key, "everyone")
                        if val not in ("everyone", "subscribers"):
                            val = "everyone"
                    else:
                        val = form.get(key, "")
                    await db.relic_set_setting(key, val)
                # Reset the next-event timer whenever interval settings change
                # so the new values take effect immediately.
                if _interval_keys & set(form.keys()):
                    await db.relic_set_setting("auto_event_next_at", "0")
                await flash("Settings saved.", "success")

            elif action == "toggle_game":
                enabled = (await db.relic_get_setting("enabled")) != "false"
                await db.relic_set_setting("enabled", "false" if enabled else "true")
                await flash(f"Game {'disabled' if enabled else 'enabled'}.", "success")

            elif action == "start_listener":
                await db.ensure_relic_tables()
                if relic_hunt._running:
                    await relic_hunt.stop()
                client_id   = await db.get_setting("exp_radio_twitch_client_id")
                refresh_tok = await db.get_setting("exp_radio_twitch_refresh_token")
                broadcaster = await db.get_setting("exp_radio_twitch_broadcaster_login")
                if not (client_id and refresh_tok and broadcaster):
                    await flash("Twitch credentials not configured. Set them in Exp. Radio \u2192 Settings.", "error")
                else:
                    bot = _TwitchBot(db, key_prefix="exp_radio_twitch")
                    ok, msg = await bot.start()
                    if ok:
                        await relic_hunt.start(bot)
                        await flash("Relic Hunt listener started.", "success")
                    else:
                        await flash(f"Twitch bot error: {msg}", "error")

            elif action == "stop_listener":
                await relic_hunt.stop()
                await flash("Relic Hunt listener stopped.", "success")

            elif action == "upsert_item":
                item_id = form.get("item_id", "").strip().replace(" ", "_").lower()
                if not item_id:
                    await flash("Item ID is required.", "error")
                else:
                    item = {
                        "id": item_id,
                        "name": form.get("name", ""),
                        "rarity": form.get("rarity", "common"),
                        "enabled": 1 if form.get("enabled") else 0,
                        "drop_weight": float(form.get("drop_weight") or 1),
                        "min_points": int(form.get("min_points") or 0),
                        "max_points": int(form.get("max_points") or 0),
                        "min_xp": int(form.get("min_xp") or 0),
                        "max_xp": int(form.get("max_xp") or 0),
                        "flavor_text": form.get("flavor_text", ""),
                        "announce_globally": 1 if form.get("announce_globally") else 0,
                        "can_be_used_in_ritual": 1 if form.get("can_be_used_in_ritual") else 0,
                        "ritual_energy": int(form.get("ritual_energy") or 0),
                        "icon": form.get("icon", ""),
                        "category": form.get("category", ""),
                        "seasonal_tag": form.get("seasonal_tag") or None,
                        "required_event": form.get("required_event") or None,
                    }
                    await db.relic_upsert_item(item)
                    await flash(f"Item '{item['name']}' saved.", "success")

            elif action == "delete_item":
                item_id = form.get("item_id", "")
                await db.relic_delete_item(item_id)
                await flash("Item deleted.", "success")

            elif action == "toggle_item":
                item_id = form.get("item_id", "")
                item = await db.relic_get_item(item_id)
                if item:
                    item["enabled"] = 0 if item["enabled"] else 1
                    await db.relic_upsert_item(item)

            elif action == "import_items":
                raw = form.get("items_json", "")
                try:
                    items = _json.loads(raw)
                    if not isinstance(items, list):
                        raise ValueError("Expected a JSON array")
                    count = 0
                    for it in items:
                        it.setdefault("enabled", True)
                        it.setdefault("announce_globally", it.get("rarity") in ("rare","epic","legendary","mythic"))
                        it["enabled"] = 1 if it["enabled"] else 0
                        it["announce_globally"] = 1 if it["announce_globally"] else 0
                        it["can_be_used_in_ritual"] = 1 if it.get("can_be_used_in_ritual") else 0
                        await db.relic_upsert_item(it)
                        count += 1
                    await flash(f"Imported {count} item(s).", "success")
                except Exception as e:
                    await flash(f"Import failed: {e}", "error")

            elif action == "reset_items":
                from bot.relic_hunt import DEFAULT_ITEMS
                import time as _time
                for item in DEFAULT_ITEMS:
                    row = {
                        "id": item["id"], "name": item["name"],
                        "rarity": item.get("rarity","common"), "enabled": 1,
                        "drop_weight": item.get("drop_weight",1),
                        "min_points": item.get("min_points",0), "max_points": item.get("max_points",0),
                        "min_xp": item.get("min_xp",0), "max_xp": item.get("max_xp",0),
                        "flavor_text": item.get("flavor_text",""),
                        "announce_globally": 1 if item.get("announce_globally") else 0,
                        "can_be_used_in_ritual": 1 if item.get("can_be_used_in_ritual") else 0,
                        "ritual_energy": item.get("ritual_energy",0),
                        "icon": item.get("icon",""), "category": item.get("category",""),
                        "seasonal_tag": None, "required_event": None,
                    }
                    await db.relic_upsert_item(row)
                await flash(f"Default item library restored ({len(DEFAULT_ITEMS)} items).", "success")

            elif action == "upsert_rank":
                rank_id = form.get("rank_id", "").strip().replace(" ", "_").lower()
                if not rank_id:
                    await flash("Rank ID is required.", "error")
                elif not form.get("name", "").strip():
                    await flash("Rank name is required.", "error")
                else:
                    await db.relic_upsert_rank({
                        "id": rank_id,
                        "name": form.get("name", "").strip(),
                        "icon": form.get("icon", "").strip(),
                        "min_points": max(0, int(form.get("min_points") or 0)),
                        "enabled": 1 if form.get("enabled") else 0,
                    })
                    await flash("Rank saved.", "success")

            elif action == "toggle_rank":
                rank_id = form.get("rank_id", "")
                rank = await db.relic_get_rank(rank_id)
                if rank:
                    rank["enabled"] = 0 if rank["enabled"] else 1
                    await db.relic_upsert_rank(rank)

            elif action == "delete_rank":
                await db.relic_delete_rank(form.get("rank_id", ""))
                await flash("Rank deleted.", "success")

            elif action == "reset_ranks":
                from bot.relic_hunt import DEFAULT_RANKS
                for rank in DEFAULT_RANKS:
                    await db.relic_upsert_rank({**rank, "enabled": 1})
                await flash(f"Default ranks restored ({len(DEFAULT_RANKS)} ranks).", "success")

            elif action == "upsert_combine_recipe":
                recipe_id = form.get("recipe_id", "").strip().replace(" ", "_").lower()
                ingredient_a_id = form.get("ingredient_a_id", "")
                ingredient_b_id = form.get("ingredient_b_id", "")
                result_item_id = form.get("result_item_id", "")
                item_ids = {item["id"] for item in await db.relic_get_all_items()}
                if not recipe_id:
                    await flash("Recipe ID is required.", "error")
                elif not all((ingredient_a_id, ingredient_b_id, result_item_id)):
                    await flash("Both ingredients and a result item are required.", "error")
                elif not {ingredient_a_id, ingredient_b_id, result_item_id}.issubset(item_ids):
                    await flash("Every recipe item must exist in the item library.", "error")
                else:
                    await db.relic_upsert_combine_recipe({
                        "id": recipe_id,
                        "ingredient_a_id": ingredient_a_id,
                        "ingredient_b_id": ingredient_b_id,
                        "result_item_id": result_item_id,
                        "bonus_points": max(0, int(form.get("bonus_points") or 0)),
                        "priority": max(0, int(form.get("priority") or 100)),
                        "enabled": 1 if form.get("enabled") else 0,
                    })
                    await flash("Combine recipe saved.", "success")

            elif action == "toggle_combine_recipe":
                recipe = await db.relic_get_combine_recipe(form.get("recipe_id", ""))
                if recipe:
                    recipe["enabled"] = 0 if recipe["enabled"] else 1
                    await db.relic_upsert_combine_recipe(recipe)

            elif action == "delete_combine_recipe":
                await db.relic_delete_combine_recipe(form.get("recipe_id", ""))
                await flash("Combine recipe deleted.", "success")

            elif action == "reset_combine_recipes":
                from bot.relic_hunt import DEFAULT_COMBINE_RECIPES
                item_ids = {item["id"] for item in await db.relic_get_all_items()}
                restored = 0
                for recipe in DEFAULT_COMBINE_RECIPES:
                    recipe_items = {
                        recipe["ingredient_a_id"],
                        recipe["ingredient_b_id"],
                        recipe["result_item_id"],
                    }
                    if recipe_items.issubset(item_ids):
                        await db.relic_upsert_combine_recipe({**recipe, "enabled": 1})
                        restored += 1
                await flash(
                    f"Default combine recipes restored ({restored} recipes).",
                    "success",
                )

            elif action == "upsert_custom_command":
                command = (form.get("command") or "").strip().lstrip("!").lower()
                response = (form.get("response") or "").strip()
                reserved_commands = {
                    "raven", "nest", "items", "top", "rank", "daily",
                    "ritual", "combine", "phrase", "solve", "relichelp", "relic",
                }
                if not re.fullmatch(r"[a-z0-9_][a-z0-9_-]{0,31}", command):
                    await flash("Command must use 1-32 letters, numbers, underscores or hyphens.", "error")
                elif command in reserved_commands:
                    await flash(f"'!{command}' is a built-in Raven's Nest command.", "error")
                elif not response:
                    await flash("Response text is required.", "error")
                else:
                    await db.relic_upsert_custom_command(
                        command,
                        response[:500],
                        enabled=bool(form.get("enabled")),
                    )
                    await flash(f"Command '!{command}' saved.", "success")

            elif action == "toggle_custom_command":
                await db.relic_toggle_custom_command(form.get("command", ""))

            elif action == "delete_custom_command":
                command = (form.get("command") or "").strip().lstrip("!")
                await db.relic_delete_custom_command(command)
                await flash(f"Command '!{command}' deleted.", "success")

            elif action == "save_phrase_puzzle":
                enabled = bool(form.get("phrase_enabled"))
                loop_queue = bool(form.get("phrase_loop_queue"))
                try:
                    chance_percent = float(
                        form.get("letter_find_chance_percent") or 5
                    )
                    reward_xp = max(
                        0, int(form.get("winner_xp_reward") or 500)
                    )
                except ValueError:
                    await flash("Chance and XP reward must be valid numbers.", "error")
                else:
                    await db.relic_save_phrase_puzzle(
                        enabled=enabled,
                        loop_queue=loop_queue,
                        letter_find_chance=min(
                            100.0, max(0.0, chance_percent)
                        ) / 100.0,
                        winner_xp_reward=reward_xp,
                    )
                    await flash("Phrase puzzle settings saved.", "success")

            elif action == "add_phrase_to_queue":
                phrase = form.get("phrase", "").strip()
                if not any(char.isalpha() for char in phrase):
                    await flash(
                        "A queued phrase must contain at least one letter.",
                        "error",
                    )
                else:
                    await db.relic_add_phrase_to_queue(phrase)
                    await flash("Phrase added to the queue.", "success")

            elif action == "generate_phrase_suggestion":
                import bot.exp_stream_manager as _esm
                if exp_stream_manager.is_running or _esm.stream_is_live:
                    await flash("Phrase generation is disabled while the stream is running.", "error")
                elif session.get("relic_phrase_generation_running"):
                    await flash("Phrase generation is already running. Please wait for it to finish.", "error")
                else:
                    session["relic_phrase_generation_running"] = True
                    try:
                        from bot.llm import OllamaClient
                        from config import Config

                        phrase_puzzle = await db.relic_get_phrase_puzzle()
                        phrase_queue = await db.relic_get_phrase_queue()
                        existing = []
                        if phrase_puzzle.get("phrase"):
                            existing.append(phrase_puzzle["phrase"])
                        for queued in phrase_queue:
                            phrase = (queued.get("phrase") or "").strip()
                            if phrase and phrase not in existing:
                                existing.append(phrase)
                        examples = "\n".join(
                            f"- {phrase}" for phrase in existing[:40]
                        ) or "- The raven waits in the moonlit nest"
                        prompt = (
                            "Generate exactly 10 new phrases for a Twitch chat "
                            "word/phrase puzzle in a game called Raven's Nest: Relic Hunt.\n"
                            "Match the vibe of these existing phrases: dark fantasy, ravens, relics, rituals, "
                            "mist, moonlight, playful mystery. The phrase should be memorable and solvable.\n\n"
                            f"Existing phrases:\n{examples}\n\n"
                            "Rules:\n"
                            "- Output exactly 10 lines, one phrase per line.\n"
                            "- No quotes, no numbering, no explanation.\n"
                            "- Each phrase must be 4 to 10 words.\n"
                            "- Do not use apostrophes or contractions. Write phrases without possessive forms like raven's or relic's.\n"
                            "- Separate words with normal spaces only. Do not use hyphens or dashes between words.\n"
                            "- Use only letters, numbers, spaces and simple commas if needed.\n"
                            "- Avoid duplicating any existing phrase.\n"
                            "- English only.\n"
                        )
                        client = OllamaClient(
                            base_url=Config.OLLAMA_URL,
                            model=Config.LLM_MODEL,
                            timeout=Config.LLM_REQUEST_TIMEOUT,
                        )
                        data = await client.chat(
                            [
                                {
                                    "role": "system",
                                    "content": "You create concise phrase puzzle answers. Output only the requested phrases, one per line.",
                                },
                                {"role": "user", "content": prompt},
                            ],
                            max_tokens=220,
                            temperature=0.85,
                            top_p=0.9,
                        )
                        content = (
                            ((data.get("message") or {}).get("content") or "")
                            .strip()
                        )
                        suggestions = []
                        seen = {phrase.casefold() for phrase in existing}
                        for line in content.splitlines():
                            suggestion = line.strip().strip('"“”').strip()
                            suggestion = re.sub(r"^[\-\d\.\)\s]+", "", suggestion).strip()
                            suggestion = re.sub(r"[-‐‑‒–—―]+", " ", suggestion)
                            suggestion = re.sub(r"\s+", " ", suggestion)
                            if not any(char.isalpha() for char in suggestion):
                                continue
                            if "'" in suggestion or "’" in suggestion:
                                continue
                            key = suggestion.casefold()
                            if key in seen:
                                continue
                            seen.add(key)
                            suggestions.append(suggestion[:160])
                            if len(suggestions) >= 10:
                                break
                        if not suggestions:
                            await flash("LLM did not return usable phrases.", "error")
                        else:
                            session["relic_phrase_suggestions"] = suggestions
                            session["relic_phrase_suggestion"] = suggestions[0]
                            await flash(f"Generated {len(suggestions)} phrase suggestions. Pick one and review it before adding.", "success")
                    except Exception as e:
                        await flash(f"Phrase generation failed: {e}", "error")
                    finally:
                        session.pop("relic_phrase_generation_running", None)

            elif action == "confirm_phrase_suggestion":
                phrase = (
                    (form.get("suggested_phrase") or "")
                    or (form.get("suggested_phrase_fallback") or "")
                    or (session.get("relic_phrase_suggestion") or "")
                ).strip()
                phrase = re.sub(r"[-‐‑‒–—―]+", " ", phrase)
                phrase = re.sub(r"\s+", " ", phrase).strip()
                if not any(char.isalpha() for char in phrase):
                    await flash(
                        "A suggested phrase must contain at least one letter.",
                        "error",
                    )
                else:
                    suggestions = session.get("relic_phrase_suggestions", [])
                    if not isinstance(suggestions, list):
                        suggestions = []
                    phrases_to_add = []
                    seen = set()
                    for candidate in [phrase, *suggestions]:
                        candidate = (candidate or "").strip()
                        candidate = re.sub(r"[-‐‑‒–—―]+", " ", candidate)
                        candidate = re.sub(r"\s+", " ", candidate).strip()
                        if not any(char.isalpha() for char in candidate):
                            continue
                        key = candidate.casefold()
                        if key in seen:
                            continue
                        seen.add(key)
                        phrases_to_add.append(candidate)
                    added = 0
                    for candidate in phrases_to_add:
                        await db.relic_add_phrase_to_queue(candidate)
                        added += 1
                    session.pop("relic_phrase_suggestion", None)
                    session.pop("relic_phrase_suggestions", None)
                    await flash(f"Added {added} suggested phrase(s) to the queue.", "success")

            elif action == "clear_phrase_suggestion":
                session.pop("relic_phrase_suggestion", None)
                session.pop("relic_phrase_suggestions", None)
                await flash("Phrase suggestion discarded.", "success")

            elif action == "start_phrase":
                phrase_id = int(form.get("phrase_id") or 0)
                if await db.relic_activate_phrase(phrase_id):
                    await flash("Phrase started.", "success")
                else:
                    await flash("Phrase not found.", "error")

            elif action == "delete_phrase":
                phrase_id = int(form.get("phrase_id") or 0)
                await db.relic_delete_phrase_from_queue(phrase_id)
                await flash("Phrase removed from the queue.", "success")

            elif action == "skip_phrase":
                next_phrase = await db.relic_activate_next_phrase()
                if next_phrase:
                    await flash("Skipped to the next queued phrase.", "success")
                else:
                    await flash(
                        "No queued phrase remains. Phrase Puzzle disabled.",
                        "success",
                    )

            elif action == "reset_phrase_progress":
                await db.relic_reset_phrase_progress()
                await flash("Phrase puzzle progress reset.", "success")

            elif action == "edit_user_points":
                uid = form.get("twitch_user_id", "")
                user = await db.relic_get_user(uid)
                if user:
                    user["points"] = max(0, int(form.get("points") or 0))
                    await db.relic_upsert_user(user)
                    await flash(f"Updated points for {user['username']}.", "success")

            elif action == "reset_user":
                uid = form.get("twitch_user_id", "")
                await db.relic_delete_user(uid)
                await flash("User reset.", "success")

            elif action == "give_item":
                uid = form.get("twitch_user_id", "")
                item_id = form.get("item_id", "")
                amount = int(form.get("amount") or 1)
                for _ in range(amount):
                    await db.relic_add_item_to_user(uid, item_id)
                await flash(f"Gave {amount}x {item_id} to user.", "success")

            elif action == "upsert_event":
                eid = form.get("event_id", "").strip().replace(" ","_").lower()
                if not eid:
                    await flash("Event ID required.", "error")
                else:
                    cfg = {
                        "durationMinutes": int(form.get("duration_minutes") or 10),
                        "rareDropMultiplier": float(form.get("rare_drop_multiplier") or 1.0),
                        "epicDropMultiplier": float(form.get("epic_drop_multiplier") or 1.0),
                        "pointsMultiplier": float(form.get("points_multiplier") or 1.0),
                        "xpMultiplier": float(form.get("xp_multiplier") or 1.0),
                        "ritualEnergyMultiplier": float(form.get("ritual_energy_multiplier") or 1.0),
                        "startMessage": form.get("start_message",""),
                        "endMessage": form.get("end_message",""),
                    }
                    await db.relic_upsert_event({
                        "id": eid, "name": form.get("event_name",eid),
                        "enabled": 1 if form.get("event_enabled") else 0,
                        "config_json": _json.dumps(cfg),
                    })
                    await flash(f"Event '{eid}' saved.", "success")

            elif action == "delete_event":
                await db.relic_delete_event(form.get("event_id",""))
                await flash("Event deleted.", "success")

            elif action == "start_event":
                eid = form.get("event_id","")
                events = {e["id"]: e for e in await db.relic_get_all_events()}
                if eid in events:
                    cfg = _json.loads(events[eid]["config_json"])
                    dur = int(form.get("duration_minutes") or cfg.get("durationMinutes",10))
                    await db.relic_start_event(eid, dur * 60, "admin")
                    msg = cfg.get("startMessage", f"Event {eid} started!")
                    if relic_hunt._bot:
                        await relic_hunt._bot.send(msg)
                    await flash(f"Event started: {msg}", "success")

            elif action == "stop_event":
                eid = form.get("event_id","")
                events = {e["id"]: e for e in await db.relic_get_all_events()}
                if eid in events:
                    cfg = _json.loads(events[eid]["config_json"])
                    await db.relic_stop_event(eid)
                    msg = cfg.get("endMessage", f"Event {eid} stopped.")
                    if relic_hunt._bot:
                        await relic_hunt._bot.send(msg)
                    await flash(f"Event stopped.", "success")

            elif action == "ritual_add_energy":
                ritual = await db.relic_get_ritual()
                add = int(form.get("add_energy") or 0)
                await db.relic_update_ritual(ritual["energy"] + add)
                await flash(f"Added {add} ritual energy.", "success")

            elif action == "ritual_reset":
                await db.relic_update_ritual(0)
                await flash("Ritual reset.", "success")

            elif action == "ritual_save_settings":
                for key in ("ritual_goal", "ritual_reward_points", "ritual_reward_xp",
                             "ritual_legendary_chance", "ritual_active_window_minutes",
                             "ritual_cooldown_seconds"):
                    await db.relic_set_setting(key.replace("ritual_","",1), form.get(key,""))
                goal = int(form.get("ritual_goal") or 500)
                ritual = await db.relic_get_ritual()
                await db.relic_update_ritual(ritual["energy"], goal)
                await flash("Ritual settings saved.", "success")

            return redirect(request.url)

        # GET
        await db.ensure_relic_tables()
        from bot.relic_hunt import DEFAULT_RANKS
        ranks = await db.relic_get_all_ranks()
        if not ranks:
            for rank in DEFAULT_RANKS:
                await db.relic_upsert_rank({**rank, "enabled": 1})
            ranks = await db.relic_get_all_ranks()
        items   = await db.relic_get_all_items()
        from bot.relic_hunt import (
            DEFAULT_COMBINE_RECIPES,
            DEFAULT_COMBINE_RECIPES_VERSION,
        )
        recipe_version = int(
            (await db.relic_get_setting("combine_recipes_version")) or 0
        )
        if recipe_version < DEFAULT_COMBINE_RECIPES_VERSION:
            item_ids = {item["id"] for item in items}
            for recipe in DEFAULT_COMBINE_RECIPES:
                recipe_items = {
                    recipe["ingredient_a_id"],
                    recipe["ingredient_b_id"],
                    recipe["result_item_id"],
                }
                if recipe_items.issubset(item_ids):
                    await db.relic_insert_combine_recipe_if_missing(
                        {**recipe, "enabled": 1}
                    )
            await db.relic_set_setting(
                "combine_recipes_version",
                str(DEFAULT_COMBINE_RECIPES_VERSION),
            )
        recipes = await db.relic_get_all_combine_recipes()
        custom_commands = await db.relic_get_all_custom_commands()
        users   = await db.relic_get_all_users()
        events  = await db.relic_get_all_events()
        active_events = await db.relic_get_active_events()
        active_event_ids = {ae["event_id"] for ae in active_events}
        ritual  = await db.relic_get_ritual()
        phrase_puzzle = await db.relic_get_phrase_puzzle()
        phrase_queue = await db.relic_get_phrase_queue()
        from bot.relic_hunt import _phrase_progress
        phrase_progress = _phrase_progress(
            phrase_puzzle.get("phrase", ""),
            phrase_puzzle.get("revealed_mask", ""),
        )
        phrase_total_letters = sum(
            char.isalpha() for char in phrase_puzzle.get("phrase", "")
        )
        phrase_found_letters = sum(
            1
            for index, char in enumerate(phrase_puzzle.get("phrase", ""))
            if char.isalpha()
            and index < len(phrase_puzzle.get("revealed_mask", ""))
            and phrase_puzzle["revealed_mask"][index] == "1"
        )
        log     = await db.relic_get_recent_log(30)
        game_enabled = (await db.relic_get_setting("enabled")) != "false"
        listener_running = relic_hunt._running
        import bot.exp_stream_manager as _esm
        exp_stream_running = bool(exp_stream_manager.is_running or _esm.stream_is_live)
        phrase_suggestion = session.get("relic_phrase_suggestion", "")
        phrase_suggestions = session.get("relic_phrase_suggestions", [])
        if not isinstance(phrase_suggestions, list):
            phrase_suggestions = []

        # Load settings
        import json as _jrh
        settings = {}
        for key in (
            "command_prefix", "timezone", "raven_cooldown_seconds",
            "ritual_cooldown_seconds", "leaderboard_size", "announce_level_ups",
            "announce_rank_ups", "mods_bypass_cooldowns",
            "subscriber_cooldown_multiplier", "vip_cooldown_multiplier",
            "ritual_reward_points", "ritual_reward_xp", "ritual_legendary_chance",
            "ritual_active_window_minutes",
            "access_mode",
            "auto_event_enabled",
            "auto_event_min_interval_minutes",
            "auto_event_max_interval_minutes",
        ):
            settings[key] = await db.relic_get_setting(key) or ""

        # Enrich events with parsed config
        events_parsed = []
        for ev in events:
            try:
                cfg = _jrh.loads(ev["config_json"])
            except Exception:
                cfg = {}
            events_parsed.append({**ev, "cfg": cfg, "is_active": ev["id"] in active_event_ids})

        # Stats
        total_hunts = sum(u.get("commands_used", 0) for u in users)
        total_legendary = sum(u.get("legendary_finds", 0) for u in users)
        total_mythic = sum(u.get("mythic_finds", 0) for u in users)

        return await render_template(
            "relic_hunt.html",
            items=items, users=users[:50],
            events=events_parsed, ritual=ritual,
            log=log, game_enabled=game_enabled,
            listener_running=listener_running,
            exp_stream_running=exp_stream_running,
            settings=settings,
            total_hunts=total_hunts,
            total_legendary=total_legendary,
            total_mythic=total_mythic,
            ranks=ranks,
            recipes=recipes,
            custom_commands=custom_commands,
            phrase_puzzle=phrase_puzzle,
            phrase_queue=phrase_queue,
            phrase_suggestion=phrase_suggestion,
            phrase_suggestions=phrase_suggestions,
            phrase_progress=phrase_progress,
            phrase_found_letters=phrase_found_letters,
            phrase_total_letters=phrase_total_letters,
            active_event_ids=active_event_ids,
        )

    @app.route("/relic-hunt/export-items")
    @permission_required('relic_hunt')
    async def relic_hunt_export_items():
        import json as _json
        from quart import Response
        items = await db.relic_get_all_items()
        return Response(
            _json.dumps(items, indent=2),
            mimetype="application/json",
            headers={"Content-Disposition": "attachment; filename=relic_items.json"},
        )

    # --- Suno Audio Analyzer ---
    @app.route("/suno-analyzer")
    @permission_required('suno_analyzer')
    async def suno_analyzer():
        return await render_template("suno_analyzer.html")

    @app.route("/suno-analyzer/resolve")
    @permission_required('suno_analyzer')
    async def suno_analyzer_resolve():
        """Server-side proxy to resolve Suno share links and fetch metadata (avoids CORS)."""
        from quart import jsonify
        import aiohttp, re as _re
        song_id = request.args.get("id", "").strip()
        if not song_id:
            return jsonify({"error": "No id provided"}), 400
        # Determine URL
        is_uuid = bool(_re.match(r'^[a-f0-9]{8}-[a-f0-9]{4}', song_id))
        url = f"https://suno.com/song/{song_id}" if is_uuid else f"https://suno.com/s/{song_id}"
        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            async with aiohttp.ClientSession() as sess:
                async with sess.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return jsonify({"error": f"Suno returned {resp.status}"}), 502
                    html = await resp.text()
            result = {}
            # Extract real UUID
            m = _re.search(r'"id"\s*:\s*"([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})"', html)
            if not m:
                m = _re.search(r'song/([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})', html)
            result["realId"] = m.group(1) if m else None
            # Title
            m = _re.search(r'<title>([^<|]+)', html)
            result["title"] = m.group(1).strip() if m else ""
            # Artwork
            m = _re.search(r'cdn2\.suno\.ai/[^"]+\.jpeg', html)
            result["artwork"] = "https://" + m.group(0) if m else ""
            # Author — try title "by Author" first (most reliable), then display_name
            author = ""
            title_by = _re.search(r'\bby\s+(.+?)(?:\s*\||\s*-\s*Suno|$)', result.get("title", ""))
            if title_by:
                author = title_by.group(1).strip()
            if not author:
                m = _re.search(r'\\"display_name\\":\\"([^\\]+)\\"', html)
                if not m:
                    m = _re.search(r'"display_name":"([^"]+)"', html)
                if m:
                    author = m.group(1)
            result["author"] = author
            # Tags
            m = _re.search(r'\\"tags\\":\\"([^\\]+)\\"', html)
            if not m:
                m = _re.search(r'"tags":"([^"]+)"', html)
            result["tags"] = [t.strip() for t in m.group(1).split(",") if t.strip()] if m else []
            # Stats
            for key, pat in [("plays", r'\\"play_count\\":(\d+)'), ("likes", r'\\"upvote_count\\":(\d+)'), ("comments", r'\\"comment_count\\":(\d+)')]:
                m = _re.search(pat, html)
                result[key] = int(m.group(1)) if m else None
            # Created at
            m = _re.search(r'\\"created_at\\":\\"([^\\]+)\\"', html)
            result["created_at"] = m.group(1) if m else ""
            # Model
            m = _re.search(r'\\"major_model_version\\":\\"([^\\]+)\\"', html)
            result["model"] = m.group(1) if m else ""
            # Lyrics — extract from Next.js RSC payload (same method as stream_manager)
            lyrics_text = ""
            rsc_match = _re.search(
                r'self\.__next_f\.push\(\[1,"[0-9a-f]+:T[0-9a-f]+,"\]\)</script>'
                r'<script>self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>',
                html, _re.DOTALL
            )
            if rsc_match:
                raw = rsc_match.group(1)
                lyrics_text = raw.replace("\\n", "\n").replace("\\t", " ").replace('\\"', '"').strip()
                print(f"[suno-resolve] Lyrics from RSC payload, length={len(lyrics_text)}", flush=True)
            else:
                # Fallback: try _fetch_suno_meta helper
                real_id = result.get("realId") or song_id
                try:
                    meta = await _fetch_suno_meta(real_id)
                    if meta.get("lyrics"):
                        lyrics_text = meta["lyrics"]
                        print(f"[suno-resolve] Lyrics from embed helper, length={len(lyrics_text)}", flush=True)
                    if not result.get("author") and meta.get("artist"):
                        result["author"] = meta["artist"]
                except Exception as e:
                    print(f"[suno-resolve] Helper failed: {e}", flush=True)
            if not lyrics_text:
                print(f"[suno-resolve] No lyrics found for {song_id}", flush=True)
            result["lyrics"] = lyrics_text
            # GPT description prompt (style/genre prompt used for generation)
            m = _re.search(r'\\"gpt_description_prompt\\":\\"((?:[^\\]|\\.)*)(?:\\"|")', html)
            if not m:
                m = _re.search(r'"gpt_description_prompt":"((?:[^"\\]|\\.)*)"', html)
            result["prompt"] = m.group(1).replace('\\n', '\n').replace('\\"', '"') if m else ""
            # Type: cover, remix, or original
            m = _re.search(r'\\"type\\":\\"([^\\]+)\\"', html)
            if not m:
                m = _re.search(r'"type":"([^"]+)"', html)
            result["type"] = m.group(1) if m else ""
            return jsonify(result)
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # --- Party Playlist ---

    @app.route("/party-playlist", methods=["GET", "POST"])
    @permission_required('party_playlist')
    async def party_playlist():
        if request.method == "POST":
            form = await request.form
            action = form.get("action")

            if action == "submit_song":
                url = form.get("url", "").strip()
                if SUNO_URL_PATTERN.search(url):
                    song_title, artist, image_url = await _fetch_suno_info(url)
                elif YOUTUBE_URL_RE.search(url):
                    song_title, artist, image_url = await _fetch_youtube_info(url)
                elif ELEVENMUSIC_TRACK_RE.search(url):
                    track_id = ELEVENMUSIC_TRACK_RE.search(url).group(1).lower()
                    meta = await _fetch_elevenmusic_meta(track_id)
                    song_title = meta.get("title")
                    artist = meta.get("artist")
                    image_url = meta.get("image_url")
                else:
                    await flash("Invalid URL. Please provide a valid Suno, ElevenMusic or YouTube link.", "error")
                    return redirect(url_for("party_playlist"))
                await db.party_submit_song(
                    user_id=0,
                    user_name=artist or "Unknown Artist",
                    url=url,
                    song_title=song_title,
                    image_url=image_url,
                )
                return redirect(url_for("party_playlist"))

            elif action == "mark_heard":
                song_id = int(form.get("song_id", 0))
                await db.party_mark_heard(song_id)
                return redirect(request.referrer or url_for("party_playlist"))

            elif action == "mark_unheard":
                song_id = int(form.get("song_id", 0))
                await db.party_mark_unheard(song_id)
                return redirect(request.referrer or url_for("party_playlist"))

            elif action == "delete_song":
                song_id = int(form.get("song_id", 0))
                await db.db.execute("DELETE FROM party_playlist WHERE id = ?", (song_id,))
                await db.db.commit()
                return redirect(request.referrer or url_for("party_playlist"))

            elif action == "post_song":
                song_id = int(form.get("song_id", 0))
                channel_id_str = await db.get_setting("party_voice_channel")
                if not channel_id_str:
                    await flash("No post channel configured. Set it in Settings.", "error")
                    return redirect(url_for("party_playlist"))
                guild = get_guild()
                if not guild:
                    await flash("Bot is not connected to the guild.", "error")
                    return redirect(url_for("party_playlist"))
                channel = guild.get_channel(int(channel_id_str))
                if not channel:
                    await flash("Configured channel not found.", "error")
                    return redirect(url_for("party_playlist"))
                # Fetch song from DB
                songs = await db.party_get_all_songs()
                song = next((s for s in songs if s["id"] == song_id), None)
                if not song:
                    await flash("Song not found.", "error")
                    return redirect(url_for("party_playlist"))
                title = song.get("song_title") or "Unknown Title"
                artist = song.get("user_name") or "Unknown Artist"
                import discord
                embed = discord.Embed(
                    title=title,
                    url=song["url"],
                    description=f"by **{artist}**",
                    color=discord.Color.purple(),
                )
                if song.get("image_url"):
                    embed.set_thumbnail(url=song["image_url"])
                bot_name = await db.get_setting("bot_name") or "Slowmode Bot"
                embed.set_footer(text=f"{bot_name} — Listening Party")
                try:
                    await channel.send(embed=embed)
                    await flash(f"Posted \"{title}\" to #{channel.name}.", "success")
                except Exception as post_err:
                    await flash(f"Failed to post to #{channel.name}: {post_err}", "error")
                return redirect(url_for("party_playlist"))

            elif action == "save_playlist_url":
                playlist_url = form.get("playlist_url", "").strip()
                await db.set_setting("party_playlist_url", playlist_url)
                # Try to fetch playlist cover image
                playlist_image = ""
                if playlist_url:
                    try:
                        async with aiohttp.ClientSession() as sess:
                            async with sess.get(playlist_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                                if resp.status == 200:
                                    html = await resp.text()
                                    img_match = re.search(r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\'>]+)["\']', html)
                                    if not img_match:
                                        img_match = re.search(r'<meta\s+content=["\']([^"\'>]+)["\']\s+property=["\']og:image["\']', html)
                                    if img_match:
                                        playlist_image = img_match.group(1).strip()
                    except Exception:
                        pass
                await db.set_setting("party_playlist_image", playlist_image)
                if playlist_url:
                    await flash("Playlist URL saved.", "success")
                else:
                    await flash("Playlist URL cleared.", "success")
                return redirect(url_for("party_playlist"))

            elif action == "post_playlist_url":
                playlist_url = await db.get_setting("party_playlist_url")
                if not playlist_url:
                    await flash("No playlist URL set.", "error")
                    return redirect(url_for("party_playlist"))
                channel_id_str = await db.get_setting("party_voice_channel")
                if not channel_id_str:
                    await flash("No post channel configured. Set it in Settings.", "error")
                    return redirect(url_for("party_playlist"))
                guild = get_guild()
                if not guild:
                    await flash("Bot is not connected to the guild.", "error")
                    return redirect(url_for("party_playlist"))
                channel = guild.get_channel(int(channel_id_str))
                if not channel:
                    await flash("Configured channel not found.", "error")
                    return redirect(url_for("party_playlist"))
                import discord
                embed = discord.Embed(
                    title="\U0001F3B6 Listening Party Playlist",
                    url=playlist_url,
                    description=f"Check out the full playlist and give it a like!\n{playlist_url}",
                    color=discord.Color.purple(),
                )
                playlist_image = await db.get_setting("party_playlist_image")
                if playlist_image:
                    embed.set_thumbnail(url=playlist_image)
                bot_name = await db.get_setting("bot_name") or "Slowmode Bot"
                embed.set_footer(text=f"{bot_name} \u2014 Listening Party")
                try:
                    await channel.send(embed=embed)
                    await flash(f"Playlist posted to #{channel.name}.", "success")
                except Exception as post_err:
                    await flash(f"Failed to post to #{channel.name}: {post_err}", "error")
                return redirect(url_for("party_playlist"))

            elif action == "reset":
                await db.party_reset()
                await db.set_setting("party_playlist_url", "")
                await db.set_setting("party_playlist_image", "")
                return redirect(url_for("party_playlist"))

        # GET
        import traceback
        try:
            songs = await db.party_get_all_songs()
            party_playlist_url = await db.get_setting("party_playlist_url") or ""

            heard_count = sum(1 for s in songs if s["heard"])
            unheard_count = len(songs) - heard_count
            unheard_songs = [s for s in songs if not s["heard"]]

            filter_status = request.args.get("filter")
            if filter_status == "heard":
                display_songs = [s for s in songs if s["heard"]]
            elif filter_status == "unheard":
                display_songs = [s for s in songs if not s["heard"]]
            else:
                display_songs = songs
                filter_status = None

            return await render_template(
                "party_playlist.html",
                songs=songs,
                display_songs=display_songs,
                unheard_songs=unheard_songs,
                heard_count=heard_count,
                unheard_count=unheard_count,
                filter_status=filter_status,
                party_playlist_url=party_playlist_url,
            )
        except Exception as e:
            traceback.print_exc()
            return f"<pre>Error: {e}\n\n{traceback.format_exc()}</pre>", 500

    # --- Party Playlist JSON API (used by suno_info.html) ---

    @app.route("/api/party-playlist/songs")
    @permission_required('party_playlist')
    async def api_party_playlist_songs():
        from quart import jsonify
        songs = await db.party_get_all_songs()
        return jsonify([dict(s) for s in songs])

    @app.route("/api/party-playlist/heard/<int:song_id>", methods=["POST"])
    @permission_required('party_playlist')
    async def api_party_mark_heard(song_id):
        from quart import jsonify
        await db.party_mark_heard(song_id)
        return jsonify({"ok": True})

    @app.route("/api/party-playlist/unheard/<int:song_id>", methods=["POST"])
    @permission_required('party_playlist')
    async def api_party_mark_unheard(song_id):
        from quart import jsonify
        await db.party_mark_unheard(song_id)
        return jsonify({"ok": True})

    @app.route("/api/party-playlist/song/<int:song_id>", methods=["DELETE"])
    @permission_required('party_playlist')
    async def api_party_delete_song(song_id):
        from quart import jsonify
        await db.db.execute("DELETE FROM party_playlist WHERE id = ?", (song_id,))
        await db.db.commit()
        return jsonify({"ok": True})

    @app.route("/api/party-playlist/submit", methods=["POST"])
    @permission_required('party_playlist')
    async def api_party_submit_song():
        from quart import jsonify
        data = await request.get_json(silent=True) or {}
        url = (data.get("url") or "").strip()
        if not url:
            return jsonify({"ok": False, "error": "Please enter a URL"}), 400
        if SUNO_URL_PATTERN.search(url):
            song_title, artist, image_url = await _fetch_suno_info(url)
        elif YOUTUBE_URL_RE.search(url):
            song_title, artist, image_url = await _fetch_youtube_info(url)
        elif ELEVENMUSIC_TRACK_RE.search(url):
            track_id = ELEVENMUSIC_TRACK_RE.search(url).group(1).lower()
            meta = await _fetch_elevenmusic_meta(track_id)
            song_title = meta.get("title")
            artist = meta.get("artist")
            image_url = meta.get("image_url")
        else:
            return jsonify({"ok": False, "error": "Please enter a valid Suno, ElevenMusic or YouTube URL"}), 400
        await db.party_submit_song(
            user_id=0,
            user_name=artist or "Unknown Artist",
            url=url,
            song_title=song_title,
            image_url=image_url,
        )
        return jsonify({"ok": True, "song_title": song_title, "artist": artist})

    @app.route("/api/party-playlist/reset", methods=["POST"])
    @permission_required('party_playlist')
    async def api_party_reset():
        from quart import jsonify
        await db.party_reset()
        await db.set_setting("party_playlist_url", "")
        await db.set_setting("party_playlist_image", "")
        return jsonify({"ok": True})

    @app.route("/api/party-playlist/post/<int:song_id>", methods=["POST"])
    @permission_required('party_playlist')
    async def api_party_post_song(song_id):
        from quart import jsonify
        import discord as _discord
        songs = await db.party_get_all_songs()
        song = next((s for s in songs if s["id"] == song_id), None)
        if not song:
            return jsonify({"ok": False, "error": "Song not found"}), 404
        channel_id_str = await db.get_setting("party_voice_channel")
        if not channel_id_str:
            return jsonify({"ok": False, "error": "No post channel configured"}), 400
        guild = get_guild()
        if not guild:
            return jsonify({"ok": False, "error": "Bot not connected"}), 503
        channel = guild.get_channel(int(channel_id_str))
        if not channel:
            return jsonify({"ok": False, "error": "Channel not found"}), 404
        title = song.get("song_title") or "Unknown Title"
        artist = song.get("user_name") or "Unknown Artist"
        embed = _discord.Embed(
            title=title, url=song["url"],
            description=f"by **{artist}**",
            color=_discord.Color.purple(),
        )
        if song.get("image_url"):
            embed.set_thumbnail(url=song["image_url"])
        bot_name = await db.get_setting("bot_name") or "Slowmode Bot"
        embed.set_footer(text=f"{bot_name} — Listening Party")
        try:
            await channel.send(embed=embed)
            return jsonify({"ok": True, "channel": channel.name})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    # --- Suno Promotion (per logged-in web user) ---

    SUNO_PROFILE_PATTERN = re.compile(r'^https?://(?:www\.)?suno\.com/@([A-Za-z0-9_.\-]+)/?$')

    def _normalize_suno_profile_url(raw: str) -> tuple[str | None, str | None]:
        """Returns (canonical_url, handle_lower) or (None, None) if invalid."""
        if not raw:
            return None, None
        raw = raw.strip()
        # Allow bare handle input "@tarja_ravenveil" or "tarja_ravenveil"
        if raw.startswith("@"):
            raw = "https://suno.com/" + raw
        elif not raw.startswith("http") and re.fullmatch(r'[A-Za-z0-9_.\-]+', raw):
            raw = f"https://suno.com/@{raw}"
        m = SUNO_PROFILE_PATTERN.match(raw)
        if not m:
            return None, None
        handle = m.group(1)
        canonical = f"https://suno.com/@{handle}"
        return canonical, handle.lower()

    async def _fetch_suno_profile(profile_url: str) -> dict:
        """Fetch a Suno profile page and extract display name, avatar, pinned and latest song.

        Returns dict with keys:
          - display_name, avatar_url
          - pinned_song_url, pinned_song_title (first prominent song on profile)
          - latest_song_url, latest_song_title (most recently created song by date)
        Best-effort; missing fields are None.
        """
        import html as _html
        import json as _json
        out = {
            "display_name": None,
            "avatar_url": None,
            "pinned_song_url": None,
            "pinned_song_title": None,
            "latest_song_url": None,
            "latest_song_title": None,
        }
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

        # Fetch both pages concurrently
        async with aiohttp.ClientSession(headers=headers) as session:
            main_task = session.get(
                profile_url, timeout=aiohttp.ClientTimeout(total=15)
            )
            songs_page_url = f"{profile_url}?page=songs"
            songs_task = session.get(
                songs_page_url, timeout=aiohttp.ClientTimeout(total=15)
            )

            main_resp, songs_resp = await asyncio.gather(main_task, songs_task)

            main_html = await main_resp.text() if main_resp.status == 200 else ""
            songs_html = await songs_resp.text() if songs_resp.status == 200 else ""

            if not main_html:
                print(f"[suno_promotion] HTTP {main_resp.status} for {profile_url}")
                return out

        # Use main HTML for most things, but songs_html for song list
        html = main_html

        # --- Pinned song: first /song/<uuid> in the main profile HTML ---
        pinned_uuid = None
        m = re.search(r'/song/([a-f0-9-]{36})', html)
        if m:
            pinned_uuid = m.group(1)

        # og:title — typically "Display Name | Suno" or just "Display Name"
        m = re.search(
            r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']+)["\']', html
        )
        if not m:
            m = re.search(
                r'<meta\s+content=["\']([^"\']+)["\']\s+property=["\']og:title["\']', html
            )
        if m:
            raw = _html.unescape(m.group(1).strip())
            raw = re.sub(r'\s*[|\-–]\s*Suno\s*$', '', raw).strip()
            # Some pages format as "Display Name | Suno AI" or "Display Name's profile"
            raw = re.sub(r"\s*'?s\s+(?:Suno\s+)?profile\s*$", '', raw, flags=re.IGNORECASE)
            if raw:
                out["display_name"] = raw

        # Fallback: <title>
        if not out["display_name"]:
            m = re.search(r'<title>([^<]+)</title>', html)
            if m:
                raw = _html.unescape(m.group(1).strip())
                raw = re.sub(r'\s*[|\-–]\s*Suno\s*$', '', raw).strip()
                if raw:
                    out["display_name"] = raw

        # og:image — avatar
        m = re.search(
            r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', html
        )
        if not m:
            m = re.search(
                r'<meta\s+content=["\']([^"\']+)["\']\s+property=["\']og:image["\']', html
            )
        if m:
            out["avatar_url"] = m.group(1).strip()

        # Avatar fallback: first cdn2.suno.ai/<uuid>.jpeg image referenced in HTML
        if not out["avatar_url"]:
            m = re.search(r'https://cdn[12]\.suno\.ai/([a-f0-9-]{36})\.jpeg', html)
            if m:
                out["avatar_url"] = m.group(0)

        # --- Pinned song: first /song/<uuid> in the profile HTML ---
        pinned_uuid = None
        m = re.search(r'/song/([a-f0-9-]{36})', html)
        if m:
            pinned_uuid = m.group(1)

        # --- Collect ALL candidate song UUIDs from the user's playlists ---
        # Suno renders songs client-side; only the pinned song is in the SSR HTML.
        # Playlists ARE rendered server-side, so we scrape them for song UUIDs.
        # Match both absolute (https://suno.com/playlist/UUID) and relative (/playlist/UUID)
        raw_pl_ids = re.findall(r'(?:https://suno\.com)?/playlist/([a-f0-9-]{36})', html)
        playlist_urls = list(dict.fromkeys(
            f"https://suno.com/playlist/{pid}" for pid in raw_pl_ids
        ))[:8]
        print(f"[suno_promotion] playlists found: {len(playlist_urls)} for {profile_url}")
        # Debug: dump first 500 chars of html to see what we actually got
        print(f"[suno_promotion] html snippet: {html[:500]!r}")

        candidate_ids: list[str] = []
        seen_ids: set[str] = set()

        # Non-song UUIDs to exclude (playlists, video uploads from CDN preload links)
        exclude_ids: set[str] = set(raw_pl_ids)
        for cdn_uuid in re.findall(r'video_upload_([a-f0-9-]{36})', html):
            exclude_ids.add(cdn_uuid)

        if pinned_uuid:
            candidate_ids.append(pinned_uuid)
            seen_ids.add(pinned_uuid)

        # --- Primary source: ?page=songs page contains ALL songs in Recent order ---
        # The first song is the latest, the list is already sorted by Suno
        songs_from_page = []
        if songs_html:
            songs_from_page = re.findall(r'/song/([a-f0-9-]{36})', songs_html)
            print(f"[suno_promotion] Found {len(songs_from_page)} songs on ?page=songs")
            if songs_from_page:
                print(f"[suno_promotion] First (latest) song from ?page=songs: {songs_from_page[0]}")
        else:
            print(f"[suno_promotion] No songs_html available (HTTP error)")

        # Add songs from ?page=songs to candidates (take first 20 for metadata fetch)
        latest_from_songs_page = songs_from_page[0] if songs_from_page else None
        for sid in songs_from_page[:20]:
            if sid not in seen_ids:
                candidate_ids.append(sid)
                seen_ids.add(sid)

        print(f"[suno_promotion] {len(candidate_ids)} candidate songs total for {profile_url}")

        # --- Determine creation time via CDN timestamp in og:image ---
        # og:image URL contains a Unix timestamp: _snapshot_0s_<ts>_image.jpeg
        # We fetch the embed page (lightweight) for each candidate to get that ts.
        sem = asyncio.Semaphore(6)

        async def _get_ts_and_title(sid: str) -> tuple[str, int, str | None]:
            """Returns (song_id, unix_timestamp, title_or_None)."""
            async with sem:
                try:
                    async with aiohttp.ClientSession(headers=headers) as s:
                        async with s.get(
                            f"https://suno.com/song/{sid}",
                            timeout=aiohttp.ClientTimeout(total=10),
                        ) as r:
                            if r.status != 200:
                                return sid, 0, None
                            body = await r.text()
                    # Timestamp from CDN image URL — pattern: _snapshot_0s_<unix_ts>_image
                    # The full og:image URL from song pages contains this pattern
                    tm = re.search(r'snapshot_0s_(\d{9,12})', body)
                    ts = int(tm.group(1)) if tm else 0
                    # Title from <title> or og:title
                    title_val = None
                    ttm = re.search(r'<title>(.+?)\s*\|\s*Suno</title>', body)
                    if ttm:
                        raw = ttm.group(1).strip()
                        parts = raw.rsplit(' by ', 1)
                        title_val = parts[0].strip() if len(parts) == 2 else raw
                    if not title_val:
                        ttm = re.search(r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']+)["\']', body)
                        if ttm:
                            title_val = _html.unescape(ttm.group(1).strip())
                    return sid, ts, title_val
                except Exception:
                    return sid, 0, None

        ts_results: list[tuple[str, int, str | None]] = list(
            await asyncio.gather(
                *[_get_ts_and_title(sid) for sid in candidate_ids],
                return_exceptions=False,
            )
        )

        # Sort candidates by timestamp descending
        ts_results.sort(key=lambda x: x[1], reverse=True)

        print(f"[suno_promotion] Top candidates by timestamp:")
        for sid, ts, t in ts_results[:5]:
            print(f"[suno_promotion]   {sid}: ts={ts} title={t!r}")

        ts_map   = {sid: ts    for sid, ts, _ in ts_results}
        title_map = {sid: title for sid, _, title in ts_results if title}

        # Pinned = first UUID found in profile HTML
        if pinned_uuid:
            out["pinned_song_url"]   = f"https://suno.com/song/{pinned_uuid}"
            out["pinned_song_title"] = title_map.get(pinned_uuid)

        # Latest = the first song from ?page=songs (most recent) if available,
        # otherwise fall back to highest timestamp from candidates
        if latest_from_songs_page:
            # Find title from our fetched results
            latest_title = title_map.get(latest_from_songs_page)
            # If we didn't fetch it, try to get metadata now
            if not latest_title:
                try:
                    meta = await _fetch_suno_meta(latest_from_songs_page)
                    latest_title = meta.get("title") if meta else None
                except Exception:
                    pass
            out["latest_song_url"]   = f"https://suno.com/song/{latest_from_songs_page}"
            out["latest_song_title"] = latest_title
        elif ts_results:
            # Fallback: use highest timestamp from candidates
            latest_id, latest_ts, latest_title = ts_results[0]
            out["latest_song_url"]   = f"https://suno.com/song/{latest_id}"
            out["latest_song_title"] = latest_title

            # If pinned == latest, keep both pointing to same song
            if pinned_uuid and latest_id == pinned_uuid:
                out["pinned_song_title"] = out["pinned_song_title"] or latest_title

        return out

    @app.route("/suno-promotion", methods=["GET", "POST"])
    @permission_required('suno_promotion')
    async def suno_promotion():
        owner_id = session["user_id"]
        if request.method == "POST":
            form = await request.form
            action = form.get("action")

            if action == "add":
                raw_url = (form.get("profile_url") or "").strip()
                priority = form.get("priority", "medium")
                if priority not in ("high", "medium", "low"):
                    priority = "medium"
                canonical, handle = _normalize_suno_profile_url(raw_url)
                if not canonical:
                    await flash("Invalid Suno profile URL. Expected: https://suno.com/@handle", "error")
                else:
                    info = await _fetch_suno_profile(canonical)
                    new_id = await db.suno_userlist_add(
                        owner_user_id=owner_id,
                        profile_url=canonical,
                        handle=handle,
                        display_name=info.get("display_name"),
                        avatar_url=info.get("avatar_url"),
                        last_song_url=info.get("latest_song_url") or info.get("last_song_url"),
                        last_song_title=info.get("latest_song_title") or info.get("last_song_title"),
                        pinned_song_url=info.get("pinned_song_url"),
                        pinned_song_title=info.get("pinned_song_title"),
                        latest_song_url=info.get("latest_song_url"),
                        latest_song_title=info.get("latest_song_title"),
                        priority=priority,
                    )
                    if new_id is None:
                        await flash(f"@{handle} is already in your list.", "error")
                    else:
                        await flash(f"Added @{handle}.", "success")

            elif action == "delete":
                entry_id = int(form.get("entry_id", "0"))
                if await db.suno_userlist_delete(owner_id, entry_id):
                    await flash("Entry removed.", "success")

            elif action == "set_priority":
                entry_id = int(form.get("entry_id", "0"))
                priority = form.get("priority", "medium")
                await db.suno_userlist_set_priority(owner_id, entry_id, priority)

            elif action == "mark_done":
                entry_id = int(form.get("entry_id", "0"))
                await db.suno_userlist_set_done(owner_id, entry_id, True)

            elif action == "mark_undone":
                entry_id = int(form.get("entry_id", "0"))
                await db.suno_userlist_set_done(owner_id, entry_id, False)

            elif action == "pause":
                entry_id = int(form.get("entry_id", "0"))
                await db.suno_userlist_set_paused(owner_id, entry_id, True)

            elif action == "unpause":
                entry_id = int(form.get("entry_id", "0"))
                await db.suno_userlist_set_paused(owner_id, entry_id, False)

            elif action == "edit_url":
                entry_id = int(form.get("entry_id", "0"))
                raw_url = (form.get("profile_url") or "").strip()
                canonical, handle = _normalize_suno_profile_url(raw_url)
                if not canonical:
                    await flash("Invalid Suno profile URL.", "error")
                else:
                    info = await _fetch_suno_profile(canonical)
                    ok = await db.suno_userlist_update_url(
                        owner_user_id=owner_id,
                        entry_id=entry_id,
                        profile_url=canonical,
                        handle=handle,
                        display_name=info.get("display_name"),
                        avatar_url=info.get("avatar_url"),
                        last_song_url=info.get("latest_song_url") or info.get("last_song_url"),
                        last_song_title=info.get("latest_song_title") or info.get("last_song_title"),
                        pinned_song_url=info.get("pinned_song_url"),
                        pinned_song_title=info.get("pinned_song_title"),
                        latest_song_url=info.get("latest_song_url"),
                        latest_song_title=info.get("latest_song_title"),
                    )
                    if ok:
                        await flash(f"Entry updated: @{handle}.", "success")
                    else:
                        await flash("Update failed (handle already in list?).", "error")

            elif action == "reset_cycle":
                affected = await db.suno_userlist_reset_done(owner_id)
                if affected:
                    await flash(f"Cycle ended — {affected} entr{'y' if affected == 1 else 'ies'} reopened.", "success")
                else:
                    await flash("No entries to reset.", "error")

            elif action == "set_latest":
                entry_id = int(form.get("entry_id", "0"))
                raw_url = (form.get("latest_song_url") or "").strip()
                # Accept full suno.com/song/UUID or bare UUID
                m = re.search(r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}', raw_url)
                if not m:
                    await flash("Invalid song URL.", "error")
                else:
                    song_uuid = m.group(0)
                    canonical_song_url = f"https://suno.com/song/{song_uuid}"
                    # Try to fetch title from song page
                    try:
                        meta = await _fetch_suno_meta(song_uuid)
                        song_title = meta.get("title") if meta else None
                    except Exception:
                        song_title = None
                    entry = await db.suno_userlist_get(owner_id, entry_id)
                    if entry:
                        await db.suno_userlist_update_meta(
                            owner_user_id=owner_id,
                            entry_id=entry_id,
                            display_name=entry.get("display_name"),
                            avatar_url=entry.get("avatar_url"),
                            last_song_url=canonical_song_url,
                            last_song_title=song_title,
                            pinned_song_url=entry.get("pinned_song_url"),
                            pinned_song_title=entry.get("pinned_song_title"),
                            latest_song_url=canonical_song_url,
                            latest_song_title=song_title,
                        )
                        await flash(f"Latest song set to: {song_title or song_uuid}", "success")

            elif action == "refresh":
                entry_id = int(form.get("entry_id", "0"))
                entry = await db.suno_userlist_get(owner_id, entry_id)
                if entry:
                    info = await _fetch_suno_profile(entry["profile_url"])
                    await db.suno_userlist_update_meta(
                        owner_user_id=owner_id,
                        entry_id=entry_id,
                        display_name=info.get("display_name") or entry.get("display_name"),
                        avatar_url=info.get("avatar_url") or entry.get("avatar_url"),
                        last_song_url=info.get("latest_song_url") or info.get("last_song_url") or entry.get("last_song_url"),
                        last_song_title=info.get("latest_song_title") or info.get("last_song_title") or entry.get("last_song_title"),
                        pinned_song_url=info.get("pinned_song_url") or entry.get("pinned_song_url"),
                        pinned_song_title=info.get("pinned_song_title") or entry.get("pinned_song_title"),
                        latest_song_url=info.get("latest_song_url") or entry.get("latest_song_url"),
                        latest_song_title=info.get("latest_song_title") or entry.get("latest_song_title"),
                    )

            elif action == "refresh_all":
                all_entries = await db.suno_userlist_list(owner_id)
                refreshed = 0
                errors = 0
                for entry in all_entries:
                    try:
                        info = await _fetch_suno_profile(entry["profile_url"])
                        await db.suno_userlist_update_meta(
                            owner_user_id=owner_id,
                            entry_id=entry["id"],
                            display_name=info.get("display_name") or entry.get("display_name"),
                            avatar_url=info.get("avatar_url") or entry.get("avatar_url"),
                            last_song_url=info.get("latest_song_url") or info.get("last_song_url") or entry.get("last_song_url"),
                            last_song_title=info.get("latest_song_title") or info.get("last_song_title") or entry.get("last_song_title"),
                            pinned_song_url=info.get("pinned_song_url") or entry.get("pinned_song_url"),
                            pinned_song_title=info.get("pinned_song_title") or entry.get("pinned_song_title"),
                            latest_song_url=info.get("latest_song_url") or entry.get("latest_song_url"),
                            latest_song_title=info.get("latest_song_title") or entry.get("latest_song_title"),
                        )
                        refreshed += 1
                    except Exception as exc:
                        print(f"[suno_promotion] Refresh failed for {entry.get('handle')}: {exc}")
                        errors += 1
                await flash(f"Refreshed {refreshed} entries ({errors} errors).", "success")

            # Preserve current filter state
            qs = {k: v for k, v in (await request.form).items()
                  if k in ("filter_priority", "filter_status", "hide_paused")}
            return redirect(url_for("suno_promotion", **qs))

        # GET — apply filters
        all_entries = await db.suno_userlist_list(owner_id)
        filter_priority = request.args.get("filter_priority", "all")
        filter_status = request.args.get("filter_status", "open")  # open | done | paused | all
        hide_paused = request.args.get("hide_paused", "0") == "1"

        def keep(e):
            if filter_priority in ("high", "medium", "low") and e["priority"] != filter_priority:
                return False
            if filter_status == "open" and (e["done"] or e["paused"]):
                return False
            if filter_status == "done" and not e["done"]:
                return False
            if filter_status == "paused" and not e["paused"]:
                return False
            if filter_status == "all" and hide_paused and e["paused"]:
                return False
            return True

        entries = [e for e in all_entries if keep(e)]
        total = len(all_entries)
        open_count = sum(1 for e in all_entries if not e["done"])
        done_count = total - open_count
        paused_count = sum(1 for e in all_entries if e["paused"])

        return await render_template(
            "suno_promotion.html",
            entries=entries,
            total=total,
            open_count=open_count,
            done_count=done_count,
            paused_count=paused_count,
            shown_count=len(entries),
            filter_priority=filter_priority,
            filter_status=filter_status,
            hide_paused=hide_paused,
        )

    # --- Suno Info -----------------------------------------------------------
    #
    # Loads a public Suno playlist URL and shows every song in a table, with
    # an integrated player (audio + optional video cover), full lyrics, and
    # the style prompt / tags used to generate the song.

    @app.route("/suno-info")
    @permission_required('suno_info')
    async def suno_info():
        import json as _json
        _user = await db.get_web_user_by_id(session["user_id"])
        _perms = []
        try: _perms = _json.loads(_user.get("permissions") or "[]")
        except (ValueError, TypeError): pass
        has_party = bool(_user.get("is_admin")) or "party_playlist" in _perms
        return await render_template("suno_info.html",
            channels=await _get_player_channels(), has_party=has_party)

    @app.route("/api/suno-info/playlist")
    @permission_required('suno_info')
    async def api_suno_info_playlist():
        """Parse a Suno playlist URL or ElevenMusic track URL."""
        import aiohttp as _aiohttp, html as _html
        from quart import jsonify
        from bot.stream_manager import parse_suno_playlist
        url = (request.args.get("url") or "").strip()
        if not url:
            return jsonify({"error": "missing url"}), 400

        eleven_match = ELEVENMUSIC_TRACK_RE.search(url)
        if eleven_match:
            track_id = eleven_match.group(1).lower()
            meta = await _fetch_elevenmusic_meta(track_id)
            song = {
                "uuid": track_id,
                "type": "elevenmusic",
                "title": meta.get("title") or track_id[:8],
                "artist": meta.get("artist") or "",
                "image_url": meta.get("image_url") or "",
                "audio_url": meta.get("audio_url") or "",
                "duration": meta.get("duration"),
                "source_url": meta.get("source_url") or url,
                "_meta": meta,
            }
            return jsonify({"songs": [song], "name": "ElevenMusic"})

        try:
            songs = await parse_suno_playlist(url)
        except Exception as e:
            return jsonify({"error": f"parse failed: {e}"}), 502
        if not songs:
            return jsonify({"error": "no songs found in playlist"}), 404

        # Best-effort: fetch the playlist's display name from the page's
        # og:title — independent of which scraping path returned the songs.
        name = ""
        try:
            async with _aiohttp.ClientSession() as sess:
                async with sess.get(
                    url,
                    headers={"User-Agent": "Mozilla/5.0 (X11; Linux) AppleWebKit/537.36"},
                    timeout=_aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        page = await resp.text()
                        m = re.search(
                            r'<meta\s+property="og:title"\s+content="([^"]+)"',
                            page,
                        )
                        if not m:
                            m = re.search(r'<title>([^<]+)</title>', page)
                        if m:
                            raw = _html.unescape(m.group(1).strip())
                            # strip trailing "| Suno"
                            raw = re.sub(r'\s*[|\-\u2013]\s*Suno\s*$', '', raw).strip()
                            name = raw
        except Exception:
            pass
        return jsonify({"songs": songs, "name": name})

    @app.route("/api/suno-info/eleven-song/<track_id>")
    @permission_required('suno_info')
    async def api_suno_info_eleven_song(track_id):
        """Return public metadata for a single ElevenMusic track."""
        from quart import jsonify

        if not re.fullmatch(r"[A-Fa-f0-9]{24}", track_id or ""):
            return jsonify({"error": "invalid track id"}), 400
        return jsonify(await _fetch_elevenmusic_meta(track_id.lower()))

    @app.route("/api/suno-info/song/<uuid>")
    @permission_required('suno_info')
    async def api_suno_info_song(uuid):
        """Return rich metadata for a single Suno song: title, artist, cover,
        video_url, lyrics, style prompt, tags, model, stats."""
        import aiohttp as _aiohttp, re as _re, html as _html
        from quart import jsonify

        # Reuse the embed-based helper for the basics (lyrics, image, video).
        base = await _fetch_suno_meta(uuid)

        # Now fetch the public song page for richer fields (gpt_description_prompt,
        # tags, model, stats). This is the same scraping strategy used in the
        # Suno Analyzer route.
        url = f"https://suno.com/song/{uuid}"
        headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
        result: dict = {
            "uuid": uuid,
            "title": base.get("title"),
            "artist": base.get("artist"),
            "image_url": base.get("image_url"),
            "video_url": base.get("video_url"),
            "lyrics": base.get("lyrics"),
            "handle": base.get("handle"),
            "tags": [],
            "prompt": "",
            "model": "",
            "type": "",
            "plays": None,
            "likes": None,
            "created_at": "",
            "duration": None,
        }
        try:
            async with _aiohttp.ClientSession() as sess:
                async with sess.get(url, headers=headers,
                                    timeout=_aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        html = await resp.text()
                    else:
                        html = ""
        except Exception:
            html = ""

        if html:
            # Tags / genre — comma-separated string in JSON
            m = _re.search(r'\\"tags\\":\\"([^\\]+)\\"', html)
            if not m:
                m = _re.search(r'"tags":"([^"]+)"', html)
            if m:
                result["tags"] = [
                    t.strip() for t in m.group(1).split(",") if t.strip()
                ]
            # Style prompt — usually inline, but for longer prompts (or with
            # emojis) Suno stores it as an RSC chunk reference like `$3d`.
            m = _re.search(
                r'\\"gpt_description_prompt\\":\\"((?:[^\\]|\\.)*?)\\"', html
            )
            if not m:
                m = _re.search(
                    r'"gpt_description_prompt":"((?:[^"\\]|\\.)*)"', html
                )
            if m:
                raw_prompt = m.group(1).replace('\\n', '\n').replace('\\"', '"')
            else:
                raw_prompt = ""
            if raw_prompt and not _RSC_REF_RE.match(raw_prompt):
                result["prompt"] = raw_prompt
            else:
                # Resolve RSC reference from the flight payload
                try:
                    import json as _json
                    chunks = _re.findall(
                        r'self\.__next_f\.push\(\[1,"(.*?)"\]\)',
                        html, _re.DOTALL,
                    )
                    decoded = []
                    for c in chunks:
                        try:
                            decoded.append(_json.loads('"' + c + '"'))
                        except Exception:
                            decoded.append(
                                c.replace('\\n', '\n')
                                 .replace('\\"', '"')
                                 .replace('\\\\', '\\')
                            )
                    full = "".join(decoded)
                    mref = _re.search(
                        r'"gpt_description_prompt":"\$([0-9a-f]+)"', full
                    )
                    if mref:
                        ref = mref.group(1)
                        # The hex prefix is the UTF-8 byte length of the
                        # payload — slice on a bytes view to avoid
                        # over-capturing on emoji / non-ASCII characters.
                        full_bytes = full.encode("utf-8")
                        tpat_b = _re.compile(
                            rb'(?:^|\n)' + _re.escape(ref).encode() +
                            rb':T([0-9a-f]+),'
                        )
                        tfnd = tpat_b.search(full_bytes)
                        if tfnd:
                            length = int(tfnd.group(1), 16)
                            b_start = tfnd.end()
                            result["prompt"] = full_bytes[b_start:b_start + length] \
                                .decode("utf-8", errors="replace").rstrip()
                except Exception:
                    pass
            # Stats
            for key, pat in [
                ("plays", r'\\"play_count\\":(\d+)'),
                ("likes", r'\\"upvote_count\\":(\d+)'),
            ]:
                m = _re.search(pat, html)
                if m:
                    result[key] = int(m.group(1))
            # Model + type + created_at
            m = _re.search(r'\\"major_model_version\\":\\"([^\\]+)\\"', html)
            if m:
                result["model"] = m.group(1)
            m = _re.search(r'\\"type\\":\\"([^\\]+)\\"', html)
            if m:
                result["type"] = m.group(1)
            m = _re.search(r'\\"created_at\\":\\"([^\\]+)\\"', html)
            if m:
                result["created_at"] = m.group(1)
            # Duration in seconds (float). Stored as `audio_duration` or `duration`.
            for pat in (
                r'\\"audio_duration\\":([0-9.]+)',
                r'\\"duration\\":([0-9.]+)',
                r'"audio_duration":([0-9.]+)',
                r'"duration":([0-9.]+)',
            ):
                m = _re.search(pat, html)
                if m:
                    try:
                        result["duration"] = float(m.group(1))
                    except ValueError:
                        pass
                    break
            # Author fallback if og:description didn't have it
            if not result.get("artist"):
                m = _re.search(r'\\"display_name\\":\\"([^\\]+)\\"', html)
                if m:
                    result["artist"] = m.group(1)

        # Final image fallback to the standard Suno CDN path
        if not result.get("image_url"):
            result["image_url"] = f"https://cdn1.suno.ai/image_large_{uuid}.jpeg"
        return jsonify(result)

    @app.route("/api/translate-lyrics", methods=["POST"])
    @login_required
    async def api_translate_lyrics():
        from quart import jsonify
        import asyncio
        data = await request.get_json(silent=True) or {}
        text = (data.get("text") or "").strip()
        lang = (data.get("lang") or "").strip().upper()
        if not text or lang not in ("DE", "EN", "FR", "ES", "JA", "IT", "PT"):
            return jsonify({"ok": False, "error": "Invalid input"}), 400
        try:
            from deep_translator import GoogleTranslator
            loop = asyncio.get_event_loop()

            # Split at paragraph/stanza boundaries (blank lines) so each stanza
            # gets independent language detection — essential for mixed-language
            # lyrics (e.g. English intro + Finnish verses in the same song).
            import re as _re
            # Preserve blank lines as empty entries so we can reassemble later
            raw_paragraphs = _re.split(r'(\n\s*\n)', text)
            MAX_CHUNK = 4000
            chunks = []
            separators = []  # parallel list of separators between chunks
            cur_text = ''
            cur_sep = ''
            for i, part in enumerate(raw_paragraphs):
                if _re.fullmatch(r'\n\s*\n', part):
                    cur_sep = part  # this is a separator, hold it
                    continue
                candidate = cur_text + (cur_sep if cur_text else '') + part
                if cur_text and len(candidate) > MAX_CHUNK:
                    chunks.append(cur_text)
                    separators.append(cur_sep)
                    cur_text = part
                    cur_sep = ''
                else:
                    cur_text = candidate
                    cur_sep = ''
            if cur_text:
                chunks.append(cur_text)

            # Helpers for per-chunk translation
            import unicodedata as _ud
            _LETTER_RE = _re.compile(
                r'[a-zA-ZÀ-ÿ\u0100-\u024F\u0370-\u03FF\u0400-\u04FF\u0600-\u06FF]'
            )
            _SEC_RE = _re.compile(r'^\s*\[[^\]]+\]\s*$')

            def _clean(text: str) -> str:
                """NFKC-normalize and strip invisible Unicode format characters
                (zero-width joiners, BOM, soft-hyphens, etc.) that can confuse
                Google Translate's language detector."""
                text = _ud.normalize("NFKC", text)
                return ''.join(
                    ch for ch in text
                    if ch in ' \t\n\r' or not _ud.category(ch).startswith('C')
                )

            def _is_structural(line: str) -> bool:
                s = line.strip()
                if not s or _SEC_RE.match(s):
                    return True
                return len(_LETTER_RE.findall(s)) / len(s) < 0.45

            async def _do_translate(text: str) -> str:
                """Translate text; if result is unchanged, retry with source=de."""
                result = await loop.run_in_executor(
                    None,
                    lambda c=text: GoogleTranslator(source="auto", target=lang.lower()).translate(c),
                )
                result = result or ''
                if result.strip() == text.strip():
                    # Auto-detect returned source unchanged — retry with explicit German
                    result = await loop.run_in_executor(
                        None,
                        lambda c=text: GoogleTranslator(source="de", target=lang.lower()).translate(c),
                    )
                    result = result or text
                return result

            async def _translate_chunk(raw: str) -> str:
                plain = _clean(raw)
                lines = plain.split('\n')
                struct_idx = {i for i, l in enumerate(lines) if _is_structural(l)}
                content = [lines[i] for i in range(len(lines)) if i not in struct_idx]
                if not content:
                    return plain
                content_text = '\n'.join(content)
                try:
                    translated = await _do_translate(content_text)
                    t_lines = translated.split('\n')
                except Exception:
                    t_lines = content
                t_iter = iter(t_lines)
                return '\n'.join(
                    lines[i] if i in struct_idx else next(t_iter, lines[i])
                    for i in range(len(lines))
                )

            # Translate each stanza independently (auto-detect per stanza)
            translated_parts = []
            for chunk in chunks:
                stripped = chunk.strip()
                if not stripped:
                    translated_parts.append('')
                    continue
                translated_parts.append(await _translate_chunk(stripped))

            return jsonify({"ok": True, "translated": '\n\n'.join(translated_parts)})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.route("/api/suno-info/comments/<uuid>")
    @permission_required('suno_info')
    async def api_suno_info_comments(uuid):
        """Proxy Suno's public comments endpoint for a clip.

        Suno exposes `GET /api/gen/{clip_id}/comments` on `studio-api.prod.suno.com`
        without authentication. Response shape:
            {"next_cursor": "<base64>"|null, "results": [ {comment...}, ... ]}
        We pass through the cursor for pagination.
        """
        import aiohttp as _aiohttp
        from quart import jsonify
        cursor = (request.args.get("cursor") or "").strip()
        api_url = (
            f"https://studio-api.prod.suno.com/api/gen/{uuid}/comments"
        )
        params = {"cursor": cursor} if cursor else {}
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
        try:
            async with _aiohttp.ClientSession() as sess:
                async with sess.get(api_url, params=params, headers=headers,
                                    timeout=_aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return jsonify({"error": f"upstream HTTP {resp.status}",
                                        "results": [], "next_cursor": None}), resp.status
                    data = await resp.json()
        except Exception as e:
            return jsonify({"error": str(e), "results": [],
                            "next_cursor": None}), 502
        # Also fetch total count (cheap, separate small endpoint).
        count = None
        try:
            async with _aiohttp.ClientSession() as sess:
                async with sess.get(
                    f"https://studio-api.prod.suno.com/api/gen/{uuid}/comments/count",
                    headers=headers, timeout=_aiohttp.ClientTimeout(total=8),
                ) as r:
                    if r.status == 200:
                        cdata = await r.json()
                        count = cdata.get("count")
        except Exception:
            pass
        return jsonify({
            "results": data.get("results") or [],
            "next_cursor": data.get("next_cursor"),
            "count": count,
        })

    # --- User Preferences API -------------------------------------------------

    @app.route("/api/user/preferences", methods=["GET"])
    async def api_get_user_preferences():
        """Get current user's UI preferences."""
        from quart import jsonify
        user_id = session.get("user_id")
        if not user_id:
            return jsonify({"error": "not authenticated"}), 401
        prefs = await db.get_all_user_preferences(user_id)
        return jsonify(prefs)

    @app.route("/api/user/preferences", methods=["POST"])
    async def api_set_user_preferences():
        """Save current user's UI preferences."""
        from quart import jsonify
        user_id = session.get("user_id")
        if not user_id:
            return jsonify({"error": "not authenticated"}), 401
        data = await request.get_json()
        if not data:
            return jsonify({"error": "no data"}), 400
        try:
            saved = {}
            if "suno_player_split" in data:
                val = max(0.2, min(0.8, float(data["suno_player_split"])))
                await db.set_user_preference(user_id, "suno_player_split", val)
                saved["suno_player_split"] = val
            if "dc_channel" in data:
                await db.set_user_preference(user_id, "dc_channel", str(data["dc_channel"] or ""))
                saved["dc_channel"] = data["dc_channel"]
            if "dc_limit" in data:
                val = max(1, min(500, int(data["dc_limit"])))
                await db.set_user_preference(user_id, "dc_limit", val)
                saved["dc_limit"] = val
            if "dc_days" in data:
                val = max(0, int(data["dc_days"]))
                await db.set_user_preference(user_id, "dc_days", val)
                saved["dc_days"] = val
            if saved:
                return jsonify(saved)
            return jsonify({"error": "no known keys"}), 400
        except Exception as e:
            import traceback
            print(f"[api_set_user_preferences] Error: {e}")
            print(traceback.format_exc())
            return jsonify({"error": str(e)}), 500

    # --- Auto Translate ---

    @app.route("/auto-translate", methods=["GET", "POST"])
    @permission_required('auto_translate')
    async def auto_translate_admin():
        if request.method == "POST":
            form = await request.form
            enabled = "on" if form.get("enabled") else "off"
            channel_id = (form.get("channel_id") or "").strip()
            langs = form.getlist("languages")
            engine = form.get("engine", "google")
            if engine not in ("google", "llm"):
                engine = "google"
            skip_open  = (form.get("skip_open")  or "").strip()[:4]
            skip_close = (form.get("skip_close") or "").strip()[:4]
            await db.set_setting("auto_translate_enabled", enabled)
            await db.set_setting("auto_translate_channel_id", channel_id)
            await db.set_setting("auto_translate_languages", ",".join(langs))
            await db.set_setting("auto_translate_engine", engine)
            await db.set_setting("auto_translate_skip_open", skip_open)
            await db.set_setting("auto_translate_skip_close", skip_close)
            await flash("Auto-translate settings saved.", "success")
            return redirect(url_for("auto_translate_admin"))

        enabled = (await db.get_setting("auto_translate_enabled")) == "on"
        channel_id = str(await db.get_setting("auto_translate_channel_id") or "")
        langs_str = await db.get_setting("auto_translate_languages") or ""
        selected_langs = [l.strip() for l in langs_str.split(",") if l.strip()]
        engine     = await db.get_setting("auto_translate_engine") or "google"
        skip_open  = await db.get_setting("auto_translate_skip_open")  or ""
        skip_close = await db.get_setting("auto_translate_skip_close") or ""
        guild = get_guild()
        text_channels = []
        if guild:
            import discord as _discord
            for ch in sorted(guild.text_channels, key=lambda c: c.position):
                text_channels.append({"id": ch.id, "name": ch.name})
        from config import Config as _Cfg
        return await render_template(
            "auto_translate.html",
            enabled=enabled,
            channel_id=channel_id,
            selected_langs=selected_langs,
            engine=engine,
            skip_open=skip_open,
            skip_close=skip_close,
            llm_model=_Cfg.LLM_MODEL,
            text_channels=text_channels,
        )

    # --- RPG Admin ---

    @app.route("/rpg", methods=["GET", "POST"])
    @permission_required('rpg')
    async def rpg_admin():
        import json as _json
        await db.ensure_rpg_tables()

        tab = request.args.get("tab", "adventures")
        adventure_id_arg = request.args.get("adventure_id", type=int)

        if request.method == "POST":
            form = await request.form
            action = form.get("action", "")

            # --- Adventures ---
            if action == "adv_create":
                name = (form.get("name") or "").strip()
                if not name:
                    await flash("Adventure name is required.", "error")
                else:
                    new_id = await db.rpg_create_adventure(
                        name=name,
                        description=(form.get("description") or "").strip(),
                        intro_text=(form.get("intro_text") or "").strip(),
                        llm_system_prompt=(form.get("llm_system_prompt") or "").strip(),
                        start_scene_key=(form.get("start_scene_key") or "").strip(),
                    )
                    await flash(f"Adventure '{name}' created.", "success")
                    return redirect(url_for("rpg_admin", tab="adventures",
                                            adventure_id=new_id))

            elif action == "adv_update":
                aid = int(form.get("adventure_id") or 0)
                if aid:
                    await db.rpg_update_adventure(
                        aid,
                        name=(form.get("name") or "").strip(),
                        description=(form.get("description") or "").strip(),
                        intro_text=(form.get("intro_text") or "").strip(),
                        llm_system_prompt=(form.get("llm_system_prompt") or "").strip(),
                        start_scene_key=(form.get("start_scene_key") or "").strip(),
                        is_active=1 if form.get("is_active") else 0,
                    )
                    await flash("Adventure updated.", "success")
                return redirect(url_for("rpg_admin", tab="adventures",
                                        adventure_id=aid))

            elif action == "adv_delete":
                aid = int(form.get("adventure_id") or 0)
                if aid:
                    await db.rpg_delete_adventure(aid)
                    await flash("Adventure deleted.", "success")
                return redirect(url_for("rpg_admin", tab="adventures"))

            elif action == "adv_import":
                raw_json = (form.get("adventures_json") or "").strip()
                if not raw_json:
                    await flash("JSON input is required.", "error")
                    return redirect(url_for("rpg_admin", tab="adventures"))
                try:
                    payload = _json.loads(raw_json)
                except _json.JSONDecodeError as exc:
                    await flash(
                        f"Invalid JSON: {exc.msg} at line {exc.lineno}.", "error"
                    )
                    return redirect(url_for("rpg_admin", tab="adventures"))

                if isinstance(payload, dict) and "adventures" in payload:
                    items = payload["adventures"]
                elif isinstance(payload, dict) and "name" in payload:
                    items = [payload]
                elif isinstance(payload, list):
                    items = payload
                else:
                    await flash(
                        "JSON must be an adventure object, a list of "
                        "adventures, or {\"adventures\":[...]}.", "error",
                    )
                    return redirect(url_for("rpg_admin", tab="adventures"))

                imported = 0
                errors: list[str] = []
                last_id = None
                for idx, raw_adv in enumerate(items, start=1):
                    if not isinstance(raw_adv, dict):
                        errors.append(f"Adventure {idx}: expected an object.")
                        continue
                    name = str(raw_adv.get("name") or "").strip()
                    if not name:
                        errors.append(f"Adventure {idx}: 'name' is required.")
                        continue
                    raw_scenes = raw_adv.get("scenes") or []
                    if not isinstance(raw_scenes, list):
                        errors.append(
                            f"Adventure {idx} ({name}): 'scenes' must be an array."
                        )
                        continue
                    prepared_scenes: list[dict] = []
                    keys_seen: set[str] = set()
                    bad = False
                    for sidx, raw_scene in enumerate(raw_scenes, start=1):
                        if not isinstance(raw_scene, dict):
                            errors.append(
                                f"Adventure {idx} scene {sidx}: must be an object."
                            )
                            bad = True
                            break
                        skey = str(raw_scene.get("scene_key") or "").strip()
                        if not skey:
                            errors.append(
                                f"Adventure {idx} scene {sidx}: 'scene_key' required."
                            )
                            bad = True
                            break
                        if skey in keys_seen:
                            errors.append(
                                f"Adventure {idx}: duplicate scene_key '{skey}'."
                            )
                            bad = True
                            break
                        keys_seen.add(skey)
                        # Accept either inline object under "data" or string "data_json"
                        if "data_json" in raw_scene:
                            data_str = raw_scene["data_json"]
                            if not isinstance(data_str, str):
                                data_str = _json.dumps(data_str)
                            try:
                                _json.loads(data_str or "{}")
                            except _json.JSONDecodeError as exc:
                                errors.append(
                                    f"Adventure {idx} scene '{skey}': "
                                    f"invalid data_json ({exc.msg})."
                                )
                                bad = True
                                break
                        else:
                            data_obj = raw_scene.get("data") or {}
                            if not isinstance(data_obj, (dict, list)):
                                errors.append(
                                    f"Adventure {idx} scene '{skey}': "
                                    "'data' must be an object or array."
                                )
                                bad = True
                                break
                            data_str = _json.dumps(data_obj)
                        prepared_scenes.append({
                            "scene_key": skey,
                            "title": str(raw_scene.get("title") or ""),
                            "narration": str(raw_scene.get("narration") or ""),
                            "scene_type": str(raw_scene.get("scene_type") or "story"),
                            "data_json": data_str or "{}",
                        })
                    if bad:
                        continue
                    # Validate start_scene_key references an actual scene if given
                    start_key = str(raw_adv.get("start_scene_key") or "").strip()
                    if start_key and start_key not in keys_seen:
                        errors.append(
                            f"Adventure {idx} ({name}): start_scene_key "
                            f"'{start_key}' not found among the imported scenes."
                        )
                        continue
                    # Validate that every choice/next/after_combat reference exists
                    ref_errors: list[str] = []
                    for ps in prepared_scenes:
                        try:
                            data_obj = _json.loads(ps["data_json"])
                        except _json.JSONDecodeError:
                            continue
                        if not isinstance(data_obj, dict):
                            continue
                        for choice in (data_obj.get("choices") or []):
                            nxt = choice.get("next") if isinstance(choice, dict) else None
                            if nxt and nxt not in keys_seen:
                                ref_errors.append(
                                    f"scene '{ps['scene_key']}' choice -> '{nxt}' missing"
                                )
                        for ref_key in ("next", "after_combat"):
                            ref = data_obj.get(ref_key)
                            if ref and ref not in keys_seen:
                                ref_errors.append(
                                    f"scene '{ps['scene_key']}' {ref_key} -> '{ref}' missing"
                                )
                    if ref_errors:
                        errors.append(
                            f"Adventure {idx} ({name}): unresolved scene refs — "
                            + "; ".join(ref_errors[:5])
                            + (" …" if len(ref_errors) > 5 else "")
                        )
                        continue
                    try:
                        last_id = await db.rpg_import_adventure(
                            {
                                "name": name,
                                "description": str(raw_adv.get("description") or ""),
                                "intro_text": str(raw_adv.get("intro_text") or ""),
                                "llm_system_prompt": str(
                                    raw_adv.get("llm_system_prompt") or ""
                                ),
                                "start_scene_key": start_key,
                                "is_active": bool(raw_adv.get("is_active", True)),
                            },
                            prepared_scenes,
                        )
                        imported += 1
                    except Exception as e:
                        errors.append(f"Adventure {idx} ({name}): DB error — {e}")

                if errors:
                    preview = " · ".join(errors[:5])
                    if len(errors) > 5:
                        preview += f" (+{len(errors) - 5} more)"
                    if imported:
                        await flash(
                            f"Imported {imported} adventure(s) with errors: {preview}",
                            "error",
                        )
                    else:
                        await flash(f"Import failed: {preview}", "error")
                else:
                    await flash(
                        f"Imported {imported} adventure(s) successfully.", "success"
                    )
                if imported == 1 and last_id:
                    return redirect(url_for("rpg_admin", tab="adventures",
                                            adventure_id=last_id))
                return redirect(url_for("rpg_admin", tab="adventures"))

            # --- Scenes ---
            elif action == "scene_create":
                aid = int(form.get("adventure_id") or 0)
                key = (form.get("scene_key") or "").strip()
                if not aid or not key:
                    await flash("Adventure and scene key are required.", "error")
                else:
                    data_raw = (form.get("data_json") or "{}").strip() or "{}"
                    try:
                        _json.loads(data_raw)
                    except _json.JSONDecodeError as e:
                        await flash(f"Invalid scene JSON: {e.msg}", "error")
                        return redirect(url_for("rpg_admin", tab="scenes",
                                                adventure_id=aid))
                    await db.rpg_create_scene(
                        aid, key,
                        (form.get("title") or "").strip(),
                        (form.get("narration") or "").strip(),
                        (form.get("scene_type") or "story").strip(),
                        data_raw,
                    )
                    await flash(f"Scene '{key}' created.", "success")
                return redirect(url_for("rpg_admin", tab="scenes",
                                        adventure_id=aid))

            elif action == "scene_update":
                sid = int(form.get("scene_id") or 0)
                aid = int(form.get("adventure_id") or 0)
                if sid:
                    data_raw = (form.get("data_json") or "{}").strip() or "{}"
                    try:
                        _json.loads(data_raw)
                    except _json.JSONDecodeError as e:
                        await flash(f"Invalid scene JSON: {e.msg}", "error")
                        return redirect(url_for("rpg_admin", tab="scenes",
                                                adventure_id=aid))
                    await db.rpg_update_scene(
                        sid,
                        scene_key=(form.get("scene_key") or "").strip(),
                        title=(form.get("title") or "").strip(),
                        narration=(form.get("narration") or "").strip(),
                        scene_type=(form.get("scene_type") or "story").strip(),
                        data_json=data_raw,
                    )
                    await flash("Scene updated.", "success")
                return redirect(url_for("rpg_admin", tab="scenes",
                                        adventure_id=aid))

            elif action == "scene_delete":
                sid = int(form.get("scene_id") or 0)
                aid = int(form.get("adventure_id") or 0)
                if sid:
                    await db.rpg_delete_scene(sid)
                    await flash("Scene deleted.", "success")
                return redirect(url_for("rpg_admin", tab="scenes",
                                        adventure_id=aid))

            # --- Classes ---
            elif action == "class_upsert":
                abilities_raw = (form.get("abilities_json") or "[]").strip() or "[]"
                try:
                    _json.loads(abilities_raw)
                except _json.JSONDecodeError as e:
                    await flash(f"Invalid abilities JSON: {e.msg}", "error")
                    return redirect(url_for("rpg_admin", tab="classes"))
                await db.rpg_upsert_class(
                    class_key=(form.get("class_key") or "").strip(),
                    name=(form.get("name") or "").strip(),
                    description=(form.get("description") or "").strip(),
                    base_hp=int(form.get("base_hp") or 20),
                    base_attack=int(form.get("base_attack") or 5),
                    base_defense=int(form.get("base_defense") or 5),
                    base_agility=int(form.get("base_agility") or 5),
                    base_mana=int(form.get("base_mana") or 10),
                    abilities_json=abilities_raw,
                )
                await flash("Class saved.", "success")
                return redirect(url_for("rpg_admin", tab="classes"))

            elif action == "class_delete":
                key = (form.get("class_key") or "").strip()
                if key:
                    await db.rpg_delete_class(key)
                    await flash("Class deleted.", "success")
                return redirect(url_for("rpg_admin", tab="classes"))

            # --- Enemies ---
            elif action == "enemy_upsert":
                abilities_raw = (form.get("abilities_json") or "[]").strip() or "[]"
                loot_raw = (form.get("loot_json") or "[]").strip() or "[]"
                try:
                    _json.loads(abilities_raw)
                    _json.loads(loot_raw)
                except _json.JSONDecodeError as e:
                    await flash(f"Invalid JSON: {e.msg}", "error")
                    return redirect(url_for("rpg_admin", tab="enemies"))
                await db.rpg_upsert_enemy(
                    enemy_key=(form.get("enemy_key") or "").strip(),
                    name=(form.get("name") or "").strip(),
                    description=(form.get("description") or "").strip(),
                    hp=int(form.get("hp") or 15),
                    attack=int(form.get("attack") or 4),
                    defense=int(form.get("defense") or 3),
                    agility=int(form.get("agility") or 4),
                    abilities_json=abilities_raw,
                    loot_json=loot_raw,
                    xp_reward=int(form.get("xp_reward") or 10),
                )
                await flash("Enemy saved.", "success")
                return redirect(url_for("rpg_admin", tab="enemies"))

            elif action == "enemy_delete":
                key = (form.get("enemy_key") or "").strip()
                if key:
                    await db.rpg_delete_enemy(key)
                    await flash("Enemy deleted.", "success")
                return redirect(url_for("rpg_admin", tab="enemies"))

            elif action == "enemy_import":
                count, errors = await _rpg_import_enemies(
                    db, (form.get("enemies_json") or "").strip(), _json
                )
                if errors:
                    preview = " · ".join(errors[:5])
                    if len(errors) > 5:
                        preview += f" (+{len(errors) - 5} more)"
                    if count:
                        await flash(
                            f"Imported {count} enemy/enemies with errors: {preview}",
                            "error",
                        )
                    else:
                        await flash(f"Import failed: {preview}", "error")
                else:
                    await flash(
                        f"Imported {count} enemy/enemies successfully.", "success"
                    )
                return redirect(url_for("rpg_admin", tab="enemies"))

            # --- Items ---
            elif action == "item_upsert":
                effect_raw = (form.get("effect_json") or "{}").strip() or "{}"
                try:
                    _json.loads(effect_raw)
                except _json.JSONDecodeError as e:
                    await flash(f"Invalid effect JSON: {e.msg}", "error")
                    return redirect(url_for("rpg_admin", tab="items"))
                await db.rpg_upsert_item(
                    item_key=(form.get("item_key") or "").strip(),
                    name=(form.get("name") or "").strip(),
                    description=(form.get("description") or "").strip(),
                    item_type=(form.get("item_type") or "misc").strip(),
                    effect_json=effect_raw,
                )
                await flash("Item saved.", "success")
                return redirect(url_for("rpg_admin", tab="items"))

            elif action == "item_delete":
                key = (form.get("item_key") or "").strip()
                if key:
                    await db.rpg_delete_item(key)
                    await flash("Item deleted.", "success")
                return redirect(url_for("rpg_admin", tab="items"))

            elif action == "item_import":
                count, errors = await _rpg_import_items(
                    db, (form.get("items_json") or "").strip(), _json
                )
                if errors:
                    preview = " · ".join(errors[:5])
                    if len(errors) > 5:
                        preview += f" (+{len(errors) - 5} more)"
                    if count:
                        await flash(
                            f"Imported {count} item(s) with errors: {preview}",
                            "error",
                        )
                    else:
                        await flash(f"Import failed: {preview}", "error")
                else:
                    await flash(f"Imported {count} item(s) successfully.", "success")
                return redirect(url_for("rpg_admin", tab="items"))

            # --- Characters / Parties (admin override) ---
            elif action == "char_delete":
                cid = int(form.get("character_id") or 0)
                if cid:
                    await db.rpg_delete_character(cid)
                    await flash("Character deleted.", "success")
                return redirect(url_for("rpg_admin", tab="characters"))

            elif action == "party_delete":
                pid = int(form.get("party_id") or 0)
                if pid:
                    await db.rpg_delete_party(pid)
                    await flash("Party deleted.", "success")
                return redirect(url_for("rpg_admin", tab="parties"))

            elif action == "save_settings":
                await db.set_setting(
                    "rpg_channel_id",
                    (form.get("rpg_channel_id") or "").strip(),
                )
                await db.set_setting(
                    "rpg_enabled",
                    "true" if form.get("rpg_enabled") else "false",
                )
                await db.set_setting(
                    "rpg_block_during_stream",
                    "true" if form.get("rpg_block_during_stream") else "false",
                )
                await flash("RPG settings saved.", "success")
                return redirect(url_for("rpg_admin", tab="settings"))

            return redirect(url_for("rpg_admin", tab=tab))

        # GET
        adventures = await db.rpg_list_adventures()
        selected_adv = None
        scenes = []
        if adventure_id_arg:
            selected_adv = await db.rpg_get_adventure(adventure_id_arg)
            if selected_adv:
                scenes = await db.rpg_list_scenes(adventure_id_arg)
        elif adventures and tab == "scenes":
            selected_adv = adventures[0]
            scenes = await db.rpg_list_scenes(selected_adv["id"])

        classes_ = await db.rpg_list_classes()
        enemies = await db.rpg_list_enemies()
        items = await db.rpg_list_items()
        characters = await db.rpg_list_characters()
        parties = await db.rpg_list_parties()

        guild = None
        try:
            if app.bot and getattr(app.bot, "guilds", None):
                guild = next(iter(app.bot.guilds), None)
        except Exception:
            guild = None
        text_channels = []
        if guild is not None:
            for ch in sorted(guild.text_channels, key=lambda c: c.position):
                text_channels.append({"id": ch.id, "name": ch.name})

        rpg_channel_id = await db.get_setting("rpg_channel_id") or ""
        rpg_enabled = (await db.get_setting("rpg_enabled")) != "false"
        rpg_block_during_stream = (
            (await db.get_setting("rpg_block_during_stream")) != "false"
        )
        stream_is_live = False
        try:
            from bot.exp_stream_manager import stream_is_live as _live
            stream_is_live = bool(_live)
        except Exception:
            stream_is_live = False

        return await render_template(
            "rpg.html",
            tab=tab,
            adventures=adventures,
            selected_adv=selected_adv,
            scenes=scenes,
            classes=classes_,
            enemies=enemies,
            items=items,
            characters=characters,
            parties=parties,
            text_channels=text_channels,
            rpg_channel_id=rpg_channel_id,
            rpg_enabled=rpg_enabled,
            rpg_block_during_stream=rpg_block_during_stream,
            stream_is_live=stream_is_live,
        )

    return app
