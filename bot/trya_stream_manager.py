"""TrYa Stream – FFmpeg stream manager.

Pipeline per full playlist rotation (no gap between songs):
  One FFmpeg job covers all songs using concat:
    - Audio:     per-song MP3 inputs concat'd in filtergraph
    - Covers:    each song's image/video as a trimmed input, concat'd in filtergraph
    - Background + loop overlay: loop for total duration
    - ASS:       all subtitle files merged with time offsets + NowPlaying title cards

The stream only briefly disconnects once per full playlist loop (not between songs).
"""

import asyncio
import hashlib
import json
import os
import re
import random
import time
from collections import deque

from bot.trya_stream_files import (
    cleanup_trya_stream_song_files,
    trya_stream_hook_cache_path,
)
from bot.twitch_bot import TwitchBot

_RTMP_BASE  = "rtmps://live.twitch.tv:443/app/"
_LEGACY_RTMP_BASE = "rtmp://live.twitch.tv/app/"
_FPS        = 30
_DEFAULT_OBS_OVERLAY_FPS = 20
_W, _H      = 1920, 1080
_INSET_W    = 480
_INSET_H    = 480
_LOOP_OVERLAY_W = 650
_LOOP_OVERLAY_H = 366
_OBS_BRIDGE_W = 480
_OBS_BRIDGE_H = 270
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def _rounded_rect_condition(radius: int, inset: int = 0) -> str:
    """FFmpeg expression that is true inside an inset rounded rectangle."""
    radius = max(1, int(radius))
    inset = max(0, int(inset))
    inner_radius = max(1, radius - inset)
    half_w = f"W/2-{inset}" if inset else "W/2"
    half_h = f"H/2-{inset}" if inset else "H/2"
    return (
        "lte(hypot("
        f"max(abs(X-W/2)-({half_w}-{inner_radius}),0),"
        f"max(abs(Y-H/2)-({half_h}-{inner_radius}),0)"
        f"),{inner_radius})"
    )


def _rounded_media_filters(
    input_label: str,
    output_label: str,
    *,
    label_prefix: str,
    width: int,
    height: int,
    fps: int,
    radius: int,
    border_enabled: bool,
    border_width: int,
    border_color: str,
    preserve_input_alpha: bool = False,
) -> list[str]:
    """Apply a cached rounded mask and an optional low-cost inward border."""
    color = (border_color or "#A855F7").strip().lstrip("#")
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", color):
        color = "A855F7"
    outer = _rounded_rect_condition(radius)
    mask = f"{label_prefix}_outer_mask"
    filters = [
        f"color=c=white:s={width}x{height}:r={fps},format=gray,"
        f"geq=lum='if({outer},255,0)',trim=end_frame=1,"
        f"loop=loop=-1:size=1:start=0,setpts=N/({fps}*TB)[{mask}]"
    ]

    visual = input_label
    if border_enabled and border_width > 0:
        visual = f"[{label_prefix}_bordered]"
        filters.append(
            f"{input_label}drawbox=x=0:y=0:w=iw:h=ih:"
            f"color=0x{color}:t={border_width}{visual}"
        )

    if preserve_input_alpha:
        # The OBS bridge is transparent while no live override is present.
        # Multiply its alpha by the rounded mask so styling cannot make an
        # absent source visible. drawbox keeps this path to one composition.
        filters.extend([
            f"{visual}format=rgba,split[{label_prefix}_visual][{label_prefix}_alpha_src]",
            f"[{label_prefix}_alpha_src]alphaextract[{label_prefix}_source_alpha]",
            f"[{mask}][{label_prefix}_source_alpha]blend=all_mode=multiply[{label_prefix}_alpha]",
            f"[{label_prefix}_visual][{label_prefix}_alpha]alphamerge{output_label}",
        ])
        return filters

    filters.append(f"{visual}[{mask}]alphamerge{output_label}")
    return filters

# ── ASS timestamp helpers ──────────────────────────────────────────────────────

def _ts_to_cs(ts: str) -> int:
    """'H:MM:SS.cs' → centiseconds."""
    h, rest = ts.split(":", 1)
    m, rest = rest.split(":", 1)
    s, cs   = rest.split(".")
    return int(h) * 360000 + int(m) * 6000 + int(s) * 100 + int(cs)


def _cs_to_ts(cs: int) -> str:
    """centiseconds → 'H:MM:SS.cs'."""
    cs = max(0, int(cs))
    h  = cs // 360000; cs %= 360000
    m  = cs //   6000; cs %=   6000
    s  = cs //    100; cs %=    100
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _shift_ass_dialogue(content: str, offset_cs: int) -> list:
    """Return Dialogue lines from ASS content shifted by offset_cs centiseconds.

    Also strips emoji codepoints from the text payload — see _strip_emoji.
    """
    out = []
    for line in content.splitlines():
        match = re.match(
            r'(Dialogue:[^,]*,)(\d+:\d+:\d+\.\d+),(\d+:\d+:\d+\.\d+),(.*)',
            line.rstrip(),
        )
        if match:
            start = _cs_to_ts(_ts_to_cs(match.group(2)) + offset_cs)
            end   = _cs_to_ts(_ts_to_cs(match.group(3)) + offset_cs)
            out.append(f"{match.group(1)}{start},{end},{_strip_emoji(match.group(4))}")
    return out


# FFmpeg progress lines ("frame=  123 fps= 30 q=28 ... time=00:00:04.10 ...")
# emit roughly once per second and would flood the log buffer. We parse them
# for the dashboard health line and drop them from the live log.
_FFMPEG_PROGRESS_RE = re.compile(r"^frame=\s*\d+.*time=")
_FFMPEG_STALL_SECONDS = 30.0
_FFMPEG_PROGRESS_KEYS = {
    "frame", "fps", "stream_0_0_q", "bitrate", "total_size", "out_time_us",
    "out_time_ms", "out_time", "dup_frames", "drop_frames", "speed",
    "progress",
}

# Emoji codepoints have no glyph in any font we ship in the container, so
# libass spams `fontselect: failed to find any fallback with glyph 0xXXXX`
# once per rendered frame. We strip them before they reach the ASS file.
# Range U+1F000–U+1FFFF covers all modern emoji (Misc Symbols/Pictographs,
# Emoticons, Transport, Supplemental Symbols, etc.). U+FE0F is the
# Variation-Selector-16 that follows many emoji — only meaningful with one,
# so safe to drop too. We deliberately keep U+2600–U+27BF (♥ ♪ ✨…) since
# those are used in our title cards and most fonts cover them.
_EMOJI_STRIP_RE = re.compile("[\U0001F000-\U0001FFFF\uFE0F]")

def _strip_emoji(text: str) -> str:
    if not text:
        return text
    return _EMOJI_STRIP_RE.sub("", text)


def _assign_ass_symbol_fonts(text: str) -> str:
    """Avoid repeated libass font searches for supported decorative glyphs."""
    return text.replace("✨", r"{\fnFreeSerif}✨{\r}")


# ── Module-level Live Log ────────────────────────────────────────────────────
# Re-exported from bot.live_log so the rest of this file can keep calling
# log_event() / _LOG_BUFFER unchanged, and other modules (twitch_bot,
# relic_hunt) share the same buffer without a circular import.
from bot.trya_live_log import _LOG_BUFFER, log_event as _live_log_event

_LOG_BUFFER_MAX = 1000

# Module-level flag: True while the trya-stream stream is actively running.
stream_is_live: bool = False

_PRE_START_LOCK_MINUTES = 60
_EARLY_SUBMITTER_LOGIN = "t_ravenveil"
_EARLY_SUBMITTER_WINDOW_SECONDS = 60 * 60
_NOW_PLAYING_CHAT_DELAY = 15.0


def _place_submitter_song_in_early_window(
    songs: list[dict],
    *,
    submitter_login: str,
    initial_offset: float = 0.0,
    window_seconds: float = _EARLY_SUBMITTER_WINDOW_SECONDS,
) -> tuple[list[dict], dict | None, int | None, float | None]:
    """Place one matching submission at a random start inside an early window."""
    normalized_login = (submitter_login or "").strip().lstrip("@").casefold()
    candidates = [
        song for song in songs
        if (song.get("playlist_source") or "submission") == "submission"
        and str(song.get("user_name") or "").strip().lstrip("@").casefold()
        == normalized_login
    ]
    if not candidates or initial_offset >= window_seconds:
        return list(songs), None, None, None

    selected = random.choice(candidates)
    remaining = list(songs)
    remaining.remove(selected)

    valid_slots: list[tuple[int, float]] = []
    starts_at = max(0.0, float(initial_offset or 0.0))
    if starts_at < window_seconds:
        valid_slots.append((0, starts_at))
    for index, song in enumerate(remaining, start=1):
        try:
            duration = max(0.0, float(song.get("duration") or 300.0))
        except (TypeError, ValueError):
            duration = 300.0
        starts_at += duration
        if starts_at < window_seconds:
            valid_slots.append((index, starts_at))
        else:
            break

    if not valid_slots:
        return list(songs), None, None, None

    insert_at, starts_at = random.choice(valid_slots)
    remaining.insert(insert_at, selected)
    return remaining, selected, insert_at, starts_at


async def is_submissions_locked(db) -> tuple[bool, str]:
    """Return (locked, reason_str).

    Locked when:
      • the stream is currently live  →  reason "stream_live"
      • OR the scheduler is enabled, today is a scheduled day, and the
        configured start time is within the next 60 minutes
        →  reason "pre_start_Nmin"
    """
    if stream_is_live:
        return True, "stream_live"

    from datetime import datetime
    from zoneinfo import ZoneInfo
    try:
        enabled = await db.get_setting("trya_stream_schedule_enabled") or "off"
        if enabled != "on":
            return False, ""
        days_csv = await db.get_setting("trya_stream_schedule_days") or ""
        days = {int(d) for d in days_csv.split(",") if d.strip().isdigit()}
        hhmm = (await db.get_setting("trya_stream_schedule_time") or "").strip()
        if not days or not hhmm or ":" not in hhmm:
            return False, ""
        h_str, m_str = hhmm.split(":", 1)
        target_h, target_m = int(h_str), int(m_str)
        now = datetime.now(ZoneInfo("Europe/Berlin"))
        if now.weekday() not in days:
            return False, ""
        diff = (target_h * 60 + target_m) - (now.hour * 60 + now.minute)
        if 0 < diff <= _PRE_START_LOCK_MINUTES:
            return True, f"pre_start_{diff}min"
        return False, ""
    except Exception:
        return False, ""


def log_event(line: str, level: str = "info", prefix: str = "[trya-stream]") -> None:
    _live_log_event(line, level, prefix)


