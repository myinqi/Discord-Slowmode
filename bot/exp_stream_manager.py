"""Experimental Radio – FFmpeg stream manager.

Pipeline per full playlist rotation (no gap between songs):
  One FFmpeg job covers all songs using concat:
    - Audio:     FFmpeg concat demuxer (concat.txt listing all MP3s)
    - Covers:    each song's image/video as a trimmed input, concat'd in filtergraph
    - Background + loop overlay: loop for total duration
    - ASS:       all subtitle files merged with time offsets + NowPlaying title cards

The stream only briefly disconnects once per full playlist loop (not between songs).
"""

import asyncio
import os
import re
import random
import time
from collections import deque

from bot.twitch_bot import TwitchBot

_RTMP_BASE  = "rtmp://live.twitch.tv/app/"
_FPS        = 30
_W, _H      = 1920, 1080
_INSET_W    = 360   # portrait inset width  (9:16 ≈ 360×640, doubled from 180×320)
_INSET_H    = 640   # portrait inset height
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

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
# emit roughly once per second and would flood the log buffer. We drop them
# entirely — anything else (warnings, errors, our own [exp-stream] messages)
# is interesting and goes through.
_FFMPEG_PROGRESS_RE = re.compile(r"^frame=\s*\d+.*time=")

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


# ── Module-level Live Log ────────────────────────────────────────────────────
# Re-exported from bot.live_log so the rest of this file can keep calling
# log_event() / _LOG_BUFFER unchanged, and other modules (twitch_bot,
# relic_hunt) share the same buffer without a circular import.
from bot.live_log import _LOG_BUFFER, log_event as _live_log_event

_LOG_BUFFER_MAX = 1000

# Module-level flag: True while the exp-radio stream is actively running.
stream_is_live: bool = False


def log_event(line: str, level: str = "info", prefix: str = "[exp-radio]") -> None:
    _live_log_event(line, level, prefix)


