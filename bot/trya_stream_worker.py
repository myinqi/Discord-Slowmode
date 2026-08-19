"""TrYa Stream – background processing pipeline.

For each submitted song:
  1. Validate a local MP3/M4A and immutably archive its exact bytes
  2. Normalize a separate work MP3 and optionally scrape Suno metadata/covers
  3. Clean lyrics (strip section tags like [Verse], [Chorus])
  4. Run faster-whisper (in thread pool) → word-level timestamps
  5. Generate ASS subtitle file with moving-window karaoke style
  6. Update DB with all results throughout
"""

import asyncio
import aiohttp
import html as _html
import hashlib
import json
import os
import re
import time
import unicodedata
from difflib import SequenceMatcher
from typing import Optional

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Matches an RSC reference placeholder like '$3d' that Next.js emits for
# long string fields (resolved later from a separate flight chunk).
_RSC_REF_RE = re.compile(r'^\$[0-9a-f]+$')

_SUNO_SUFFIX_RE = re.compile(r'\s*[|\-–]\s*Suno(?:\s*AI)?\s*$', re.IGNORECASE)


def _clean_suno_title(raw_title: str | None) -> str | None:
    if not raw_title:
        return None
    title = _html.unescape(raw_title).strip()
    title = _SUNO_SUFFIX_RE.sub("", title).strip()
    return title or None


def _split_title_artist_fallback(raw_title: str) -> tuple[str, str | None]:
    """Best-effort fallback for old Suno titles like 'Title by Artist | Suno'.

    Suno pages also expose the owner as structured RSC data. Prefer that
    source whenever available; splitting title text on "by" can eat legitimate
    song titles such as "... by Saki".
    """
    matches = list(re.finditer(r"\s+by\s+", raw_title, flags=re.IGNORECASE))
    if not matches:
        return raw_title, None
    match = matches[-1]
    title = raw_title[:match.start()].strip()
    artist = raw_title[match.end():].strip()
    if not title or not artist:
        return raw_title, None
    return title, artist


def _decode_suno_json_string(value: str) -> str:
    value = re.sub(r'\\\\u([0-9a-fA-F]{4})',
                   lambda m: chr(int(m.group(1), 16)), value)
    value = re.sub(r'\\u([0-9a-fA-F]{4})',
                   lambda m: chr(int(m.group(1), 16)), value)
    return _html.unescape(
        value.replace(r'\"', '"').replace(r"\/", "/").strip()
    )


def _valid_suno_display_name(name: str | None) -> bool:
    if not name:
        return False
    return (
        len(name) > 1
        and not re.match(r'^v\d', name)
        and name not in ("Cover", "Remix")
    )


def _extract_suno_clip_owner_display_name(page: str, uuid: str | None = None) -> str | None:
    """Return the display_name attached to the main clip owner.

    Remix pages include additional `clip_roots` users. A global/reversed
    display_name scan can therefore pick the original clip artist instead of
    the creator of the current clip.
    """
    id_part = re.escape(uuid) if uuid else r'[a-f0-9-]{8,36}'
    patterns = [
        rf'\\"id\\":\\"{id_part}\\".*?\\"user_id\\":\\"[^"\\]+\\".*?\\"display_name\\":\\"((?:(?!\\").)*)\\"',
        rf'"id"\s*:\s*"{id_part}".*?"user_id"\s*:\s*"[^"]+".*?"display_name"\s*:\s*"((?:[^"\\]|\\.)*)"',
    ]
    for pat in patterns:
        m = re.search(pat, page, re.S)
        if not m:
            continue
        name = _decode_suno_json_string(m.group(1))
        if _valid_suno_display_name(name):
            return name
    return None


def _extract_suno_display_name(page: str) -> str | None:
    # Iterate matches in REVERSE — song owner is usually the last display_name
    # on the page. Filter out version strings (v5.5), "Cover", "Remix", and
    # single-char names.
    owner = _extract_suno_clip_owner_display_name(page)
    if owner:
        return owner
    candidates = re.findall(r'display_name\\":\\"((?:[^"\\]|\\[^"])*)\\"', page)
    if not candidates:
        candidates = re.findall(r'"display_name"\s*:\s*"((?:[^"\\]|\\.)*)"', page)
    for dn in reversed(candidates):
        dn = _decode_suno_json_string(dn)
        if _valid_suno_display_name(dn):
            return dn
    return None


TRYA_RIGHTS_VERSION = "trya-suno-download-v1-2026-09-03"
TRYA_RIGHTS_DECLARATION = (
    "I attest that this exact audio file was obtained through an official Suno download channel "
    "while this specific song carried paid-plan commercial-use rights; it is not a Suno Remix; "
    "I hold every required right and permission for its lyrics, samples, voices, performances and "
    "other elements; and I grant TrYa Stream a perpetual, non-exclusive authorization to archive "
    "and process the file and to use the song in Twitch live streams, recordings, clips and VODs."
)

MAX_DURATION_SECS = 360  # 6 minutes


# ─── Directory helpers ────────────────────────────────────────────────────────

def ensure_exp_dirs(trya_stream_dir: str):
    for sub in ("mp3", "ass", "assets", "originals"):
        os.makedirs(os.path.join(trya_stream_dir, sub), exist_ok=True)


MAX_UPLOAD_BYTES = 20 * 1024 * 1024
_ALLOWED_UPLOAD_FORMATS = {"mp3", "mov,mp4,m4a,3gp,3g2,mj2"}


async def _probe_uploaded_audio(path: str) -> tuple[str, str]:
    """Return canonical extension and MIME after ffprobe validates MP3/M4A audio."""
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries",
        "format=format_name:stream=codec_type", "-of", "json", path,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate()
    if proc.returncode != 0:
        raise ValueError(f"ffprobe rejected upload: {(err or b'').decode('utf-8', 'replace')[:300]}")
    try:
        probe = json.loads(out or b"{}")
    except Exception as exc:
        raise ValueError("ffprobe returned invalid metadata") from exc
    if not any(stream.get("codec_type") == "audio" for stream in probe.get("streams", [])):
        raise ValueError("upload contains no audio stream")
    format_name = str(probe.get("format", {}).get("format_name") or "")
    if format_name == "mp3":
        return ".mp3", "audio/mpeg"
    if format_name in _ALLOWED_UPLOAD_FORMATS or "m4a" in format_name or "mp4" in format_name:
        return ".m4a", "audio/mp4"
    raise ValueError("only valid MP3 or M4A uploads are accepted")


