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
    """Return Dialogue lines from ASS content shifted by offset_cs centiseconds."""
    out = []
    for line in content.splitlines():
        match = re.match(
            r'(Dialogue:[^,]*,)(\d+:\d+:\d+\.\d+),(\d+:\d+:\d+\.\d+),(.*)',
            line.rstrip(),
        )
        if match:
            start = _cs_to_ts(_ts_to_cs(match.group(2)) + offset_cs)
            end   = _cs_to_ts(_ts_to_cs(match.group(3)) + offset_cs)
            out.append(f"{match.group(1)}{start},{end},{match.group(4)}")
    return out


class ExpStreamManager:
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

    # ── Public API ─────────────────────────────────────────────────────────────

    async def start(self, twitch_key: str) -> dict:
        if self.is_running:
            return {"ok": False, "error": "Stream already running."}
        songs = await self.db.get_all_exp_radio_songs(active_only=True)
        ready = [s for s in songs if s.get("analysis_status") == "done" and s.get("mp3_filename")]
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
                print(f"[exp-stream] Twitch chat disabled: {msg}", flush=True)
                self._twitch_chat = None
            else:
                print(f"[exp-stream] Twitch chat ready ({msg}).", flush=True)
        self.is_running  = True
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
        if self._twitch_chat:
            await self._twitch_chat.stop()
            self._twitch_chat = None
        print("[exp-stream] Stopped.", flush=True)
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
                print(f"[exp-stream] Playlist error: {e}", flush=True)
                await asyncio.sleep(3)

    async def _play_playlist(self, songs: list):
        """Run one FFmpeg job that covers all songs without stopping between them."""
        bg_fn   = await self.db.get_setting("exp_radio_bg_filename") or ""
        bg_type = await self.db.get_setting("exp_radio_bg_type") or "image"
        loop_fn = await self.db.get_setting("exp_radio_loop_filename") or ""
        bg_path   = os.path.join(self.exp_radio_dir, "assets", bg_fn)   if bg_fn   else None
        loop_path = os.path.join(self.exp_radio_dir, "assets", loop_fn) if loop_fn else None

        # Prefetch all covers/videos
        media_paths = []
        for song in songs:
            path = await self._get_video(song) or await self._get_cover(song)
            media_paths.append(path)
            print(f"[exp-stream] Media ready: {song.get('title')} → {path}", flush=True)

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
        print(f"[exp-stream] FFmpeg playlist: {titles}", flush=True)

        self._process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        chat_task = asyncio.create_task(self._post_now_playing_loop(songs))
        _, stderr = await self._process.communicate()
        chat_task.cancel()
        try:
            await chat_task
        except asyncio.CancelledError:
            pass
        rc = self._process.returncode
        if rc and rc != 0 and self.is_running:
            err_tail = (stderr or b"")[-800:].decode("utf-8", errors="replace")
            print(f"[exp-stream] FFmpeg exited {rc}:\n{err_tail}", flush=True)

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
            return dest
        url = song.get("cover_url") or f"https://cdn1.suno.ai/image_large_{uuid}.jpeg"
        try:
            async with aiohttp.ClientSession(headers={"User-Agent": _BROWSER_UA}) as sess:
                async with sess.get(url, timeout=aiohttp.ClientTimeout(total=20)) as r:
                    if r.status == 200:
                        with open(dest, "wb") as f:
                            f.write(await r.read())
                        return dest
        except Exception as e:
            print(f"[exp-stream] Cover download error ({uuid}): {e}", flush=True)
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
            return dest
        try:
            async with aiohttp.ClientSession(headers={"User-Agent": _BROWSER_UA}) as sess:
                async with sess.get(video_url, timeout=aiohttp.ClientTimeout(total=60)) as r:
                    if r.status == 200:
                        with open(dest, "wb") as f:
                            f.write(await r.read())
                        print(f"[exp-stream] Video cached: {uuid}.mp4", flush=True)
                        return dest
        except Exception as e:
            print(f"[exp-stream] Video download error ({uuid}): {e}", flush=True)
        return None

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
            title  = (song.get("title")  or "Unknown").replace("\\", "\\\\").replace("{", "\\{")
            artist = (song.get("artist") or "").replace("\\", "\\\\").replace("{", "\\{")
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
