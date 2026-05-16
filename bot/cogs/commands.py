import re
import time
import math
import random
import asyncio
from datetime import datetime, timedelta, timezone
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

SUNO_URL_PATTERN = re.compile(r'https://suno\.com/(?:s|song)/[\w-]+')
YOUTUBE_URL_RE   = re.compile(
    r'(?:https?://)?(?:www\.)?(?:youtu\.be/|youtube\.com/(?:watch\?v=|embed/|v/|shorts/))'
    r'([A-Za-z0-9_-]{11})'
)
SUNO_PLAYLIST_PATTERN = re.compile(r'https://suno\.com/playlist/[\w-]+')
SPOTIFY_ALBUM_PATTERN = re.compile(r'https://open\.spotify\.com/album/[\w?=&-]+')

DEFAULT_REACTION_EMOJIS = ["👍", "❤️", "🔥", "🎵"]


class NewSongCarouselView(discord.ui.View):
    """Carousel view for /new — shows one song at a time with emoji reaction buttons."""

    def __init__(self, bot, songs: list[dict], user: discord.Member, guild: discord.Guild, bot_name: str):
        super().__init__(timeout=600)
        self.bot = bot
        self.songs = songs
        self.user = user
        self.guild = guild
        self.bot_name = bot_name
        self.index = 0
        self.emoji_list: list[str] = []

    async def setup_emojis(self):
        """Load user's most-used emojis and build initial buttons."""
        user_emojis = await self.bot.db.get_user_top_emojis(self.user.id, limit=4)
        for e in DEFAULT_REACTION_EMOJIS:
            if e not in user_emojis and len(user_emojis) < 4:
                user_emojis.append(e)
        self.emoji_list = user_emojis[:4]
        self._rebuild_buttons()

    def _rebuild_buttons(self):
        self.clear_items()
        song = self.songs[self.index]

        # Row 1: emoji reaction buttons
        for emoji_str in self.emoji_list:
            btn = discord.ui.Button(style=discord.ButtonStyle.secondary, emoji=emoji_str, row=0)
            btn.callback = self._make_emoji_callback(emoji_str)
            self.add_item(btn)

        # Row 2: skip + jump link
        skip_btn = discord.ui.Button(label="Skip", emoji="⏭️", style=discord.ButtonStyle.secondary, row=1)
        skip_btn.callback = self._skip_callback
        self.add_item(skip_btn)

        if song.get("message_id") and song.get("channel_id") and self.guild:
            jump_url = f"https://discord.com/channels/{self.guild.id}/{song['channel_id']}/{song['message_id']}"
            self.add_item(discord.ui.Button(
                label="Im Kanal öffnen", emoji="🔗",
                style=discord.ButtonStyle.link, url=jump_url, row=1,
            ))

    def build_embed(self) -> discord.Embed:
        song = self.songs[self.index]
        title = song.get("song_title") or "Unknown Title"
        url = song.get("song_url", "")
        unique = song["unique_count"]
        total = song["total_count"]

        author_name = f"User {song['post_author_id']}"
        if self.guild and song.get("post_author_id"):
            member = self.guild.get_member(song["post_author_id"])
            if member:
                author_name = member.display_name

        embed = discord.Embed(
            title=f"🆕 Song {self.index + 1} von {len(self.songs)}",
            color=discord.Color.green(),
        )
        embed.add_field(
            name=title,
            value=(
                f"by **{author_name}** — {unique} unique reactions ({total} total)\n"
                f"[▶ Listen on Suno]({url})"
            ),
            inline=False,
        )

        song_id_match = re.search(r'suno\.com/(?:s|song)/([\w-]+)', url)
        if song_id_match:
            embed.set_thumbnail(url=f"https://cdn2.suno.ai/image_{song_id_match.group(1)}.jpeg")

        embed.set_footer(text=f"{self.bot_name} • React or skip to continue")
        embed.timestamp = discord.utils.utcnow()
        return embed

    def _build_done_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="✅ All caught up!",
            description=f"You've gone through all {len(self.songs)} songs.",
            color=discord.Color.green(),
        )
        embed.set_footer(text=self.bot_name)
        embed.timestamp = discord.utils.utcnow()
        return embed

    def _make_emoji_callback(self, emoji_str: str):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.user.id:
                await interaction.response.send_message("This is not your session.", ephemeral=True)
                return

            song = self.songs[self.index]
            try:
                await self.bot.db.add_song_reaction(
                    message_id=song["message_id"],
                    channel_id=song["channel_id"],
                    song_url=song["song_url"],
                    post_author_id=song["post_author_id"],
                    reactor_user_id=self.user.id,
                    reactor_user_name=str(self.user),
                    emoji=emoji_str,
                    song_title=song.get("song_title"),
                )
            except Exception as e:
                print(f"[new carousel] Error saving reaction: {e}")

            self.index += 1
            if self.index < len(self.songs):
                self._rebuild_buttons()
                await interaction.response.edit_message(embed=self.build_embed(), view=self)
            else:
                await interaction.response.edit_message(embed=self._build_done_embed(), view=None)
                self.stop()

        return callback

    async def _skip_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user.id:
            await interaction.response.send_message("This is not your session.", ephemeral=True)
            return

        self.index += 1
        if self.index < len(self.songs):
            self._rebuild_buttons()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
        else:
            await interaction.response.edit_message(embed=self._build_done_embed(), view=None)
            self.stop()

    async def on_timeout(self):
        pass


