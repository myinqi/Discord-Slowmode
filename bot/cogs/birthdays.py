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


class BirthdayCalendarView(discord.ui.View):
    def __init__(
        self,
        entries: list[dict],
        *,
        viewer_id: int,
        title: str,
    ):
        super().__init__(timeout=600)
        self.entries = entries
        self.viewer_id = viewer_id
        self.title = title
        self.page = 0
        self.page_size = 10
        self.page_count = max(1, (len(entries) + self.page_size - 1) // self.page_size)
        self._sync_buttons()

    def _sync_buttons(self):
        self.previous.disabled = self.page == 0
        self.next.disabled = self.page >= self.page_count - 1

    def build_embed(self) -> discord.Embed:
        start = self.page * self.page_size
        page_entries = self.entries[start:start + self.page_size]
        lines = []
        for birthday in page_entries:
            days_until = birthday["days_until"]
            if days_until == 0:
                distance = "today"
            elif days_until == 1:
                distance = "tomorrow"
            else:
                distance = f"in {days_until} days"
            lines.append(
                f"🎂 **{birthday['next_date'].strftime('%d %B')}** — "
                f"<@{birthday['user_id']}> · {distance}"
            )

        embed = discord.Embed(
            title=f"🎂 {self.title}",
            description="\n".join(lines),
            color=0x9B59B6,
        )
        embed.set_footer(
            text=(
                f"Page {self.page + 1} of {self.page_count} · "
                "Use /birthday-set to join the calendar"
            )
        )
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.viewer_id:
            await interaction.response.send_message(
                "Only the person who opened this calendar can change pages.",
                ephemeral=True,
            )
            return False
        return True

    async def _change_page(self, interaction: discord.Interaction, direction: int):
        self.page = max(0, min(self.page_count - 1, self.page + direction))
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Previous", emoji="◀", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._change_page(interaction, -1)

    @discord.ui.button(label="Next", emoji="▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._change_page(interaction, 1)


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

    @app_commands.command(
        name="birthdays",
        description="Show the server birthday calendar",
    )
    @app_commands.describe(
        month="Show upcoming birthdays, all birthdays, or one month",
    )
    @app_commands.choices(month=[
        app_commands.Choice(name="Upcoming", value="upcoming"),
        app_commands.Choice(name="All", value="all"),
        app_commands.Choice(name="January", value="1"),
        app_commands.Choice(name="February", value="2"),
        app_commands.Choice(name="March", value="3"),
        app_commands.Choice(name="April", value="4"),
        app_commands.Choice(name="May", value="5"),
        app_commands.Choice(name="June", value="6"),
        app_commands.Choice(name="July", value="7"),
        app_commands.Choice(name="August", value="8"),
        app_commands.Choice(name="September", value="9"),
        app_commands.Choice(name="October", value="10"),
        app_commands.Choice(name="November", value="11"),
        app_commands.Choice(name="December", value="12"),
    ])
    async def birthdays(
        self,
        interaction: discord.Interaction,
        month: str = "upcoming",
    ):
        today = discord.utils.utcnow().astimezone(BERLIN_TZ).date()
        entries = await self.bot.db.get_birthdays()
        for birthday in entries:
            occurrence = next_birthday(
                int(birthday["birth_day"]),
                int(birthday["birth_month"]),
                today,
            )
            birthday["next_date"] = occurrence
            birthday["days_until"] = (occurrence - today).days

        if month.isdigit():
            selected_month = int(month)
            entries = [
                birthday for birthday in entries
                if int(birthday["birth_month"]) == selected_month
            ]
            entries.sort(key=lambda birthday: (birthday["birth_day"], birthday["display_name"].lower()))
            title = f"Birthdays in {calendar.month_name[selected_month]}"
        else:
            entries.sort(key=lambda birthday: (birthday["days_until"], birthday["display_name"].lower()))
            if month == "upcoming":
                entries = entries[:10]
                title = "Upcoming Birthdays"
            else:
                title = "Server Birthday Calendar"

        if not entries:
            await interaction.response.send_message(
                "No birthdays were found for this selection.", ephemeral=True
            )
            return

        view = BirthdayCalendarView(
            entries,
            viewer_id=interaction.user.id,
            title=title,
        )
        await interaction.response.send_message(
            embed=view.build_embed(),
            view=view,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

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
                        allowed_mentions=discord.AllowedMentions.none(),
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
