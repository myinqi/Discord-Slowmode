import discord
from discord.ext import commands
from bot.database import Database
from config import Config


class SlowmodeBot(commands.Bot):
    def __init__(self, db: Database):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.guilds = True

        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None,
        )
        self.db = db
        self.config = Config

    async def setup_hook(self):
        await self.load_extension("bot.cogs.slowmode")
        await self.load_extension("bot.cogs.commands")

        if self.config.GUILD_ID:
            guild = discord.Object(id=self.config.GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)

    async def on_ready(self):
        print(f"Bot is ready as {self.user} (ID: {self.user.id})")
        print(f"Connected to {len(self.guilds)} guild(s)")

        bot_name = await self.db.get_setting("bot_name")
        if not bot_name:
            await self.db.set_setting("bot_name", self.config.BOT_NAME)
