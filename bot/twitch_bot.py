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
    chat:read         — required for IRC command listening.
    moderator:read:followers and channel:read:subscriptions are used by the
                        optional Twitch EventSub chat alerts.

Setup expectation: the bot account must be **moderator** (or VIP) in the
broadcaster's channel — easiest done by the broadcaster typing
`/mod <bot_login>` once in their own chat.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

import aiohttp

from bot.live_log import log_event as _log


_TOKEN_URL    = "https://id.twitch.tv/oauth2/token"
_VALIDATE_URL = "https://id.twitch.tv/oauth2/validate"
_USERS_URL    = "https://api.twitch.tv/helix/users"
_CHAT_URL     = "https://api.twitch.tv/helix/chat/messages"
_IRC_URL      = "wss://irc-ws.chat.twitch.tv:443"

# How early (in seconds) to proactively refresh the access token before its
# stated expiry. Twitch tokens last ~4h, 5 minutes safety is plenty.
_REFRESH_LEAD_TIME = 300

# Parse: @tag=val;tag2=val2 :nick!nick@nick.tmi.twitch.tv PRIVMSG #channel :message
_PRIVMSG_RE = re.compile(
    r"^(?:@(?P<tags>[^ ]+) )?:(?P<user>[^!]+)![^ ]+ PRIVMSG #(?P<channel>[^ ]+) :(?P<text>.+)$"
)
_TAG_RE = re.compile(r"([^;=]+)=([^;]*)")


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

    def __init__(self, db, key_prefix: str = "radio_twitch"):
        self.db = db
        self.SETTING_KEYS = {
            "client_id":         f"{key_prefix}_client_id",
            "client_secret":     f"{key_prefix}_client_secret",
            "refresh_token":     f"{key_prefix}_refresh_token",
            "broadcaster_login": f"{key_prefix}_broadcaster_login",
            "bot_login":         f"{key_prefix}_bot_login",
            "bot_user_id":       f"{key_prefix}_bot_user_id",
            "broadcaster_id":    f"{key_prefix}_broadcaster_user_id",
        }
        self._access_token: Optional[str] = None
        self._access_expires_at: float = 0.0
        self._bot_user_id: Optional[str] = None
        self._broadcaster_user_id: Optional[str] = None
        self._client_id: Optional[str] = None
        self._command_handlers: Dict[str, Callable[[dict], Awaitable[None]]] = {}
        self._lock = asyncio.Lock()
        self._irc_task: Optional[asyncio.Task] = None
        self._irc_running: bool = False

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
        _log(f"Connected: bot_id={self._bot_user_id} broadcaster_id={self._broadcaster_user_id}", "info", "[twitch]")
        return True, "Connected"

    async def stop(self) -> None:
        self._access_token = None
        self._access_expires_at = 0.0
        await self.stop_listener()

    # ------------------------------------------------------------------
    # IRC WebSocket listener (chat command reading)
    # ------------------------------------------------------------------
    async def start_listener(self) -> None:
        """Start the background IRC WebSocket task that reads chat messages
        and dispatches registered !commands. Safe to call multiple times."""
        if self._irc_task and not self._irc_task.done():
            return
        self._irc_running = True
        self._irc_task = asyncio.create_task(self._irc_loop())

    async def stop_listener(self) -> None:
        """Cancel the IRC listener task gracefully."""
        self._irc_running = False
        if self._irc_task and not self._irc_task.done():
            self._irc_task.cancel()
            try:
                await self._irc_task
            except asyncio.CancelledError:
                pass
        self._irc_task = None

    async def _irc_loop(self) -> None:
        """Persistent IRC WebSocket loop with automatic reconnect."""
        backoff = 2.0
        while self._irc_running:
            try:
                await self._irc_session()
                backoff = 2.0
            except asyncio.CancelledError:
                break
            except Exception as e:
                _log(f"IRC connection error: {e} — reconnecting in {backoff:.0f}s", "error", "[twitch-irc]")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120.0)

    async def _irc_session(self) -> None:
        """Single IRC WebSocket session: authenticate, join channel, read messages."""
        if not await self._ensure_token():
            _log("IRC: no valid token, skipping connect", "error", "[twitch-irc]")
            await asyncio.sleep(30)
            return

        # ── Scope check ──────────────────────────────────────────────
        try:
            async with aiohttp.ClientSession() as _s:
                async with _s.get(
                    _VALIDATE_URL,
                    headers={"Authorization": f"OAuth {self._access_token}"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as _r:
                    _v = await _r.json()
                    _scopes = _v.get("scopes") or []
                    _login  = _v.get("login", "?")
                    _log(f"Token owner: {_login} | scopes: {_scopes}", "info", "[twitch-irc]")
                    if "chat:read" not in _scopes:
                        _log(
                            "⚠️ MISSING SCOPE: chat:read — re-authorize the bot in "
                            "Exp. Radio → Twitch settings and add the chat:read scope.",
                            "error", "[twitch-irc]")
                        await asyncio.sleep(60)
                        return
        except Exception as _e:
            _log(f"Scope check error (non-fatal): {_e}", "error", "[twitch-irc]")

        broadcaster_login = _normalize_login(
            await self.db.get_setting(self.SETTING_KEYS["broadcaster_login"]) or ""
        )
        bot_login = await self.db.get_setting(self.SETTING_KEYS["bot_login"]) or ""
        if not broadcaster_login or not bot_login:
            _log("IRC: broadcaster_login / bot_login not configured, skipping", "error", "[twitch-irc]")
            await asyncio.sleep(30)
            return

        _log(f"IRC connecting as {bot_login} → #{broadcaster_login}", "info", "[twitch-irc]")
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                _IRC_URL,
                heartbeat=120,
                timeout=aiohttp.ClientWSTimeout(ws_close=10),
            ) as ws:
                await ws.send_str(f"PASS oauth:{self._access_token}")
                await ws.send_str(f"NICK {bot_login}")
                await ws.send_str("CAP REQ :twitch.tv/tags twitch.tv/commands twitch.tv/membership")
                await ws.send_str(f"JOIN #{broadcaster_login}")
                _log(f"Joined #{broadcaster_login} — listening for commands", "info", "[twitch-irc]")

                async for msg in ws:
                    if not self._irc_running:
                        break
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        for line in msg.data.strip().split("\r\n"):
                            await self._handle_irc_line(line, ws)
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        _log(f"IRC WebSocket closed/error: {msg.type}", "error", "[twitch-irc]")
                        break

    async def _handle_irc_line(self, line: str, ws) -> None:
        """Parse one IRC line and dispatch commands."""
        if line.startswith("PING"):
            pong = "PONG" + line[4:]
            await ws.send_str(pong)
            return

        m = _PRIVMSG_RE.match(line)
        if not m:
            return

        tags_raw = m.group("tags") or ""
        tags: Dict[str, str] = dict(_TAG_RE.findall(tags_raw))
        username    = tags.get("display-name") or m.group("user")
        user_id     = tags.get("user-id") or ""
        text        = m.group("text").strip()
        is_mod      = tags.get("mod") == "1"
        is_sub      = tags.get("subscriber") == "1"
        is_vip      = "vip" in (tags.get("badges") or "")
        is_broadcaster = "broadcaster" in (tags.get("badges") or "")

        if not text.startswith("!"):
            return

        parts   = text.split(None, 1)
        cmd     = parts[0].lstrip("!").lower()
        args    = parts[1] if len(parts) > 1 else ""

        _log(f"Command !{cmd} from {username}", "info", "[twitch-irc]")

        handler = self._command_handlers.get(cmd)
        if not handler:
            await self._dispatch_custom_command(cmd)
            return

        context = {
            "username":       username,
            "user_id":        user_id,
            "text":           text,
            "args":           args,
            "is_mod":         is_mod,
            "is_sub":         is_sub,
            "is_vip":         is_vip,
            "is_broadcaster": is_broadcaster,
            "tags":           tags,
        }
        asyncio.create_task(self._dispatch(handler, context))

    async def _dispatch_custom_command(self, command: str) -> None:
        getter = getattr(self.db, "relic_get_custom_command", None)
        if not getter:
            return
        try:
            custom = await getter(command)
            if not custom or not custom.get("enabled"):
                return
            response = (custom.get("response") or "").strip()
            if response:
                await self.send(response)
        except Exception as e:
            _log(f"Custom command error: {e}", "error", "[twitch-irc]")

    async def _dispatch(self, handler, context: dict) -> None:
        try:
            await handler(context)
        except Exception as e:
            _log(f"Command handler error: {e}", "error", "[twitch-irc]")

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
                        _log(f"Send failed {r.status}: {body[:300]}", "error", "[twitch]")
                        return False
                    return True
        except Exception as e:
            _log(f"Send error: {e}", "error", "[twitch]")
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
                            _log("Refresh token rotated and saved.", "info", "[twitch]")
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
