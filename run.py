import asyncio
import bcrypt
import json
import os
from config import Config
from bot.database import Database
from bot.main import SlowmodeBot
from web.app import create_app


async def init_admin(db: Database):
    """Create the initial admin user if no users exist."""
    users = await db.get_all_web_users()
    if not users:
        pw_hash = bcrypt.hashpw(
            Config.ADMIN_PASSWORD.encode(), bcrypt.gensalt()
        ).decode()
        await db.create_web_user(Config.ADMIN_USERNAME, pw_hash, is_admin=1)
        print(f"Initial admin user '{Config.ADMIN_USERNAME}' created.")


async def record_completed_database_restore(db: Database):
    """Record a completed restore in the database that was just activated."""
    data_dir = os.path.dirname(os.path.abspath(db.db_path))
    marker_path = os.path.join(data_dir, "database-restore-pending.json")
    if not os.path.isfile(marker_path):
        return

    try:
        with open(marker_path, "r", encoding="utf-8") as handle:
            marker = json.load(handle)
        await db.add_audit_log(
            event_type="database_restored",
            details=(
                "Database restored successfully; pre-restore safety backup: "
                f"{marker.get('safety_backup', 'unknown')}"
            ),
            actor=marker.get("actor", "unknown"),
        )
        print("[backup] Database restore completed successfully.", flush=True)
    except Exception as exc:
        print(f"[backup] Could not record completed database restore: {exc}", flush=True)
    finally:
        try:
            os.remove(marker_path)
        except OSError:
            pass


async def main():
    db = Database(Config.DATABASE_PATH)
    await db.connect()
    await init_admin(db)
    await record_completed_database_restore(db)

    # Store guild_id setting if not yet set
    if Config.GUILD_ID:
        existing = await db.get_setting("guild_id")
        if not existing:
            await db.set_setting("guild_id", str(Config.GUILD_ID))

    bot = SlowmodeBot(db)
    app = create_app(db, bot)
    app.secret_key = Config.SECRET_KEY

    from hypercorn.asyncio import serve
    from hypercorn.config import Config as HyperConfig

    hyper_cfg = HyperConfig()
    hyper_cfg.bind = [f"{Config.WEB_HOST}:{Config.WEB_PORT}"]
    hyper_cfg.accesslog = "-"
    hyper_cfg.keep_alive_timeout = 75
    # Trust X-Forwarded-Proto / X-Forwarded-For from the Caddy reverse proxy
    # so that request.url is correctly built as https:// inside the app.
    hyper_cfg.forwarded_allow_ips = "*"

    async with asyncio.TaskGroup() as tg:
        tg.create_task(bot.start(Config.DISCORD_TOKEN))
        tg.create_task(serve(app, hyper_cfg))


if __name__ == "__main__":
    asyncio.run(main())
