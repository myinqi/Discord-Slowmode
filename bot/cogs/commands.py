import re
import random
from datetime import datetime, timedelta, timezone
import discord
from discord import app_commands
from discord.ext import commands

SUNO_URL_PATTERN = re.compile(r'https://suno\.com/(?:s|song)/[\w-]+')
SUNO_PLAYLIST_PATTERN = re.compile(r'https://suno\.com/playlist/[\w-]+')
SPOTIFY_ALBUM_PATTERN = re.compile(r'https://open\.spotify\.com/album/[\w?=&-]+')


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

        try:
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
        except Exception as e:
            print(f"[random-song] Error: {e}")
            await interaction.followup.send(f"Error: {e}", ephemeral=True)


    @app_commands.command(name="find-list", description="Search for Suno playlists by artist, @user, or keyword")
    @app_commands.describe(search="Artist name, @user mention, or keyword to search for")
    async def find_list(self, interaction: discord.Interaction, search: str):
        await interaction.response.defer(ephemeral=True)

        try:
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
                    urls += SPOTIFY_ALBUM_PATTERN.findall(message.content)
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
        except Exception as e:
            print(f"[find-list] Error: {e}")
            await interaction.followup.send(f"Error: {e}", ephemeral=True)


    @app_commands.command(name="song-stats", description="Show song posting statistics for monitored channels")
    @app_commands.describe(channel="Optional: show stats for a specific channel only")
    async def song_stats(self, interaction: discord.Interaction, channel: discord.TextChannel = None):
        await interaction.response.defer(ephemeral=True)

        try:
            if channel:
                stats = await self.bot.db.get_song_stats(channel_id=channel.id)
                header = f"📈 **Song Stats for #{channel.name}**\n\n"
            else:
                stats = await self.bot.db.get_song_stats()
                header = "📈 **Song Stats (all channels)**\n\n"

            if stats["total"] == 0:
                await interaction.followup.send(
                    f"{header}No song data yet. An admin can run a history scan from the web interface.",
                    ephemeral=True,
                )
                return

            lines = [header, f"**Total:** {stats['total']} songs\n"]

            if stats["by_year"]:
                lines.append("\n**By Year:**")
                for item in stats["by_year"]:
                    lines.append(f"  {item['label']}: **{item['count']}**")

            if stats["by_month"]:
                lines.append("\n**By Month** (last 12):")
                for item in stats["by_month"]:
                    lines.append(f"  {item['label']}: **{item['count']}**")

            if stats["by_week"]:
                lines.append("\n**By Week** (last 12):")
                for item in stats["by_week"]:
                    lines.append(f"  {item['label']}: **{item['count']}**")

            if stats["by_day"]:
                lines.append("\n**By Day** (last 30):")
                for item in stats["by_day"][:10]:
                    lines.append(f"  {item['label']}: **{item['count']}**")
                if len(stats["by_day"]) > 10:
                    lines.append(f"  ... and {len(stats['by_day']) - 10} more days")

            text = "\n".join(lines)

            # Split if needed
            if len(text) <= 2000:
                await interaction.followup.send(text, ephemeral=True)
            else:
                chunks = []
                current = ""
                for line in lines:
                    if len(current) + len(line) + 1 > 1900:
                        chunks.append(current)
                        current = ""
                    current += line + "\n"
                if current.strip():
                    chunks.append(current)
                for chunk in chunks:
                    await interaction.followup.send(chunk, ephemeral=True)

        except Exception as e:
            print(f"[song-stats] Error: {e}")
            await interaction.followup.send(f"Error: {e}", ephemeral=True)


    @app_commands.command(name="user-stats", description="Show song posting statistics for a user")
    @app_commands.describe(user="The user to show stats for")
    async def user_stats(self, interaction: discord.Interaction, user: discord.Member = None):
        await interaction.response.defer(ephemeral=True)

        try:
            target = user or interaction.user
            stats = await self.bot.db.get_user_song_stats(target.id)

            if stats["total"] == 0:
                await interaction.followup.send(
                    f"No song data found for {target.mention}.", ephemeral=True
                )
                return

            lines = [f"📊 **User Stats for {target.mention}**\n"]
            lines.append(f"**Total Songs:** {stats['total']}")
            lines.append(f"**Avg/Week:** {stats['avg_per_week']}  •  **Avg/Month:** {stats['avg_per_month']}")
            lines.append(f"**Active Weeks:** {stats['active_weeks']}")

            if stats["first_post"]:
                lines.append(
                    f"**First Post:** {discord.utils.format_dt(datetime.fromtimestamp(stats['first_post'], tz=timezone.utc), style='D')}  •  "
                    f"**Last Post:** {discord.utils.format_dt(datetime.fromtimestamp(stats['last_post'], tz=timezone.utc), style='R')}"
                )

            # Per channel
            if stats["per_channel"]:
                lines.append("\n**Songs per Channel:**")
                for pc in stats["per_channel"]:
                    ch = interaction.guild.get_channel(pc["channel_id"])
                    ch_name = f"#{ch.name}" if ch else f"channel-{pc['channel_id']}"
                    lines.append(f"  {ch_name}: **{pc['count']}**")

            # By month
            if stats["by_month"]:
                lines.append("\n**By Month** (last 12):")
                for item in stats["by_month"]:
                    lines.append(f"  {item['label']}: **{item['count']}**")

            # By weekday
            if stats["by_weekday"]:
                wd_str = " • ".join(f"{w['label']}: **{w['count']}**" for w in stats["by_weekday"])
                lines.append(f"\n**By Weekday:** {wd_str}")

            # Top days
            if stats["top_days"]:
                lines.append("\n**Top Posting Days:**")
                for item in stats["top_days"]:
                    lines.append(f"  {item['label']}: **{item['count']}** songs")

            text = "\n".join(lines)

            if len(text) <= 2000:
                await interaction.followup.send(text, ephemeral=True)
            else:
                chunks = []
                current = ""
                for line in lines:
                    if len(current) + len(line) + 1 > 1900:
                        chunks.append(current)
                        current = ""
                    current += line + "\n"
                if current.strip():
                    chunks.append(current)
                for chunk in chunks:
                    await interaction.followup.send(chunk, ephemeral=True)

        except Exception as e:
            print(f"[user-stats] Error: {e}")
            await interaction.followup.send(f"Error: {e}", ephemeral=True)


    @app_commands.command(name="user-score", description="Show the song posting leaderboard")
    async def user_score(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=False)

        try:
            ranking = await self.bot.db.get_all_users_ranking()

            if not ranking:
                await interaction.followup.send("No song data yet.")
                return

            bot_name = await self.bot.db.get_setting("bot_name") or "Slowmode Bot"

            medals = ["🥇", "🥈", "🥉"]
            lines = []
            for i, entry in enumerate(ranking[:20]):
                prefix = medals[i] if i < 3 else f"**{i+1}.**"
                lines.append(f"{prefix} <@{entry['user_id']}> — **{entry['score']}** pts ({entry['song_count']} songs · {entry['reaction_count']} reactions)")

            embed = discord.Embed(
                title="🏆 Song Highscore",
                description="\n".join(lines),
                color=discord.Color.gold(),
            )
            if len(ranking) > 20:
                embed.set_footer(text=f"{bot_name} • Top 20 of {len(ranking)} users")
            else:
                embed.set_footer(text=f"{bot_name} • {len(ranking)} users")
            embed.timestamp = discord.utils.utcnow()

            await interaction.followup.send(embed=embed)

        except Exception as e:
            print(f"[user-score] Error: {e}")
            await interaction.followup.send(f"Error: {e}", ephemeral=True)


    @app_commands.command(name="top", description="Show the most reacted songs (only visible to you)")
    @app_commands.describe(
        period="Time range: today, 7, 30, 90, or all (default: 30)",
    )
    @app_commands.choices(period=[
        app_commands.Choice(name="Today", value="1"),
        app_commands.Choice(name="Last 7 days", value="7"),
        app_commands.Choice(name="Last 30 days", value="30"),
        app_commands.Choice(name="Last 90 days", value="90"),
        app_commands.Choice(name="All time", value="all"),
    ])
    async def top_songs_cmd(self, interaction: discord.Interaction, period: str = "30"):
        await interaction.response.defer(ephemeral=True)

        try:
            days = {"1": 1, "7": 7, "30": 30, "90": 90, "all": 0}.get(period, 30)
            top_songs = await self.bot.db.get_top_songs(days=days)

            if not top_songs:
                await interaction.followup.send("No reacted songs found for this period.", ephemeral=True)
                return

            bot_name = await self.bot.db.get_setting("bot_name") or "Slowmode Bot"
            guild = interaction.guild
            period_label = {"1": "Today", "7": "Last 7 days", "30": "Last 30 days", "90": "Last 90 days", "all": "All time"}.get(period, "Last 30 days")

            medals = ["🥇", "🥈", "🥉"]
            lines = []
            for i, song in enumerate(top_songs):
                prefix = medals[i] if i < 3 else f"**{i+1}.**"
                # Resolve author name
                author_name = f"User {song['post_author_id']}"
                if guild and song.get("post_author_id"):
                    member = guild.get_member(song["post_author_id"])
                    if member:
                        author_name = member.display_name

                title = song.get("song_title") or "Unknown Title"
                url = song.get("song_url", "")
                unique = song["unique_count"]
                total = song["total_count"]

                lines.append(
                    f"{prefix} **[{title}]({url})**\n"
                    f"ㅤby **{author_name}** — {unique} unique reactions ({total} total)"
                )

            embed = discord.Embed(
                title=f"🎵 Most Reacted Songs ({period_label})",
                description="\n\n".join(lines),
                color=discord.Color.blurple(),
            )

            # Set thumbnail from top song cover image
            top_url = top_songs[0].get("song_url", "")
            song_id_match = re.search(r'suno\.com/(?:s|song)/([\w-]+)', top_url)
            if song_id_match:
                embed.set_thumbnail(url=f"https://cdn2.suno.ai/image_{song_id_match.group(1)}.jpeg")

            embed.set_footer(text=f"{bot_name} • {period_label}")
            embed.timestamp = discord.utils.utcnow()

            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            print(f"[top] Error: {e}")
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

    @app_commands.command(name="new", description="Show songs from the last 3 days you haven't reacted to yet")
    async def new_songs_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        try:
            channel_id_str = await self.bot.db.get_setting("new_command_channel")
            if not channel_id_str:
                await interaction.followup.send(
                    "The `/new` command is not configured yet. An admin needs to set the channel in **Settings** on the web interface.",
                    ephemeral=True,
                )
                return

            channel_id = int(channel_id_str)
            songs = await self.bot.db.get_unseen_songs(channel_id, interaction.user.id)
            songs = [s for s in songs if s.get("song_title")]

            if not songs:
                await interaction.followup.send("You're all caught up! No new unreacted songs in the last 2 days.", ephemeral=True)
                return

            bot_name = await self.bot.db.get_setting("bot_name") or "Slowmode Bot"
            guild = interaction.guild

            lines = []
            first_url = None
            for i, song in enumerate(songs):
                # Resolve author name
                author_name = f"User {song['post_author_id']}"
                if guild and song.get("post_author_id"):
                    member = guild.get_member(song["post_author_id"])
                    if member:
                        author_name = member.display_name

                title = song.get("song_title") or "Unknown Title"
                url = song.get("song_url", "")
                unique = song["unique_count"]
                total = song["total_count"]

                if first_url is None:
                    first_url = url

                lines.append(
                    f"**{i+1}.** **[{title}]({url})**\n"
                    f"ㅤby **{author_name}** — {unique} unique reactions ({total} total)"
                )

            # Discord embed description limit is 4096 chars — split if needed
            description = "\n\n".join(lines)
            if len(description) > 4096:
                description = description[:4090] + "\n…"

            embed = discord.Embed(
                title=f"🆕 Unreacted Songs ({len(songs)})",
                description=description,
                color=discord.Color.green(),
            )

            # Set thumbnail from first song cover image
            if first_url:
                song_id_match = re.search(r'suno\.com/(?:s|song)/([\w-]+)', first_url)
                if song_id_match:
                    embed.set_thumbnail(url=f"https://cdn2.suno.ai/image_{song_id_match.group(1)}.jpeg")

            embed.set_footer(text=f"{bot_name} • Last 2 days")
            embed.timestamp = discord.utils.utcnow()

            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            print(f"[new] Error: {e}")
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

    @app_commands.command(name="find-song", description="Find a song — by user, title, or random")
    @app_commands.describe(
        user="Optional: filter by user",
        title="Optional: search for a title/keyword in the message",
    )
    async def find_song(self, interaction: discord.Interaction, user: discord.Member = None, title: str = None):
        await interaction.response.defer(ephemeral=True)

        try:
            # Case 1: Title search — need to scan channel history
            if title:
                search_lower = title.lower()
                results = []
                monitored = await self.bot.db.get_monitored_channels()

                for cfg in monitored:
                    channel = interaction.guild.get_channel(cfg["channel_id"])
                    if not channel:
                        continue

                    try:
                        async for message in channel.history(limit=10000):
                            if message.author.bot:
                                continue
                            if user and message.author.id != user.id:
                                continue
                            urls = SUNO_URL_PATTERN.findall(message.content)
                            if not urls:
                                continue
                            # Search in message content AND embeds (title appears in link unfurl)
                            searchable = message.content.lower()
                            for embed in message.embeds:
                                if embed.title:
                                    searchable += " " + embed.title.lower()
                                if embed.description:
                                    searchable += " " + embed.description.lower()
                                if embed.author and embed.author.name:
                                    searchable += " " + embed.author.name.lower()
                            if search_lower not in searchable:
                                continue
                            for url in urls:
                                results.append({
                                    "url": url,
                                    "author_id": message.author.id,
                                    "author": str(message.author),
                                    "posted_at": message.created_at,
                                    "context": message.content[:150],
                                    "channel_name": channel.name,
                                })
                    except Exception:
                        continue

                if not results:
                    msg = f"No songs found for title **\"{title}\"**"
                    if user:
                        msg += f" by {user.mention}"
                    await interaction.followup.send(msg + ".", ephemeral=True)
                    return

                # Deduplicate by URL
                seen = set()
                unique = []
                for r in results:
                    if r["url"] not in seen:
                        seen.add(r["url"])
                        unique.append(r)
                results = unique

                header = f"🎵 **{len(results)} song(s) found for \"{title}\""
                if user:
                    header += f" by {user.mention}"
                header += ":**\n\n"

                entries = []
                for i, r in enumerate(results[:20], 1):
                    entry = (
                        f"**{i}.** {r['url']}\n"
                        f"   by <@{r['author_id']}> in #{r['channel_name']} "
                        f"({discord.utils.format_dt(r['posted_at'], style='R')})"
                    )
                    entries.append(entry)

                if len(results) > 20:
                    entries.append(f"\n*...and {len(results) - 20} more*")

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
                return

            # Case 2: User only (no title) — random song from that user via DB
            # Case 3: No user, no title — random song from anyone via DB
            songs = await self.bot.db.find_songs(user_id=user.id if user else None, limit=1, random=True)

            if not songs:
                if user:
                    await interaction.followup.send(f"No songs found for {user.mention}.", ephemeral=True)
                else:
                    await interaction.followup.send("No songs in the database yet.", ephemeral=True)
                return

            song = songs[0]
            ch = interaction.guild.get_channel(song["channel_id"])
            ch_name = f"#{ch.name}" if ch else f"channel-{song['channel_id']}"

            desc = "🎲 **Random Song"
            if user:
                desc += f" from {user.mention}"
            desc += ":**\n\n"
            desc += f"{song['url']}\n"
            desc += f"by <@{song['user_id']}> in {ch_name}"

            await interaction.followup.send(desc, ephemeral=True)

        except Exception as e:
            print(f"[find-song] Error: {e}")
            await interaction.followup.send(f"Error: {e}", ephemeral=True)


    @app_commands.command(name="talk", description="Let the bot speak for you in the current channel")
    @app_commands.describe(
        translate="Translate output to a language, e.g. de, en, fr, es, no, ja (leave empty for no translation)",
        text="The message the bot should say",
    )
    async def talk(self, interaction: discord.Interaction, translate: str = None, text: str = None):
        if not text:
            await interaction.response.send_message("Please provide a text to say.", ephemeral=True)
            return

        await interaction.response.send_message("Message sent!", ephemeral=True)

        output = text
        if translate:
            try:
                import asyncio
                from deep_translator import GoogleTranslator
                loop = asyncio.get_event_loop()
                output = await loop.run_in_executor(
                    None, lambda: GoogleTranslator(source="auto", target=translate.lower()).translate(text)
                )
            except Exception as e:
                await interaction.followup.send(f"Translation failed: {e}", ephemeral=True)
                return

        await interaction.channel.send(f"*{interaction.user.display_name} says: {output}*")


    @app_commands.command(name="translate", description="Translate a message (only you can see the result)")
    @app_commands.describe(
        to="Target language code, e.g. en, de, fr, es, no, ja",
        text="The text to translate",
    )
    async def translate_cmd(self, interaction: discord.Interaction, to: str, text: str):
        await interaction.response.defer(ephemeral=True)
        try:
            import asyncio
            from deep_translator import GoogleTranslator
            loop = asyncio.get_event_loop()
            translated = await loop.run_in_executor(
                None, lambda: GoogleTranslator(source="auto", target=to.lower()).translate(text)
            )
            await interaction.followup.send(
                f"**Translation** → `{to.lower()}`\n{translated}",
                ephemeral=True,
            )
        except Exception as e:
            await interaction.followup.send(f"Translation failed: {e}", ephemeral=True)


