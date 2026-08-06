"""Manages ffmpeg streaming to Twitch RTMP.

Uses ffmpeg's concat demuxer to play songs back-to-back in a single process,
ensuring seamless transitions without stream interruptions.
"""

import asyncio
import html as html_lib
import math
import os
import random
import re
import shutil
import tempfile
import textwrap
import time
import unicodedata

import aiohttp

from bot.twitch_bot import TwitchBot

_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FFFF"  # All supplemental symbols/pictographs/alchemy/etc.
    "\U00002600-\U000027BF"  # Misc symbols + dingbats
    "\U0000FE00-\U0000FE0F"  # variation selectors
    "\U0000200B-\U0000200D"  # zero-width joiner / non-joiners
    "\U000020D0-\U000020FF"  # combining marks for symbols
    "\U00002300-\U000023FF"  # misc technical (incl. ⌚⌛)
    "\U00002460-\U000024FF"  # enclosed alphanumerics
    "\U00002500-\U000025FF"  # box drawing + geometric shapes
    "\U00002B00-\U00002BFF"  # misc symbols and arrows
    "\U00003000-\U0000303F"  # CJK punctuation (incl. wavy dashes)
    "\U00003200-\U000032FF"  # enclosed CJK
    "\U0000FE0F"
    "\U0000FFFC-\U0000FFFD"  # object replacement / replacement char
    "]+"
)

_PLAYLIST_REPEATS = 50
_LYRICS_WINDOW_LINES = 20   # fills the 800 px box at the current font size
_LYRICS_MIN_INTERVAL = 0.5  # min seconds between scroll steps (very short songs)
_LYRICS_MAX_INTERVAL = 4.0  # max seconds between scroll steps (very long songs)
_LYRICS_SCROLL_DURATION_FACTOR = 0.85  # leave a calm tail after all lyrics passed
_NOW_PLAYING_CHAT_DELAY = 12.0

# Map common typographic Unicode that some renderers / fonts handle poorly
# back to their plain ASCII equivalents. We keep diacritics intact.
_TYPOGRAPHIC_MAP = str.maketrans({
    "\u2018": "'",  "\u2019": "'",   # curly single quotes
    "\u201A": "'",  "\u201B": "'",
    "\u201C": '"',  "\u201D": '"',   # curly double quotes
    "\u201E": '"',  "\u201F": '"',
    "\u2032": "'",  "\u2033": '"',   # primes
    "\u2013": "-",  "\u2014": "-",   # en/em dash
    "\u2026": "...",                  # ellipsis
    "\u00A0": " ",                    # nbsp
    "\u00AD": "",                      # soft-hyphen (invisible, renders as box)
    "\u00B7": "",                      # middle dot (renders as tofu in drawtext)
    "\u200B": "",   "\u200C": "",     # zero-width spaces
    "\u200D": "",   "\uFEFF": "",
    "\u2028": "",   "\u2029": "",    # line / paragraph separators (render as box)
    "\u2022": "-",                     # bullet → dash
    "\u2027": "-",                     # hyphenation point → dash
    "\u2043": "-",                     # hyphen bullet → dash
    "\u00B6": "",                      # pilcrow sign (paragraph mark)
    "\u00A7": "",                      # section sign
})

# Fallback wrap width used before the final overlay dimensions are available.
_LYRICS_MAX_LINE_CHARS = 85

_LYRIC_SECTION_RE = re.compile(
    r"^(?:intro|verse(?:\s+\d+)?|pre[- ]?chorus|chorus|hook|refrain|"
    r"post[- ]?chorus|bridge|break|interlude|instrumental|solo|outro)$",
    re.IGNORECASE,
)


def _renderable_char(ch: str) -> bool:
    """Whitelist code-points that ffmpeg drawtext + Noto Sans can render
    without producing glyph boxes (\"tofu\"). Anything decorative or in
    scripts we don't ship a font for is dropped silently."""
    if ch == "\n":
        return True
    # Always reject control/format/surrogate/private-use/unassigned. This
    # catches U+202A-U+202E (BIDI controls) and friends that would otherwise
    # slip through the General Punctuation range below.
    if unicodedata.category(ch)[0] == "C":
        return False
    cp = ord(ch)
    if cp < 0x0020:                              # leftover C0 controls
        return False
    if cp <= 0x024F:                             # Basic Latin … Latin Extended-B
        return True
    if 0x0250 <= cp <= 0x02FF:                   # IPA Extensions + spacing modifiers
        return True
    if 0x0300 <= cp <= 0x036F:                   # Combining diacritics
        return True
    if 0x0370 <= cp <= 0x03FF:                   # Greek
        return True
    if 0x0400 <= cp <= 0x04FF:                   # Cyrillic
        return True
    if 0x2010 <= cp <= 0x205E:                   # General punctuation (en-dash etc.)
        return True
    if 0x20A0 <= cp <= 0x20CF:                   # Currency symbols
        return True
    if 0x3040 <= cp <= 0x30FF:                   # Hiragana + Katakana
        return True
    if 0x4E00 <= cp <= 0x9FFF:                   # CJK Unified Ideographs
        return True
    return False


def _normalize_text(text: str) -> str:
    """Normalize fancy Unicode and strip anything ffmpeg drawtext cannot
    render with the Noto Sans family (emoji, decorative scripts, control
    chars, private-use, unpaired surrogates, etc.). Used for *lyrics* where
    we keep the original script if it's renderable."""
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_TYPOGRAPHIC_MAP)
    text = "".join(ch for ch in text if _renderable_char(ch))
    return text.strip()


def _clean_lyric_lines(raw: str) -> list[str]:
    """Turn Suno's lightly formatted prompt into overlay-friendly lyrics."""
    lines: list[str] = []

    def add_blank():
        if lines and lines[-1] != "":
            lines.append("")

    for raw_line in raw.replace("\r", "").split("\n"):
        cleaned = _normalize_text(raw_line)
        if not cleaned:
            add_blank()
            continue

        is_heading = bool(re.match(r"^#{1,6}(?:\s|\*)", cleaned))
        cleaned = re.sub(r"^#{1,6}\s*", "", cleaned).strip()
        is_bold_line = (
            len(cleaned) >= 4
            and cleaned.startswith(("**", "__"))
            and cleaned.endswith(("**", "__"))
        )
        cleaned = cleaned.replace("**", "").replace("__", "")
        cleaned = cleaned.replace("~~", "").replace("`", "").strip(" *_")
        if not cleaned:
            continue

        tag = re.fullmatch(r"\[([^\]]+)\]", cleaned)
        if tag:
            section = re.sub(r"\s+", " ", tag.group(1)).strip()
            if not _LYRIC_SECTION_RE.fullmatch(section):
                continue
            add_blank()
            lines.append(section.upper())
            lines.append("")
            continue

        if is_heading or is_bold_line:
            add_blank()
            lines.append(cleaned.upper())
            lines.append("")
        else:
            lines.append(cleaned)

    while lines and lines[-1] == "":
        lines.pop()
    return lines


def _transliterate_for_overlay(text: str) -> str:
    """Like _normalize_text, but instead of dropping non-renderable chars we
    transliterate them to ASCII equivalents via Unidecode. Used for the
    Now-Playing line (title + artist) where exotic decorations like
    \"꧁༺ Tαɾʝα ༻꧂\" should still produce something readable."""
    from unidecode import unidecode
    text = unicodedata.normalize("NFKC", text)
    text = text.translate(_TYPOGRAPHIC_MAP)
    out = []
    for ch in text:
        if _renderable_char(ch):
            out.append(ch)
            continue
        cat = unicodedata.category(ch)
        # Only transliterate letters & numbers from non-supported scripts.
        # Decorative punctuation/symbols (꧁, ༺, ꧂ …) get dropped silently
        # so we never end up with stray "]" or "[" in artist names.
        if cat[0] in ("L", "N"):
            out.append(unidecode(ch))
    result = "".join(out)
    result = re.sub(r"\s+", " ", result).strip()
    return result