class PartyCarouselView(discord.ui.View):
    """Carousel view for /party — browse party playlist songs one at a time."""

    def __init__(self, bot, songs: list[dict], user: discord.Member, guild: discord.Guild, bot_name: str):
        super().__init__(timeout=600)
        self.bot = bot
        self.songs = songs
        self.user = user
        self.guild = guild
        self.bot_name = bot_name
        self.index = 0
        self._rebuild_buttons()

    def _rebuild_buttons(self):
        self.clear_items()
        song = self.songs[self.index]
        url = song.get("url", "")

        # Row 0: Listen link
        self.add_item(discord.ui.Button(
            label="Listen on Suno", emoji="▶️",
            style=discord.ButtonStyle.link, url=url, row=0,
        ))

        # Row 1: Heard + Next
        heard_btn = discord.ui.Button(label="Heard ✅", style=discord.ButtonStyle.success, row=1)
        heard_btn.callback = self._heard_callback
        self.add_item(heard_btn)

        next_btn = discord.ui.Button(label="Next Song ⏭️", style=discord.ButtonStyle.secondary, row=1)
        next_btn.callback = self._next_callback
        self.add_item(next_btn)

    def build_embed(self) -> discord.Embed:
        song = self.songs[self.index]
        title = song.get("song_title") or "Unknown Title"
        url = song.get("url", "")
        submitter_name = song.get("user_name") or f"User {song['user_id']}"
        if self.guild:
            member = self.guild.get_member(song["user_id"])
            if member:
                submitter_name = member.display_name

        unheard = sum(1 for s in self.songs if not s.get("_heard"))
        embed = discord.Embed(
            title=f"🎧 Party Song {self.index + 1} von {len(self.songs)}",
            description=f"**{unheard}** songs remaining",
            color=discord.Color.purple(),
        )
        embed.add_field(
            name=title,
            value=f"Submitted by **{submitter_name}**\n[▶ Listen on Suno]({url})",
            inline=False,
        )

        song_id_match = re.search(r'suno\.com/(?:s|song)/([\w-]+)', url)
        if song_id_match:
            embed.set_thumbnail(url=f"https://cdn2.suno.ai/image_{song_id_match.group(1)}.jpeg")

        embed.set_footer(text=f"{self.bot_name} • Listening Party")
        embed.timestamp = discord.utils.utcnow()
        return embed

    def _build_done_embed(self) -> discord.Embed:
        total = len(self.songs)
        heard = sum(1 for s in self.songs if s.get("_heard"))
        embed = discord.Embed(
            title="🎉 Listening Party Complete!",
            description=f"All **{heard}** of **{total}** songs have been listened to!",
            color=discord.Color.green(),
        )
        embed.set_footer(text=self.bot_name)
        embed.timestamp = discord.utils.utcnow()
        return embed

    def _find_next_unheard(self) -> bool:
        """Advance index to the next unheard song. Returns False if all heard."""
        start = self.index
        for _ in range(len(self.songs)):
            self.index = (self.index + 1) % len(self.songs)
            if not self.songs[self.index].get("_heard"):
                return True
            if self.index == start:
                break
        return not all(s.get("_heard") for s in self.songs)

    async def _heard_callback(self, interaction: discord.Interaction):
        song = self.songs[self.index]
        song["_heard"] = True
        try:
            await self.bot.db.party_mark_heard(song["id"])
        except Exception as e:
            print(f"[party] Error marking heard: {e}")

        if self._find_next_unheard():
            self._rebuild_buttons()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
        else:
            await interaction.response.edit_message(embed=self._build_done_embed(), view=None)
            self.stop()

    async def _next_callback(self, interaction: discord.Interaction):
        if self._find_next_unheard():
            self._rebuild_buttons()
            await interaction.response.edit_message(embed=self.build_embed(), view=self)
        else:
            await interaction.response.edit_message(embed=self._build_done_embed(), view=None)
            self.stop()

    async def on_timeout(self):
        pass


