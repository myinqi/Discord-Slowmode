"""Manages ffmpeg streaming to Twitch RTMP.

Uses ffmpeg's concat demuxer to play songs back-to-back in a single process,
ensuring seamless transitions without stream interruptions.
"""

import asyncio
import os
import random
import re
import shutil
import tempfile
import time
import unicodedata

import aiohttp

_EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001FAFF"  # emoticons, symbols, pictographs, transport, maps
    "\U00002702-\U000027B0"  # dingbats
    "\U0000FE00-\U0000FE0F"  # variation selectors
    "\U0000200D"              # zero width joiner
    "\U000020E3"              # combining enclosing keycap
    "\U00002600-\U000026FF"  # misc symbols
    "\U0000231A-\U0000231B"  # watch, hourglass
    "\U00002934-\U00002935"  # arrows
    "\U000025AA-\U000025AB"  # squares
    "\U000025FB-\U000025FE"  # squares
    "\U00003030\U0000303D"   # wavy dash, part alternation mark
    "\U00003297\U00003299"   # circled ideograph
    "]+"
)

_PLAYLIST_REPEATS = 50
_LYRICS_LINE_DURATION = 6  # seconds per lyrics line on screen
_LYRICS_MAX_LINES = 8      # max lines shown at once


def _normalize_text(text: str) -> str:
    """Normalize fancy Unicode (mathematical bold etc.) to plain text and strip emoji."""
    text = unicodedata.normalize("NFKC", text)
    text = _EMOJI_RE.sub("", text)
    return text.strip()


class TwitchChat:
    """Minimal async Twitch IRC client for posting chat messages."""

    def __init__(self, oauth_token: str, channel: str):
        self._token = oauth_token if oauth_token.startswith("oauth:") else f"oauth:{oauth_token}"
        self._channel = channel.lower().lstrip("#")
        self._reader = None
        self._writer = None
        self._connected = False
        self._keepalive_task = None

    async def connect(self):
        try:
            self._reader, self._writer = await asyncio.open_connection(
                "irc.chat.twitch.tv", 6667,
            )
            self._writer.write(f"PASS {self._token}\r\n".encode())
            self._writer.write(f"NICK bot\r\n".encode())
            await self._writer.drain()
            # Wait for server welcome or auth failure
            authed = False
            for _ in range(30):  # read up to 30 lines
                line = await asyncio.wait_for(self._reader.readline(), timeout=5)
                msg = line.decode(errors="replace").strip()
                if not msg:
                    continue
                if "Welcome" in msg or "001" in msg:
                    authed = True
                    break
                if "Login authentication failed" in msg:
                    print(f"[radio] Twitch chat auth FAILED: {msg}")
                    self._connected = False
                    return
            if not authed:
                print("[radio] Twitch chat: no welcome received, auth may have failed")
            self._writer.write(f"JOIN #{self._channel}\r\n".encode())
            await self._writer.drain()
            self._connected = True
            self._keepalive_task = asyncio.create_task(self._keepalive())
            print(f"[radio] Twitch chat connected and authenticated to #{self._channel}")
        except Exception as e:
            print(f"[radio] Twitch chat connect error: {e}")
            self._connected = False

    async def _keepalive(self):
        """Read server messages and respond to PINGs to keep connection alive."""
        try:
            while self._connected and self._reader:
                line = await asyncio.wait_for(self._reader.readline(), timeout=300)
                msg = line.decode(errors="replace").strip()
                if msg.startswith("PING"):
                    pong = msg.replace("PING", "PONG", 1)
                    self._writer.write(f"{pong}\r\n".encode())
                    await self._writer.drain()
        except asyncio.TimeoutError:
            print("[radio] Twitch chat keepalive timeout")
            self._connected = False
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print(f"[radio] Twitch chat keepalive error: {e}")
            self._connected = False

    async def send(self, message: str):
        if not self._connected or not self._writer:
            print("[radio] Twitch chat: not connected, skipping message")
            return
        try:
            self._writer.write(f"PRIVMSG #{self._channel} :{message}\r\n".encode())
            await self._writer.drain()
            print(f"[radio] Twitch chat sent: {message[:80]}")
        except Exception as e:
            print(f"[radio] Twitch chat send error: {e}")
            self._connected = False

    async def disconnect(self):
        self._connected = False
        if self._keepalive_task:
            self._keepalive_task.cancel()
            try:
                await self._keepalive_task
            except Exception:
                pass
            self._keepalive_task = None
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:
                pass
            self._writer = None
            self._reader = None
        print("[radio] Twitch chat disconnected")


