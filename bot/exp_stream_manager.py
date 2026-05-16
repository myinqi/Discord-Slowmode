"""Experimental Radio – FFmpeg stream manager.

Pipeline per song:
  [background image/video loop] + [cover/video clip] + [ASS karaoke subtitles]
  + [MP3 audio]  →  RTMP → Twitch

The playlist is frozen (randomised) at stream start. Deletion via the admin UI
only sets active=0 in the DB; it does not affect a running stream.
"""

import asyncio
import os
import random
import time

_RTMP_BASE = "rtmp://live.twitch.tv/app/"
_FPS       = 30
_W, _H     = 1920, 1080
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


class ExpStreamManager:
    def __init__(self, db, exp_radio_dir: str):
        self.db           = db
        self.exp_radio_dir = exp_radio_dir
        self._process     = None
        self._task        = None
        self.is_running   = False
        self.current_song: dict | None = None
        self.playlist: list[dict]      = []
        self._twitch_key  = ""
        self._song_idx    = 0

    # ── Public API ─────────────────────────────────────────────────────────────

    async def start(self, twitch_key: str) -> dict:
        if self.is_running:
            return {"ok": False, "error": "Stream already running."}
        songs = await self.db.get_all_exp_radio_songs(active_only=True)
        ready = [s for s in songs if s.get("analysis_status") == "done" and s.get("mp3_filename")]
        if not ready:
            return {"ok": False, "error": "No ready songs in the playlist. Wait for analysis to finish."}
        self._twitch_key = twitch_key
        random.shuffle(ready)
        self.playlist   = ready
        self._song_idx  = 0
        self.is_running = True
        self._task = asyncio.create_task(self._stream_loop())
        print(f"[exp-stream] Started with {len(ready)} songs.", flush=True)
        return {"ok": True, "song_count": len(ready)}

    async def stop(self) -> dict:
        self.is_running = False
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
        print("[exp-stream] Stopped.", flush=True)
        return {"ok": True}

    async def get_status(self) -> dict:
        return {
            "running": self.is_running,
            "song": {
                "title":  self.current_song.get("title") if self.current_song else None,
                "artist": self.current_song.get("artist") if self.current_song else None,
                "suno_url": self.current_song.get("suno_url") if self.current_song else None,
            } if self.current_song else None,
            "playlist_length": len(self.playlist),
            "song_index": self._song_idx,
        }

    # ── Internal stream loop ───────────────────────────────────────────────────

    async def _stream_loop(self):
        while self.is_running:
            if self._song_idx >= len(self.playlist):
                self._song_idx = 0
            song = self.playlist[self._song_idx]
            self.current_song = song
            self._song_idx   += 1
            try:
                await self._play_song(song)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[exp-stream] Error playing song #{song.get('id')}: {e}", flush=True)
                await asyncio.sleep(2)

    async def _play_song(self, song: dict):
        mp3_path  = os.path.join(self.exp_radio_dir, "mp3", song["mp3_filename"])
        ass_path  = os.path.join(self.exp_radio_dir, "ass", song["ass_filename"]) \
                    if song.get("ass_filename") else None
        bg_fn     = await self.db.get_setting("exp_radio_bg_filename") or ""
        bg_type   = await self.db.get_setting("exp_radio_bg_type") or "image"
        loop_fn   = await self.db.get_setting("exp_radio_loop_filename") or ""

        bg_path   = os.path.join(self.exp_radio_dir, "assets", bg_fn) if bg_fn else None
        loop_path = os.path.join(self.exp_radio_dir, "assets", loop_fn) if loop_fn else None

        # Download cover / video if not cached
        cover_path = await self._get_cover(song)

        cmd = await self._build_ffmpeg_cmd(
            mp3_path=mp3_path,
            bg_path=bg_path if bg_path and os.path.exists(bg_path) else None,
            bg_type=bg_type,
            loop_path=loop_path if loop_path and os.path.exists(loop_path) else None,
            cover_path=cover_path,
            ass_path=ass_path if ass_path and os.path.exists(ass_path) else None,
            twitch_key=self._twitch_key,
            duration=song.get("duration") or 300,
        )

        title  = song.get("title") or "Unknown"
        artist = song.get("artist") or ""
        print(f"[exp-stream] Playing: {title} – {artist}", flush=True)

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await self._process.communicate()
        rc = self._process.returncode
        if rc and rc != 0 and self.is_running:
            err_tail = (stderr or b"")[-400:].decode("utf-8", errors="replace")
            print(f"[exp-stream] FFmpeg exited {rc}: …{err_tail}", flush=True)

    async def _get_cover(self, song: dict) -> str | None:
        """Download cover image to local cache. Returns local path or None."""
        import aiohttp
        uuid      = song.get("suno_uuid") or ""
        cache_dir = os.path.join(self.exp_radio_dir, "cover_cache")
        os.makedirs(cache_dir, exist_ok=True)
        dest = os.path.join(cache_dir, f"{uuid}.jpg")
        if os.path.exists(dest):
            return dest
        url = song.get("cover_url") or f"https://cdn1.suno.ai/image_large_{uuid}.jpeg"
        try:
            async with aiohttp.ClientSession(headers={"User-Agent": _BROWSER_UA}) as sess:
                async with sess.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
                    if r.status == 200:
                        data = await r.read()
                        with open(dest, "wb") as f:
                            f.write(data)
                        return dest
        except Exception as e:
            print(f"[exp-stream] Cover download error: {e}", flush=True)
        return None

    async def _build_ffmpeg_cmd(
        self,
        mp3_path: str,
        bg_path: str | None,
        bg_type: str,
        loop_path: str | None,
        cover_path: str | None,
        ass_path: str | None,
        twitch_key: str,
        duration: float,
    ) -> list:
        """Build the FFmpeg command for one song.

        Layer stack (bottom → top):
          0: Background (static image or looping video)      1920×1080
          1: Loop video overlay (looping), scaled to 400×400, top-right corner
          2: Cover image (current song), scaled to 300×300, bottom-left
          3: ASS karaoke subtitles (burned in via subtitles filter)
        """
        W, H = _W, _H
        cmd  = ["ffmpeg", "-y"]

        # ── Inputs ─────────────────────────────────────────────────────────────
        input_idx = 0
        bg_input  = None
        lv_input  = None
        cv_input  = None

        if bg_path and bg_type == "video":
            cmd += ["-stream_loop", "-1", "-re", "-i", bg_path]
            bg_input = input_idx; input_idx += 1
        elif bg_path:
            cmd += ["-loop", "1", "-i", bg_path]
            bg_input = input_idx; input_idx += 1

        if loop_path:
            cmd += ["-stream_loop", "-1", "-re", "-i", loop_path]
            lv_input = input_idx; input_idx += 1

        if cover_path:
            cmd += ["-loop", "1", "-i", cover_path]
            cv_input = input_idx; input_idx += 1

        cmd += ["-i", mp3_path]
        audio_input = input_idx; input_idx += 1

        # ── Filtergraph ────────────────────────────────────────────────────────
        filters = []
        last   = None

        # Background
        if bg_input is not None:
            filters.append(
                f"[{bg_input}:v]scale={W}:{H}:force_original_aspect_ratio=increase,"
                f"crop={W}:{H},fps={_FPS}[bg]"
            )
            last = "[bg]"
        else:
            filters.append(
                f"color=size={W}x{H}:color=0x111111:rate={_FPS}[bg]"
            )
            last = "[bg]"

        # Loop video overlay – top-right, 400×400
        if lv_input is not None:
            filters.append(
                f"[{lv_input}:v]scale=400:400:force_original_aspect_ratio=decrease,"
                f"fps={_FPS}[lv]"
            )
            filters.append(
                f"{last}[lv]overlay=x={W}-400-20:y=20:shortest=0[after_lv]"
            )
            last = "[after_lv]"

        # Cover image – bottom-left, 300×300
        if cv_input is not None:
            filters.append(
                f"[{cv_input}:v]scale=300:300:force_original_aspect_ratio=decrease,"
                f"pad=300:300:(ow-iw)/2:(oh-ih)/2:color=black[cv]"
            )
            filters.append(
                f"{last}[cv]overlay=x=20:y={H}-300-20:shortest=0[after_cv]"
            )
            last = "[after_cv]"

        # ASS subtitles (burned in)
        if ass_path:
            esc = ass_path.replace("\\", "/").replace(":", "\\:")
            filters.append(f"{last}subtitles='{esc}'[vout]")
            last = "[vout]"
        else:
            filters.append(f"{last}copy[vout]")

        # Trim to song duration
        cmd += ["-filter_complex", ";".join(filters)]
        cmd += ["-map", "[vout]", "-map", f"{audio_input}:a"]
        cmd += ["-t", str(duration + 1)]

        # ── Encode ─────────────────────────────────────────────────────────────
        cmd += [
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
            "-pix_fmt", "yuv420p", "-g", str(_FPS * 2), "-keyint_min", str(_FPS),
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
            "-f", "flv", f"{_RTMP_BASE}{twitch_key}",
        ]
        return cmd