class TranslateLanguageSelect(discord.ui.Select):
    """Dropdown to pick target language for message translation."""

    def __init__(self, original_text: str, author_name: str):
        self.original_text = original_text
        self.author_name = author_name
        options = [
            discord.SelectOption(label="English", value="en", emoji="🇬🇧"),
            discord.SelectOption(label="German", value="de", emoji="🇩🇪"),
            discord.SelectOption(label="Portuguese", value="pt", emoji="🇵🇹"),
            discord.SelectOption(label="Spanish", value="es", emoji="🇪🇸"),
            discord.SelectOption(label="Italian", value="it", emoji="🇮🇹"),
            discord.SelectOption(label="Russian", value="ru", emoji="🇷🇺"),
            discord.SelectOption(label="Norwegian", value="no", emoji="🇳🇴"),
            discord.SelectOption(label="French", value="fr", emoji="🇫🇷"),
            discord.SelectOption(label="Japanese", value="ja", emoji="🇯🇵"),
        ]
        super().__init__(placeholder="Select target language...", options=options)

    async def callback(self, interaction: discord.Interaction):
        target = self.values[0]
        await interaction.response.defer(ephemeral=True)
        try:
            import asyncio
            from deep_translator import GoogleTranslator
            loop = asyncio.get_event_loop()
            translated = await loop.run_in_executor(
                None, lambda: GoogleTranslator(source="auto", target=target).translate(self.original_text)
            )
            label = [o.label for o in self.options if o.value == target][0]
            await interaction.followup.send(
                f"**Translation** → {label} (`{target}`) of {self.author_name}'s message:\n\n{translated}",
                ephemeral=True,
            )
        except Exception as e:
            await interaction.followup.send(f"Translation failed: {e}", ephemeral=True)


class TranslateView(discord.ui.View):
    def __init__(self, original_text: str, author_name: str):
        super().__init__(timeout=60)
        self.add_item(TranslateLanguageSelect(original_text, author_name))


async def setup(bot):
    cog = CommandsCog(bot)

    @app_commands.context_menu(name="Translate Message")
    async def translate_message(interaction: discord.Interaction, message: discord.Message):
        text = message.content
        if not text:
            await interaction.response.send_message("This message has no text content to translate.", ephemeral=True)
            return
        view = TranslateView(text, message.author.display_name)
        await interaction.response.send_message("Select a language to translate to:", view=view, ephemeral=True)

    bot.tree.add_command(translate_message)
    await bot.add_cog(cog)
