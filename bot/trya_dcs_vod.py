from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import textwrap
import time
from datetime import datetime, timezone

from collections import defaultdict
from urllib.request import Request, urlopen

from bot.trya_dcs_events import trya_dcs_events

_CUSTOM_EMOJI_RE = re.compile(r"<a?:([A-Za-z0-9_]+):(\d{17,20})>")
_EMOJI_PLACEHOLDER = "\u25a1"


def chat_text_with_emoji_slots(content: str) -> tuple[str, list[tuple[str, str]]]:
    """Replace Discord custom emojis with a wide slot; return slots in order."""
    slots: list[tuple[str, str]] = []

    def replace(match: re.Match) -> str:
        slots.append((match.group(1), match.group(2)))
        return _EMOJI_PLACEHOLDER

    return _CUSTOM_EMOJI_RE.sub(replace, str(content or "")), slots


def rendered_keep_limit(value: str | None) -> int | None:
    """None means keep every rendered VOD."""
    if str(value or "") in {"4", "8"}:
        return int(value)
    return None


def select_oldest_rendered_to_delete(
    vods: list[dict],
    keep: int | None,
    protect_id: str | None = None,
) -> list[str]:
    if keep is None or keep < 1:
        return []
    ready = [
        item for item in vods
        if item.get("rendered_exists") or item.get("status") == "ready"
    ]
    ready.sort(key=lambda item: float(item.get("started_at") or 0))
    overflow = len(ready) - keep
    if overflow <= 0:
        return []
    removed: list[str] = []
    for item in ready:
        vod_id = str(item.get("id") or "")
        if not vod_id or vod_id == protect_id:
            continue
        removed.append(vod_id)
        if len(removed) >= overflow:
            break
    return removed


def classify_vod_stop(
    *,
    interrupted: bool,
    master_ok: bool,
    assembly_error: str = "",
    recorder_error: str = "",
) -> tuple[str, str]:
    """Keep a usable recording even when FFmpeg is stopped with the live stream."""
    if not master_ok:
        return "failed", (
            assembly_error
            or recorder_error
            or "Recorder did not produce a valid master file."
        )
    if interrupted:
        return "interrupted", ""
    return "master_ready", ""


def ffmpeg_tee_escape(value: str) -> str:
    return str(value or "").replace("\\", "\\\\").replace(":", "\\:").replace("|", "\\|")


def ffmpeg_live_output_args(stream_target: str, vod_path: str | None = None) -> list[str]:
    """Live output stays a single RTMP/FLV publisher.

    VOD is recorded separately from MediaMTX HLS so a local muxer cannot
    reset the live ingest connection.
    """
    del vod_path
    return ["-f", "flv", stream_target]


def hls_url_from_rtmp(rtmp_url: str) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(str(rtmp_url or "").strip())
    host = parsed.hostname or "mediamtx"
    path = (parsed.path or "").strip("/") or "trya-dcs"
    return f"http://{host}:8888/{path}/index.m3u8"


def safe_stop_target_out_time(
    out_time: float | None,
    remaining: float,
    stored_duration: float,
    probed_duration: float | None,
    extra: float = 2.0,
) -> float | None:
    """FFmpeg clock time at which the current song has actually finished."""
    if out_time is None:
        return None
    stored = max(0.0, float(stored_duration or 0))
    probed = float(probed_duration or 0)
    if probed > stored:
        extra += probed - stored
    return float(out_time) + max(0.0, float(remaining or 0)) + extra