class UserSongsCarouselView(discord.ui.View):
    """Carousel view for /find-usersongs — browse a user's songs with cover, title, link."""

    def __init__(self, bot, songs: list[dict], target_user: discord.Member, guild: discord.Guild, bot_name: str, fetch_info):
        super().__init__(timeout=600)
        self.bot = bot
        self.songs = songs  # newest first; mutated in-place to cache title/artist/image
        self.target_user = target_user
        self.guild = guild
        self.bot_name = bot_name
        self._fetch_info = fetch_info  # async (url) -> (title, artist, image_url)
        self.index = 0
        self._rebuild_buttons()

    def _rebuild_buttons(self):
        self.clear_items()
        song = self.songs[self.index]
        url = song.get("url", "")

        prev_btn = discord.ui.Button(label="◀ Prev", style=discord.ButtonStyle.secondary, row=0,
                                     disabled=(self.index == 0))
        prev_btn.callback = self._prev_callback
        self.add_item(prev_btn)

        counter = discord.ui.Button(
            label=f"{self.index + 1} / {len(self.songs)}",
            style=discord.ButtonStyle.secondary, row=0, disabled=True,
        )
        self.add_item(counter)

        next_btn = discord.ui.Button(label="Next ▶", style=discord.ButtonStyle.secondary, row=0,
                                     disabled=(self.index >= len(self.songs) - 1))
        next_btn.callback = self._next_callback
        self.add_item(next_btn)

        self.add_item(discord.ui.Button(
            label="Listen on Suno", emoji="▶️",
            style=discord.ButtonStyle.link, url=url, row=1,
        ))

    async def _ensure_meta(self, song: dict):
        if song.get("_meta_fetched"):
            return
        song["_meta_fetched"] = True
        try:
            title, artist, image_url = await self._fetch_info(song.get("url", ""))
        except Exception:
            title, artist, image_url = None, None, None
        song["_title"] = title
        song["_artist"] = artist
        song["_image_url"] = image_url

    def build_embed(self) -> discord.Embed:
        song = self.songs[self.index]
        url = song.get("url", "")
        title = song.get("_title") or song.get("song_title") or "Unknown Title"
        artist = song.get("_artist")

        ch = self.guild.get_channel(song["channel_id"]) if self.guild else None
        ch_name = f"#{ch.name}" if ch else f"channel-{song['channel_id']}"

        posted_ts = song.get("posted_at")
        try:
            posted_dt = datetime.fromtimestamp(float(posted_ts), tz=timezone.utc) if posted_ts else None
        except Exception:
            posted_dt = None

        header = f"🎵 Song {self.index + 1} / {len(self.songs)} from {self.target_user.mention}"

        desc_parts = [f"**[{title}]({url})**"]
        if artist:
            desc_parts.append(f"by **{artist}**")
        desc_parts.append(f"posted in {ch_name}" + (f" • {discord.utils.format_dt(posted_dt, style='R')}" if posted_dt else ""))
        desc_parts.append(url)

        embed = discord.Embed(
            title=header,
            description="\n".join(desc_parts),
            color=discord.Color.blurple(),
        )

        image_url = song.get("_image_url")
        if not image_url:
            song_id_match = re.search(r'suno\.com/(?:s|song)/([\w-]+)', url)
            if song_id_match:
                image_url = f"https://cdn2.suno.ai/image_{song_id_match.group(1)}.jpeg"
        if image_url:
            embed.set_image(url=image_url)

        embed.set_footer(text=f"{self.bot_name}")
        if posted_dt:
            embed.timestamp = posted_dt
        return embed

    async def _navigate(self, interaction: discord.Interaction, delta: int):
        # Defer the component interaction immediately (silent update) so we have
        # time to fetch metadata for songs whose cover wasn't pre-cached yet.
        # Without this, slow Suno responses exceed the 3s interaction window
        # and the embed edit (with cover image) is silently dropped.
        try:
            await interaction.response.defer()
        except discord.InteractionResponded:
            pass
        new_idx = self.index + delta
        if 0 <= new_idx < len(self.songs):
            self.index = new_idx
        await self._ensure_meta(self.songs[self.index])
        self._rebuild_buttons()
        try:
            await interaction.edit_original_response(embed=self.build_embed(), view=self)
        except discord.HTTPException as e:
            print(f"[find-usersongs] edit failed: {e}")

    async def _prev_callback(self, interaction: discord.Interaction):
        await self._navigate(interaction, -1)

    async def _next_callback(self, interaction: discord.Interaction):
        await self._navigate(interaction, +1)

    async def on_timeout(self):
        pass


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

    @app_commands.command(name="timer", description="Show your remaining cooldown timers")
    async def timer(self, interaction: discord.Interaction):
        monitored = await self.bot.db.get_monitored_channels()
        active_timers = []

        for ch in monitored:
            cooldown_minutes = ch["cooldown_minutes"]
            if cooldown_minutes <= 0 or not ch.get("enabled", True):
                continue

            record = await self.bot.db.get_cooldown_record(interaction.user.id, ch["channel_id"])
            if not record:
                continue

            elapsed = time.time() - record["timestamp"]
            remaining = (cooldown_minutes * 60) - elapsed
            if remaining <= 0:
                continue

            # Format remaining time
            if remaining >= 3600:
                hours = math.ceil(remaining / 3600)
                time_str = f"{hours}h"
            elif remaining >= 60:
                mins = math.ceil(remaining / 60)
                time_str = f"{mins}min"
            else:
                secs = math.ceil(remaining)
                time_str = f"{secs}s"

            # Resolve channel name
            guild_ch = interaction.guild.get_channel(ch["channel_id"]) if interaction.guild else None
            ch_name = f"#{guild_ch.name}" if guild_ch else f"#channel-{ch['channel_id']}"

            active_timers.append(f"**{ch_name}** — {time_str} remaining")

        if active_timers:
            bot_name = await self.bot.db.get_setting("bot_name") or "Slowmode Bot"
            embed = discord.Embed(
                title="⏱️ Your Active Cooldowns",
                description="\n".join(active_timers),
                color=discord.Color.orange(),
            )
            embed.set_footer(text=bot_name)
            embed.timestamp = discord.utils.utcnow()
            await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message("✅ You have no active cooldowns.", ephemeral=True)

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

    @app_commands.command(name="new", description="Show songs from the last 2 days you haven't reacted to yet")
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
            songs = [s for s in songs if s.get("message_id")]

            if not songs:
                await interaction.followup.send("You're all caught up! No new unreacted songs in the last 2 days.", ephemeral=True)
                return

            bot_name = await self.bot.db.get_setting("bot_name") or "Slowmode Bot"

            view = NewSongCarouselView(
                bot=self.bot,
                songs=songs,
                user=interaction.user,
                guild=interaction.guild,
                bot_name=bot_name,
            )
            await view.setup_emojis()
            embed = view.build_embed()

            await interaction.followup.send(embed=embed, view=view, ephemeral=True)

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

    @app_commands.command(name="find-usersongs", description="Browse a user's recent songs (carousel, newest first)")
    @app_commands.describe(
        user="User whose songs to browse",
        count="How many of the most recent songs to show (1–25, default 1)",
        channel="Limit search to one monitored channel (default: all channels)",
    )
    async def find_usersongs(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        count: int = 1,
        channel: discord.TextChannel = None,
    ):
        await interaction.response.defer(ephemeral=True)
        try:
            if count < 1:
                count = 1
            if count > 25:
                count = 25

            channel_id = channel.id if channel else None
            songs = await self.bot.db.find_songs(
                user_id=user.id, channel_id=channel_id, limit=count, random=False
            )
            if not songs:
                where = f" in {channel.mention}" if channel else ""
                await interaction.followup.send(f"No songs found for {user.mention}{where}.", ephemeral=True)
                return

            # Pre-fetch metadata for ALL songs in parallel so every cover is cached
            # before the carousel is shown. This avoids per-click race-conditions
            # against Discord's interaction window.
            async def _prefetch(song):
                try:
                    title, artist, image_url = await self._fetch_suno_info(song.get("url", ""))
                except Exception as e:
                    print(f"[find-usersongs] prefetch failed for {song.get('url')}: {e}")
                    title, artist, image_url = None, None, None
                song["_meta_fetched"] = True
                song["_title"] = title
                song["_artist"] = artist
                song["_image_url"] = image_url

            await asyncio.gather(*(_prefetch(s) for s in songs))

            bot_name = await self.bot.db.get_setting("bot_name") or "Slowmode Bot"
            view = UserSongsCarouselView(
                bot=self.bot,
                songs=songs,
                target_user=user,
                guild=interaction.guild,
                bot_name=bot_name,
                fetch_info=self._fetch_suno_info,
            )
            await interaction.followup.send(embed=view.build_embed(), view=view, ephemeral=True)

        except Exception as e:
            print(f"[find-usersongs] Error: {e}")
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


    @app_commands.command(name="imageposting", description="Post an image from the library to the current channel")
    @app_commands.describe(
        category="Category to pick from (required)",
        title="Image title (optional — random from category if not provided)",
    )
    async def imageposting_cmd(self, interaction: discord.Interaction, category: str, title: str = None):
        await interaction.response.defer()

        try:
            cat = await self.bot.db.get_image_category_by_name(category)
            if not cat:
                await interaction.followup.send(f"Category `{category}` not found.", ephemeral=True)
                return

            if title:
                image = await self.bot.db.get_image_post_by_title(title, cat["id"])
                if not image:
                    await interaction.followup.send(
                        f"No image with title `{title}` found in category `{category}`.", ephemeral=True
                    )
                    return
            else:
                image = await self.bot.db.get_random_image_post(cat["id"])
                if not image:
                    await interaction.followup.send(
                        f"No images in category `{category}`.", ephemeral=True
                    )
                    return

            import os
            upload_dir = os.path.join(os.path.dirname(self.bot.db.db_path), "uploads")
            filepath = os.path.join(upload_dir, image["filename"])

            if not os.path.exists(filepath):
                await interaction.followup.send("Image file not found on disk.", ephemeral=True)
                return

            file = discord.File(filepath, filename=image["filename"])
            description = image.get("description") or ""

            await interaction.followup.send(file=file)
            if description:
                await interaction.channel.send(description)

        except Exception as e:
            print(f"[imageposting] Error: {e}")
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

    @imageposting_cmd.autocomplete("category")
    async def _imageposting_category_ac(self, interaction: discord.Interaction, current: str):
        categories = await self.bot.db.get_image_categories()
        return [
            app_commands.Choice(name=c["name"], value=c["name"])
            for c in categories if current.lower() in c["name"].lower()
        ][:25]

    @imageposting_cmd.autocomplete("title")
    async def _imageposting_title_ac(self, interaction: discord.Interaction, current: str):
        # Try to get the category from the already-filled options
        cat_name = None
        for opt in interaction.data.get("options", []):
            if opt["name"] == "category":
                cat_name = opt.get("value")
                break

        if not cat_name:
            return []

        cat = await self.bot.db.get_image_category_by_name(cat_name)
        if not cat:
            return []

        images = await self.bot.db.get_image_posts(category_id=cat["id"])
        return [
            app_commands.Choice(name=img["title"], value=img["title"])
            for img in images if current.lower() in img["title"].lower()
        ][:25]


    # --- Listening Party Playlist Commands ---

    @staticmethod
    async def _fetch_suno_info(url: str) -> tuple[str | None, str | None, str | None]:
        """Fetch song title, artist and image from a Suno URL. Returns (title, artist, image_url)."""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return None, None, None
                    html = await resp.text()
                    # Extract og:image
                    image_url = None
                    img_match = re.search(r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', html)
                    if not img_match:
                        img_match = re.search(r'<meta\s+content=["\']([^"\']+)["\']\s+property=["\']og:image["\']', html)
                    if img_match:
                        image_url = img_match.group(1).strip()
                    # <title> format: "Song Title by Artist Name | Suno"
                    match = re.search(r'<title>([^<]+)</title>', html)
                    if match:
                        raw = match.group(1).strip()
                        raw = re.sub(r'\s*[|\-–]\s*Suno$', '', raw).strip()
                        by_match = re.search(r'^(.+?)\s+by\s+(.+)$', raw)
                        if by_match:
                            return by_match.group(1).strip(), by_match.group(2).strip(), image_url
                        return raw, None, image_url
        except Exception:
            pass
        return None, None, None

    async def _fetch_youtube_info(self, url: str):
        """Fetch title, author and thumbnail from a YouTube URL via oEmbed."""
        import aiohttp
        try:
            oembed = f"https://www.youtube.com/oembed?url={url}&format=json"
            async with aiohttp.ClientSession() as sess:
                async with sess.get(oembed, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return None, None, None
                    data = await resp.json()
                    title  = data.get("title")
                    author = data.get("author_name")
                    m = YOUTUBE_URL_RE.search(url)
                    thumb = f"https://img.youtube.com/vi/{m.group(1)}/hqdefault.jpg" if m else None
                    return title, author, thumb
        except Exception:
            return None, None, None

    @app_commands.command(name="party-submit", description="Submit a song to the Listening Party playlist (max 2 per user)")
    @app_commands.describe(url="Suno or YouTube URL to submit")
    async def party_submit(self, interaction: discord.Interaction, url: str):
        await interaction.response.defer(ephemeral=True)
        try:
            is_suno = bool(SUNO_URL_PATTERN.search(url))
            is_yt   = bool(YOUTUBE_URL_RE.search(url))
            if not is_suno and not is_yt:
                await interaction.followup.send("Invalid URL. Please provide a valid Suno or YouTube link.", ephemeral=True)
                return

            max_songs = int(await self.bot.db.get_setting("party_max_songs") or "2")
            count = await self.bot.db.party_get_user_song_count(interaction.user.id)
            if count >= max_songs:
                await interaction.followup.send(f"You have already submitted {max_songs} songs. Remove one first with `/party-remove`.", ephemeral=True)
                return

            if is_suno:
                title, artist, image_url = await self._fetch_suno_info(url)
            else:
                title, artist, image_url = await self._fetch_youtube_info(url)

            await self.bot.db.party_submit_song(
                user_id=interaction.user.id,
                user_name=artist or str(interaction.user),
                url=url,
                song_title=title,
                image_url=image_url,
            )
            display = f"**{title}**" if title else url
            if artist:
                display += f" by **{artist}**"
            display += f"\n{url}"
            await interaction.followup.send(
                f"✅ Song submitted! ({count + 1}/{max_songs})\n{display}",
                ephemeral=True,
            )
        except Exception as e:
            print(f"[party-submit] Error: {e}")
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

    @app_commands.command(name="party-songs", description="View your submitted songs for the Listening Party")
    async def party_songs(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            songs = await self.bot.db.party_get_user_songs(interaction.user.id)
            if not songs:
                await interaction.followup.send("You haven't submitted any songs yet. Use `/party-submit` to add one.", ephemeral=True)
                return

            embed = discord.Embed(
                title="🎵 Your Party Songs",
                color=discord.Color.purple(),
            )
            for i, song in enumerate(songs, 1):
                title = song.get("song_title") or "Unknown Title"
                status = "✅ Heard" if song["heard"] else "⏳ Pending"
                embed.add_field(
                    name=f"Song {i} — {status}",
                    value=f"[{title}]({song['url']})\nID: `{song['id']}`",
                    inline=False,
                )
            embed.set_footer(text=f"{len(songs)}/2 slots used")
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

    @app_commands.command(name="party-remove", description="Remove one of your submitted songs from the Listening Party playlist")
    @app_commands.describe(song_id="ID of the song to remove (shown in /party-songs)")
    async def party_remove(self, interaction: discord.Interaction, song_id: int):
        await interaction.response.defer(ephemeral=True)
        try:
            removed = await self.bot.db.party_remove_song(song_id, interaction.user.id)
            if removed:
                await interaction.followup.send(f"✅ Song `{song_id}` removed from the playlist.", ephemeral=True)
            else:
                await interaction.followup.send("Song not found or it doesn't belong to you. Use `/party-songs` to see your songs and their IDs.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

    @app_commands.command(name="party-list", description="List all submitted songs for the Listening Party")
    async def party_list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            songs = await self.bot.db.party_get_all_songs()
            if not songs:
                await interaction.followup.send("The party playlist is empty. Submit songs with `/party-submit`!", ephemeral=True)
                return

            embed = discord.Embed(
                title="🎶 Listening Party Playlist",
                description=f"**{len(songs)}** songs submitted",
                color=discord.Color.purple(),
            )
            for i, song in enumerate(songs, 1):
                title = song.get("song_title") or "Unknown Title"
                submitter = song.get("user_name") or f"User {song['user_id']}"
                if interaction.guild:
                    member = interaction.guild.get_member(song["user_id"])
                    if member:
                        submitter = member.display_name
                status = "✅" if song["heard"] else "⏳"
                embed.add_field(
                    name=f"{status} {i}. {title}",
                    value=f"by **{submitter}** — [Listen]({song['url']})",
                    inline=False,
                )
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

    @app_commands.command(name="party", description="Start the Listening Party carousel — browse and listen to submitted songs")
    async def party_carousel(self, interaction: discord.Interaction):
        if not await self._permission_check(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            songs = await self.bot.db.party_get_unheard_songs()
            if not songs:
                all_songs = await self.bot.db.party_get_all_songs()
                if all_songs:
                    await interaction.followup.send("🎉 All songs have been listened to! Use `/party-reset` to start a new round.", ephemeral=True)
                else:
                    await interaction.followup.send("The party playlist is empty. Submit songs with `/party-submit`!", ephemeral=True)
                return

            bot_name = await self.bot.db.get_setting("bot_name") or "Slowmode Bot"
            view = PartyCarouselView(self.bot, songs, interaction.user, interaction.guild, bot_name)
            await interaction.followup.send(embed=view.build_embed(), view=view, ephemeral=True)
        except Exception as e:
            print(f"[party] Error: {e}")
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

    @app_commands.command(name="party-reset", description="Reset the Listening Party playlist (Admin/Mod only)")
    async def party_reset(self, interaction: discord.Interaction):
        if not await self._permission_check(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            count = await self.bot.db.party_reset()
            await interaction.followup.send(f"🗑️ Party playlist cleared. {count} songs removed.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Error: {e}", ephemeral=True)

    @app_commands.command(name="poll-create", description="Create and post a new poll")
    @app_commands.describe(channel="Channel or forum to post the poll in")
    async def poll_create(self, interaction: discord.Interaction, channel: discord.abc.GuildChannel = None):
        target = channel or interaction.channel
        if not isinstance(target, (discord.TextChannel, discord.ForumChannel, discord.VoiceChannel, discord.Thread)):
            await interaction.response.send_message("Please select a text, voice, or forum channel.", ephemeral=True)
            return
        modal = PollOptionsModal(self.bot, target, creator_id=interaction.user.id)
        await interaction.response.send_modal(modal)

    @app_commands.command(name="poll-edit", description="Edit an existing poll")
    async def poll_edit(self, interaction: discord.Interaction):
        is_admin = await self._has_command_permission(interaction)
        polls = await self.bot.db.get_all_polls()
        if is_admin:
            active_polls = [p for p in polls if p["active"]]
        else:
            active_polls = [p for p in polls if p["active"] and p.get("creator_id") == interaction.user.id]
        if not active_polls:
            await interaction.response.send_message("No polls available for you to edit.", ephemeral=True)
            return
        view = PollSelectView(self.bot, active_polls)
        await interaction.response.send_message("Select a poll to edit:", view=view, ephemeral=True)

    @app_commands.command(name="player", description="Post the link to the Suno web player")
    async def player_cmd(self, interaction: discord.Interaction):
        player_url = await self.bot.db.get_setting("player_url")
        if not player_url:
            await interaction.response.send_message(
                "The player URL is not configured yet. An admin needs to set it in **Settings** on the web interface.",
                ephemeral=True,
            )
            return

        bot_name = await self.bot.db.get_setting("bot_name") or "Slowmode Bot"
        embed = discord.Embed(
            title="🎵 Suno Web Player",
            description=f"Listen to all posted songs directly in your browser!\n\n**[▶ Open Player]({player_url})**",
            color=discord.Color.purple(),
        )
        embed.set_footer(text=bot_name)
        embed.timestamp = discord.utils.utcnow()
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="help", description="Show a list of all available commands")
    async def help_command(self, interaction: discord.Interaction):
        has_admin = await self._has_command_permission(interaction)

        embed = discord.Embed(
            title="📖 Bot Commands",
            description="Here are the commands available to you:",
            color=discord.Color.blue(),
        )

        # Cooldown Management (Admin only)
        if has_admin:
            cooldown_cmds = (
                "**`/cooldown-set #channel <minutes>`** — Set cooldown for a channel (0–2880 min)\n"
                "**`/cooldown-info #channel`** — Show current cooldown configuration\n"
                "**`/cooldown-reset @user [#channel]`** — Reset a user's cooldown\n"
                "**`/cooldown-clear [#channel]`** — Clear all cooldowns in a channel\n"
                "**`/cooldown-toggle #channel <true/false>`** — Enable/disable monitoring\n"
                "**`/timer`** — Show your remaining cooldown timers"
            )
            embed.add_field(name="⏱️ Cooldown Management", value=cooldown_cmds, inline=False)
        else:
            # Non-admins only see timer
            embed.add_field(
                name="⏱️ Cooldown",
                value="**`/timer`** — Show your remaining cooldown timers",
                inline=False,
            )

        # Song Discovery & Stats (all users)
        song_cmds = (
            "**`/find-list <search>`** — Search Suno playlists by artist/user/keyword\n"
            "**`/find-song [@user] [title]`** — Find songs by user, title, or random\n"
            "**`/find-usersongs <@user> [count]`** — Browse a user's recent songs in a carousel\n"
            "**`/random-song [#channel]`** — Post a random Suno song from a listening party\n"
            "**`/new`** — Browse unreacted songs from the last 2 days (carousel)\n"
            "**`/top <period>`** — Top 10 most reacted songs (today/7/30/90/all)\n"
            "**`/song-stats [#channel]`** — Song posting statistics\n"
            "**`/user-stats [@user]`** — User song posting stats\n"
            "**`/user-score`** — Song posting leaderboard"
        )
        embed.add_field(name="🎵 Song Discovery & Stats", value=song_cmds, inline=False)

        # Listening Party (all users + admin reset)
        if has_admin:
            party_cmds = (
                "**`/party-submit <url>`** — Submit a Suno or YouTube song (max 2 per user)\n"
                "**`/party-songs`** — View your submitted songs\n"
                "**`/party-remove <id>`** — Remove one of your songs\n"
                "**`/party-list`** — List all submitted songs\n"
                "**`/party`** — Browse unheard songs in carousel mode\n"
                "**`/party-reset`** — Reset the playlist (Admin/Mod only)"
            )
        else:
            party_cmds = (
                "**`/party-submit <url>`** — Submit a Suno or YouTube song (max 2 per user)\n"
                "**`/party-songs`** — View your submitted songs\n"
                "**`/party-remove <id>`** — Remove one of your songs\n"
                "**`/party-list`** — List all submitted songs\n"
                "**`/party`** — Browse unheard songs in carousel mode"
            )
        embed.add_field(name="🎉 Listening Party", value=party_cmds, inline=False)

        # Utility & Fun (all users)
        utility_cmds = (
            "**`/player`** — Post the link to the Suno web player\n"
            "**`/talk [translate] <text>`** — Bot speaks your message in channel\n"
            "**`/translate <to> <text>`** — Translate text privately\n"
            "**`/imageposting <category> [title]`** — Post an image from the library\n"
            "**`/poll-create [channel]`** — Create a poll\n"
            "**`/poll-edit`** — Edit your existing polls\n"
            "**`/twitch-playlist`** — Show the Twitch radio playlist\n"
            "**`/help`** — Show this help message"
        )
        embed.add_field(name="🛠️ Utility & Fun", value=utility_cmds, inline=False)

        # Context Menu (all users)
        embed.add_field(
            name="📋 Context Menu (Right-click a message)",
            value="**`Translate Message`** — Translate any message",
            inline=False,
        )

        # Experimental Radio (admin only)
        if has_admin:
            embed.add_field(
                name="🎙️ Experimental Radio (Admin)",
                value=(
                    "**`/twitch-submit`** — Submit a Suno song to the Experimental Radio\n"
                    "**`/twitch-delete`** — Remove one of your Experimental Radio submissions"
                ),
                inline=False,
            )

        # Dynamic footer based on permissions
        if has_admin:
            embed.set_footer(text="✅ You have admin/mod permissions — all commands shown")
        else:
            embed.set_footer(text="ℹ️ Some admin commands are hidden — ask a moderator for access")

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="twitch-submit", description="Submit a Suno song to the Experimental Radio (max 2 per user)")
    @app_commands.default_permissions(administrator=True)
    async def exp_radio_submit(self, interaction: discord.Interaction):
        embed = discord.Embed(
            title="🎙️ Submit to Experimental Radio",
            description=_EXP_TERMS_DISPLAY,
            color=discord.Color.purple(),
        )
        embed.set_footer(text="Click 'I Agree & Submit' to proceed with your submission.")
        view = ExpRadioTermsView(self.bot)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(name="twitch-delete", description="Remove one of your Experimental Radio submissions")
    @app_commands.default_permissions(administrator=True)
    async def exp_radio_delete(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        songs = await self.bot.db.get_exp_radio_songs_by_user(interaction.user.id)
        if not songs:
            await interaction.followup.send("You have no active Experimental Radio submissions.", ephemeral=True)
            return
        view = ExpRadioDeleteView(self.bot, songs)
        lines = [f"**{i+1}.** {s['title'] or 'Pending…'} — {s['artist'] or ''} (#{s['id']})" for i, s in enumerate(songs)]
        await interaction.followup.send(
            "**Your Experimental Radio submissions:**\n" + "\n".join(lines) + "\n\nSelect a song to delete:",
            view=view, ephemeral=True,
        )

    @app_commands.command(name="twitch-playlist", description="Show the current Twitch radio playlist")
    async def twitch_playlist(self, interaction: discord.Interaction):
        from datetime import datetime, timezone
        songs = await self.bot.db.get_all_radio_songs(active_only=True)
        if not songs:
            await interaction.response.send_message("📻 The playlist is currently empty.", ephemeral=True)
            return
        lines = []
        for i, s in enumerate(songs, 1):
            expires = datetime.fromtimestamp(s["expires_at"], tz=timezone.utc).strftime("%d.%m.%Y")
            dur = f"{s['duration'] / 60:.1f}"
            lines.append(f"**{i}.** {s['title']} — {s['artist']}  ({dur} min, expires {expires})")
        # Discord message limit is 2000 chars — split if needed
        header = f"📻 **Twitch Radio Playlist** ({len(songs)} songs)\n\n"
        chunks = []
        current = header
        for line in lines:
            if len(current) + len(line) + 1 > 1900:
                chunks.append(current)
                current = ""
            current += line + "\n"
        if current:
            chunks.append(current)
        await interaction.response.send_message(chunks[0], ephemeral=True)
        for chunk in chunks[1:]:
            await interaction.followup.send(chunk, ephemeral=True)


_SUNO_SUBMIT_RE = re.compile(r'(?:suno\.com/(?:s|song)/)([A-Za-z0-9_-]{8,})')
_EXP_MAX_PER_USER = 2

_EXP_TERMS_DISPLAY = (
    "**By submitting you confirm:**\n"
    "• You created this song on Suno.ai and hold the rights to stream it\n"
    "• You grant a **14-day streaming license** for Twitch live streams & VODs\n"
    "• The content complies with community guidelines (no hate speech, explicit or illegal content)\n"
    "• Songs expire and are deleted automatically after **14 days**\n"
    f"• Maximum **{_EXP_MAX_PER_USER} songs** per user\n"
    "• Maximum song length: **5 minutes**"
)

_EXP_RIGHTS_DECLARATION = (
    "I confirm that I created this song on Suno.ai, hold the necessary rights to stream it, "
    "and grant a non-exclusive 14-day streaming license for Twitch live streams and VODs. "
    "I confirm the content complies with the community content guidelines."
)


class ExpRadioTermsView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=120)
        self.bot = bot

    @discord.ui.button(label="✅ I Agree & Submit", style=discord.ButtonStyle.green)
    async def agree(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ExpRadioSubmitModal(self.bot))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Submission cancelled.", embed=None, view=None)

    async def on_timeout(self):
        pass


class ExpRadioSubmitModal(discord.ui.Modal, title="Submit to Experimental Radio"):
    url = discord.ui.TextInput(
        label="Suno Song URL",
        placeholder="https://suno.com/s/… or https://suno.com/song/…",
        max_length=250,
    )

    def __init__(self, bot):
        super().__init__()
        self.bot = bot

    async def on_submit(self, interaction: discord.Interaction):
        import hashlib
        await interaction.response.defer(ephemeral=True)
        raw_url = self.url.value.strip()

        if not SUNO_URL_PATTERN.search(raw_url):
            await interaction.followup.send(
                "❌ Invalid URL. Please provide a valid Suno song link (e.g. `https://suno.com/s/…`).",
                ephemeral=True,
            )
            return

        m = _SUNO_SUBMIT_RE.search(raw_url)
        if not m:
            await interaction.followup.send("❌ Could not extract song ID from URL.", ephemeral=True)
            return
        suno_uuid = m.group(1)

        count = await self.bot.db.count_exp_radio_songs_by_user(interaction.user.id)
        if count >= _EXP_MAX_PER_USER:
            await interaction.followup.send(
                f"❌ You already have {_EXP_MAX_PER_USER} submissions. "
                "Use `/twitch-delete` to remove one first.",
                ephemeral=True,
            )
            return

        rights_hash = hashlib.sha256(
            f"{_EXP_RIGHTS_DECLARATION}|{interaction.user.id}|{raw_url}|{suno_uuid}".encode()
        ).hexdigest()

        song_id, upload_token = await self.bot.db.add_exp_radio_song(
            user_id=interaction.user.id,
            user_name=str(interaction.user),
            suno_url=raw_url,
            suno_uuid=suno_uuid,
            rights_declaration=_EXP_RIGHTS_DECLARATION,
            rights_hash=rights_hash,
        )

        web_url = self.bot.web_url
        if web_url:
            upload_link = f"{web_url}/exp-radio/upload/{upload_token}"
            view = discord.ui.View()
            view.add_item(discord.ui.Button(
                label="⬆  Complete Submission — Upload Song",
                url=upload_link,
                style=discord.ButtonStyle.link,
            ))
            await interaction.followup.send(
                f"✅ **Song registered!** (#{song_id})\n\n"
                "**⚠️ One more step required:**\n"
                "Click the button below to transfer the audio from Suno to the server.\n"
                "*(Takes ~10 seconds — the page does it automatically.)*",
                view=view,
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"✅ **Song registered!** (#{song_id})\n\n"
                "⚠️ Upload link unavailable — ask an admin to set `WEB_URL`.",
                ephemeral=True,
            )

        # Pipeline is triggered by the upload endpoint once the MP3 arrives


class ExpRadioDeleteView(discord.ui.View):
    def __init__(self, bot, songs: list):
        super().__init__(timeout=60)
        self.bot = bot
        select = discord.ui.Select(
            placeholder="Select a song to delete…",
            options=[
                discord.SelectOption(
                    label=f"#{s['id']}: {(s['title'] or 'Pending')[:60]}",
                    description=(s['artist'] or '')[:80],
                    value=str(s["id"]),
                )
                for s in songs[:25]
            ],
        )
        select.callback = self._select_cb
        self.add_item(select)

    async def _select_cb(self, interaction: discord.Interaction):
        song_id = int(interaction.data["values"][0])
        data = await self.bot.db.delete_exp_radio_song(song_id)
        if data:
            title = data.get("title") or f"#{song_id}"
            await interaction.response.edit_message(
                content=f"🗑️ **{title}** has been removed from the Experimental Radio.",
                view=None,
            )
        else:
            await interaction.response.edit_message(content="Song not found.", view=None)

    async def on_timeout(self):
        pass


NUMBER_EMOJIS = ["1\u20e3", "2\u20e3", "3\u20e3", "4\u20e3", "5\u20e3", "6\u20e3", "7\u20e3", "8\u20e3", "9\u20e3", "\U0001F51F"]


class PollOptionsModal(discord.ui.Modal, title="Create Poll"):
    """Modal for creating a poll via slash command."""

    poll_title = discord.ui.TextInput(label="Title", placeholder="What should we play next?", max_length=100)
    description = discord.ui.TextInput(label="Description (optional)", placeholder="Optional context...", required=False, style=discord.TextStyle.paragraph)
    options = discord.ui.TextInput(label="Options (one per line, 2-10)", placeholder="Option 1\nOption 2\nOption 3", style=discord.TextStyle.paragraph)

    def __init__(self, bot, channel, creator_id: int = None):
        super().__init__()
        self.bot = bot
        self.target_channel = channel
        self.creator_id = creator_id

    async def on_submit(self, interaction: discord.Interaction):
        import json
        options_list = [o.strip() for o in self.options.value.split("\n") if o.strip()]
        if len(options_list) < 2:
            await interaction.response.send_message("At least 2 options are required.", ephemeral=True)
            return
        if len(options_list) > 10:
            await interaction.response.send_message("Maximum 10 options allowed.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            poll_id = await self.bot.db.create_poll(
                str(self.poll_title.value).strip(),
                str(self.description.value or "").strip(),
                json.dumps(options_list),
                creator_id=self.creator_id,
            )

            options_text = "\n".join(f"{NUMBER_EMOJIS[i]}  {opt}" for i, opt in enumerate(options_list))
            desc = str(self.description.value or "").strip()
            embed = discord.Embed(
                title=f"\U0001F4CA {self.poll_title.value}",
                description=f"{desc}\n\n{options_text}" if desc else options_text,
                color=discord.Color.blue(),
            )
            bot_name = await self.bot.db.get_setting("bot_name") or "Slowmode Bot"
            embed.set_footer(text=f"{bot_name} — Poll")
            if isinstance(self.target_channel, discord.ForumChannel):
                thread, msg = await self.target_channel.create_thread(
                    name=f"\U0001F4CA {self.poll_title.value}",
                    embed=embed,
                )
            else:
                msg = await self.target_channel.send(embed=embed)
            for i in range(len(options_list)):
                await msg.add_reaction(NUMBER_EMOJIS[i])
            await self.bot.db.update_poll_message(poll_id, self.target_channel.id, msg.id)
            await interaction.followup.send(f"Poll #{poll_id} posted to #{self.target_channel.name}!", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Error creating poll: {e}", ephemeral=True)


class PollEditModal(discord.ui.Modal, title="Edit Poll"):
    """Modal for editing an existing poll."""

    poll_title = discord.ui.TextInput(label="Title", max_length=100)
    description = discord.ui.TextInput(label="Description (optional)", required=False, style=discord.TextStyle.paragraph)
    options = discord.ui.TextInput(label="Options (one per line, 2-10)", style=discord.TextStyle.paragraph)

    def __init__(self, bot, poll_id: int, old_title: str, old_desc: str, old_options: list):
        super().__init__()
        self.bot = bot
        self.poll_id = poll_id
        self.poll_title.default = old_title
        self.description.default = old_desc or ""
        self.options.default = "\n".join(old_options)

    async def on_submit(self, interaction: discord.Interaction):
        import json
        options_list = [o.strip() for o in self.options.value.split("\n") if o.strip()]
        if len(options_list) < 2:
            await interaction.response.send_message("At least 2 options are required.", ephemeral=True)
            return
        if len(options_list) > 10:
            await interaction.response.send_message("Maximum 10 options allowed.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            await self.bot.db.update_poll(
                self.poll_id,
                str(self.poll_title.value).strip(),
                str(self.description.value or "").strip(),
                json.dumps(options_list),
            )

            poll = await self.bot.db.get_poll(self.poll_id)
            if poll and poll.get("message_id") and poll.get("channel_id"):
                try:
                    guild = interaction.guild
                    channel = guild.get_channel(poll["channel_id"]) or guild.get_thread(poll["channel_id"])
                    if channel:
                        msg = await channel.fetch_message(poll["message_id"])
                        options_text = "\n".join(f"{NUMBER_EMOJIS[i]}  {opt}" for i, opt in enumerate(options_list))
                        desc = str(self.description.value or "").strip()
                        embed = discord.Embed(
                            title=f"\U0001F4CA {self.poll_title.value}",
                            description=f"{desc}\n\n{options_text}" if desc else options_text,
                            color=discord.Color.blue(),
                        )
                        bot_name = await self.bot.db.get_setting("bot_name") or "Slowmode Bot"
                        embed.set_footer(text=f"{bot_name} — Poll")
                        await msg.edit(embed=embed)
                except Exception:
                    pass

            await interaction.followup.send(f"Poll #{self.poll_id} updated.", ephemeral=True)
        except Exception as e:
            await interaction.followup.send(f"Error updating poll: {e}", ephemeral=True)


class PollSelectView(discord.ui.View):
    """View with a dropdown to select a poll, then Edit/Delete buttons."""

    def __init__(self, bot, polls: list):
        super().__init__(timeout=120)
        self.bot = bot
        self.selected_poll_id = None
        select = discord.ui.Select(placeholder="Select a poll...")
        for p in polls[:25]:
            label = f"#{p['id']}: {p['title'][:80]}"
            select.add_option(label=label, value=str(p["id"]))
        select.callback = self.select_callback
        self.add_item(select)

    async def select_callback(self, interaction: discord.Interaction):
        import json
        poll_id = int(interaction.data["values"][0])
        poll = await self.bot.db.get_poll(poll_id)
        if not poll:
            await interaction.response.send_message("Poll not found.", ephemeral=True)
            return
        self.selected_poll_id = poll_id
        options_list = json.loads(poll["options"])
        options_preview = "\n".join(f"{i+1}. {o}" for i, o in enumerate(options_list))
        action_view = PollActionView(self.bot, poll_id, poll["title"], poll.get("description", ""), options_list)
        await interaction.response.edit_message(
            content=f"**#{poll_id}: {poll['title']}**\n{options_preview}\n\nChoose an action:",
            view=action_view,
        )


class PollActionView(discord.ui.View):
    """Edit or Delete buttons for a selected poll."""

    def __init__(self, bot, poll_id: int, title: str, desc: str, options_list: list):
        super().__init__(timeout=120)
        self.bot = bot
        self.poll_id = poll_id
        self.title = title
        self.desc = desc
        self.options_list = options_list

    @discord.ui.button(label="Edit", style=discord.ButtonStyle.primary)
    async def edit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = PollEditModal(self.bot, self.poll_id, self.title, self.desc, self.options_list)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger)
    async def delete_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        confirm_view = PollDeleteConfirmView(self.bot, self.poll_id)
        await interaction.response.edit_message(
            content=f"Are you sure you want to delete poll **#{self.poll_id}: {self.title}**?",
            view=confirm_view,
        )


class PollDeleteConfirmView(discord.ui.View):
    """Confirmation view for deleting a poll."""

    def __init__(self, bot, poll_id: int):
        super().__init__(timeout=60)
        self.bot = bot
        self.poll_id = poll_id

    @discord.ui.button(label="Yes, delete", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        import os
        filename = await self.bot.db.delete_poll(self.poll_id)
        if filename:
            upload_dir = os.path.join(os.path.dirname(self.bot.db.db_path), "uploads")
            filepath = os.path.join(upload_dir, filename)
            if os.path.exists(filepath):
                os.remove(filepath)
        await interaction.response.edit_message(content=f"Poll #{self.poll_id} deleted.", view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Deletion cancelled.", view=None)


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
