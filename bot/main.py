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
        self.exp_radio_dir = Config.EXP_RADIO_DIR
        self.web_url = Config.WEB_URL.rstrip("/")

    async def setup_hook(self):
        await self.load_extension("bot.cogs.slowmode")
        await self.load_extension("bot.cogs.commands")
        await self.load_extension("bot.cogs.llm_chat")
        await self.load_extension("bot.cogs.reaction_roles")
        await self.load_extension("bot.cogs.auto_translate")
        await self.load_extension("bot.cogs.quiz")
        await self.load_extension("bot.cogs.rpg")
        await self.load_extension("bot.cogs.birthdays")
        await self.load_extension("bot.cogs.reminders")

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
