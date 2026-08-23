"""Discord side of the TrYa DCS live-chat bridge."""

import re

import discord
from discord.ext import commands

from bot.relic_hunt import RelicHunt
from bot.trya_dcs_events import trya_dcs_events
from bot.trya_dcs_web_chat import (
    forget_web_chat_fingerprint,
    neutralize_mass_mentions,
    parse_web_chat_payload,
    register_web_chat_fingerprint,
)


_WEBHOOK_NAME = "TrYa DCS Web Chat"
_USER_MENTION_RE = re.compile(r"<@!?(\d{17,20})>")


def _avatar_url(author) -> str:
    avatar = getattr(author, "display_avatar", None)
    return str(avatar.url) if avatar else ""


def is_public_channel_message(message: discord.Message) -> bool:
    """Skip Discord-only ephemeral interaction replies.

    Translate and similar commands are ephemeral in the channel, but the bot
    still receives them on the gateway. Relaying those would show private
    bot feedback to every DCS web listener.
    """
    flags = getattr(message, "flags", None)
    if flags is not None and getattr(flags, "ephemeral", False):
        return False
    return True


def _mention_entry(user) -> dict:
    mention_color = ""
    if isinstance(user, discord.Member) and user.color.value:
        mention_color = f"#{user.color.value:06x}"
    return {
        "id": str(user.id),
        "display_name": getattr(user, "display_name", None) or user.name,
        "role_color": mention_color,
    }


async def _serialize_mentions(message: discord.Message) -> list[dict]:
    """Resolve <@id> mentions even when Discord omits them on webhook messages."""
    by_id: dict[str, dict] = {}
    for mentioned in message.mentions:
        by_id[str(mentioned.id)] = _mention_entry(mentioned)

    ids = {str(user_id) for user_id in getattr(message, "raw_mentions", []) or []}
    ids.update(_USER_MENTION_RE.findall(message.content or ""))
    guild = message.guild
    for raw_id in ids:
        if raw_id in by_id:
            continue
        member = None
        if guild is not None:
            try:
                user_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            member = guild.get_member(user_id)
            if member is None:
                try:
                    member = await guild.fetch_member(user_id)
                except (discord.HTTPException, discord.NotFound, discord.Forbidden):
                    member = None
        if member is not None:
            by_id[raw_id] = _mention_entry(member)

    return list(by_id.values())


async def serialize_discord_message(message: discord.Message) -> dict:
    author = message.author
    role_color = ""
    if isinstance(author, discord.Member) and author.color.value:
        role_color = f"#{author.color.value:06x}"

    reply = None
    reference = message.reference
    if reference and reference.message_id:
        resolved = reference.resolved
        if not isinstance(resolved, discord.Message):
            try:
                resolved = await message.channel.fetch_message(reference.message_id)
            except (discord.HTTPException, discord.NotFound, discord.Forbidden):
                resolved = None
        if isinstance(resolved, discord.Message):
            reply = {
                "message_id": str(resolved.id),
                "author": getattr(resolved.author, "display_name", resolved.author.name),
                "content": (resolved.content or "")[:240],
            }

    attachments = []
    for item in message.attachments:
        content_type = item.content_type or ""
        attachments.append({
            "id": str(item.id),
            "filename": item.filename,
            "url": item.url,
            "content_type": content_type,
            "is_image": content_type.startswith("image/"),
        })

    mentions = await _serialize_mentions(message)

    embeds = []
    for embed in message.embeds:
        image_url = ""
        if embed.image and embed.image.url:
            image_url = embed.image.url
        elif embed.thumbnail and embed.thumbnail.url:
            image_url = embed.thumbnail.url
        embeds.append({
            "title": embed.title or "",
            "description": (embed.description or "")[:1000],
            "url": embed.url or "",
            "image_url": image_url,
        })

    return {
        "message_id": str(message.id),
        "channel_id": str(message.channel.id),
        "author_id": str(author.id),
        "display_name": getattr(author, "display_name", author.name),
        "avatar_url": _avatar_url(author),
        "role_color": role_color,
        "content": message.content or "",
        "created_at": message.created_at.timestamp(),
        "edited_at": message.edited_at.timestamp() if message.edited_at else None,
        "web_origin": bool(message.webhook_id),
        "reply": reply,
        "mentions": mentions,
        "attachments": attachments,
        "embeds": embeds,
    }


