"""Shared audio inspection helpers."""

from __future__ import annotations

import asyncio


async def get_decoded_audio_duration(audio_path: str) -> float:
    """Return the duration of the decodable audio stream in seconds.

    Decoding to FFmpeg's null muxer avoids trusting container metadata, which
    can report incorrect durations for some Suno MP3 files.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-v", "error",
            "-nostats", "-progress", "pipe:1",
            "-i", audio_path,
            "-map", "0:a:0",
            "-f", "null", "-",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        progress: dict[str, str] = {}
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
    except Exception as exc:
        print(f"[audio] decoded duration probe failed for {audio_path}: {exc}", flush=True)

    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "quiet",
            "-select_streams", "a:0",
            "-show_entries", "stream=duration",
            "-of", "csv=p=0",
            audio_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await proc.communicate()
        value = float(out.decode().strip() or "0")
        return value if value > 0 else 0.0
    except Exception as exc:
        print(f"[audio] stream duration probe failed for {audio_path}: {exc}", flush=True)
        return 0.0
