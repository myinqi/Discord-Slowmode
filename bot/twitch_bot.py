"""Twitch chat bot with auto-refreshing OAuth and Helix-based posting.

Uses Twitch's modern Helix `Send Chat Message` endpoint instead of legacy IRC,
so the access token is refreshed automatically from a long-lived refresh token.

Designed to be extended later — `register_command()` is a hook for future
chat-command handlers (e.g. !song, !skip). For now we only post messages.

Scopes required on the bot user's token:
    user:write:chat   — required to call Helix Send-Chat-Message
    user:bot          — marks the user as a bot so Twitch allows it to post
                        in channels where it is a moderator / VIP (or where
                        the broadcaster has granted `channel:bot`).

Setup expectation: the bot account must be **moderator** (or VIP) in the
broadcaster's channel — easiest done by the broadcaster typing
`/mod <bot_login>` once in their own chat.
"""

from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, Dict, Optional, Tuple

import aiohttp


_TOKEN_URL = "https://id.twitch.tv/oauth2/token"
_VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"
_USERS_URL = "https://api.twitch.tv/helix/users"
_CHAT_URL = "https://api.twitch.tv/helix/chat/messages"

# How early (in seconds) to proactively refresh the access token before its
# stated expiry. Twitch tokens last ~4h, 5 minutes safety is plenty.
_REFRESH_LEAD_TIME = 300


def _normalize_login(value: str) -> str:
    """Accept 'name', '#name', or 'https://twitch.tv/name' → return 'name'."""
    v = (value or "").strip().rstrip("/").lstrip("#").lower()
    if "twitch.tv/" in v:
        v = v.split("twitch.tv/", 1)[1].split("/")[0]
    return v


