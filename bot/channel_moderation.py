"""Background lyric moderation for Suno URLs posted in monitored Discord
channels.

Sits *next to* the existing exp_radio submission pipeline:

  - exp_radio:    user opts in by submitting a song to the Experimental Radio,
                  pipeline runs Whisper + LLM moderation. Heavy.
  - channel-mod:  any Suno URL pasted in a watched channel is silently
                  screened (lyrics-only — no Whisper). On a 'flagged' verdict
                  a short report is posted to a configured report channel.

Settings keys (stored in the generic `settings` table):

  - channel_moderation_enabled         "on" | "off"
  - channel_moderation_source_channels CSV of channel ids to watch
  - channel_moderation_report_channel  single channel id for reports

All processing happens in a fire-and-forget asyncio.Task that's gated by a
module-level Semaphore so simultaneous posts don't pile up on the LLM.
"""
from __future__ import annotations

import asyncio
import re
from typing import Optional

# Limit concurrent LLM calls. Each lyric moderation = 1 small-model translate
# + 1 medium-model verdict; running >2 in parallel on a single Ollama instance
# starves the queue and yields nothing.
_MOD_SEMAPHORE = asyncio.Semaphore(2)

_SUNO_ID_RE = re.compile(r'https?://suno\.com/(?:s|song)/([\w-]+)')


def extract_suno_uuid(url: str) -> Optional[str]:
    m = _SUNO_ID_RE.search(url or "")
    return m.group(1) if m else None


async def screen_posted_song(
    bot,
    *,
    message_id: int,
    channel_id: int,
    channel_name: str,
    user_id: int,
    user_name: str,
    suno_url: str,
    jump_url: str,
) -> None:
    """End-to-end screen of one posted Suno URL.

    Idempotent: a DB unique constraint on (message_id, suno_url) and an
    explicit pre-check prevent double-processing on rescans / edits.

    On a 'flagged' verdict, posts a short embed to the configured report
    channel and writes the verdict row. On any other outcome only the
    verdict row is written — useful for the admin UI history but no Discord
    noise.
    """
    db = bot.db
    log_prefix = "[chmod]"

    # Defensive: dedup. on_message can be invoked twice on resumed sessions.
    try:
        if await db.has_channel_moderation_check(message_id, suno_url):
            return
    except Exception:
        pass  # fail-open: better to re-check than to miss

    async with _MOD_SEMAPHORE:
        # Re-check inside the semaphore — covers the race where two near-
        # simultaneous posts of the same URL both pass the pre-check.
        try:
            if await db.has_channel_moderation_check(message_id, suno_url):
                return
        except Exception:
            pass

        uuid = extract_suno_uuid(suno_url)
        if not uuid:
            await _safe_log_verdict(
                db, message_id=message_id, channel_id=channel_id,
                channel_name=channel_name, user_id=user_id, user_name=user_name,
                suno_url=suno_url, verdict="skipped",
                reason="Could not parse Suno UUID from URL.",
            )
            return

        # Lazy imports — keep this module importable without the heavy
        # exp_radio dependency chain at top-level.
        try:
            from bot.exp_radio_worker import scrape_suno, clean_lyrics
            from bot.exp_moderation import moderate_lyrics
            from bot.llm import OllamaClient
            from config import Config
        except Exception as e:
            print(f"{log_prefix} import error: {e}", flush=True)
            await _safe_log_verdict(
                db, message_id=message_id, channel_id=channel_id,
                channel_name=channel_name, user_id=user_id, user_name=user_name,
                suno_url=suno_url, verdict="error", reason=f"import: {e}",
            )
            return

        try:
            meta = await scrape_suno(uuid)
        except Exception as e:
            print(f"{log_prefix} scrape error for {uuid}: {e}", flush=True)
            await _safe_log_verdict(
                db, message_id=message_id, channel_id=channel_id,
                channel_name=channel_name, user_id=user_id, user_name=user_name,
                suno_url=suno_url, verdict="error", reason=f"scrape: {e}",
            )
            return

        title  = meta.get("title")  or ""
        artist = meta.get("artist") or ""
        raw    = meta.get("raw_lyrics") or ""
        lyrics = clean_lyrics(raw) if raw else ""

        if not lyrics:
            # Instrumental / lyrics not exposed — skip gracefully.
            await _safe_log_verdict(
                db, message_id=message_id, channel_id=channel_id,
                channel_name=channel_name, user_id=user_id, user_name=user_name,
                suno_url=suno_url, title=title, artist=artist,
                verdict="skipped", reason="No lyrics available.",
            )
            return

        try:
            # Channel moderation is background fire-and-forget; allow a
            # generous timeout so slower CPUs can finish evaluating.
            _CHMOD_LLM_TIMEOUT = 600
            client = OllamaClient(
                base_url=Config.OLLAMA_URL,
                model=Config.LLM_MODEL,
                timeout=_CHMOD_LLM_TIMEOUT,
            )
            verdict = await moderate_lyrics(
                client, lyrics=lyrics, title=title, artist=artist,
                timeout=_CHMOD_LLM_TIMEOUT,
            )
        except Exception as e:
            print(f"{log_prefix} LLM error for {uuid}: {e}", flush=True)
            await _safe_log_verdict(
                db, message_id=message_id, channel_id=channel_id,
                channel_name=channel_name, user_id=user_id, user_name=user_name,
                suno_url=suno_url, title=title, artist=artist,
                verdict="error", reason=f"llm: {e}",
            )
            return

        status = verdict.get("status") or "pending"
        reason = (verdict.get("reason") or "").strip()
        print(
            f"{log_prefix} #{message_id} {title!r} → {status}"
            + (f" ({reason})" if reason else ""),
            flush=True,
        )

        await _safe_log_verdict(
            db, message_id=message_id, channel_id=channel_id,
            channel_name=channel_name, user_id=user_id, user_name=user_name,
            suno_url=suno_url, title=title, artist=artist,
            verdict=status, reason=reason,
        )

        if status == "flagged":
            await _post_report(
                bot,
                source_channel_id=channel_id,
                source_channel_name=channel_name,
                user_name=user_name,
                suno_url=suno_url,
                jump_url=jump_url,
                title=title,
                artist=artist,
                reason=reason,
            )


