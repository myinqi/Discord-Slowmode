"""Experimental Radio – background processing pipeline.

For each submitted song:
  1. Download MP3 from Suno CDN
  2. Scrape title / artist / cover / video URL from Suno embed
  3. Clean lyrics (strip section tags like [Verse], [Chorus])
  4. Run faster-whisper (in thread pool) → word-level timestamps
  5. Generate ASS subtitle file with moving-window karaoke style
  6. Update DB with all results throughout
"""

import asyncio
import aiohttp
import html as _html
import json
import os
import re
import time

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

EXP_RIGHTS_DECLARATION = (
    "I confirm that I created this song on Suno.ai, hold the necessary rights to stream it, "
    "and grant a non-exclusive 14-day streaming license for Twitch live streams and VODs. "
    "I confirm that the content complies with the community content guidelines "
    "(no hate speech, harassment, explicit or illegal content)."
)

EXP_TERMS_SHORT = (
    "**By submitting you confirm:**\n"
    "• You created this song on Suno.ai and hold the streaming rights\n"
    "• You grant a **14-day** streaming license for Twitch live streams & VODs\n"
    "• The content complies with community guidelines (no hate speech, explicit/illegal content)\n"
    "• Songs expire and are deleted automatically after 14 days\n"
    "• Maximum **3 songs** per user"
)


MAX_DURATION_SECS = 300  # 5 minutes


# ─── Directory helpers ────────────────────────────────────────────────────────

def ensure_exp_dirs(exp_radio_dir: str):
    for sub in ("mp3", "ass", "assets"):
        os.makedirs(os.path.join(exp_radio_dir, sub), exist_ok=True)


# ─── MP3 download ─────────────────────────────────────────────────────────────

async def download_mp3(uuid: str, mp3_dir: str) -> str | None:
    """Download Suno MP3 from CDN. Returns local path or None."""
    dest = os.path.join(mp3_dir, f"{uuid}.mp3")
    if os.path.exists(dest):
        return dest
    url = f"https://cdn1.suno.ai/{uuid}.mp3"
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": _BROWSER_UA}) as sess:
            async with sess.get(url, timeout=aiohttp.ClientTimeout(total=90)) as resp:
                if resp.status != 200:
                    print(f"[exp-radio] MP3 HTTP {resp.status} for {uuid}", flush=True)
                    return None
                data = await resp.read()
        with open(dest, "wb") as f:
            f.write(data)
        print(f"[exp-radio] Downloaded {uuid}.mp3 ({len(data) // 1024} KB)", flush=True)
        return dest
    except Exception as e:
        print(f"[exp-radio] MP3 download error {uuid}: {e}", flush=True)
        return None


# ─── Suno metadata scrape ─────────────────────────────────────────────────────

_FULL_UUID_RE = re.compile(
    r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$'
)


