"""Manages ffmpeg streaming to Twitch RTMP.

Architecture: one persistent ffmpeg encoder process reads raw PCM audio from a
named FIFO pipe. A separate feeder coroutine decodes each MP3 with ffmpeg and
writes the raw PCM data into that pipe — so song transitions happen without any
stream interruption on Twitch's side.

Video side: the background image/video + drawtext overlay are rendered by the
encoder process; the drawtext filter is updated between songs via the ffmpeg
sendcmd/drawtext reload mechanism (writing a commands file).
"""

import asyncio
import os
import random
import tempfile


# Audio format shared between feeder and encoder
_PCM_RATE = 44100
_PCM_CHANNELS = 2
_PCM_FORMAT = "s16le"          # signed 16-bit little-endian


class StreamManager:
    def __init__(self, db, radio_dir: str):
        self.db = db
        self.radio_dir = radio_dir
        self._encoder = None       # the single long-running ffmpeg process
        self._feeder_task = None   # coroutine writing PCM into the pipe
        self._skip_event = asyncio.Event()
        self.is_running = False
        self.current_index = 0
        self.current_song = None
        self.playlist = []
        self._fifo_path = None
        self._font_path = None

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

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

        self.shuffle = (await self.db.get_setting("radio_shuffle") or "0") == "1"
        if self.shuffle:
            random.shuffle(self.playlist)

        self._font_path = await self._resolve_font()
        self.is_running = True
        self.current_index = 0
        self._skip_event.clear()

        # Create FIFO
        fifo_dir = tempfile.mkdtemp(prefix="radio_")
        self._fifo_path = os.path.join(fifo_dir, "audio.pcm")
        os.mkfifo(self._fifo_path)

        # Start encoder first (it will block on open(fifo) until feeder opens it)
        self._encoder = await self._start_encoder(twitch_key)

        # Start feeder
        self._feeder_task = asyncio.create_task(self._feed_loop())

        return await self.get_status()

    async def stop(self):
        self.is_running = False
        self._skip_event.set()
        if self._feeder_task:
            self._feeder_task.cancel()
            try:
                await self._feeder_task
            except (asyncio.CancelledError, Exception):
                pass
            self._feeder_task = None
        if self._encoder:
            try:
                self._encoder.terminate()
                await asyncio.wait_for(self._encoder.wait(), timeout=5)
            except Exception:
                pass
            self._encoder = None
        self._cleanup_fifo()
        self.current_song = None
        return await self.get_status()

    async def skip_next(self):
        if not self.is_running:
            return {"error": "Stream is not running."}
        self._skip_event.set()
        return await self.get_status()

    async def skip_prev(self):
        if not self.is_running:
            return {"error": "Stream is not running."}
        self.current_index = max(0, self.current_index - 2)  # -2 because feed_loop will +1
        self._skip_event.set()
        return await self.get_status()

    # ------------------------------------------------------------------ #
    #  Internal                                                            #
    # ------------------------------------------------------------------ #

    def _cleanup_fifo(self):
        if self._fifo_path:
            try:
                os.remove(self._fifo_path)
                os.rmdir(os.path.dirname(self._fifo_path))
            except Exception:
                pass
            self._fifo_path = None

    async def _resolve_font(self) -> str:
        path = "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf"
        if not os.path.exists(path):
            for root, dirs, files in os.walk("/usr/share/fonts"):
                for f in files:
                    if "NotoSans" in f and f.endswith(".ttf"):
                        return os.path.join(root, f)
        return path

    async def _start_encoder(self, twitch_key: str):
        """Launch the single persistent ffmpeg encoder that reads PCM from FIFO."""
        rtmp_url = f"rtmp://live.twitch.tv/app/{twitch_key}"
        bg_filename = await self.db.get_setting("radio_background_filename")
        bg_type = await self.db.get_setting("radio_background_type") or "image"

        cmd = ["ffmpeg", "-y"]

        # Background input
        if bg_filename and os.path.exists(os.path.join(self.radio_dir, bg_filename)):
            bg_path = os.path.join(self.radio_dir, bg_filename)
            if bg_type == "video":
                cmd += ["-stream_loop", "-1", "-re", "-i", bg_path]
            else:
                cmd += ["-loop", "1", "-i", bg_path]
        else:
            cmd += ["-f", "lavfi", "-i", "color=c=black:s=1920x1080:r=30"]

        # PCM audio input from FIFO (no -re here — feeder controls timing via decode)
        cmd += [
            "-f", _PCM_FORMAT,
            "-ar", str(_PCM_RATE),
            "-ac", str(_PCM_CHANNELS),
            "-i", self._fifo_path,
        ]

        # Initial overlay text (empty, feeder updates it via drawtext reload)
        font = self._font_path
        overlay = (
            f"drawtext=fontfile='{font}'"
            f":text='':fontsize=28:fontcolor=white"
            f":borderw=2:bordercolor=black"
            f":x=(w-text_w)/2:y=h-60"
            f":reload=1:textfile='{self._fifo_path}.txt'"
        )

        # Create initial textfile
        with open(f"{self._fifo_path}.txt", "w", encoding="utf-8") as fh:
            fh.write("")

        vf = f"scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,format=yuv420p,{overlay}"

        cmd += [
            "-vf", vf,
            "-c:v", "libx264", "-profile:v", "main", "-level", "4.0",
            "-preset", "ultrafast",
            "-pix_fmt", "yuv420p",
            "-b:v", "2500k", "-maxrate", "2500k", "-bufsize", "5000k",
            "-r", "30",
            "-g", "60", "-keyint_min", "60",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
            "-f", "flv", rtmp_url,
        ]

        print(f"[radio] Starting encoder: {' '.join(cmd[:8])} ...")
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        return proc

    async def _feed_loop(self):
        """Decode each MP3 and write raw PCM into the FIFO, back-to-back."""
        try:
            # Open FIFO for writing (blocks until encoder opens it for reading)
            loop = asyncio.get_event_loop()
            fifo_fd = await loop.run_in_executor(
                None, lambda: open(self._fifo_path, "wb")  # noqa
            )
        except Exception as e:
            print(f"[radio] Could not open FIFO: {e}")
            self.is_running = False
            return

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

                # Update overlay text file
                self._update_overlay_text(song)

                print(f"[radio] Now feeding: {song['title']} by {song['artist']}")
                self._skip_event.clear()
                await self._feed_song(audio_path, fifo_fd)

                if self.is_running:
                    self.current_index += 1

        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[radio] Feeder error: {e}")
        finally:
            try:
                fifo_fd.close()
            except Exception:
                pass
            # Terminate encoder gracefully
            if self._encoder:
                try:
                    self._encoder.terminate()
                except Exception:
                    pass
            self.is_running = False
            self.current_song = None
            print("[radio] Feeder stopped.")

    def _update_overlay_text(self, song: dict):
        """Write current song info to the textfile that drawtext:reload reads."""
        def esc(text):
            # drawtext textfile does not need escaping for most chars
            return text.replace("\n", " ")
        text = f"{esc(song['title'])}  —  {esc(song['artist'])}"
        try:
            with open(f"{self._fifo_path}.txt", "w", encoding="utf-8") as fh:
                fh.write(text)
        except Exception as e:
            print(f"[radio] Could not update overlay text: {e}")

    async def _feed_song(self, audio_path: str, fifo_fd):
        """Decode one MP3 to raw PCM and stream it into the FIFO.
        Returns when the song ends or skip is requested."""
        cmd = [
            "ffmpeg", "-v", "quiet",
            "-i", audio_path,
            "-f", _PCM_FORMAT,
            "-ar", str(_PCM_RATE),
            "-ac", str(_PCM_CHANNELS),
            "pipe:1",
        ]
        decoder = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

        try:
            while True:
                if self._skip_event.is_set():
                    decoder.terminate()
                    break
                chunk = await decoder.stdout.read(65536)
                if not chunk:
                    break
                await asyncio.get_event_loop().run_in_executor(
                    None, fifo_fd.write, chunk
                )
        except asyncio.CancelledError:
            decoder.terminate()
            raise
        except BrokenPipeError:
            # Encoder died
            decoder.terminate()
            self.is_running = False
        finally:
            try:
                await asyncio.wait_for(decoder.wait(), timeout=3)
            except Exception:
                pass