class TryaStreamManager:
    # Ring buffer size — keeps roughly the last 10–15 minutes of activity
    # (the UI then filters by an explicit time window).
    _LOG_BUFFER_MAX = 1000

    def __init__(self, db, trya_stream_dir: str):
        self.db            = db
        self.trya_stream_dir = trya_stream_dir
        self._process      = None
        self._task         = None
        self._start_lock   = asyncio.Lock()
        self.is_running    = False
        self.current_song: dict | None = None
        self.playlist: list[dict]      = []
        self._twitch_key   = ""
        self._twitch_chat: TwitchBot | None = None
        # Safe-stop: when set, the stream stops after the current song ends.
        self._safe_stop_requested: bool = False
        self._current_song_end_time: float = 0.0   # monotonic
        # Outro: song to play once at the very end before the stream stops.
        self._outro_song: dict | None = None
        self._outro_played: bool = False
        self._outro_in_playlist: bool = False
        self._outro_counts_in_progress: bool = False
        self._progress_total_count: int = 0
        self._current_song_index: int = 0
        self._legacy_pipeline: bool = False
        # Set when the first FFmpeg process exists. start() returns earlier,
        # while media is still being prepared.
        self._stream_ready_event = asyncio.Event()
        self._submission_bans_advanced = False
        # Live log: each entry is (unix_ts: float, level: str, line: str).
        # The actual buffer is module-level (see _LOG_BUFFER below) so that
        # background workers and web actions which don't have a reference to
        # the manager instance can still publish into the same live log.
        self._log_buffer = _LOG_BUFFER
        self._ffmpeg_progress: dict = {}
        self._ffmpeg_progress_pending: dict = {}
        self._ffmpeg_last_progress_at: float = 0.0
        self._ffmpeg_health_state: str = "offline"
        self._obs_bridge_task: asyncio.Task | None = None
        self._obs_bridge_proc = None
        self._obs_bridge_path: str | None = None
        self._obs_overlay_status: dict = {
            "enabled": False,
            "mode": "local",
            "state": "disabled",
            "label": "OBS overlay disabled",
        }

    # ── Live log buffer ──────────────────────────────────────────────────────────────────────

    def _log(self, line: str, level: str = "info") -> None:
        """Instance shortcut that forwards to the module-level `log_event`."""
        log_event(line, level, prefix="[trya-stream]")

    def get_log(self, since_ts: float = 0.0, max_age_secs: float = 300.0) -> list[dict]:
        """Return log entries newer than `since_ts` (or within max_age_secs).

        The UI uses `since_ts` for incremental polling and falls back to
        `max_age_secs` on first load."""
        cutoff = max(since_ts, time.time() - max_age_secs)
        return [
            {"ts": ts, "level": level, "line": line}
            for (ts, level, line) in self._log_buffer
            if ts > cutoff
        ]

    # ── FFmpeg health tracking ───────────────────────────────────────────────

    def _reset_ffmpeg_health(self, state: str = "offline") -> None:
        self._ffmpeg_progress = {}
        self._ffmpeg_progress_pending = {}
        self._ffmpeg_last_progress_at = 0.0
        self._ffmpeg_health_state = state

    def _set_obs_overlay_status(self, enabled: bool, state: str, label: str, mode: str = "local") -> None:
        self._obs_overlay_status = {
            "enabled": enabled,
            "mode": mode,
            "state": state,
            "label": label,
        }

    def _set_ffmpeg_health_state(self, state: str, age: float | None = None) -> None:
        old_state = self._ffmpeg_health_state
        if state == old_state:
            return
        self._ffmpeg_health_state = state
        if state == "healthy" and old_state == "stalled":
            self._log("FFmpeg health: recovered.")
        elif state == "healthy" and old_state in ("starting", "unknown"):
            self._log("FFmpeg health: healthy.")
        elif state == "stalled":
            age_label = f"{int(age)}s" if age is not None else "unknown"
            self._log(f"FFmpeg health: stalled, no progress for {age_label}.", "error")

    def _record_ffmpeg_progress(self, progress: dict) -> None:
        if not progress:
            return
        self._ffmpeg_progress.update(progress)
        self._ffmpeg_last_progress_at = time.time()
        self._set_ffmpeg_health_state("healthy", 0.0)

    def _record_ffmpeg_progress_line(self, line: str) -> None:
        progress = {}
        patterns = {
            "frame": r"frame=\s*([0-9]+)",
            "fps": r"fps=\s*([0-9.]+)",
            "bitrate": r"bitrate=\s*([^\s]+)",
            "out_time": r"time=\s*([0-9:.]+)",
            "speed": r"speed=\s*([^\s]+)",
        }
        for key, pattern in patterns.items():
            match = re.search(pattern, line)
            if match:
                progress[key] = match.group(1)
        self._record_ffmpeg_progress(progress)

    def _get_ffmpeg_health(self) -> dict:
        if not self.is_running:
            self._set_ffmpeg_health_state("offline")
            return {"state": "offline", "healthy": False}

        proc_alive = bool(self._process and self._process.returncode is None)
        if not proc_alive:
            self._set_ffmpeg_health_state("starting")
            return {"state": "starting", "healthy": False}

        if not self._ffmpeg_last_progress_at:
            self._set_ffmpeg_health_state("starting")
            return {"state": "starting", "healthy": None}

        age = max(0.0, time.time() - self._ffmpeg_last_progress_at)
        state = "healthy" if age <= _FFMPEG_STALL_SECONDS else "stalled"
        self._set_ffmpeg_health_state(state, age)
        progress = dict(self._ffmpeg_progress)
        return {
            "state": state,
            "healthy": state == "healthy",
            "last_progress_age": round(age, 1),
            "stall_after_seconds": _FFMPEG_STALL_SECONDS,
            "frame": progress.get("frame"),
            "fps": progress.get("fps"),
            "bitrate": progress.get("bitrate"),
            "speed": progress.get("speed"),
            "out_time": progress.get("out_time") or progress.get("time"),
        }

    # ── Public API ─────────────────────────────────────────────────────────────

    async def start(
        self,
        twitch_key: str,
        fresh_cache: bool = False,
        legacy_pipeline: bool = False,
        scheduled: bool = False,
    ) -> dict:
        # is_running is set only after the comparatively long media setup.
        # Serialise that setup so a scheduler and a manual click cannot build
        # the same intermediates concurrently.
        async with self._start_lock:
            return await self._start_locked(
                twitch_key,
                fresh_cache=fresh_cache,
                legacy_pipeline=legacy_pipeline,
                scheduled=scheduled,
            )

    async def _start_locked(
        self,
        twitch_key: str,
        fresh_cache: bool = False,
        legacy_pipeline: bool = False,
        scheduled: bool = False,
    ) -> dict:
        if self.is_running:
            return {"ok": False, "error": "Stream already running."}
        self._legacy_pipeline = legacy_pipeline
        if fresh_cache:
            # Scheduled / forced fresh starts: wipe the per-stream cache so
            # covers are re-downloaded (any cover_url changes upstream are
            # picked up) and the audio concat / combined ASS are rebuilt
            # from scratch. The mp3/ and ass/ directories produced by Whisper
            # are NOT touched — those are expensive and source-of-truth.
            import shutil
            try:
                cover_cache = os.path.join(self.trya_stream_dir, "cover_cache")
                if os.path.isdir(cover_cache):
                    shutil.rmtree(cover_cache, ignore_errors=True)
                for fn in ("_audio_concat.txt", "_combined.ass"):
                    p = os.path.join(self.trya_stream_dir, fn)
                    if os.path.exists(p):
                        try: os.remove(p)
                        except Exception: pass
                self._log("Fresh-cache start: cover cache and intermediates cleared.")
            except Exception as e:
                self._log(f"Fresh-cache cleanup error: {e}", "error")
        active_pl = (await self.db.get_setting("trya_stream_active_playlist")) or "submission"
        pl_source = None if active_pl == "both" else active_pl
        songs = await self.db.get_all_trya_stream_songs(active_only=True, source=pl_source)
        if active_pl == "both":
            songs = [
                song for song in songs
                if (song.get("playlist_source") or "submission") in {"submission", "admin"}
            ]

        async def _restore_missing_mp3(song: dict) -> bool:
            """Regenerate work audio exclusively from immutable original evidence."""
            filename = song.get("mp3_filename")
            if filename and os.path.isfile(
                os.path.join(self.trya_stream_dir, "mp3", filename)
            ):
                return True
            try:
                from bot.trya_stream_worker import regenerate_work_mp3
                restored = await regenerate_work_mp3(
                    self.db, int(song["id"]), self.trya_stream_dir
                )
                song["mp3_filename"] = os.path.basename(restored)
                self._log(
                    f"Work MP3 regenerated from original: {song.get('title') or song.get('id')}."
                )
                return True
            except Exception as exc:
                self._log(
                    f"Could not regenerate work MP3 for {song.get('title') or song.get('id')}: {exc}",
                    "error",
                )
                return False

        for song in songs:
            if song.get("analysis_status") == "done" and song.get("mp3_filename"):
                await _restore_missing_mp3(song)
        # A song is stream-eligible when:
        #   - Whisper analysis completed and the MP3 exists, AND
        #   - Moderation either was never run (NULL — grandfathered before the
        #     moderation feature was enabled) or returned 'passed' / was
        #     manually 'approved'. 'pending' and 'flagged' songs are held
        #     back until an admin acts on them.
        def _mod_ok(s: dict) -> bool:
            ms = s.get("moderation_status")
            return ms is None or ms in ("passed", "approved")

        def _audio_exists(s: dict) -> bool:
            filename = s.get("mp3_filename")
            return bool(
                filename
                and os.path.isfile(os.path.join(self.trya_stream_dir, "mp3", filename))
            )

        def _approval_ok(s: dict) -> bool:
            return s.get("approval_status") == "approved"

        ready = [
            s for s in songs
            if s.get("analysis_status") == "done"
            and _audio_exists(s)
            and _mod_ok(s)
            and _approval_ok(s)
        ]
        if not ready:
            self._legacy_pipeline = False
            pl_label = {"submission": "submission", "admin": "admin", "both": "either"}.get(active_pl, active_pl)
            return {"ok": False, "error": f"No ready songs in the {pl_label} playlist."}

        def _special_ready(s: dict) -> bool:
            return (
                s.get("analysis_status") == "done"
                and _audio_exists(s)
                and _approval_ok(s)
            )

        def _pick_special_song(songs: list[dict], selection: str) -> dict | None:
            ready_songs = [s for s in songs if _special_ready(s)]
            if not ready_songs:
                return None
            if selection and selection != "random":
                try:
                    selected_id = int(selection)
                    for song in ready_songs:
                        if int(song.get("id") or 0) == selected_id:
                            return song
                except (TypeError, ValueError):
                    pass
            return random.choice(ready_songs)

        random.shuffle(ready)

        # Select the intro first so its duration can count towards the early
        # play window, then prepend it after the regular playlist is arranged.
        intro_song = None
        intro_enabled = (await self.db.get_setting("trya_stream_intro_enabled")) or "off"
        if intro_enabled == "on":
            intro_songs = await self.db.get_all_trya_stream_songs(active_only=True, source="intro")
            for song in intro_songs:
                if song.get("analysis_status") == "done" and song.get("mp3_filename"):
                    await _restore_missing_mp3(song)
            intro_selection = await self.db.get_setting("trya_stream_intro_selection") or "random"
            intro_song = _pick_special_song(intro_songs, intro_selection)
            if intro_song:
                self._log(f"Intro song: {intro_song.get('title', '?')}")

        early_boost = (await self.db.get_setting("trya_stream_ravenveil_early_boost")) or "off"
        if early_boost == "on":
            try:
                intro_duration = float(intro_song.get("duration") or 300.0) if intro_song else 0.0
            except (TypeError, ValueError):
                intro_duration = 300.0 if intro_song else 0.0
            ready, boosted_song, regular_slot, starts_at = _place_submitter_song_in_early_window(
                ready,
                submitter_login=_EARLY_SUBMITTER_LOGIN,
                initial_offset=intro_duration,
            )
            if boosted_song:
                minutes, seconds = divmod(int(starts_at or 0), 60)
                self._log(
                    f"Tarja early-play boost: {boosted_song.get('title', '?')} placed at "
                    f"regular slot {(regular_slot or 0) + 1} (starts around {minutes}:{seconds:02d})."
                )
            else:
                self._log("Tarja early-play boost: no eligible t_ravenveil submission found.")

        if intro_song:
            ready = [intro_song] + ready

        # Store outro for end-of-stream injection
        self._outro_song = None
        self._outro_played = False
        outro_enabled = (await self.db.get_setting("trya_stream_outro_enabled")) or "off"
        if outro_enabled == "on":
            outro_songs = await self.db.get_all_trya_stream_songs(active_only=True, source="outro")
            for song in outro_songs:
                if song.get("analysis_status") == "done" and song.get("mp3_filename"):
                    await _restore_missing_mp3(song)
            outro_selection = await self.db.get_setting("trya_stream_outro_selection") or "random"
            self._outro_song = _pick_special_song(outro_songs, outro_selection)
            if self._outro_song:
                self._log(f"Outro song configured: {self._outro_song.get('title', '?')}")
        loop_mode = (await self.db.get_setting("trya_stream_loop_mode")) or "reshuffle"
        self._outro_in_playlist = bool(self._outro_song and loop_mode == "stop")
        if self._outro_in_playlist:
            ready = ready + [self._outro_song]
        self._outro_counts_in_progress = False
        self._progress_total_count = len(ready)
        self._twitch_key = twitch_key
        self.playlist    = ready
        await self._save_playlist_snapshot(ready, active_pl, scheduled)
        self._stream_ready_event.clear()
        self._submission_bans_advanced = False
        self._reset_ffmpeg_health("offline")
        self._set_obs_overlay_status(False, "disabled", "OBS overlay disabled", "local")
        # Connect to Twitch chat if enabled and credentials exist
        chat_enabled = await self.db.get_setting("trya_stream_twitch_chat_enabled") or "off"
        client_id    = await self.db.get_setting("trya_stream_twitch_client_id")
        refresh_tok  = await self.db.get_setting("trya_stream_twitch_refresh_token")
        broadcaster  = await self.db.get_setting("trya_stream_twitch_broadcaster_login")
        if chat_enabled == "on" and client_id and refresh_tok and broadcaster:
            self._twitch_chat = TwitchBot(self.db, key_prefix="trya_stream_twitch")
            ok, msg = await self._twitch_chat.start()
            if not ok:
                self._log(f"Twitch chat disabled: {msg}", "error")
                self._twitch_chat = None
            else:
                self._log(f"Twitch chat ready ({msg}).")
        self.is_running  = True
        global stream_is_live
        stream_is_live = True
        self._task = asyncio.create_task(self._stream_loop())
        mode = "legacy FFmpeg pipeline" if self._legacy_pipeline else "current FFmpeg pipeline"
        self._log(f"Started with {len(ready)} songs ({mode}).")
        return {"ok": True, "song_count": len(ready)}

    async def _save_playlist_snapshot(self, songs: list[dict], source: str, scheduled: bool) -> None:
        urls = []
        for song in songs:
            url = (song.get("suno_url") or "").strip()
            if url:
                urls.append(url)
        payload = {
            "created_at": int(time.time()),
            "source": source,
            "scheduled": bool(scheduled),
            "song_count": len(songs),
            "urls": urls,
        }
        try:
            await self.db.save_trya_stream_playlist_snapshot(
                created_at=payload["created_at"],
                source=source,
                scheduled=scheduled,
                urls=urls,
            )
            await self.db.set_setting("trya_stream_last_playlist_snapshot", json.dumps(payload))
            if scheduled:
                await self.db.set_setting("trya_stream_last_scheduled_playlist_snapshot", json.dumps(payload))
        except Exception as e:
            self._log(f"Playlist snapshot save failed: {e}", "error")

    async def wait_until_live(self, timeout: float = 900.0) -> bool:
        """Wait until FFmpeg has actually started after start()."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._stream_ready_event.is_set():
                return bool(
                    self.is_running
                    and self._process
                    and self._process.returncode is None
                )
            if not self.is_running:
                return False
            if self._process and self._process.returncode is not None:
                return False
            await asyncio.sleep(0.25)
        return False

    async def stop(self) -> dict:
        self.is_running = False
        global stream_is_live
        stream_is_live = False
        self._stream_ready_event.clear()
        if self._process and self._process.returncode is None:
            try:
                self._process.terminate()
                await asyncio.wait_for(self._process.wait(), timeout=10)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
        self._process = None
        await self._stop_obs_overlay_bridge()
        self._reset_ffmpeg_health("offline")
        self._set_obs_overlay_status(False, "disabled", "OBS overlay disabled", "local")
        self._safe_stop_requested = False
        self._outro_in_playlist = False
        self._outro_counts_in_progress = False
        self._progress_total_count = 0
        self._current_song_index = 0
        self._legacy_pipeline = False
        if self._task:
            self._task.cancel()
            self._task = None
        self.current_song = None
        if self._twitch_chat:
            await self._twitch_chat.stop()
            self._twitch_chat = None
        # Expired/deleted rows may have stayed on disk while FFmpeg held them
        # open. Once the stream is stopped they are safe to collect.
        removed_files = 0
        protected_songs = await self.db.get_all_trya_stream_songs(active_only=True)
        for playlist_song in self.playlist or []:
            song_id = playlist_song.get("id")
            if not song_id:
                continue
            stored = await self.db.get_trya_stream_song(int(song_id))
            if stored and not stored.get("active"):
                removed_files += cleanup_trya_stream_song_files(
                    self.trya_stream_dir, stored, protected_songs
                )
        if removed_files:
            self._log(f"Post-stream cleanup removed {removed_files} inactive file(s).")
        self._log("Stopped.")
        return {"ok": True}

    async def safe_stop(self) -> dict:
        """Post a chat notice and stop the stream after the current song ends."""
        if not self.is_running:
            return {"ok": False, "error": "Stream is not running."}
        if self._safe_stop_requested:
            return {"ok": False, "error": "Safe stop already pending."}
        self._safe_stop_requested = True
        # Announce in chat
        if self._twitch_chat:
            song_title = (self.current_song or {}).get("title") or "current song"
            try:
                await self._twitch_chat.send(
                    f"🎙️ The stream will end after '{song_title}' finishes. Thanks for listening!"
                )
            except Exception as e:
                self._log(f"Safe-stop announcement failed: {e}", "error")
        self._log("Safe stop requested — stream will end after current song.")
        # Schedule the actual stop to fire when the current song ends.
        asyncio.create_task(self._safe_stop_waiter())
        return {"ok": True}

    async def _safe_stop_waiter(self) -> None:
        """Wait until the current song is expected to end, then stop."""
        end_t = self._current_song_end_time
        remaining = end_t - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(remaining + 1.0)   # +1s buffer
        if not self._safe_stop_requested or not self.is_running:
            return
        if (
            self._outro_in_playlist
            and self._outro_song
            and self.current_song
            and self.current_song.get("id") == self._outro_song.get("id")
        ):
            self._outro_played = True
        self._log("Safe stop: stopping stream.")
        await self.stop()

    async def get_status(self) -> dict:
        return {
            "running": self.is_running,
            "safe_stop_pending": self._safe_stop_requested,
            "song": {
                "title":    self.current_song.get("title")    if self.current_song else None,
                "artist":   self.current_song.get("artist")   if self.current_song else None,
                "suno_url": self.current_song.get("suno_url") if self.current_song else None,
            } if self.current_song else None,
            "song_index": self._current_song_index,
            "playlist_length": self._progress_total_count or len(self.playlist),
            "playlist": [
                {
                    "title": song.get("title") or "Unknown title",
                    "artist": song.get("artist") or "Unknown artist",
                }
                for song in self.playlist
            ] if self.is_running else [],
            "legacy_pipeline": self._legacy_pipeline,
            "ffmpeg": self._get_ffmpeg_health(),
            "obs_overlay": dict(self._obs_overlay_status),
        }

    # ── Stream loop (one FFmpeg per full playlist rotation) ────────────────────

    async def _stream_loop(self):
        while self.is_running:
            # Set current_song to first track so status shows something immediately
            if self.playlist:
                self.current_song = self.playlist[0]
                self._current_song_index = 1
            try:
                await self._play_playlist(self.playlist)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._log(f"Playlist error: {e}", "error")
                await asyncio.sleep(3)
                continue

            # Rotation finished cleanly. Decide what to do next based on the
            # admin-configured loop mode.
            if not self.is_running:
                break
            mode = (await self.db.get_setting("trya_stream_loop_mode")) or "reshuffle"
            if mode == "stop":
                self._log("Loop mode 'stop' — playlist finished.")
                if self._outro_in_playlist:
                    self._outro_played = True
                elif self._outro_song and not self._outro_played:
                    self._outro_played = True
                    self._log(f"Playing outro: {self._outro_song.get('title', '?')}")
                    self.playlist = [self._outro_song]
                    continue  # plays outro; next iteration: outro done → stop
                asyncio.create_task(self.stop())
                break
            # mode == "reshuffle": reload eligible songs from DB so newly
            # approved submissions get picked up, then reshuffle.
            active_pl = (await self.db.get_setting("trya_stream_active_playlist")) or "submission"
            pl_source = None if active_pl == "both" else active_pl
            songs = await self.db.get_all_trya_stream_songs(active_only=True, source=pl_source)
            if active_pl == "both":
                songs = [
                    song for song in songs
                    if (song.get("playlist_source") or "submission") in {"submission", "admin"}
                ]
            def _mod_ok(s: dict) -> bool:
                ms = s.get("moderation_status")
                return ms is None or ms in ("passed", "approved")
            ready = [
                s for s in songs
                if s.get("analysis_status") == "done"
                and s.get("mp3_filename")
                and os.path.isfile(os.path.join(
                    self.trya_stream_dir, "mp3", s["mp3_filename"]
                ))
                and s.get("approval_status") == "approved"
                and _mod_ok(s)
            ]
            if not ready:
                self._log("Reshuffle: no eligible songs left — stopping.", "error")
                asyncio.create_task(self.stop())
                break
            random.shuffle(ready)
            self.playlist = ready
            self._log(f"Reshuffled playlist ({len(ready)} songs) — next rotation starting.")

    async def _get_square_blur_media(self, source_path: str, force: bool = False) -> str:
        """Pre-render one source into a cached square blur composition."""
        stat = os.stat(source_path)
        signature = hashlib.sha256(
            f"square-blur-v2-{_INSET_W}x{_INSET_H}|{os.path.abspath(source_path)}|{stat.st_size}|{stat.st_mtime_ns}".encode()
        ).hexdigest()[:20]
        cache_dir = os.path.join(self.trya_stream_dir, "square_cache")
        os.makedirs(cache_dir, exist_ok=True)
        output_path = os.path.join(cache_dir, f"{signature}.mp4")
        if not force and os.path.isfile(output_path) and os.path.getsize(output_path) > 0:
            return output_path

        temporary_path = f"{output_path}.tmp.mp4"
        is_image = os.path.splitext(source_path)[1].lower() in {".jpg", ".jpeg", ".png", ".webp"}
        input_args = ["-loop", "1", "-t", "1", "-i", source_path] if is_image else ["-i", source_path]
        filtergraph = (
            f"[0:v]split=2[bg][fg];"
            f"[bg]scale={_INSET_W}:{_INSET_H}:force_original_aspect_ratio=increase,"
            f"crop={_INSET_W}:{_INSET_H},boxblur=30:10[blurred];"
            f"[fg]scale={_INSET_W}:{_INSET_H}:force_original_aspect_ratio=decrease:"
            f"force_divisible_by=2[front];"
            f"[blurred][front]overlay=(W-w)/2:(H-h)/2,setsar=1,fps=24,format=yuv420p[vout]"
        )
        command = [
            "ffmpeg", "-y", "-hide_banner", "-v", "error", *input_args,
            "-filter_complex", filtergraph, "-map", "[vout]", "-an",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", temporary_path,
        ]
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0 or not os.path.isfile(temporary_path):
            try:
                os.remove(temporary_path)
            except OSError:
                pass
            detail = (stderr or b"").decode("utf-8", "replace").strip()
            raise RuntimeError(detail or "square media conversion failed")
        os.replace(temporary_path, output_path)
        return output_path

    async def prepare_song_square_media(self, song: dict, force: bool = False) -> str | None:
        """Resolve one song's visual and persist its prepared square cache state."""
        song_id = int(song.get("id") or 0)
        if not song_id:
            return None
        await self.db.update_trya_stream_song(
            song_id, square_media_status="processing", square_media_error=None
        )
        try:
            source_path = await self._get_video(song) or await self._get_cover(song)
            if not source_path:
                raise RuntimeError("No song video or cover is available")
            output_path = await self._get_square_blur_media(source_path, force=force)
            filename = os.path.basename(output_path)
            await self.db.update_trya_stream_song(
                song_id,
                square_media_filename=filename,
                square_media_status="ready",
                square_media_at=time.time(),
                square_media_error=None,
            )
            self._log(f"Square media ready: {song.get('title') or song_id} → {output_path}")
            return output_path
        except Exception as exc:
            await self.db.update_trya_stream_song(
                song_id,
                square_media_status="failed",
                square_media_at=time.time(),
                square_media_error=str(exc)[:1000],
            )
            self._log(
                f"Square media preparation failed for {song.get('title') or song_id}: {exc}",
                "error",
            )
            return None

    async def _play_playlist(self, songs: list):
        """Run one FFmpeg job that covers all songs without stopping between them."""
        bg_fn   = await self.db.get_setting("trya_stream_bg_filename") or ""
        bg_type = await self.db.get_setting("trya_stream_bg_type") or "image"
        songs = await self._prepare_playback_songs(songs)

        # Resolve loop video: supports multiple uploads + shuffle / fixed / concat-all.
        loop_fn   = ""
        loop_path = None
        obs_overlay_path = None
        obs_overlay_fps = _DEFAULT_OBS_OVERLAY_FPS
        legacy_loop_source = ((await self.db.get_setting("trya_stream_loop_source")) or "local").strip().lower()
        obs_overlay_enabled = (await self.db.get_setting("trya_stream_obs_overlay_enabled") or "off") == "on"
        if legacy_loop_source == "rtmp":
            obs_overlay_enabled = True
        import json as _json
        loop_raw = await self.db.get_setting("trya_stream_loop_videos") or "[]"
        loop_vids = _json.loads(loop_raw) if loop_raw else []
        if loop_vids:
            loop_sel = await self.db.get_setting("trya_stream_loop_selection") or "shuffle"
            if loop_sel == "concat_all":
                if len(loop_vids) == 1:
                    loop_fn = loop_vids[0]["filename"]
                    self._log(f"Loop video (concat_all single): {loop_fn}")
                else:
                    loop_path = await self._build_concat_all_video(loop_vids)
                    if not loop_path:
                        loop_fn = random.choice(loop_vids)["filename"]
                        self._log(f"Concat-all failed, shuffle fallback: {loop_fn}", "error")
            elif loop_sel == "concat_all_random":
                if len(loop_vids) == 1:
                    loop_fn = loop_vids[0]["filename"]
                    self._log(f"Loop video (concat_all_random single): {loop_fn}")
                else:
                    shuffled_loop_vids = list(loop_vids)
                    random.shuffle(shuffled_loop_vids)
                    loop_path = await self._build_concat_all_video(shuffled_loop_vids, random_order=True)
                    if not loop_path:
                        loop_fn = random.choice(loop_vids)["filename"]
                        self._log(f"Random concat-all failed, shuffle fallback: {loop_fn}", "error")
            elif loop_sel == "concat_random_subset":
                try:
                    subset_count = int(
                        await self.db.get_setting("trya_stream_loop_random_count") or "10"
                    )
                except (TypeError, ValueError):
                    subset_count = 10
                subset_count = max(1, min(subset_count, len(loop_vids)))
                selected_loop_vids = await self._select_random_loop_video_subset(
                    loop_vids, subset_count
                )
                if len(selected_loop_vids) == 1:
                    loop_fn = selected_loop_vids[0]["filename"]
                    self._log(f"Loop video (random subset single): {loop_fn}")
                else:
                    self._log(
                        f"Loop video random subset: selected {subset_count} of "
                        f"{len(loop_vids)} videos."
                    )
                    loop_path = await self._build_concat_all_video(
                        selected_loop_vids, random_order=True
                    )
                    if not loop_path:
                        loop_fn = random.choice(selected_loop_vids)["filename"]
                        self._log(
                            f"Random subset concat failed, shuffle fallback: {loop_fn}",
                            "error",
                        )
            elif loop_sel == "shuffle":
                loop_fn = random.choice(loop_vids)["filename"]
                self._log(f"Loop video (shuffle): {loop_fn}")
            else:
                # Fixed selection — verify it still exists in the list
                match = [v for v in loop_vids if v["filename"] == loop_sel]
                if match:
                    loop_fn = match[0]["filename"]
                else:
                    loop_fn = loop_vids[0]["filename"]
                    self._log(f"Selected loop video gone, falling back to {loop_fn}", "error")
        elif not loop_fn:
            # Legacy fallback: single-video setting from before the migration
            loop_fn = await self.db.get_setting("trya_stream_loop_filename") or ""

        bg_path = os.path.join(self.trya_stream_dir, "assets", bg_fn) if bg_fn else None
        if loop_path is None:
            loop_path = os.path.join(self.trya_stream_dir, "assets", loop_fn) if loop_fn else None

        if obs_overlay_enabled:
            obs_rtmp_key = (await self.db.get_setting("trya_stream_loop_rtmp_key") or "").strip()
            if obs_rtmp_key:
                obs_overlay_fps = await self._get_obs_overlay_fps()
                obs_overlay_path = await self._start_obs_overlay_bridge(obs_rtmp_key, obs_overlay_fps)
                self._log(f"OBS RTMP override enabled on port 1936 ({obs_overlay_fps} fps).")
            else:
                self._set_obs_overlay_status(True, "missing_key", "OBS RTMP: stream key missing", "rtmp")
                self._log("OBS RTMP override is enabled, but no stream key is configured.", "error")
        else:
            self._set_obs_overlay_status(False, "fallback", "OBS overlay off · local loop active", "local")

        # Prefetch all covers/videos
        media_paths = []
        for song in songs:
            square_filename = os.path.basename(song.get("square_media_filename") or "")
            square_path = os.path.join(self.trya_stream_dir, "square_cache", square_filename) if square_filename else ""
            if song.get("square_media_status") == "ready" and os.path.isfile(square_path):
                path = square_path
            else:
                path = await self._get_video(song) or await self._get_cover(song)
                self._log(
                    f"Prepared square media missing for {song.get('title') or song.get('id')}; using low-cost fit fallback.",
                    "error",
                )
            media_paths.append(path)
            self._log(f"Media ready: {song.get('title')} → {path}")

        # Build combined ASS (subtitles + NowPlaying title cards)
        show_progress = (await self.db.get_setting("trya_stream_progress_overlay") or "off") == "on"
        stream_title_enabled = (
            (await self.db.get_setting("trya_stream_title_enabled") or "off") == "on"
        )
        stream_title_text = (await self.db.get_setting("trya_stream_title_text") or "").strip()
        disclaimer_enabled = (
            (await self.db.get_setting("trya_stream_disclaimer_enabled") or "off") == "on"
        )
        disclaimer_text = (await self.db.get_setting("trya_stream_disclaimer_text") or "").strip()
        media_corners_enabled = (
            (await self.db.get_setting("trya_stream_media_corners_enabled") or "off") == "on"
        )
        media_border_enabled = (
            (await self.db.get_setting("trya_stream_media_border_enabled") or "off") == "on"
        )
        try:
            media_corner_radius = max(
                1,
                min(120, int(await self.db.get_setting("trya_stream_media_corner_radius") or "28")),
            )
        except (TypeError, ValueError):
            media_corner_radius = 28
        try:
            media_border_width = max(
                1,
                min(20, int(await self.db.get_setting("trya_stream_media_border_width") or "3")),
            )
        except (TypeError, ValueError):
            media_border_width = 3
        media_border_color = (
            await self.db.get_setting("trya_stream_media_border_color") or "#A855F7"
        ).strip().upper()
        if not re.fullmatch(r"#[0-9A-F]{6}", media_border_color):
            media_border_color = "#A855F7"
        media_style = {
            "enabled": media_corners_enabled,
            "radius": media_corner_radius,
            "border_enabled": media_border_enabled,
            "border_width": media_border_width,
            "border_color": media_border_color,
        }
        if media_corners_enabled:
            border_status = (
                f"on ({media_border_width}px {media_border_color})"
                if media_border_enabled
                else "off"
            )
            self._log(
                f"Media frame: rounded corners {media_corner_radius}px, border {border_status}."
            )
        progress_total_count = len(songs)
        progress_index_offset = 0
        progress_extra_duration = 0.0
        if self._outro_song and self._outro_counts_in_progress and not self._outro_played:
            progress_total_count = max(self._progress_total_count, len(songs) + 1)
            progress_extra_duration = self._outro_song.get("duration") or 300
        elif self._outro_song and self._outro_counts_in_progress and self._outro_played and len(songs) == 1:
            progress_total_count = max(self._progress_total_count, 1)
            progress_index_offset = max(progress_total_count - 1, 0)
        combined_ass = self._build_combined_ass(
            songs,
            show_progress=show_progress,
            progress_total_count=progress_total_count,
            progress_index_offset=progress_index_offset,
            progress_extra_duration=progress_extra_duration,
            stream_title_enabled=stream_title_enabled,
            stream_title_text=stream_title_text,
            disclaimer_enabled=disclaimer_enabled,
            disclaimer_text=disclaimer_text,
        )

        total_dur = sum(s.get("duration") or 300 for s in songs)
        audio_concat_file = None
        if self._legacy_pipeline:
            audio_concat_file = os.path.join(self.trya_stream_dir, "_audio_concat.txt")
            with open(audio_concat_file, "w", encoding="utf-8") as f:
                for song in songs:
                    mp3 = os.path.join(self.trya_stream_dir, "mp3", song["mp3_filename"])
                    f.write(f"file '{mp3}'\n")

        cmd = self._build_playlist_cmd(
            songs=songs,
            media_paths=media_paths,
            bg_path=bg_path   if bg_path   and os.path.exists(bg_path)   else None,
            bg_type=bg_type,
            loop_path=loop_path if loop_path and os.path.exists(loop_path) else None,
            obs_overlay_path=obs_overlay_path,
            obs_overlay_fps=obs_overlay_fps if obs_overlay_path else _DEFAULT_OBS_OVERLAY_FPS,
            video_bitrate_kbps=await self._get_video_bitrate_kbps(),
            ass_path=combined_ass if combined_ass and os.path.exists(combined_ass) else None,
            twitch_key=self._twitch_key,
            total_dur=total_dur,
            legacy_pipeline=self._legacy_pipeline,
            audio_concat_file=audio_concat_file,
            media_style=media_style,
        )

        titles = " → ".join(s.get("title") or "?" for s in songs)
        self._log(f"FFmpeg playlist: {titles}")
        self._reset_ffmpeg_health("starting")

        # limit=1 MiB: FFmpeg can occasionally emit very long progress/banner
        # lines without an embedded \n (e.g. when a filter logs a giant
        # parameter dump). The default StreamReader limit is 64 KiB which
        # made readline() raise LimitOverrunError after ~5 minutes,
        # killing our stderr reader.
        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            limit=1024 * 1024,
        )
        stderr_task = asyncio.create_task(self._pipe_ffmpeg_stderr(self._process))
        await asyncio.sleep(2)
        startup_stable = self._process.returncode is None
        chat_task = None
        announce_task = None
        if startup_stable:
            self._stream_ready_event.set()
            self._log("FFmpeg process remained stable for 2 seconds; stream is live.")
            chat_task = asyncio.create_task(self._post_now_playing_loop(songs))
            announce_task = asyncio.create_task(self._announce_rotation_end(total_dur))
        else:
            self._log(
                f"FFmpeg failed during startup (exit {self._process.returncode}).",
                "error",
            )
        if startup_stable and not self._submission_bans_advanced:
            self._submission_bans_advanced = True
            try:
                advanced = await self.db.advance_trya_stream_submission_bans()
                for ban in advanced:
                    remaining = int(ban.get("streams_remaining_after") or 0)
                    name = ban.get("display_name") or ban.get("user_name") or ban.get("user_id")
                    if remaining:
                        self._log(
                            f"Submission ban advanced for {name}: {remaining} stream(s) remaining."
                        )
                    else:
                        self._log(f"Submission ban completed for {name}.")
                    await self.db.add_audit_log(
                        event_type="trya_stream_submission_ban_advanced",
                        user_id=ban.get("user_id"),
                        user_name=ban.get("user_name"),
                        details=f"Stream started; {remaining} blocked stream(s) remaining",
                        actor="system",
                    )
            except Exception as exc:
                self._log(f"Could not advance submission bans: {exc}", "error")
        proc = self._process
        await proc.wait()
        rc = proc.returncode
        if chat_task:
            chat_task.cancel()
            try:
                await chat_task
            except asyncio.CancelledError:
                pass
        if announce_task:
            announce_task.cancel()
            try:
                await announce_task
            except asyncio.CancelledError:
                pass
        # stderr task ends naturally when EOF — just await it.
        try:
            await stderr_task
        except Exception:
            pass
        await self._stop_obs_overlay_bridge()
        if rc and rc != 0 and self.is_running:
            self._log(f"FFmpeg exited {rc}.", "error")
        elif self.is_running:
            self._set_ffmpeg_health_state("starting")

    async def _pipe_ffmpeg_stderr(self, proc) -> None:
        """Read FFmpeg stderr line by line and feed it into the live log.

        Filters out the per-second progress lines (frame=... time=...) which
        would otherwise drown out anything interesting. Warnings and errors
        pass through."""
        if not proc.stderr:
            return
        while True:
            try:
                raw = await proc.stderr.readline()
            except (asyncio.LimitOverrunError, ValueError) as e:
                # A single line exceeded the StreamReader buffer. Drain the
                # over-long chunk in fixed-size reads so we can keep going
                # instead of letting the reader die for the rest of the
                # rotation. We log the event once and discard the payload.
                self._log(
                    f"stderr reader: dropping over-long line ({type(e).__name__})",
                    "error",
                )
                try:
                    # Read whatever is currently buffered + a bit more, until
                    # we hit the next newline. asyncio's readuntil gives no
                    # easy way to do this once it has raised, so we fall back
                    # to chunked .read() until '\n'.
                    while True:
                        chunk = await proc.stderr.read(64 * 1024)
                        if not chunk:
                            return
                        if b"\n" in chunk:
                            break
                except Exception:
                    return
                continue
            except Exception as e:
                self._log(f"stderr reader exception: {e}", "error")
                return
            if not raw:
                return
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line:
                continue
            if "=" in line:
                key, value = line.split("=", 1)
                if key in _FFMPEG_PROGRESS_KEYS:
                    if key == "progress":
                        self._record_ffmpeg_progress(self._ffmpeg_progress_pending)
                        self._ffmpeg_progress_pending = {}
                    else:
                        self._ffmpeg_progress_pending[key] = value.strip()
                    continue
            if _FFMPEG_PROGRESS_RE.match(line):
                self._record_ffmpeg_progress_line(line)
                continue
            # Heuristic: anything containing 'error' or starting with
            # '[<x>] @ ...' suggests a problem worth flagging.
            level = "error" if ("error" in line.lower() or "failed" in line.lower()) else "ffmpeg"
            self._log(line, level)

    async def _announce_rotation_end(self, total_dur: float):
        """Post a heads-up to Twitch chat ~30s before the rotation ends.

        Message text depends on the configured loop mode:
          - 'stop':       "Stream ending in ~30s…"
          - 'reshuffle':  "Restarting with a freshly shuffled playlist in ~30s…"
        Silently no-ops if there's no Twitch chat connection or the rotation
        is too short for a meaningful pre-warning (<45s).
        """
        try:
            if not self._twitch_chat:
                return
            lead = 30.0
            wait = total_dur - lead
            if wait < 15:
                return
            await asyncio.sleep(wait)
            mode = (await self.db.get_setting("trya_stream_loop_mode")) or "reshuffle"
            if mode == "stop":
                msg = "\u26A0\uFE0F Heads up: the stream will end in ~30 seconds when the last track finishes. Thanks for tuning in!"
            else:
                msg = "\U0001F501 Heads up: the playlist will restart in ~30 seconds with a freshly shuffled order (including any newly approved tracks)."
            try:
                await self._twitch_chat.send(msg)
                self._log(f"Rotation end announcement posted (mode={mode}).")
            except Exception as e:
                self._log(f"Rotation end announcement failed: {e}", "error")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._log(f"_announce_rotation_end error: {e}", "error")

    async def _get_obs_overlay_fps(self) -> int:
        try:
            fps = int(await self.db.get_setting("trya_stream_obs_overlay_fps") or _DEFAULT_OBS_OVERLAY_FPS)
        except (TypeError, ValueError):
            fps = _DEFAULT_OBS_OVERLAY_FPS
        return fps if fps in (15, 20, 24) else _DEFAULT_OBS_OVERLAY_FPS

    async def _get_video_bitrate_kbps(self) -> int:
        try:
            bitrate = int(await self.db.get_setting("trya_stream_video_bitrate_kbps") or "2500")
        except (TypeError, ValueError):
            bitrate = 2500
        return bitrate if bitrate in (1800, 2000, 2500) else 2500

    def _obs_listen_port(self) -> int:
        return 1936

    async def _start_obs_overlay_bridge(self, stream_key: str, fps: int) -> str | None:
        """Start a small raw-video bridge for the optional OBS RTMP override.

        The main stream reads a continuous RGBA rawvideo input from a FIFO.
        This bridge keeps that FIFO fed with transparent frames while no OBS
        client is connected, and swaps in OBS frames as soon as FFmpeg receives
        them. That keeps the local loop video as the stable fallback.
        """
        await self._stop_obs_overlay_bridge()
        fifo_path = os.path.join(self.trya_stream_dir, "_obs_overlay.rgba")
        try:
            if os.path.exists(fifo_path):
                os.remove(fifo_path)
            os.mkfifo(fifo_path)
        except Exception as e:
            self._set_obs_overlay_status(True, "error", "OBS RTMP: bridge setup failed", "rtmp")
            self._log(f"OBS overlay bridge setup failed: {e}", "error")
            return None

        self._obs_bridge_path = fifo_path
        self._set_obs_overlay_status(True, "waiting", "OBS RTMP: waiting · local loop fallback active", "rtmp")
        self._obs_bridge_task = asyncio.create_task(self._obs_overlay_bridge_loop(fifo_path, stream_key, fps))
        return fifo_path

    async def _stop_obs_overlay_bridge(self) -> None:
        task = self._obs_bridge_task
        self._obs_bridge_task = None
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        proc = self._obs_bridge_proc
        self._obs_bridge_proc = None
        if proc and proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if self._obs_bridge_path and os.path.exists(self._obs_bridge_path):
            try:
                os.remove(self._obs_bridge_path)
            except Exception:
                pass
        self._obs_bridge_path = None

    async def _write_obs_overlay_frame(self, fd: int, frame: bytes) -> bool:
        view = memoryview(frame)
        written = 0
        while written < len(view):
            try:
                written += os.write(fd, view[written:])
            except BlockingIOError:
                await asyncio.sleep(0.005)
            except BrokenPipeError:
                return False
        return True

    async def _obs_overlay_bridge_loop(self, fifo_path: str, stream_key: str, fps: int) -> None:
        frame_size = _OBS_BRIDGE_W * _OBS_BRIDGE_H * 4
        frame_interval = 1.0 / fps
        fd = None
        proc = None
        fallback_proc = None
        reader_task = None
        last_connected = False
        last_frame = None

        async def stop_proc(process, timeout: float = 3.0):
            if process and process.returncode is None:
                try:
                    process.terminate()
                    await asyncio.wait_for(process.wait(), timeout=timeout)
                except Exception:
                    try:
                        process.kill()
                    except Exception:
                        pass

        async def start_listener():
            rtmp_url = f"rtmp://0.0.0.0:{self._obs_listen_port()}/live/{stream_key}"
            vf = (
                f"scale={_OBS_BRIDGE_W}:{_OBS_BRIDGE_H}:force_original_aspect_ratio=decrease,"
                f"pad={_OBS_BRIDGE_W}:{_OBS_BRIDGE_H}:(ow-iw)/2:(oh-ih)/2:color=black@0,"
                f"setsar=1,fps={fps},format=rgba"
            )
            return await asyncio.create_subprocess_exec(
                "ffmpeg", "-hide_banner", "-loglevel", "warning",
                "-f", "flv", "-listen", "1",
                "-rw_timeout", "5000000",
                "-i", rtmp_url,
                "-an", "-vf", vf,
                "-f", "rawvideo", "pipe:1",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )

        async def start_fallback_writer():
            return await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-re",
                "-f", "lavfi",
                "-i", f"color=color=black:size={_OBS_BRIDGE_W}x{_OBS_BRIDGE_H}:rate={fps}",
                "-vf", "format=rgba,colorchannelmixer=aa=0",
                "-f", "rawvideo", fifo_path,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )

        async def read_frames(ffmpeg_proc, queue: asyncio.Queue):
            try:
                while True:
                    frame = await ffmpeg_proc.stdout.readexactly(frame_size)
                    if queue.full():
                        try:
                            queue.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                    await queue.put(frame)
            except (asyncio.IncompleteReadError, asyncio.CancelledError):
                return

        try:
            fd = os.open(fifo_path, os.O_RDWR | os.O_NONBLOCK)
            fallback_proc = await start_fallback_writer()
            proc = await start_listener()
            self._obs_bridge_proc = proc
            frame_queue: asyncio.Queue = asyncio.Queue(maxsize=2)
            reader_task = asyncio.create_task(read_frames(proc, frame_queue))
            self._log(
                f"OBS overlay bridge listening on rtmp://0.0.0.0:{self._obs_listen_port()}/live/<key>."
            )
            next_frame_at = time.monotonic()

            while True:
                if fallback_proc and fallback_proc.returncode is not None and not last_connected:
                    fallback_proc = await start_fallback_writer()

                if proc is None or proc.returncode is not None or (reader_task and reader_task.done() and frame_queue.empty()):
                    if last_connected:
                        self._set_obs_overlay_status(True, "fallback", "OBS RTMP: disconnected · local loop fallback active", "rtmp")
                        self._log("OBS RTMP disconnected; local loop fallback active.")
                        last_connected = False
                        fallback_proc = await start_fallback_writer()
                    if reader_task:
                        reader_task.cancel()
                        try:
                            await reader_task
                        except asyncio.CancelledError:
                            pass
                    await stop_proc(proc)
                    proc = await start_listener()
                    self._obs_bridge_proc = proc
                    frame_queue = asyncio.Queue(maxsize=2)
                    reader_task = asyncio.create_task(read_frames(proc, frame_queue))

                pending_frame = None
                got_frame = False
                try:
                    while True:
                        pending_frame = frame_queue.get_nowait()
                        got_frame = True
                except asyncio.QueueEmpty:
                    pass

                if got_frame:
                    last_frame = pending_frame
                    if not last_connected:
                        await stop_proc(fallback_proc, timeout=1.0)
                        fallback_proc = None
                        self._set_obs_overlay_status(True, "connected", "OBS RTMP: connected · live overlay active", "rtmp")
                        self._log("OBS RTMP connected; live overlay active.")
                        last_connected = True

                now = time.monotonic()
                if next_frame_at > now:
                    await asyncio.sleep(next_frame_at - now)
                # Keep the main FFmpeg graph fed even if OBS briefly jitters.
                # Repeating the last frame is better than blocking Twitch output.
                frame_to_write = last_frame if last_connected else pending_frame
                if frame_to_write is not None:
                    if not await self._write_obs_overlay_frame(fd, frame_to_write):
                        await asyncio.sleep(0.05)
                next_frame_at = max(next_frame_at + frame_interval, time.monotonic())
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._set_obs_overlay_status(True, "error", "OBS RTMP: bridge error", "rtmp")
            self._log(f"OBS overlay bridge error: {e}", "error")
        finally:
            if reader_task:
                reader_task.cancel()
                try:
                    await reader_task
                except asyncio.CancelledError:
                    pass
            await stop_proc(proc)
            await stop_proc(fallback_proc)
            if fd is not None:
                try:
                    os.close(fd)
                except Exception:
                    pass

    async def _post_now_playing_loop(self, songs: list):
        """Post ♪ Now Playing to Twitch chat at each song boundary.
        Also updates current_song and _current_song_end_time for safe_stop."""
        if not songs:
            return

        starts = []
        cursor = 0.0
        for song in songs:
            starts.append(cursor)
            cursor += float(song.get("duration") or 300)
        total_duration = cursor
        posted: set[int] = set()
        safe_stop_logged: set[int] = set()
        active_idx = -1

        while self.is_running and self._process and self._process.returncode is None:
            out_time = self._get_ffmpeg_out_time_seconds()
            if out_time is None:
                await asyncio.sleep(0.5)
                continue

            idx = 0
            for i, start in enumerate(starts):
                if out_time + 0.25 >= start:
                    idx = i
                else:
                    break

            if idx != active_idx:
                active_idx = idx
                song = songs[idx]
                dur = float(song.get("duration") or 300)
                elapsed_in_song = max(0.0, out_time - starts[idx])
                remaining = max(0.0, dur - elapsed_in_song)
                self.current_song = song
                self._current_song_index = idx + 1
                self._current_song_end_time = time.monotonic() + remaining

            song = songs[idx]
            dur = float(song.get("duration") or 300)
            elapsed_in_song = max(0.0, out_time - starts[idx])
            self._current_song_end_time = time.monotonic() + max(0.0, dur - elapsed_in_song)

            if self._safe_stop_requested and idx not in safe_stop_logged:
                safe_stop_logged.add(idx)
                self._log(f"Safe stop: finishing after '{song.get('title','?')}' ({dur}s).")

            # FFmpeg's output clock runs a few seconds ahead of what viewers
            # hear after RTMP ingest and Twitch player buffering. Posting at
            # 15s on that clock lands close to 10s after the audible change.
            post_at = starts[idx] + min(_NOW_PLAYING_CHAT_DELAY, dur)
            if idx not in posted and out_time >= post_at:
                posted.add(idx)
                if self._twitch_chat:
                    title    = song.get("title")  or "Unknown"
                    artist   = song.get("artist") or ""
                    suno_url = song.get("suno_url") or ""
                    msg = f"\U0001F3B5 Now Playing: {title}"
                    if artist:
                        msg += f" - {artist}"
                    if suno_url:
                        msg += f" | {suno_url}"
                    await self._twitch_chat.send(msg)
                    self._log(
                        f"Now Playing chat post: '{title}' at "
                        f"{elapsed_in_song:.1f}s on the FFmpeg clock."
                    )

            if out_time >= total_duration:
                break
            await asyncio.sleep(0.5)

    def _get_ffmpeg_out_time_seconds(self) -> float | None:
        progress = self._ffmpeg_progress or {}
        raw_us = progress.get("out_time_us") or progress.get("out_time_ms")
        if raw_us:
            try:
                return max(0.0, float(raw_us) / 1_000_000.0)
            except (TypeError, ValueError):
                pass
        raw = progress.get("out_time")
        if not raw:
            return None
        try:
            parts = str(raw).split(":")
            if len(parts) != 3:
                return None
            hours = float(parts[0])
            minutes = float(parts[1])
            seconds = float(parts[2])
            return max(0.0, hours * 3600 + minutes * 60 + seconds)
        except (TypeError, ValueError):
            return None

    async def _probe_audio_duration(self, song: dict) -> float | None:
        mp3_filename = song.get("mp3_filename")
        if not mp3_filename:
            return None
        mp3_path = os.path.join(self.trya_stream_dir, "mp3", mp3_filename)
        if not os.path.exists(mp3_path):
            return None
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-hide_banner", "-v", "error",
                "-nostats", "-progress", "pipe:1",
                "-i", mp3_path,
                "-map", "0:a:0",
                "-f", "null", "-",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
            progress = {}
            for raw_line in out.decode("utf-8", errors="replace").splitlines():
                if "=" not in raw_line:
                    continue
                key, value = raw_line.split("=", 1)
                progress[key.strip()] = value.strip()
            raw_us = progress.get("out_time_us") or progress.get("out_time_ms")
            if raw_us:
                value = float(raw_us) / 1_000_000.0
                if value > 0:
                    return value
            raw_time = progress.get("out_time")
            if raw_time:
                parts = raw_time.split(":")
                if len(parts) == 3:
                    value = float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
                    if value > 0:
                        return value
        except Exception as e:
            self._log(f"Decoded duration probe failed for '{song.get('title') or mp3_filename}': {e}", "error")

        try:
            proc = await asyncio.create_subprocess_exec(
                "ffprobe", "-v", "quiet",
                "-select_streams", "a:0",
                "-show_entries", "stream=duration",
                "-of", "csv=p=0",
                mp3_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
            value = float((out.decode().strip() or "0"))
            return value if value > 0 else None
        except Exception as e:
            self._log(f"Stream duration probe failed for '{song.get('title') or mp3_filename}': {e}", "error")
            return None

    async def _prepare_playback_songs(self, songs: list[dict]) -> list[dict]:
        """Use the actual local MP3 duration for playback timing.

        The DB duration can become stale if Suno metadata or manual edits drift
        from the uploaded MP3. A wrong duration shifts every later song boundary
        in the single-FFmpeg rotation, so the local audio file is the source of
        truth for stream timing.
        """
        prepared: list[dict] = []
        for song in songs:
            item = dict(song)
            probed = await self._probe_audio_duration(item)
            if probed is not None:
                try:
                    stored = float(item.get("duration") or 0)
                except (TypeError, ValueError):
                    stored = 0.0
                if not stored or abs(probed - stored) > 1.0:
                    title = item.get("title") or item.get("mp3_filename") or "unknown"
                    if stored:
                        self._log(f"Duration corrected for '{title}': DB {stored:.1f}s → MP3 {probed:.1f}s")
                    else:
                        self._log(f"Duration filled for '{title}': MP3 {probed:.1f}s")
                    song_id = item.get("id")
                    if song_id:
                        try:
                            await self.db.update_trya_stream_song(song_id, duration=probed)
                        except Exception as e:
                            self._log(f"Duration DB update failed for song #{song_id}: {e}", "error")
                item["duration"] = probed
            prepared.append(item)
        return prepared

    # ── Media helpers ──────────────────────────────────────────────────────────

    async def _get_cover(self, song: dict) -> str | None:
        """Download cover JPEG to local cache. Returns local path or None."""
        import aiohttp
        uuid      = song.get("suno_uuid") or ""
        cache_dir = os.path.join(self.trya_stream_dir, "cover_cache")
        os.makedirs(cache_dir, exist_ok=True)
        dest = os.path.join(cache_dir, f"{uuid}.jpg")
        if os.path.exists(dest):
            # Idempotent re-check: covers downloaded before the normaliser
            # existed (or by some other code path) may still be 2000×2000
            # mjpeg, which murders FFmpeg performance when looped 25× per
            # song. The normaliser early-returns if size is already ≤720.
            await self._normalize_cover_image(dest)
            return dest
        url = song.get("cover_url") or f"https://cdn1.suno.ai/image_large_{uuid}.jpeg"
        try:
            async with aiohttp.ClientSession(headers={"User-Agent": _BROWSER_UA}) as sess:
                async with sess.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
                    if r.status == 200:
                        with open(dest, "wb") as f:
                            f.write(await r.read())
                        await self._normalize_cover_image(dest)
                        return dest
        except Exception as e:
            self._log(f"Cover download error ({uuid}): {e}", "error")
        return None

    async def _get_video(
        self, song: dict, *, allow_hook_fallback: bool = True
    ) -> str | None:
        """Download the Hook override or regular Suno video to local cache."""
        import aiohttp
        hook_id = (song.get("hook_id") or "").strip()
        video_url = song.get("hook_video_url") if hook_id else song.get("video_url")
        if not video_url:
            if hook_id and allow_hook_fallback:
                fallback_song = dict(song)
                fallback_song.update(hook_id=None, hook_video_url=None)
                return await self._get_video(fallback_song)
            return None
        uuid      = song.get("suno_uuid") or ""
        cache_dir = os.path.join(self.trya_stream_dir, "cover_cache")
        os.makedirs(cache_dir, exist_ok=True)
        if hook_id:
            dest = trya_stream_hook_cache_path(
                self.trya_stream_dir, song.get("id") or uuid, hook_id
            )
            media_label = f"Hook {hook_id}"
        else:
            dest = os.path.join(cache_dir, f"{uuid}.mp4")
            media_label = f"video {uuid}"
        if os.path.exists(dest):
            # Idempotent re-check: files cached before the normaliser was
            # introduced (or in a previous, looser version of it) may still
            # exceed 720px on a side. The check is ffprobe-cheap and
            # early-returns when the file is already compliant.
            normalized = await self._normalize_cover_video(dest)
            if hook_id and not normalized:
                try:
                    os.remove(dest)
                except OSError:
                    pass
                if allow_hook_fallback:
                    fallback_song = dict(song)
                    fallback_song.update(hook_id=None, hook_video_url=None)
                    return await self._get_video(fallback_song)
                return None
            return dest
        try:
            async with aiohttp.ClientSession(headers={"User-Agent": _BROWSER_UA}) as sess:
                async with sess.get(video_url, timeout=aiohttp.ClientTimeout(total=60)) as r:
                    if r.status == 200:
                        with open(dest, "wb") as f:
                            f.write(await r.read())
                        self._log(f"{media_label} cached: {os.path.basename(dest)}")
                        # Normalize immediately so the stream pipeline always
                        # sees lightweight covers (see _normalize_cover_video).
                        normalized = await self._normalize_cover_video(dest)
                        if hook_id and not normalized:
                            try:
                                os.remove(dest)
                            except OSError:
                                pass
                            if allow_hook_fallback:
                                fallback_song = dict(song)
                                fallback_song.update(hook_id=None, hook_video_url=None)
                                return await self._get_video(fallback_song)
                            return None
                        return dest
        except Exception as e:
            self._log(f"Video download error ({uuid}): {e}", "error")
        if hook_id and allow_hook_fallback:
            self._log(
                f"Hook unavailable for {song.get('title') or uuid}; using regular video fallback.",
                "error",
            )
            fallback_song = dict(song)
            fallback_song.update(hook_id=None, hook_video_url=None)
            return await self._get_video(fallback_song)
        return None

    async def _normalize_cover_video(self, path: str) -> bool:
        """Re-encode an over-sized cover MP4 to a stream-friendly form.

        Some Suno cover videos ship at 1440×1440 @ 8 Mbit/s. Looping such a
        heavy source ~25× per song through scale+crop+fps in the playlist
        FFmpeg causes periodic CPU spikes on each loop wrap (decoder
        reinitialisation + heavy 16× downscale) and produces visible stutter.
        We trim every cover to a uniform, lightweight baseline:

          - Max 720x720 (Lanczos downscale, preserves aspect)
          - 24 fps (matches Suno's native framerate, no resampling)
          - Keyframe every second (smoother loop seeks)
          - libx264 CRF 24, veryfast preset → ~1-2 Mbit/s

        Returns True on success. Files already at or below the target size
        are skipped (idempotent).
        """
        # Probe current properties
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height,r_frame_rate,bit_rate",
                "-of", "csv=p=0", path,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await proc.communicate()
            parts = (out.decode().strip() or "").split(",")
            w  = int(parts[0]) if len(parts) > 0 and parts[0] else 0
            h  = int(parts[1]) if len(parts) > 1 and parts[1] else 0
        except Exception as e:
            self._log(f"Cover probe failed ({path}): {e}", "error")
            return False
        # Already small enough → no-op (idempotency)
        if w and h and max(w, h) <= 720:
            self._log(f"Cover already normalized ({w}x{h}): {path}")
            return True
        tmp = path + ".norm.mp4"
        cmd = [
            "ffmpeg", "-y", "-i", path,
            # force_divisible_by=2 guarantees even W/H, which libx264 + yuv420p
            # require. Without it sources like 1920x1074 produce odd output
            # dimensions and FFmpeg fails with `Invalid argument`.
            "-vf", "scale=720:720:force_original_aspect_ratio=decrease:force_divisible_by=2,fps=24",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
            "-pix_fmt", "yuv420p",
            "-g", "24", "-keyint_min", "24",  # one keyframe per second
            "-movflags", "+faststart",
            "-an",
            tmp,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:
            err_tail = (err or b"")[-400:].decode("utf-8", errors="replace")
            self._log(f"Cover normalize failed ({path}): {err_tail}", "error")
            try: os.remove(tmp)
            except Exception: pass
            return False
        try:
            os.replace(tmp, path)
        except Exception as e:
            self._log(f"Cover normalize replace failed ({path}): {e}", "error")
            return False
        self._log(f"Cover normalized ({w}x{h} → ≤720): {os.path.basename(path)}")
        return True

    async def _normalize_cover_image(self, path: str) -> bool:
        """Re-encode an over-sized cover JPG/PNG to a stream-friendly form.

        Suno covers ship at up to 2194×2194 yuvj444p mjpeg (~6–10 MB). The
        playlist FFmpeg loops each cover ≈25× per song through scale+crop+fps;
        decoding a multi-MB 4:4:4 JPEG that often causes audible audio/video
        stutter at every loop boundary. We trim every cover to a uniform,
        lightweight baseline:

          - Max 720x720 (Lanczos downscale, preserves aspect)
          - yuvj420p (matches the rest of the pipeline; libx264-friendly)
          - JPEG q=4 (≈visually lossless thumbnail size, ~50–150 KB)

        Returns True on success. Files already ≤720 on both sides are skipped
        (idempotent). Files that don't exist or fail probing are no-ops.
        """
        if not path or not os.path.exists(path):
            return False
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffprobe", "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "csv=p=0", path,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await proc.communicate()
            parts = (out.decode().strip() or "").split(",")
            w = int(parts[0]) if len(parts) > 0 and parts[0] else 0
            h = int(parts[1]) if len(parts) > 1 and parts[1] else 0
        except Exception as e:
            self._log(f"Cover image probe failed ({path}): {e}", "error")
            return False
        if w and h and max(w, h) <= 720:
            return True
        tmp = path + ".norm.jpg"
        cmd = [
            "ffmpeg", "-y", "-i", path,
            "-vf", "scale=720:720:force_original_aspect_ratio=decrease:force_divisible_by=2,format=yuvj420p",
            "-q:v", "4",
            tmp,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode != 0:
            err_tail = (err or b"")[-400:].decode("utf-8", errors="replace")
            self._log(f"Cover image normalize failed ({path}): {err_tail}", "error")
            try: os.remove(tmp)
            except Exception: pass
            return False
        try:
            os.replace(tmp, path)
        except Exception as e:
            self._log(f"Cover image normalize replace failed ({path}): {e}", "error")
            return False
        self._log(f"Cover image normalized ({w}x{h} → ≤720): {os.path.basename(path)}")
        return True

    async def renormalize_cover(self, song: dict) -> tuple[bool, str]:
        """Manual entry point for the per-song UI button.

        If the cached MP4 exists, re-encodes it in place. Otherwise downloads
        it fresh (which also normalizes via _get_video).
        Returns (ok, message)."""
        uuid = song.get("suno_uuid") or ""
        if not uuid:
            return False, "Song has no suno_uuid."
        cache_dir = os.path.join(self.trya_stream_dir, "cover_cache")
        mp4 = os.path.join(cache_dir, f"{uuid}.mp4")
        jpg = os.path.join(cache_dir, f"{uuid}.jpg")
        if os.path.exists(mp4):
            ok = await self._normalize_cover_video(mp4)
            return ok, ("Cover video normalized." if ok
                        else "Video normalization failed — see logs.")
        if os.path.exists(jpg):
            ok = await self._normalize_cover_image(jpg)
            return ok, ("Cover image normalized." if ok
                        else "Image normalization failed — see logs.")
        # No cached file — try a fresh download (also normalizes)
        path = await self._get_video(song) or await self._get_cover(song)
        if path:
            return True, "Cover downloaded and normalized."
        return False, "No usable cover URL available."

    # ── Concat-all loop video builder ─────────────────────────────────────────

    async def _select_random_loop_video_subset(self, loop_vids: list, count: int) -> list:
        """Draw a random subset while cycling through every uploaded video."""
        filenames = [video.get("filename") for video in loop_vids if video.get("filename")]
        available = set(filenames)
        state_raw = await self.db.get_setting("trya_stream_loop_random_rotation") or "{}"
        try:
            state = json.loads(state_raw)
            if not isinstance(state, dict):
                state = {}
        except (TypeError, ValueError, json.JSONDecodeError):
            state = {}

        known_values = state.get("known", [])
        if not isinstance(known_values, list):
            known_values = []
        remaining_values = state.get("remaining", [])
        if not isinstance(remaining_values, list):
            remaining_values = []
        known = {
            filename for filename in known_values
            if isinstance(filename, str)
        }
        remaining = []
        seen = set()
        for filename in remaining_values:
            if filename in available and filename not in seen:
                remaining.append(filename)
                seen.add(filename)

        new_filenames = [filename for filename in filenames if filename not in known]
        random.shuffle(new_filenames)
        remaining.extend(filename for filename in new_filenames if filename not in seen)

        selected = []
        target = max(1, min(int(count), len(filenames)))
        while len(selected) < target:
            if not remaining:
                remaining = [filename for filename in filenames if filename not in selected]
                random.shuffle(remaining)
                if not remaining:
                    break
            filename = remaining.pop(0)
            if filename not in selected:
                selected.append(filename)

        await self.db.set_setting(
            "trya_stream_loop_random_rotation",
            json.dumps({"known": filenames, "remaining": remaining}),
        )
        videos_by_filename = {video.get("filename"): video for video in loop_vids}
        return [videos_by_filename[filename] for filename in selected]

    async def _video_decodes_cleanly(self, path: str) -> tuple[bool, str]:
        """Fully decode a generated loop video before exposing it to a stream."""
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-v", "error", "-i", path,
            "-map", "0:v:0", "-f", "null", "-",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        error = stderr.decode("utf-8", errors="replace").strip()
        return proc.returncode == 0 and not error, error[-2000:]

    async def _build_concat_all_video(self, loop_vids: list, random_order: bool = False) -> str | None:
        """Concatenate every uploaded loop video into one MP4.

        The ordered result is cached in assets/_concat_all.mp4. Random-order
        builds use a separate output file and are rebuilt on each stream setup.
        A sidecar hash file records the input file signatures and FFmpeg runtime
        so the ordered file is rebuilt automatically whenever videos are
        added/removed or the container's FFmpeg version changes."""
        import hashlib
        assets   = os.path.join(self.trya_stream_dir, "assets")
        stem = "_concat_all_random" if random_order else "_concat_all"
        out_path = os.path.join(assets, f"{stem}.mp4")
        hash_file = os.path.join(assets, f"{stem}.hash")

        paths = [os.path.join(assets, v["filename"]) for v in loop_vids]
        missing = [p for p in paths if not os.path.exists(p)]
        if missing:
            self._log(f"Concat-all: missing files: {missing}", "error")
            return None

        builder_version = "concat_all_filter_v3_720p_cfr30"
        file_sig_parts = []
        total_duration = 0.0
        for p in paths:
            try:
                st = os.stat(p)
                file_sig_parts.append(f"{os.path.basename(p)}:{st.st_size}:{int(st.st_mtime)}")
            except OSError:
                file_sig_parts.append(os.path.basename(p))
            try:
                probe = await asyncio.create_subprocess_exec(
                    "ffprobe", "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    p,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                out, _ = await probe.communicate()
                total_duration += max(0.0, float((out.decode().strip() or "0")))
            except Exception:
                pass
        vid_sig = "|".join(file_sig_parts if random_order else sorted(file_sig_parts))
        ffmpeg_sig = "ffmpeg:unknown"
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
            ffmpeg_sig = (out.decode("utf-8", errors="replace").splitlines() or [ffmpeg_sig])[0]
        except Exception:
            pass
        cur_hash = hashlib.md5(f"{builder_version}|{vid_sig}|{ffmpeg_sig}".encode()).hexdigest()

        # Cache hit? Random-order mode intentionally rebuilds every stream setup.
        if not random_order and os.path.exists(out_path) and os.path.exists(hash_file):
            try:
                with open(hash_file) as fh:
                    if fh.read().strip() == cur_hash:
                        valid, error = await self._video_decodes_cleanly(out_path)
                        if valid:
                            self._log("Concat-all loop video: validated cache hit.")
                            return out_path
                        self._log(
                            f"Concat-all cache is damaged and will be rebuilt: {error}",
                            "error",
                        )
            except Exception:
                pass

        order_note = "random-order " if random_order else ""
        self._log(f"Building {order_note}concat-all loop video ({len(paths)} clips, 720p CFR 30)…")
        cmd = ["ffmpeg", "-y", "-nostats", "-progress", "pipe:2"]
        for p in paths:
            cmd += ["-fflags", "+genpts", "-i", p]

        filters = []
        concat_in = []
        for i in range(len(paths)):
            filters.append(
                f"[{i}:v]scale=1280:720:force_original_aspect_ratio=decrease,"
                f"pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=black,"
                f"fps=30,setsar=1,setpts=PTS-STARTPTS[v{i}]"
            )
            concat_in.append(f"[v{i}]")
        filters.append(f"{''.join(concat_in)}concat=n={len(paths)}:v=1:a=0[vout]")

        # Every builder gets a private output. The previous fixed .tmp.mp4
        # allowed overlapping manual/scheduled starts to corrupt each other.
        tmp_out = os.path.join(
            assets,
            f".{stem}.{os.getpid()}.{time.time_ns()}.tmp.mp4",
        )
        cmd += [
            "-filter_complex", ";".join(filters),
            "-map", "[vout]",
            "-an",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
            "-pix_fmt", "yuv420p",
            "-r", "30",
            "-g", "60", "-keyint_min", "60", "-sc_threshold", "0",
            "-video_track_timescale", "30000",
            "-movflags", "+faststart",
            tmp_out,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        err_tail = deque(maxlen=40)
        progress_pending = {}
        last_pct = -1
        last_progress_log = 0.0
        while True:
            raw = await proc.stderr.readline() if proc.stderr else b""
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            if "=" in line:
                key, value = line.split("=", 1)
                if key in _FFMPEG_PROGRESS_KEYS:
                    if key == "progress":
                        out_ms = progress_pending.get("out_time_ms")
                        if out_ms and total_duration > 0:
                            try:
                                pct = min(99, int((float(out_ms) / 1_000_000.0) / total_duration * 100))
                                now = time.time()
                                if pct >= last_pct + 10 or now - last_progress_log >= 20:
                                    self._log(f"Concat-all build progress: {pct}%")
                                    last_pct = pct
                                    last_progress_log = now
                            except (TypeError, ValueError):
                                pass
                        progress_pending = {}
                    else:
                        progress_pending[key] = value.strip()
                    continue
            err_tail.append(line)
        await proc.wait()
        if proc.returncode != 0:
            self._log(f"Concat-all build failed: {' | '.join(err_tail)}", "error")
            try: os.remove(tmp_out)
            except Exception: pass
            return None
        valid, validation_error = await self._video_decodes_cleanly(tmp_out)
        if not valid:
            self._log(
                f"Concat-all validation failed: {validation_error or 'video could not be decoded'}",
                "error",
            )
            try: os.remove(tmp_out)
            except Exception: pass
            return None
        try:
            os.replace(tmp_out, out_path)
        except Exception as e:
            self._log(f"Concat-all replace failed: {e}", "error")
            try: os.remove(tmp_out)
            except Exception: pass
            return None

        with open(hash_file, "w") as fh:
            fh.write(cur_hash)
        label = "Random-order concat-all" if random_order else "Concat-all"
        self._log(f"{label} loop video ready ({len(paths)} clips combined, 720p CFR 30).")
        return out_path

    # ── Combined ASS builder ───────────────────────────────────────────────────

    def _build_combined_ass(
        self,
        songs: list,
        show_progress: bool = False,
        progress_total_count: int | None = None,
        progress_index_offset: int = 0,
        progress_extra_duration: float = 0.0,
        stream_title_enabled: bool = False,
        stream_title_text: str = "",
        disclaimer_enabled: bool = False,
        disclaimer_text: str = "",
    ) -> str | None:
        """Merge all per-song ASS files into one with time offsets.
        Adds a NowPlaying title card at the start of each song.
        When show_progress=True also adds a bottom-right card with
        song duration and playlist position (e.g. '4:33  ·  Song 9/30').
        An enabled title remains visible above the optional disclaimer and
        progress line."""
        ass_dir = os.path.join(self.trya_stream_dir, "ass")
        out_path = os.path.join(self.trya_stream_dir, "_combined.ass")

        header = (
            "[Script Info]\n"
            "ScriptType: v4.00+\n"
            "PlayResX: 1920\n"
            "PlayResY: 1080\n\n"
            "[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
            "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding\n"
            "Style: Default,Noto Sans,52,&H00FFFFFF,&H000000FF,&H00000000,"
            "&H80000000,-1,0,0,0,100,100,0,0,1,2.5,1,2,10,10,30,1\n"
            "Style: NowPlaying,Noto Sans,48,&H00E8C97A,&H000000FF,&H00000000,"
            "&HC8000000,0,0,0,0,100,100,0,0,1,1.5,0.8,7,20,20,14,1\n"
            "Style: Progress,Noto Sans,34,&H00FFFFFF,&H000000FF,&H00000000,"
            "&HA0000000,0,0,0,0,100,100,0,0,1,1.2,0.5,3,10,30,18,1\n"
            "Style: StreamTitle,Noto Sans,48,&H00E8C97A,&H000000FF,&H00000000,"
            "&HC8000000,0,0,0,0,100,100,0,0,1,1.5,0.8,3,20,30,72,1\n"
            "Style: Disclaimer,Noto Sans,28,&H00FFFFFF,&H000000FF,&H00000000,"
            "&HA8000000,0,0,0,0,100,100,0,0,3,1.0,0,3,1050,30,72,1\n\n"
            "[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        )

        events = []
        offset_cs = 0
        has_any = False
        n_songs = progress_total_count or len(songs)

        for song_idx, song in enumerate(songs):
            dur = song.get("duration") or 300
            dur_cs = int(dur * 100)
            t_start = _cs_to_ts(offset_cs)
            t_end   = _cs_to_ts(offset_cs + dur_cs)

            # NowPlaying card: top-left, full song duration, fades in/out
            title  = _strip_emoji(song.get("title")  or "Unknown").replace("\\", "\\\\").replace("{", "\\{")
            artist = _strip_emoji(song.get("artist") or "").replace("\\", "\\\\").replace("{", "\\{")
            label  = f"\\N{artist}" if artist else ""
            events.append(
                f"Dialogue: 2,{t_start},{t_end},NowPlaying,,0,0,0,,"
                f"{{\\pos(20,20)\\an7\\fad(600,600)}}♪  {title}{label}  ♪"
            )

            # Progress info card: bottom-right, e.g. "4:33  ·  Song 9/30  ·  ~1h 12m left"
            if show_progress:
                dur_mins = int(dur) // 60
                dur_secs = int(dur) % 60
                dur_str  = f"{dur_mins}:{dur_secs:02d}"
                remaining_secs = int(
                    sum(s.get("duration") or 300 for s in songs[song_idx:])
                    + progress_extra_duration
                )
                if remaining_secs >= 3600:
                    r_h = remaining_secs // 3600
                    r_m = (remaining_secs % 3600) // 60
                    rem_str = f"~{r_h}h {r_m}m left"
                elif remaining_secs >= 60:
                    rem_str = f"~{remaining_secs // 60}m left"
                else:
                    rem_str = f"~{remaining_secs}s left"
                info = f"{dur_str}  \u00b7  Song {progress_index_offset + song_idx + 1}/{n_songs}  \u00b7  {rem_str}"
                events.append(
                    f"Dialogue: 1,{t_start},{t_end},Progress,,0,0,0,,"
                    f"{{\\fad(400,400)}}{info}"
                )

            # Per-song subtitle lines (shifted)
            ass_fn = song.get("ass_filename")
            if ass_fn:
                ass_path = os.path.join(ass_dir, ass_fn)
                if os.path.exists(ass_path):
                    try:
                        with open(ass_path, encoding="utf-8") as f:
                            content = f.read()
                        events.extend(_shift_ass_dialogue(content, offset_cs))
                        has_any = True
                    except Exception as e:
                        print(f"[trya-stream] ASS read error ({ass_fn}): {e}", flush=True)

            offset_cs += dur_cs

        if disclaimer_enabled and disclaimer_text:
            disclaimer = disclaimer_text.replace("\\", "\\\\")
            disclaimer = disclaimer.replace("{", "\\{").replace("}", "\\}")
            disclaimer = disclaimer.replace("\r\n", "\\N").replace("\r", "\\N").replace("\n", "\\N")
            events.append(
                f"Dialogue: 3,{_cs_to_ts(0)},{_cs_to_ts(offset_cs)},Disclaimer,,0,0,0,,"
                f"{{\\fad(300,300)}}{disclaimer}"
            )

        if stream_title_enabled and stream_title_text:
            stream_title = _strip_emoji(stream_title_text).replace("\\", "\\\\")
            stream_title = stream_title.replace("{", "\\{").replace("}", "\\}")
            # Keep the title above every visible disclaimer line while using
            # the same 30 px right margin as the other bottom-right overlays.
            disclaimer_lines = 0
            if disclaimer_enabled and disclaimer_text:
                disclaimer_lines = max(1, len(disclaimer_text.splitlines()))
            title_margin_v = 72 + (disclaimer_lines * 36) + (16 if disclaimer_lines else 0)
            events.append(
                f"Dialogue: 4,{_cs_to_ts(0)},{_cs_to_ts(offset_cs)},StreamTitle,,0,0,{title_margin_v},,"
                f"{{\\fad(300,300)}}{stream_title}"
            )

        try:
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(header)
                for e in events:
                    f.write(_assign_ass_symbol_fonts(e) + "\n")
            return out_path
        except Exception as e:
            print(f"[trya-stream] Combined ASS write error: {e}", flush=True)
            return None

    # ── FFmpeg command builder ─────────────────────────────────────────────────

    def _build_playlist_cmd(
        self,
        songs: list,
        media_paths: list,
        bg_path: str | None,
        bg_type: str,
        loop_path: str | None,
        obs_overlay_path: str | None,
        obs_overlay_fps: int,
        video_bitrate_kbps: int,
        ass_path: str | None,
        twitch_key: str,
        total_dur: float,
        legacy_pipeline: bool = False,
        audio_concat_file: str | None = None,
        media_style: dict | None = None,
        output_url: str | None = None,
        audio_bitrate_kbps: int = 128,
        realtime_audio: bool = False,
    ) -> list:
        """Build ONE FFmpeg command for the full playlist.

        Layer stack (bottom → top):
          0: Background (static image or looping video)  1920×1080
          1: Local loop overlay, 650×366, top-right
          2: Optional OBS RTMP override, 650×366, top-right
          3: Song media concat (9:16 video or square cover), bottom-left inset
          4: Combined ASS subtitles (lyrics + NowPlaying title cards)
        """
        W, H = _W, _H
        cmd = ["ffmpeg", "-y"]
        if not legacy_pipeline:
            cmd += ["-nostats", "-progress", "pipe:2"]

        input_idx = 0
        bg_input = lv_input = obs_input = audio_input = None

        # Background
        if bg_path:
            if bg_type == "video":
                cmd += ["-stream_loop", "-1", "-re", "-i", bg_path]
            else:
                cmd += ["-loop", "1", "-i", bg_path]
            bg_input = input_idx; input_idx += 1

        # Local loop overlay
        if loop_path:
            cmd += ["-stream_loop", "-1", "-re", "-i", loop_path]
            lv_input = input_idx; input_idx += 1

        # Optional OBS override bridge. This is a continuous RGBA stream:
        # transparent when OBS is absent, live frames when OBS is connected.
        if obs_overlay_path:
            cmd += [
                "-thread_queue_size", "512",
                "-f", "rawvideo", "-pix_fmt", "rgba",
                "-s", f"{_OBS_BRIDGE_W}x{_OBS_BRIDGE_H}",
                "-r", str(obs_overlay_fps),
                "-i", obs_overlay_path,
            ]
            obs_input = input_idx; input_idx += 1

        # Per-song media inputs (each trimmed to song duration)
        media_inputs = []
        for song, path in zip(songs, media_paths):
            dur = song.get("duration") or 300
            if path and path.endswith(".mp4"):
                cmd += ["-stream_loop", "-1", "-t", str(dur + 0.5), "-i", path]
            elif path:
                cmd += ["-loop", "1", "-t", str(dur + 0.5), "-i", path]
            else:
                cmd += ["-f", "lavfi", "-t", str(dur + 0.5),
                        "-i", f"color=size={_INSET_W}x{_INSET_H}:color=black:rate={_FPS}"]
            media_inputs.append(input_idx); input_idx += 1

        audio_inputs = []
        if legacy_pipeline:
            if not audio_concat_file:
                raise ValueError("audio_concat_file is required for legacy stream mode")
            cmd += ["-f", "concat", "-safe", "0", "-i", audio_concat_file]
            audio_input = input_idx; input_idx += 1
        else:
            # Per-song audio inputs. Keeping audio in the same filtergraph as
            # the cover concat gives every song boundary a fresh timestamp
            # origin, avoiding MP3 concat-demuxer timestamp jumps in Twitch's
            # live player.
            for song in songs:
                dur = song.get("duration") or 300
                mp3 = os.path.join(self.trya_stream_dir, "mp3", song["mp3_filename"])
                if realtime_audio:
                    cmd += ["-re"]
                cmd += ["-t", str(dur + 0.5), "-i", mp3]
                audio_inputs.append(input_idx); input_idx += 1

        # ── Filtergraph ────────────────────────────────────────────────────────
        filters = []
        media_style = media_style or {}
        rounded_media = bool(media_style.get("enabled"))
        corner_radius = max(1, min(120, int(media_style.get("radius") or 28)))
        border_enabled = bool(media_style.get("border_enabled"))
        border_width = max(1, min(20, int(media_style.get("border_width") or 3)))
        border_color = str(media_style.get("border_color") or "#A855F7")

        # Background
        if bg_input is not None:
            filters.append(
                f"[{bg_input}:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
                f"crop={W}:{H},fps={_FPS}[bg]"
            )
        else:
            filters.append(f"color=size={W}x{H}:color=0x111111:rate={_FPS}[bg]")
        last = "[bg]"

        # Loop video overlay – top-right, 650×366 (16:9)
        if lv_input is not None:
            if rounded_media:
                filters.append(
                    f"[{lv_input}:v]scale={_LOOP_OVERLAY_W}:{_LOOP_OVERLAY_H}:"
                    f"force_original_aspect_ratio=increase,crop={_LOOP_OVERLAY_W}:{_LOOP_OVERLAY_H},"
                    f"setsar=1,fps={_FPS}[lv_base]"
                )
                filters.extend(_rounded_media_filters(
                    "[lv_base]", "[lv]",
                    label_prefix="lv_style",
                    width=_LOOP_OVERLAY_W,
                    height=_LOOP_OVERLAY_H,
                    fps=_FPS,
                    radius=corner_radius,
                    border_enabled=border_enabled,
                    border_width=border_width,
                    border_color=border_color,
                ))
            else:
                filters.append(
                    f"[{lv_input}:v]scale={_LOOP_OVERLAY_W}:{_LOOP_OVERLAY_H}:"
                    f"force_original_aspect_ratio=decrease,fps={_FPS}[lv]"
                )
            filters.append(
                f"{last}[lv]overlay=x={W}-{_LOOP_OVERLAY_W}-20:y=20:shortest=0:eof_action=pass[after_lv]"
            )
            last = "[after_lv]"

        # OBS RTMP override. Transparent bridge frames leave the local loop
        # visible; live OBS frames cover it in the same position.
        if obs_input is not None:
            filters.append(
                f"[{obs_input}:v]scale={_LOOP_OVERLAY_W}:{_LOOP_OVERLAY_H}:flags=bicubic,format=rgba[obs_base]"
            )
            if rounded_media:
                filters.extend(_rounded_media_filters(
                    "[obs_base]", "[obs]",
                    label_prefix="obs_style",
                    width=_LOOP_OVERLAY_W,
                    height=_LOOP_OVERLAY_H,
                    fps=obs_overlay_fps,
                    radius=corner_radius,
                    border_enabled=border_enabled,
                    border_width=border_width,
                    border_color=border_color,
                    preserve_input_alpha=True,
                ))
            else:
                filters.append("[obs_base]copy[obs]")
            filters.append(
                f"{last}[obs]overlay=x={W}-{_LOOP_OVERLAY_W}-20:y=20:shortest=0:eof_action=pass[after_obs]"
            )
            last = "[after_obs]"

        # Square media is pre-rendered during submission processing. The live
        # graph only performs a cheap fit/pad fallback if that cache is unavailable.
        n = len(songs)
        for i, (song, mid) in enumerate(zip(songs, media_inputs)):
            dur = song.get("duration") or 300
            filters.append(
                f"[{mid}:v]scale={_INSET_W}:{_INSET_H}:force_original_aspect_ratio=decrease:"
                f"force_divisible_by=2,pad={_INSET_W}:{_INSET_H}:(ow-iw)/2:(oh-ih)/2:black,"
                f"setsar=1,fps={_FPS},trim=0:{dur},setpts=PTS-STARTPTS[cv_base{i}]"
            )
        concat_in = "".join(f"[cv_base{i}]" for i in range(n))
        filters.append(f"{concat_in}concat=n={n}:v=1:a=0[covers_base]")
        if rounded_media:
            filters.extend(_rounded_media_filters(
                "[covers_base]", "[covers]",
                label_prefix="song_style",
                width=_INSET_W,
                height=_INSET_H,
                fps=_FPS,
                radius=corner_radius,
                border_enabled=border_enabled,
                border_width=border_width,
                border_color=border_color,
            ))
        else:
            filters.append("[covers_base]copy[covers]")

        if not legacy_pipeline:
            audio_concat_in = []
            for i, (song, aid) in enumerate(zip(songs, audio_inputs)):
                dur = song.get("duration") or 300
                filters.append(
                    f"[{aid}:a]atrim=0:{dur},asetpts=PTS-STARTPTS,"
                    f"aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}]"
                )
                audio_concat_in.append(f"[a{i}]")
            filters.append(f"{''.join(audio_concat_in)}concat=n={n}:v=0:a=1[aout]")
        filters.append(
            f"{last}[covers]overlay=x=20:y={H}-{_INSET_H}-20:shortest=0[after_cv]"
        )
        last = "[after_cv]"

        # Combined ASS subtitles
        if ass_path:
            esc = ass_path.replace("\\", "/").replace(":", "\\:")
            filters.append(f"{last}subtitles='{esc}'[vout]")
        else:
            filters.append(f"{last}copy[vout]")

        cmd += ["-filter_complex", ";".join(filters)]
        if legacy_pipeline:
            cmd += ["-map", "[vout]", "-map", f"{audio_input}:a"]
        else:
            cmd += ["-map", "[vout]", "-map", "[aout]"]
        cmd += ["-t", str(total_dur + 2)]

        # ── Encode ─────────────────────────────────────────────────────────────
        if legacy_pipeline:
            cmd += [
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
                "-pix_fmt", "yuv420p", "-g", str(_FPS * 2), "-keyint_min", str(_FPS),
                "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
                "-f", "flv", f"{_LEGACY_RTMP_BASE}{twitch_key}",
            ]
            return cmd

        # Twitch is much happier with predictable H.264 than CRF-only output,
        # especially around playlist source changes. Keep keyframes exactly
        # 2s apart and avoid FLV duration/file-size metadata on live RTMP.
        video_bitrate = f"{video_bitrate_kbps}k"
        video_bufsize = f"{video_bitrate_kbps * 2}k"
        audio_bitrate = f"{max(96, min(320, int(audio_bitrate_kbps)))}k"
        stream_target = output_url or f"{_RTMP_BASE}{twitch_key}"
        cmd += [
            "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
            "-b:v", video_bitrate, "-maxrate", video_bitrate, "-bufsize", video_bufsize,
            "-x264-params", "nal-hrd=cbr:force-cfr=1",
            "-pix_fmt", "yuv420p", "-r", str(_FPS),
            "-g", str(_FPS * 2), "-keyint_min", str(_FPS * 2), "-sc_threshold", "0",
            "-c:a", "aac", "-b:a", audio_bitrate, "-ar", "44100",
            "-rtmp_live", "live",
            "-flvflags", "no_duration_filesize",
            "-f", "flv", stream_target,
        ]
        return cmd
