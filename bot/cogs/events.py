import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands


BERLIN_TZ = ZoneInfo("Europe/Berlin")


class EventRegistrationCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.event_image_dir = os.path.join(
            os.path.dirname(bot.db.db_path), "event_images"
        )

    @app_commands.command(
        name="join-event",
        description="Join one of the available community events",
    )
    @app_commands.describe(event="The event you want to join")
    async def join_event(self, interaction: discord.Interaction, event: str):
        if not interaction.guild:
            await interaction.response.send_message(
                "This command is only available on the server.", ephemeral=True
            )
            return
        if not event.isdigit():
            await interaction.response.send_message(
                "Select an event from the available choices.", ephemeral=True
            )
            return

        event_record = await self.bot.db.get_community_event(int(event))
        if not event_record or not event_record.get("active"):
            await interaction.response.send_message(
                "This event is no longer available.", ephemeral=True
            )
            return

        joined = await self.bot.db.join_community_event(
            event_id=event_record["id"],
            user_id=interaction.user.id,
            user_name=interaction.user.name,
            display_name=interaction.user.display_name,
        )
        await self.bot.db.add_audit_log(
            event_type="community_event_joined" if joined else "community_event_rejoined",
            user_id=interaction.user.id,
            user_name=interaction.user.display_name,
            details=f"Event #{event_record['id']}: {event_record['name']}",
            actor=interaction.user.name,
        )

        event_time = datetime.fromtimestamp(
            float(event_record["event_at"]), tz=timezone.utc
        )
        embed = discord.Embed(
            title=event_record["name"],
            description=event_record.get("description") or "No description provided.",
            color=discord.Color.green(),
        )
        embed.add_field(
            name="Date",
            value=discord.utils.format_dt(event_time, style="F"),
            inline=False,
        )
        embed.add_field(
            name="Status",
            value="✅ Joined" if joined else "✅ You have already joined this event.",
            inline=False,
        )
        embed.set_footer(text="This confirmation is only visible to you.")

        image_filename = os.path.basename(event_record.get("image_filename") or "")
        image_path = os.path.join(self.event_image_dir, image_filename)
        if image_filename and os.path.isfile(image_path):
            attachment = discord.File(image_path, filename=image_filename)
            embed.set_image(url=f"attachment://{image_filename}")
            await interaction.response.send_message(
                embed=embed, file=attachment, ephemeral=True
            )
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

    @join_event.autocomplete("event")
    async def event_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        events = await self.bot.db.get_community_events(include_inactive=False)
        search = current.strip().casefold()
        choices = []
        for event in events:
            event_time = datetime.fromtimestamp(
                float(event["event_at"]), tz=timezone.utc
            ).astimezone(BERLIN_TZ)
            label = f"{event['name']} · {event_time.strftime('%d.%m.%Y %H:%M')}"
            if search and search not in label.casefold():
                continue
            choices.append(
                app_commands.Choice(name=label[:100], value=str(event["id"]))
            )
            if len(choices) >= 25:
                break
        return choices


async def setup(bot):
    await bot.add_cog(EventRegistrationCog(bot))