class ExpStreamManager:
    # Ring buffer size — keeps roughly the last 10–15 minutes of activity
    # (the UI then filters by an explicit time window).
    _LOG_BUFFER_MAX = 1000

    def __init__(self, db, exp_radio_dir: str):
        self.db            = db
        self.exp_radio_dir = exp_radio_dir
        self._process      = None
        self._task         = None
        self.is_running    = False
        self.current_song: dict | None = None
        self.playlist: list[dict]      = []
        self._twitch_key   = ""
        self._twitch_chat: TwitchBot | None = None
        # Live log: each entry is (unix_ts: float, level: str, line: str).
        # The actual buffer is module-level (see _LOG_BUFFER below) so that
        # background workers and web actions which don't have a reference to
        # the manager instance can still publish into the same live log.
        self._log_buffer = _LOG_BUFFER

    # ── Live log buffer ──────────────────────────────────────────────────────────────────────

    def _log(self, line: str, level: str = "info") -> None:
        """Instance shortcut that forwards to the module-level `log_event`."""
        log_event(line, level, prefix="[exp-stream]")

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

    # ── Public API ─────────────────────────────────────────────────────────────

    async def start(self, twitch_key: str, fresh_cache: bool = False) -> dict:
        if self.is_running:
            return {"ok": False, "error": "Stream already running."}
        if fresh_cache:
            # Scheduled / forced fresh starts: wipe the per-stream cache so
            # covers are re-downloaded (any cover_url changes upstream are
            # picked up) and the audio concat / combined ASS are rebuilt
            # from scratch. The mp3/ and ass/ directories produced by Whisper
            # are NOT touched — those are expensive and source-of-truth.
            import shutil
            try:
                cover_cache = os.path.join(self.exp_radio_dir, "cover_cache")
                if os.path.isdir(cover_cache):
                    shutil.rmtree(cover_cache, ignore_errors=True)
                for fn in ("_audio_concat.txt", "_combined.ass"):
                    p = os.path.join(self.exp_radio_dir, fn)
                    if os.path.exists(p):
                        try: os.remove(p)
                        except Exception: pass
                self._log("Fresh-cache start: cover cache and intermediates cleared.")
            except Exception as e:
                self._log(f"Fresh-cache cleanup error: {e}", "error")
        songs = await self.db.get_all_exp_radio_songs(active_only=True)
        # A song is stream-eligible when:
        #   - Whisper analysis completed and the MP3 exists, AND
        #   - Moderation either was never run (NULL — grandfathered before the
        #     moderation feature was enabled) or returned 'passed' / was
        #     manually 'approved'. 'pending' and 'flagged' songs are held
        #     back until an admin acts on them.
        def _mod_ok(s: dict) -> bool:
            ms = s.get("moderation_status")
            return ms is None or ms in ("passed", "approved")

        ready = [
            s for s in songs
            if s.get("analysis_status") == "done"
            and s.get("mp3_filename")
            and _mod_ok(s)
        ]
        if not ready:
            return {"ok": False, "error": "No ready songs in the playlist."}
        self._twitch_key = twitch_key
        random.shuffle(ready)
        self.playlist    = ready
        # Connect to Twitch chat if enabled and credentials exist
        chat_enabled = await self.db.get_setting("exp_radio_twitch_chat_enabled") or "off"
        client_id    = await self.db.get_setting("exp_radio_twitch_client_id")
        refresh_tok  = await self.db.get_setting("exp_radio_twitch_refresh_token")
        broadcaster  = await self.db.get_setting("exp_radio_twitch_broadcaster_login")
        if chat_enabled == "on" and client_id and refresh_tok and broadcaster:
            self._twitch_chat = TwitchBot(self.db, key_prefix="exp_radio_twitch")
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
        self._log(f"Started with {len(ready)} songs.")
        return {"ok": True, "song_count": len(ready)}

    async def stop(self) -> dict:
        self.is_running = False
        global stream_is_live
        stream_is_live = False
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
        if self._task:
            self._task.cancel()
            self._task = None
        self.current_song = None
        if self._twitch_chat:
            await self._twitch_chat.stop()
            self._twitch_chat = None
        self._log("Stopped.")
        return {"ok": True}

    async def get_status(self) -> dict:
        return {
            "running": self.is_running,
            "song": {
                "title":    self.current_song.get("title")    if self.current_song else None,
                "artist":   self.current_song.get("artist")   if self.current_song else None,
                "suno_url": self.current_song.get("suno_url") if self.current_song else None,
            } if self.current_song else None,
            "playlist_length": len(self.playlist),
        }

    # ── Stream loop (one FFmpeg per full playlist rotation) ────────────────────

    async def _stream_loop(self):
        while self.is_running:
            # Set current_song to first track so status shows something immediately
            if self.playlist:
                self.current_song = self.playlist[0]
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
            mode = (await self.db.get_setting("exp_radio_loop_mode")) or "reshuffle"
            if mode == "stop":
                self._log("Loop mode 'stop' — playlist finished, ending stream.")
                # Trigger normal stop() cleanup (sets is_running=False, closes
                # Twitch chat, clears current_song). We schedule it instead of
                # awaiting because stop() will cancel this very task.
                asyncio.create_task(self.stop())
                break
            # mode == "reshuffle": reload eligible songs from DB so newly
            # approved submissions get picked up, then reshuffle.
            songs = await self.db.get_all_exp_radio_songs(active_only=True)
            def _mod_ok(s: dict) -> bool:
                ms = s.get("moderation_status")
                return ms is None or ms in ("passed", "approved")
            ready = [
                s for s in songs
                if s.get("analysis_status") == "done"
                and s.get("mp3_filename")
                and _mod_ok(s)
            ]
            if not ready:
                self._log("Reshuffle: no eligible songs left — stopping.", "error")
                asyncio.create_task(self.stop())
                break
            random.shuffle(ready)
            self.playlist = ready
            self._log(f"Reshuffled playlist ({len(ready)} songs) — next rotation starting.")

    async def _play_playlist(self, songs: list):
        """Run one FFmpeg job that covers all songs without stopping between them."""
        bg_fn   = await self.db.get_setting("exp_radio_bg_filename") or ""
        bg_type = await self.db.get_setting("exp_radio_bg_type") or "image"

        # Resolve loop video: supports multiple uploads + shuffle / fixed pick.
        loop_fn = ""
        import json as _json
        loop_raw = await self.db.get_setting("exp_radio_loop_videos") or "[]"
        loop_vids = _json.loads(loop_raw) if loop_raw else []
        if loop_vids:
            loop_sel = await self.db.get_setting("exp_radio_loop_selection") or "shuffle"
            if loop_sel == "shuffle":
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
            loop_fn = await self.db.get_setting("exp_radio_loop_filename") or ""

        bg_path   = os.path.join(self.exp_radio_dir, "assets", bg_fn)   if bg_fn   else None
        loop_path = os.path.join(self.exp_radio_dir, "assets", loop_fn) if loop_fn else None

        # Prefetch all covers/videos
        media_paths = []
        for song in songs:
            path = await self._get_video(song) or await self._get_cover(song)
            media_paths.append(path)
            self._log(f"Media ready: {song.get('title')} → {path}")

        # Build combined ASS (subtitles + NowPlaying title cards)
        combined_ass = self._build_combined_ass(songs)

        # Build audio concat file
        concat_txt = os.path.join(self.exp_radio_dir, "_audio_concat.txt")
        with open(concat_txt, "w", encoding="utf-8") as f:
            for song in songs:
                mp3 = os.path.join(self.exp_radio_dir, "mp3", song["mp3_filename"])
                f.write(f"file '{mp3}'\n")

        total_dur = sum(s.get("duration") or 300 for s in songs)

        cmd = self._build_playlist_cmd(
            songs=songs,
            media_paths=media_paths,
            bg_path=bg_path   if bg_path   and os.path.exists(bg_path)   else None,
            bg_type=bg_type,
            loop_path=loop_path if loop_path and os.path.exists(loop_path) else None,
            ass_path=combined_ass if combined_ass and os.path.exists(combined_ass) else None,
            audio_concat_file=concat_txt,
            twitch_key=self._twitch_key,
            total_dur=total_dur,
        )

        titles = " → ".join(s.get("title") or "?" for s in songs)
        self._log(f"FFmpeg playlist: {titles}")

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
        chat_task   = asyncio.create_task(self._post_now_playing_loop(songs))
        stderr_task = asyncio.create_task(self._pipe_ffmpeg_stderr(self._process))
        announce_task = asyncio.create_task(self._announce_rotation_end(total_dur))
        await self._process.wait()
        chat_task.cancel()
        announce_task.cancel()
        try:
            await chat_task
        except asyncio.CancelledError:
            pass
        try:
            await announce_task
        except asyncio.CancelledError:
            pass
        # stderr task ends naturally when EOF — just await it.
        try:
            await stderr_task
        except Exception:
            pass
        rc = self._process.returncode
        if rc and rc != 0 and self.is_running:
            self._log(f"FFmpeg exited {rc}.", "error")

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
            if not line or _FFMPEG_PROGRESS_RE.match(line):
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
            mode = (await self.db.get_setting("exp_radio_loop_mode")) or "reshuffle"
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

    async def _post_now_playing_loop(self, songs: list):
        """Post ♪ Now Playing to Twitch chat at each song boundary."""
        if not self._twitch_chat:
            return
        _POST_DELAY = 10  # seconds after song start before posting
        for song in songs:
            dur = song.get("duration") or 300
            await asyncio.sleep(min(_POST_DELAY, dur))
            title    = song.get("title")  or "Unknown"
            artist   = song.get("artist") or ""
            suno_url = song.get("suno_url") or ""
            msg = f"\U0001F3B5 Now Playing: {title}"
            if artist:
                msg += f" - {artist}"
            if suno_url:
                msg += f" | {suno_url}"
            await self._twitch_chat.send(msg)
            await asyncio.sleep(max(0, dur - _POST_DELAY))

    # ── Media helpers ──────────────────────────────────────────────────────────

    async def _get_cover(self, song: dict) -> str | None:
        """Download cover JPEG to local cache. Returns local path or None."""
        import aiohttp
        uuid      = song.get("suno_uuid") or ""
        cache_dir = os.path.join(self.exp_radio_dir, "cover_cache")
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

    async def _get_video(self, song: dict) -> str | None:
        """Download Suno 9:16 video (MP4) to local cache. Returns local path or None."""
        import aiohttp
        video_url = song.get("video_url")
        if not video_url:
            return None
        uuid      = song.get("suno_uuid") or ""
        cache_dir = os.path.join(self.exp_radio_dir, "cover_cache")
        os.makedirs(cache_dir, exist_ok=True)
        dest = os.path.join(cache_dir, f"{uuid}.mp4")
        if os.path.exists(dest):
            # Idempotent re-check: files cached before the normaliser was
            # introduced (or in a previous, looser version of it) may still
            # exceed 720px on a side. The check is ffprobe-cheap and
            # early-returns when the file is already compliant.
            await self._normalize_cover_video(dest)
            return dest
        try:
            async with aiohttp.ClientSession(headers={"User-Agent": _BROWSER_UA}) as sess:
                async with sess.get(video_url, timeout=aiohttp.ClientTimeout(total=60)) as r:
                    if r.status == 200:
                        with open(dest, "wb") as f:
                            f.write(await r.read())
                        self._log(f"Video cached: {uuid}.mp4")
                        # Normalize immediately so the stream pipeline always
                        # sees lightweight covers (see _normalize_cover_video).
                        await self._normalize_cover_video(dest)
                        return dest
        except Exception as e:
            self._log(f"Video download error ({uuid}): {e}", "error")
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
        cache_dir = os.path.join(self.exp_radio_dir, "cover_cache")
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

    # ── Combined ASS builder ───────────────────────────────────────────────────

    def _build_combined_ass(self, songs: list) -> str | None:
        """Merge all per-song ASS files into one with time offsets.
        Adds a NowPlaying title card at the start of each song."""
        ass_dir = os.path.join(self.exp_radio_dir, "ass")
        out_path = os.path.join(self.exp_radio_dir, "_combined.ass")

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
            "Style: Default,Arial,52,&H00FFFFFF,&H000000FF,&H00000000,"
            "&H80000000,-1,0,0,0,100,100,0,0,1,2.5,1,2,10,10,30,1\n"
            "Style: NowPlaying,Arial,48,&H00E8C97A,&H000000FF,&H00000000,"
            "&HC8000000,0,0,0,0,100,100,0,0,1,1.5,0.8,7,20,20,14,1\n\n"
            "[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        )

        events = []
        offset_cs = 0
        has_any = False

        for song in songs:
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
                        print(f"[exp-stream] ASS read error ({ass_fn}): {e}", flush=True)

            offset_cs += dur_cs

        try:
            with open(out_path, "w", encoding="utf-8") as f:
                f.write(header)
                for e in events:
                    f.write(e + "\n")
            return out_path
        except Exception as e:
            print(f"[exp-stream] Combined ASS write error: {e}", flush=True)
            return None

    # ── FFmpeg command builder ─────────────────────────────────────────────────

    def _build_playlist_cmd(
        self,
        songs: list,
        media_paths: list,
        bg_path: str | None,
        bg_type: str,
        loop_path: str | None,
        ass_path: str | None,
        audio_concat_file: str,
        twitch_key: str,
        total_dur: float,
    ) -> list:
        """Build ONE FFmpeg command for the full playlist.

        Layer stack (bottom → top):
          0: Background (static image or looping video)  1920×1080
          1: Loop video overlay, 500×500, top-right
          2: Song media concat (9:16 video or square cover), bottom-left inset
          3: Combined ASS subtitles (lyrics + NowPlaying title cards)
        """
        W, H = _W, _H
        cmd  = ["ffmpeg", "-y"]

        input_idx = 0
        bg_input = lv_input = None

        # Background
        if bg_path:
            if bg_type == "video":
                cmd += ["-stream_loop", "-1", "-re", "-i", bg_path]
            else:
                cmd += ["-loop", "1", "-i", bg_path]
            bg_input = input_idx; input_idx += 1

        # Loop overlay
        if loop_path:
            cmd += ["-stream_loop", "-1", "-re", "-i", loop_path]
            lv_input = input_idx; input_idx += 1

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

        # Audio (concat demuxer)
        cmd += ["-f", "concat", "-safe", "0", "-i", audio_concat_file]
        audio_input = input_idx; input_idx += 1

        # ── Filtergraph ────────────────────────────────────────────────────────
        filters = []

        # Background
        if bg_input is not None:
            filters.append(
                f"[{bg_input}:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
                f"crop={W}:{H},fps={_FPS}[bg]"
            )
        else:
            filters.append(f"color=size={W}x{H}:color=0x111111:rate={_FPS}[bg]")
        last = "[bg]"

        # Loop video overlay – top-right, 650×650 (+30% from 500×500)
        if lv_input is not None:
            filters.append(
                f"[{lv_input}:v]scale=650:650:force_original_aspect_ratio=decrease,"
                f"fps={_FPS}[lv]"
            )
            filters.append(
                f"{last}[lv]overlay=x={W}-650-20:y=20:shortest=0[after_lv]"
            )
            last = "[after_lv]"

        # Scale each media input to portrait inset, then concat
        n = len(songs)
        for i, (song, mid) in enumerate(zip(songs, media_inputs)):
            dur = song.get("duration") or 300
            filters.append(
                f"[{mid}:v]scale={_INSET_W}:{_INSET_H}:force_original_aspect_ratio=increase,"
                f"crop={_INSET_W}:{_INSET_H},setsar=1,"
                f"fps={_FPS},trim=0:{dur},setpts=PTS-STARTPTS[cv{i}]"
            )
        concat_in = "".join(f"[cv{i}]" for i in range(n))
        filters.append(f"{concat_in}concat=n={n}:v=1:a=0[covers]")
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
        cmd += ["-map", "[vout]", "-map", f"{audio_input}:a"]
        cmd += ["-t", str(total_dur + 2)]

        # ── Encode ─────────────────────────────────────────────────────────────
        cmd += [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
            "-pix_fmt", "yuv420p", "-g", str(_FPS * 2), "-keyint_min", str(_FPS),
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
            "-f", "flv", f"{_RTMP_BASE}{twitch_key}",
        ]
        return cmd
