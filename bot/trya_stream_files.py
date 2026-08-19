"""File lifecycle helpers for TrYa Stream songs."""

from __future__ import annotations

import glob
import os


def trya_stream_hook_cache_pattern(trya_stream_dir: str, song_id: int | str) -> str:
    return os.path.join(trya_stream_dir, "cover_cache", f"hook_{song_id}_*.mp4")


def trya_stream_hook_cache_path(
    trya_stream_dir: str, song_id: int | str, hook_id: str
) -> str:
    return os.path.join(
        trya_stream_dir, "cover_cache", f"hook_{song_id}_{hook_id}.mp4"
    )


def cleanup_trya_stream_hook_files(trya_stream_dir: str, song: dict) -> int:
    """Remove every cached Hook video belonging to one database row."""
    removed = 0
    song_id = song.get("id")
    if not song_id:
        return removed
    for path in glob.glob(trya_stream_hook_cache_pattern(trya_stream_dir, song_id)):
        try:
            os.remove(path)
            removed += 1
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"[trya-stream] Could not remove {path}: {exc}", flush=True)
    return removed


def _shared_song_paths(trya_stream_dir: str, song: dict) -> set[str]:
    """Return paths that can be shared by rows referring to the same song."""
    paths: set[str] = set()
    mp3_filename = song.get("mp3_filename")
    if mp3_filename:
        paths.add(os.path.abspath(os.path.join(trya_stream_dir, "mp3", mp3_filename)))
    ass_filename = song.get("ass_filename")
    if ass_filename:
        paths.add(os.path.abspath(os.path.join(trya_stream_dir, "ass", ass_filename)))
    suno_uuid = song.get("suno_uuid")
    if suno_uuid:
        for ext in (".jpg", ".mp4"):
            paths.add(os.path.abspath(
                os.path.join(trya_stream_dir, "cover_cache", f"{suno_uuid}{ext}")
            ))
    return paths


def cleanup_trya_stream_song_files(
    trya_stream_dir: str,
    song: dict,
    protected_songs: list[dict] | None = None,
) -> int:
    """Remove only reproducible CDN caches; retain original/work evidence.

    In particular this function never targets ``originals/``, normalized MP3s,
    or ASS analysis output. Playlist removal and expiry are evidence-preserving.
    """
    targets: list[str] = []
    suno_uuid = song.get("suno_uuid")
    if suno_uuid:
        for ext in (".jpg", ".mp4"):
            targets.append(
                os.path.join(trya_stream_dir, "cover_cache", f"{suno_uuid}{ext}")
            )

    protected_paths: set[str] = set()
    for protected_song in protected_songs or []:
        protected_paths.update(_shared_song_paths(trya_stream_dir, protected_song))

    removed = cleanup_trya_stream_hook_files(trya_stream_dir, song)
    originals_root = os.path.abspath(os.path.join(trya_stream_dir, "originals"))
    cache_root = os.path.abspath(os.path.join(trya_stream_dir, "cover_cache"))
    for path in targets:
        absolute_path = os.path.abspath(path)
        # Defense in depth: even malformed legacy identifiers cannot escape
        # cover_cache or point cleanup at the immutable originals directory.
        if os.path.commonpath((absolute_path, originals_root)) == originals_root:
            continue
        if os.path.commonpath((absolute_path, cache_root)) != cache_root:
            continue
        if absolute_path in protected_paths:
            continue
        try:
            os.remove(path)
            removed += 1
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"[trya-stream] Could not remove {path}: {exc}", flush=True)
    return removed


def cleanup_orphan_trya_stream_hook_files(
    trya_stream_dir: str, active_songs: list[dict]
) -> int:
    """Remove Hook cache files that no active database row references."""
    expected = {
        os.path.abspath(
            trya_stream_hook_cache_path(
                trya_stream_dir, song["id"], str(song["hook_id"])
            )
        )
        for song in active_songs
        if song.get("id") and song.get("hook_id") and song.get("hook_video_url")
    }
    cache_dir = os.path.join(trya_stream_dir, "cover_cache")
    removed = 0
    for path in glob.glob(os.path.join(cache_dir, "hook_*.mp4")):
        if os.path.abspath(path) in expected:
            continue
        try:
            os.remove(path)
            removed += 1
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"[trya-stream] Could not remove orphan Hook {path}: {exc}", flush=True)
    return removed
