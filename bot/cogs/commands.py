import discord
from discord import app_commands
from discord.ext import commands


class CommandsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _has_command_permission(self, interaction: discord.Interaction) -> bool:
        member = interaction.user

        if member.id == interaction.guild.owner_id:
            return True

        if member.guild_permissions.administrator:
            return True

        command_roles = await self.bot.db.get_command_roles()
        command_role_ids = {r["role_id"] for r in command_roles}

        for role in member.roles:
            if role.id in command_role_ids:
                return True

        return False

    async def _permission_check(self, interaction: discord.Interaction) -> bool:
        if not await self._has_command_permission(interaction):
            await interaction.response.send_message(
                "You do not have permission to use this command.", ephemeral=True
            )
            return False
        return True

    @app_commands.command(name="cooldown-set", description="Set cooldown for a channel in minutes")
    @app_commands.describe(channel="The channel to configure", minutes="Cooldown in minutes (0 = disabled)")
    async def cooldown_set(
        self, interaction: discord.Interaction, channel: discord.TextChannel, minutes: int
    ):
        if not await self._permission_check(interaction):
            return

        if minutes < 0 or minutes > 2880:
            await interaction.response.send_message(
                "Cooldown must be between 0 and 2880 minutes (48 hours).", ephemeral=True
            )
            return

        await self.bot.db.add_monitored_channel(channel.id, channel.name, minutes)
        await self.bot.db.add_audit_log(
            event_type="channel_config",
            channel_id=channel.id,
            channel_name=channel.name,
            details=f"Cooldown set to {minutes}min via slash command",
            actor=str(interaction.user),
        )

        if minutes == 0:
            await interaction.response.send_message(
                f"#{channel.name} is now monitored with **no cooldown**.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"#{channel.name} cooldown set to **{minutes} minute(s)**.", ephemeral=True
            )

    @app_commands.command(name="cooldown-info", description="Show cooldown info for a channel")
    @app_commands.describe(channel="The channel to check")
    async def cooldown_info(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ):
        if not await self._permission_check(interaction):
            return

        config = await self.bot.db.get_monitored_channel(channel.id)
        if not config:
            await interaction.response.send_message(
                f"#{channel.name} is not monitored.", ephemeral=True
            )
            return

        status = "enabled" if config["enabled"] else "disabled"
        minutes = config["cooldown_minutes"]
        cooldown_str = f"{minutes}min" if minutes > 0 else "none"

        embed = discord.Embed(
            title=f"Channel Config: #{channel.name}",
            color=discord.Color.blue(),
        )
        embed.add_field(name="Status", value=status, inline=True)
        embed.add_field(name="Cooldown", value=cooldown_str, inline=True)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="cooldown-reset", description="Reset cooldown for a user in a channel")
    @app_commands.describe(user="The user to reset", channel="The channel (optional, all if omitted)")
    async def cooldown_reset(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        channel: discord.TextChannel = None,
    ):
        if not await self._permission_check(interaction):
            return

        if channel:
            await self.bot.db.clear_cooldown_record(user.id, channel.id)
            msg = f"Cooldown for {user.mention} in #{channel.name} has been reset."
            detail = f"Cooldown reset for user {user} in #{channel.name}"
        else:
            monitored = await self.bot.db.get_monitored_channels()
            for ch in monitored:
                await self.bot.db.clear_cooldown_record(user.id, ch["channel_id"])
            msg = f"All cooldowns for {user.mention} have been reset."
            detail = f"All cooldowns reset for user {user}"

        await self.bot.db.add_audit_log(
            event_type="cooldown_reset",
            user_id=user.id,
            user_name=str(user),
            channel_id=channel.id if channel else None,
            channel_name=channel.name if channel else None,
            details=detail,
            actor=str(interaction.user),
        )

        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="cooldown-clear", description="Clear all cooldowns for a channel")
    @app_commands.describe(channel="The channel to clear (omit for all channels)")
    async def cooldown_clear(
        self, interaction: discord.Interaction, channel: discord.TextChannel = None
    ):
        if not await self._permission_check(interaction):
            return

        if channel:
            await self.bot.db.clear_all_cooldowns(channel.id)
            msg = f"All cooldowns in #{channel.name} have been cleared."
        else:
            await self.bot.db.clear_all_cooldowns()
            msg = "All cooldowns across all channels have been cleared."

        await self.bot.db.add_audit_log(
            event_type="cooldown_clear",
            channel_id=channel.id if channel else None,
            channel_name=channel.name if channel else None,
            details=msg,
            actor=str(interaction.user),
        )

        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="cooldown-toggle", description="Enable or disable monitoring for a channel")
    @app_commands.describe(channel="The channel to toggle", enabled="Enable or disable")
    async def cooldown_toggle(
        self, interaction: discord.Interaction, channel: discord.TextChannel, enabled: bool
    ):
        if not await self._permission_check(interaction):
            return

        config = await self.bot.db.get_monitored_channel(channel.id)
        if not config:
            await interaction.response.send_message(
                f"#{channel.name} is not monitored. Add it first.", ephemeral=True
            )
            return

        await self.bot.db.toggle_channel(channel.id, enabled)
        state = "enabled" if enabled else "disabled"
        await self.bot.db.add_audit_log(
            event_type="channel_toggle",
            channel_id=channel.id,
            channel_name=channel.name,
            details=f"Monitoring {state} via slash command",
            actor=str(interaction.user),
        )

        await interaction.response.send_message(
            f"Monitoring for #{channel.name} is now **{state}**.", ephemeral=True
        )


async def setup(bot):
    await bot.add_cog(CommandsCog(bot))
