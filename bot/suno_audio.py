"""Fetch playable Suno audio without the logged-in official download API.

After the public cdn1.suno.ai/{uuid}.mp3 path started returning 403, clips
are served as encrypted .m4a from rotating CloudFront URLs listed in the
embed page (media_urls). The AES-CTR content key is unwrapped from
POST /api/mango/rights using AES-GCM keyed by SHA-256(glt). Same anonymous
embed-player flow as Songalyze's suno_audio.py and web/static/suno_playback.js.

This path never calls /api/download/clip and does not consume Suno download quota.
"""

from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

import aiohttp
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SUNO_ORIGIN = "https://suno.com"
SUNO_API_BASE = "https://studio-api.prod.suno.com"
OFFICIAL_DOWNLOAD_PATH = "/api/download/clip"
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_MAX_AUDIO_BYTES = 64 * 1024 * 1024


def extract_suno_audio_url(page_html: str, song_uuid: str) -> str | None:
    """Return Suno's current playable asset, preferring non-legacy CDNs."""
    candidates = [
        candidate.replace(r"\/", "/")
        for candidate in re.findall(
            r'https://[^"\\\s]+\.(?:mp3|m4a)(?:\?[^"\\\s]*)?',
            page_html or "",
            flags=re.I,
        )
        if song_uuid in candidate
    ]
    return next(
        (
            candidate
            for candidate in candidates
            if "cdn1.suno.ai" not in candidate and "cdn2.suno.ai" not in candidate
        ),
        candidates[0] if candidates else None,
    )


def audio_url_is_encrypted(audio_url: str | None) -> bool:
    if not audio_url:
        return False
    return "cdn1.suno.ai" not in audio_url and "cdn2.suno.ai" not in audio_url


def decrypt_suno_audio(song_uuid: str, encrypted: bytes, license: dict[str, str]) -> bytes:
    glt = str(license.get("glt") or "")
    key_b64 = str(license.get("key") or "")
    iv_b64 = str(license.get("iv") or "")
    if not glt or not key_b64 or not iv_b64:
        raise RuntimeError("Incomplete Suno playback license (key/iv/glt)")
    guest_key = hashlib.sha256(glt.encode("utf-8")).digest()
    aesgcm = AESGCM(guest_key)
    context = song_uuid.encode("utf-8")

    def unwrap(value: str) -> bytes:
        wrapped = base64.b64decode(value)
        if len(wrapped) < 13:
            raise RuntimeError("Suno license value is too short")
        return aesgcm.decrypt(wrapped[:12], wrapped[12:], context)

    content_key = unwrap(key_b64)
    content_iv = unwrap(iv_b64)
    if len(content_iv) < 16:
        content_iv = content_iv + b"\x00" * (16 - len(content_iv))
    else:
        content_iv = content_iv[:16]
    decryptor = Cipher(algorithms.AES(content_key), modes.CTR(content_iv)).decryptor()
    clear = decryptor.update(encrypted) + decryptor.finalize()
    if len(clear) < 12 or clear[4:8] != b"ftyp":
        raise RuntimeError("Decrypted Suno audio is not a valid MP4/M4A")
    return clear


async def fetch_embed_html(session: aiohttp.ClientSession, song_uuid: str) -> str:
    url = f"{SUNO_ORIGIN}/embed/{song_uuid}"
    async with session.get(
        url,
        headers={"User-Agent": _BROWSER_UA},
        timeout=aiohttp.ClientTimeout(total=20),
    ) as response:
        if response.status != 200:
            raise RuntimeError(f"Suno embed HTTP {response.status}")
        return await response.text()


async def request_audio_license(
    song_uuid: str,
    session: aiohttp.ClientSession | None = None,
) -> dict[str, str]:
    async def _post(sess: aiohttp.ClientSession) -> dict[str, str]:
        async with sess.post(
            f"{SUNO_API_BASE}/api/mango/rights",
            json={"content_params": {"content_id": song_uuid, "content_type": "clip"}},
            headers={
                "Origin": SUNO_ORIGIN,
                "Referer": f"{SUNO_ORIGIN}/song/{song_uuid}",
                "User-Agent": _BROWSER_UA,
                "Content-Type": "application/json",
            },
            timeout=aiohttp.ClientTimeout(total=15),
        ) as response:
            if response.status != 200:
                raise RuntimeError(f"Suno audio license HTTP {response.status}")
            rights = await response.json(content_type=None)
        if not isinstance(rights, dict) or not all(
            isinstance(rights.get(key), str) and rights[key] for key in ("key", "iv", "glt")
        ):
            raise RuntimeError("Invalid Suno audio license")
        return {"key": rights["key"], "iv": rights["iv"], "glt": rights["glt"]}

    if session is not None:
        return await _post(session)
    async with aiohttp.ClientSession() as owned:
        return await _post(owned)


async def fetch_bytes(
    session: aiohttp.ClientSession,
    url: str,
    *,
    timeout: float = 90,
    referer: str | None = None,
) -> tuple[int, bytes]:
    if OFFICIAL_DOWNLOAD_PATH in url:
        raise RuntimeError("Official Suno download API is not used for anonymous audio fetch")
    headers = {"User-Agent": _BROWSER_UA}
    if referer:
        headers["Referer"] = referer
        headers["Origin"] = SUNO_ORIGIN
    async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=timeout)) as response:
        if response.status != 200:
            return response.status, b""
        length = int(response.headers.get("Content-Length") or 0)
        if length > _MAX_AUDIO_BYTES:
            raise RuntimeError("Suno audio file is too large")
        data = await response.read()
        if len(data) > _MAX_AUDIO_BYTES:
            raise RuntimeError("Suno audio file is too large")
        return response.status, data


async def write_playable_suno_audio(
    song_uuid: str,
    dest_path: str | Path,
    *,
    session: aiohttp.ClientSession | None = None,
) -> Path:
    """Write decrypted (or legacy) Suno audio to dest_path. No login, no quota."""
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)

    async def _run(sess: aiohttp.ClientSession) -> Path:
        audio_url: str | None = None
        try:
            embed = await fetch_embed_html(sess, song_uuid)
            audio_url = extract_suno_audio_url(embed, song_uuid)
        except Exception:
            audio_url = None

        if audio_url and audio_url_is_encrypted(audio_url):
            license_data = await request_audio_license(song_uuid, sess)
            status, encrypted = await fetch_bytes(
                sess,
                audio_url,
                timeout=120,
                referer=f"{SUNO_ORIGIN}/song/{song_uuid}",
            )
            if status != 200 or not encrypted:
                raise RuntimeError(f"Suno audio HTTP {status or 'empty'}")
            dest.write_bytes(decrypt_suno_audio(song_uuid, encrypted, license_data))
            return dest

        if audio_url:
            status, data = await fetch_bytes(sess, audio_url, timeout=90)
            if status == 200 and data:
                dest.write_bytes(data)
                return dest

        last_status: int | None = None
        for ext in ("mp3", "m4a"):
            legacy = f"https://cdn1.suno.ai/{song_uuid}.{ext}"
            status, data = await fetch_bytes(sess, legacy, timeout=90)
            last_status = status
            if status == 200 and data:
                dest.write_bytes(data)
                return dest

        raise RuntimeError(
            "Suno audio is unavailable: encrypted media_urls and legacy CDN both failed"
            + (f" (HTTP {last_status})" if last_status is not None else "")
        )

    if session is not None:
        return await _run(session)
    async with aiohttp.ClientSession(headers={"User-Agent": _BROWSER_UA}) as owned:
        return await _run(owned)