async def _normalize_original_to_mp3(original_path: str, work_path: str) -> None:
    tmp_path = f"{work_path}.tmp.mp3"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-nostdin", "-y", "-i", original_path, "-map", "0:a:0",
        "-vn", "-map_metadata", "-1", "-ac", "2", "-ar", "48000",
        "-codec:a", "libmp3lame", "-b:a", "192k", tmp_path,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise ValueError(f"FFmpeg normalization failed: {(err or b'').decode('utf-8', 'replace')[-500:]}")
    os.replace(tmp_path, work_path)


async def ingest_uploaded_audio(
    db, song_id: int, uploaded_file: str | os.PathLike | bytes | bytearray,
    trya_stream_dir: str, *, original_filename: str | None = None,
    rights_version: str | None = None, paid_download_attested: bool = False,
    official_download_attested: bool = False, is_suno_remix: bool = False,
    third_party_rights_attested: bool = False,
    commercial_rights_attested: bool = False,
    rights_accepted_at: float | None = None,
) -> dict:
    """Validate, immutably archive exact bytes, and create a normalized work MP3.

    This function performs no network audio retrieval and does not start
    Whisper. On success callers may queue ``process_exp_song`` using song_id.
    """
    song = await db.get_trya_stream_song(song_id)
    if not song:
        raise ValueError("TrYa song not found")
    if song.get("original_uploaded_at") or song.get("original_archive_filename"):
        raise ValueError("original upload is immutable and already finalized")
    if not all((paid_download_attested, official_download_attested,
                third_party_rights_attested, commercial_rights_attested)):
        raise ValueError("all required rights attestations must be accepted")
    if is_suno_remix:
        raise ValueError("Suno Remixes are not eligible for TrYa Stream")

    ensure_exp_dirs(trya_stream_dir)
    originals_dir = os.path.join(trya_stream_dir, "originals")
    mp3_dir = os.path.join(trya_stream_dir, "mp3")
    staged_path = os.path.join(originals_dir, f".{song_id}.{os.getpid()}.upload")
    size = 0
    digest = hashlib.sha256()
    try:
        with open(staged_path, "xb") as dest:
            if isinstance(uploaded_file, (bytes, bytearray)):
                chunks = (bytes(uploaded_file),)
                supplied_name = original_filename or f"song-{song_id}"
            else:
                supplied_name = original_filename or os.path.basename(os.fspath(uploaded_file))
                source = open(os.fspath(uploaded_file), "rb")
                chunks = iter(lambda: source.read(1024 * 1024), b"")
            try:
                for chunk in chunks:
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise ValueError("audio upload exceeds 20 MiB")
                    digest.update(chunk)
                    dest.write(chunk)
                dest.flush()
                os.fsync(dest.fileno())
            finally:
                if not isinstance(uploaded_file, (bytes, bytearray)):
                    source.close()
        if size == 0:
            raise ValueError("audio upload is empty")
        extension, mime = await _probe_uploaded_audio(staged_path)
        sha256 = digest.hexdigest()
        archive_filename = f"{song_id}_{sha256}{extension}"
        archive_path = os.path.join(originals_dir, archive_filename)
        # Never overwrite. A pre-existing exact archive is accepted only when
        # its immutable bytes match; otherwise this is an integrity failure.
        try:
            # Hard-link publication is atomic and fails if the immutable name
            # already exists; unlike rename it can never overwrite that name.
            os.link(staged_path, archive_path)
            os.chmod(archive_path, 0o444)
            os.remove(staged_path)
        except FileExistsError:
            existing_digest = hashlib.sha256()
            with open(archive_path, "rb") as existing:
                for chunk in iter(lambda: existing.read(1024 * 1024), b""):
                    existing_digest.update(chunk)
            if existing_digest.hexdigest() != sha256:
                raise ValueError("original archive collision")
            os.remove(staged_path)
        work_filename = f"{song_id}_{sha256[:16]}.mp3"
        work_path = os.path.join(mp3_dir, work_filename)
        # Normalization always runs, even for MP3 input. The immutable archive
        # remains untouched while the derived work file is atomically replaced.
        await _normalize_original_to_mp3(archive_path, work_path)
        duration = await get_duration(work_path)
        if not duration or duration <= 0:
            raise ValueError("normalized audio has no decodable duration")
        evidence = dict(
            original_sha256=sha256, original_filename=os.path.basename(supplied_name),
            original_mime=mime, original_size=size, original_uploaded_at=time.time(),
            original_archive_filename=archive_filename, mp3_filename=work_filename,
            duration=duration, rights_version=rights_version,
            rights_accepted_at=rights_accepted_at or time.time(),
            paid_download_attested=1, official_download_attested=1,
            is_suno_remix=int(bool(is_suno_remix)), third_party_rights_attested=1,
            commercial_rights_attested=1,
        )
        finalized = await db.finalize_trya_stream_upload(song_id, **evidence)
        if not finalized:
            raise ValueError("could not finalize TrYa upload evidence")
        return finalized
    finally:
        try:
            os.remove(staged_path)
        except OSError:
            pass