async def guild_emoji_payload(guild: discord.Guild | None) -> list[dict]:
    if guild is None:
        return []
    return [
        {
            "id": str(emoji.id),
            "name": emoji.name,
            "animated": emoji.animated,
            "available": emoji.available,
            "markup": f"<{'a' if emoji.animated else ''}:{emoji.name}:{emoji.id}>",
            "url": str(emoji.url),
        }
        for emoji in guild.emojis
        if emoji.available
    ]


async def _resolve_guild_member(guild: discord.Guild, user_id: int):
    member = guild.get_member(user_id)
    if member is not None:
        return member
    try:
        return await guild.fetch_member(user_id)
    except (discord.HTTPException, discord.NotFound, discord.Forbidden):
        return None


async def prepare_web_chat_content(
    guild: discord.Guild,
    content: str,
    mention_ids: list | None = None,
) -> tuple[str, list]:
    """Keep only current guild-member mentions and insert Discord mention markup."""
    clean = neutralize_mass_mentions(str(content or "").strip())[:1800]
    allowed: list = []
    seen: set[int] = set()

    async def allow(user_id: int):
        if user_id in seen:
            return
        member = await _resolve_guild_member(guild, user_id)
        if member is None or getattr(member, "bot", False):
            return
        seen.add(user_id)
        allowed.append(member)

    for match in _USER_MENTION_RE.finditer(clean):
        await allow(int(match.group(1)))

    for raw_id in mention_ids or []:
        try:
            user_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        member = await _resolve_guild_member(guild, user_id)
        if member is None or getattr(member, "bot", False):
            continue
        await allow(user_id)
        mention = f"<@{user_id}>"
        if mention in clean:
            continue
        for token in (f"@{member.display_name}", f"@{member.name}"):
            if token and token in clean:
                clean = clean.replace(token, mention, 1)
                break
        else:
            clean = f"{mention} {clean}".strip()

    def keep_user_mention(match: re.Match) -> str:
        user_id = int(match.group(1))
        if user_id in seen:
            return f"<@{user_id}>"
        return match.group(0).replace("@", "@\u200b", 1)

    clean = _USER_MENTION_RE.sub(keep_user_mention, clean).strip()[:1800]
    return clean, allowed


async def send_web_chat_message(
    bot,
    channel_id: int,
    user_id: int,
    content: str,
    *,
    reply_to: str | int | None = None,
    mention_ids: list | None = None,
) -> None:
    """Send as the authenticated member through a clearly marked webhook."""
    channel = bot.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        channel = await bot.fetch_channel(channel_id)
    guild = channel.guild
    member = guild.get_member(user_id)
    if member is None:
        member = await guild.fetch_member(user_id)

    clean, mentioned = await prepare_web_chat_content(guild, content, mention_ids)
    if not clean:
        raise ValueError("Message is empty.")
    command_text = clean
    if not register_web_chat_fingerprint(user_id, command_text, reply_to):
        print(f"[trya-dcs-chat] Dropped duplicate web message from {user_id}", flush=True)
        return

    reference = None
    if reply_to:
        try:
            reference = await channel.fetch_message(int(reply_to))
        except (TypeError, ValueError, discord.HTTPException, discord.NotFound, discord.Forbidden):
            reference = None
        if reference is not None and reference.channel.id != channel.id:
            reference = None
        if reference is not None:
            snippet = re.sub(r"\s+", " ", reference.content or "").strip()[:80]
            author_name = getattr(reference.author, "display_name", reference.author.name)
            quote = f"> **{author_name}:** {snippet}" if snippet else f"> **{author_name}**"
            clean = f"{quote}\n{clean}"[:1800]

    allowed_mentions = discord.AllowedMentions(
        everyone=False,
        roles=False,
        users=mentioned,
        replied_user=False,
    )

    webhook = None
    try:
        webhooks = await channel.webhooks()
        webhook = next(
            (item for item in webhooks if item.name == _WEBHOOK_NAME and item.user == bot.user),
            None,
        )
        if webhook is None:
            webhook = await channel.create_webhook(name=_WEBHOOK_NAME, reason="TrYa DCS web chat")
    except (discord.Forbidden, discord.HTTPException):
        webhook = None

    try:
        if webhook is not None:
            await webhook.send(
                clean,
                username=f"{member.display_name} · Web"[:80],
                avatar_url=_avatar_url(member) or None,
                allowed_mentions=allowed_mentions,
                wait=True,
            )
        else:
            send_kwargs = {
                "content": f"**{member.display_name} · Web:** {clean}",
                "allowed_mentions": allowed_mentions,
            }
            if reference is not None:
                send_kwargs["reference"] = reference
            await channel.send(**send_kwargs)
    except Exception:
        forget_web_chat_fingerprint(user_id, command_text, reply_to)
        raise

    cog = bot.get_cog("TryaDcsChat")
    if cog:
        await cog.process_web_relic_command(member, channel, command_text)