_UNICODE_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")
# Surrogate pair: high surrogate (D800-DBFF) + low surrogate (DC00-DFFF)
_SURROGATE_PAIR_RE = re.compile(
    r"\\u([dD][89aAbB][0-9a-fA-F]{2})\\u([dD][c-fC-F][0-9a-fA-F]{2})"
)


def _surrogate_pair_to_char(match) -> str:
    high = int(match.group(1), 16)
    low  = int(match.group(2), 16)
    cp   = 0x10000 + (high - 0xD800) * 0x400 + (low - 0xDC00)
    return chr(cp)


def _decode_json_string(raw: str) -> str:
    """Properly decode all JSON-style backslash-escapes that Suno's RSC payload
    contains — including surrogate pairs for non-BMP code points (emoji etc.)
    which were previously left half-decoded and produced glyph boxes."""
    raw = raw.replace("\\\\", "\x00")           # placeholder so we don't double-decode
    raw = raw.replace("\\n", "\n").replace("\\t", " ").replace("\\r", "")
    raw = raw.replace('\\"', '"').replace("\\/", "/")
    # Decode surrogate pairs FIRST so we don't accidentally split them.
    raw = _SURROGATE_PAIR_RE.sub(_surrogate_pair_to_char, raw)
    raw = _UNICODE_ESCAPE_RE.sub(
        lambda m: chr(int(m.group(1), 16)), raw
    )
    raw = raw.replace("\x00", "\\")
    return raw


SUNO_PLAYLIST_UUID_RE = re.compile(r'suno\.com/playlist/([a-f0-9-]{36})')
_BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:149.0) Gecko/20100101 Firefox/149.0"
SUNO_SONG_RE = re.compile(r'https?://suno\.com/song/([a-f0-9-]{36})')


async def _parse_suno_api(playlist_uuid: str) -> list[dict]:
    """Try Suno studio API to get playlist songs."""
    api_url = f"https://studio-api.suno.ai/api/playlist/{playlist_uuid}"
    headers = {"User-Agent": _BROWSER_UA, "Accept": "application/json"}
    songs = []
    page = 1
    while True:
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(
                    api_url, params={"page": page}, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as resp:
                    if resp.status != 200:
                        print(f"[radio] Suno API failed: HTTP {resp.status}")
                        return []
                    data = await resp.json()
        except Exception as e:
            print(f"[radio] Suno API error: {e}")
            return []

        clips = data.get("playlist_clips") or []
        if not clips:
            break

        for entry in clips:
            clip = entry.get("clip") or entry
            clip_id = clip.get("id", "")
            if not clip_id:
                continue
            title = clip.get("title") or clip_id[:12]
            artist = clip.get("display_name") or ""
            # Duration: try several common field names — Suno's API has used
            # `audio_duration` (top-level) and `metadata.duration` historically.
            duration = (
                clip.get("audio_duration")
                or clip.get("duration")
                or (clip.get("metadata") or {}).get("duration")
            )
            try:
                duration = float(duration) if duration is not None else None
            except (TypeError, ValueError):
                duration = None
            songs.append({
                "uuid": clip_id,
                "title": title,
                "artist": artist,
                "duration": duration,
                "suno_url": f"https://suno.com/song/{clip_id}",
            })

        if not data.get("has_more", False):
            break
        page += 1

    return songs


async def _parse_suno_html(url: str) -> list[dict]:
    """Fallback: scrape the playlist HTML for song data from embedded RSC/JSON payloads."""
    headers = {"User-Agent": _BROWSER_UA}
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status != 200:
                    print(f"[radio] Suno HTML fetch failed: HTTP {resp.status}")
                    return []
                html = await resp.text()
    except Exception as e:
        print(f"[radio] Suno HTML fetch error: {e}")
        return []

    import json as _json
    print(f"[radio] HTML page size: {len(html)} chars")

    # Collect all text content: decode RSC flight chunks into a single blob
    full_text = html
    rsc_chunks = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html, re.DOTALL)
    if rsc_chunks:
        decoded_parts = []
        for chunk in rsc_chunks:
            decoded = chunk.replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"').replace("\\\\", "\\")
            decoded_parts.append(decoded)
        full_text = html + "\n" + "\n".join(decoded_parts)
        print(f"[radio] Decoded {len(rsc_chunks)} RSC chunks, total text: {len(full_text)} chars")

    # Strategy 1: Look for CDN audio URLs (most reliable)
    cdn_pattern = re.compile(r'cdn[12]\.suno\.ai/([a-f0-9-]{36})\.mp3')
    cdn_uuids = cdn_pattern.findall(full_text)
    if cdn_uuids:
        seen = set()
        songs = []
        for uuid in cdn_uuids:
            if uuid not in seen:
                seen.add(uuid)
                songs.append({
                    "uuid": uuid,
                    "title": uuid[:12],
                    "artist": "",
                    "suno_url": f"https://suno.com/song/{uuid}",
                })
        print(f"[radio] Found {len(songs)} songs via CDN URL pattern")
        return songs

    # Strategy 2: Look for song page URLs
    song_uuids = SUNO_SONG_RE.findall(full_text)
    if song_uuids:
        seen = set()
        songs = []
        for uuid in song_uuids:
            if uuid not in seen:
                seen.add(uuid)
                songs.append({
                    "uuid": uuid,
                    "title": uuid[:12],
                    "artist": "",
                    "suno_url": f"https://suno.com/song/{uuid}",
                })
        if len(songs) > 1:
            print(f"[radio] Found {len(songs)} songs via song URL pattern")
            return songs

    # Strategy 3: Look for __NEXT_DATA__ (Pages Router)
    nd_match = re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
    if nd_match:
        try:
            nd = _json.loads(nd_match.group(1))
            props = nd.get("props", {}).get("pageProps", {})
            clips = props.get("playlist", {}).get("playlist_clips") or props.get("clips") or []
            songs = []
            for entry in clips:
                clip = entry.get("clip") or entry
                clip_id = clip.get("id", "")
                if clip_id:
                    songs.append({
                        "uuid": clip_id,
                        "title": clip.get("title") or clip_id[:12],
                        "artist": clip.get("display_name") or "",
                        "suno_url": f"https://suno.com/song/{clip_id}",
                    })
            if songs:
                print(f"[radio] Found {len(songs)} songs from __NEXT_DATA__")
                return songs
        except Exception as e:
            print(f"[radio] __NEXT_DATA__ parse error: {e}")

    # Return whatever we found (even if just 1)
    if song_uuids:
        seen = set()
        songs = []
        for uuid in song_uuids:
            if uuid not in seen:
                seen.add(uuid)
                songs.append({
                    "uuid": uuid,
                    "title": uuid[:12],
                    "artist": "",
                    "suno_url": f"https://suno.com/song/{uuid}",
                })
        print(f"[radio] Fallback: found {len(songs)} songs via song URL regex")
        return songs

    print("[radio] No songs found in HTML at all")
    return []