async def regenerate_work_mp3(db, song_id: int, trya_stream_dir: str) -> str:
    """Regenerate a missing normalized work MP3 from immutable archive only."""
    song = await db.get_trya_stream_song(song_id)
    if not song or not song.get("original_archive_filename") or not song.get("original_sha256"):
        raise ValueError("song has no original archive evidence")
    ensure_exp_dirs(trya_stream_dir)
    archive_filename = str(song["original_archive_filename"])
    if os.path.basename(archive_filename) != archive_filename:
        raise ValueError("invalid original archive filename")
    archive_path = os.path.join(trya_stream_dir, "originals", archive_filename)
    if not os.path.isfile(archive_path):
        raise FileNotFoundError("original archive is missing")
    digest = hashlib.sha256()
    with open(archive_path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != song["original_sha256"]:
        raise ValueError("original archive SHA-256 mismatch")
    filename = song.get("mp3_filename") or f"{song_id}_{song['original_sha256'][:16]}.mp3"
    work_path = os.path.join(trya_stream_dir, "mp3", filename)
    if not os.path.isfile(work_path):
        await _normalize_original_to_mp3(archive_path, work_path)
    await db.update_trya_stream_song(song_id, mp3_filename=filename)
    return work_path


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
            result["title"] = _clean_suno_title(raw_title)

        # Artist fallback: display_name from RSC payload
        if not result["artist"]:
            result["artist"] = (
                _extract_suno_clip_owner_display_name(page, result.get("real_uuid") or uuid)
                or _extract_suno_display_name(page)
            )

        # Last-resort title-based extraction for pages without structured owner data.
        if not result["artist"] and result["title"]:
            result["title"], result["artist"] = _split_title_artist_fallback(result["title"])

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

        # RSC long-form lyrics fallback (still on /song page)
        if not result["raw_lyrics"]:
            rsc = re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', page, re.DOTALL)
            for chunk in rsc:
                m = re.search(r'"prompt"\s*:\s*"((?:[^"\\]|\\.)*)"', chunk)
                if m:
                    cand = m.group(1).replace("\\n", "\n").replace('\\"', '"')
                    if _valid(cand):
                        result["raw_lyrics"] = cand
                        break

        # Fallback: /embed/<real_uuid> page exposes video_cover_url and lyrics
        # more reliably (same approach used by the working Suno Info player).
        # In particular, longer prompts are serialised as RSC references like
        # `"prompt":"$3d"` and the actual content lives in a separate flight
        # chunk `3d:T<hexlen>,<bytes>`. The simple regex above cannot follow
        # that indirection, so we replicate the resolver here.
        need_video = not result["video_url"]
        need_lyrics = not result["raw_lyrics"]
        if (need_video or need_lyrics) and result.get("real_uuid"):
            try:
                async with aiohttp.ClientSession(headers={"User-Agent": _BROWSER_UA}) as sess:
                    embed_url = f"https://suno.com/embed/{result['real_uuid']}"
                    async with sess.get(embed_url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                        if r.status == 200:
                            embed_html = await r.text()

                            if need_video:
                                m = re.search(r'"video_cover_url"\s*:\s*"([^"]+)"', embed_html)
                                if not m:
                                    m = re.search(r'video_cover_url\\":\\"([^"\\]+)\\"', embed_html)
                                if m:
                                    result["video_url"] = m.group(1).replace("\\/", "/")

                            if need_lyrics:
                                # Inline lyrics in embed page
                                idx = embed_html.find(result["real_uuid"])
                                if idx > -1:
                                    chunk = embed_html[max(0, idx - 500):idx + 5000]
                                    m = re.search(r'"prompt"\s*:\s*"((?:[^"\\]|\\.)*)"', chunk)
                                    if m:
                                        cand = m.group(1).replace("\\n", "\n").replace('\\"', '"')
                                        if _valid(cand):
                                            result["raw_lyrics"] = cand

                                # RSC-reference resolution (for long prompts)
                                if not result["raw_lyrics"] or _RSC_REF_RE.match(result["raw_lyrics"] or ""):
                                    chunks = re.findall(
                                        r'self\.__next_f\.push\(\[1,"(.*?)"\]\)',
                                        embed_html, re.DOTALL,
                                    )
                                    if chunks:
                                        decoded = []
                                        for c in chunks:
                                            try:
                                                decoded.append(json.loads('"' + c + '"'))
                                            except Exception:
                                                decoded.append(
                                                    c.replace('\\n', '\n')
                                                     .replace('\\"', '"')
                                                     .replace('\\\\', '\\')
                                                )
                                        full = "".join(decoded)
                                        mref = re.search(r'"prompt":"\$([0-9a-f]+)"', full)
                                        if mref:
                                            ref = mref.group(1)
                                            full_bytes = full.encode("utf-8")
                                            tpat_b = re.compile(
                                                rb'(?:^|\n)' + re.escape(ref).encode() +
                                                rb':T([0-9a-f]+),'
                                            )
                                            tfnd = tpat_b.search(full_bytes)
                                            if tfnd:
                                                length = int(tfnd.group(1), 16)
                                                b_start = tfnd.end()
                                                cand = full_bytes[b_start:b_start + length] \
                                                    .decode("utf-8", errors="replace").rstrip()
                                                if _valid(cand):
                                                    result["raw_lyrics"] = cand
            except Exception as e:
                print(f"[trya-stream] embed fetch error: {e}", flush=True)

    except Exception as e:
        print(f"[trya-stream] Suno scrape error {uuid}: {e}", flush=True)

    if not result["cover_url"]:
        result["cover_url"] = f"https://cdn1.suno.ai/image_large_{uuid}.jpeg"

    return result


# ─── Lyrics cleaning ─────────────────────────────────────────────────────────

# Unicode ranges for emoji + decorative symbols we want to strip from lyrics
_DECOR_RE = re.compile(
    r'['
    r'\u2500-\u257F'      # Box drawing
    r'\u2580-\u259F'      # Block elements
    r'\u25A0-\u25FF'      # Geometric shapes
    r'\u2600-\u26FF'      # Misc symbols (✦ ⚜ ☀ etc.)
    r'\u2700-\u27BF'      # Dingbats
    r'\u2B00-\u2BFF'      # Misc symbols & arrows
    r'\u2E00-\u2E7F'      # Supplemental punctuation
    r'\u3000-\u303F'      # CJK symbols & punctuation
    r'\u0F00-\u0FFF'      # Tibetan (includes ༼ ༽ ༺ ༻ decorative brackets)
    r'\uA9C0-\uA9DF'      # Javanese punctuation (꧁ ꧂ etc.)
    r'\U0001F300-\U0001F9FF'  # Misc pictographs + emoticons
    r'\U0001FA00-\U0001FAFF'  # Symbols & pictographs ext.
    r'\U0001F700-\U0001F77F'  # Alchemical symbols (🜁 🜂 🜔)
    r'\u2728\uFE0F'       # Sparkles + variation selector
    r']+'
)


_BRACKET_MARKER_RE = re.compile(r'^\s*\[[^\]]+\]\s*$')          # [Verse 1], [Chorus]
_MARKDOWN_HEADING_RE = re.compile(r'^\s*#{1,6}\s+\S')            # ## Title
_SEPARATOR_RE        = re.compile(r'^\s*-{3,}\s*$')              # --- or ------
_NAMED_MARKER_WORDS = {
    "verse", "chorus", "bridge", "intro", "outro",
    "prechorus", "pre-chorus", "hook", "refrain",
    "interlude", "tag", "coda", "post-chorus", "postchorus",
}


def _is_section_marker(line: str) -> bool:
    """True if the line is only a section marker (no actual lyrics)."""
    if _BRACKET_MARKER_RE.match(line):
        return True
    if _MARKDOWN_HEADING_RE.match(line):
        return True
    # Strip trailing punctuation and split into tokens. A marker line should
    # collapse to one keyword plus at most one Roman-numeral / digit suffix.
    stripped = line.strip().rstrip(":.,)-").strip()
    if not stripped:
        return False
    parts = stripped.split()
    if len(parts) > 2:
        return False
    head = parts[0].lower()
    if head not in _NAMED_MARKER_WORDS:
        return False
    if len(parts) == 1:
        return True
    # Second token must be a Roman numeral (I, II, III…) or digit
    return bool(re.fullmatch(r"[IVXivx]+|\d+", parts[1]))


def extract_vocab_hints(lyrics: str, max_chars: int = 220) -> str:
    """Pull out proper-noun-style tokens (capitalised neologisms / names) from
    cleaned lyrics for use as a Whisper initial_prompt.

    Feeding Whisper actual lyric lines as a prompt causes it to echo the
    prompt as the first transcribed words (a classic faster-whisper failure
    mode). A comma-separated vocabulary list does not get echoed verbatim
    but still biases the decoder toward the correct spelling of rare words
    like 'Morrowmire', 'Tarja', 'Vorralune'.
    """
    if not lyrics:
        return ""
    seen = []
    seen_lower = set()
    # Tokenize, then keep tokens that:
    #   - are >=4 chars
    #   - start with an uppercase letter
    #   - are not at the start of a sentence (would catch ordinary words)
    # We approximate the sentence-initial check by ignoring the first token of
    # every line; this also drops common capitalised line starts.
    for line in lyrics.splitlines():
        toks = re.findall(r"[A-Za-z][A-Za-z'\-]+", line)
        for tok in toks[1:]:  # skip line-initial token
            if len(tok) < 4 or not tok[0].isupper():
                continue
            low = tok.lower()
            if low in seen_lower:
                continue
            seen_lower.add(low)
            seen.append(tok)
    hint = ", ".join(seen)
    if len(hint) > max_chars:
        hint = hint[:max_chars].rsplit(",", 1)[0]
    return hint


def clean_lyrics(raw: str) -> str:
    """Strip section headers, decorative box/emoji glyphs, markdown formatting,
    and the non-lyric preamble (author description, URLs, event blurbs) that
    Suno users frequently paste before the actual lyrics. Normalises fancy
    Unicode (𝑰𝒏𝒕𝒓𝒐 → 'Intro') so lyrics are usable as a Whisper initial_prompt
    and as alignment tokens.
    """
    if not raw:
        return ""
    # NFKC folds Mathematical Italic / Bold / Sans-serif letters back to ASCII
    text = unicodedata.normalize("NFKC", raw)
    lines = text.splitlines()

    # Find the first structural marker (Verse/Chorus/##/[...]) or a
    # dash-separator line (--- or ------) — everything before it is preamble
    # (copyright, URLs, author blurbs) and is dropped.  Suno/Ape_Music songs
    # frequently use '---...---' as a separator instead of [Verse] markers;
    # without this the copyright header leaks into lyric_tokens and corrupts
    # the Whisper alignment boundary detection.
    # If no marker or separator is found we keep all lines (fallback).
    start = 0
    for i, line in enumerate(lines):
        if _is_section_marker(line):
            start = i      # marker itself will be skipped in the loop below
            break
        if _SEPARATOR_RE.match(line):
            start = i + 1  # start *after* the separator
            break

    out = []
    for line in lines[start:]:
        # Skip section markers and separator lines — they are not sung.
        if _is_section_marker(line) or _SEPARATOR_RE.match(line):
            continue
        line = _DECOR_RE.sub('', line)
        # Strip markdown bold/italic markers but keep their contents
        line = re.sub(r'\*+', '', line)
        # Collapse whitespace
        line = re.sub(r'\s+', ' ', line).strip()
        if line:
            out.append(line)
    return "\n".join(out).strip()


# ─── Audio duration ───────────────────────────────────────────────────────────

async def get_duration(mp3_path: str) -> float:
    """Measure the actual decodable audio duration.

    Some Suno MP3s carry broken container/header duration metadata. Decoding
    the audio stream to null gives the duration FFmpeg can really play, which
    is the value used for the submission length gate and stream timing.
    """
    from bot.audio_utils import get_decoded_audio_duration

    return await get_decoded_audio_duration(mp3_path)


# ─── Whisper analysis ─────────────────────────────────────────────────────────

# Model size can be overridden via env (tiny, base, small, medium, large-v3)
_WHISPER_MODEL = os.environ.get("TRYA_STREAM_WHISPER_MODEL", "large-v3-turbo")


# ── Lyric-sheet language detection ────────────────────────────────────────────

# Whisper supports ~99 languages. We only hard-pin ones we are highly
# confident about — for everything else we let Whisper auto-detect. This
# whitelist prevents langdetect's known instability on ambiguous text from
# pinning the decoder to an exotic language.
_WHISPER_LANG_WHITELIST = {
    "en", "de", "fr", "es", "it", "pt", "nl", "sv", "no", "da", "fi",
    "pl", "cs", "ru", "uk", "tr", "ja", "zh-cn", "zh-tw", "ko",
}

# Map langdetect language codes to Whisper's two-letter codes where they
# differ (langdetect uses BCP-47-ish, Whisper uses ISO-639-1).
_LANG_CODE_MAP = {"zh-cn": "zh", "zh-tw": "zh"}

# Hard character-based overrides — these are unambiguous and faster/safer
# than langdetect for the common problem cases.
_LANG_CHAR_RULES = [
    # Icelandic-specific thorn / eth. Faroese also uses these but is very
    # rare in Suno content; Whisper handles Faroese-as-Icelandic decently.
    (re.compile(r"[\u00fe\u00f0\u00de\u00d0]"), "is"),
    # Cyrillic block → Russian (Whisper's strongest Cyrillic language).
    (re.compile(r"[\u0400-\u04FF]"), "ru"),
    # Hiragana / Katakana → Japanese.
    (re.compile(r"[\u3040-\u30FF]"), "ja"),
    # Hangul → Korean.
    (re.compile(r"[\uAC00-\uD7AF]"), "ko"),
    # CJK Unified Ideographs without kana/hangul → Chinese.
    (re.compile(r"[\u4E00-\u9FFF]"), "zh"),
]

# German-specific characters. Used only as a tiebreaker; ASCII-only German
# text falls through to langdetect.
_GERMAN_CHARS_RE = re.compile(r"[\u00e4\u00f6\u00fc\u00c4\u00d6\u00dc\u00df]")


def detect_lyrics_language(lyrics: str) -> Optional[str]:
    """Detect the language of a lyric sheet for use as Whisper's `language=` hint.

    Strategy (mirrors `bot/llm.py:_detect_reply_language` lessons learned):
    1. Hard character-class rules first — unambiguous scripts (Cyrillic,
       CJK) and distinctive Latin extensions (þ/ð).
    2. langdetect with a high confidence threshold AND a whitelist of
       languages Whisper transcribes reliably.
    3. Return None → caller falls back to Whisper's audio-based auto-detect.

    Returns a Whisper-compatible ISO-639-1 code or None.
    """
    if not lyrics or len(lyrics.strip()) < 20:
        return None
    for pat, lang in _LANG_CHAR_RULES:
        if pat.search(lyrics):
            return lang
    if _GERMAN_CHARS_RE.search(lyrics):
        return "de"
    try:
        from langdetect import detect_langs, DetectorFactory
        DetectorFactory.seed = 0  # deterministic
        candidates = detect_langs(lyrics)
    except Exception:
        return None
    if not candidates:
        return None
    top = candidates[0]
    if top.prob < 0.95:
        return None
    code = top.lang.lower()
    if code not in _WHISPER_LANG_WHITELIST:
        return None
    return _LANG_CODE_MAP.get(code, code)


# ─── Whisper hallucination filter ────────────────────────────────────────────
#
# Whisper absorbed German public-broadcasting subtitle conventions during
# training and injects them verbatim into quiet or ambient passages.
# These token values are essentially *never* found in actual song lyrics.
_HALLUC_TOKENS: frozenset[str] = frozenset({
    "untertitelung", "untertitel", "untertitelt",  # subtitling metadata
    "zdf", "ndr", "ard", "swr", "mdr", "wdr", "rbb", "br",  # German broadcasters
    "tagesschau",  # ARD news show
})


def _strip_hallucinations(words: list) -> list:
    """Drop word tokens that are known Whisper broadcast-metadata hallucinations."""
    if not words:
        return words
    out = [
        w for w in words
        if re.sub(r"[^\w]", "", w["word"], flags=re.UNICODE).lower()
           not in _HALLUC_TOKENS
    ]
    dropped = len(words) - len(out)
    if dropped:
        print(
            f"[trya-stream] Stripped {dropped} hallucination token(s) from Whisper output",
            flush=True,
        )
    return out


def _whisper_sync(mp3_path: str, lyrics_prompt: str = "", language: Optional[str] = None) -> list:
    """Run faster-whisper synchronously (called in thread pool).

    `lyrics_prompt` is accepted for backwards compatibility but is NOT passed
    to Whisper as initial_prompt. Earlier experiments fed a comma-separated
    vocabulary hint hoping it would bias spelling of rare words without being
    echoed. In practice faster-whisper *does* echo such prompts as the first
    transcribed words (visible as a garbled token list at t≈0). Spelling
    correction is handled downstream by `_align_to_lyrics` (exact-match
    word substitution against the lyric sheet) which is safer and produces
    no echo artifacts."""
    from faster_whisper import WhisperModel
    model = WhisperModel(_WHISPER_MODEL, device="cpu", compute_type="int8")
    segments, _ = model.transcribe(
        mp3_path,
        word_timestamps=True,
        # `language` is provided by the caller when we could confidently
        # detect it from the lyric sheet (see detect_lyrics_language). If
        # None, faster-whisper falls back to audio-based auto-detect which
        # is unreliable for less common languages (Icelandic gets pegged
        # as Russian, triggering the famous DimaTorzok watermark).
        language=language,
        initial_prompt=None,
        # vad_filter is deliberately OFF: Silero VAD is too aggressive on
        # sung audio (held vowels and quiet passages get classified as
        # silence and dropped entirely), which causes the karaoke to start
        # late, hang mid-song, then resume. Running Whisper over the full
        # audio yields continuous word timestamps that match the actual
        # vocals frame-for-frame.
        vad_filter=False,
        # Each 30s window decodes against the original initial_prompt only,
        # not against the previous window's output. This prevents the
        # cascading-hallucination failure mode where a single bad window
        # (silence misread as repeated punctuation, etc.) poisons every
        # subsequent window and produces a transcript of just dots.
        condition_on_previous_text=False,
    )
    _has_alnum = re.compile(r"[\w]", re.UNICODE)
    words = []
    for seg in segments:
        for w in (seg.words or []):
            word = w.word.strip()
            # Drop standalone punctuation tokens ('.', ',', '?'…) — they
            # carry no karaoke value and only inflate the word count.
            if not word or not _has_alnum.search(word):
                continue
            words.append({
                "word": word,
                "start": round(w.start, 3),
                "end": round(w.end, 3),
            })
    words = _strip_hallucinations(words)
    return words


_WHISPER_SEM = asyncio.Semaphore(1)   # serialise Whisper runs → prevent OOM


async def run_whisper(mp3_path: str, lyrics_prompt: str = "", language: Optional[str] = None) -> list:
    """Run Whisper in thread pool so the event loop is not blocked.

    Only one Whisper job may run at a time (guarded by _WHISPER_SEM).
    The large-v3-turbo model on CPU uses ~2 GB RAM; two concurrent runs
    exceed the container's memory limit and trigger an OOM-kill.
    """
    async with _WHISPER_SEM:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, _whisper_sync, mp3_path, lyrics_prompt, language,
        )


def _align_to_lyrics(whisper_words: list, lyrics_text: str) -> list:
    """Forced-alignment-light: keep Whisper's word *timings* but replace each
    word's *text* with the corresponding token from the original lyrics via
    sequence alignment. This eliminates Whisper hallucinations on artistic
    or non-dictionary lyrics (e.g. 'Morrowmire', 'Vorralune').

    Falls back to the raw Whisper output if the alignment quality is too poor
    (i.e. nothing matches → lyrics likely don't correspond to the audio).
    """
    if not lyrics_text or not whisper_words:
        return whisper_words

    lyric_tokens = re.findall(r"[\w']+", lyrics_text, flags=re.UNICODE)
    if not lyric_tokens:
        return whisper_words

    whisper_norm = [
        re.sub(r"[^\w']", "", w["word"], flags=re.UNICODE).lower()
        for w in whisper_words
    ]
    lyric_norm = [t.lower() for t in lyric_tokens]

    sm = SequenceMatcher(a=whisper_norm, b=lyric_norm, autojunk=False)
    opcodes = sm.get_opcodes()

    # Sanity check: if <10% of Whisper words match the lyrics exactly,
    # the lyrics probably don't fit this audio — keep Whisper as-is.
    matched = sum((i2 - i1) for tag, i1, i2, _, _ in opcodes if tag == "equal")
    if matched < max(3, int(0.1 * len(whisper_words))):
        print(
            f"[trya-stream] Alignment confidence too low "
            f"({matched}/{len(whisper_words)} matched) — keeping raw Whisper output",
            flush=True,
        )
        return whisper_words

    # Whisper-driven, lyric-corrected: preserve Whisper's structure & timing 1:1;
    # only overwrite a word's *text* where Whisper and the lyric sheet agree
    # exactly (the `equal` opcodes). This avoids the previous 1:1 proportional
    # remapping which mangled karaoke whenever the sheet's token count differed
    # from the sung word count (chorus repeats, instrumental sections, etc.).
    _has_latin = re.compile(r"[A-Za-z]")
    out = [dict(w) for w in whisper_words]
    corrected = 0
    for tag, i1, i2, j1, j2 in opcodes:
        if tag != "equal":
            continue
        for k in range(i2 - i1):
            new_text = lyric_tokens[j1 + k]
            # Refuse to overwrite Whisper output with a lyric token that has
            # no Latin letters — those are almost always decorative ornaments
            # (༼ ꧂ ✦) bleeding in from artist-name banners in the lyric sheet.
            if not _has_latin.search(new_text):
                continue
            if out[i1 + k]["word"] != new_text:
                out[i1 + k]["word"] = new_text
                corrected += 1
    # Trim boundary hallucinations: drop 'delete' words (Whisper words with no
    # lyric match) that appear before the first or after the last matched word.
    # Words in the *middle* of the transcript are kept — they may be ad-libs or
    # bridge sections not captured in the lyric sheet.
    first_eq = next((i1 for tag, i1, i2, _, _ in opcodes if tag == "equal"), None)
    last_eq  = next((i2 - 1 for tag, i1, i2, _, _ in reversed(opcodes) if tag == "equal"), None)
    trimmed = 0
    if first_eq is not None and last_eq is not None:
        drop = set()
        for tag, i1, i2, _, _ in opcodes:
            if tag == "delete":
                for wi in range(i1, i2):
                    if wi < first_eq or wi > last_eq:
                        drop.add(wi)
        if drop:
            trimmed = len(drop)
            out = [w for idx, w in enumerate(out) if idx not in drop]

    print(
        f"[trya-stream] Aligned: {matched}/{len(whisper_words)} exact matches, "
        f"{corrected} word(s) re-cased, {trimmed} boundary word(s) trimmed "
        f"({len(whisper_words)} whisper vs {len(lyric_tokens)} lyric tokens)",
        flush=True,
    )
    return out


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
        f"Style: Default,Noto Sans,54,&H00FFFFFF,&H00808080,&H00000000,&HA0000000,"
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

_WHISPER_YEAR_ANOMALIES = {"2018", "2020"}


def whisper_year_anomalies(words: list[dict]) -> list[str]:
    """Return known hallucinated standalone year tokens in the final transcript."""
    found: set[str] = set()
    for item in words or []:
        text = str(item.get("word") or "")
        for match in re.finditer(r"(?<!\w)(2018|2020)(?!\w)", text):
            found.add(match.group(1))
    return sorted(found)


async def retry_whisper_year_anomaly_if_needed(
    db, song_id: int, trya_stream_dir: str
) -> bool:
    """Run one additional Whisper pass for a final eligible anomalous transcript."""
    from bot.trya_stream_manager import log_event

    song = await db.get_trya_stream_song(song_id)
    if not song or song.get("analysis_status") != "done":
        return False
    if song.get("approval_status") != "approved":
        return False
    if song.get("moderation_status") not in (None, "passed", "approved"):
        return False
    if int(song.get("whisper_anomaly_retry_count") or 0) >= 1:
        return False
    try:
        words = json.loads(song.get("word_timestamps") or "[]")
    except (TypeError, ValueError):
        return False
    anomalies = whisper_year_anomalies(words)
    if not anomalies:
        return False
    trigger = ",".join(anomalies)
    if not await db.claim_trya_whisper_anomaly_retry(song_id, trigger):
        return False

    mp3_filename = song.get("mp3_filename")
    if not mp3_filename:
        log_event(
            f"#{song_id}: claimed year-anomaly retry but no work MP3 is available.",
            level="error", prefix="[whisper-retry]",
        )
        return False
    mp3_path = os.path.join(trya_stream_dir, "mp3", mp3_filename)
    if not os.path.isfile(mp3_path):
        try:
            mp3_path = await regenerate_work_mp3(db, song_id, trya_stream_dir)
        except Exception as exc:
            log_event(
                f"#{song_id}: year-anomaly retry could not regenerate work MP3: {exc}",
                level="error", prefix="[whisper-retry]",
            )
            return False

    log_event(
        f"#{song_id}: final transcript contains {trigger}; starting the one-time second Whisper pass.",
        prefix="[whisper-retry]",
    )
    await db.update_trya_stream_song(song_id, analysis_status="processing")
    ass_path = None
    tmp_path = None
    original_ass = None
    ass_replaced = False
    try:
        lyrics = song.get("lyrics") or ""
        retry_words = await run_whisper(
            mp3_path,
            lyrics_prompt=extract_vocab_hints(lyrics),
            language=detect_lyrics_language(lyrics),
        )
        if not retry_words:
            raise ValueError("second Whisper pass returned no words")
        if lyrics:
            retry_words = _align_to_lyrics(retry_words, lyrics)
        ass_filename = song.get("ass_filename") or f"{song.get('suno_uuid') or song_id}.ass"
        ass_dir = os.path.join(trya_stream_dir, "ass")
        os.makedirs(ass_dir, exist_ok=True)
        ass_path = os.path.join(ass_dir, ass_filename)
        if os.path.isfile(ass_path):
            with open(ass_path, "rb") as handle:
                original_ass = handle.read()
        tmp_path = f"{ass_path}.retry.tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            handle.write(build_ass(
                retry_words,
                title=song.get("title") or "Unknown",
                artist=song.get("artist") or "Unknown",
            ))
        os.replace(tmp_path, ass_path)
        ass_replaced = True
        await db.update_trya_stream_song(
            song_id,
            word_timestamps=json.dumps(retry_words),
            ass_filename=ass_filename,
            analysis_status="done",
        )
        remaining = whisper_year_anomalies(retry_words)
        log_event(
            f"#{song_id}: one-time second Whisper pass complete"
            + (f"; {','.join(remaining)} still present, no further retry will run." if remaining else "; anomaly cleared."),
            level="error" if remaining else "info",
            prefix="[whisper-retry]",
        )
        return True
    except Exception as exc:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        if ass_replaced and ass_path:
            try:
                if original_ass is None:
                    os.remove(ass_path)
                else:
                    with open(ass_path, "wb") as handle:
                        handle.write(original_ass)
            except OSError:
                pass
        await db.update_trya_stream_song(song_id, analysis_status="done")
        log_event(
            f"#{song_id}: one-time second Whisper pass failed: {exc}; original transcript retained.",
            level="error", prefix="[whisper-retry]",
        )
        return False


