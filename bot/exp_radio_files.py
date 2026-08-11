"""File lifecycle helpers for Experimental Radio songs."""

from __future__ import annotations

import glob
import os


def exp_radio_hook_cache_pattern(exp_radio_dir: str, song_id: int | str) -> str:
    return os.path.join(exp_radio_dir, "cover_cache", f"hook_{song_id}_*.mp4")


def exp_radio_hook_cache_path(
    exp_radio_dir: str, song_id: int | str, hook_id: str
) -> str:
    return os.path.join(
        exp_radio_dir, "cover_cache", f"hook_{song_id}_{hook_id}.mp4"
    )


def cleanup_exp_radio_hook_files(exp_radio_dir: str, song: dict) -> int:
    """Remove every cached Hook video belonging to one database row."""
    removed = 0
    song_id = song.get("id")
    if not song_id:
        return removed
    for path in glob.glob(exp_radio_hook_cache_pattern(exp_radio_dir, song_id)):
        try:
            os.remove(path)
            removed += 1
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"[exp-radio] Could not remove {path}: {exc}", flush=True)
    return removed


def _shared_song_paths(exp_radio_dir: str, song: dict) -> set[str]:
    """Return paths that can be shared by rows referring to the same song."""
    paths: set[str] = set()
    mp3_filename = song.get("mp3_filename")
    if mp3_filename:
        paths.add(os.path.abspath(os.path.join(exp_radio_dir, "mp3", mp3_filename)))
    ass_filename = song.get("ass_filename")
    if ass_filename:
        paths.add(os.path.abspath(os.path.join(exp_radio_dir, "ass", ass_filename)))
    suno_uuid = song.get("suno_uuid")
    if suno_uuid:
        for ext in (".jpg", ".mp4"):
            paths.add(os.path.abspath(
                os.path.join(exp_radio_dir, "cover_cache", f"{suno_uuid}{ext}")
            ))
    return paths


def cleanup_exp_radio_song_files(
    exp_radio_dir: str,
    song: dict,
    protected_songs: list[dict] | None = None,
) -> int:
    """Remove one row's media unless another active row still references it."""
    targets: list[str] = []
    mp3_filename = song.get("mp3_filename")
    if mp3_filename:
        targets.append(os.path.join(exp_radio_dir, "mp3", mp3_filename))
    ass_filename = song.get("ass_filename")
    if ass_filename:
        targets.append(os.path.join(exp_radio_dir, "ass", ass_filename))
    suno_uuid = song.get("suno_uuid")
    if suno_uuid:
        for ext in (".jpg", ".mp4"):
            targets.append(
                os.path.join(exp_radio_dir, "cover_cache", f"{suno_uuid}{ext}")
            )

    protected_paths: set[str] = set()
    for protected_song in protected_songs or []:
        protected_paths.update(_shared_song_paths(exp_radio_dir, protected_song))

    removed = cleanup_exp_radio_hook_files(exp_radio_dir, song)
    for path in targets:
        if os.path.abspath(path) in protected_paths:
            continue
        try:
            os.remove(path)
            removed += 1
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"[exp-radio] Could not remove {path}: {exc}", flush=True)
    return removed


def cleanup_orphan_exp_radio_hook_files(
    exp_radio_dir: str, active_songs: list[dict]
) -> int:
    """Remove Hook cache files that no active database row references."""
    expected = {
        os.path.abspath(
            exp_radio_hook_cache_path(
                exp_radio_dir, song["id"], str(song["hook_id"])
            )
        )
        for song in active_songs
        if song.get("id") and song.get("hook_id") and song.get("hook_video_url")
    }
    cache_dir = os.path.join(exp_radio_dir, "cover_cache")
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
            print(f"[exp-radio] Could not remove orphan Hook {path}: {exc}", flush=True)
    return removed
