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
from config import Config


class TryaDcsManager(TryaStreamManager):
    """Publish DCS songs to MediaMTX without Twitch runtime dependencies."""

    _LOG_BUFFER_MAX = 800

    def __init__(self, db, base_dir: str):
        super().__init__(db, base_dir)
        self._log_buffer = deque(maxlen=self._LOG_BUFFER_MAX)
        self._output_url = ""

    def _log(self, line: str, level: str = "info") -> None:
        self._log_buffer.append((time.time(), level, line))
        print(f"[trya-dcs] {line}", flush=True)

    def get_log(self, since_ts: float = 0.0, max_age_secs: float = 600.0) -> list[dict]:
        cutoff = max(float(since_ts or 0), time.time() - max_age_secs)
        return [
            {"ts": ts, "level": level, "line": line}
            for ts, level, line in self._log_buffer
            if ts > cutoff
        ]

    async def start(self, *, created_by: str = "admin") -> dict:
        async with self._start_lock:
            if self.is_running:
                return {"ok": False, "error": "The DCS stream is already running."}
            if (await self.db.get_setting("trya_dcs_enabled") or "off") != "on":
                return {"ok": False, "error": "Enable TrYa DCS before starting the stream."}

            songs = await self.db.get_trya_dcs_songs(active_only=True)
            ready = []
            for stored in songs:
                filename = stored.get("mp3_filename")
                path = os.path.join(self.trya_stream_dir, "mp3", filename or "")
                if (
                    stored.get("analysis_status") == "done"
                    and stored.get("approval_status") == "approved"
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
            if not ready:
                return {"ok": False, "error": "No approved and analyzed DCS songs are ready."}

            random.shuffle(ready)
            self.playlist = ready
            self._progress_total_count = len(ready)
            self._current_song_index = 1
            self.current_song = ready[0]
            self._current_song_end_time = time.monotonic() + float(
                ready[0].get("duration") or 300
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
                for song in ready
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
            self._log(f"Starting {len(ready)} songs toward MediaMTX.")
            await trya_dcs_events.publish("radio.mode", {
                "mode": "starting",
                "playlist_length": len(ready),
            })
            await trya_dcs_events.publish("radio.queue_update", {
                "songs": snapshot,
            })
            return {"ok": True, "song_count": len(ready)}

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

    async def get_status(self) -> dict:
        status = await super().get_status()
        status["output_url"] = self._output_url
        status["listener_count"] = trya_dcs_events.listener_count
        if status.get("song") and self.current_song:
            status["song"]["submitted_by"] = self.current_song.get("user_name") or ""
        return status

    async def _stream_loop(self):
        try:
            while self.is_running:
                await self._play_dcs_playlist(self.playlist)
                if not self.is_running:
                    break
                mode = await self.db.get_setting("trya_dcs_loop_mode") or "stop"
                if mode != "reshuffle":
                    self._log("Playlist finished.")
                    await self.stop()
                    break
                random.shuffle(self.playlist)
                self._log("Playlist reshuffled for the next rotation.")
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._log(f"Publisher failed: {exc}", "error")
            await self.stop()

    async def _play_dcs_playlist(self, songs: list[dict]) -> None:
        reuse_visuals = (
            await self.db.get_setting("trya_dcs_reuse_trya_visuals") or "on"
        ) == "on"
        bg_path = None
        bg_type = "image"
        loop_path = None
        media_style = None
        if reuse_visuals:
            asset_dir = os.path.join(Config.TRYA_STREAM_DIR, "assets")
            bg_filename = os.path.basename(
                await self.db.get_setting("trya_stream_bg_filename") or ""
            )
            bg_type = await self.db.get_setting("trya_stream_bg_type") or "image"
            candidate = os.path.join(asset_dir, bg_filename) if bg_filename else ""
            if candidate and os.path.isfile(candidate):
                bg_path = candidate

            try:
                loop_videos = json.loads(
                    await self.db.get_setting("trya_stream_loop_videos") or "[]"
                )
            except (TypeError, json.JSONDecodeError):
                loop_videos = []
            valid_loops = []
            for item in loop_videos if isinstance(loop_videos, list) else []:
                filename = os.path.basename(str(item.get("filename") or ""))
                candidate = os.path.join(asset_dir, filename) if filename else ""
                if candidate and os.path.isfile(candidate):
                    valid_loops.append(candidate)
            if valid_loops:
                loop_path = random.choice(valid_loops)

            corners = (
                await self.db.get_setting("trya_stream_media_corners_enabled") or "off"
            ) == "on"
            border = (
                await self.db.get_setting("trya_stream_media_border_enabled") or "off"
            ) == "on"
            radius = await self._bounded_setting(
                "trya_stream_media_corner_radius", 28, 1, 120
            )
            border_width = await self._bounded_setting(
                "trya_stream_media_border_width", 3, 1, 20
            )
            border_color = (
                await self.db.get_setting("trya_stream_media_border_color") or "#A855F7"
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

        media_paths = []
        for song in songs:
            path = await self._get_video(song) or await self._get_cover(song)
            media_paths.append(path)
            self._log(f"Media ready: {song.get('title') or song['id']}")

        disclaimer = (await self.db.get_setting("trya_dcs_disclaimer") or "").strip()
        stream_title = (await self.db.get_setting("trya_dcs_stream_title") or "").strip()
        ass_path = self._build_combined_ass(
            songs,
            show_progress=True,
            progress_total_count=len(songs),
            stream_title_enabled=bool(stream_title),
            stream_title_text=stream_title,
            disclaimer_enabled=bool(disclaimer),
            disclaimer_text=disclaimer,
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
            obs_overlay_path=None,
            obs_overlay_fps=20,
            video_bitrate_kbps=video_bitrate,
            ass_path=ass_path,
            twitch_key="",
            total_dur=total_duration,
            media_style=media_style,
            output_url=self._output_url,
            audio_bitrate_kbps=audio_bitrate,
            realtime_audio=True,
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
