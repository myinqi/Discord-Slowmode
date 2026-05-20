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
    "• Maximum **4 songs** per user"
)


MAX_DURATION_SECS = 360  # 6 minutes


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
            # Use (?:[^"\\]|\\[^"])* so \\uXXXX is captured but \" closing delimiter is NOT consumed
            candidates = re.findall(r'display_name\\":\\"((?:[^"\\]|\\[^"])*)\\"', page)
            if not candidates:
                candidates = re.findall(r'"display_name"\s*:\s*"((?:[^"\\]|\\.)*)"', page)
            for dn in reversed(candidates):
                # Decode double-escaped RSC unicode (\\uXXXX → char), then single-escaped (\uXXXX)
                dn = re.sub(r'\\\\u([0-9a-fA-F]{4})',
                            lambda m: chr(int(m.group(1), 16)), dn)
                dn = re.sub(r'\\u([0-9a-fA-F]{4})',
                            lambda m: chr(int(m.group(1), 16)), dn)
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
                print(f"[exp-radio] embed fetch error: {e}", flush=True)

    except Exception as e:
        print(f"[exp-radio] Suno scrape error {uuid}: {e}", flush=True)

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

    # Find the first structural marker (Verse/Chorus/##/[...]) — anything
    # before it is treated as preamble (author note, URLs, etc.) and dropped.
    # If no marker is present we keep all lines.
    start = 0
    for i, line in enumerate(lines):
        if _is_section_marker(line):
            start = i
            break

    out = []
    for line in lines[start:]:
        # Skip the section marker lines themselves — they are not sung.
        if _is_section_marker(line):
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

# Model size can be overridden via env (tiny, base, small, medium, large-v3)
_WHISPER_MODEL = os.environ.get("EXP_RADIO_WHISPER_MODEL", "large-v3-turbo")


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
    return words


async def run_whisper(mp3_path: str, lyrics_prompt: str = "", language: Optional[str] = None) -> list:
    """Run Whisper in thread pool so the event loop is not blocked."""
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
            f"[exp-radio] Alignment confidence too low "
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
    print(
        f"[exp-radio] Aligned: {matched}/{len(whisper_words)} exact matches, "
        f"{corrected} word(s) re-cased from lyrics "
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
        raw_lyrics_text = meta["raw_lyrics"] or ""
        lyrics    = clean_lyrics(raw_lyrics_text)
        print(
            f"[exp-radio] #{song_id} lyrics: {len(raw_lyrics_text)} raw chars → "
            f"{len(lyrics)} clean chars",
            flush=True,
        )
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

        # 3) Whisper word-timestamp analysis. We do NOT pass the lyric text
        # itself as initial_prompt (Whisper echoes it as the first sung
        # words). Instead we pass a comma-separated vocabulary hint built
        # from capitalised proper-noun candidates so the model still learns
        # to spell rare names correctly.
        vocab_hint = extract_vocab_hints(lyrics)
        detected_lang = detect_lyrics_language(lyrics)
        print(
            f"[exp-radio] Starting Whisper analysis for #{song_id} "
            f"(model={_WHISPER_MODEL}, lang={detected_lang or 'auto'}, "
            f"vocab_hint={vocab_hint!r})…",
            flush=True,
        )
        words = await run_whisper(
            mp3_path, lyrics_prompt=vocab_hint, language=detected_lang,
        )
        print(f"[exp-radio] Whisper done: {len(words)} words for #{song_id}", flush=True)

        # 3b) Forced-alignment-light: keep Whisper structure/timing, correct
        # word spellings from the lyric sheet wherever they agree exactly.
        if lyrics:
            words = _align_to_lyrics(words, lyrics)

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

        # 5) Optional LLM-based lyric moderation. Runs only when explicitly
        #    enabled in the admin settings; otherwise the column stays NULL
        #    and the stream playlist filter treats it as grandfathered/passed.
        moderation_enabled = await db.get_setting("exp_radio_moderation_enabled") or "off"
        if moderation_enabled == "on":
            from bot.exp_stream_manager import log_event
            try:
                from bot.exp_moderation import moderate_lyrics
                from bot.llm import OllamaClient
                from config import Config
                client = OllamaClient(
                    base_url=Config.OLLAMA_URL,
                    model=Config.LLM_MODEL,
                    timeout=Config.LLM_REQUEST_TIMEOUT,
                )
                log_event(f"Moderation start for #{song_id} ({title!r})", prefix="[mod]")
                verdict = await moderate_lyrics(
                    client, lyrics=lyrics, title=title, artist=artist,
                )
                await db.update_exp_radio_song(
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
                        f"⚠️ **Experimental Radio** — your submission **{title}** "
                        f"was flagged by automated lyric review and is pending "
                        f"manual approval before it joins the stream playlist.\n"
                        f"Reason: _{verdict.get('reason') or 'no reason given'}_",
                    )
            except Exception as e:
                log_event(
                    f"Moderation pipeline error for #{song_id}: {e}",
                    level="error", prefix="[mod]",
                )
                await db.update_exp_radio_song(
                    song_id,
                    moderation_status="pending",
                    moderation_reason=f"Moderation error: {e!s}",
                    moderation_at=time.time(),
                )

    except Exception as e:
        print(f"[exp-radio] Pipeline error for #{song_id}: {e}", flush=True)
        await db.update_exp_radio_song(song_id, analysis_status="failed")