async def scrape_suno(uuid: str) -> dict:
    """Fetch title, artist, cover_url, video_url, raw_lyrics from Suno embed.
    Also returns 'real_uuid' (full UUID) which may differ from the short ID."""
    result = {"title": None, "artist": None, "cover_url": None,
              "video_url": None, "raw_lyrics": None, "real_uuid": None}
    is_full = bool(_FULL_UUID_RE.match(uuid))
    url = f"https://suno.com/song/{uuid}" if is_full else f"https://suno.com/s/{uuid}"
    try:
        async with aiohttp.ClientSession(headers={"User-Agent": _BROWSER_UA}) as sess:
            async with sess.get(url, timeout=aiohttp.ClientTimeout(total=25)) as resp:
                if resp.status != 200:
                    return result
                page = await resp.text()

        # Extract real full UUID from RSC payload
        m = re.search(
            r'"id"\s*:\s*"([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})"',
            page,
        )
        if not m:
            m = re.search(
                r'song/([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})',
                page,
            )
        result["real_uuid"] = m.group(1) if m else (uuid if is_full else None)

        # og:title → "Song Title by Artist – Suno"
        # Try both attribute orderings (property before content, or content before property)
        raw_title = None
        for pat in [
            r'<meta\s+(?:name|property)=["\']og:title["\']\s+content=["\']([^"\']+)["\']',
            r'<meta\s+content=["\']([^"\']+)["\']\s+(?:name|property)=["\']og:title["\']',
        ]:
            m = re.search(pat, page)
            if m:
                raw_title = _html.unescape(m.group(1)).strip()
                break
        # Fallback: <title> tag
        if not raw_title:
            m = re.search(r'<title>([^<]+)</title>', page)
            if m:
                raw_title = _html.unescape(m.group(1)).strip()

        if raw_title:
            raw_title = re.sub(r'\s*[|\-–]\s*Suno\s*$', '', raw_title, flags=re.IGNORECASE).strip()
            bm = re.search(r'^(.+?)\s+by\s+(.+)$', raw_title)
            if bm:
                result["title"] = bm.group(1).strip()
                result["artist"] = bm.group(2).strip()
            else:
                result["title"] = raw_title

        # Artist fallback: display_name from RSC payload
        # Iterate matches in REVERSE — song owner is usually the last display_name on the page.
        # Filter out version strings (v5.5), "Cover", "Remix", and single-char names.
        if not result["artist"]:
            candidates = re.findall(r'display_name\\":\\"([^"\\]+)\\"', page)
            if not candidates:
                candidates = re.findall(r'"display_name"\s*:\s*"([^"]+)"', page)
            for dn in reversed(candidates):
                dn = _html.unescape(dn).strip()
                if (len(dn) > 1
                        and not re.match(r'^v\d', dn)
                        and dn not in ("Cover", "Remix")):
                    result["artist"] = dn
                    break

        # Title-based artist extraction fallback (handles "Title by Artist" patterns)
        if not result["artist"] and result["title"]:
            tm = re.search(r'\bby\s+(.+?)(?:\s*\||\s*-\s*Suno|$)', result["title"])
            if tm:
                # Strip the "by Artist" part from the title and use it as artist
                result["artist"] = tm.group(1).strip()
                result["title"] = re.sub(r'\s+by\s+.+$', '', result["title"]).strip()

        # og:image
        m = re.search(r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', page)
        if m:
            result["cover_url"] = _html.unescape(m.group(1))

        # video_cover_url from RSC payload (Suno's actual field name for 9:16 MP4)
        for pat in [
            r'"video_cover_url"\s*:\s*"([^"]+\.mp4[^"]*)"',
            r'video_cover_url\\":\\"([^"\\]+\.mp4[^"\\]*)\\"',
        ]:
            m = re.search(pat, page)
            if m:
                result["video_url"] = m.group(1).replace("\\/", "/")
                break

        # Fallback: /embed/<real_uuid> page exposes video_cover_url more reliably
        # (same approach used by the working Suno Info player)
        if not result["video_url"] and result.get("real_uuid"):
            try:
                async with aiohttp.ClientSession(headers={"User-Agent": _BROWSER_UA}) as sess:
                    embed_url = f"https://suno.com/embed/{result['real_uuid']}"
                    async with sess.get(embed_url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                        if r.status == 200:
                            embed_html = await r.text()
                            m = re.search(r'"video_cover_url"\s*:\s*"([^"]+)"', embed_html)
                            if not m:
                                m = re.search(r'video_cover_url\\":\\"([^"\\]+)\\"', embed_html)
                            if m:
                                result["video_url"] = m.group(1).replace("\\/", "/")
            except Exception as e:
                print(f"[exp-radio] embed fetch error: {e}", flush=True)

        # Lyrics from prompt field (two patterns)
        def _valid(s):
            return s and len(s.strip()) >= 10 and not re.match(r'^\$\w+$', s.strip())

        for pat in [
            r'"prompt"\s*:\s*"((?:[^"\\]|\\.)*)"',
            r'prompt\\":\\"((?:[^\\]|\\.)*?)\\"',
        ]:
            m = re.search(pat, page)
            if m:
                cand = m.group(1).replace("\\n", "\n").replace('\\"', '"')
                if _valid(cand):
                    result["raw_lyrics"] = cand
                    break

        # RSC long-form lyrics fallback
        if not result["raw_lyrics"]:
            rsc = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', page, re.DOTALL)
            for chunk in rsc:
                m = re.search(r'"prompt"\s*:\s*"((?:[^"\\]|\\.)*)"', chunk)
                if m:
                    cand = m.group(1).replace("\\n", "\n").replace('\\"', '"')
                    if _valid(cand):
                        result["raw_lyrics"] = cand
                        break

    except Exception as e:
        print(f"[exp-radio] Suno scrape error {uuid}: {e}", flush=True)

    if not result["cover_url"]:
        result["cover_url"] = f"https://cdn1.suno.ai/image_large_{uuid}.jpeg"

    return result


# ─── Lyrics cleaning ─────────────────────────────────────────────────────────

def clean_lyrics(raw: str) -> str:
    """Remove section headers ([Verse 1], [Chorus], etc.) and normalize whitespace."""
    if not raw:
        return ""
    lines = []
    for line in raw.splitlines():
        if re.match(r'^\s*\[[^\]]+\]\s*$', line):
            continue
        lines.append(line)
    result = []
    prev_blank = False
    for line in lines:
        is_blank = not line.strip()
        if is_blank and prev_blank:
            continue
        result.append(line)
        prev_blank = is_blank
    return "\n".join(result).strip()


# ─── Audio duration ───────────────────────────────────────────────────────────

async def get_duration(mp3_path: str) -> float:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "quiet", "-show_entries", "format=duration",
            "-of", "csv=p=0", mp3_path,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
        return float(out.strip()) if out.strip() else 0.0
    except Exception:
        return 0.0


# ─── Whisper analysis ─────────────────────────────────────────────────────────

def _whisper_sync(mp3_path: str) -> list:
    """Run faster-whisper synchronously (called in thread pool)."""
    from faster_whisper import WhisperModel
    model = WhisperModel("base", device="cpu", compute_type="int8")
    segments, _ = model.transcribe(mp3_path, word_timestamps=True)
    words = []
    for seg in segments:
        for w in (seg.words or []):
            word = w.word.strip()
            if word:
                words.append({
                    "word": word,
                    "start": round(w.start, 3),
                    "end": round(w.end, 3),
                })
    return words


async def run_whisper(mp3_path: str) -> list:
    """Run Whisper in thread pool so the event loop is not blocked."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _whisper_sync, mp3_path)


# ─── ASS subtitle generation ──────────────────────────────────────────────────

def build_ass(words: list, title: str = "", artist: str = "",
              res_w: int = 1920, res_h: int = 1080) -> str:
    """Generate ASS subtitle file with moving-window word highlight.

    Display window:  [gray] [gray] [WHITE BOLD current] [gray] [gray] [gray] [gray]
    Each Dialogue entry lasts from word.start to next_word.start.
    Colors:  current = white (#FFFFFF), context = gray (#808080).
    ASS color format: &HAABBGGRR (alpha=00 → opaque).
    """
    BEFORE = 2
    AFTER  = 4

    def ass_ts(secs: float) -> str:
        h = int(secs // 3600)
        m = int((secs % 3600) // 60)
        s = int(secs % 60)
        cs = int(round((secs - int(secs)) * 100))
        return f"{h}:{m:02d}:{s:02d}.{cs:02d}"

    header = (
        f"[Script Info]\n"
        f"ScriptType: v4.00+\n"
        f"PlayResX: {res_w}\n"
        f"PlayResY: {res_h}\n"
        f"ScaledBorderAndShadow: yes\n"
        f"Title: {title} – {artist}\n\n"
        f"[V4+ Styles]\n"
        f"Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,"
        f"BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,"
        f"BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n"
        f"Style: Default,Arial,54,&H00FFFFFF,&H00808080,&H00000000,&HA0000000,"
        f"0,0,0,0,100,100,0,0,1,3,1,2,80,80,90,1\n\n"
        f"[Events]\n"
        f"Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n"
    )

    if not words:
        return header

    lines = []
    for i, word in enumerate(words):
        start = word["start"]
        end = words[i + 1]["start"] if i + 1 < len(words) else word["end"] + 0.15

        w_start = max(0, i - BEFORE)
        w_end = min(len(words), i + AFTER + 1)
        parts = []
        for j in range(w_start, w_end):
            w = words[j]["word"].replace("{", "").replace("}", "")
            if j < i:
                parts.append(f"{{\\c&H808080&}}{w}")
            elif j == i:
                parts.append(f"{{\\c&HFFFFFF&}}{{\\b1}}{w}{{\\b0}}")
            else:
                parts.append(f"{{\\c&H808080&}}{w}")
        text = " ".join(parts) + "{\\r}"
        lines.append(
            f"Dialogue: 0,{ass_ts(start)},{ass_ts(end)},Default,,0,0,0,,{text}"
        )

    return header + "\n".join(lines) + "\n"


# ─── Main processing pipeline ─────────────────────────────────────────────────

async def _notify_user(bot, user_id: int, msg: str):
    """Send a DM to the Discord user if possible."""
    if bot is None:
        return
    try:
        user = await bot.fetch_user(user_id)
        await user.send(msg)
    except Exception as e:
        print(f"[exp-radio] DM to {user_id} failed: {e}", flush=True)


async def process_exp_song(db, song_id: int, exp_radio_dir: str, bot=None):
    """Full async pipeline for one submitted experimental radio song."""
    mp3_dir = os.path.join(exp_radio_dir, "mp3")
    ass_dir = os.path.join(exp_radio_dir, "ass")
    ensure_exp_dirs(exp_radio_dir)

    song = await db.get_exp_radio_song(song_id)
    if not song:
        print(f"[exp-radio] Song #{song_id} not found in DB", flush=True)
        return

    uuid = song["suno_uuid"]
    print(f"[exp-radio] Processing #{song_id} uuid={uuid}", flush=True)
    await db.update_exp_radio_song(song_id, analysis_status="processing")

    try:
        # 1) Locate MP3 — supplied by the browser upload endpoint
        mp3_filename = song.get("mp3_filename")
        if not mp3_filename:
            print(f"[exp-radio] #{song_id}: no mp3_filename in DB yet — upload not completed", flush=True)
            await db.update_exp_radio_song(song_id, analysis_status="failed")
            return
        mp3_path = os.path.join(mp3_dir, mp3_filename)
        if not os.path.exists(mp3_path):
            print(f"[exp-radio] #{song_id}: MP3 file missing on disk: {mp3_path}", flush=True)
            await db.update_exp_radio_song(song_id, analysis_status="failed")
            return

        # 2) Scrape Suno metadata
        meta = await scrape_suno(uuid)
        real_uuid = meta.get("real_uuid") or uuid
        title     = meta["title"]     or song.get("title")  or "Unknown"
        artist    = meta["artist"]    or song.get("artist") or "Unknown"
        cover_url = meta["cover_url"] or f"https://cdn1.suno.ai/image_large_{real_uuid}.jpeg"
        video_url = meta["video_url"]
        lyrics    = clean_lyrics(meta["raw_lyrics"] or "")
        duration  = await get_duration(mp3_path)

        # Duration gate: reject songs longer than MAX_DURATION_SECS
        if duration and duration > MAX_DURATION_SECS:
            os.remove(mp3_path)
            await db.update_exp_radio_song(
                song_id, duration=duration, analysis_status="failed"
            )
            mins = int(MAX_DURATION_SECS // 60)
            await _notify_user(
                bot, song["user_id"],
                f"❌ **Experimental Radio** — your submission **{title}** was rejected.\n"
                f"The song is {duration / 60:.1f} min long. Maximum allowed is **{mins} minutes**.\n"
                "Please submit a shorter song."
            )
            print(f"[exp-radio] #{song_id} rejected: duration {duration:.0f}s > {MAX_DURATION_SECS}s", flush=True)
            return

        await db.update_exp_radio_song(
            song_id,
            title=title, artist=artist,
            cover_url=cover_url, video_url=video_url,
            lyrics=lyrics, duration=duration,
        )

        # 3) Whisper word-timestamp analysis
        print(f"[exp-radio] Starting Whisper analysis for #{song_id}…", flush=True)
        words = await run_whisper(mp3_path)
        print(f"[exp-radio] Whisper done: {len(words)} words for #{song_id}", flush=True)

        # 4) Build ASS subtitle file
        ass_content  = build_ass(words, title=title, artist=artist)
        ass_filename = f"{uuid}.ass"
        with open(os.path.join(ass_dir, ass_filename), "w", encoding="utf-8") as f:
            f.write(ass_content)

        await db.update_exp_radio_song(
            song_id,
            word_timestamps=json.dumps(words),
            ass_filename=ass_filename,
            analysis_status="done",
        )
        print(f"[exp-radio] Pipeline complete for #{song_id}", flush=True)

    except Exception as e:
        print(f"[exp-radio] Pipeline error for #{song_id}: {e}", flush=True)
        await db.update_exp_radio_song(song_id, analysis_status="failed")