class StreamManager:
    def __init__(self, db, radio_dir: str):
        self.db = db
        self.radio_dir = radio_dir
        self._process = None
        self._tasks = []
        self.is_running = False
        self.current_index = 0
        self.current_song = None
        self.playlist = []
        self._temp_dir = None
        self._font_path = None
        self._start_time = None
        self._twitch_key = None
        self._overlay_path = None
        self._lyrics_path = None
        self._lyrics_cache = {}  # {song_id: [lines]}
        self._concat_start = 0
        self._twitch_chat = None

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
        self._twitch_key = await self.db.get_setting("radio_twitch_key")
        if not self._twitch_key:
            return {"error": "Twitch stream key not configured."}
        shuffle = (await self.db.get_setting("radio_shuffle") or "0") == "1"
        if shuffle:
            random.shuffle(self.playlist)
        self._font_path = await self._resolve_font()
        self._temp_dir = tempfile.mkdtemp(prefix="radio_")
        self._overlay_path = os.path.join(self._temp_dir, "nowplaying.txt")
        self._lyrics_path = os.path.join(self._temp_dir, "lyrics.txt")
        with open(self._lyrics_path, "w", encoding="utf-8") as f:
            f.write(" ")
        # Connect to Twitch chat if configured
        chat_token = await self.db.get_setting("radio_twitch_chat_token")
        chat_channel = await self.db.get_setting("radio_twitch_chat_channel")
        if chat_token and chat_channel:
            self._twitch_chat = TwitchChat(chat_token, chat_channel)
            await self._twitch_chat.connect()
        self.is_running = True
        self.current_index = 0
        await self._launch()
        return await self.get_status()

    async def stop(self):
        self.is_running = False
        await self._teardown()
        self._cleanup_temp()
        if self._twitch_chat:
            await self._twitch_chat.disconnect()
            self._twitch_chat = None
        self.current_song = None
        return await self.get_status()

    async def skip_next(self):
        if not self.is_running:
            return {"error": "Stream is not running."}
        self.current_index = (self.current_index + 1) % len(self.playlist)
        await self._teardown()
        await self._launch()
        return await self.get_status()

    async def skip_prev(self):
        if not self.is_running:
            return {"error": "Stream is not running."}
        self.current_index = (self.current_index - 1) % len(self.playlist)
        await self._teardown()
        await self._launch()
        return await self.get_status()

    async def reload_playlist(self):
        if not self.is_running:
            return {"error": "Stream is not running."}
        new_songs = await self.db.get_all_radio_songs(active_only=True)
        if not new_songs:
            return {"error": "No active songs found."}
        old_count = len(self.playlist)
        shuffle = (await self.db.get_setting("radio_shuffle") or "0") == "1"
        if shuffle:
            random.shuffle(new_songs)
        self.playlist = new_songs
        self.current_index = 0
        print(f"[radio] Playlist reloaded: {old_count} -> {len(self.playlist)} songs")
        await self._teardown()
        await self._launch()
        return await self.get_status()

    # ------------------------------------------------------------------ #

    async def _launch(self):
        self._concat_start = self.current_index
        self.current_song = self.playlist[self.current_index]
        self._write_overlay(self.current_song)
        playlist_path = self._build_concat_file()
        cmd = await self._build_cmd(playlist_path)
        # Debug: log command and playlist
        print(f"[radio] Starting encoder (song {self.current_index + 1}/{len(self.playlist)})")
        print(f"[radio] CMD: {' '.join(cmd[:12])} ...")
        try:
            with open(playlist_path) as pf:
                lines = pf.readlines()[:5]
                print(f"[radio] Concat file ({len(open(playlist_path).readlines())} lines): {lines}")
        except Exception as e:
            print(f"[radio] Could not read concat file: {e}")
        self._process = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        self._start_time = time.monotonic()
        self._restart_delay = 5
        self._restart_retries = 0
        self._tasks = [
            asyncio.create_task(self._track_now_playing()),
            asyncio.create_task(self._monitor_process()),
        ]

    async def _teardown(self):
        for t in self._tasks:
            t.cancel()
            try:
                await t
            except Exception:
                pass
        self._tasks = []
        if self._process:
            try:
                self._process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
            self._process = None

    def _cleanup_temp(self):
        if self._temp_dir:
            shutil.rmtree(self._temp_dir, ignore_errors=True)
            self._temp_dir = None

    async def _fetch_lyrics(self, suno_url: str) -> list[str]:
        """Scrape lyrics from a Suno song page. Returns list of text lines."""
        if not suno_url:
            return []
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(suno_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return []
                    html = await resp.text()
                    # Lyrics are in the metadata.prompt field inside Next.js RSC payload
                    # Format: self.__next_f.push([1,"48:T<hex>,"]) followed by push with the text
                    match = re.search(
                        r'self\.__next_f\.push\(\[1,"[0-9a-f]+:T[0-9a-f]+,"\]\)</script>'
                        r'<script>self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>',
                        html, re.DOTALL
                    )
                    if not match:
                        return []
                    raw = match.group(1)
                    # Unescape JSON string escapes
                    raw = raw.replace("\\n", "\n").replace("\\t", " ").replace('\\"', '"')
                    # Split into lines, strip emoji, skip empty
                    lines = []
                    for line in raw.split("\n"):
                        cleaned = _normalize_text(line)
                        if cleaned:
                            lines.append(cleaned)
                    print(f"[radio] Scraped {len(lines)} lyrics lines from {suno_url}")
                    return lines
        except Exception as e:
            print(f"[radio] Lyrics scrape error: {e}")
            return []

    def _write_lyrics(self, lines: list[str], offset: int):
        """Write a window of lyrics lines to the lyrics overlay file."""
        if not lines:
            text = " "
        else:
            start = offset % len(lines)
            window = []
            for i in range(_LYRICS_MAX_LINES):
                idx = (start + i) % len(lines)
                window.append(lines[idx])
            text = "\n".join(window)
        try:
            with open(self._lyrics_path, "w", encoding="utf-8") as fh:
                fh.write(text)
        except Exception as e:
            print(f"[radio] Lyrics write error: {e}")

    async def _resolve_font(self) -> str:
        """Return font family name for fontconfig-based rendering."""
        # Use fontconfig (font= instead of fontfile=) for automatic
        # per-character fallback across all installed Noto fonts
        print("[radio] Using fontconfig: font='Noto Sans'")
        return "Noto Sans"

    def _build_concat_file(self) -> str:
        path = os.path.join(self._temp_dir, "playlist.txt")
        n = len(self.playlist)
        with open(path, "w") as f:
            for _ in range(_PLAYLIST_REPEATS):
                for i in range(n):
                    idx = (self._concat_start + i) % n
                    audio = os.path.join(self.radio_dir, self.playlist[idx]["filename"])
                    if os.path.exists(audio):
                        safe = audio.replace("'", "'\\''")
                        f.write(f"file '{safe}'\n")
        return path

    def _write_overlay(self, song: dict):
        text = f"{song['title']}  \u2014  {song['artist']}"
        text = _normalize_text(text)
        try:
            with open(self._overlay_path, "w", encoding="utf-8") as fh:
                fh.write(text)
        except Exception as e:
            print(f"[radio] Overlay write error: {e}")

    async def _build_cmd(self, playlist_path: str) -> list:
        bg_filename = await self.db.get_setting("radio_background_filename")
        bg_type = await self.db.get_setting("radio_background_type") or "image"

        cmd = ["ffmpeg", "-y"]

        if bg_filename and os.path.exists(os.path.join(self.radio_dir, bg_filename)):
            bg_path = os.path.join(self.radio_dir, bg_filename)
            if bg_type == "video":
                cmd += ["-stream_loop", "-1", "-re", "-i", bg_path]
            else:
                cmd += ["-loop", "1", "-re", "-i", bg_path]
        else:
            cmd += ["-f", "lavfi", "-i", "color=c=black:s=1920x1080:r=30"]

        cmd += ["-f", "concat", "-safe", "0", "-i", playlist_path]

        # Explicit mapping: video from background (input 0), audio from concat (input 1)
        # This ignores embedded cover art in MP3 files that concat picks up
        cmd += ["-map", "0:v:0", "-map", "1:a:0"]

        # Use fontconfig for automatic fallback across all Noto fonts
        font = self._font_path
        now_playing = (
            f"drawtext=font='{font}'"
            f":textfile='{self._overlay_path}'"
            f":reload=1"
            f":fontsize=28:fontcolor=white"
            f":borderw=2:bordercolor=black"
            f":x=(w-text_w)/2:y=h-60"
        )
        lyrics = (
            f"drawtext=font='{font}'"
            f":textfile='{self._lyrics_path}'"
            f":reload=1"
            f":fontsize=22:fontcolor=white"
            f":borderw=1:bordercolor=black"
            f":x=40:y=(h-text_h)/2"
        )
        vf = (
            "scale=1920:1080:force_original_aspect_ratio=decrease,"
            "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,"
            f"format=yuv420p,{now_playing},{lyrics}"
        )

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
            "-f", "flv",
            f"rtmp://live.twitch.tv/app/{self._twitch_key}",
        ]
        return cmd

    async def _track_now_playing(self):
        n = len(self.playlist)
        durations = []
        ordered = []
        for i in range(n):
            idx = (self._concat_start + i) % n
            s = self.playlist[idx]
            durations.append(s.get("duration", 180))
            ordered.append(s)
        total = sum(durations)
        if total <= 0:
            return
        try:
            while self.is_running:
                elapsed = time.monotonic() - self._start_time
                pos = elapsed % total
                cumulative = 0.0
                song_i = 0
                for i, dur in enumerate(durations):
                    cumulative += dur
                    if pos < cumulative:
                        song_i = i
                        break
                actual = ordered[song_i]
                if self.current_song is None or self.current_song["id"] != actual["id"]:
                    self.current_song = actual
                    self.current_index = (self._concat_start + song_i) % n
                    self._write_overlay(actual)
                    print(f"[radio] Now playing: {actual['title']} by {actual['artist']}")
                    # Post to Twitch chat
                    if self._twitch_chat:
                        suno_url = actual.get("suno_url", "")
                        chat_msg = f"\U0001F3B5 Now Playing: {actual['title']} — {actual['artist']}"
                        if suno_url:
                            chat_msg += f" | {suno_url}"
                        await self._twitch_chat.send(chat_msg)
                    # Scrape lyrics async for this song
                    song_id = actual["id"]
                    if song_id not in self._lyrics_cache:
                        suno_url = actual.get("suno_url", "")
                        self._lyrics_cache[song_id] = await self._fetch_lyrics(suno_url)
                    self._lyrics_offset = 0
                    self._write_lyrics(self._lyrics_cache.get(song_id, []), 0)
                # Rotate lyrics lines periodically
                song_id = actual["id"]
                lyrics = self._lyrics_cache.get(song_id, [])
                if lyrics:
                    if not hasattr(self, "_lyrics_offset"):
                        self._lyrics_offset = 0
                    if not hasattr(self, "_lyrics_timer"):
                        self._lyrics_timer = 0.0
                    self._lyrics_timer += 1.0
                    if self._lyrics_timer >= _LYRICS_LINE_DURATION:
                        self._lyrics_timer = 0.0
                        self._lyrics_offset += 1
                        self._write_lyrics(lyrics, self._lyrics_offset)
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass

    async def _monitor_process(self):
        if not self._process:
            return
        try:
            # Stream stderr line by line for real-time logging
            while True:
                try:
                    line = await asyncio.wait_for(
                        self._process.stderr.readline(), timeout=60,
                    )
                except asyncio.TimeoutError:
                    # ffmpeg hung (likely RTMP connection lost)
                    print("[radio] Watchdog: no ffmpeg output for 60s, killing process")
                    try:
                        self._process.kill()
                    except Exception:
                        pass
                    break
                if not line:
                    break
                text = line.decode(errors="replace").rstrip()
                if text:
                    print(f"[radio/ffmpeg] {text}")
            code = await self._process.wait()
            if self.is_running:
                print(f"[radio] Encoder exited (code {code})")
                self._process = None
                # Exponential backoff for restarts
                delay = getattr(self, "_restart_delay", 5)
                retries = getattr(self, "_restart_retries", 0)
                if retries >= 10:
                    print("[radio] Too many restart attempts (10), giving up. Use admin panel to restart.")
                    self.is_running = False
                    return
                self._restart_retries = retries + 1
                print(f"[radio] Restarting encoder in {delay}s (attempt {self._restart_retries}/10)...")
                await asyncio.sleep(delay)
                self._restart_delay = min(delay * 2, 60)  # max 60s
                if self.is_running:
                    await self._launch()
        except asyncio.CancelledError:
            pass
