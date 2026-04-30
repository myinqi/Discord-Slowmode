"""Manages ffmpeg streaming to Twitch RTMP.

Uses ffmpeg's concat demuxer to play songs back-to-back in a single process,
ensuring seamless transitions without stream interruptions.
"""

import asyncio
import os
import random
import re
import shutil
import tempfile
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
_LYRICS_WINDOW_LINES = 20   # how many lyrics lines are visible at once
_LYRICS_MIN_INTERVAL = 0.5  # min seconds between scroll steps (very short songs)
_LYRICS_MAX_INTERVAL = 4.0  # max seconds between scroll steps (very long songs)

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
    "\u200B": "",   "\u200C": "",     # zero-width spaces
    "\u200D": "",   "\uFEFF": "",
    "\u2028": "",   "\u2029": "",    # line / paragraph separators (render as box)
})

# Hard limit on how many chars we render per lyric line — anything longer is
# truncated with an ellipsis so the visual lyrics box has a stable width.
_LYRICS_MAX_LINE_CHARS = 85


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
        else:
            # unidecode never returns None — it falls back to "" for chars
            # it has no mapping for, which is fine.
            out.append(unidecode(ch))
    # Collapse the runs of whitespace / decoration noise that transliterate
    # may have introduced (e.g. "꧁༺" → "{[").
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
        self._suno_dl_dir = None  # temp dir for downloaded Suno playlist songs
        self._header_path = None  # playlist title overlay file
        self._loading = False  # guard against concurrent start() calls

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

    async def start(self):
        if self.is_running:
            return {"error": "Stream is already running."}
        if self._loading:
            return {"error": "Stream is currently loading, please wait."}
        self._loading = True

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
        self._temp_dir = tempfile.mkdtemp(prefix="radio_")
        self._overlay_path = os.path.join(self._temp_dir, "nowplaying.txt")
        self._lyrics_path = os.path.join(self._temp_dir, "lyrics.txt")
        self._header_path = os.path.join(self._temp_dir, "header.txt")
        with open(self._lyrics_path, "w", encoding="utf-8") as f:
            f.write(" ")
        # Write playlist title header
        await self._write_header()
        # Connect to Twitch chat if configured (Helix-based bot, modern auth)
        client_id = await self.db.get_setting("radio_twitch_client_id")
        refresh_tok = await self.db.get_setting("radio_twitch_refresh_token")
        broadcaster = await self.db.get_setting("radio_twitch_broadcaster_login")
        if client_id and refresh_tok and broadcaster:
            self._twitch_chat = TwitchBot(self.db)
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
                    lines = []
                    for line in raw.split("\n"):
                        cleaned = _normalize_text(line)
                        if cleaned:
                            lines.append(cleaned)
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
            n = len(lines)
            # Total scroll positions: padding + lines + padding
            total_steps = n + window
            # Per-song step interval, clamped so very short or very long
            # songs still look reasonable.
            interval = max(_LYRICS_MIN_INTERVAL,
                           min(_LYRICS_MAX_INTERVAL,
                               max(song_duration, 1.0) / max(total_steps, 1)))
            step = int(max(0.0, elapsed_in_song) / interval)
            step = max(0, min(step, total_steps))
            padded = [""] * window + lines + [""] * window
            visible = padded[step:step + window]
            # Always keep `window` lines so the overlay height never jumps.
            while len(visible) < window:
                visible.append("")
            # Truncate over-long lines so they never escape the fixed box width.
            visible = [
                (ln if len(ln) <= _LYRICS_MAX_LINE_CHARS
                 else ln[:_LYRICS_MAX_LINE_CHARS - 1].rstrip() + "…")
                for ln in visible
            ]
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
        """Write the playlist title to the header overlay file."""
        source_mode = getattr(self, "_source_mode", "submissions")
        if source_mode == "suno_playlist":
            active_id = await self.db.get_setting("radio_active_suno_playlist")
            if active_id:
                pl = await self.db.get_suno_playlist(int(active_id))
                title = (pl["description"] if pl and pl.get("description") else "Suno Playlist")
            else:
                title = "Suno Playlist"
        else:
            title = "Submissions Playlist"
        title = _normalize_text(title)
        try:
            tmp = self._header_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write(title)
            os.replace(tmp, self._header_path)
            print(f"[radio] Header overlay: {title}")
        except Exception as e:
            print(f"[radio] Header write error: {e}")

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
        # The 1920×1080 frame: PiP at x=1500–1880, lyrics box at x=40–1180.
        lyrics_box_x, lyrics_box_y = 40, 140
        lyrics_box_w, lyrics_box_h = 1140, 800
        lyrics_pad = 24
        lyrics_box = (
            f"drawbox=x={lyrics_box_x}:y={lyrics_box_y}"
            f":w={lyrics_box_w}:h={lyrics_box_h}"
            f":color=black@0.45:t=fill"
        )
        lyrics = (
            f"drawtext=font='{font}'"
            f":textfile='{self._lyrics_path}'"
            f":reload=1"
            f":fontsize=22:fontcolor=white:line_spacing=4"
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

        if pip_input_idx is not None:
            # Build filter_complex with PiP overlay
            pip_format = await self.db.get_setting("radio_pip_format") or "16:9"
            pip_scale_pct = int(await self.db.get_setting("radio_pip_scale") or "25")
            pip_position = await self.db.get_setting("radio_pip_position") or "center-right"

            # Calculate PiP dimensions based on aspect ratio and scale
            if pip_format == "9:16":
                pip_h = int(1080 * pip_scale_pct / 100)
                pip_w = int(pip_h * 9 / 16)
            else:  # 16:9
                pip_w = int(1920 * pip_scale_pct / 100)
                pip_h = int(pip_w * 9 / 16)

            # Position mapping with 40px padding
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
            ox, oy = pos_map.get(pip_position, pos_map["center-right"])

            fc = (
                f"[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,"
                f"pad=1920:1080:(ow-iw)/2:(oh-ih)/2,format=yuv420p[bg];"
                f"[{pip_input_idx}:v]scale={pip_w}:{pip_h}:force_original_aspect_ratio=decrease,"
                f"pad={pip_w}:{pip_h}:(ow-iw)/2:(oh-ih)/2[pip];"
                f"[bg][pip]overlay={ox}:{oy},"
                f"{header},{now_playing},{lyrics_box},{lyrics}[vout]"
            )
            cmd += ["-filter_complex", fc, "-map", "[vout]", "-map", "1:a:0"]
        else:
            # Simple filter without PiP
            cmd += ["-map", "0:v:0", "-map", "1:a:0"]
            vf = (
                "scale=1920:1080:force_original_aspect_ratio=decrease,"
                "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,"
                f"format=yuv420p,{header},{now_playing},{lyrics_box},{lyrics}"
            )
            cmd += ["-vf", vf]

        cmd += [
            "-c:v", "libx264", "-profile:v", "main", "-level", "4.0",
            "-preset", "ultrafast",
            "-pix_fmt", "yuv420p",
            "-b:v", "2500k", "-maxrate", "2500k", "-bufsize", "5000k",
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
                    # Post to Twitch chat
                    if self._twitch_chat:
                        suno_url = actual.get("suno_url", "")
                        chat_msg = f"\U0001F3B5 Now Playing: {actual['title']} - {actual['artist']}"
                        if suno_url:
                            chat_msg += f" | {suno_url}"
                        await self._twitch_chat.send(chat_msg)
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
