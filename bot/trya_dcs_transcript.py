"""Parse DCS subtitle transcripts from Whisper JSON or timed text."""

import json
import math
import re

_TIMESTAMP_LINE_RE = re.compile(
    r"^\s*\[(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)\]\s*(.*)$"
)


def _timestamp_to_seconds(hours: str | None, minutes: str, seconds: str) -> float:
    value = int(minutes) * 60 + float(seconds)
    if hours:
        value += int(hours) * 3600
    return value


def parse_dcs_transcript(raw_text: str, *, duration: float = 0) -> list[dict]:
    """Accept Whisper JSON or timestamped lines like `[1:07] word word`."""
    text = str(raw_text or "").strip()
    if not text:
        raise ValueError("transcript is empty")
    if _looks_like_json_transcript(text):
        try:
            raw_words = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"transcript is not valid JSON: {exc.msg}") from exc
        if not isinstance(raw_words, list):
            raise ValueError("JSON transcript must be an array of word objects")
        return _normalize_word_entries(raw_words, duration=duration)
    return _words_from_timestamped_text(text, duration=duration)


def _looks_like_json_transcript(text: str) -> bool:
    stripped = text.lstrip()
    if stripped.startswith("{"):
        return True
    if not stripped.startswith("["):
        return False
    rest = stripped[1:].lstrip()
    return rest.startswith("{") or rest.startswith("]")


def _words_from_timestamped_text(text: str, *, duration: float = 0) -> list[dict]:
    blocks: list[tuple[float, str]] = []
    current_time: float | None = None
    current_parts: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _TIMESTAMP_LINE_RE.match(line)
        if match:
            if current_time is not None:
                blocks.append((current_time, " ".join(current_parts).strip()))
            current_time = _timestamp_to_seconds(
                match.group(1), match.group(2), match.group(3)
            )
            current_parts = [match.group(4).strip()]
            continue
        if current_time is None:
            raise ValueError(
                "timed transcript lines must start with [m:ss] or [h:mm:ss]"
            )
        current_parts.append(line)
    if current_time is not None:
        blocks.append((current_time, " ".join(current_parts).strip()))
    if not blocks:
        raise ValueError("timed transcript did not contain any [m:ss] lines")

    words: list[dict] = []
    for index, (start, content) in enumerate(blocks):
        tokens = [token for token in content.split() if token]
        if not tokens:
            continue
        if index + 1 < len(blocks):
            end_at = blocks[index + 1][0]
        elif duration > 0:
            end_at = min(duration, start + max(1.2, 0.35 * len(tokens)))
        else:
            end_at = start + max(1.2, 0.35 * len(tokens))
        if end_at <= start:
            end_at = start + max(0.4, 0.2 * len(tokens))
        step = (end_at - start) / len(tokens)
        for offset, token in enumerate(tokens):
            word_start = start + offset * step
            word_end = start + (offset + 1) * step
            words.append({
                "word": token[:200],
                "start": round(word_start, 3),
                "end": round(max(word_end, word_start + 0.05), 3),
            })
    return _normalize_word_entries(words, duration=duration)


def _normalize_word_entries(raw_words: list, *, duration: float = 0) -> list[dict]:
    if not isinstance(raw_words, list) or not raw_words or len(raw_words) > 20000:
        raise ValueError("transcript must be a non-empty array with at most 20,000 words")
    words = []
    previous_start = -1.0
    for index, item in enumerate(raw_words):
        if not isinstance(item, dict):
            raise ValueError(f"word #{index + 1} must be an object")
        extra = set(item) - {"word", "start", "end"}
        missing = {"word", "start", "end"} - set(item)
        if missing or extra:
            raise ValueError(f"word #{index + 1} must contain exactly word, start and end")
        word = str(item.get("word") or "").strip()
        try:
            start = float(item["start"])
            end = float(item["end"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"word #{index + 1} has invalid timestamps") from exc
        if (
            not word or len(word) > 200
            or not math.isfinite(start) or not math.isfinite(end)
            or start < 0 or end <= start
        ):
            raise ValueError(f"word #{index + 1} is empty or has an invalid time range")
        if duration > 0:
            if start > duration + 5:
                raise ValueError(f"word #{index + 1} is out of chronological or song range")
            end = min(end, max(duration, start + 0.05))
        if start < previous_start:
            raise ValueError(f"word #{index + 1} is out of chronological or song range")
        words.append({"word": word, "start": round(start, 3), "end": round(end, 3)})
        previous_start = start
    return words
