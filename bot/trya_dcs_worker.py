"""Validated upload and analysis pipeline for the private TrYa DCS stream."""

import hashlib
import json
import math
import os
import time

from bot.trya_dcs_manager import log_dcs_event
from bot.trya_stream_worker import (
    _align_to_lyrics,
    _normalize_original_to_mp3,
    _probe_uploaded_audio,
    build_ass,
    clean_lyrics,
    detect_lyrics_language,
    get_duration,
    run_whisper,
    scrape_suno,
)


DCS_RIGHTS_VERSION = "trya-dcs-private-community-v1-2026-08-20"
DCS_RIGHTS_DECLARATION = (
    "I confirm that I may share this song inside the private, non-commercial "
    "TrYa DCS community; that this exact file came from an official Suno download "
    "channel; that I hold every required right and permission for my lyrics, "
    "samples, voices and all other supplied material; and that I permit the "
    "technical copies, transcoding and playback required for the closed TrYa DCS "
    "service. The documented Suno plan and original/cover/remix status does not "
    "itself decide eligibility."
)


def ensure_dcs_dirs(base_dir: str) -> None:
    for name in ("incoming", "originals", "mp3", "ass", "assets", "hooks"):
        os.makedirs(os.path.join(base_dir, name), exist_ok=True)


async def ingest_dcs_audio(
    db,
    song_id: int,
    uploaded_path: str,
    base_dir: str,
    *,
    original_filename: str,
    max_upload_bytes: int,
    max_duration_seconds: int,
    content_kind: str,
    suno_plan_status: str,
    rights_hash: str,
    accepted_at: float,
) -> dict:
    """Archive original bytes and create a normalized, decoded work MP3."""
    song = await db.get_trya_dcs_song(song_id)
    if not song:
        raise ValueError("DCS upload slot not found")
    if song.get("uploaded_at") or song.get("original_archive_filename"):
        raise ValueError("this upload has already been completed")
    if content_kind not in {"original", "cover", "remix"}:
        raise ValueError("invalid song content status")
    if suno_plan_status not in {"free", "paid", "unknown"}:
        raise ValueError("invalid Suno plan status")

    ensure_dcs_dirs(base_dir)
    log_dcs_event(f"Upload #{song_id} validation started: {original_filename}")
    size = os.path.getsize(uploaded_path)
    if size <= 0:
        raise ValueError("audio upload is empty")
    if size > max_upload_bytes:
        raise ValueError(
            f"audio upload exceeds {max_upload_bytes // (1024 * 1024)} MiB"
        )

    digest = hashlib.sha256()
    with open(uploaded_path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    extension, mime = await _probe_uploaded_audio(uploaded_path)
    sha256 = digest.hexdigest()
    archive_filename = f"{song_id}_{sha256}{extension}"
    archive_path = os.path.join(base_dir, "originals", archive_filename)
    if os.path.exists(archive_path):
        with open(archive_path, "rb") as existing:
            existing_hash = hashlib.sha256(existing.read()).hexdigest()
        if existing_hash != sha256:
            raise ValueError("original archive collision")
    else:
        os.link(uploaded_path, archive_path)
        os.chmod(archive_path, 0o444)

    work_filename = f"{song_id}_{sha256[:16]}.mp3"
    work_path = os.path.join(base_dir, "mp3", work_filename)
    try:
        await _normalize_original_to_mp3(archive_path, work_path)
        duration = await get_duration(work_path)
        if not duration or duration <= 0:
            raise ValueError("normalized audio has no decodable duration")
        if max_duration_seconds > 0 and duration > max_duration_seconds:
            raise ValueError(
                f"decoded song duration is {duration / 60:.1f} minutes; "
                f"the configured maximum is {max_duration_seconds / 60:.1f} minutes"
            )
        evidence = {
            "original_sha256": sha256,
            "original_filename": os.path.basename(original_filename),
            "original_mime": mime,
            "original_size": size,
            "original_archive_filename": archive_filename,
            "mp3_filename": work_filename,
            "duration": duration,
            "uploaded_at": time.time(),
            "content_kind": content_kind,
            "suno_plan_status": suno_plan_status,
            "rights_version": DCS_RIGHTS_VERSION,
            "rights_declaration": DCS_RIGHTS_DECLARATION,
            "rights_hash": rights_hash,
            "rights_accepted_at": accepted_at,
            "sharing_attested": 1,
            "official_download_attested": 1,
            "material_rights_attested": 1,
            "technical_processing_attested": 1,
            "private_playback_attested": 1,
        }
        finalized = await db.finalize_trya_dcs_upload(
            song_id,
            evidence_json=json.dumps(
                {
                    "original_filename": os.path.basename(original_filename),
                    "original_mime": mime,
                    "original_size": size,
                    "original_sha256": sha256,
                    "wlm_url": song.get("wlm_url") or "",
                },
                separators=(",", ":"),
            ),
            **evidence,
        )
        if not finalized:
            raise ValueError("DCS upload could not be finalized")
        log_dcs_event(f"Upload #{song_id} archived and normalized ({duration:.1f}s).")
        return finalized
    except Exception as exc:
        log_dcs_event(f"Upload #{song_id} failed: {exc}", "error")
        try:
            os.remove(work_path)
        except OSError:
            pass
        raise


async def moderate_dcs_song(db, song_id: int) -> None:
    song = await db.get_trya_dcs_song(song_id)
    if not song or not song.get("active"):
        raise ValueError("active DCS song not found")
    lyrics = clean_lyrics(song.get("lyrics") or "")
    if not lyrics:
        raise ValueError("the song has no lyrics to moderate")
    title = song.get("title") or "Unknown"
    artist = song.get("artist") or song.get("user_name") or "Unknown"
    await db.update_trya_dcs_song(
        song_id,
        moderation_status="processing",
        moderation_reason="Automated lyric review is running.",
        approval_status="pending",
        approved_at=None,
        approved_by=None,
    )
    log_dcs_event(f"Moderation #{song_id} started: {title} — {artist}")
    try:
        from bot.exp_moderation import moderate_lyrics
        from bot.llm import OllamaClient
        from bot.llm_mod_queue import PRIO_RADIO, enqueue_moderation
        from config import Config

        timeout = 600
        client = OllamaClient(
            base_url=Config.OLLAMA_URL,
            model=Config.LLM_MODEL,
            timeout=timeout,
        )
        verdict = await enqueue_moderation(
            PRIO_RADIO,
            lambda: moderate_lyrics(
                client,
                lyrics=lyrics,
                title=title,
                artist=artist,
                timeout=timeout,
            ),
        )
        status = verdict.get("status") or "pending"
        update = {
            "moderation_status": status,
            "moderation_reason": verdict.get("reason") or "",
        }
        if status == "passed":
            update.update(
                approval_status="approved",
                approved_at=time.time(),
                approved_by="automated-llm",
            )
        else:
            update.update(
                approval_status="pending",
                approved_at=None,
                approved_by=None,
            )
        await db.update_trya_dcs_song(song_id, **update)
        log_dcs_event(f"Moderation #{song_id} finished: {status}")
    except Exception as exc:
        log_dcs_event(f"Moderation #{song_id} failed: {exc}", "error")
        await db.update_trya_dcs_song(
            song_id,
            moderation_status="pending",
            moderation_reason=f"Automated lyric review failed: {exc}",
            approval_status="pending",
            approved_at=None,
            approved_by=None,
        )


async def import_dcs_transcript(
    db, song_id: int, base_dir: str, transcript_json: str
) -> int:
    song = await db.get_trya_dcs_song(song_id)
    if not song or not song.get("active") or not song.get("uploaded_at"):
        raise ValueError("active uploaded DCS song not found")
    try:
        raw_words = json.loads(transcript_json)
    except json.JSONDecodeError as exc:
        raise ValueError(f"transcript is not valid JSON: {exc.msg}") from exc
    if not isinstance(raw_words, list) or not raw_words or len(raw_words) > 20000:
        raise ValueError("transcript must be a non-empty array with at most 20,000 words")
    duration = float(song.get("duration") or 0)
    words = []
    previous_start = -1.0
    for index, item in enumerate(raw_words):
        if not isinstance(item, dict) or set(item) != {"word", "start", "end"}:
            raise ValueError(f"word #{index + 1} must contain exactly word, start and end")
        word = str(item.get("word") or "").strip()
        try:
            start = float(item["start"])
            end = float(item["end"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"word #{index + 1} has invalid timestamps") from exc
        if (
            not word or len(word) > 200
            or not math.isfinite(start) or not math.isfinite(end)
            or start < 0 or end <= start
        ):
            raise ValueError(f"word #{index + 1} is empty or has an invalid time range")
        if start < previous_start or (duration > 0 and end > duration + 5):
            raise ValueError(f"word #{index + 1} is out of chronological or song range")
        words.append({"word": word, "start": round(start, 3), "end": round(end, 3)})
        previous_start = start
    ensure_dcs_dirs(base_dir)
    ass_filename = os.path.basename(
        song.get("ass_filename") or f"{song.get('suno_uuid') or song_id}.ass"
    )
    with open(os.path.join(base_dir, "ass", ass_filename), "w", encoding="utf-8") as handle:
        handle.write(
            build_ass(
                words,
                title=song.get("title") or "Unknown",
                artist=song.get("artist") or song.get("user_name") or "Unknown",
            )
        )
    moderation_enabled = (
        await db.get_setting("trya_dcs_moderation_enabled") or "off"
    ) == "on"
    update = {
        "word_timestamps": json.dumps(words, ensure_ascii=False, separators=(",", ":")),
        "ass_filename": ass_filename,
        "analysis_status": "done",
        "moderation_status": "pending" if moderation_enabled else None,
        "moderation_reason": (
            "External transcript imported; run moderation again."
            if moderation_enabled else ""
        ),
        "approval_status": "pending" if moderation_enabled else "approved",
        "approved_at": None if moderation_enabled else time.time(),
        "approved_by": None if moderation_enabled else "moderation-disabled",
    }
    await db.update_trya_dcs_song(song_id, **update)
    log_dcs_event(f"External transcript imported for #{song_id}: {len(words)} words")
    return len(words)


async def process_dcs_song(
    db,
    song_id: int,
    base_dir: str,
    *,
    max_duration_seconds: int,
    run_moderation: bool | None = None,
) -> None:
    """Resolve metadata and create Whisper timestamps for one DCS song."""
    song = await db.get_trya_dcs_song(song_id)
    if not song or not song.get("active"):
        return
    await db.update_trya_dcs_song(song_id, analysis_status="processing")
    log_dcs_event(f"Analysis #{song_id} started: {song.get('title') or 'pending metadata'}")
    try:
        mp3_path = os.path.join(base_dir, "mp3", song["mp3_filename"])
        duration = await get_duration(mp3_path)
        if not duration or duration <= 0:
            raise ValueError("work MP3 is not decodable")
        if max_duration_seconds > 0 and duration > max_duration_seconds:
            await db.update_trya_dcs_song(
                song_id,
                duration=duration,
                analysis_status="failed",
                approval_status="rejected",
                active=0,
                removed_at=time.time(),
                remove_reason="duration_limit",
            )
            return

        metadata = await scrape_suno(song.get("suno_uuid") or "")
        title = metadata.get("title") or song.get("title") or "Unknown"
        artist = metadata.get("artist") or song.get("artist") or song["user_name"]
        lyrics = clean_lyrics(metadata.get("raw_lyrics") or song.get("lyrics") or "")
        real_uuid = metadata.get("real_uuid") or song.get("suno_uuid") or ""
        await db.update_trya_dcs_song(
            song_id,
            suno_uuid=real_uuid,
            title=title,
            artist=artist,
            cover_url=metadata.get("cover_url") or song.get("cover_url"),
            video_url=metadata.get("video_url") or song.get("video_url"),
            lyrics=lyrics,
            duration=duration,
        )

        language = detect_lyrics_language(lyrics)
        log_dcs_event(f"Whisper #{song_id} started (language={language or 'auto'}).")
        words = await run_whisper(mp3_path, language=language)
        log_dcs_event(f"Whisper #{song_id} finished: {len(words)} words.")
        if lyrics:
            words = _align_to_lyrics(words, lyrics)
        ass_filename = f"{real_uuid or song_id}.ass"
        with open(
            os.path.join(base_dir, "ass", ass_filename), "w", encoding="utf-8"
        ) as handle:
            handle.write(build_ass(words, title=title, artist=artist))
        moderation_enabled = (
            (await db.get_setting("trya_dcs_moderation_enabled") or "off") == "on"
            if run_moderation is None else bool(run_moderation)
        )
        analysis_update = {
            "word_timestamps": json.dumps(words),
            "ass_filename": ass_filename,
            "analysis_status": "done",
        }
        if moderation_enabled:
            analysis_update.update(
                moderation_status="processing",
                moderation_reason="Automated lyric review is running.",
                approval_status="pending",
                approved_at=None,
                approved_by=None,
            )
        else:
            analysis_update.update(
                moderation_status=None,
                moderation_reason="",
                approval_status="approved",
                approved_at=time.time(),
                approved_by="moderation-disabled",
            )
        await db.update_trya_dcs_song(song_id, **analysis_update)

        latest = await db.get_trya_dcs_song(song_id)
        if latest:
            from bot.trya_dcs_manager import TryaDcsManager
            await TryaDcsManager(db, base_dir).prefetch_and_normalize_visuals(latest)

        log_dcs_event(f"Analysis #{song_id} finished: ASS={ass_filename}")
        if moderation_enabled:
            await moderate_dcs_song(db, song_id)
    except Exception as exc:
        log_dcs_event(f"Song #{song_id} processing failed: {exc}", "error")
        await db.update_trya_dcs_song(song_id, analysis_status="failed")
