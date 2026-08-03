"""Resolve Suno Hook IDs/share links through Suno's public Hook endpoint."""

from __future__ import annotations

import re
from urllib.parse import urlparse

import aiohttp


HOOK_ID_RE = re.compile(
    r"^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$",
    re.IGNORECASE,
)
HOOK_PATH_RE = re.compile(
    r"/hook/([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})(?:[/?#]|$)",
    re.IGNORECASE,
)
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


class SunoHookError(ValueError):
    pass


def _validate_suno_url(value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.hostname not in {"suno.com", "www.suno.com"}:
        raise SunoHookError("Please enter a Suno Hook ID or a suno.com Hook/share link.")


async def resolve_suno_hook(value: str) -> dict:
    """Return validated public Hook metadata for an ID or Suno share URL."""
    raw = (value or "").strip()
    if not raw:
        raise SunoHookError("Hook ID or share link is required.")

    hook_id = raw if HOOK_ID_RE.fullmatch(raw) else None
    share_url = ""
    timeout = aiohttp.ClientTimeout(total=30)
    headers = {"User-Agent": _BROWSER_UA}

    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        if not hook_id:
            _validate_suno_url(raw)
            share_url = raw
            try:
                async with session.get(raw, allow_redirects=True) as response:
                    if response.status != 200:
                        raise SunoHookError(
                            f"Suno Hook link returned HTTP {response.status}."
                        )
                    match = HOOK_PATH_RE.search(str(response.url))
                    if not match:
                        match = HOOK_PATH_RE.search(await response.text())
            except aiohttp.ClientError as exc:
                raise SunoHookError(f"Could not open the Suno Hook link: {exc}") from exc
            if not match:
                raise SunoHookError("The supplied Suno link does not resolve to a Hook.")
            hook_id = match.group(1)

        api_url = f"https://studio-api-prod.suno.com/api/video/hooks/{hook_id}"
        try:
            async with session.get(api_url) as response:
                if response.status != 200:
                    raise SunoHookError(
                        f"Suno Hook API returned HTTP {response.status}."
                    )
                payload = await response.json(content_type=None)
        except (aiohttp.ClientError, ValueError) as exc:
            raise SunoHookError(f"Could not load Hook metadata: {exc}") from exc

    if not isinstance(payload, dict):
        raise SunoHookError("Suno returned invalid Hook metadata.")
    resolved_id = str(payload.get("id") or hook_id)
    video_url = str(payload.get("rendered_video_url") or "").strip()
    original_clip_id = str(payload.get("original_clip_id") or "").strip()
    parsed_video = urlparse(video_url)
    if not HOOK_ID_RE.fullmatch(resolved_id):
        raise SunoHookError("Suno returned an invalid Hook ID.")
    if (
        parsed_video.scheme != "https"
        or not parsed_video.hostname
        or not (
            parsed_video.hostname == "suno.ai"
            or parsed_video.hostname.endswith(".suno.ai")
        )
    ):
        raise SunoHookError("Suno returned no trusted Hook video URL.")
    if not original_clip_id:
        raise SunoHookError("Suno returned no source song for this Hook.")

    return {
        "hook_id": resolved_id,
        "hook_share_url": share_url,
        "hook_video_url": video_url,
        "original_clip_id": original_clip_id,
        "duration": payload.get("video_duration"),
    }
