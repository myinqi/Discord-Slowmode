"""Lightweight Suno metadata fetcher (title + artist).

Re-implements the subset of `_fetch_suno_meta` from web/app.py that Corax
needs, so the bot can enrich song rows whose `song_title` is still NULL
(older posts ingested before the column existed, or embeds that didn't
resolve in time).
"""

from __future__ import annotations

import asyncio
import html as _html
import re

import aiohttp


_SUNO_ID_RE = re.compile(r"suno\.com/s(?:ong)?/([A-Za-z0-9_-]+)")
_OG_TITLE_RE = re.compile(
    r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', re.I
)
_OG_DESC_RE = re.compile(
    r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)', re.I
)


def extract_suno_id(url: str) -> str | None:
    m = _SUNO_ID_RE.search(url or "")
    return m.group(1) if m else None


async def _fetch_one(session: aiohttp.ClientSession, url: str) -> dict:
    """Returns {'title': ..., 'artist': ...} or {} on failure."""
    sid = extract_suno_id(url)
    if not sid:
        return {}
    try:
        async with session.get(
            f"https://suno.com/song/{sid}",
            timeout=aiohttp.ClientTimeout(total=8),
            headers={"User-Agent": "Mozilla/5.0 (CoraxBot)"},
        ) as resp:
            if resp.status != 200:
                return {}
            body = await resp.text()
    except Exception:
        return {}

    out: dict[str, str] = {}
    mt = _OG_TITLE_RE.search(body)
    if mt:
        raw = _html.unescape(mt.group(1)).strip()
        # Typical format: "Song Title by Artist | Suno"
        raw = re.sub(r"\s*\|\s*Suno\s*$", "", raw)
        m2 = re.match(r"^(.*?)\s+by\s+(.+)$", raw, re.I)
        if m2:
            out["title"] = m2.group(1).strip()
            out["artist"] = m2.group(2).strip()
        else:
            out["title"] = raw
    return out


async def enrich_songs(songs: list[dict], max_concurrent: int = 5) -> list[dict]:
    """Fill in missing 'title'/'artist' fields by scraping Suno embeds.

    Only fetches when `title` is falsy. Mutates & returns the input list.
    """
    need = [s for s in songs if not s.get("title")]
    if not need:
        return songs

    sem = asyncio.Semaphore(max_concurrent)
    async with aiohttp.ClientSession() as session:
        async def work(s: dict):
            async with sem:
                meta = await _fetch_one(session, s.get("url") or "")
            if meta.get("title"):
                s["title"] = meta["title"]
            if meta.get("artist") and not s.get("artist"):
                s["artist"] = meta["artist"]

        await asyncio.gather(*(work(s) for s in need), return_exceptions=True)
    return songs
