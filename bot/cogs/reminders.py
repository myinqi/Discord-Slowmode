import asyncio

import discord
from discord import app_commands
from discord.ext import commands, tasks

from bot.reminder_schedule import (
    BERLIN_TZ,
    RECURRENCE_LABELS,
    ensure_future_recurrence,
    next_recurrence_datetime,
    parse_reminder_datetime,
    reminder_datetime_from_timestamp,
)


class RemindersCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._dispatch_lock = asyncio.Lock()
        self.reminder_dispatch.start()

    def cog_unload(self):
        self.reminder_dispatch.cancel()

    @app_commands.command(
        name="reminder-set",
        description="Create a one-time or recurring personal reminder",
    )
    @app_commands.describe(
        text="The reminder message sent to you by DM",
        date="First date in DD.MM.YYYY or YYYY-MM-DD format",
        time="Time in HH:MM format (Europe/Berlin)",
        repeat="How often the reminder repeats",
    )
    @app_commands.choices(repeat=[
        app_commands.Choice(name="Once", value="once"),
        app_commands.Choice(name="Every day", value="daily"),
        app_commands.Choice(name="Every week", value="weekly"),
        app_commands.Choice(name="Every month", value="monthly"),
    ])
    async def reminder_set(
        self,
        interaction: discord.Interaction,
        text: str,
        date: str,
        time: str,
        repeat: str = "once",
    ):
        text = text.strip()
        if not text or len(text) > 1000:
            await interaction.response.send_message(
                "Reminder text must contain between 1 and 1000 characters.",
                ephemeral=True,
            )
            return
        if repeat not in RECURRENCE_LABELS:
            repeat = "once"
        try:
            scheduled = parse_reminder_datetime(date, time)
            now = discord.utils.utcnow().astimezone(BERLIN_TZ)
            next_run = ensure_future_recurrence(
                scheduled,
                repeat,
                now=now,
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        reminder_id = await self.bot.db.create_reminder(
            user_id=interaction.user.id,
            user_name=interaction.user.name,
            display_name=interaction.user.display_name,
            reminder_text=text,
            next_run_at=next_run.timestamp(),
            recurrence=repeat,
            anchor_day=scheduled.day,
        )
        await self.bot.db.add_audit_log(
            event_type="reminder_created",
            user_id=interaction.user.id,
            user_name=interaction.user.display_name,
            details=f"Reminder #{reminder_id} · {repeat} · {next_run.isoformat()}",
            actor=interaction.user.name,
        )
        await interaction.response.send_message(
            (
                f"Reminder **#{reminder_id}** saved for "
                f"**{next_run.strftime('%d %B %Y at %H:%M')}** "
                f"(Europe/Berlin) · {RECURRENCE_LABELS[repeat]}."
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="reminder-delete",
        description="Delete one of your personal reminders",
    )
    @app_commands.describe(reminder_id="Your reminder")
    async def reminder_delete(
        self,
        interaction: discord.Interaction,
        reminder_id: int,
    ):
        reminder = await self.bot.db.get_reminder(reminder_id)
        if not reminder or int(reminder["user_id"]) != interaction.user.id:
            await interaction.response.send_message(
                "That reminder does not exist or does not belong to you.",
                ephemeral=True,
            )
            return
        deleted = await self.bot.db.delete_reminder(
            reminder_id,
            user_id=interaction.user.id,
        )
        if deleted:
            await self.bot.db.add_audit_log(
                event_type="reminder_deleted",
                user_id=interaction.user.id,
                user_name=interaction.user.display_name,
                details=f"Reminder #{reminder_id} deleted by owner",
                actor=interaction.user.name,
            )
        await interaction.response.send_message(
            f"Reminder **#{reminder_id}** was deleted."
            if deleted else "The reminder could not be deleted.",
            ephemeral=True,
        )

    @reminder_delete.autocomplete("reminder_id")
    async def reminder_delete_autocomplete(
        self,
        interaction: discord.Interaction,
        current: str,
    ) -> list[app_commands.Choice[int]]:
        reminders = await self.bot.db.get_user_reminders(interaction.user.id)
        search = str(current).strip().lower()
        choices = []
        for reminder in reminders:
            scheduled = reminder_datetime_from_timestamp(reminder["next_run_at"])
            text_preview = " ".join(str(reminder["reminder_text"]).split())
            label = (
                f"#{reminder['id']} · {scheduled.strftime('%d.%m.%Y %H:%M')} · "
                f"{text_preview}"
            )
            if search and search not in label.lower():
                continue
            choices.append(
                app_commands.Choice(name=label[:100], value=int(reminder["id"]))
            )
            if len(choices) >= 25:
                break
        return choices

    @tasks.loop(minutes=1)
    async def reminder_dispatch(self):
        async with self._dispatch_lock:
            now_utc = discord.utils.utcnow()
            reminders = await self.bot.db.claim_due_reminders(
                now_timestamp=now_utc.timestamp()
            )
            for reminder in reminders:
                current = await self.bot.db.get_reminder(reminder["id"])
                if (
                    not current
                    or not current["active"]
                    or float(current["next_run_at"]) > now_utc.timestamp()
                ):
                    continue
                reminder = current
                try:
                    user = self.bot.get_user(int(reminder["user_id"]))
                    if user is None:
                        user = await self.bot.fetch_user(int(reminder["user_id"]))
                    embed = discord.Embed(
                        title="⏰ Reminder",
                        description=reminder["reminder_text"],
                        color=0x5865F2,
                    )
                    recurrence_label = RECURRENCE_LABELS.get(
                        reminder["recurrence"], "Once"
                    )
                    embed.set_footer(text=f"Reminder #{reminder['id']} · {recurrence_label}")
                    await user.send(embed=embed)
                except (discord.Forbidden, discord.NotFound, discord.HTTPException) as exc:
                    await self.bot.db.fail_reminder(
                        reminder["id"],
                        attempted_at=now_utc.timestamp(),
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    continue
                except Exception as exc:
                    print(f"[reminders] Delivery failed for #{reminder['id']}: {exc}")
                    await self.bot.db.fail_reminder(
                        reminder["id"],
                        attempted_at=now_utc.timestamp(),
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    continue

                scheduled = reminder_datetime_from_timestamp(reminder["next_run_at"])
                next_run = next_recurrence_datetime(
                    scheduled,
                    reminder["recurrence"],
                    anchor_day=int(reminder["anchor_day"]),
                    after=now_utc.astimezone(BERLIN_TZ),
                )
                await self.bot.db.complete_reminder(
                    reminder["id"],
                    sent_at=now_utc.timestamp(),
                    next_run_at=next_run.timestamp() if next_run else None,
                )

    @reminder_dispatch.before_loop
    async def before_reminder_dispatch(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(RemindersCog(bot))
