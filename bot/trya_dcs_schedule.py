"""TrYa DCS auto-start schedule helpers and submission lockout."""

from datetime import datetime
from zoneinfo import ZoneInfo

# True while the DCS publisher is running, including FFmpeg startup.
dcs_stream_is_live: bool = False
PRE_START_LOCK_MINUTES = 60


async def is_submissions_locked(db, now=None) -> tuple[bool, str]:
    """Return (locked, reason_str).

    Locked when:
      • the DCS publisher is currently running  →  reason "stream_live"
      • OR the scheduler is enabled, today is a scheduled day, and the
        configured start time is within the next 60 minutes
        →  reason "pre_start_Nmin"

    The hour before a scheduled start is reserved so last-minute uploads can
    finish Whisper and optional LLM review without competing with FFmpeg.
    """
    if dcs_stream_is_live:
        return True, "stream_live"

    try:
        enabled = await db.get_setting("trya_dcs_schedule_enabled") or "off"
        if enabled != "on":
            return False, ""
        days_csv = await db.get_setting("trya_dcs_schedule_days") or ""
        days = {int(d) for d in days_csv.split(",") if d.strip().isdigit()}
        hhmm = (await db.get_setting("trya_dcs_schedule_time") or "").strip()
        if not days or not hhmm or ":" not in hhmm:
            return False, ""
        h_str, m_str = hhmm.split(":", 1)
        target_h, target_m = int(h_str), int(m_str)
        if now is None:
            now = datetime.now(ZoneInfo("Europe/Berlin"))
        if now.weekday() not in days:
            return False, ""
        diff = (target_h * 60 + target_m) - (now.hour * 60 + now.minute)
        if 0 < diff <= PRE_START_LOCK_MINUTES:
            return True, f"pre_start_{diff}min"
        return False, ""
    except Exception:
        return False, ""
