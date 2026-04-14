"""Manages ffmpeg streaming to Twitch RTMP."""

import asyncio
import os
import time


class StreamManager:
    def __init__(self, db, radio_dir: str):
        self.db = db
        self.radio_dir = radio_dir
        self.process = None
        self.is_running = False
        self.current_index = 0
        self.current_song = None
        self.playlist = []
        self._task = None

    async def get_status(self) -> dict:
        return {
            "running": self.is_running,
            "current_song": {
                "id": self.current_song["id"],
                "title": self.current_song["title"],
                "artist": self.current_song["artist"],
                "index": self.current_index,
                "total": len(self.playlist),
            } if self.current_song else None,
        }

    async def start(self):
        if self.is_running:
            return {"error": "Stream is already running."}
        self.playlist = await self.db.get_all_radio_songs(active_only=True)
        if not self.playlist:
            return {"error": "No songs in the playlist."}
        twitch_key = await self.db.get_setting("radio_twitch_key")
        if not twitch_key:
            return {"error": "Twitch stream key not configured."}
        self.is_running = True
        self.current_index = 0
        self._task = asyncio.create_task(self._stream_loop(twitch_key))
        return await self.get_status()

    async def stop(self):
        self.is_running = False
        if self.process:
            try:
                self.process.terminate()
            except ProcessLookupError:
                pass
        if self._task:
            self._task.cancel()
            self._task = None
        self.current_song = None
        return await self.get_status()

    async def skip_next(self):
        if not self.is_running:
            return {"error": "Stream is not running."}
        self.current_index += 1
        if self.current_index >= len(self.playlist):
            self.current_index = 0
        if self.process:
            try:
                self.process.terminate()
            except ProcessLookupError:
                pass
        return await self.get_status()

    async def skip_prev(self):
        if not self.is_running:
            return {"error": "Stream is not running."}
        self.current_index = max(0, self.current_index - 1)
        if self.process:
            try:
                self.process.terminate()
            except ProcessLookupError:
                pass
        return await self.get_status()

    async def _stream_loop(self, twitch_key: str):
        """Main streaming loop — plays songs sequentially via ffmpeg."""
        rtmp_url = f"rtmp://live.twitch.tv/app/{twitch_key}"

        try:
            while self.is_running and self.playlist:
                if self.current_index >= len(self.playlist):
                    self.current_index = 0

                song = self.playlist[self.current_index]
                self.current_song = song
                audio_path = os.path.join(self.radio_dir, song["filename"])

                if not os.path.exists(audio_path):
                    print(f"[radio] File not found: {audio_path}, skipping")
                    self.current_index += 1
                    continue

                # Build ffmpeg command
                cmd = await self._build_ffmpeg_cmd(audio_path, song, rtmp_url)
                print(f"[radio] Now playing: {song['title']} by {song['artist']}")

                try:
                    self.process = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    await self.process.wait()
                except asyncio.CancelledError:
                    if self.process:
                        try:
                            self.process.terminate()
                        except ProcessLookupError:
                            pass
                    break

                if self.is_running:
                    self.current_index += 1

        except Exception as e:
            print(f"[radio] Stream loop error: {e}")
        finally:
            self.is_running = False
            self.current_song = None
            self.process = None
            print("[radio] Stream stopped.")

    async def _build_ffmpeg_cmd(self, audio_path: str, song: dict, rtmp_url: str) -> list:
        """Build the ffmpeg command with background + overlay."""
        bg_filename = await self.db.get_setting("radio_background_filename")
        bg_type = await self.db.get_setting("radio_background_type") or "image"
        bot_name = await self.db.get_setting("bot_name") or "Stream"

        # Escape text for ffmpeg drawtext
        def esc(text):
            return text.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")

        now_playing = f"{esc(song['title'])}  -  {esc(song['artist'])}"
        overlay_filter = (
            f"drawtext=text='{now_playing}'"
            f":fontsize=28:fontcolor=white:borderw=2:bordercolor=black"
            f":x=(w-text_w)/2:y=h-60"
        )

        cmd = ["ffmpeg", "-y"]

        if bg_filename and os.path.exists(os.path.join(self.radio_dir, bg_filename)):
            bg_path = os.path.join(self.radio_dir, bg_filename)
            if bg_type == "video":
                cmd += ["-stream_loop", "-1", "-re", "-i", bg_path]
            else:
                cmd += ["-loop", "1", "-re", "-i", bg_path]
        else:
            # Generate a black background
            cmd += [
                "-f", "lavfi", "-i",
                "color=c=black:s=1920x1080:r=30",
            ]

        cmd += ["-i", audio_path]

        # Video filters + encoding
        vf = f"scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,format=yuv420p,{overlay_filter}"
        cmd += [
            "-vf", vf,
            "-c:v", "libx264", "-profile:v", "main", "-level", "4.0",
            "-preset", "ultrafast",
            "-pix_fmt", "yuv420p",
            "-b:v", "2500k", "-maxrate", "2500k", "-bufsize", "5000k",
            "-r", "30",
            "-g", "60", "-keyint_min", "60",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
            "-shortest",
            "-f", "flv", rtmp_url,
        ]

        return cmd
