"""LLM-based lyric moderation for the Experimental Radio.

Sends a song's lyrics (plus an automatic English translation when possible)
to the local qwen2.5 instance via the existing :class:`OllamaClient`, asks for
a strict JSON verdict and returns a normalised dict.

Design goals
------------
* No false-positive avalanche: artistic darkness, sadness, mild profanity and
  fictional violence are NOT grounds to flag. The model only flags real
  community-rule violations (hate speech against protected groups, sexual
  content involving minors, glorification of illegal acts, explicit slurs
  used to attack, etc.).
* Robust against LLM hiccups: any timeout / JSON parse error returns
  ``{"verdict": "pending", ...}`` so the song lands in the manual-review
  queue rather than being silently passed.
* Reuses the existing translation helper (Google Translate via
  ``deep_translator``) that the Suno Info Player already uses for lyric
  translations.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any

from bot.llm import OllamaClient


# ── Configuration ─────────────────────────────────────────────────────────────

# Hard cap on lyric size before truncation. Keeps the prompt small and the LLM
# response fast. 4000 characters comfortably covers a typical song.
_MAX_LYRIC_CHARS = 4000

# Per-call LLM timeout in seconds. qwen2.5:7b on CPU can take 60-150 s on
# longer lyrics (~2500 chars + an English translation block). We give it a
# generous ceiling but bail out if it stalls so the worker stays responsive
# — songs that time out land in 'pending' for manual review.
_LLM_TIMEOUT_SECS = 240

# Soft translation timeout — we do not want a slow Google Translate response
# to hold up moderation. If translation fails we proceed with the original
# lyrics only.
_TRANSLATE_TIMEOUT_SECS = 8


# Community rules the LLM should enforce. Keep this in sync with the user-
# facing submission terms in bot/cogs/commands.py (_EXP_TERMS_DISPLAY).
_SYSTEM_PROMPT = """You are a strict but fair content moderator for a small Discord community radio that streams user-submitted AI-generated songs to Twitch.

You evaluate song lyrics against the following community rules:

  1. NO hate speech, slurs or incitement against protected groups
     (race, ethnicity, religion, sexual orientation, gender identity, disability).
  2. NO sexual content involving minors. Adult sexual themes presented
     artistically are allowed; explicit pornographic descriptions are not.
  3. NO glorification or instruction of illegal acts (e.g. terrorism, hard
     drug synthesis, real-world violence against named individuals).
  4. Mild profanity, artistic darkness, sadness, fictional violence,
     metaphorical themes, religious/spiritual content and politically
     charged but non-hateful messages are ALLOWED. Do not flag for these.

Output rules:
  - Respond with a SINGLE JSON object, nothing else. No markdown, no prose.
  - Schema:
        {"verdict": "ok" | "flag",
         "reason": "<one short sentence in English, max 200 chars>",
         "categories": ["<short label>", ...]}
  - "verdict": "flag" ONLY if at least one of rules 1-3 is clearly violated.
  - When uncertain, prefer "ok". Errors of caution against the community
    are worse than letting a borderline-dark song through.
  - "reason" must be empty string "" when verdict is "ok".
  - "categories" must be empty list [] when verdict is "ok".
""".strip()


_USER_TEMPLATE = """Evaluate the following song lyrics.

Title: {title}
Artist: {artist}