class TryaDcsChat(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.relic_hunt = RelicHunt(bot.db, stream_kind="dcs")

    async def cog_load(self) -> None:
        await self.relic_hunt.prepare()

    async def _configured_channel_id(self) -> int:
        try:
            return int(await self.bot.db.get_setting("trya_dcs_chat_channel_id") or 0)
        except (TypeError, ValueError):
            return 0

    async def _relic_enabled(self) -> bool:
        return (
            (await self.bot.db.get_setting("trya_dcs_relic_hunt_enabled") or "on") == "on"
            and (await self.bot.db.relic_get_setting("enabled")) != "false"
        )

    async def _dispatch_relic_command(
        self,
        member: discord.Member,
        channel,
        content: str,
    ) -> bool:
        if not await self._relic_enabled():
            return False
        permissions = getattr(member, "guild_permissions", None)
        is_staff = bool(
            permissions
            and (
                permissions.administrator
                or permissions.manage_guild
                or permissions.manage_messages
            )
        )

        feedback = []

        async def capture(response: str) -> None:
            feedback.append(str(response)[:1950])

        async def send_custom(response: str) -> None:
            await channel.send(
                str(response)[:1950],
                allowed_mentions=discord.AllowedMentions.none(),
            )

        relic_user_id = f"discord:{member.id}"
        handled = await self.relic_hunt.dispatch_message(
            content,
            {
                "username": member.display_name,
                "user_id": relic_user_id,
                "is_mod": is_staff,
                "is_sub": True,
                "is_vip": False,
                "is_broadcaster": bool(
                    permissions and permissions.administrator
                ),
                "tags": {"transport": "discord"},
            },
            capture,
            custom_sender=send_custom,
        )
        if handled:
            now = discord.utils.utcnow().timestamp()
            recent = await self.bot.db.relic_get_recent_log(20)
            already_logged = any(
                row.get("twitch_user_id") == relic_user_id
                and now - float(row.get("created_at") or 0) < 8
                and (row.get("result_type") or "") != "dcs_feedback"
                for row in recent
            )
            # Combine/find/ritual already write a hunt log. A second
            # dcs_feedback row would duplicate the same action in the player.
            if not already_logged:
                existing = {
                    row.get("message")
                    for row in recent
                    if row.get("twitch_user_id") == relic_user_id
                    and now - float(row.get("created_at") or 0) < 8
                }
                command = (str(content or "").split(None, 1)[0] or "command").lower()
                for response in feedback:
                    if response in existing:
                        continue
                    await self.bot.db.relic_log_hunt({
                        "twitch_user_id": relic_user_id,
                        "username": member.display_name,
                        "item_id": None,
                        "item_name": command,
                        "rarity": "info",
                        "points_awarded": 0,
                        "xp_awarded": 0,
                        "result_type": "dcs_feedback",
                        "message": response,
                        "created_at": now,
                    })
            await trya_dcs_events.publish(
                "relic.update",
                {"user_id": str(member.id), "updated_at": now},
            )
        return handled

    async def process_web_relic_command(
        self,
        member: discord.Member,
        channel,
        content: str,
    ) -> bool:
        return await self._dispatch_relic_command(member, channel, content)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        channel_id = await self._configured_channel_id()
        if not channel_id or message.channel.id != channel_id:
            return
        if not is_public_channel_message(message):
            return
        await trya_dcs_events.publish(
            "chat.message", await serialize_discord_message(message)
        )
        if message.webhook_id or message.author.bot:
            return
        member = message.author
        if not isinstance(member, discord.Member):
            member = message.guild.get_member(message.author.id) if message.guild else None
        if member:
            await self._dispatch_relic_command(member, message.channel, message.content)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        channel_id = await self._configured_channel_id()
        if not channel_id or payload.channel_id != channel_id:
            return
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            return
        try:
            message = await channel.fetch_message(payload.message_id)
            if not is_public_channel_message(message):
                return
            await trya_dcs_events.publish(
                "chat.edit", await serialize_discord_message(message)
            )
        except (discord.HTTPException, discord.NotFound, discord.Forbidden):
            pass

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        channel_id = await self._configured_channel_id()
        if channel_id and payload.channel_id == channel_id:
            await trya_dcs_events.publish(
                "chat.delete", {"message_id": str(payload.message_id)}
            )


async def setup(bot):
    await bot.add_cog(TryaDcsChat(bot))
