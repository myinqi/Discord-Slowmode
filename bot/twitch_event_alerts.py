"""Twitch EventSub chat announcements.

Listens to Twitch EventSub over WebSockets and posts configurable chat messages
through the existing Helix chat sender used by Experimental Radio and Raven's
Nest.  All settings live in the shared settings table under ``twitch_alerts_*``.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import Any, Dict, Optional

import aiohttp

from bot.live_log import log_event as _log
from bot.twitch_bot import TwitchBot


_EVENTSUB_WS_URL = "wss://eventsub.wss.twitch.tv/ws"
_EVENTSUB_SUBS_URL = "https://api.twitch.tv/helix/eventsub/subscriptions"


DEFAULT_ALERT_SETTINGS = {
    "twitch_alerts_enabled": "off",
    "twitch_alerts_follow_enabled": "on",
    "twitch_alerts_follow_template": "💜 New follower: {user}! Welcome to the raven circle.",
    "twitch_alerts_sub_enabled": "on",
    "twitch_alerts_sub_template": "🌙 {user} subscribed ({tier})! Thank you for the support.",
    "twitch_alerts_resub_enabled": "on",
    "twitch_alerts_resub_template": "🌙 {user} subscribed for {months} month(s) ({tier})! Welcome back.",
    "twitch_alerts_gift_enabled": "on",
    "twitch_alerts_gift_template": "🎁 {gifter} gifted {total} sub(s) ({tier})!",
    "twitch_alerts_cheer_enabled": "on",
    "twitch_alerts_cheer_template": "✨ {user} cheered {bits} Bits! Thank you for the sparkle.",
    "twitch_alerts_raid_enabled": "on",
    "twitch_alerts_raid_template": "⚔️ Raid incoming from {user} with {viewers} viewer(s)! Welcome raiders!",
    "twitch_alerts_watch_streak_enabled": "on",
    "twitch_alerts_watch_streak_template": "🔥 {user} reached a watch streak of {streak} streams!",
}


class _SafeFormatDict(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class _ReconnectEventSub(Exception):
    def __init__(self, url: str):
        self.url = url
        super().__init__(url)


@dataclass
class AlertStatus:
    enabled: bool = False
    running: bool = False
    connected: bool = False
    last_message: str = "Not started"
    session_id: str = ""


def _tier_label(tier: Any) -> str:
    return {
        "1000": "Tier 1",
        "2000": "Tier 2",
        "3000": "Tier 3",
        "Prime": "Prime",
    }.get(str(tier or ""), str(tier or "sub"))


class TwitchEventAlerts:
    def __init__(self, db):
        self.db = db
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._ws = None
        self.status = AlertStatus()
        self._seen_message_ids: set[str] = set()

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._running = True
        self.status.enabled = await self._enabled()
        self.status.running = True
        self.status.connected = False
        self.status.last_message = "Starting…" if self.status.enabled else "Disabled"
        self._task = asyncio.create_task(self._loop())
        self._task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task: asyncio.Task) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is None:
            return
        self.status.running = False
        self.status.connected = False
        self.status.last_message = f"Listener stopped: {exc}"
        _log(self.status.last_message, "error", "[twitch-alerts]")

    async def stop(self) -> None:
        self._running = False
        self.status.running = False
        self.status.connected = False
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()
        if self._task and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    async def _setting(self, key: str) -> str:
        return await self.db.get_setting(key) or DEFAULT_ALERT_SETTINGS.get(key, "")

    async def _enabled(self) -> bool:
        return (await self._setting("twitch_alerts_enabled")) == "on"

    async def _any_event_enabled(self) -> bool:
        for key in (
            "twitch_alerts_follow_enabled",
            "twitch_alerts_sub_enabled",
            "twitch_alerts_resub_enabled",
            "twitch_alerts_gift_enabled",
            "twitch_alerts_cheer_enabled",
            "twitch_alerts_raid_enabled",
            "twitch_alerts_watch_streak_enabled",
        ):
            if await self._setting(key) == "on":
                return True
        return False

    async def _loop(self) -> None:
        backoff = 2.0
        next_url = _EVENTSUB_WS_URL
        while self._running:
            self.status.enabled = await self._enabled()
            if not self.status.enabled:
                self.status.connected = False
                self.status.last_message = "Disabled"
                await asyncio.sleep(5)
                next_url = _EVENTSUB_WS_URL
                continue
            if not await self._any_event_enabled():
                self.status.connected = False
                self.status.last_message = "Enabled, but no event type is active"
                await asyncio.sleep(10)
                next_url = _EVENTSUB_WS_URL
                continue

            try:
                await self._connect_once(next_url)
                backoff = 2.0
                next_url = _EVENTSUB_WS_URL
            except _ReconnectEventSub as exc:
                next_url = exc.url or _EVENTSUB_WS_URL
                self.status.last_message = "Reconnecting to Twitch EventSub"
                _log("EventSub reconnect requested by Twitch", "info", "[twitch-alerts]")
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.status.connected = False
                self.status.last_message = f"Connection error: {exc}"
                _log(f"EventSub error: {exc} — reconnecting in {backoff:.0f}s", "error", "[twitch-alerts]")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120.0)
                next_url = _EVENTSUB_WS_URL

    async def _connect_once(self, url: str) -> None:
        chat_bot = TwitchBot(self.db, key_prefix="exp_radio_twitch")
        ok, msg = await chat_bot.start()
        if not ok:
            self.status.last_message = f"Twitch chat credentials error: {msg}"
            await asyncio.sleep(30)
            return

        eventsub_bot = TwitchBot(self.db, key_prefix="twitch_alerts_eventsub")
        eventsub_ok, eventsub_msg = await eventsub_bot.start()
        if not eventsub_ok:
            # Keep Follow/Raid usable while installations migrate to the
            # separate broadcaster authorization. Broadcaster-only events
            # will be rejected by Twitch until that authorization exists.
            eventsub_bot = chat_bot
            _log(
                f"Broadcaster EventSub authorization unavailable ({eventsub_msg}); "
                "using chat token as fallback",
                "error",
                "[twitch-alerts]",
            )

        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                url,
                heartbeat=120,
                timeout=aiohttp.ClientWSTimeout(ws_close=10),
            ) as ws:
                self._ws = ws
                self.status.connected = True
                self.status.last_message = "Connected to Twitch EventSub"
                _log("Connected to Twitch EventSub", "info", "[twitch-alerts]")

                async for msg in ws:
                    if not self._running:
                        break
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await self._handle_frame(chat_bot, eventsub_bot, session, msg.json())
                    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
        self._ws = None
        self.status.connected = False

    async def _handle_frame(
        self,
        chat_bot: TwitchBot,
        eventsub_bot: TwitchBot,
        session: aiohttp.ClientSession,
        data: Dict[str, Any],
    ) -> None:
        metadata = data.get("metadata") or {}
        payload = data.get("payload") or {}
        message_type = metadata.get("message_type")
        message_id = metadata.get("message_id") or ""

        if message_id:
            if message_id in self._seen_message_ids:
                return
            self._seen_message_ids.add(message_id)
            if len(self._seen_message_ids) > 2000:
                self._seen_message_ids = set(list(self._seen_message_ids)[-1000:])

        if message_type == "session_welcome":
            session_id = ((payload.get("session") or {}).get("id") or "")
            self.status.session_id = session_id
            await self._subscribe_enabled_events(eventsub_bot, session, session_id)
            return

        if message_type == "session_reconnect":
            reconnect_url = ((payload.get("session") or {}).get("reconnect_url") or "")
            raise _ReconnectEventSub(reconnect_url)

        if message_type == "notification":
            sub = payload.get("subscription") or {}
            event = payload.get("event") or {}
            await self._announce(chat_bot, sub.get("type") or "", event)
            return

        if message_type == "revocation":
            sub = payload.get("subscription") or {}
            self.status.last_message = f"Subscription revoked: {sub.get('type')} ({sub.get('status')})"
            _log(self.status.last_message, "error", "[twitch-alerts]")

    async def _subscribe_enabled_events(
        self,
        bot: TwitchBot,
        session: aiohttp.ClientSession,
        session_id: str,
    ) -> None:
        broadcaster_id = bot._broadcaster_user_id or ""
        bot_user_id = bot._bot_user_id or broadcaster_id
        if not (bot._access_token and bot._client_id and broadcaster_id and session_id):
            self.status.last_message = "Cannot subscribe: missing token/user IDs"
            return

        subscriptions = []
        if await self._setting("twitch_alerts_follow_enabled") == "on":
            subscriptions.append((
                "channel.follow",
                "2",
                {"broadcaster_user_id": broadcaster_id, "moderator_user_id": bot_user_id},
            ))
        if await self._setting("twitch_alerts_sub_enabled") == "on":
            subscriptions.append(("channel.subscribe", "1", {"broadcaster_user_id": broadcaster_id}))
        if await self._setting("twitch_alerts_resub_enabled") == "on":
            subscriptions.append(("channel.subscription.message", "1", {"broadcaster_user_id": broadcaster_id}))
        if await self._setting("twitch_alerts_gift_enabled") == "on":
            subscriptions.append(("channel.subscription.gift", "1", {"broadcaster_user_id": broadcaster_id}))
        if await self._setting("twitch_alerts_cheer_enabled") == "on":
            subscriptions.append(("channel.cheer", "1", {"broadcaster_user_id": broadcaster_id}))
        if await self._setting("twitch_alerts_raid_enabled") == "on":
            subscriptions.append(("channel.raid", "1", {"to_broadcaster_user_id": broadcaster_id}))
        if await self._setting("twitch_alerts_watch_streak_enabled") == "on":
            subscriptions.append((
                "channel.chat.notification",
                "1",
                {"broadcaster_user_id": broadcaster_id, "user_id": bot_user_id},
            ))

        created = 0
        failed = []
        for sub_type, version, condition in subscriptions:
            payload = {
                "type": sub_type,
                "version": version,
                "condition": condition,
                "transport": {"method": "websocket", "session_id": session_id},
            }
            async with session.post(
                _EVENTSUB_SUBS_URL,
                headers={
                    "Authorization": f"Bearer {bot._access_token}",
                    "Client-Id": bot._client_id,
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status in (200, 202):
                    created += 1
                    continue
                body = await resp.text()
                failed.append(sub_type)
                _log(f"Could not subscribe to {sub_type}: HTTP {resp.status} {body[:300]}", "error", "[twitch-alerts]")
        self.status.last_message = f"Subscribed to {created}/{len(subscriptions)} Twitch event(s)"
        if failed:
            self.status.last_message += f"; failed: {', '.join(failed)}"
        _log(self.status.last_message, "info", "[twitch-alerts]")

    async def _announce(self, bot: TwitchBot, sub_type: str, event: Dict[str, Any]) -> None:
        template_key = ""
        values: Dict[str, Any] = {}

        if sub_type == "channel.follow":
            template_key = "twitch_alerts_follow_template"
            values = {
                "user": event.get("user_name") or event.get("user_login") or "Someone",
                "login": event.get("user_login") or "",
            }
        elif sub_type == "channel.subscribe":
            template_key = "twitch_alerts_sub_template"
            values = {
                "user": event.get("user_name") or event.get("user_login") or "Someone",
                "login": event.get("user_login") or "",
                "tier": _tier_label(event.get("tier")),
                "is_gift": str(bool(event.get("is_gift"))).lower(),
            }
        elif sub_type == "channel.subscription.message":
            template_key = "twitch_alerts_resub_template"
            values = {
                "user": event.get("user_name") or event.get("user_login") or "Someone",
                "login": event.get("user_login") or "",
                "tier": _tier_label(event.get("tier")),
                "months": event.get("cumulative_months") or event.get("duration_months") or "",
                "streak_months": event.get("streak_months") or "",
                "message": ((event.get("message") or {}).get("text") if isinstance(event.get("message"), dict) else event.get("message")) or "",
            }
        elif sub_type == "channel.subscription.gift":
            template_key = "twitch_alerts_gift_template"
            anonymous = bool(event.get("is_anonymous"))
            values = {
                "gifter": "An anonymous raven" if anonymous else (event.get("user_name") or event.get("user_login") or "Someone"),
                "login": "" if anonymous else (event.get("user_login") or ""),
                "tier": _tier_label(event.get("tier")),
                "total": event.get("total") or 1,
                "cumulative_total": event.get("cumulative_total") or "",
            }
        elif sub_type == "channel.cheer":
            template_key = "twitch_alerts_cheer_template"
            anonymous = bool(event.get("is_anonymous"))
            values = {
                "user": "An anonymous raven" if anonymous else (event.get("user_name") or event.get("user_login") or "Someone"),
                "login": "" if anonymous else (event.get("user_login") or ""),
                "bits": event.get("bits") or 0,
                "message": event.get("message") or "",
            }
        elif sub_type == "channel.raid":
            template_key = "twitch_alerts_raid_template"
            values = {
                "user": event.get("from_broadcaster_user_name") or event.get("from_broadcaster_user_login") or "Someone",
                "login": event.get("from_broadcaster_user_login") or "",
                "viewers": event.get("viewers") or 0,
            }
        elif sub_type == "channel.chat.notification":
            if event.get("notice_type") != "watch_streak":
                return
            watch_streak = event.get("watch_streak") or {}
            template_key = "twitch_alerts_watch_streak_template"
            values = {
                "user": event.get("chatter_user_name") or event.get("chatter_user_login") or "Someone",
                "login": event.get("chatter_user_login") or "",
                "streak": watch_streak.get("streak_count") or 0,
                "points": watch_streak.get("channel_points_awarded") or 0,
            }
        else:
            return

        template = await self._setting(template_key)
        message = template.format_map(_SafeFormatDict(values)).strip()
        if not message:
            return
        sent = await bot.send(message)
        self.status.last_message = f"Posted {sub_type} alert" if sent else f"Failed to post {sub_type} alert"