--- LYRICS (original language) ---
{lyrics}
--- END LYRICS ---
{translation_block}
Respond with the JSON verdict only.
""".lstrip()


# ── Translation helper ────────────────────────────────────────────────────────

async def _translate_to_english(text: str) -> str | None:
    """Best-effort translation of `text` to English via Google Translate.

    Returns the translated string, or ``None`` if translation failed / timed
    out. Uses the same ``deep_translator`` dependency the Suno Info Player
    already relies on (see ``/api/translate-lyrics`` in web/app.py)."""
    text = (text or "").strip()
    if not text:
        return None
    # Skip translation if the lyrics are already largely ASCII (assume
    # English/Latin script with negligible foreign content). The LLM handles
    # English natively, so the round-trip would only add latency.
    if all(ord(ch) < 128 for ch in text):
        return None
    try:
        from deep_translator import GoogleTranslator
    except Exception:
        return None
    loop = asyncio.get_event_loop()
    try:
        return await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: GoogleTranslator(source="auto", target="en").translate(
                    text[:_MAX_LYRIC_CHARS]
                ),
            ),
            timeout=_TRANSLATE_TIMEOUT_SECS,
        )
    except Exception:
        return None


# ── JSON parsing ──────────────────────────────────────────────────────────────

# Strips markdown code fences the model might emit despite the instructions.
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


def _parse_verdict(raw: str) -> dict[str, Any] | None:
    """Extract the JSON verdict object from a model response.

    Tolerates leading/trailing text and markdown fences. Returns ``None`` if
    no valid JSON object with a ``verdict`` field could be located."""
    if not raw:
        return None
    text = _FENCE_RE.sub("", raw).strip()
    # Try whole-string parse first.
    candidates: list[str] = []
    candidates.append(text)
    # Fall back to the first {...} block we can find.
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if m:
        candidates.append(m.group(0))
    for c in candidates:
        try:
            obj = json.loads(c)
            if isinstance(obj, dict) and "verdict" in obj:
                return obj
        except Exception:
            continue
    return None


# ── Public entry point ────────────────────────────────────────────────────────

async def moderate_lyrics(
    client: OllamaClient,
    lyrics: str,
    title: str = "",
    artist: str = "",
    timeout: int | None = None,
) -> dict[str, Any]:
    """Run the LLM moderation pipeline against a song's lyrics.

    Returns a dict with stable keys for direct DB storage:

        {"status":     "passed" | "flagged" | "pending",
         "reason":     <str>,
         "categories": [<str>, ...],
         "raw":        <str>,  # untouched LLM response, for debugging
         "translated": <bool>} # whether an English translation was added
    """
    lyrics = (lyrics or "").strip()
    if not lyrics:
        # Nothing to evaluate — treat as passed so instrumental tracks don't
        # block the playlist. The empty-lyrics edge case is benign.
        return {
            "status": "passed", "reason": "", "categories": [],
            "raw": "", "translated": False,
        }

    truncated = lyrics[:_MAX_LYRIC_CHARS]
    translation = await _translate_to_english(truncated)

    if translation and translation.strip() and translation.strip() != truncated.strip():
        translation_block = (
            "\n--- ENGLISH TRANSLATION (auto, for reference) ---\n"
            f"{translation.strip()[:_MAX_LYRIC_CHARS]}\n"
            "--- END TRANSLATION ---\n"
        )
        translated_flag = True
    else:
        translation_block = ""
        translated_flag = False

    user_msg = _USER_TEMPLATE.format(
        title=title or "Unknown",
        artist=artist or "Unknown",
        lyrics=truncated,
        translation_block=translation_block,
    )

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user",   "content": user_msg},
    ]

    call_timeout = timeout if timeout is not None else _LLM_TIMEOUT_SECS
    try:
        resp = await asyncio.wait_for(
            client.chat(
                messages,
                # JSON-only output, no creative tangents.
                temperature=0.1, top_p=0.9, repeat_penalty=1.05,
                max_tokens=300,
            ),
            timeout=call_timeout,
        )
    except asyncio.TimeoutError:
        return {
            "status": "pending",
            "reason": f"LLM moderation timed out after {call_timeout}s — manual review required.",
            "categories": [], "raw": "", "translated": translated_flag,
        }
    except Exception as e:
        return {
            "status": "pending",
            "reason": f"LLM moderation error: {e!s} — manual review required.",
            "categories": [], "raw": "", "translated": translated_flag,
        }

    raw = ((resp or {}).get("message") or {}).get("content") or ""
    parsed = _parse_verdict(raw)
    if not parsed:
        return {
            "status": "pending",
            "reason": "LLM returned no parseable verdict — manual review required.",
            "categories": [], "raw": raw, "translated": translated_flag,
        }

    verdict = str(parsed.get("verdict", "")).strip().lower()
    reason  = str(parsed.get("reason", "") or "").strip()
    cats    = parsed.get("categories") or []
    if not isinstance(cats, list):
        cats = []
    cats = [str(c)[:60] for c in cats if c][:8]

    if verdict == "flag":
        return {
            "status": "flagged",
            "reason": reason[:500] or "Flagged by LLM (no reason provided).",
            "categories": cats,
            "raw": raw, "translated": translated_flag,
        }
    if verdict == "ok":
        return {
            "status": "passed", "reason": "", "categories": [],
            "raw": raw, "translated": translated_flag,
        }
    # Unrecognised verdict string — fail safe to manual review.
    return {
        "status": "pending",
        "reason": f"Unrecognised LLM verdict '{verdict}' — manual review required.",
        "categories": [], "raw": raw, "translated": translated_flag,
    }
