import asyncio
import re
import time
import math
import discord
from discord.ext import commands

from bot.suno_urls import resolve_suno_uuid

SUNO_URL_PATTERN = re.compile(r'https://suno\.com/(?:s|song)/[\w-]+')


class SlowmodeCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _resolve_song_uuid(self, url: str):
        try:
            suno_uuid = await resolve_suno_uuid(url)
            if suno_uuid:
                await self.bot.db.set_song_uuid(url, suno_uuid)
        except Exception as exc:
            print(f"[song-rating] UUID resolution failed for {url}: {exc}", flush=True)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        if not message.guild:
            return

        db = self.bot.db
        try:
            await db.record_user_activity(
                user_id=message.author.id,
                user_name=str(message.author),
                activity_type="Message",
                summary=f"Message in #{getattr(message.channel, 'name', 'unknown-channel')}",
                channel_id=message.channel.id,
                channel_name=getattr(message.channel, "name", None),
                timestamp=message.created_at.timestamp(),
            )
        except Exception:
            pass

        channel_config = await db.get_monitored_channel(message.channel.id)

        if not channel_config:
            return

        if not channel_config["enabled"]:
            return

        # Track Suno song URLs for statistics
        suno_urls = SUNO_URL_PATTERN.findall(message.content)
        if suno_urls:
            # Extract song title from embed
            song_title = None
            for embed in message.embeds:
                if embed.title:
                    song_title = embed.title
                    break
            for url in suno_urls:
                try:
                    await db.add_song_post(
                        channel_id=message.channel.id,
                        user_id=message.author.id,
                        user_name=str(message.author),
                        url=url,
                        posted_at=message.created_at.timestamp(),
                        message_id=message.id,
                        song_title=song_title,
                    )
                except Exception:
                    pass

                asyncio.create_task(self._resolve_song_uuid(url))

            # ── LLM-based lyric moderation (new admin-tab feature) ───────
            # Fire-and-forget per URL. The screener internally:
            #   - dedups against the (message_id, url) history,
            #   - rate-limits via a Semaphore,
            #   - silently no-ops if moderation is globally disabled,
            #   - posts to the configured report channel only on 'flagged'.
            try:
                moderation_enabled = await db.get_setting("channel_moderation_enabled") or "off"
                if moderation_enabled == "on":
                    from bot.channel_moderation import dispatch as _chmod_dispatch
                    for url in suno_urls:
                        _chmod_dispatch(
                            self.bot,
                            message_id=message.id,
                            channel_id=message.channel.id,
                            channel_name=message.channel.name,
                            user_id=message.author.id,
                            user_name=str(message.author),
                            suno_url=url,
                            jump_url=message.jump_url,
                        )
            except Exception as e:
                print(f"[chmod] dispatch error: {e}", flush=True)

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

        # Clean up song_posts entry so deleted songs don't appear in /new
        try:
            await self.bot.db.delete_song_posts_by_message_id(message.id)
        except Exception:
            pass

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


    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        """Remove song_posts entries when a tracked message is deleted manually or by mods."""
        try:
            await self.bot.db.delete_song_posts_by_message_id(payload.message_id)
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        """Clean up song_posts for bulk-deleted messages."""
        for mid in payload.message_ids:
            try:
                await self.bot.db.delete_song_posts_by_message_id(mid)
            except Exception:
                pass

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if not payload.guild_id or payload.member.bot:
            return

        db = self.bot.db
        channel_config = await db.get_monitored_channel(payload.channel_id)
        if not channel_config:
            return

        # Helper: extract song title from message embeds
        def _extract_title(msg):
            for embed in msg.embeds:
                if embed.title:
                    return embed.title
            return None

        # Check if this message is a known song post
        song_post = await db.get_song_post_by_message_id(payload.message_id)
        if song_post:
            emoji_str = str(payload.emoji)
            # Try to get song title from the message embed
            song_title = None
            channel = self.bot.get_channel(payload.channel_id)
            if channel:
                try:
                    message = await channel.fetch_message(payload.message_id)
                    song_title = _extract_title(message)
                except (discord.NotFound, discord.Forbidden):
                    pass
            try:
                await db.add_song_reaction(
                    message_id=payload.message_id,
                    channel_id=payload.channel_id,
                    song_url=song_post["url"],
                    post_author_id=song_post["user_id"],
                    reactor_user_id=payload.user_id,
                    reactor_user_name=str(payload.member),
                    emoji=emoji_str,
                    song_title=song_title,
                )
            except Exception:
                pass
            return

        # Message not in DB — check if it contains a Suno URL
        channel = self.bot.get_channel(payload.channel_id)
        if not channel:
            return
        try:
            message = await channel.fetch_message(payload.message_id)
        except (discord.NotFound, discord.Forbidden):
            return

        if message.author.bot:
            return

        urls = SUNO_URL_PATTERN.findall(message.content)
        if not urls:
            return

        # It's a song post — store the reaction
        emoji_str = str(payload.emoji)
        song_title = _extract_title(message)
        for url in urls:
            try:
                await db.add_song_reaction(
                    message_id=payload.message_id,
                    channel_id=payload.channel_id,
                    song_url=url,
                    post_author_id=message.author.id,
                    reactor_user_id=payload.user_id,
                    reactor_user_name=str(payload.member),
                    emoji=emoji_str,
                    song_title=song_title,
                )
            except Exception:
                pass

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if not payload.guild_id:
            return

        db = self.bot.db
        channel_config = await db.get_monitored_channel(payload.channel_id)
        if not channel_config:
            return

        emoji_str = str(payload.emoji)
        try:
            await db.remove_song_reaction(
                message_id=payload.message_id,
                reactor_user_id=payload.user_id,
                emoji=emoji_str,
            )
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        """Send welcome message when a new member joins."""
        if member.bot:
            return

        db = self.bot.db
        config = await db.get_welcome_config()

        if not config["enabled"]:
            return

        # Prepare placeholders
        user_mention = member.mention
        user_name = member.display_name

        # Send to welcome channel
        if config["channel_id"]:
            channel = member.guild.get_channel(config["channel_id"])
            if channel:
                try:
                    message = config["message_text"].format(
                        user=user_mention,
                        username=user_name,
                    )
                    await channel.send(message)
                except Exception as e:
                    print(f"[welcome] Failed to send channel message: {e}")

        # Send DM
        if config["dm_enabled"]:
            try:
                dm_message = config["dm_text"].format(
                    user=user_mention,
                    username=user_name,
                )
                await member.send(dm_message)
            except Exception as e:
                print(f"[welcome] Failed to send DM to {member.id}: {e}")


async def setup(bot):
    await bot.add_cog(SlowmodeCog(bot))
