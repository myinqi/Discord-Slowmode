import asyncio
import re
from typing import Optional

import aiohttp


SUNO_UUID_RE = re.compile(
    r"(?:https?://)?(?:app\.)?suno\.(?:com|ai)/(?:song|track)/"
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})(?:\b|/)",
    re.IGNORECASE,
)
SUNO_SHORT_RE = re.compile(
    r"(?:https?://)?(?:app\.)?suno\.(?:com|ai)/s/([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)
UUID_RE = re.compile(
    r"\b([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\b"
)


def extract_suno_uuid(value: str | None) -> Optional[str]:
    """Return a canonical lowercase Suno song UUID when it is present."""
    if not value:
        return None
    match = SUNO_UUID_RE.search(str(value))
    return match.group(1).lower() if match else None


async def resolve_suno_uuid(value: str | None, timeout_seconds: int = 12) -> Optional[str]:
    """Resolve full or short Suno song URLs to their canonical song UUID."""
    direct = extract_suno_uuid(value)
    if direct:
        return direct
    short_match = SUNO_SHORT_RE.search(str(value or ""))
    if not short_match:
        return None

    headers = {"User-Agent": "Mozilla/5.0 (compatible; CoraxBot/1.0)"}
    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    try:
        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
            async with session.get(
                f"https://suno.com/s/{short_match.group(1)}",
                allow_redirects=True,
            ) as response:
                resolved = extract_suno_uuid(str(response.url))
                if resolved:
                    return resolved
                body = await response.text(errors="ignore")
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None

    direct = extract_suno_uuid(body)
    if direct:
        return direct
    cdn_match = re.search(
        r"cdn[12]\.suno\.ai/([0-9a-fA-F-]{36})\.(?:mp3|m4a)", body
    )
    return cdn_match.group(1).lower() if cdn_match else None