class TwitchBot:
    """Lightweight Twitch chat poster + (later) command listener.

    All credentials live in the `settings` table under keys prefixed with
    ``radio_twitch_``. The bot reads them on demand so changes in the admin
    UI take effect on the next operation without restart.
    """

    SETTING_KEYS = {
        "client_id":          "radio_twitch_client_id",
        "client_secret":      "radio_twitch_client_secret",
        "refresh_token":      "radio_twitch_refresh_token",
        "broadcaster_login":  "radio_twitch_broadcaster_login",
        # Resolved values cached in DB so the UI can show them and we don't
        # re-resolve on every send.
        "bot_login":          "radio_twitch_bot_login",
        "bot_user_id":        "radio_twitch_bot_user_id",
        "broadcaster_id":     "radio_twitch_broadcaster_user_id",
    }

    def __init__(self, db):
        self.db = db
        self._access_token: Optional[str] = None
        self._access_expires_at: float = 0.0
        self._bot_user_id: Optional[str] = None
        self._broadcaster_user_id: Optional[str] = None
        self._client_id: Optional[str] = None
        self._command_handlers: Dict[str, Callable[[dict], Awaitable[None]]] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API used by stream_manager.py
    # ------------------------------------------------------------------
    async def start(self) -> Tuple[bool, str]:
        """Validate credentials and resolve user IDs. Returns (ok, message)."""
        ok, msg = await self._refresh_access_token()
        if not ok:
            return False, msg
        ok, msg = await self._resolve_user_ids()
        if not ok:
            return False, msg
        print(f"[twitch] Connected as bot_id={self._bot_user_id} → "
              f"broadcaster_id={self._broadcaster_user_id}")
        return True, "Connected"

    async def stop(self) -> None:
        # Nothing persistent yet (HTTP-only). When IRC command-listener is
        # added, cancel its task here.
        self._access_token = None
        self._access_expires_at = 0.0

    async def send(self, message: str) -> bool:
        """Post a chat message in the broadcaster's channel.

        Auto-refreshes the access token and re-resolves user IDs as needed.
        Returns True on success.
        """
        if not message:
            return False
        # Twitch hard-limits chat messages to 500 chars.
        message = message[:500]
        if not await self._ensure_token():
            return False
        if not self._broadcaster_user_id or not self._bot_user_id:
            ok, _ = await self._resolve_user_ids()
            if not ok:
                return False
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    _CHAT_URL,
                    headers={
                        "Authorization": f"Bearer {self._access_token}",
                        "Client-Id": self._client_id or "",
                        "Content-Type": "application/json",
                    },
                    json={
                        "broadcaster_id": self._broadcaster_user_id,
                        "sender_id":      self._bot_user_id,
                        "message":        message,
                    },
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as r:
                    if r.status == 401:
                        # Token may have been revoked mid-stream — try a
                        # one-shot refresh and retry once.
                        self._access_token = None
                        self._access_expires_at = 0.0
                        if await self._ensure_token():
                            return await self.send(message)
                        return False
                    if r.status >= 300:
                        body = await r.text()
                        print(f"[twitch] send failed {r.status}: {body[:300]}")
                        return False
                    return True
        except Exception as e:
            print(f"[twitch] send error: {e}")
            return False

    def register_command(self, name: str,
                         handler: Callable[[dict], Awaitable[None]]) -> None:
        """Future hook — wires `!<name>` to a coroutine. No-op until the IRC
        listener is implemented."""
        self._command_handlers[name.lstrip("!").lower()] = handler

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------
    async def _refresh_access_token(self) -> Tuple[bool, str]:
        async with self._lock:
            client_id = await self.db.get_setting(self.SETTING_KEYS["client_id"])
            client_secret = await self.db.get_setting(self.SETTING_KEYS["client_secret"])
            refresh_token = await self.db.get_setting(self.SETTING_KEYS["refresh_token"])
            if not (client_id and client_secret and refresh_token):
                return False, "Missing client_id / client_secret / refresh_token"
            self._client_id = client_id
            try:
                async with aiohttp.ClientSession() as s:
                    async with s.post(_TOKEN_URL, data={
                        "client_id":     client_id,
                        "client_secret": client_secret,
                        "grant_type":    "refresh_token",
                        "refresh_token": refresh_token,
                    }, timeout=aiohttp.ClientTimeout(total=10)) as r:
                        data = await r.json()
                        if r.status != 200 or "access_token" not in data:
                            err = data.get("message") or str(data)
                            return False, f"Token refresh failed: {err}"
                        self._access_token = data["access_token"]
                        self._access_expires_at = (
                            time.monotonic()
                            + int(data.get("expires_in", 14400))
                            - _REFRESH_LEAD_TIME
                        )
                        # Twitch may rotate the refresh token — persist it.
                        new_rt = data.get("refresh_token")
                        if new_rt and new_rt != refresh_token:
                            await self.db.set_setting(
                                self.SETTING_KEYS["refresh_token"], new_rt)
                            print("[twitch] Refresh token rotated, saved.")
                        return True, "Token refreshed"
            except Exception as e:
                return False, f"Token refresh error: {e}"

    async def _ensure_token(self) -> bool:
        if self._access_token and time.monotonic() < self._access_expires_at:
            return True
        ok, _msg = await self._refresh_access_token()
        return ok

    # ------------------------------------------------------------------
    # User-ID resolution
    # ------------------------------------------------------------------
    async def _resolve_user_ids(self) -> Tuple[bool, str]:
        if not self._access_token or not self._client_id:
            return False, "No access token yet"
        broadcaster_login = _normalize_login(
            await self.db.get_setting(self.SETTING_KEYS["broadcaster_login"]) or ""
        )
        if not broadcaster_login:
            return False, "broadcaster_login not configured"
        try:
            async with aiohttp.ClientSession() as s:
                hdr = {
                    "Authorization": f"Bearer {self._access_token}",
                    "Client-Id":     self._client_id,
                }
                # Bot's own user (no params → authenticated user)
                async with s.get(_USERS_URL, headers=hdr,
                                 timeout=aiohttp.ClientTimeout(total=10)) as r:
                    d = await r.json()
                    if r.status != 200 or not d.get("data"):
                        return False, f"Could not resolve bot user: {d}"
                    me = d["data"][0]
                    self._bot_user_id = me["id"]
                    bot_login = me.get("login", "")
                    await self.db.set_setting(self.SETTING_KEYS["bot_login"], bot_login)
                    await self.db.set_setting(self.SETTING_KEYS["bot_user_id"], self._bot_user_id)
                # Broadcaster's user
                async with s.get(_USERS_URL, headers=hdr,
                                 params={"login": broadcaster_login},
                                 timeout=aiohttp.ClientTimeout(total=10)) as r:
                    d = await r.json()
                    if r.status != 200 or not d.get("data"):
                        return False, f"Could not resolve broadcaster '{broadcaster_login}': {d}"
                    self._broadcaster_user_id = d["data"][0]["id"]
                    await self.db.set_setting(
                        self.SETTING_KEYS["broadcaster_id"], self._broadcaster_user_id)
            return True, "Resolved"
        except Exception as e:
            return False, f"User resolve error: {e}"

    # ------------------------------------------------------------------
    # Diagnostics — used by the admin UI's "Test Connection" button.
    # ------------------------------------------------------------------
    async def diagnose(self) -> dict:
        """One-shot health check. Returns a dict with details for the UI."""
        out = {
            "ok": False,
            "message": "",
            "bot_login": None,
            "broadcaster_login": None,
            "scopes": [],
        }
        ok, msg = await self._refresh_access_token()
        if not ok:
            out["message"] = msg
            return out
        # Validate to surface scopes / login
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(_VALIDATE_URL,
                                 headers={"Authorization": f"OAuth {self._access_token}"},
                                 timeout=aiohttp.ClientTimeout(total=10)) as r:
                    v = await r.json()
                    if r.status != 200:
                        out["message"] = f"Token validation failed: {v}"
                        return out
                    out["bot_login"] = v.get("login")
                    out["scopes"] = v.get("scopes") or []
        except Exception as e:
            out["message"] = f"Validate error: {e}"
            return out
        ok, msg = await self._resolve_user_ids()
        if not ok:
            out["message"] = msg
            return out
        out["broadcaster_login"] = _normalize_login(
            await self.db.get_setting(self.SETTING_KEYS["broadcaster_login"]) or ""
        )
        # Verify required scopes
        required = {"user:write:chat", "user:bot"}
        missing = required - set(out["scopes"])
        if missing:
            out["message"] = f"Missing scopes: {', '.join(sorted(missing))}"
            return out
        out["ok"] = True
        out["message"] = "All checks passed."
        return out