async def _notify_user(bot, user_id: int, msg: str):
    """Send a DM to the Discord user if possible."""
    if bot is None:
        return
    try:
        user = await bot.fetch_user(user_id)
        await user.send(msg)
    except Exception as e:
        print(f"[trya-stream] DM to {user_id} failed: {e}", flush=True)


async def _process_local_destination(
    db, *, source: str, local_file_path, trya_stream_dir: str,
    suno_url: str = "", user_id: int = 0, user_name: str = "admin",
    original_filename: str | None = None, bot=None,
    replacement_song_id: int | None = None, rights_version: str | None = None,
    rights_declaration: str = "", rights_hash: str = "", **attestations,
) -> tuple[bool, str]:
    """Shared local-file entry point for all four playlist destinations."""
    if local_file_path is None:
        return False, "A local MP3 or M4A upload is required; TrYa never downloads Suno audio."
    uuid = ""
    if suno_url:
        try:
            from bot.channel_moderation import extract_suno_uuid
            uuid = extract_suno_uuid(suno_url) or ""
        except Exception:
            uuid = ""
    try:
        song_id, _ = await db.add_trya_stream_song(
            user_id=user_id, user_name=user_name, suno_url=suno_url,
            suno_uuid=uuid, rights_declaration=rights_declaration,
            rights_hash=rights_hash, playlist_source=source,
            replacement_song_id=replacement_song_id, rights_version=rights_version,
        )
        meta = await scrape_suno(uuid) if uuid else {}
        if meta:
            await db.update_trya_stream_song(
                song_id, suno_uuid=meta.get("real_uuid") or uuid,
                title=meta.get("title"), artist=meta.get("artist"),
                cover_url=meta.get("cover_url"), video_url=meta.get("video_url"),
            )
        await ingest_uploaded_audio(
            db, song_id, local_file_path, trya_stream_dir,
            original_filename=original_filename, rights_version=rights_version,
            **attestations,
        )
        await process_exp_song(
            db, song_id, trya_stream_dir, bot=bot,
            skip_moderation=source in {"admin", "intro", "outro"},
            max_duration=None if source in {"admin", "intro", "outro"} else MAX_DURATION_SECS,
        )
        status = "pending admin approval" if source in {"intro", "outro"} else "approved"
        return True, f"TrYa song #{song_id} ingested from local audio ({status})."
    except Exception as exc:
        return False, str(exc)


