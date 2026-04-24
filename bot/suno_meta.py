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
        print(f"[suno-meta] no suno id in url: {url!r}")
        return {}
    target = f"https://suno.com/song/{sid}"
    try:
        async with session.get(
            target,
            timeout=aiohttp.ClientTimeout(total=10),
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.9",
            },
        ) as resp:
            if resp.status != 200:
                print(f"[suno-meta] http {resp.status} for {target}")
                return {}
            body = await resp.text()
    except Exception as e:
        print(f"[suno-meta] fetch error {type(e).__name__}: {e} url={target}")
        return {}

    out: dict[str, str] = {}
    mt = _OG_TITLE_RE.search(body)
    if mt:
        raw = _html.unescape(mt.group(1)).strip()
        # Typical format: "Song Title by Artist | Suno" or "Song Title | Suno AI"
        raw = re.sub(r"\s*\|\s*Suno(?:\s*AI)?\s*$", "", raw, flags=re.I)
        m2 = re.match(r"^(.*?)\s+by\s+(.+)$", raw, re.I)
        if m2:
            out["title"] = m2.group(1).strip()
            out["artist"] = m2.group(2).strip()
        else:
            out["title"] = raw
    else:
        # Fallback: look for <title>…</title>
        mt2 = re.search(r"<title[^>]*>([^<]+)</title>", body, re.I)
        if mt2:
            raw = _html.unescape(mt2.group(1)).strip()
            raw = re.sub(r"\s*\|\s*Suno(?:\s*AI)?\s*$", "", raw, flags=re.I)
            if raw and raw.lower() != "suno":
                out["title"] = raw
        if not out:
            print(f"[suno-meta] no og:title found on {target} (body {len(body)} bytes)")
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
