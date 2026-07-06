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
_SUNO_SUFFIX_RE = re.compile(r"\s*\|\s*Suno(?:\s*AI)?\s*$", re.I)


def _clean_suno_title(raw_title: str | None) -> str | None:
    if not raw_title:
        return None
    title = _html.unescape(raw_title).strip()
    title = _SUNO_SUFFIX_RE.sub("", title).strip()
    return title or None


def _split_title_artist_fallback(raw_title: str) -> tuple[str, str | None]:
    matches = list(re.finditer(r"\s+by\s+", raw_title, flags=re.I))
    if not matches:
        return raw_title, None
    match = matches[-1]
    title = raw_title[:match.start()].strip()
    artist = raw_title[match.end():].strip()
    if not title or not artist:
        return raw_title, None
    return title, artist


def _extract_suno_display_name(body: str) -> str | None:
    candidates = re.findall(r'display_name\\":\\"((?:[^"\\]|\\[^"])*)\\"', body)
    if not candidates:
        candidates = re.findall(r'"display_name"\s*:\s*"((?:[^"\\]|\\.)*)"', body)
    for dn in reversed(candidates):
        dn = re.sub(r'\\\\u([0-9a-fA-F]{4})',
                    lambda m: chr(int(m.group(1), 16)), dn)
        dn = re.sub(r'\\u([0-9a-fA-F]{4})',
                    lambda m: chr(int(m.group(1), 16)), dn)
        dn = _html.unescape(dn).strip()
        if (len(dn) > 1
                and not re.match(r"^v\d", dn)
                and dn not in ("Cover", "Remix")):
            return dn
    return None


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
        raw = _clean_suno_title(mt.group(1))
        if raw:
            out["title"] = raw
    else:
        # Fallback: look for <title>…</title>
        mt2 = re.search(r"<title[^>]*>([^<]+)</title>", body, re.I)
        if mt2:
            raw = _clean_suno_title(mt2.group(1))
            if raw and raw.lower() != "suno":
                out["title"] = raw
        if not out:
            print(f"[suno-meta] no og:title found on {target} (body {len(body)} bytes)")

    artist = _extract_suno_display_name(body)
    if artist:
        out["artist"] = artist
    elif out.get("title"):
        out["title"], fallback_artist = _split_title_artist_fallback(out["title"])
        if fallback_artist:
            out["artist"] = fallback_artist
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