async def process_admin_song(
    db, suno_url: str, trya_stream_dir: str, *, local_file_path=None,
    original_filename: str | None = None, user_id: int = 0,
    user_name: str = "admin", **rights,
) -> tuple[bool, str]:
    return await _process_local_destination(
        db, source="admin", local_file_path=local_file_path,
        trya_stream_dir=trya_stream_dir, suno_url=suno_url, user_id=user_id,
        user_name=user_name, original_filename=original_filename, **rights,
    )


async def process_admin_submission_song(
    db, suno_url: str, trya_stream_dir: str, bot=None,
    submitter_user_id: int = 0, submitter_user_name: str = "admin-ui",
    *, local_file_path=None, original_filename: str | None = None, **rights,
) -> tuple[bool, str]:
    return await _process_local_destination(
        db, source="submission", local_file_path=local_file_path,
        trya_stream_dir=trya_stream_dir, suno_url=suno_url,
        user_id=submitter_user_id, user_name=submitter_user_name,
        original_filename=original_filename, bot=bot, **rights,
    )


async def process_intro_outro_song(
    db, suno_url: str, source: str, trya_stream_dir: str, *,
    local_file_path=None, original_filename: str | None = None,
    user_id: int = 0, user_name: str = "admin", **rights,
) -> tuple[bool, str]:
    if source not in {"intro", "outro"}:
        return False, "source must be intro or outro"
    return await _process_local_destination(
        db, source=source, local_file_path=local_file_path,
        trya_stream_dir=trya_stream_dir, suno_url=suno_url, user_id=user_id,
        user_name=user_name, original_filename=original_filename, **rights,
    )


