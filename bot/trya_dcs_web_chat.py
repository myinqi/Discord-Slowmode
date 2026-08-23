"""Discord-free helpers for TrYa DCS web chat payloads and send dedup."""

import re
import time

_ROLE_MENTION_RE = re.compile(r"<@&\d{17,20}>")
_EVERYONE_RE = re.compile(r"@(everyone|here)\b", re.IGNORECASE)
_WEB_CHAT_DEDUP_WINDOW = 3.0
_recent_web_chat: dict[int, list[tuple[str, str, float]]] = {}


def parse_web_chat_payload(payload: dict | None) -> tuple[str, str | None, list[str]]:
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
    return content, reply_to, mention_ids


def register_web_chat_fingerprint(
    user_id: int,
    content: str,
    reply_to: str | int | None = None,
    *,
    now: float | None = None,
    window: float = _WEB_CHAT_DEDUP_WINDOW,
    store: dict[int, list[tuple[str, str, float]]] | None = None,
) -> bool:
    """Return True if this web chat send is new and should be delivered.

    Browsers can submit the same comment twice (Enter + button, Firefox
    fallback fetch before the input is cleared). Identical content from the
    same member within a few seconds is treated as one send.
    """
    target = _recent_web_chat if store is None else store
    stamp = time.monotonic() if now is None else now
    key = int(user_id)
    fingerprint = (str(content or ""), str(reply_to or ""))
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
    *,
    store: dict[int, list[tuple[str, str, float]]] | None = None,
) -> None:
    """Drop a fingerprint so a failed delivery can be retried."""
    target = _recent_web_chat if store is None else store
    key = int(user_id)
    fingerprint = (str(content or ""), str(reply_to or ""))
    target[key] = [
        item for item in target.get(key, [])
        if not (item[0] == fingerprint[0] and item[1] == fingerprint[1])
    ]


def neutralize_mass_mentions(content: str) -> str:
    """Prevent @everyone/@here and role pings from untrusted web input."""
    clean = _ROLE_MENTION_RE.sub(lambda match: match.group(0).replace("@", "@\u200b", 1), content)
    return _EVERYONE_RE.sub(lambda match: "@\u200b" + match.group(1).lower(), clean)