async def _safe_log_verdict(db, **kwargs):
    try:
        await db.add_channel_moderation_log(**kwargs)
    except Exception as e:
        print(f"[chmod] DB log error: {e}", flush=True)


async def _post_report(
    bot, *, source_channel_id: int, source_channel_name: str,
    user_name: str, suno_url: str, jump_url: str,
    title: str, artist: str, reason: str,
) -> None:
    """Send the flag embed to the configured report channel (if any)."""
    db = bot.db
    report_ch_id = (await db.get_setting("channel_moderation_report_channel") or "").strip()
    if not report_ch_id:
        return
    try:
        ch_int = int(report_ch_id)
    except Exception:
        return
    guild = next(iter(bot.guilds), None)
    if not guild:
        return
    channel = guild.get_channel(ch_int) or guild.get_thread(ch_int)
    if not channel:
        return

    import discord
    label = f"**{title}**" if title else "Untitled"
    if artist:
        label += f" \u2014 {artist}"

    embed = discord.Embed(
        title="\u26A0\ufe0f Possibly rule-violating song posted",
        description=(
            f"{label}\n"
            f"Posted by **{user_name}** in <#{source_channel_id}>"
        ),
        color=discord.Color.orange(),
    )
    embed.add_field(name="Reason", value=(reason or "_no reason provided_")[:1000], inline=False)
    embed.add_field(name="Song", value=suno_url, inline=False)
    if jump_url:
        embed.add_field(name="Original message", value=f"[Jump to message]({jump_url})", inline=False)

    try:
        await channel.send(embed=embed)
    except Exception as e:
        print(f"[chmod] report send error: {e} (source #{source_channel_name})", flush=True)


def dispatch(bot, *, message_id, channel_id, channel_name,
             user_id, user_name, suno_url, jump_url) -> None:
    """Schedule `screen_posted_song` on the running loop.

    Callers should never await this — it's strictly fire-and-forget.
    """
    asyncio.create_task(
        screen_posted_song(
            bot,
            message_id=message_id, channel_id=channel_id,
            channel_name=channel_name, user_id=user_id, user_name=user_name,
            suno_url=suno_url, jump_url=jump_url,
        )
    )