async def parse_suno_playlist(url: str) -> list[dict]:
    """Fetch songs from a Suno playlist. Tries API first, then HTML fallback."""
    m = SUNO_PLAYLIST_UUID_RE.search(url)
    if not m:
        print(f"[radio] Could not extract playlist UUID from: {url}")
        return []
    playlist_uuid = m.group(1)

    # Try API first
    songs = await _parse_suno_api(playlist_uuid)
    if songs:
        print(f"[radio] Got {len(songs)} songs from Suno API")
        return songs

    # Fallback to HTML scraping
    print("[radio] API failed, trying HTML fallback...")
    songs = await _parse_suno_html(url)
    print(f"[radio] Parsed {len(songs)} songs total from Suno playlist")
    return songs


async def download_suno_song(uuid: str, target_dir: str) -> str | None:
    """Download a Suno song MP3 to target_dir. Returns filepath or None."""
    url = f"https://cdn1.suno.ai/{uuid}.mp3"
    filepath = os.path.join(target_dir, f"{uuid}.mp3")
    if os.path.exists(filepath):
        return filepath
    headers = {"User-Agent": _BROWSER_UA}
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status != 200:
                    print(f"[radio] Download failed for {uuid}: HTTP {resp.status}")
                    return None
                data = await resp.read()
                with open(filepath, "wb") as f:
                    f.write(data)
                print(f"[radio] Downloaded {uuid}.mp3 ({len(data) // 1024} KB)")
                return filepath
    except Exception as e:
        print(f"[radio] Download error for {uuid}: {e}")
        return None


async def resolve_suno_meta(uuid: str) -> dict:
    """Fetch title + artist from Suno embed page."""
    import html as _html
    url = f"https://suno.com/song/{uuid}"
    headers = {"User-Agent": _BROWSER_UA}
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    return {}
                page = await resp.text()
                match = re.search(r'<title>([^<]+)</title>', page)
                if match:
                    raw = _html.unescape(match.group(1).strip())
                    raw = re.sub(r'\s*[|\-\u2013]\s*Suno$', '', raw).strip()
                    by_match = re.search(r'^(.+?)\s+by\s+(.+)$', raw)
                    if by_match:
                        return {"title": by_match.group(1).strip(), "artist": by_match.group(2).strip()}
                    return {"title": raw, "artist": ""}
    except Exception:
        pass
    return {}


