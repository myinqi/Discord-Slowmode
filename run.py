import asyncio
import bcrypt
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


async def main():
    db = Database(Config.DATABASE_PATH)
    await db.connect()
    await init_admin(db)

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

    async with asyncio.TaskGroup() as tg:
        tg.create_task(bot.start(Config.DISCORD_TOKEN))
        tg.create_task(serve(app, hyper_cfg))


if __name__ == "__main__":
    asyncio.run(main())
