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
    app.db = db
    app.bot = bot
    app.scan_status = {"running": False, "progress": "", "result": ""}
    app.title_scan_status = {"running": False, "progress": "", "result": ""}
    app.reaction_scan_status = {"running": False, "progress": "", "result": ""}

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
    @login_required
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

            if bot_name:
                await db.set_setting("bot_name", bot_name)
            if guild_id:
                await db.set_setting("guild_id", guild_id)
            await db.set_setting("new_command_channel", new_channel)
            await db.set_setting("party_max_songs", party_max_songs)
            await db.set_setting("party_voice_channel", party_voice_channel)

            await db.add_audit_log(
                event_type="settings_changed",
                details=f"Bot name: {bot_name}, Guild ID: {guild_id}, /new channel: {new_channel}, party_max_songs: {party_max_songs}, party_voice_channel: {party_voice_channel}",
                actor=session.get("username", "unknown"),
            )
            await flash("Settings saved.", "success")
            return redirect(url_for("settings"))

        bot_name = await db.get_setting("bot_name") or "Slowmode Bot"
        guild_id = await db.get_setting("guild_id") or ""
        new_command_channel = await db.get_setting("new_command_channel") or ""
        party_max_songs = await db.get_setting("party_max_songs") or "2"
        party_voice_channel = await db.get_setting("party_voice_channel") or ""
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
                                     all_text_channels=all_text_channels,
                                     monitored_channels=monitored,
                                     available_output_channels=available_output_channels,
                                     lp_configs=lp_configs)

    @app.route("/channels", methods=["GET", "POST"])
    @login_required
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
    @login_required
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
    @login_required
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

            return redirect(url_for("users"))

        user_list = await db.get_all_web_users()
        return await render_template("users.html", users=user_list, current_user_id=session.get("user_id"))

    @app.route("/audit")
    @login_required
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

    @app.route("/listening-party", methods=["GET", "POST"])
    @login_required
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
    @login_required
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

    @app.route("/song-stats", methods=["GET", "POST"])
    @login_required
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
            )
        except Exception as e:
            traceback.print_exc()
            return f"<pre>Error: {e}\n\n{traceback.format_exc()}</pre>", 500

    @app.route("/user-stats", methods=["GET", "POST"])
    @login_required
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
    @login_required
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
    @login_required
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
    @login_required
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
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return None, None, None
                    html = await resp.text()
                    # Extract og:image
                    image_url = None
                    img_match = re.search(r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', html)
                    if not img_match:
                        img_match = re.search(r'<meta\s+content=["\']([^"\']+)["\']\s+property=["\']og:image["\']', html)
                    if img_match:
                        image_url = img_match.group(1).strip()
                    # <title> format: "Song Title by Artist Name | Suno"
                    match = re.search(r'<title>([^<]+)</title>', html)
                    if match:
                        raw = match.group(1).strip()
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
    @admin_required
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
                chat_token = form.get("twitch_chat_token", "").strip()
                chat_channel = form.get("twitch_chat_channel", "").strip()

                # Only update key if not masked placeholder
                if twitch_key and not twitch_key.startswith("****"):
                    await db.set_setting("radio_twitch_key", twitch_key)
                if stream_url:
                    await db.set_setting("radio_stream_url", stream_url)
                await db.set_setting("radio_upload_enabled", upload_enabled)
                await db.set_setting("radio_shuffle", shuffle)
                await db.set_setting("radio_post_channel_1_id", post_ch1)
                await db.set_setting("radio_post_channel_2_id", post_ch2)
                if chat_token and not chat_token.startswith("****"):
                    await db.set_setting("radio_twitch_chat_token", chat_token)
                if chat_channel:
                    await db.set_setting("radio_twitch_chat_channel", chat_channel)

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
                        embed = discord.Embed(
                            title="\U0001F4FA Live Stream",
                            description=f"Watch the stream now!\n\n**[Tune in]({stream_url})**",
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
        chat_token = await db.get_setting("radio_twitch_chat_token") or ""
        masked_chat_token = f"****{chat_token[-4:]}" if len(chat_token) > 4 else ""
        chat_channel = await db.get_setting("radio_twitch_chat_channel") or ""

        return await render_template(
            "radio.html",
            songs=songs, masked_key=masked_key, stream_url=stream_url,
            upload_enabled=upload_enabled, bg_filename=bg_filename, bg_type=bg_type,
            text_channels=text_channels,
            post_channel_1_id=post_channel_1_id,
            post_channel_2_id=post_channel_2_id,
            shuffle=shuffle,
            masked_chat_token=masked_chat_token,
            chat_channel=chat_channel,
        )

    @app.route("/radio/files/<filename>")
    async def radio_file(filename):
        from quart import send_from_directory
        return await send_from_directory(RADIO_UPLOAD_DIR, filename)

    @app.route("/radio/export-rights")
    @admin_required
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
    @admin_required
    async def radio_stream_status():
        from quart import jsonify
        return jsonify(await stream_manager.get_status())

    @app.route("/radio/stream/<action>", methods=["POST"])
    @admin_required
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
        else:
            result = {"error": "Unknown action."}
        return jsonify(result)

    # --- Auto-cleanup task ---

    async def _radio_cleanup_loop():
        """Periodically delete expired radio songs (every hour)."""
        while True:
            try:
                await asyncio.sleep(3600)
                filenames = await db.cleanup_expired_radio_songs()
                for fn in filenames:
                    filepath = os.path.join(RADIO_UPLOAD_DIR, fn)
                    if os.path.exists(filepath):
                        os.remove(filepath)
                if filenames:
                    print(f"[radio] Cleaned up {len(filenames)} expired songs.")
            except Exception as e:
                print(f"[radio] Cleanup error: {e}")

    @app.before_serving
    async def start_cleanup_task():
        app.radio_cleanup_task = asyncio.create_task(_radio_cleanup_loop())

    # --- Party Playlist ---

    @app.route("/party-playlist", methods=["GET", "POST"])
    @login_required
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

    return app
