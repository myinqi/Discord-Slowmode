"""Dedicated FFmpeg publisher for the private TrYa DCS stream."""

import asyncio
import json
import os
import random
import re
import time
from collections import deque

from bot.trya_stream_manager import TryaStreamManager
from bot.trya_dcs_events import trya_dcs_events
from bot.suno_urls import SUNO_UUID_RE, UUID_RE


_DCS_LOG_BUFFER = deque(maxlen=2000)


def log_dcs_event(line: str, level: str = "info") -> None:
    _DCS_LOG_BUFFER.append((time.time(), level, str(line)))
    print(f"[trya-dcs] {line}", flush=True)


def get_dcs_log(since_ts: float = 0.0, max_age_secs: float = 3600.0) -> list[dict]:
    cutoff = max(float(since_ts or 0), time.time() - max_age_secs)
    return [
        {"ts": ts, "level": level, "line": line}
        for ts, level, line in _DCS_LOG_BUFFER
        if ts > cutoff
    ]


class TryaDcsManager(TryaStreamManager):
    """Publish DCS songs to MediaMTX without Twitch runtime dependencies."""

    def __init__(self, db, base_dir: str):
        super().__init__(db, base_dir)
        self._output_url = ""
        self._regular_playlist: list[dict] = []
        self._intro_song: dict | None = None
        self._outro_song: dict | None = None

    def _log(self, line: str, level: str = "info") -> None:
        log_dcs_event(line, level)

    @staticmethod
    def _public_suno_url(song: dict | None) -> str:
        if not song:
            return ""
        match = SUNO_UUID_RE.search(str(song.get("suno_url") or ""))
        if not match:
            match = UUID_RE.fullmatch(str(song.get("suno_uuid") or "").strip())
        return f"https://suno.com/song/{match.group(1).lower()}" if match else ""

    @staticmethod
    def _public_wlm_url(song: dict | None) -> str:
        if not song:
            return ""
        match = re.fullmatch(
            r"https://www\.welovemusic\.ai/track/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})/?",
            str(song.get("wlm_url") or "").strip(),
        )
        return (
            f"https://www.welovemusic.ai/track/{match.group(1).lower()}"
            if match else ""
        )

    def _obs_listen_port(self) -> int:
        return 1937

    def _set_obs_overlay_status(
        self, enabled: bool, state: str, label: str, mode: str = "local"
    ) -> None:
        previous = getattr(self, "_obs_overlay_status", {}).get("state")
        super()._set_obs_overlay_status(enabled, state, label, mode)
        if state != previous:
            event_name = (
                "radio.obs_online" if state in {"connected", "live"} else "radio.obs_offline"
            )
            try:
                asyncio.get_running_loop().create_task(
                    trya_dcs_events.publish(event_name, {
                        "enabled": enabled,
                        "state": state,
                        "label": label,
                    })
                )
            except RuntimeError:
                pass

    def get_log(self, since_ts: float = 0.0, max_age_secs: float = 600.0) -> list[dict]:
        return get_dcs_log(since_ts=since_ts, max_age_secs=max_age_secs)

    async def start(self, *, created_by: str = "admin") -> dict:
        async with self._start_lock:
            if self.is_running:
                return {"ok": False, "error": "The DCS stream is already running."}
            if (await self.db.get_setting("trya_dcs_enabled") or "off") != "on":
                return {"ok": False, "error": "Enable TrYa DCS before starting the stream."}

            songs = await self.db.get_trya_dcs_songs(active_only=True)
            moderation_enabled = (
                await self.db.get_setting("trya_dcs_moderation_enabled") or "off"
            ) == "on"
            ready = []
            for stored in songs:
                filename = stored.get("mp3_filename")
                path = os.path.join(self.trya_stream_dir, "mp3", filename or "")
                if (
                    stored.get("analysis_status") == "done"
                    and (
                        not moderation_enabled
                        or stored.get("approval_status") == "approved"
                    )
                    and filename
                    and os.path.isfile(path)
                ):
                    song = dict(stored)
                    actual = await self._probe_audio_duration(song)
                    if actual and actual > 0:
                        song["duration"] = actual
                        if abs(float(stored.get("duration") or 0) - actual) > 1:
                            await self.db.update_trya_dcs_song(song["id"], duration=actual)
                    ready.append(song)
            regular = [
                song for song in ready
                if (song.get("playlist_source") or "submission") not in {"intro", "outro"}
            ]
            intro_pool = [song for song in ready if song.get("playlist_source") == "intro"]
            outro_pool = [song for song in ready if song.get("playlist_source") == "outro"]
            if not regular:
                requirement = "approved and analyzed" if moderation_enabled else "analyzed"
                return {"ok": False, "error": f"No {requirement} DCS playlist songs are ready."}

            def select_special(pool: list[dict], selection: str) -> dict | None:
                if not pool:
                    return None
                if selection and selection != "random":
                    try:
                        selected_id = int(selection)
                    except (TypeError, ValueError):
                        selected_id = 0
                    selected = next(
                        (song for song in pool if int(song.get("id") or 0) == selected_id),
                        None,
                    )
                    if selected:
                        return selected
                return random.choice(pool)

            intro_enabled = (await self.db.get_setting("trya_dcs_intro_enabled") or "off") == "on"
            outro_enabled = (await self.db.get_setting("trya_dcs_outro_enabled") or "off") == "on"
            self._intro_song = select_special(
                intro_pool,
                await self.db.get_setting("trya_dcs_intro_selection") or "random",
            ) if intro_enabled else None
            self._outro_song = select_special(
                outro_pool,
                await self.db.get_setting("trya_dcs_outro_selection") or "random",
            ) if outro_enabled else None

            random.shuffle(regular)
            self._regular_playlist = regular
            mode = await self.db.get_setting("trya_dcs_loop_mode") or "stop"
            first_playlist = list(regular)
            if self._intro_song:
                first_playlist.insert(0, self._intro_song)
                self._log(f"Intro song: {self._intro_song.get('title') or self._intro_song['id']}")
            if self._outro_song and mode != "reshuffle":
                first_playlist.append(self._outro_song)
                self._log(f"Outro song: {self._outro_song.get('title') or self._outro_song['id']}")

            self.playlist = first_playlist
            self._progress_total_count = len(first_playlist)
            self._current_song_index = 1
            self.current_song = first_playlist[0]
            self._current_song_end_time = time.monotonic() + float(
                first_playlist[0].get("duration") or 300
            )
            self._safe_stop_requested = False
            self._output_url = (
                await self.db.get_setting("trya_dcs_rtmp_ingest_url")
                or "rtmp://mediamtx:1935/trya-dcs"
            ).strip()
            if not self._output_url.startswith(("rtmp://", "rtmps://")):
                return {"ok": False, "error": "The configured MediaMTX ingest URL is invalid."}

            snapshot = [
                {
                    "id": int(song["id"]),
                    "suno_uuid": song.get("suno_uuid"),
                    "title": song.get("title"),
                    "artist": song.get("artist"),
                    "duration": song.get("duration"),
                }
                for song in first_playlist
            ]
            await self.db.save_trya_dcs_playlist_snapshot(
                created_by=created_by,
                mode="manual",
                songs=snapshot,
            )
            self._stream_ready_event.clear()
            self._reset_ffmpeg_health("starting")
            self.is_running = True
            self._task = asyncio.create_task(self._stream_loop())
            self._log(f"Starting {len(first_playlist)} songs toward MediaMTX.")
            await trya_dcs_events.publish("radio.mode", {
                "mode": "starting",
                "playlist_length": len(first_playlist),
            })
            await trya_dcs_events.publish("radio.queue_update", {
                "songs": snapshot,
            })
            return {"ok": True, "song_count": len(first_playlist)}

    async def stop(self) -> dict:
        self.is_running = False
        self._safe_stop_requested = False
        self._stream_ready_event.clear()
        process = self._process
        if process and process.returncode is None:
            try:
                process.terminate()
                await asyncio.wait_for(process.wait(), timeout=10)
            except Exception:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
        self._process = None
        await self._stop_obs_overlay_bridge()
        self._set_obs_overlay_status(False, "disabled", "OBS overlay disabled", "local")
        task = self._task
        self._task = None
        if task and task is not asyncio.current_task():
            task.cancel()
        self.current_song = None
        self._current_song_index = 0
        self._reset_ffmpeg_health("offline")
        self._log("Stopped.")
        await trya_dcs_events.publish("radio.mode", {"mode": "offline"})
        return {"ok": True}

    async def safe_stop(self) -> dict:
        if not self.is_running:
            return {"ok": False, "error": "The DCS stream is not running."}
        if self._safe_stop_requested:
            return {"ok": False, "error": "Safe stop is already pending."}
        self._safe_stop_requested = True
        self._log("Safe stop requested; the current song will finish.")
        asyncio.create_task(self._safe_stop_waiter())
        return {"ok": True}

    async def _safe_stop_waiter(self) -> None:
        remaining = self._current_song_end_time - time.monotonic()
        if remaining > 0:
            await asyncio.sleep(remaining + 0.5)
        if not self._safe_stop_requested or not self.is_running:
            return
        process = self._process
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=10)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        if self._outro_song and (
            not self.current_song
            or int(self.current_song.get("id") or 0) != int(self._outro_song.get("id") or 0)
        ):
            self._log(f"Safe stop: playing outro {self._outro_song.get('title') or self._outro_song['id']}.")
            await self._play_dcs_playlist([self._outro_song])
        if self.is_running:
            await self.stop()

    async def get_status(self) -> dict:
        status = await super().get_status()
        status["output_url"] = self._output_url
        status["listener_count"] = trya_dcs_events.listener_count
        if status.get("song") and self.current_song:
            status["song"]["submitted_by"] = self.current_song.get("user_name") or ""
            status["song"]["suno_url"] = self._public_suno_url(self.current_song)
            status["song"]["wlm_url"] = self._public_wlm_url(self.current_song)
            status["song"]["duration"] = float(self.current_song.get("duration") or 0)
        status["playlist"] = [
            {
                "title": song.get("title") or "Unknown title",
                "artist": song.get("artist") or "Unknown artist",
                "duration": float(song.get("duration") or 0),
                "suno_url": self._public_suno_url(song),
                "wlm_url": self._public_wlm_url(song),
            }
            for song in self.playlist
        ] if self.is_running else []
        song_remaining = (
            max(0.0, self._current_song_end_time - time.monotonic())
            if self.is_running and self.current_song else 0.0
        )
        upcoming = self.playlist[self._current_song_index:] if self.is_running else []
        status["song_remaining_seconds"] = song_remaining
        status["stream_remaining_seconds"] = song_remaining + sum(
            float(song.get("duration") or 0) for song in upcoming
        )
        return status

    async def _stream_loop(self):
        try:
            while self.is_running:
                await self._play_dcs_playlist(self.playlist)
                if not self.is_running:
                    break
                if self._safe_stop_requested:
                    break
                mode = await self.db.get_setting("trya_dcs_loop_mode") or "stop"
                if mode != "reshuffle":
                    self._log("Playlist finished.")
                    await self.stop()
                    break
                self.playlist = list(self._regular_playlist)
                random.shuffle(self.playlist)
                self._log("Playlist reshuffled for the next rotation; intro is not repeated.")
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._log(f"Publisher failed: {exc}", "error")
            await self.stop()

    async def _resolve_dcs_loop_path(self) -> str | None:
        assets_dir = os.path.join(self.trya_stream_dir, "assets")
        try:
            configured = json.loads(
                await self.db.get_setting("trya_dcs_loop_videos") or "[]"
            )
        except (TypeError, json.JSONDecodeError):
            configured = []
        videos = []
        for item in configured if isinstance(configured, list) else []:
            if not isinstance(item, dict):
                continue
            filename = os.path.basename(str(item.get("filename") or ""))
            path = os.path.join(assets_dir, filename) if filename else ""
            if path and os.path.isfile(path):
                videos.append({"filename": filename, "label": str(item.get("label") or filename)[:100]})
        if not videos:
            self._log("No DCS overlay videos configured; continuing without a top-right local overlay.")
            return None
        selection = (
            await self.db.get_setting("trya_dcs_loop_selection") or "shuffle"
        ).strip()
        selected_filename = ""
        built_path = None
        if selection == "concat_all":
            if len(videos) == 1:
                selected_filename = videos[0]["filename"]
            else:
                built_path = await self._build_concat_all_video(videos)
        elif selection == "concat_all_random":
            if len(videos) == 1:
                selected_filename = videos[0]["filename"]
            else:
                shuffled = list(videos)
                random.shuffle(shuffled)
                built_path = await self._build_concat_all_video(shuffled, random_order=True)
        elif selection == "concat_random_subset":
            try:
                count = int(await self.db.get_setting("trya_dcs_loop_random_count") or "10")
            except (TypeError, ValueError):
                count = 10
            count = max(1, min(count, len(videos)))
            selected = await self._select_random_loop_video_subset(
                videos,
                count,
                rotation_setting_key="trya_dcs_loop_random_rotation",
            )
            self._log(f"DCS overlay random subset: selected {len(selected)} of {len(videos)} videos.")
            if len(selected) == 1:
                selected_filename = selected[0]["filename"]
            else:
                built_path = await self._build_concat_all_video(selected, random_order=True)
        elif selection == "shuffle":
            selected_filename = random.choice(videos)["filename"]
            self._log(f"DCS overlay shuffle selected: {selected_filename}")
        else:
            match = next(
                (video for video in videos if video["filename"] == selection),
                None,
            )
            selected_filename = (match or videos[0])["filename"]
            if match is None:
                self._log(
                    f"Configured DCS overlay is unavailable; using {selected_filename}.",
                    "error",
                )
        if built_path and os.path.isfile(built_path):
            return built_path
        if built_path is None and selected_filename:
            return os.path.join(assets_dir, selected_filename)
        fallback = random.choice(videos)["filename"]
        self._log(f"DCS overlay build failed; using {fallback}.", "error")
        return os.path.join(assets_dir, fallback)

    async def _play_dcs_playlist(self, songs: list[dict]) -> None:
        asset_dir = os.path.join(self.trya_stream_dir, "assets")
        bg_filename = os.path.basename(
            await self.db.get_setting("trya_dcs_bg_filename") or ""
        )
        bg_type = await self.db.get_setting("trya_dcs_bg_type") or "image"
        bg_candidate = os.path.join(asset_dir, bg_filename) if bg_filename else ""
        bg_path = bg_candidate if bg_candidate and os.path.isfile(bg_candidate) else None
        loop_path = await self._resolve_dcs_loop_path()
        corners = (
            await self.db.get_setting("trya_dcs_media_corners_enabled") or "off"
        ) == "on"
        border = (
            await self.db.get_setting("trya_dcs_media_border_enabled") or "off"
        ) == "on"
        radius = await self._bounded_setting(
            "trya_dcs_media_corner_radius", 28, 1, 120
        )
        border_width = await self._bounded_setting(
            "trya_dcs_media_border_width", 3, 1, 20
        )
        border_color = (
            await self.db.get_setting("trya_dcs_media_border_color") or "#A855F7"
        ).strip().upper()
        if not re.fullmatch(r"#[0-9A-F]{6}", border_color):
            border_color = "#A855F7"
        media_style = {
            "enabled": corners,
            "radius": radius,
            "border_enabled": border,
            "border_width": border_width,
            "border_color": border_color,
        }

        obs_overlay_path = None
        obs_overlay_fps = await self._bounded_setting(
            "trya_dcs_obs_fps", 20, 15, 24
        )
        obs_enabled = (await self.db.get_setting("trya_dcs_obs_enabled") or "off") == "on"
        if obs_enabled:
            obs_key = (await self.db.get_setting("trya_dcs_obs_stream_key") or "").strip()
            if obs_key:
                obs_overlay_path = await self._start_obs_overlay_bridge(obs_key, obs_overlay_fps)
                self._log(
                    f"OBS contribution enabled on port {self._obs_listen_port()} ({obs_overlay_fps} fps)."
                )
            else:
                self._set_obs_overlay_status(
                    True, "missing_key", "OBS stream key missing", "rtmp"
                )
                self._log("OBS contribution is enabled but no stream key is configured.", "error")
        else:
            self._set_obs_overlay_status(False, "disabled", "OBS overlay disabled", "local")

        media_paths = []
        for song in songs:
            path = await self._get_video(song) or await self._get_cover(song)
            media_paths.append(path)
            self._log(f"Media ready: {song.get('title') or song['id']}")

        stream_title = (await self.db.get_setting("trya_dcs_stream_title") or "").strip()
        ass_path = self._build_combined_ass(
            songs,
            show_progress=False,
            progress_total_count=len(songs),
            stream_title_enabled=bool(stream_title),
            stream_title_text=stream_title,
            disclaimer_enabled=False,
            disclaimer_text="",
            now_playing_enabled=False,
            stream_title_top_left=True,
        )
        video_bitrate = await self._bounded_setting(
            "trya_dcs_video_bitrate_kbps", 2500, 1000, 6000
        )
        audio_bitrate = await self._bounded_setting(
            "trya_dcs_audio_bitrate_kbps", 192, 96, 320
        )
        total_duration = sum(float(song.get("duration") or 300) for song in songs)
        command = self._build_playlist_cmd(
            songs=songs,
            media_paths=media_paths,
            bg_path=bg_path,
            bg_type=bg_type,
            loop_path=loop_path,
            obs_overlay_path=obs_overlay_path,
            obs_overlay_fps=obs_overlay_fps,
            video_bitrate_kbps=video_bitrate,
            ass_path=ass_path,
            twitch_key="",
            total_dur=total_duration,
            media_style=media_style,
            output_url=self._output_url,
            audio_bitrate_kbps=audio_bitrate,
            realtime_audio=True,
            video_preset="ultrafast",
            filter_complex_threads=min(10, os.cpu_count() or 1),
        )
        self._reset_ffmpeg_health("starting")
        self._process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            limit=1024 * 1024,
        )
        stderr_task = asyncio.create_task(self._pipe_ffmpeg_stderr(self._process))
        await asyncio.sleep(2)
        if self._process.returncode is not None:
            await stderr_task
            raise RuntimeError(f"FFmpeg exited during startup ({self._process.returncode}).")
        self._stream_ready_event.set()
        self._log("MediaMTX publisher is live.")
        tracker = asyncio.create_task(self._track_song_progress(songs))
        await self._process.wait()
        tracker.cancel()
        try:
            await tracker
        except asyncio.CancelledError:
            pass
        await stderr_task
        if self._process.returncode and self.is_running:
            raise RuntimeError(f"FFmpeg exited with code {self._process.returncode}.")

    async def _bounded_setting(self, key: str, default: int, low: int, high: int) -> int:
        try:
            value = int(await self.db.get_setting(key) or default)
        except (TypeError, ValueError):
            value = default
        return max(low, min(high, value))

    async def _track_song_progress(self, songs: list[dict]) -> None:
        starts = []
        cursor = 0.0
        for song in songs:
            starts.append(cursor)
            cursor += float(song.get("duration") or 300)
        active_index = -1
        while self.is_running and self._process and self._process.returncode is None:
            out_time = self._get_ffmpeg_out_time_seconds()
            if out_time is None:
                await asyncio.sleep(0.5)
                continue
            index = 0
            for candidate, start in enumerate(starts):
                if out_time + 0.25 >= start:
                    index = candidate
                else:
                    break
            song = songs[index]
            duration = float(song.get("duration") or 300)
            elapsed = max(0.0, out_time - starts[index])
            self.current_song = song
            self._current_song_index = index + 1
            self._current_song_end_time = time.monotonic() + max(0.0, duration - elapsed)
            if index != active_index:
                active_index = index
                self._log(f"Now playing: {song.get('title') or 'Unknown'} — {song.get('artist') or 'Unknown'}")
                await trya_dcs_events.publish("radio.now_playing", {
                    "song_id": int(song["id"]),
                    "title": song.get("title") or "Unknown",
                    "artist": song.get("artist") or "Unknown",
                    "submitted_by": song.get("user_name") or "",
                    "suno_url": self._public_suno_url(song),
                    "wlm_url": self._public_wlm_url(song),
                    "duration": duration,
                    "song_index": index + 1,
                    "playlist_length": len(songs),
                })
                next_song = songs[index + 1] if index + 1 < len(songs) else None
                await trya_dcs_events.publish("radio.next_song", {
                    "song_id": int(next_song["id"]) if next_song else None,
                    "title": (next_song.get("title") or "Unknown") if next_song else None,
                    "artist": (next_song.get("artist") or "Unknown") if next_song else None,
                    "song_index": index + 2 if next_song else None,
                    "playlist_length": len(songs),
                })
            await trya_dcs_events.publish("radio.progress", {
                "song_id": int(song["id"]),
                "elapsed": min(duration, elapsed),
                "duration": duration,
                "song_index": index + 1,
                "playlist_length": len(songs),
            })
            if out_time >= cursor:
                break
            await asyncio.sleep(0.5)
