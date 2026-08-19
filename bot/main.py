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
        self.trya_stream_dir = Config.TRYA_STREAM_DIR
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
        await self.load_extension("bot.cogs.events")

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

        for guild in self.guilds:
            if self.config.GUILD_ID and guild.id != self.config.GUILD_ID:
                continue
            known_joins = [
                (member.id, member.joined_at.timestamp())
                for member in guild.members
                if member.joined_at is not None
            ]
            try:
                await self.db.backfill_discord_member_joins(guild.id, known_joins)
                print(
                    f"Member history ready for {guild.name}: "
                    f"{len(known_joins)} known join date(s)"
                )
            except Exception as exc:
                print(f"Member history backfill failed for {guild.id}: {exc}")

    async def on_member_join(self, member: discord.Member):
        if self.config.GUILD_ID and member.guild.id != self.config.GUILD_ID:
            return
        occurred_at = (member.joined_at or discord.utils.utcnow()).timestamp()
        try:
            await self.db.record_discord_member_event(
                member.guild.id,
                member.id,
                "join",
                occurred_at,
                user_name=member.name,
                display_name=member.display_name,
            )
        except Exception as exc:
            print(f"Member join history failed for {member.id}: {exc}")

    async def on_member_remove(self, member: discord.Member):
        if self.config.GUILD_ID and member.guild.id != self.config.GUILD_ID:
            return
        try:
            await self.db.record_discord_member_event(
                member.guild.id,
                member.id,
                "leave",
                discord.utils.utcnow().timestamp(),
                user_name=member.name,
                display_name=member.display_name,
            )
        except Exception as exc:
            print(f"Member leave history failed for {member.id}: {exc}")
