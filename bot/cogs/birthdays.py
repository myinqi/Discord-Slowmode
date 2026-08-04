import asyncio
import calendar
from datetime import date
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks


BERLIN_TZ = ZoneInfo("Europe/Berlin")


def birthday_occurrence(day: int, month: int, year: int) -> date:
    if month == 2 and day == 29 and not calendar.isleap(year):
        return date(year, 2, 28)
    return date(year, month, day)


def next_birthday(day: int, month: int, today: date) -> date:
    occurrence = birthday_occurrence(day, month, today.year)
    if occurrence < today:
        occurrence = birthday_occurrence(day, month, today.year + 1)
    return occurrence


class BirthdayCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._notification_lock = asyncio.Lock()
        self.birthday_notifications.start()

    def cog_unload(self):
        self.birthday_notifications.cancel()

    @app_commands.command(
        name="birthday-set",
        description="Save or update your birthday",
    )
    @app_commands.describe(
        day="Day of the month",
        month="Month number (1-12)",
        year="Birth year (optional)",
    )
    async def birthday_set(
        self,
        interaction: discord.Interaction,
        day: app_commands.Range[int, 1, 31],
        month: app_commands.Range[int, 1, 12],
        year: app_commands.Range[int, 1900, 2100] | None = None,
    ):
        current_year = discord.utils.utcnow().astimezone(BERLIN_TZ).year
        if year is not None and year > current_year:
            await interaction.response.send_message(
                "The birth year cannot be in the future.", ephemeral=True
            )
            return
        try:
            date(year or 2000, month, day)
        except ValueError:
            await interaction.response.send_message(
                "That is not a valid calendar date.", ephemeral=True
            )
            return

        await self.bot.db.save_birthday(
            user_id=interaction.user.id,
            user_name=interaction.user.name,
            display_name=interaction.user.display_name,
            birth_day=day,
            birth_month=month,
            birth_year=year,
        )
        year_text = f"{year:04d}" if year is not None else "year hidden"
        await interaction.response.send_message(
            f"Your birthday was saved as **{day:02d}.{month:02d}.** ({year_text}).",
            ephemeral=True,
        )

    @app_commands.command(
        name="birthday-remove",
        description="Remove your saved birthday",
    )
    async def birthday_remove(self, interaction: discord.Interaction):
        removed = await self.bot.db.delete_birthday(interaction.user.id)
        message = (
            "Your birthday was removed."
            if removed
            else "You do not have a saved birthday."
        )
        await interaction.response.send_message(message, ephemeral=True)

    @tasks.loop(minutes=30)
    async def birthday_notifications(self):
        async with self._notification_lock:
            channel_id = await self.bot.db.get_setting("birthday_notification_channel_id")
            if not channel_id or not channel_id.isdigit():
                return

            channel = self.bot.get_channel(int(channel_id))
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(int(channel_id))
                except (discord.HTTPException, discord.NotFound, discord.Forbidden):
                    return
            if not hasattr(channel, "send"):
                return

            today = discord.utils.utcnow().astimezone(BERLIN_TZ).date()
            for birthday in await self.bot.db.get_birthdays():
                occurrence = next_birthday(
                    int(birthday["birth_day"]),
                    int(birthday["birth_month"]),
                    today,
                )
                days_until = (occurrence - today).days
                if days_until == 2:
                    notice_type = "two_days"
                    message = (
                        f"🎂 Birthday reminder: <@{birthday['user_id']}>'s birthday "
                        "is in two days!"
                    )
                elif days_until == 0:
                    notice_type = "birthday"
                    message = f"🎉 Happy birthday, <@{birthday['user_id']}>! Have a wonderful day!"
                else:
                    continue

                occurrence_date = occurrence.isoformat()
                already_sent = await self.bot.db.has_birthday_notification(
                    user_id=birthday["user_id"],
                    occurrence_date=occurrence_date,
                    notice_type=notice_type,
                )
                if already_sent:
                    continue
                try:
                    await channel.send(
                        message,
                        allowed_mentions=discord.AllowedMentions(users=True),
                    )
                except (discord.HTTPException, discord.Forbidden):
                    continue
                await self.bot.db.mark_birthday_notification(
                    user_id=birthday["user_id"],
                    occurrence_date=occurrence_date,
                    notice_type=notice_type,
                )

    @birthday_notifications.before_loop
    async def before_birthday_notifications(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(BirthdayCog(bot))
