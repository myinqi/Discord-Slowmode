import time
import math
import discord
from discord.ext import commands


class SlowmodeCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        if not message.guild:
            return

        db = self.bot.db
        channel_config = await db.get_monitored_channel(message.channel.id)

        if not channel_config:
            return

        if not channel_config["enabled"]:
            return

        cooldown_minutes = channel_config["cooldown_minutes"]
        if cooldown_minutes <= 0:
            return

        if await self._is_exempt(message.author):
            return

        record = await db.get_cooldown_record(message.author.id, message.channel.id)

        if record:
            elapsed = time.time() - record["timestamp"]
            cooldown_seconds = cooldown_minutes * 60
            remaining = cooldown_seconds - elapsed

            if remaining > 0:
                await self._enforce_cooldown(message, remaining)
                return

        await db.set_cooldown_record(message.author.id, message.channel.id)

    async def _is_exempt(self, member: discord.Member) -> bool:
        if member.guild_permissions.administrator:
            return True

        if member.id == member.guild.owner_id:
            return True

        exempt_roles = await self.bot.db.get_exempt_roles()
        exempt_role_ids = {r["role_id"] for r in exempt_roles}

        for role in member.roles:
            if role.id in exempt_role_ids:
                return True

        return False

    async def _enforce_cooldown(self, message: discord.Message, remaining_seconds: float):
        try:
            await message.delete()
        except discord.Forbidden:
            print(f"Missing permissions to delete message in #{message.channel.name}")
            return
        except discord.NotFound:
            return

        hours = remaining_seconds / 3600
        if hours >= 1:
            hours_int = math.ceil(hours)
            time_str = f"{hours_int} hour{'s' if hours_int != 1 else ''}"
        else:
            minutes = math.ceil(remaining_seconds / 60)
            time_str = f"{minutes} minute{'s' if minutes != 1 else ''}"

        bot_name = await self.bot.db.get_setting("bot_name") or "Slowmode Bot"

        embed = discord.Embed(
            title="Message Removed — Cooldown Active",
            description=(
                f"Your message in **#{message.channel.name}** was removed because "
                f"you are still within the posting cooldown period.\n\n"
                f"**Time remaining:** {time_str}\n\n"
                f"Please wait before posting again in that channel."
            ),
            color=discord.Color.orange(),
        )
        embed.set_footer(text=bot_name)
        embed.timestamp = discord.utils.utcnow()

        try:
            await message.author.send(embed=embed)
        except discord.Forbidden:
            pass

        await self.bot.db.add_audit_log(
            event_type="message_deleted",
            user_id=message.author.id,
            user_name=str(message.author),
            channel_id=message.channel.id,
            channel_name=message.channel.name,
            details=f"Cooldown active. {time_str} remaining.",
            actor="bot",
        )


async def setup(bot):
    await bot.add_cog(SlowmodeCog(bot))
