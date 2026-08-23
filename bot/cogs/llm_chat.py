"""Corax LLM chat cog.

Triggers:
- Direct @mention of the bot in a monitored channel, OR
- Reply to a message the bot authored.

Never responds to other messages. Honours admin config for:
- global enable flag
- allowed channels
- allowed roles (empty = everyone in allowed channels)
- per-user / per-channel rate limits
- whitelisted tools
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks

from bot.llm import (
    DEFAULT_PERSONA,
    OllamaClient,
    run_corax_turn,
)
from config import Config


# Detect when the user explicitly asks for N results.
_NUMBER_RE = re.compile(
    r"\b(\d{1,3})\s*(?:songs?|lieder|st[üu]cke|tracks?|ergebnisse?|results?)\b",
    re.IGNORECASE,
)


class SongCarouselView(discord.ui.View):
    """Pagination carousel: one song per page, prev/next buttons."""

    def __init__(self, songs: list[dict], invoker_id: int, timeout: float = 300.0):
        super().__init__(timeout=timeout)
        self.songs = songs
        self.invoker_id = invoker_id
        self.index = 0
        self._update_buttons()

    def _update_buttons(self):
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                if child.custom_id == "prev":
                    child.disabled = self.index <= 0
                elif child.custom_id == "next":
                    child.disabled = self.index >= len(self.songs) - 1

    def current_embed(self) -> discord.Embed:
        s = self.songs[self.index]
        title = s.get("title") or "Unknown title"
        url = s.get("url")
        e = discord.Embed(title=title, url=url, color=0x7c3aed)
        meta = []
        if s.get("artist"):
            meta.append(f"by {s['artist']}")
        if s.get("posted_by"):
            meta.append(f"shared by {s['posted_by']}")
        if s.get("posted_at"):
            try:
                dt = datetime.fromtimestamp(float(s["posted_at"]), tz=timezone.utc)
                meta.append(f"<t:{int(dt.timestamp())}:R>")
            except Exception:
                pass
        if meta:
            e.description = " · ".join(meta)
        if s.get("reactions"):
            e.add_field(name="Reactions", value=str(s["reactions"]), inline=True)
        e.set_footer(text=f"Song {self.index + 1} / {len(self.songs)}")
        return e

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message(
                "Nur die Person, die Corax angefragt hat, kann blättern.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, custom_id="prev")
    async def prev_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if self.index > 0:
            self.index -= 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary, custom_id="next")
    async def next_btn(self, interaction: discord.Interaction, _button: discord.ui.Button):
        if self.index < len(self.songs) - 1:
            self.index += 1
        self._update_buttons()
        await interaction.response.edit_message(embed=self.current_embed(), view=self)


class LLMChatCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.client = OllamaClient(
            base_url=Config.OLLAMA_URL,
            model=Config.LLM_MODEL,
            timeout=Config.LLM_REQUEST_TIMEOUT,
        )
        self.retention_task.start()

    def cog_unload(self):
        self.retention_task.cancel()

    # ---------------------------------------------------------------- listener

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot:
            return

        # Manual Corax conversations use Discord DMs and remain available even
        # while automatic LLM replies are disabled. The web console sends the
        # outbound half; this listener records user replies.
        if not message.guild:
            guild = self.bot.get_guild(Config.GUILD_ID)
            member = guild.get_member(message.author.id) if guild else None
            if guild and member is None:
                try:
                    member = await guild.fetch_member(message.author.id)
                except Exception:
                    member = None
            if member is None:
                return
            content = (message.content or "").strip()
            if message.attachments:
                attachment_lines = [
                    f"Attachment: {item.filename} ({item.url})"
                    for item in message.attachments
                ]
                content = "\n".join(part for part in [content, *attachment_lines] if part)
            if content:
                try:
                    await self.bot.db.add_corax_dm_message(
                        user_id=message.author.id,
                        user_name=str(member),
                        direction="inbound",
                        content=content,
                        timestamp=message.created_at.timestamp(),
                        discord_message_id=message.id,
                    )
                except Exception as exc:
                    print(
                        f"[corax-dm] Could not store message from "
                        f"{message.author.id}: {exc}",
                        flush=True,
                    )
            return

        me = message.guild.me or self.bot.user
        if not me:
            return

        # Mentioned directly, or replied to a bot message.
        mentioned = me in message.mentions
        replied_to_bot = False
        if not mentioned and message.reference and message.reference.message_id:
            try:
                ref = await message.channel.fetch_message(message.reference.message_id)
                replied_to_bot = ref.author.id == me.id
            except Exception:
                pass
        if not mentioned and not replied_to_bot:
            return

        db = self.bot.db
        cfg = await db.get_llm_config()
        if not cfg or not cfg.get("enabled"):
            return

        # Channel allow-list.
        if not await db.is_llm_channel_allowed(message.channel.id):
            return

        # Role allow-list (empty = everyone in allowed channels).
        allowed_role_ids = await db.get_llm_allowed_role_ids()
        if allowed_role_ids:
            user_role_ids = {r.id for r in getattr(message.author, "roles", [])}
            if not (user_role_ids & allowed_role_ids):
                await db.log_llm_interaction(
                    user_id=message.author.id,
                    user_name=str(message.author),
                    channel_id=message.channel.id,
                    blocked=True,
                    block_reason="role_not_allowed",
                )
                return

        # Rate limits (per minute, blocked interactions don't count).
        now = time.time()
        since = now - 60.0
        u_cnt = await db.count_llm_user_recent(message.author.id, since)
        if u_cnt >= int(cfg.get("rate_per_user_min") or 3):
            await db.log_llm_interaction(
                user_id=message.author.id,
                user_name=str(message.author),
                channel_id=message.channel.id,
                blocked=True,
                block_reason="rate_user",
            )
            try:
                await message.reply(
                    "Kurze Pause bitte – du hast gerade dein Limit erreicht. 🐦‍⬛",
                    mention_author=False,
                )
            except Exception:
                pass
            return
        c_cnt = await db.count_llm_channel_recent(message.channel.id, since)
        if c_cnt >= int(cfg.get("rate_per_channel_min") or 10):
            await db.log_llm_interaction(
                user_id=message.author.id,
                user_name=str(message.author),
                channel_id=message.channel.id,
                blocked=True,
                block_reason="rate_channel",
            )
            return

        # Clean the prompt: strip the bot mention.
        prompt = re.sub(rf"<@!?{me.id}>", "", message.content).strip()
        if not prompt:
            prompt = "Sag hi."

        # Collect non-bot Discord mentions so the LLM can reference them by ID.
        mentioned_users = []
        for m in message.mentions:
            if m.bot or m.id == me.id:
                continue
            mentioned_users.append({
                "id": str(m.id),
                "name": str(m),
                "display": getattr(m, "display_name", None) or m.name,
            })

        # Explicit #channel references in the message → hard filter.
        mentioned_channels = [
            {"id": str(ch.id), "name": ch.name}
            for ch in getattr(message, "channel_mentions", []) or []
        ]

        # If the user asks for a specific number of results, override default.
        num_match = _NUMBER_RE.search(prompt)
        if num_match:
            try:
                n = max(1, min(25, int(num_match.group(1))))
                # Stuff it into cfg for this turn only.
                cfg = dict(cfg)
                cfg["default_result_limit"] = n
            except Exception:
                pass

        t0 = time.monotonic()
        error: str | None = None
        result = {"text": "", "songs": None, "tools_used": []}
        try:
            async with message.channel.typing():
                result = await run_corax_turn(
                    db=db,
                    client=self.client,
                    cfg=cfg,
                    user_prompt=prompt,
                    user_display=message.author.display_name,
                    user_id=message.author.id,
                    channel_id=message.channel.id,
                    mentioned_users=mentioned_users,
                    mentioned_channels=mentioned_channels,
                )
        except Exception as e:
            import traceback
            error = f"{type(e).__name__}: {e}"
            print(f"[corax] LLM call failed: {error}")
            traceback.print_exc()

        latency_ms = int((time.monotonic() - t0) * 1000)

        # Respond.
        try:
            if error:
                await message.reply(
                    "Ich komme gerade nicht durch – versuche es gleich nochmal.",
                    mention_author=False,
                )
            else:
                text = (result.get("text") or "").strip()
                songs = result.get("songs")
                if songs:
                    view = SongCarouselView(songs, invoker_id=message.author.id)
                    header = text if text else f"{len(songs)} Treffer:"
                    try:
                        await message.reply(
                            content=header[:500],
                            embed=view.current_embed(),
                            view=view,
                            mention_author=False,
                        )
                    except Exception as e:
                        import traceback
                        print(f"[corax] reply with embed failed: {type(e).__name__}: {e}")
                        traceback.print_exc()
                        # Fallback: send without embed so the user at least sees
                        # something — the view (buttons) can still paginate.
                        try:
                            await message.reply(
                                content=(header + "\n_(embed rendering failed — "
                                        "use the arrows to browse)_")[:1500],
                                view=view,
                                mention_author=False,
                            )
                        except Exception as e2:
                            print(f"[corax] fallback reply also failed: {e2}")
                elif text:
                    if await self._post_dcs_feedback_instead(message, text[:1900]):
                        pass
                    else:
                        await message.reply(text[:1900], mention_author=False)
                else:
                    await message.reply(
                        "…hm, dazu fällt mir gerade nichts ein.",
                        mention_author=False,
                    )
        except Exception as e:
            import traceback
            print(f"[corax] outer reply error: {type(e).__name__}: {e}")
            traceback.print_exc()

        await db.log_llm_interaction(
            user_id=message.author.id,
            user_name=str(message.author),
            channel_id=message.channel.id,
            prompt=prompt[:2000],
            response=(result.get("text") or "")[:2000],
            tools_used=json.dumps(result.get("tools_used") or []),
            error=error,
            latency_ms=latency_ms,
        )

    async def _post_dcs_feedback_instead(self, message: discord.Message, text: str) -> bool:
        """Keep DCS Discord chat clean: Corax flavor goes to the player panel."""
        try:
            channel_id = int(
                await self.bot.db.get_setting("trya_dcs_chat_channel_id") or 0
            )
        except (TypeError, ValueError):
            channel_id = 0
        if not channel_id or message.channel.id != channel_id:
            return False
        from bot.cogs.trya_dcs_chat import publish_dcs_game_feedback
        await publish_dcs_game_feedback(self.bot.db, text, username="Corax")
        try:
            await message.add_reaction("🐦‍⬛")
        except Exception:
            pass
        return True

    # ---------------------------------------------------------- retention job

    @tasks.loop(hours=6)
    async def retention_task(self):
        try:
            cfg = await self.bot.db.get_llm_config()
            days = int((cfg or {}).get("retention_days") or 30)
            await self.bot.db.purge_llm_audit_log(days)
        except Exception:
            pass

    @retention_task.before_loop
    async def _before_retention(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(LLMChatCog(bot))