async def process_exp_song(db, song_id: int, trya_stream_dir: str, bot=None,
                           skip_moderation: bool = False,
                           max_duration: int | None = MAX_DURATION_SECS):
    """Full async pipeline for one submitted experimental radio song."""
    # Late import keeps the worker importable in tests without the streaming
    # stack — log_event lives in the stream manager module.
    from bot.trya_stream_manager import log_event
    mp3_dir = os.path.join(trya_stream_dir, "mp3")
    ass_dir = os.path.join(trya_stream_dir, "ass")
    ensure_exp_dirs(trya_stream_dir)

    song = await db.get_trya_stream_song(song_id)
    if not song:
        log_event(f"Song #{song_id} not found in DB", level="error", prefix="[whisper]")
        return

    uuid = song["suno_uuid"]
    log_event(f"Processing #{song_id} (uuid={uuid})", prefix="[whisper]")
    await db.update_trya_stream_song(song_id, analysis_status="processing")

    try:
        # 1) Locate MP3 — supplied by the browser upload endpoint
        mp3_filename = song.get("mp3_filename")
        if not mp3_filename:
            log_event(f"#{song_id}: no mp3_filename in DB yet — upload not completed", level="error", prefix="[whisper]")
            await db.update_trya_stream_song(song_id, analysis_status="failed")
            return
        mp3_path = os.path.join(mp3_dir, mp3_filename)
        if not os.path.exists(mp3_path):
            log_event(
                f"#{song_id}: work MP3 missing; regenerating from immutable original",
                prefix="[whisper]",
            )
            try:
                mp3_path = await regenerate_work_mp3(db, song_id, trya_stream_dir)
            except Exception as exc:
                log_event(f"#{song_id}: work MP3 regeneration failed: {exc}", level="error", prefix="[whisper]")
                await db.update_trya_stream_song(song_id, analysis_status="failed")
                return

        # 2) Optional Suno metadata/cover lookup (audio is always local).
        meta = await scrape_suno(uuid) if uuid else {
            "title": None, "artist": None, "cover_url": None,
            "video_url": None, "raw_lyrics": None, "real_uuid": None,
        }
        real_uuid = meta.get("real_uuid") or uuid
        title     = meta["title"]     or song.get("title")  or "Unknown"
        artist    = meta["artist"]    or song.get("artist") or "Unknown"
        cover_url = meta["cover_url"] or song.get("cover_url")
        video_url = meta["video_url"] or song.get("video_url")
        raw_lyrics_text = meta["raw_lyrics"] or song.get("lyrics") or ""
        lyrics    = clean_lyrics(raw_lyrics_text)
        print(
            f"[trya-stream] #{song_id} lyrics: {len(raw_lyrics_text)} raw chars → "
            f"{len(lyrics)} clean chars",
            flush=True,
        )
        duration  = await get_duration(mp3_path)
        log_event(
            f"#{song_id} decoded MP3 duration: {duration:.1f}s",
            prefix="[whisper]",
        )

        # Duration gate: reject songs that are too long (skipped for admin songs)
        if max_duration is not None and duration and duration > max_duration:
            # Retain both normalized work audio and immutable original as
            # evidence; playlist rejection/deactivation never deletes media.
            await db.update_trya_stream_song(
                song_id, duration=duration, analysis_status="failed"
            )
            mins = int(max_duration // 60)
            await _notify_user(
                bot, song["user_id"],
                f"❌ **TrYa Stream** — your submission **{title}** was rejected.\n"
                f"The song is {duration / 60:.1f} min long. Maximum allowed is **{mins} minutes**.\n"
                "Please submit a shorter song."
            )
            log_event(f"#{song_id} rejected: duration {duration:.0f}s > {max_duration}s", level="error", prefix="[whisper]")
            return

        await db.update_trya_stream_song(
            song_id,
            title=title, artist=artist,
            cover_url=cover_url, video_url=video_url,
            lyrics=lyrics, duration=duration,
        )

        # 3) Whisper word-timestamp analysis. We do NOT pass the lyric text
        # itself as initial_prompt (Whisper echoes it as the first sung
        # words). Instead we pass a comma-separated vocabulary hint built
        # from capitalised proper-noun candidates so the model still learns
        # to spell rare names correctly.
        vocab_hint = extract_vocab_hints(lyrics)
        detected_lang = detect_lyrics_language(lyrics)
        log_event(
            f"Starting Whisper analysis for #{song_id} "
            f"(model={_WHISPER_MODEL}, lang={detected_lang or 'auto'})…",
            prefix="[whisper]",
        )
        whisper_t0 = time.time()
        words = await run_whisper(
            mp3_path, lyrics_prompt=vocab_hint, language=detected_lang,
        )
        log_event(
            f"Whisper done for #{song_id}: {len(words)} words in "
            f"{time.time() - whisper_t0:.1f}s",
            prefix="[whisper]",
        )

        latest = await db.get_trya_stream_song(song_id)
        if (
            latest
            and latest.get("playlist_source") == "admin"
            and latest.get("analysis_status") == "done"
            and (latest.get("word_timestamps") or "") == "[]"
            and not latest.get("ass_filename")
        ):
            log_event(
                f"#{song_id}: Whisper result discarded; admin playlist bypass is active.",
                prefix="[whisper]",
            )
            manager = getattr(bot, "trya_stream_manager", None)
            if manager is None:
                from bot.trya_stream_manager import TryaStreamManager
                manager = TryaStreamManager(db, trya_stream_dir)
            await manager.prepare_song_square_media(latest)
            return

        # 3b) Forced-alignment-light: keep Whisper structure/timing, correct
        # word spellings from the lyric sheet wherever they agree exactly.
        if lyrics:
            words = _align_to_lyrics(words, lyrics)

        # 4) Build ASS subtitle file
        ass_content  = build_ass(words, title=title, artist=artist)
        ass_filename = f"{uuid or song_id}.ass"
        with open(os.path.join(ass_dir, ass_filename), "w", encoding="utf-8") as f:
            f.write(ass_content)

        await db.update_trya_stream_song(
            song_id,
            word_timestamps=json.dumps(words),
            ass_filename=ass_filename,
            analysis_status="done",
        )
        log_event(f"Pipeline complete for #{song_id} ({title!r})", prefix="[whisper]")

        # 5) Optional LLM-based lyric moderation. Runs only when explicitly
        #    enabled in the admin settings AND not bypassed for admin songs.
        moderation_enabled = await db.get_setting("trya_stream_moderation_enabled") or "off"
        if moderation_enabled == "on" and not skip_moderation:
            from bot.trya_stream_manager import log_event
            try:
                from bot.exp_moderation import moderate_lyrics
                from bot.llm import OllamaClient
                from config import Config
                # Generous timeout: queue serialises calls so there's no
                # competition; CPU inference on long lyrics can take > 4 min.
                _RADIO_LLM_TIMEOUT = 600
                client = OllamaClient(
                    base_url=Config.OLLAMA_URL,
                    model=Config.LLM_MODEL,
                    timeout=_RADIO_LLM_TIMEOUT,
                )
                log_event(f"Moderation start for #{song_id} ({title!r})", prefix="[mod]")
                from bot.llm_mod_queue import enqueue_moderation, PRIO_RADIO
                verdict = await enqueue_moderation(
                    PRIO_RADIO,
                    lambda: moderate_lyrics(
                        client, lyrics=lyrics, title=title, artist=artist,
                        timeout=_RADIO_LLM_TIMEOUT,
                    ),
                )
                await db.update_trya_stream_song(
                    song_id,
                    moderation_status=verdict["status"],
                    moderation_reason=verdict.get("reason") or "",
                    moderation_at=time.time(),
                )
                level = "error" if verdict["status"] in ("flagged", "pending") else "info"
                summary = (
                    f"Moderation #{song_id} → {verdict['status']}"
                    f"{' (translated)' if verdict.get('translated') else ''}"
                )
                if verdict.get("reason"):
                    summary += f": {verdict['reason']}"
                log_event(summary, level=level, prefix="[mod]")
                if verdict["status"] == "flagged":
                    await _notify_user(
                        bot, song["user_id"],
                        f"⚠️ **TrYa Stream** — your submission **{title}** "
                        f"was flagged by automated lyric review and is pending "
                        f"manual approval before it joins the stream playlist.\n"
                        f"Reason: _{verdict.get('reason') or 'no reason given'}_",
                    )
            except Exception as e:
                log_event(
                    f"Moderation pipeline error for #{song_id}: {e}",
                    level="error", prefix="[mod]",
                )
                await db.update_trya_stream_song(
                    song_id,
                    moderation_status="pending",
                    moderation_reason=f"Moderation error: {e!s}",
                    moderation_at=time.time(),
                )

        await retry_whisper_year_anomaly_if_needed(db, song_id, trya_stream_dir)
        latest = await db.get_trya_stream_song(song_id)
        if latest:
            manager = getattr(bot, "trya_stream_manager", None)
            if manager is None:
                from bot.trya_stream_manager import TryaStreamManager
                manager = TryaStreamManager(db, trya_stream_dir)
            await manager.prepare_song_square_media(latest)

    except Exception as e:
        log_event(f"Pipeline error for #{song_id}: {e}", level="error", prefix="[whisper]")
        await db.update_trya_stream_song(song_id, analysis_status="failed")
