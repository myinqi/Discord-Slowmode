"""Discord-free helpers for TrYa DCS web chat payloads and send dedup."""

import re
import time

_ROLE_MENTION_RE = re.compile(r"<@&\d{17,20}>")
_EVERYONE_RE = re.compile(r"@(everyone|here)\b", re.IGNORECASE)
_WEB_CHAT_DEDUP_WINDOW = 3.0
_recent_web_chat: dict[int, list[tuple[str, str, float]]] = {}


def parse_web_chat_payload(
    payload: dict | None,
) -> tuple[str, str | None, list[str], str | None]:
    data = payload if isinstance(payload, dict) else {}
    content = str(data.get("content") or "").strip()
    reply_to = str(data.get("reply_to") or "").strip() or None
    if reply_to and not reply_to.isdigit():
        reply_to = None
    mention_ids = []
    raw_ids = data.get("mention_ids") if isinstance(data.get("mention_ids"), list) else []
    for raw in raw_ids[:20]:
        value = str(raw).strip()
        if value.isdigit() and value not in mention_ids:
            mention_ids.append(value)
    client_message_id = str(data.get("client_message_id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", client_message_id):
        client_message_id = None
    return content, reply_to, mention_ids, client_message_id


def register_web_chat_fingerprint(
    user_id: int,
    content: str,
    reply_to: str | int | None = None,
    client_message_id: str | None = None,
    *,
    now: float | None = None,
    window: float = _WEB_CHAT_DEDUP_WINDOW,
    store: dict[int, list[tuple[str, str, float]]] | None = None,
) -> bool:
    """Return True if this web chat send is new and should be delivered.

    Modern clients supply a unique message ID, so intentional repetitions such
    as game commands remain distinct while a transport retry is deduplicated.
    Older clients fall back to the original short content-based window.
    """
    target = _recent_web_chat if store is None else store
    stamp = time.monotonic() if now is None else now
    key = int(user_id)
    fingerprint = (
        f"id:{client_message_id}" if client_message_id else str(content or ""),
        str(reply_to or ""),
    )
    recent = [
        item for item in target.get(key, [])
        if stamp - item[2] < window
    ]
    if any(item[0] == fingerprint[0] and item[1] == fingerprint[1] for item in recent):
        target[key] = recent
        return False
    recent.append((fingerprint[0], fingerprint[1], stamp))
    target[key] = recent
    return True


def forget_web_chat_fingerprint(
    user_id: int,
    content: str,
    reply_to: str | int | None = None,
    client_message_id: str | None = None,
    *,
    store: dict[int, list[tuple[str, str, float]]] | None = None,
) -> None:
    """Drop a fingerprint so a failed delivery can be retried."""
    target = _recent_web_chat if store is None else store
    key = int(user_id)
    fingerprint = (
        f"id:{client_message_id}" if client_message_id else str(content or ""),
        str(reply_to or ""),
    )
    target[key] = [
        item for item in target.get(key, [])
        if not (item[0] == fingerprint[0] and item[1] == fingerprint[1])
    ]


def neutralize_mass_mentions(content: str) -> str:
    """Prevent @everyone/@here and role pings from untrusted web input."""
    clean = _ROLE_MENTION_RE.sub(lambda match: match.group(0).replace("@", "@\u200b", 1), content)
    return _EVERYONE_RE.sub(lambda match: "@\u200b" + match.group(1).lower(), clean)


_CUSTOM_EMOJI_MARKUP_RE = re.compile(r"^<a?:[A-Za-z0-9_]{2,32}:\d{17,20}>$")


def is_allowed_dcs_emoji_markup(value: str) -> bool:
    """Accept guild custom markup or a short unicode emoji, nothing else."""
    text = str(value or "").strip()
    if _CUSTOM_EMOJI_MARKUP_RE.fullmatch(text):
        return True
    if not text or len(text) > 16 or "<" in text or ">" in text or "\n" in text:
        return False
    return any(ord(char) > 127 for char in text)
