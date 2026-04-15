#!/usr/bin/env python3
"""Strip cover art from all existing radio MP3 files.

Run inside the Docker container:
    docker exec slowmode-bot python /app/scripts/strip_cover_art.py
"""

import glob
import os
import subprocess
import sys

RADIO_DIR = "/app/data/radio"


def main():
    pattern = os.path.join(RADIO_DIR, "radio_*.mp3")
    files = sorted(glob.glob(pattern))
    if not files:
        print("No radio MP3 files found.")
        return

    print(f"Found {len(files)} MP3 files in {RADIO_DIR}")
    stripped = 0
    skipped = 0
    errors = 0

    for filepath in files:
        name = os.path.basename(filepath)
        tmp = filepath + ".stripped.mp3"

        # Check if file has a video stream (cover art)
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_streams", "-select_streams", "v", filepath],
            capture_output=True, text=True,
        )
        if not probe.stdout.strip():
            print(f"  SKIP {name} (no cover art)")
            skipped += 1
            continue

        # Strip video streams, copy audio
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", filepath, "-vn", "-acodec", "copy", tmp],
            capture_output=True, text=True,
        )
        if result.returncode == 0 and os.path.exists(tmp):
            old_size = os.path.getsize(filepath)
            new_size = os.path.getsize(tmp)
            os.replace(tmp, filepath)
            saved = old_size - new_size
            print(f"  OK   {name} ({old_size // 1024}KB -> {new_size // 1024}KB, saved {saved // 1024}KB)")
            stripped += 1
        else:
            print(f"  ERR  {name}: ffmpeg failed")
            if os.path.exists(tmp):
                os.remove(tmp)
            errors += 1

    print(f"\nDone: {stripped} stripped, {skipped} skipped, {errors} errors")


if __name__ == "__main__":
    main()