class StreamManager:
    def __init__(self, db, radio_dir: str):
        self.db = db
        self.radio_dir = radio_dir
        self._process = None
        self._tasks = []
        self.is_running = False
        self.current_index = 0
        self.current_song = None
        self.playlist = []
        self._temp_dir = None
        self._font_path = None
        self._start_time = None
        self._twitch_key = None
        self._overlay_path = None
        self._lyrics_path = None
        self._lyrics_cache = {}  # {song_id: [lines]}
        self._concat_start = 0
        self._twitch_chat = None
        self._chat_announcement_task = None
        self._chat_announcement_token = 0
        self._last_announced_song_key = None
        self._last_announced_at = 0.0
        self._suno_dl_dir = None  # temp dir for downloaded Suno playlist songs
        self._header_path = None  # playlist title overlay file
        self._disclaimer_path = None
        self._loading = False  # guard against concurrent start() calls
        self._video_url_cache = {}   # uuid -> video_url str | None
        self._visual_meta_cache = {} # uuid -> video, cover and resolved Suno UUID
        self._song_pip_paths = {}    # uuid -> path to loop-trimmed pip clip
        self._song_pip_concat_path = None
        self._song_pip_ready = False  # True only after ALL clips are prepared

    async def get_status(self) -> dict:
        return {
            "running": self.is_running,
            "current_song": {
                "id": self.current_song["id"],
                "title": self.current_song["title"],
                "artist": self.current_song["artist"],
                "index": self.current_index,
                "total": len(self.playlist),
            } if self.current_song else None,
        }

    async def _load_suno_playlist(self, playlist_id: int) -> list[dict]:
        """Parse a Suno playlist, download MP3s, resolve metadata. Returns playlist entries."""
        pl = await self.db.get_suno_playlist(playlist_id)
        if not pl:
            return []
        songs = await parse_suno_playlist(pl["url"])
        if not songs:
            return []
        # Create download dir (persistent across restarts — reuse cached files)
        dl_dir = os.path.join(self.radio_dir, "suno_cache")
        os.makedirs(dl_dir, exist_ok=True)
        self._suno_dl_dir = dl_dir

        entries = []
        for i, s in enumerate(songs):
            print(f"[radio] Preparing Suno song {i+1}/{len(songs)}: {s['uuid']}")
            filepath = await download_suno_song(s["uuid"], dl_dir)
            if not filepath:
                continue
            # Use metadata from API; fall back to scraping only if missing
            title = s.get("title") or ""
            artist = s.get("artist") or ""
            if not title or title == s["uuid"][:12]:
                meta = await resolve_suno_meta(s["uuid"])
                title = meta.get("title") or title or s["uuid"][:12]
                artist = meta.get("artist") or artist
            artist = artist or "Unknown"
            # Get duration via ffprobe
            duration = await self._probe_duration(filepath)
            entries.append({
                "id": s["uuid"],
                "title": title,
                "artist": artist,
                "filename": os.path.basename(filepath),
                "suno_url": s["suno_url"],
                "duration": duration,
            })
        print(f"[radio] Suno playlist ready: {len(entries)} songs")
        return entries

    async def _probe_duration(self, filepath: str) -> float:
        """Get audio duration in seconds via ffprobe."""
        try:
            import json as _json
            proc = await asyncio.create_subprocess_exec(
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_format", filepath,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode == 0:
                info = _json.loads(stdout)
                return float(info.get("format", {}).get("duration", 180))
        except Exception:
            pass
        return 180

    async def _fetch_visual_meta(self, suno_url: str) -> dict:
        """Scrape video and cover metadata from a Suno song or short URL."""
        result = {"video_url": None, "cover_url": None, "real_uuid": None}
        if not suno_url:
            return result
        try:
            headers = {"User-Agent": _BROWSER_UA}
            async with aiohttp.ClientSession() as sess:
                async with sess.get(suno_url, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        print(f"[radio] Visual metadata HTTP {resp.status}: {suno_url}")
                        return result
                    page = await resp.text()
                    final_url = str(resp.url)

            uuid_match = re.search(
                r'([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})',
                final_url,
                re.IGNORECASE,
            )
            if not uuid_match:
                uuid_match = re.search(
                    r'"id"\s*:\s*"([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})"',
                    page,
                    re.IGNORECASE,
                )
            if not uuid_match:
                uuid_match = re.search(
                    r'song/([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})',
                    page,
                    re.IGNORECASE,
                )
            if uuid_match:
                result["real_uuid"] = uuid_match.group(1)

            m = re.search(r'"video_cover_url"\s*:\s*"([^"]+)"', page)
            if not m:
                m = re.search(r'video_cover_url\\":\\"([^"\\]+)\\"', page)
            if m:
                url = m.group(1).replace("\\/", "/")
                print(f"[radio] Song PiP video URL: {url[:60]}")
                result["video_url"] = html_lib.unescape(url)

            for pattern in (
                r'<meta\s+(?:property|name)=["\']og:image["\']\s+content=["\']([^"\']+)["\']',
                r'<meta\s+content=["\']([^"\']+)["\']\s+(?:property|name)=["\']og:image["\']',
                r'"image_url"\s*:\s*"([^"]+)"',
            ):
                image_match = re.search(pattern, page, re.IGNORECASE)
                if image_match:
                    result["cover_url"] = html_lib.unescape(
                        image_match.group(1).replace("\\/", "/")
                    )
                    break
        except Exception as e:
            print(f"[radio] _fetch_visual_meta error: {e}")
        return result

    async def _download_file(self, url: str, dest: str) -> bool:
        """Download url to dest. Returns True on success."""
        if os.path.exists(dest):
            return True
        try:
            headers = {"User-Agent": _BROWSER_UA}
            async with aiohttp.ClientSession() as sess:
                async with sess.get(url, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=60)) as resp:
                    if resp.status != 200:
                        print(f"[radio] Download HTTP {resp.status}: {url[:80]}")
                        return False
                    data = await resp.read()
            with open(dest, "wb") as fh:
                fh.write(data)
            return True
        except Exception as e:
            print(f"[radio] _download_file error ({url[:60]}): {e}")
            return False

    async def _prepare_song_pip_clip(self, song: dict) -> str | None:
        """Download Suno video source (or cover fallback) for song PiP.
        Suno clips are always ~10 s — no loop-trim transcode needed.
        Works for both suno_playlist songs (uuid field) and submissions (suno_url field).
        All files are persistent in suno_cache/video/."""
        # key: prefer Suno UUID, fall back to stringified DB id
        key = str(song.get("uuid") or song.get("id") or "")
        if not key:
            return None

        # Derive suno_url: prefer stored value, then build from uuid if it looks like one
        suno_url = song.get("suno_url") or ""
        if not suno_url:
            uuid_val = song.get("uuid", "")
            if isinstance(uuid_val, str) and len(uuid_val) == 36:
                suno_url = f"https://suno.com/song/{uuid_val}"

        # Extract the actual Suno UUID from the URL for CDN image fallback
        _suno_uuid_re = re.compile(r'([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})', re.I)
        m = _suno_uuid_re.search(suno_url)
        suno_uuid = m.group(1) if m else None

        vid_cache_dir = os.path.join(self.radio_dir, "suno_cache", "video")
        os.makedirs(vid_cache_dir, exist_ok=True)

        # 1) Fetch video URL, cover URL and canonical UUID in one request.
        if key not in self._visual_meta_cache:
            self._visual_meta_cache[key] = (
                await self._fetch_visual_meta(suno_url) if suno_url else {}
            )
        visual_meta = self._visual_meta_cache.get(key) or {}
        video_url = visual_meta.get("video_url")
        self._video_url_cache[key] = video_url
        suno_uuid = visual_meta.get("real_uuid") or suno_uuid

        # Target: all clips normalized to 720×1280 (9:16 HD portrait), h264.
        # Crop-to-fill so content always fills the frame without black bars.
        # Timestamps reset to 0 to prevent DTS discontinuities in concat.
        _NW, _NH = 720, 1280
        _NORM_VF = (
            f"scale={_NW}:{_NH}:force_original_aspect_ratio=increase,"
            f"crop={_NW}:{_NH},"
            f"setpts=PTS-STARTPTS,format=yuv420p"
        )

        async def _normalize(src: str, dst: str) -> bool:
            if os.path.exists(dst):
                return True
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-i", src,
                "-vf", _NORM_VF,
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                "-r", "24", "-an", dst,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            return os.path.exists(dst)

        # 2) Try Suno video clip (~10 s) — download then normalize
        if video_url:
            raw_path = os.path.join(vid_cache_dir, f"raw_vid_{key}.mp4")
            norm_path = os.path.join(vid_cache_dir, f"norm_{_NW}x{_NH}_{key}.mp4")
            if os.path.exists(norm_path):
                self._song_pip_paths[key] = norm_path
                print(f"[radio] Song PiP ready (video): {key}")
                return norm_path
            if (os.path.exists(raw_path) or await self._download_file(video_url, raw_path)) \
                    and await _normalize(raw_path, norm_path):
                self._song_pip_paths[key] = norm_path
                print(f"[radio] Song PiP ready (video): {key}")
                return norm_path

        # 3) Fallback: cover image → 10 s static clip at same normalized size
        image_url = song.get("image_url") or visual_meta.get("cover_url") or (
            f"https://cdn1.suno.ai/image_large_{suno_uuid}.jpeg" if suno_uuid else None
        )
        cover_path = os.path.join(vid_cache_dir, f"cover_{key}.jpg")
        cover_vid = os.path.join(vid_cache_dir, f"cover_{_NW}x{_NH}_{key}.mp4")
        if os.path.exists(cover_vid):
            self._song_pip_paths[key] = cover_vid
            print(f"[radio] Song PiP ready (cached cover): {key}")
            return cover_vid
        if image_url and await self._download_file(image_url, cover_path):
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y",
                "-loop", "1", "-i", cover_path,
                "-t", "10", "-r", "24",
                "-vf", _NORM_VF,
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                "-an", cover_vid,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
            if os.path.exists(cover_vid):
                self._song_pip_paths[key] = cover_vid
                print(f"[radio] Song PiP ready (cover): {key}")
                return cover_vid

        # 4) Last resort: 10 s black frame (encode once, reuse)
        black_vid = os.path.join(vid_cache_dir, f"black_{_NW}x{_NH}.mp4")
        if not os.path.exists(black_vid):
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y",
                "-f", "lavfi", "-i", f"color=c=black:s={_NW}x{_NH}:r=24:d=10",
                "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
                "-an", black_vid,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await proc.wait()
        if os.path.exists(black_vid):
            self._song_pip_paths[key] = black_vid
            return black_vid
        return None

    def _cleanup_video_source_cache(self):
        """Remove cached video/cover files no longer in the active playlist.
        Keeps black_10s.mp4 (shared fallback) and any file whose UUID appears
        in the current playlist."""
        vid_cache_dir = os.path.join(self.radio_dir, "suno_cache", "video")
        if not os.path.isdir(vid_cache_dir):
            return
        active_uuids = {str(s.get("uuid") or s.get("id")) for s in self.playlist if s.get("uuid") or s.get("id")}
        removed = 0
        for fname in os.listdir(vid_cache_dir):
            if fname.startswith("black_"):
                continue
            if not any(uid in fname for uid in active_uuids):
                try:
                    os.remove(os.path.join(vid_cache_dir, fname))
                    removed += 1
                except OSError:
                    pass
        if removed:
            print(f"[radio] Video cache cleanup: removed {removed} stale file(s)")

    async def _prepare_song_pip_and_reload(self):
        """Background task: prepare all clips, then hot-reload the running stream."""
        try:
            await self._prepare_all_song_pip_clips()
            self._song_pip_ready = True
            if self._song_pip_paths and self.is_running:
                print("[radio] Song-PiP clips ready — reloading stream with video overlay...")
                await self.reload_pip()
        except Exception as e:
            import traceback
            print(f"[radio] Song-PiP background task error: {e}")
            traceback.print_exc()

    async def _prepare_all_song_pip_clips(self):
        """Download + transcode pip clips for all playlist songs (semaphore-limited)."""
        sem = asyncio.Semaphore(3)  # max 3 parallel downloads/transcodes

        async def _prep(song):
            async with sem:
                await self._prepare_song_pip_clip(song)

        total = len(self.playlist)
        print(f"[radio] Preparing {total} Song-PiP clips…")
        # Clean up stale source files from previous playlists first
        self._cleanup_video_source_cache()
        await asyncio.gather(*[_prep(s) for s in self.playlist])
        ready = sum(1 for s in self.playlist
                    if self._song_pip_paths.get(str(s.get("uuid") or s.get("id") or "")))
        print(f"[radio] Song-PiP ready: {ready}/{total}")

    def _build_song_pip_concat(self) -> str | None:
        """Build a video concat file for the song-pip track.
        All clips are normalized to 480x854 / 24 fps / PTS-from-0, so the
        concat 'duration' directive gives exact per-song sync."""
        _CLIP_DUR = 10.0  # normalized clips are always ~10 s
        n = len(self.playlist)
        path = os.path.join(self._temp_dir, "song_pip.txt")
        with open(path, "w") as f:
            for rep in range(_PLAYLIST_REPEATS):
                for i in range(n):
                    idx = (self._concat_start + i) % n
                    song = self.playlist[idx]
                    uuid = str(song.get("uuid") or song.get("id") or "")
                    clip = self._song_pip_paths.get(uuid)
                    if not clip or not os.path.exists(clip):
                        continue
                    safe = clip.replace("'", "'\\''")
                    song_dur = max(5.0, float(song.get("duration") or 180))
                    full = int(song_dur / _CLIP_DUR)
                    remainder = song_dur - full * _CLIP_DUR
                    for _ in range(full):
                        f.write(f"file '{safe}'\n")
                    if remainder > 0.05:
                        f.write(f"file '{safe}'\n")
                        f.write(f"duration {remainder:.3f}\n")
        self._song_pip_concat_path = path
        return path

    async def start(self):
        if self.is_running:
            return {"error": "Stream is already running."}
        if self._loading:
            return {"error": "Stream is currently loading, please wait."}
        self._loading = True
        # Reset Song-PiP state for this new session
        self._song_pip_ready = False
        self._song_pip_paths = {}
        self._video_url_cache = {}
        self._visual_meta_cache = {}
        self._chat_announcement_token = 0
        self._last_announced_song_key = None
        self._last_announced_at = 0.0

        # Determine radio source mode
        source_mode = await self.db.get_setting("radio_source_mode") or "submissions"
        if source_mode == "suno_playlist":
            active_pl_id = await self.db.get_setting("radio_active_suno_playlist")
            if not active_pl_id:
                self._loading = False
                return {"error": "No Suno playlist selected."}
            self.playlist = await self._load_suno_playlist(int(active_pl_id))
            if not self.playlist:
                self._loading = False
                return {"error": "Suno playlist is empty or could not be loaded."}
            self._source_mode = "suno_playlist"
        else:
            self.playlist = await self.db.get_all_radio_songs(active_only=True)
            if not self.playlist:
                self._loading = False
                return {"error": "No songs in the playlist."}
            self._source_mode = "submissions"

        self._twitch_key = await self.db.get_setting("radio_twitch_key")
        if not self._twitch_key:
            self._loading = False
            return {"error": "Twitch stream key not configured."}
        shuffle = (await self.db.get_setting("radio_shuffle") or "0") == "1"
        if shuffle:
            random.shuffle(self.playlist)
        self._font_path = await self._resolve_font()
        # Clean up any orphaned temp dirs from previous crashed sessions
        import glob as _glob
        for stale in _glob.glob(os.path.join(tempfile.gettempdir(), "radio_*")):
            if os.path.isdir(stale):
                shutil.rmtree(stale, ignore_errors=True)
        self._temp_dir = tempfile.mkdtemp(prefix="radio_")
        self._overlay_path = os.path.join(self._temp_dir, "nowplaying.txt")
        self._lyrics_path = os.path.join(self._temp_dir, "lyrics.txt")
        self._header_path = os.path.join(self._temp_dir, "header.txt")
        self._disclaimer_path = os.path.join(self._temp_dir, "disclaimer.txt")
        with open(self._lyrics_path, "w", encoding="utf-8") as f:
            f.write(" ")
        # Write playlist title header
        await self._write_header()
        await self._write_disclaimer()
        # Song-Video PiP: prepare clips in background so stream starts immediately.
        # Works for both suno_playlist (uuid) and submissions (suno_url from DB).
        if (await self.db.get_setting("radio_song_pip_enabled") or "off") == "on":
            asyncio.ensure_future(self._prepare_song_pip_and_reload())
        # Connect to Twitch chat if configured (Helix-based bot, modern auth)
        client_id = await self.db.get_setting("radio_twitch_client_id")
        refresh_tok = await self.db.get_setting("radio_twitch_refresh_token")
        broadcaster = await self.db.get_setting("radio_twitch_broadcaster_login")
        if client_id and refresh_tok and broadcaster:
            self._twitch_chat = TwitchBot(self.db, key_prefix="radio_twitch")
            ok, msg = await self._twitch_chat.start()
            if not ok:
                print(f"[radio] Twitch bot disabled: {msg}")
                self._twitch_chat = None
            else:
                print(f"[radio] Twitch bot ready ({msg}).")
        self.is_running = True
        self._loading = False
        self.current_index = 0
        await self._launch()
        return await self.get_status()

    async def stop(self):
        self.is_running = False
        await self._teardown()
        self._cleanup_temp()
        if self._twitch_chat:
            await self._twitch_chat.stop()
            self._twitch_chat = None
        self.current_song = None
        return await self.get_status()

    async def skip_next(self):
        if not self.is_running:
            return {"error": "Stream is not running."}
        self.current_index = (self.current_index + 1) % len(self.playlist)
        await self._teardown()
        await self._launch()
        return await self.get_status()

    async def skip_prev(self):
        if not self.is_running:
            return {"error": "Stream is not running."}
        self.current_index = (self.current_index - 1) % len(self.playlist)
        await self._teardown()
        await self._launch()
        return await self.get_status()

    async def reload_playlist(self):
        if not self.is_running:
            return {"error": "Stream is not running."}
        new_songs = await self.db.get_all_radio_songs(active_only=True)
        if not new_songs:
            return {"error": "No active songs found."}
        old_count = len(self.playlist)
        shuffle = (await self.db.get_setting("radio_shuffle") or "0") == "1"
        if shuffle:
            random.shuffle(new_songs)
        self.playlist = new_songs
        self.current_index = 0
        print(f"[radio] Playlist reloaded: {old_count} -> {len(self.playlist)} songs")
        await self._teardown()
        await self._launch()
        return await self.get_status()

    async def reload_pip(self):
        """Hot-swap PiP overlay by gracefully restarting ffmpeg at the current song position."""
        if not self.is_running:
            return {"error": "Stream is not running."}
        # Preserve elapsed time so we can resume at the right point in the playlist
        elapsed = time.monotonic() - self._start_time if self._start_time else 0
        print(f"[radio] PiP reload: restarting encoder (elapsed {elapsed:.0f}s)")
        await self._teardown()
        await self._launch()
        return await self.get_status()

    # ------------------------------------------------------------------ #

    async def _launch(self):
        # Cancel any leftover tasks from a previous launch (e.g. restart from monitor)
        for t in getattr(self, "_tasks", []):
            if not t.done():
                t.cancel()
        self._tasks = []
        self._concat_start = self.current_index
        # Write initial overlay but leave current_song=None so tracker
        # detects first song as a change (triggers lyrics + chat post)
        self._write_overlay(self.playlist[self.current_index])
        self.current_song = None
        playlist_path = self._build_concat_file()
        cmd = await self._build_cmd(playlist_path)
        # Debug: log command and playlist
        print(f"[radio] Starting encoder (song {self.current_index + 1}/{len(self.playlist)})")
        print(f"[radio] CMD: {' '.join(cmd[:12])} ...")
        try:
            with open(playlist_path) as pf:
                lines = pf.readlines()[:5]
                print(f"[radio] Concat file ({len(open(playlist_path).readlines())} lines): {lines}")
        except Exception as e:
            print(f"[radio] Could not read concat file: {e}")
        self._process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        self._start_time = time.monotonic()
        self._last_song_change = 0.0
        self._restart_delay = 5
        self._restart_retries = 0
        self._tasks = [
            asyncio.create_task(self._track_now_playing()),
            asyncio.create_task(self._monitor_process()),
        ]

    async def _teardown(self):
        self._chat_announcement_token += 1
        if self._chat_announcement_task and not self._chat_announcement_task.done():
            self._chat_announcement_task.cancel()
            try:
                await self._chat_announcement_task
            except asyncio.CancelledError:
                pass
        self._chat_announcement_task = None
        for t in self._tasks:
            t.cancel()
            try:
                await t
            except Exception:
                pass
        self._tasks = []
        if self._process:
            try:
                self._process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
            self._process = None

    def _schedule_now_playing_announcement(self, song: dict):
        """Post a stable song change to Twitch chat after a short delay."""
        if not self._twitch_chat:
            return
        if self._chat_announcement_task and not self._chat_announcement_task.done():
            self._chat_announcement_task.cancel()
        self._chat_announcement_token += 1
        token = self._chat_announcement_token
        self._chat_announcement_task = asyncio.create_task(
            self._post_now_playing_delayed(dict(song), token)
        )

    async def _post_now_playing_delayed(self, song: dict, token: int):
        try:
            await asyncio.sleep(_NOW_PLAYING_CHAT_DELAY)
            if (
                not self.is_running
                or token != self._chat_announcement_token
                or not self._twitch_chat
                or not self.current_song
                or self.current_song.get("id") != song.get("id")
            ):
                return

            song_key = (
                str(song.get("id") or ""),
                str(song.get("title") or ""),
                str(song.get("suno_url") or ""),
            )
            now = time.monotonic()
            duration = max(30.0, float(song.get("duration") or 180.0) * 0.9)
            if (
                song_key == self._last_announced_song_key
                and now - self._last_announced_at < duration
            ):
                print(f"[radio] Duplicate Now Playing suppressed: {song.get('title', '')}")
                return

            chat_msg = f"\U0001F3B5 Now Playing: {song['title']} - {song['artist']}"
            suno_url = song.get("suno_url", "")
            if suno_url:
                chat_msg += f" | {suno_url}"
            await self._twitch_chat.send(chat_msg)
            self._last_announced_song_key = song_key
            self._last_announced_at = now
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            print(f"[radio] Delayed Now Playing error: {exc}")

    def _cleanup_temp(self):
        if self._temp_dir:
            shutil.rmtree(self._temp_dir, ignore_errors=True)
            self._temp_dir = None

    async def _fetch_lyrics(self, suno_url: str) -> list[str]:
        """Scrape lyrics from a Suno song page. Returns list of text lines."""
        if not suno_url:
            return []
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(suno_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return []
                    html = await resp.text()
                    # Lyrics are in the metadata.prompt field inside Next.js RSC payload
                    # Format: self.__next_f.push([1,"48:T<hex>,"]) followed by push with the text
                    match = re.search(
                        r'self\.__next_f\.push\(\[1,"[0-9a-f]+:T[0-9a-f]+,"\]\)</script>'
                        r'<script>self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>',
                        html, re.DOTALL
                    )
                    if not match:
                        return []
                    raw = match.group(1)
                    # Properly decode all JSON string escapes (incl. \uXXXX —
                    # the old `.replace()` chain missed this and left literal
                    # `\u00e4` etc. in the lyrics).
                    raw = _decode_json_string(raw)
                    lines = _clean_lyric_lines(raw)
                    print(f"[radio] Scraped {len(lines)} lyrics lines from {suno_url}")
                    return lines
        except Exception as e:
            print(f"[radio] Lyrics scrape error: {e}")
            return []

    def _write_lyrics(self, lines: list[str], elapsed_in_song: float,
                      song_duration: float, window: int = _LYRICS_WINDOW_LINES):
        """Render a credits-roll style lyrics window.

        Lyrics enter from the bottom and scroll up so that **all** lines pass
        through the visible window exactly once during the song. Empty padding
        is inserted before the first line and after the last line, so the song
        starts with a calm empty window, then lyrics scroll in, all lines pass
        through, and the window empties again towards the song's end.
        """
        if not lines:
            text = " "
        else:
            max_chars = getattr(self, "_lyrics_max_chars", _LYRICS_MAX_LINE_CHARS)
            wrapped_lines = []
            for line in lines:
                if not line:
                    if wrapped_lines and wrapped_lines[-1] != "":
                        wrapped_lines.append("")
                    continue
                wrapped_lines.extend(textwrap.wrap(
                    line,
                    width=max_chars,
                    break_long_words=False,
                    break_on_hyphens=False,
                ) or [line])

            n = len(wrapped_lines)
            # Total scroll positions: padding + lines + padding
            total_steps = n + window
            # Per-song step interval, clamped so very short or very long
            # songs still look reasonable.
            interval = max(_LYRICS_MIN_INTERVAL,
                           min(_LYRICS_MAX_INTERVAL,
                               (max(song_duration, 1.0) * _LYRICS_SCROLL_DURATION_FACTOR)
                               / max(total_steps, 1)))
            step = int(max(0.0, elapsed_in_song) / interval)
            step = max(0, min(step, total_steps))
            padded = [""] * window + wrapped_lines + [""] * window
            visible = padded[step:step + window]
            # Always keep `window` lines so the overlay height never jumps.
            while len(visible) < window:
                visible.append("")
            text = "\n".join(visible) or " "
        # Atomic write: ffmpeg reads this file every frame (reload=1), so a
        # mid-write read can corrupt drawtext's parser and crash the encoder.
        try:
            tmp = self._lyrics_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, self._lyrics_path)
        except Exception as e:
            print(f"[radio] Lyrics write error: {e}")

    async def _resolve_font(self) -> str:
        """Return font family name for fontconfig-based rendering."""
        # Use fontconfig (font= instead of fontfile=) for automatic
        # per-character fallback across all installed Noto fonts
        print("[radio] Using fontconfig: font='Noto Sans'")
        return "Noto Sans"

    def _build_concat_file(self) -> str:
        path = os.path.join(self._temp_dir, "playlist.txt")
        n = len(self.playlist)
        # Determine base directory for audio files
        if getattr(self, "_source_mode", "submissions") == "suno_playlist" and self._suno_dl_dir:
            base_dir = self._suno_dl_dir
        else:
            base_dir = self.radio_dir
        with open(path, "w") as f:
            for _ in range(_PLAYLIST_REPEATS):
                for i in range(n):
                    idx = (self._concat_start + i) % n
                    audio = os.path.join(base_dir, self.playlist[idx]["filename"])
                    if os.path.exists(audio):
                        safe = audio.replace("'", "'\\''")
                        f.write(f"file '{safe}'\n")
        return path

    async def _write_header(self):
        """Write the configured stream name to the header overlay file."""
        title = (await self.db.get_setting("radio_stream_name") or "Twitch Radio").strip()
        title = title or "Twitch Radio"
        title = _normalize_text(title)
        try:
            tmp = self._header_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(title)
            os.replace(tmp, self._header_path)
            print(f"[radio] Header overlay: {title}")
        except Exception as e:
            print(f"[radio] Header write error: {e}")

    async def _write_disclaimer(self):
        """Write the optional persistent disclaimer used by FFmpeg drawtext."""
        enabled = (await self.db.get_setting("radio_disclaimer_enabled") or "off") == "on"
        text = (await self.db.get_setting("radio_disclaimer_text") or "").strip()
        if not enabled:
            text = ""
        # Keep intentional line breaks while removing unsupported control glyphs.
        text = "\n".join(_normalize_text(line) for line in text.splitlines())
        try:
            tmp = self._disclaimer_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, self._disclaimer_path)
            if text:
                print("[radio] Persistent disclaimer enabled")
        except Exception as e:
            print(f"[radio] Disclaimer write error: {e}")

    def _write_overlay(self, song: dict):
        # Transliterate exotic decorations so artist names like "꧁༺ Tαɾʝα ༻꧂"
        # render as something legible instead of being aggressively stripped.
        text = f"{song['title']}  \u2014  {song['artist']}"
        text = _transliterate_for_overlay(text)
        try:
            tmp = self._overlay_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, self._overlay_path)
        except Exception as e:
            print(f"[radio] Overlay write error: {e}")

    async def _build_cmd(self, playlist_path: str) -> list:
        bg_filename = await self.db.get_setting("radio_background_filename")
        bg_type = await self.db.get_setting("radio_background_type") or "image"

        cmd = ["ffmpeg", "-y"]

        # Input 0: background
        if bg_filename and os.path.exists(os.path.join(self.radio_dir, bg_filename)):
            bg_path = os.path.join(self.radio_dir, bg_filename)
            if bg_type == "video":
                cmd += ["-stream_loop", "-1", "-re", "-i", bg_path]
            else:
                cmd += ["-loop", "1", "-re", "-i", bg_path]
        else:
            cmd += ["-f", "lavfi", "-i", "color=c=black:s=1920x1080:r=30"]

        # Input 1: audio concat
        cmd += ["-f", "concat", "-safe", "0", "-i", playlist_path]

        # PiP settings
        pip_mode = await self.db.get_setting("radio_pip_mode") or "off"
        pip_input_idx = None  # Will be set if PiP is active

        if pip_mode == "local":
            pip_fn = await self.db.get_setting("radio_pip_filename") or ""
            pip_ft = await self.db.get_setting("radio_pip_file_type") or "image"
            pip_path = os.path.join(self.radio_dir, pip_fn) if pip_fn else ""
            if pip_path and os.path.exists(pip_path):
                # Input 2: PiP local file
                if pip_ft == "video":
                    cmd += ["-stream_loop", "-1", "-i", pip_path]
                else:
                    cmd += ["-loop", "1", "-i", pip_path]
                pip_input_idx = 2
                print(f"[radio] PiP: local {pip_ft} ({pip_fn})")
            else:
                print(f"[radio] PiP: local file not found, skipping")

        elif pip_mode == "rtmp":
            pip_rtmp_key = await self.db.get_setting("radio_pip_rtmp_key") or ""
            if pip_rtmp_key:
                # Input 2: RTMP listener — OBS sends to this endpoint
                rtmp_url = f"rtmp://0.0.0.0:1936/live/{pip_rtmp_key}"
                cmd += [
                    "-f", "flv", "-listen", "1",
                    "-rw_timeout", "5000000",  # 5s timeout waiting for connection
                    "-i", rtmp_url,
                ]
                pip_input_idx = 2
                print(f"[radio] PiP: RTMP listener on port 1936")
            else:
                print("[radio] PiP: RTMP mode but no key configured, skipping")

        # Use fontconfig for automatic fallback across all Noto fonts
        font = self._font_path
        now_playing = (
            f"drawtext=font='{font}'"
            f":textfile='{self._overlay_path}'"
            f":reload=1"
            f":fontsize=28:fontcolor=white"
            f":borderw=2:bordercolor=black"
            f":x=(w-text_w)/2:y=h-60"
        )
        # Lyrics: fixed-width semi-transparent box on the left so it never
        # resizes with content and never overlaps the PiP camera on the right.
        # Width is configurable: 80% (default, 1140 px) → 60% → 40% of frame.
        lyrics_box_x, lyrics_box_y = 40, 140
        _lyrics_width_pct = int(await self.db.get_setting("radio_lyrics_width") or "80")
        lyrics_box_w = max(200, round(1140 * _lyrics_width_pct / 80))
        lyrics_box_h = 800
        lyrics_pad = 24
        # Approximate proportional Noto Sans width at 28 px, minus padding.
        self._lyrics_max_chars = max(18, int((lyrics_box_w - 2 * lyrics_pad) / 14))
        lyrics_box = (
            f"drawbox=x={lyrics_box_x}:y={lyrics_box_y}"
            f":w={lyrics_box_w}:h={lyrics_box_h}"
            f":color=black@0.45:t=fill"
        )
        lyrics = (
            f"drawtext=font='{font}'"
            f":textfile='{self._lyrics_path}'"
            f":reload=1"
            f":fontsize=28:fontcolor=white:line_spacing=7"
            f":borderw=2:bordercolor=black@0.9"
            f":x={lyrics_box_x + lyrics_pad}"
            f":y={lyrics_box_y + lyrics_pad}"
        )
        header = (
            f"drawtext=font='{font}'"
            f":textfile='{self._header_path}'"
            f":reload=1"
            f":fontsize=42:fontcolor=white"
            f":borderw=3:bordercolor=black"
            f":x=(w-text_w)/2:y=30"
        )
        disclaimer_enabled = (
            (await self.db.get_setting("radio_disclaimer_enabled") or "off") == "on"
        )
        disclaimer_text = (await self.db.get_setting("radio_disclaimer_text") or "").strip()
        disclaimer = None
        if disclaimer_enabled and disclaimer_text:
            disclaimer = (
                f"drawtext=font='{font}'"
                f":textfile='{self._disclaimer_path}'"
                f":expansion=none"
                f":fontsize=28:fontcolor=white:line_spacing=5"
                f":borderw=2:bordercolor=black@0.9"
                f":box=1:boxcolor=black@0.55:boxborderw=12"
                f":fix_bounds=1"
                f":x=40:y=h-text_h-100"
            )

        # --- Song-Video PiP (second overlay) ---------------------------------
        song_pip_input_idx = None
        song_pip_enabled = (await self.db.get_setting("radio_song_pip_enabled") or "off") == "on"
        if song_pip_enabled and self._song_pip_ready and self._song_pip_paths:
            song_pip_concat = self._build_song_pip_concat()
            if song_pip_concat:
                # Next available input index (after background + audio + optional PiP)
                song_pip_input_idx = 2 if pip_input_idx is None else pip_input_idx + 1
                cmd += ["-f", "concat", "-safe", "0", "-i", song_pip_concat]
                print(f"[radio] Song-PiP input idx={song_pip_input_idx}")

        # --- Build overlay chain ------------------------------------------
        # Helper: resolve PiP dimensions + position for any pip block
        def _pip_dims_pos(fmt_key, scale_key, pos_key, defaults):
            fmt = defaults.get(fmt_key, "16:9")
            scale_pct = int(defaults.get(scale_key, 25))
            position = defaults.get(pos_key, "center-right")
            if fmt == "9:16":
                h = int(1080 * scale_pct / 100)
                w = int(h * 9 / 16)
            elif fmt == "1:1":
                h = int(1080 * scale_pct / 100)
                w = h
            else:  # 16:9
                w = int(1920 * scale_pct / 100)
                h = int(w * 9 / 16)
            # Force even dimensions (required for yuv420p; odd values cause
            # scale to round up beyond the pad target → "pad smaller than input")
            w = (w // 2) * 2
            h = (h // 2) * 2
            pad = 40
            pos_map = {
                "top-left":      (f"{pad}", f"{pad}"),
                "top-center":    (f"(W-w)/2", f"{pad}"),
                "top-right":     (f"W-w-{pad}", f"{pad}"),
                "center-left":   (f"{pad}", f"(H-h)/2"),
                "center":        (f"(W-w)/2", f"(H-h)/2"),
                "center-right":  (f"W-w-{pad}", f"(H-h)/2"),
                "bottom-left":   (f"{pad}", f"H-h-{pad}"),
                "bottom-center": (f"(W-w)/2", f"H-h-{pad}"),
                "bottom-right":  (f"W-w-{pad}", f"H-h-{pad}"),
            }
            ox, oy = pos_map.get(position, pos_map["center-right"])
            return w, h, ox, oy

        # Collect active overlays: (input_idx, w, h, ox, oy)
        overlays = []
        if pip_input_idx is not None:
            pip_fmt      = await self.db.get_setting("radio_pip_format") or "16:9"
            pip_scale    = int(await self.db.get_setting("radio_pip_scale") or "25")
            pip_position = await self.db.get_setting("radio_pip_position") or "center-right"
            pw, ph, pox, poy = _pip_dims_pos(
                "fmt", "scale", "pos",
                {"fmt": pip_fmt, "scale": pip_scale, "pos": pip_position}
            )
            overlays.append((pip_input_idx, pw, ph, pox, poy))

        if song_pip_input_idx is not None:
            spip_fmt      = await self.db.get_setting("radio_song_pip_format") or "9:16"
            spip_scale    = int(await self.db.get_setting("radio_song_pip_scale") or "20")
            spip_position = await self.db.get_setting("radio_song_pip_position") or "top-right"
            sw, sh, sox, soy = _pip_dims_pos(
                "fmt", "scale", "pos",
                {"fmt": spip_fmt, "scale": spip_scale, "pos": spip_position}
            )
            overlays.append((song_pip_input_idx, sw, sh, sox, soy))

        text_filters = ",".join(
            filter(None, (header, now_playing, lyrics_box, lyrics, disclaimer))
        )

        if overlays:
            # Chain overlays: bg → pip0 → [pip1 →] drawtext → [vout]
            bg = (
                "[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,"
                "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,format=yuv420p[bg0]"
            )
            parts = [bg]
            prev = "bg0"
            for i, (inp, w, h, ox, oy) in enumerate(overlays):
                scale = (
                    f"[{inp}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
                    f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2[p{i}]"
                )
                parts.append(scale)
                if i == len(overlays) - 1:
                    # song_pip is always the last overlay; eof_action=pass keeps
                    # the stream alive if the concat unexpectedly exhausts.
                    is_song_pip = (inp == song_pip_input_idx)
                    eof = ":eof_action=pass" if is_song_pip else ""
                    parts.append(
                        f"[{prev}][p{i}]overlay={ox}:{oy}{eof},{text_filters}[vout]"
                    )
                else:
                    nxt = f"m{i}"
                    parts.append(f"[{prev}][p{i}]overlay={ox}:{oy}[{nxt}]")
                    prev = nxt
            fc = ";".join(parts)
            cmd += ["-filter_complex", fc, "-map", "[vout]", "-map", "1:a:0"]
        else:
            # Simple filter without any PiP
            cmd += ["-map", "0:v:0", "-map", "1:a:0"]
            vf = (
                "scale=1920:1080:force_original_aspect_ratio=decrease,"
                "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,"
                f"format=yuv420p,{text_filters}"
            )
            cmd += ["-vf", vf]

        cmd += [
            "-c:v", "libx264", "-profile:v", "main", "-level", "4.0",
            "-preset", "ultrafast",
            "-pix_fmt", "yuv420p",
            "-b:v", "2000k", "-maxrate", "2000k", "-bufsize", "4000k",
            "-r", "30",
            "-g", "60", "-keyint_min", "60",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
            "-shortest",
            "-f", "flv",
            f"rtmp://live.twitch.tv/app/{self._twitch_key}",
        ]
        return cmd

    async def _track_now_playing(self):
        n = len(self.playlist)
        durations = []
        ordered = []
        for i in range(n):
            idx = (self._concat_start + i) % n
            s = self.playlist[idx]
            durations.append(s.get("duration", 180))
            ordered.append(s)
        # Build cumulative boundary list for the full concat (50 repeats)
        boundaries = []
        for _ in range(_PLAYLIST_REPEATS):
            for dur in durations:
                prev = boundaries[-1] if boundaries else 0.0
                boundaries.append(prev + dur)
        total = boundaries[-1] if boundaries else 0
        if total <= 0:
            return
        print(f"[radio] Tracker: {n} songs, single-pass={sum(durations):.0f}s, total={total:.0f}s")
        try:
            while self.is_running:
                elapsed = time.monotonic() - self._start_time
                # Find which song index we're in (linear, no modulo — matches concat file exactly)
                song_i = 0
                for i, end_t in enumerate(boundaries):
                    if elapsed < end_t:
                        song_i = i % n
                        break
                else:
                    song_i = 0  # wrapped past all repeats
                actual = ordered[song_i]
                now = time.monotonic()
                last_change = getattr(self, "_last_song_change", 0.0)
                if (self.current_song is None or self.current_song["id"] != actual["id"]) and (now - last_change) > 5:
                    self.current_song = actual
                    self._last_song_change = now
                    self.current_index = (self._concat_start + song_i) % n
                    self._write_overlay(actual)
                    print(f"[radio] Now playing: {actual['title']} by {actual['artist']}")
                    self._schedule_now_playing_announcement(actual)
                    # Scrape lyrics async for this song
                    song_id = actual["id"]
                    if song_id not in self._lyrics_cache:
                        suno_url = actual.get("suno_url", "")
                        self._lyrics_cache[song_id] = await self._fetch_lyrics(suno_url)
                    # Mark when this song actually started, so the credits-roll
                    # is anchored to the real song timeline.
                    self._song_start_time = now
                    # Boundary of the current song inside the concat sequence.
                    prev_boundary = boundaries[i - 1] if i > 0 else 0.0
                    self._song_duration = max(1.0, end_t - prev_boundary)
                    self._write_lyrics(
                        self._lyrics_cache.get(song_id, []),
                        elapsed_in_song=0.0,
                        song_duration=self._song_duration,
                    )
                # Continuously refresh the credits-roll position based on how
                # far into the *current* song we are.
                song_id = actual["id"]
                lyrics = self._lyrics_cache.get(song_id, [])
                if lyrics and getattr(self, "_song_start_time", None):
                    elapsed_in_song = max(0.0, time.monotonic() - self._song_start_time)
                    self._write_lyrics(
                        lyrics,
                        elapsed_in_song=elapsed_in_song,
                        song_duration=getattr(self, "_song_duration", 180.0),
                    )
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass

    async def _monitor_process(self):
        if not self._process:
            return
        try:
            # Read stderr in chunks (ffmpeg uses \r for progress, not \n)
            while True:
                try:
                    data = await asyncio.wait_for(
                        self._process.stderr.read(4096), timeout=60,
                    )
                except asyncio.TimeoutError:
                    # ffmpeg hung (likely RTMP connection lost)
                    print("[radio] Watchdog: no ffmpeg output for 60s, killing process")
                    try:
                        self._process.kill()
                    except Exception:
                        pass
                    break
                if not data:
                    break
                text = data.decode(errors="replace")
                # Log only lines with actual content, skip noisy progress
                for line in text.replace("\r", "\n").splitlines():
                    line = line.strip()
                    if line and not line.startswith("frame="):
                        print(f"[radio/ffmpeg] {line}")
            code = await self._process.wait()
            if self.is_running:
                print(f"[radio] Encoder exited (code {code})")
                self._process = None
                # Exponential backoff for restarts
                delay = getattr(self, "_restart_delay", 5)
                retries = getattr(self, "_restart_retries", 0)
                if retries >= 10:
                    print("[radio] Too many restart attempts (10), giving up. Use admin panel to restart.")
                    self.is_running = False
                    return
                self._restart_retries = retries + 1
                print(f"[radio] Restarting encoder in {delay}s (attempt {self._restart_retries}/10)...")
                await asyncio.sleep(delay)
                self._restart_delay = min(delay * 2, 60)  # max 60s
                if self.is_running:
                    await self._launch()
        except asyncio.CancelledError:
            pass
