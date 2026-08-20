"""Discord side of the TrYa DCS live-chat bridge."""

import discord
from discord.ext import commands

from bot.trya_dcs_events import trya_dcs_events


_WEBHOOK_NAME = "TrYa DCS Web Chat"


def _avatar_url(author) -> str:
    avatar = getattr(author, "display_avatar", None)
    return str(avatar.url) if avatar else ""


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


async def send_web_chat_message(bot, channel_id: int, user_id: int, content: str) -> None:
    """Send as the authenticated member through a clearly marked webhook."""
    channel = bot.get_channel(channel_id)
    if not isinstance(channel, discord.TextChannel):
        channel = await bot.fetch_channel(channel_id)
    guild = channel.guild
    member = guild.get_member(user_id)
    if member is None:
        member = await guild.fetch_member(user_id)

    clean = str(content or "").strip()
    if not clean:
        raise ValueError("Message is empty.")
    clean = clean[:1800]
    allowed_mentions = discord.AllowedMentions.none()

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

    if webhook is not None:
        await webhook.send(
            clean,
            username=f"{member.display_name} · Web"[:80],
            avatar_url=_avatar_url(member) or None,
            allowed_mentions=allowed_mentions,
            wait=True,
        )
        return

    await channel.send(
        f"**{member.display_name} · Web:** {clean}",
        allowed_mentions=allowed_mentions,
    )


class TryaDcsChat(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _configured_channel_id(self) -> int:
        try:
            return int(await self.bot.db.get_setting("trya_dcs_chat_channel_id") or 0)
        except (TypeError, ValueError):
            return 0

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        channel_id = await self._configured_channel_id()
        if not channel_id or message.channel.id != channel_id:
            return
        await trya_dcs_events.publish(
            "chat.message", await serialize_discord_message(message)
        )

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
