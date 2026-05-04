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


def create_app(db: Database, bot=None) -> Quart:
    app = Quart(
        __name__,
        template_folder="templates",
        static_folder="static",
    )
    # Allow larger uploads (PiP videos, radio backgrounds). Default is 16 MB.
    app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024   # 50 MB
    app.config["BODY_TIMEOUT"]       = 120                # 2 min for slow uplinks
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
        ('radio', 'Twitch Radio'),
        ('suno_analyzer', 'Suno Analyzer'),
        ('suno_promotion', 'Suno Promotion'),
        ('suno_info', 'Suno Info'),
        ('audit', 'Audit Log'),
        ('settings', 'Settings'),
        ('llm', 'Corax Chat (LLM)'),
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
                        matches = re.findall(r'display_name\\":\\"([^"\\]+)\\"', html)
                        for dn in reversed(matches):
                            if len(dn) > 1 and not dn.startswith('v') and dn not in ('Cover', 'Remix'):
                                artist = dn.strip()
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

                poll_id = await db.create_poll(title, description, _json.dumps(options_list), image_filename)
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
        for p in all_polls:
            p["options_list"] = _json.loads(p["options"])
        guild = get_guild()
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

    async def _fetch_suno_info(url: str) -> tuple[str | None, str | None, str | None]:
        """Fetch song title, artist and image from a Suno URL. Returns (title, artist, image_url)."""
        import html as _html
        try:
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
                    # <title> format: "Song Title by Artist Name | Suno"
                    match = re.search(r'<title>([^<]+)</title>', page_html)
                    if match:
                        raw = _html.unescape(match.group(1).strip())
                        raw = re.sub(r'\s*[|\-\u2013]\s*Suno$', '', raw).strip()
                        by_match = re.search(r'^(.+?)\s+by\s+(.+)$', raw)
                        if by_match:
                            return by_match.group(1).strip(), by_match.group(2).strip(), image_url
                        return raw, None, image_url
        except Exception:
            pass
        return None, None, None

    # --- Radio ---

    RADIO_UPLOAD_DIR = os.path.join(os.path.dirname(db.db_path), "radio")
    os.makedirs(RADIO_UPLOAD_DIR, exist_ok=True)

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

    @app.before_serving
    async def start_cleanup_task():
        app.radio_cleanup_task = asyncio.create_task(_radio_cleanup_loop())

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
                if not SUNO_URL_PATTERN.search(url):
                    await flash("Invalid URL. Please provide a valid Suno song link.", "error")
                    return redirect(url_for("party_playlist"))
                song_title, artist, image_url = await _fetch_suno_info(url)
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
        return await render_template("suno_info.html", channels=await _get_player_channels())

    @app.route("/api/suno-info/playlist")
    @permission_required('suno_info')
    async def api_suno_info_playlist():
        """Parse a Suno playlist URL and return its songs + name."""
        import aiohttp as _aiohttp, html as _html
        from quart import jsonify
        from bot.stream_manager import parse_suno_playlist
        url = (request.args.get("url") or "").strip()
        if not url:
            return jsonify({"error": "missing url"}), 400
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

    return app