class DcsVodManager:
    def __init__(self, db, base_dir: str, logger):
        self.db = db
        self.base_dir = os.path.abspath(os.path.join(base_dir, "vods"))
        self.log = logger
        self.active: dict | None = None
        self.recorder = None
        self.capture_task = None
        self.capture_ready = asyncio.Event()
        self.render_tasks: dict[str, asyncio.Task] = {}
        os.makedirs(self.base_dir, exist_ok=True)
        self._recover_stale_jobs()

    def _directory(self, vod_id: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_-]", "", str(vod_id))
        if not safe or safe != vod_id:
            raise ValueError("invalid VOD id")
        path = os.path.abspath(os.path.join(self.base_dir, safe))
        if os.path.commonpath((path, self.base_dir)) != self.base_dir:
            raise ValueError("invalid VOD path")
        return path

    def _metadata_path(self, vod_id: str) -> str:
        return os.path.join(self._directory(vod_id), "metadata.json")

    def _write_metadata(self, metadata: dict) -> None:
        path = self._metadata_path(metadata["id"])
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(metadata, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def _recover_stale_jobs(self) -> None:
        try:
            names = os.listdir(self.base_dir)
        except OSError:
            return
        for vod_id in names:
            try:
                path = self._metadata_path(vod_id)
                with open(path, encoding="utf-8") as handle:
                    metadata = json.load(handle)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            directory = self._directory(vod_id)
            if metadata.get("status") == "recording":
                master = os.path.join(directory, "master.mp4")
                mp4_partial = os.path.join(directory, "master.partial.mp4")
                if os.path.isfile(mp4_partial) and os.path.getsize(mp4_partial) > 0:
                    os.replace(mp4_partial, master)
                ts_partial = os.path.join(directory, "master.partial.ts")
                if (
                    (not os.path.isfile(master) or os.path.getsize(master) <= 0)
                    and os.path.isfile(ts_partial)
                    and os.path.getsize(ts_partial) > 0
                ):
                    os.replace(ts_partial, os.path.join(directory, "part_001.ts"))
                    metadata.setdefault("parts", []).append("part_001.ts")
                metadata["status"] = "interrupted"
                metadata["ended_at"] = metadata.get("ended_at") or time.time()
                metadata["error"] = "Recording was interrupted by a process restart."
                self._write_metadata(metadata)
            elif metadata.get("status") == "rendering":
                metadata["status"] = "render_failed"
                metadata["error"] = "Rendering was interrupted by a process restart."
                self._write_metadata(metadata)

    def get(self, vod_id: str) -> dict | None:
        try:
            with open(self._metadata_path(vod_id), encoding="utf-8") as handle:
                metadata = json.load(handle)
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        directory = self._directory(vod_id)
        for key, filename in (("master", "master.mp4"), ("rendered", "rendered.mp4"), ("events", "events.jsonl")):
            path = os.path.join(directory, filename)
            metadata[f"{key}_exists"] = os.path.isfile(path)
            metadata[f"{key}_size"] = os.path.getsize(path) if os.path.isfile(path) else 0
        return metadata

    def list(self) -> list[dict]:
        entries = []
        try:
            names = os.listdir(self.base_dir)
        except OSError:
            names = []
        for name in names:
            metadata = self.get(name)
            if metadata:
                entries.append(metadata)
        return sorted(entries, key=lambda item: float(item.get("started_at") or 0), reverse=True)

    async def enabled(self) -> bool:
        return (await self.db.get_setting("trya_dcs_vod_record_enabled") or "off") == "on"

    def _partial_path(self, metadata: dict) -> str:
        return os.path.join(self._directory(metadata["id"]), "master.partial.mp4")

    def sidecar_bytes(self) -> int:
        if not self.active:
            return 0
        directory = self._directory(self.active["id"])
        total = 0
        for name in os.listdir(directory) if os.path.isdir(directory) else []:
            if name.startswith("master.partial") or name.startswith("part_"):
                path = os.path.join(directory, name)
                if os.path.isfile(path):
                    total += os.path.getsize(path)
        return total

    def describe_files(self, vod_id: str | None = None) -> str:
        metadata = self.active if vod_id is None else {"id": vod_id}
        if not metadata:
            return "no active VOD"
        directory = self._directory(metadata["id"])
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            return f"{directory} (unreadable)"
        parts = []
        for name in names:
            path = os.path.join(directory, name)
            if os.path.isfile(path):
                parts.append(f"{name}={os.path.getsize(path)}")
        return ", ".join(parts) or "empty directory"

    async def attach(self, playlist: list[dict]) -> str | None:
        """Prepare a same-encode fragmented MP4 sidecar and return its path."""
        if self.active:
            self.active.setdefault("playlist", []).extend(playlist)
            await self._archive_partial(self.active)
            self._write_metadata(self.active)
            return self._partial_path(self.active)
        if not await self.enabled():
            return None
        now = time.time()
        vod_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        directory = self._directory(vod_id)
        suffix = 1
        while os.path.exists(directory):
            vod_id = f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{suffix}"
            directory = self._directory(vod_id)
            suffix += 1
        os.makedirs(directory)
        metadata = {
            "id": vod_id,
            "status": "recording",
            "started_at": now,
            "ended_at": None,
            "duration": 0,
            "playlist": playlist,
            "parts": [],
            "render_resolution": await self.db.get_setting("trya_dcs_vod_resolution") or "720",
            "error": "",
        }
        self._write_metadata(metadata)
        self.active = metadata
        events_path = os.path.join(directory, "events.jsonl")
        self.capture_ready.clear()
        self.capture_task = asyncio.create_task(self._capture_events(events_path, now))
        await asyncio.wait_for(self.capture_ready.wait(), timeout=5)
        self.log(f"VOD recording started: {vod_id} (HLS stream copy).")
        return self._partial_path(metadata)

    async def start_hls_recorder(self, hls_url: str) -> None:
        try:
            if not self.active or not hls_url:
                return
            if self.recorder and self.recorder.returncode is None:
                return
            await asyncio.sleep(2)
            if not self.active:
                return
            partial = os.path.join(self._directory(self.active["id"]), "master.partial.ts")
            command = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
                "-live_start_index", "-3", "-rw_timeout", "15000000",
                "-i", hls_url,
                "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
                "-f", "mpegts",
                partial,
            ]
            self.recorder = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            self.log(f"VOD HLS recorder started ({hls_url}).")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.log(f"VOD HLS recorder failed: {exc}", "error")

    async def _archive_partial(self, metadata: dict) -> None:
        directory = self._directory(metadata["id"])
        parts = metadata.setdefault("parts", [])
        archived = False
        for name in ("master.partial.mp4", "master.partial.ts"):
            partial = os.path.join(directory, name)
            if not os.path.isfile(partial) or os.path.getsize(partial) <= 0:
                continue
            ext = os.path.splitext(name)[1]
            filename = f"part_{len(parts) + 1:03d}{ext}"
            os.replace(partial, os.path.join(directory, filename))
            parts.append(filename)
            size = os.path.getsize(os.path.join(directory, filename))
            self.log(f"VOD archived {filename} ({size / 1048576:.1f} MiB).")
            archived = True
        if archived:
            self._write_metadata(metadata)

    async def _capture_events(self, path: str, started_at: float) -> None:
        try:
            with open(path, "a", encoding="utf-8", buffering=1) as handle:
                async with trya_dcs_events.subscribe() as queue:
                    self.capture_ready.set()
                    while True:
                        event = await queue.get()
                        record = {
                            "time": max(0.0, time.time() - started_at),
                            "type": event.get("type"),
                            "data": event.get("data") or {},
                        }
                        handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self.log(f"VOD event capture failed: {exc}", "error")

    async def stop(self, interrupted: bool = False) -> dict | None:
        if not self.active:
            return None
        metadata = self.active
        self.active = None
        if self.capture_task:
            self.capture_task.cancel()
            try:
                await self.capture_task
            except asyncio.CancelledError:
                pass
        self.capture_task = None
        process = self.recorder
        self.recorder = None
        recorder_error = await self._stop_recorder(process)
        self.log(f"VOD files before assemble: {self.describe_files(metadata['id'])}")
        await self._archive_partial(metadata)
        directory = self._directory(metadata["id"])
        master = os.path.join(directory, "master.mp4")
        assembly_error = ""
        try:
            await self._assemble_master(metadata, master)
        except Exception as exc:
            assembly_error = str(exc)
            self.log(f"VOD assemble failed: {exc}", "error")
        if not os.path.isfile(master) or os.path.getsize(master) <= 0:
            for leftover_name in ("master.partial.mp4", "master.partial.ts"):
                leftover = os.path.join(directory, leftover_name)
                if not os.path.isfile(leftover) or os.path.getsize(leftover) <= 0:
                    continue
                try:
                    await self._remux_copy(leftover, master)
                except Exception as exc:
                    assembly_error = assembly_error or str(exc)
                    if leftover_name.endswith(".mp4") and not os.path.isfile(master):
                        os.replace(leftover, master)
                if os.path.isfile(master) and os.path.getsize(master) > 0:
                    break
        metadata["ended_at"] = time.time()
        metadata["duration"] = await self._probe_duration(master)
        master_ok = os.path.isfile(master) and os.path.getsize(master) > 0
        status, error = classify_vod_stop(
            interrupted=interrupted,
            master_ok=master_ok,
            assembly_error=assembly_error,
            recorder_error=recorder_error,
        )
        metadata["status"] = status
        if error:
            metadata["error"] = error
        elif status in {"master_ready", "interrupted"}:
            metadata["error"] = ""
        self._write_metadata(metadata)
        size_mib = (os.path.getsize(master) / 1048576) if master_ok else 0
        self.log(
            f"VOD recording stopped: {metadata['id']} ({metadata['status']}"
            f"{f', {metadata['duration']:.1f}s' if metadata['duration'] else ''}"
            f"{f', {size_mib:.1f} MiB' if size_mib else ''})."
        )
        if (
            metadata["status"] in {"master_ready", "interrupted"}
            and metadata["duration"] >= 5
            and (os.path.getsize(master) if os.path.isfile(master) else 0) >= 500_000
            and (await self.db.get_setting("trya_dcs_vod_auto_render") or "off") == "on"
        ):
            await self.queue_render(metadata["id"])
        elif metadata["status"] in {"master_ready", "interrupted"} and (
            await self.db.get_setting("trya_dcs_vod_auto_render") or "off"
        ) == "on":
            self.log("VOD auto-render skipped; master is too short to render.")
        return metadata

    async def _stop_recorder(self, process) -> str:
        if process is None:
            return ""
        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=8)
            except asyncio.TimeoutError:
                try:
                    process.send_signal(signal.SIGINT)
                    await asyncio.wait_for(process.wait(), timeout=20)
                except Exception:
                    try:
                        process.kill()
                        await process.wait()
                    except ProcessLookupError:
                        pass
        return ""

    async def _remux_copy(self, source: str, destination: str) -> None:
        command = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
            "-err_detect", "ignore_err", "-fflags", "+genpts+discardcorrupt",
            "-analyzeduration", "10000000", "-probesize", "10000000",
            "-i", source, "-c", "copy",
        ]
        if source.endswith(".ts"):
            command += ["-bsf:a", "aac_adtstoasc"]
        command += ["-movflags", "+faststart", destination]
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0 or not os.path.isfile(destination) or os.path.getsize(destination) <= 0:
            detail = stderr.decode("utf-8", errors="replace")[-2000:]
            self.log(f"VOD remux failed for {os.path.basename(source)}: {detail}", "error")
            raise RuntimeError("VOD remux failed: " + detail)

    async def _assemble_master(self, metadata: dict, master: str) -> None:
        directory = self._directory(metadata["id"])
        parts = [
            os.path.join(directory, filename)
            for filename in metadata.get("parts") or []
            if os.path.isfile(os.path.join(directory, filename))
        ]
        if not parts:
            return
        if len(parts) == 1:
            try:
                await self._remux_copy(parts[0], master)
            except Exception:
                os.replace(parts[0], master)
            else:
                try:
                    os.remove(parts[0])
                except FileNotFoundError:
                    pass
            metadata["parts"] = []
            return
        concat_path = os.path.join(directory, "parts.txt")
        with open(concat_path, "w", encoding="utf-8") as handle:
            for path in parts:
                handle.write("file '" + path.replace("'", "'\\''") + "'\n")
        concat_cmd = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning",
            "-f", "concat", "-safe", "0", "-i", concat_path,
            "-c", "copy",
        ]
        if any(path.endswith(".ts") for path in parts):
            concat_cmd += ["-bsf:a", "aac_adtstoasc"]
        concat_cmd += ["-movflags", "+faststart", master]
        process = await asyncio.create_subprocess_exec(
            *concat_cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0:
            raise RuntimeError(
                "VOD part assembly failed: "
                + stderr.decode("utf-8", errors="replace")[-2000:]
            )
        for path in parts:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
        try:
            os.remove(concat_path)
        except FileNotFoundError:
            pass
        metadata["parts"] = []

    async def _probe_duration(self, path: str) -> float:
        if not path or not os.path.isfile(path):
            return 0.0
        process = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await process.communicate()
        try:
            return max(0.0, float(stdout.decode().strip() or 0))
        except ValueError:
            return 0.0

    async def queue_render(self, vod_id: str, resolution: str | None = None) -> bool:
        metadata = self.get(vod_id)
        if not metadata or not metadata.get("master_exists"):
            return False
        task = self.render_tasks.get(vod_id)
        if task and not task.done():
            return False
        if resolution in {"720", "1080"}:
            metadata["render_resolution"] = resolution
        metadata["status"] = "rendering"
        metadata["error"] = ""
        self._write_metadata(metadata)
        self.render_tasks[vod_id] = asyncio.create_task(self._render(metadata))
        return True

    async def _render(self, metadata: dict) -> None:
        vod_id = metadata["id"]
        directory = self._directory(vod_id)
        master = os.path.join(directory, "master.mp4")
        output_tmp = os.path.join(directory, "rendered.partial.mp4")
        output = os.path.join(directory, "rendered.mp4")
        ass_path = os.path.join(directory, "vod.ass")
        try:
            width, height = (1920, 1080) if metadata.get("render_resolution") == "1080" else (1280, 720)
            video_width = int(width * 0.75)
            top_height = int(height * 0.75)
            chat_width = width - video_width
            bottom_height = height - top_height
            overlays = self._build_ass(
                metadata, ass_path, width, height, video_width, top_height, chat_width, bottom_height
            )
            await self._download_emoji_assets(directory, overlays)
            escaped_ass = ass_path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
            base = (
                f"scale={video_width}:{top_height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:0:0:color=0x08070c,"
                f"drawbox=x={video_width}:y=0:w={chat_width}:h={top_height}:color=0x12101a:t=fill,"
                f"drawbox=x=0:y={top_height}:w={width}:h={bottom_height}:color=0x0d0b12:t=fill,"
                f"drawbox=x={video_width}:y=0:w=1:h={top_height}:color=0x3a3247:t=fill,"
                f"drawbox=x=0:y={top_height}:w={width}:h=1:color=0x3a3247:t=fill,"
                f"subtitles='{escaped_ass}'"
            )
            bitrate = "3200k" if height == 1080 else "1800k"
            command = [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "warning", "-i", master,
            ]
            overlay_inputs, overlay_filters, overlay_map = self._emoji_overlay_graph(directory, overlays)
            command += overlay_inputs
            if overlay_filters:
                command += ["-filter_complex", f"[0:v]{base}[base];{overlay_filters}", "-map", overlay_map]
            else:
                command += ["-vf", base, "-map", "0:v:0"]
            command += [
                "-map", "0:a:0?",
                "-c:v", "libx264", "-preset", "ultrafast", "-b:v", bitrate,
                "-maxrate", bitrate, "-bufsize", str(int(bitrate[:-1]) * 2) + "k",
                "-pix_fmt", "yuv420p", "-r", "30", "-g", "60",
                "-c:a", "copy", "-movflags", "+faststart", output_tmp,
            ]
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await process.communicate()
            if process.returncode != 0:
                raise RuntimeError(stderr.decode("utf-8", errors="replace")[-3000:])
            os.replace(output_tmp, output)
            metadata["status"] = "ready"
            metadata["rendered_at"] = time.time()
            if (await self.db.get_setting("trya_dcs_vod_keep_master") or "on") != "on":
                try:
                    os.remove(master)
                except FileNotFoundError:
                    pass
            self.log(f"VOD render ready: {vod_id} ({height}p).")
            self._write_metadata(metadata)
            await self.prune_rendered(protect_id=vod_id)
        except Exception as exc:
            metadata["status"] = "render_failed"
            metadata["error"] = str(exc)[-3000:]
            self.log(f"VOD render failed for {vod_id}: {exc}", "error")
            try:
                os.remove(output_tmp)
            except FileNotFoundError:
                pass
        finally:
            self._write_metadata(metadata)

    @staticmethod
    def _ass_time(seconds: float) -> str:
        seconds = max(0.0, float(seconds))
        hours = int(seconds // 3600)
        minutes = int(seconds % 3600 // 60)
        secs = seconds % 60
        return f"{hours}:{minutes:02d}:{secs:05.2f}"

    @staticmethod
    def _ass_escape(value: str) -> str:
        return str(value or "").replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("\n", "\\N")

    @staticmethod
    def _ass_color(value: str, fallback: str = "&H00FFFFFF") -> str:
        match = re.fullmatch(r"#([0-9A-Fa-f]{6})", str(value or ""))
        if not match:
            return fallback
        rgb = match.group(1)
        return f"&H00{rgb[4:6]}{rgb[2:4]}{rgb[0:2]}"

    def _message_text(self, data: dict) -> str:
        content, _slots = self._message_body(data)
        return content.replace(_EMOJI_PLACEHOLDER, " ")

    def _message_body(self, data: dict) -> tuple[str, list[tuple[str, str]]]:
        content = str(data.get("content") or "")
        mentions = {str(item.get("id")): item.get("display_name") or "Member" for item in data.get("mentions") or []}
        content = re.sub(r"<@!?(\d+)>", lambda match: "@" + mentions.get(match.group(1), "Member"), content)
        content, slots = chat_text_with_emoji_slots(content)
        attachments = data.get("attachments") or []
        if attachments:
            labels = ", ".join(item.get("filename") or "attachment" for item in attachments[:3])
            content += f" [Attachment: {labels}]"
        if data.get("reply"):
            content = f"Reply to {data['reply'].get('author')}: {content}"
        return content.strip(), slots

    async def _download_emoji_assets(self, directory: str, overlays: list[dict]) -> None:
        cache = os.path.join(directory, "emoji_cache")
        os.makedirs(cache, exist_ok=True)
        seen: set[str] = set()
        for overlay in overlays:
            emoji_id = overlay.get("id")
            if not emoji_id or emoji_id in seen:
                continue
            seen.add(emoji_id)
            path = os.path.join(cache, f"{emoji_id}.png")
            overlay["path"] = path
            if os.path.isfile(path) and os.path.getsize(path) > 8:
                with open(path, "rb") as handle:
                    if handle.read(8).startswith(b"\x89PNG"):
                        continue
            url = f"https://cdn.discordapp.com/emojis/{emoji_id}.png?size=128&quality=lossless"
            try:
                await asyncio.to_thread(self._fetch_url, url, path)
            except Exception as exc:
                self.log(f"VOD emoji {emoji_id} download failed: {exc}", "error")
        for overlay in overlays:
            emoji_id = overlay.get("id")
            path = os.path.join(cache, f"{emoji_id}.png") if emoji_id else ""
            overlay["path"] = path if path and os.path.isfile(path) and os.path.getsize(path) > 0 else ""

    @staticmethod
    def _fetch_url(url: str, path: str) -> None:
        request = Request(url, headers={"User-Agent": "TrYaDCS-VOD/1.0"})
        with urlopen(request, timeout=12) as response:
            payload = response.read()
        if not payload or not payload.startswith(b"\x89PNG"):
            raise RuntimeError("emoji image was not a PNG")
        tmp = f"{path}.tmp"
        with open(tmp, "wb") as handle:
            handle.write(payload)
        os.replace(tmp, path)

    def _emoji_overlay_graph(self, directory: str, overlays: list[dict]) -> tuple[list[str], str, str]:
        usable = [item for item in overlays if item.get("path") and os.path.isfile(item["path"])][:80]
        if not usable:
            return [], "", "0:v:0"
        inputs: list[str] = []
        id_to_index: dict[str, int] = {}
        for item in usable:
            emoji_id = item["id"]
            if emoji_id in id_to_index:
                continue
            id_to_index[emoji_id] = len(id_to_index) + 1
            inputs += ["-i", item["path"]]
        filters: list[str] = []
        last = "base"
        for index, item in enumerate(usable):
            src = f"em{index}"
            size = max(16, int(item["size"]))
            filters.append(
                f"[{id_to_index[item['id']]}:v]scale={size}:{size}:force_original_aspect_ratio=decrease,"
                f"format=rgba,pad={size}:{size}:(ow-iw)/2:(oh-ih)/2:color=0x00000000[{src}]"
            )
            nxt = "vout" if index == len(usable) - 1 else f"ov{index}"
            filters.append(
                f"[{last}][{src}]overlay={int(item['x'])}:{int(item['y'])}:"
                f"enable='between(t,{item['start']:.3f},{item['end']:.3f})'[{nxt}]"
            )
            last = nxt
        return inputs, ";".join(filters), "[vout]"

    def _build_ass(self, metadata: dict, path: str, width: int, height: int, video_width: int, top_height: int, chat_width: int, bottom_height: int) -> list[dict]:
        duration = float(metadata.get("duration") or 0)
        scale = height / 720
        chat_font = max(18, int(22 * scale))
        title_font = max(20, int(25 * scale))
        small_font = max(15, int(18 * scale))
        header = (
            "[Script Info]\nScriptType: v4.00+\n"
            f"PlayResX: {width}\nPlayResY: {height}\nWrapStyle: 2\n\n"
            "[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
            f"Style: Chat,Noto Sans,{chat_font},&H00E8E2EF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1,0,7,0,0,0,1\n"
            f"Style: Song,Noto Sans,{title_font},&H00FFFFFF,&H000000FF,&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,1,0,7,0,0,0,1\n"
            f"Style: Small,Noto Sans,{small_font},&H00958C9F,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,1,0,7,0,0,0,1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        )
        events = []
        events.append(f"Dialogue: 2,0:00:00.00,{self._ass_time(duration)},Song,,0,0,0,,{{\\pos({video_width + int(18*scale)},{int(22*scale)})}}Discord Chat")
        self._append_song_events(events, metadata.get("playlist") or [], duration, width, top_height, bottom_height, scale)
        overlays = self._append_chat_events(
            events,
            os.path.join(self._directory(metadata["id"]), "events.jsonl"),
            duration,
            video_width,
            top_height,
            chat_width,
            scale,
            chat_font,
        )
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(header)
            for event in events:
                handle.write(event + "\n")
        return overlays

    def _append_song_events(self, events: list[str], playlist: list[dict], duration: float, width: int, top_height: int, bottom_height: int, scale: float) -> None:
        starts = []
        cursor = 0.0
        for song in playlist:
            starts.append(cursor)
            cursor += float(song.get("duration") or 0)
        for index, song in enumerate(playlist):
            start = starts[index]
            end = min(duration, starts[index + 1] if index + 1 < len(starts) else duration)
            if end <= start:
                continue
            previous = playlist[index - 1] if index else None
            next_song = playlist[index + 1] if index + 1 < len(playlist) else None
            columns = [
                ("PREVIOUS", previous, int(25 * scale)),
                ("NOW PLAYING", song, width // 3 + int(25 * scale)),
                ("NEXT", next_song, width * 2 // 3 + int(25 * scale)),
            ]
            for label, item, x in columns:
                title = self._ass_escape((item or {}).get("title") or "—")
                artist = self._ass_escape((item or {}).get("artist") or "")
                y = top_height + int(30 * scale)
                text = f"{{\\pos({x},{y})}}{label}\\N{title}"
                if artist:
                    text += f"\\N{{\\fs{max(14, int(17*scale))}\\c&H00958C9F&}}{artist}"
                events.append(f"Dialogue: 3,{self._ass_time(start)},{self._ass_time(end)},Song,,0,0,0,,{text}")

    def _append_chat_events(
        self,
        events: list[str],
        sidecar: str,
        duration: float,
        video_width: int,
        top_height: int,
        chat_width: int,
        scale: float,
        chat_font: int,
    ) -> list[dict]:
        records = []
        try:
            with open(sidecar, encoding="utf-8") as handle:
                records = [json.loads(line) for line in handle if line.strip()]
        except (OSError, json.JSONDecodeError):
            records = []
        chat_records = [record for record in records if str(record.get("type") or "").startswith("chat.")]
        messages: dict[str, dict] = {}
        order: list[str] = []
        snapshots = []
        for record in chat_records:
            data = record.get("data") or {}
            message_id = str(data.get("message_id") or "")
            event_type = record.get("type")
            if event_type in {"chat.message", "chat.edit"} and message_id:
                messages[message_id] = data
                if message_id not in order:
                    order.append(message_id)
            elif event_type == "chat.delete" and message_id:
                messages.pop(message_id, None)
                order = [item for item in order if item != message_id]
            snapshots.append((float(record.get("time") or 0), [messages[item] for item in order[-5:] if item in messages]))
        overlays: list[dict] = []
        wrap_width = 34 if scale <= 1 else 46
        char_w = max(10, chat_font * 0.62)
        inline_size = max(26, int(chat_font * 1.25))
        solo_size = max(44, int(48 * scale))
        origin_x = video_width + int(16 * scale)
        gap = max(4, int(6 * scale))
        for index, (start, visible) in enumerate(snapshots):
            end = min(duration, snapshots[index + 1][0] if index + 1 < len(snapshots) else duration)
            if end <= start:
                continue
            y = int(65 * scale)
            for message in visible:
                name = self._ass_escape(message.get("display_name") or "Discord member")
                color = self._ass_color(message.get("role_color"), "&H00D8B4FE")
                raw, slots = self._message_body(message)
                emoji_only = bool(slots) and not raw.replace(_EMOJI_PLACEHOLDER, "").strip()
                if emoji_only:
                    block = f"{{\\pos({origin_x},{y})\\c{color}\\b1}}{name}"
                    events.append(f"Dialogue: 2,{self._ass_time(start)},{self._ass_time(end)},Chat,,0,0,0,,{block}")
                    row_y = y + int(chat_font * 1.35)
                    for slot_index, (_slot_name, emoji_id) in enumerate(slots[:6]):
                        overlays.append({
                            "id": emoji_id,
                            "x": origin_x + slot_index * (solo_size + gap),
                            "y": row_y,
                            "start": start,
                            "end": end,
                            "size": solo_size,
                        })
                    y += int(chat_font * 1.35) + solo_size + int(14 * scale)
                else:
                    lines = textwrap.wrap(raw, width=wrap_width, break_long_words=True, break_on_hyphens=False)[:3] or [""]
                    slot_index = 0
                    body_y = y + int(chat_font * 1.28)
                    for line_index, line in enumerate(lines):
                        col = 0
                        for char in line:
                            if char == _EMOJI_PLACEHOLDER and slot_index < len(slots):
                                _slot_name, emoji_id = slots[slot_index]
                                overlays.append({
                                    "id": emoji_id,
                                    "x": origin_x + int(col * char_w),
                                    "y": body_y + int(line_index * chat_font * 1.22) - int(inline_size * 0.18),
                                    "start": start,
                                    "end": end,
                                    "size": inline_size,
                                })
                                slot_index += 1
                            col += 1
                    text = self._ass_escape("\n".join(lines))
                    block = f"{{\\pos({origin_x},{y})\\c{color}\\b1}}{name}{{\\c&H00E8E2EF&\\b0}}\\N{text}"
                    events.append(f"Dialogue: 2,{self._ass_time(start)},{self._ass_time(end)},Chat,,0,0,0,,{block}")
                    y += int((40 + 20 * len(lines)) * scale)
                if y > top_height - int(55 * scale):
                    break
        return overlays

    def file(self, vod_id: str, kind: str) -> str | None:
        filename = {"master": "master.mp4", "rendered": "rendered.mp4", "events": "events.jsonl"}.get(kind)
        if not filename:
            return None
        path = os.path.join(self._directory(vod_id), filename)
        return path if os.path.isfile(path) else None

    async def prune_rendered(self, protect_id: str | None = None) -> int:
        keep = rendered_keep_limit(
            await self.db.get_setting("trya_dcs_vod_keep_rendered")
        )
        doomed = select_oldest_rendered_to_delete(self.list(), keep, protect_id)
        removed = 0
        for vod_id in doomed:
            if await self.delete(vod_id):
                removed += 1
                self.log(f"VOD retention removed oldest rendered copy: {vod_id}.")
        return removed

    async def delete(self, vod_id: str) -> bool:
        if self.active and self.active.get("id") == vod_id:
            return False
        task = self.render_tasks.get(vod_id)
        if task and not task.done():
            return False
        directory = self._directory(vod_id)
        if not os.path.isdir(directory):
            return False
        import shutil
        shutil.rmtree(directory)
        return True
