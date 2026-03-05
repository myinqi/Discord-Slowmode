import re
import random
from datetime import datetime, timedelta, timezone
import discord
from discord import app_commands
from discord.ext import commands

SUNO_URL_PATTERN = re.compile(r'https://suno\.com/(?:s|song)/[\w-]+')
SUNO_PLAYLIST_PATTERN = re.compile(r'https://suno\.com/playlist/[\w-]+')


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

    @app_commands.command(name="random-song", description="Pick a random Suno song from a listening party input channel")
    @app_commands.describe(input_channel="The input channel to scan (must have a listening party config)")
    async def random_song(
        self, interaction: discord.Interaction, input_channel: discord.TextChannel = None
    ):
        await interaction.response.defer(ephemeral=False)

        configs = await self.bot.db.get_listening_party_configs()
        if not configs:
            await interaction.followup.send("No listening party configs found. Set one up in the web interface.", ephemeral=True)
            return

        if input_channel:
            config = None
            for c in configs:
                if c["input_channel_id"] == input_channel.id:
                    config = c
                    break
            if not config:
                await interaction.followup.send(
                    f"#{input_channel.name} is not configured as a listening party input channel.", ephemeral=True
                )
                return
        else:
            config = configs[0]

        source_channel = interaction.guild.get_channel(config["input_channel_id"])
        output_channel = interaction.guild.get_channel(config["output_channel_id"])

        if not source_channel:
            await interaction.followup.send("Input channel not found.", ephemeral=True)
            return
        if not output_channel:
            await interaction.followup.send("Output channel not found.", ephemeral=True)
            return

        time_range_hours = config["time_range_hours"]
        after_time = datetime.now(timezone.utc) - timedelta(hours=time_range_hours)

        suno_urls = []
        async for message in source_channel.history(after=after_time, limit=5000):
            if message.author.bot:
                continue
            urls = SUNO_URL_PATTERN.findall(message.content)
            for url in urls:
                suno_urls.append({
                    "url": url,
                    "author": str(message.author),
                    "author_id": message.author.id,
                    "posted_at": message.created_at,
                })

        if not suno_urls:
            await interaction.followup.send(
                f"No Suno songs found in #{source_channel.name} within the last {time_range_hours}h.", ephemeral=True
            )
            return

        pick = random.choice(suno_urls)
        bot_name = await self.bot.db.get_setting("bot_name") or "Slowmode Bot"

        embed = discord.Embed(
            title="🎵 Random Song Pick",
            description=f"From #{source_channel.name} (last {time_range_hours}h)",
            color=discord.Color.purple(),
        )
        embed.add_field(name="Posted by", value=f"<@{pick['author_id']}>", inline=True)
        embed.add_field(name="Originally posted", value=discord.utils.format_dt(pick["posted_at"], style="R"), inline=True)
        embed.set_footer(text=f"{bot_name} • {len(suno_urls)} songs scanned")
        embed.timestamp = discord.utils.utcnow()

        await output_channel.send(embed=embed)
        await output_channel.send(pick["url"])
        await interaction.followup.send(
            f"Random song posted to #{output_channel.name}! ({len(suno_urls)} songs found)", ephemeral=True
        )

        await self.bot.db.add_audit_log(
            event_type="random_song",
            user_id=interaction.user.id,
            user_name=str(interaction.user),
            channel_id=output_channel.id,
            channel_name=output_channel.name,
            details=f"Random song picked from #{source_channel.name}: {pick['url']}",
            actor=str(interaction.user),
        )


    @app_commands.command(name="find-list", description="Search for Suno playlists by artist, @user, or keyword")
    @app_commands.describe(search="Artist name, @user mention, or keyword to search for")
    async def find_list(self, interaction: discord.Interaction, search: str):
        await interaction.response.defer(ephemeral=True)

        configs = await self.bot.db.get_playlist_search_channels()
        if not configs:
            await interaction.followup.send(
                "No playlist search channels configured. Ask an admin to set one up.", ephemeral=True
            )
            return

        # Check if search is a user mention like <@123456> or <@!123456>
        mention_match = re.match(r'<@!?(\d+)>', search)
        search_user_id = int(mention_match.group(1)) if mention_match else None
        search_lower = search.lower()

        results = []

        for cfg in configs:
            channel = interaction.guild.get_channel(cfg["channel_id"])
            if not channel:
                continue

            async for message in channel.history(limit=10000):
                if message.author.bot:
                    continue

                urls = SUNO_PLAYLIST_PATTERN.findall(message.content)
                if not urls:
                    continue

                # Match by: user mention, author name/display name, or message content
                matched = False
                if search_user_id and message.author.id == search_user_id:
                    matched = True
                elif search_lower in message.author.name.lower():
                    matched = True
                elif search_lower in message.author.display_name.lower():
                    matched = True
                elif search_lower in message.content.lower():
                    matched = True

                if not matched:
                    continue

                for url in urls:
                    results.append({
                        "url": url,
                        "author": str(message.author),
                        "author_id": message.author.id,
                        "posted_at": message.created_at,
                        "context": message.content[:150],
                        "channel_name": channel.name,
                    })

        if not results:
            await interaction.followup.send(
                f"No playlists found for **{search}**.", ephemeral=True
            )
            return

        # Deduplicate by URL
        seen = set()
        unique = []
        for r in results:
            if r["url"] not in seen:
                seen.add(r["url"])
                unique.append(r)
        results = unique

        # Build response (Discord has a 2000 char limit, split if needed)
        header = f"🔍 **{len(results)} playlist(s) found for \"{search}\":**\n\n"
        entries = []
        for i, r in enumerate(results, 1):
            entry = (
                f"**{i}.** {r['url']}\n"
                f"   Posted by <@{r['author_id']}> in #{r['channel_name']} "
                f"({discord.utils.format_dt(r['posted_at'], style='R')})"
            )
            entries.append(entry)

        # Split into chunks that fit Discord's 2000 char limit
        chunks = []
        current = header
        for entry in entries:
            if len(current) + len(entry) + 2 > 1900:
                chunks.append(current)
                current = ""
            current += entry + "\n\n"
        if current.strip():
            chunks.append(current)

        for chunk in chunks:
            await interaction.followup.send(chunk, ephemeral=True)

        await self.bot.db.add_audit_log(
            event_type="playlist_search",
            user_id=interaction.user.id,
            user_name=str(interaction.user),
            details=f"Searched for '{search}', found {len(results)} result(s)",
            actor=str(interaction.user),
        )


async def setup(bot):
    await bot.add_cog(CommandsCog(bot))
