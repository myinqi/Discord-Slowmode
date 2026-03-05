import functools
import math
import time
import bcrypt
from quart import Quart, render_template, request, redirect, url_for, session, flash
from bot.database import Database


def create_app(db: Database, bot=None) -> Quart:
    app = Quart(
        __name__,
        template_folder="templates",
        static_folder="static",
    )
    app.db = db
    app.bot = bot

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
            bot_name = form.get("bot_name", "").strip()
            guild_id = form.get("guild_id", "").strip()

            if bot_name:
                await db.set_setting("bot_name", bot_name)
            if guild_id:
                await db.set_setting("guild_id", guild_id)

            await db.add_audit_log(
                event_type="settings_changed",
                details=f"Bot name: {bot_name}, Guild ID: {guild_id}",
                actor=session.get("username", "unknown"),
            )
            await flash("Settings saved.", "success")
            return redirect(url_for("settings"))

        bot_name = await db.get_setting("bot_name") or "Slowmode Bot"
        guild_id = await db.get_setting("guild_id") or ""
        return await render_template("settings.html", bot_name=bot_name, guild_id=guild_id)

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

    return app
