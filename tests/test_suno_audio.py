import base64
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from bot.suno_audio import (
    OFFICIAL_DOWNLOAD_PATH,
    decrypt_suno_audio,
    extract_suno_audio_url,
    write_playable_suno_audio,
)


SONG_UUID = "d5cb3c90-dc11-4d79-9553-3fe1fcdf4e8c"


def _wrap_license_value(raw: bytes, glt: str, song_uuid: str) -> str:
    guest_key = hashlib.sha256(glt.encode("utf-8")).digest()
    nonce = os.urandom(12)
    wrapped = nonce + AESGCM(guest_key).encrypt(nonce, raw, song_uuid.encode("utf-8"))
    return base64.b64encode(wrapped).decode("ascii")


def _encrypted_sample(song_uuid: str = SONG_UUID):
    glt = "guest-license-token"
    content_key = os.urandom(16)
    content_iv = os.urandom(16)
    clear = b"\x00\x00\x00\x18ftypisom" + os.urandom(24)
    encryptor = Cipher(algorithms.AES(content_key), modes.CTR(content_iv)).encryptor()
    encrypted = encryptor.update(clear) + encryptor.finalize()
    license_data = {
        "glt": glt,
        "key": _wrap_license_value(content_key, glt, song_uuid),
        "iv": _wrap_license_value(content_iv, glt, song_uuid),
    }
    return clear, encrypted, license_data


class _FakeResponse:
    def __init__(self, status: int, body: bytes | str | dict):
        self.status = status
        self._body = body
        self.headers = {}
        if isinstance(body, (bytes, str)):
            self.headers["Content-Length"] = str(len(body if isinstance(body, bytes) else body.encode()))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def text(self):
        if isinstance(self._body, bytes):
            return self._body.decode("utf-8")
        if isinstance(self._body, str):
            return self._body
        raise TypeError("text() on JSON response")

    async def read(self):
        if isinstance(self._body, bytes):
            return self._body
        if isinstance(self._body, str):
            return self._body.encode("utf-8")
        raise TypeError("read() on JSON response")

    async def json(self, content_type=None):
        if isinstance(self._body, dict):
            return self._body
        raise TypeError("json() on non-dict response")


class _FakeSession:
    def __init__(self, routes: dict[str, _FakeResponse]):
        self.routes = routes
        self.requested = []

    def get(self, url, **kwargs):
        self.requested.append(("GET", url))
        if OFFICIAL_DOWNLOAD_PATH in url:
            raise AssertionError("official download API must not be used")
        response = self.routes.get(("GET", url))
        if response is None:
            return _FakeResponse(404, b"")
        return response

    def post(self, url, **kwargs):
        self.requested.append(("POST", url))
        if OFFICIAL_DOWNLOAD_PATH in url:
            raise AssertionError("official download API must not be used")
        response = self.routes.get(("POST", url))
        if response is None:
            return _FakeResponse(404, {})
        return response


class SunoAudioTests(unittest.TestCase):
    def test_current_media_url_is_preferred_over_legacy_cdn(self):
        current = f"https://audio.example/1/clip/{SONG_UUID}.m4a"
        legacy = f"https://cdn1.suno.ai/{SONG_UUID}.mp3"
        page_html = rf'\"media_urls\":[{{\"url\":\"{current}\"}}] {legacy}'
        self.assertEqual(extract_suno_audio_url(page_html, SONG_UUID), current)

    def test_decrypt_roundtrip(self):
        clear, encrypted, license_data = _encrypted_sample()
        self.assertEqual(decrypt_suno_audio(SONG_UUID, encrypted, license_data), clear)


class SunoAudioDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_write_playable_audio_uses_anonymous_encrypted_path(self):
        clear, encrypted, license_data = _encrypted_sample()
        media_url = f"https://d111111abcdef8.cloudfront.net/clip/{SONG_UUID}.m4a"
        embed = f'https://cdn1.suno.ai/{SONG_UUID}.mp3 {media_url}'
        session = _FakeSession({
            ("GET", f"https://suno.com/embed/{SONG_UUID}"): _FakeResponse(200, embed),
            ("POST", "https://studio-api.prod.suno.com/api/mango/rights"): _FakeResponse(200, license_data),
            ("GET", media_url): _FakeResponse(200, encrypted),
        })
        with tempfile.TemporaryDirectory() as tmpdir:
            dest = Path(tmpdir) / "source.audio"
            result = await write_playable_suno_audio(SONG_UUID, dest, session=session)
            self.assertEqual(result.read_bytes(), clear)
        methods_and_urls = session.requested
        self.assertIn(("POST", "https://studio-api.prod.suno.com/api/mango/rights"), methods_and_urls)
        self.assertTrue(all(OFFICIAL_DOWNLOAD_PATH not in url for _, url in methods_and_urls))

    async def test_fetch_bytes_rejects_official_download_url(self):
        from bot.suno_audio import fetch_bytes

        session = _FakeSession({})
        with self.assertRaisesRegex(RuntimeError, "Official Suno download API"):
            await fetch_bytes(
                session,
                f"https://studio-api.prod.suno.com{OFFICIAL_DOWNLOAD_PATH}/{SONG_UUID}",
            )


class SongripperTemplateTests(unittest.TestCase):
    def test_songripper_does_not_use_legacy_cdn_or_official_download(self):
        source = (
            Path(__file__).resolve().parents[1] / "web/templates/songripper.html"
        ).read_text(encoding="utf-8")
        self.assertIn("suno_playback.js", source)
        self.assertIn("SunoPlayback.load", source)
        self.assertNotRegex(source, r"cdn1\.suno\.ai/['\"]?\s*\+.*\.(?:mp3|m4a)")
        self.assertNotIn("/api/download/clip", source)


if __name__ == "__main__":
    unittest.main()
