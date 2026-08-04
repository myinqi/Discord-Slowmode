import calendar
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


BERLIN_TZ = ZoneInfo("Europe/Berlin")
RECURRENCE_LABELS = {
    "once": "Once",
    "daily": "Every day",
    "weekly": "Every week",
    "monthly": "Every month",
}


def parse_reminder_datetime(date_text: str, time_text: str) -> datetime:
    raw_date = date_text.strip()
    parsed_date = None
    for date_format in ("%Y-%m-%d", "%d.%m.%Y"):
        try:
            parsed_date = datetime.strptime(raw_date, date_format).date()
            break
        except ValueError:
            continue
    if parsed_date is None:
        raise ValueError("Use DD.MM.YYYY or YYYY-MM-DD for the date.")

    try:
        parsed_time = datetime.strptime(time_text.strip(), "%H:%M").time()
    except ValueError as exc:
        raise ValueError("Use HH:MM in 24-hour format for the time.") from exc
    return datetime.combine(parsed_date, parsed_time, tzinfo=BERLIN_TZ)


def reminder_datetime_from_timestamp(timestamp: float) -> datetime:
    return datetime.fromtimestamp(float(timestamp), tz=BERLIN_TZ)


def next_recurrence_datetime(
    scheduled: datetime,
    recurrence: str,
    *,
    anchor_day: int,
    after: datetime,
) -> datetime | None:
    if recurrence == "once":
        return None

    candidate = scheduled.astimezone(BERLIN_TZ)
    after = after.astimezone(BERLIN_TZ)
    if recurrence == "daily":
        elapsed_days = max(1, (after.date() - candidate.date()).days)
        candidate = datetime.combine(
            candidate.date() + timedelta(days=elapsed_days),
            candidate.timetz().replace(tzinfo=None),
            tzinfo=BERLIN_TZ,
        )
        if candidate <= after:
            candidate = datetime.combine(
                candidate.date() + timedelta(days=1),
                candidate.timetz().replace(tzinfo=None),
                tzinfo=BERLIN_TZ,
            )
        return candidate
    if recurrence == "weekly":
        elapsed_days = max(7, (after.date() - candidate.date()).days)
        elapsed_weeks = max(1, elapsed_days // 7)
        candidate = datetime.combine(
            candidate.date() + timedelta(days=elapsed_weeks * 7),
            candidate.timetz().replace(tzinfo=None),
            tzinfo=BERLIN_TZ,
        )
        if candidate <= after:
            candidate = datetime.combine(
                candidate.date() + timedelta(days=7),
                candidate.timetz().replace(tzinfo=None),
                tzinfo=BERLIN_TZ,
            )
        return candidate
    if recurrence == "monthly":
        elapsed_months = max(
            1,
            (after.year - candidate.year) * 12 + after.month - candidate.month,
        )
        month_index = candidate.year * 12 + candidate.month - 1 + elapsed_months
        year, zero_based_month = divmod(month_index, 12)
        month = zero_based_month + 1
        day = min(anchor_day, calendar.monthrange(year, month)[1])
        candidate = datetime(
            year, month, day, candidate.hour, candidate.minute, tzinfo=BERLIN_TZ
        )
        if candidate <= after:
            month_index += 1
            year, zero_based_month = divmod(month_index, 12)
            month = zero_based_month + 1
            day = min(anchor_day, calendar.monthrange(year, month)[1])
            candidate = datetime(
                year, month, day, candidate.hour, candidate.minute, tzinfo=BERLIN_TZ
            )
        return candidate
    raise ValueError("Unsupported recurrence.")


def ensure_future_recurrence(
    scheduled: datetime,
    recurrence: str,
    *,
    now: datetime,
) -> datetime:
    if scheduled > now:
        return scheduled
    if recurrence == "once":
        raise ValueError("A one-time reminder must be in the future.")
    next_run = next_recurrence_datetime(
        scheduled,
        recurrence,
        anchor_day=scheduled.day,
        after=now,
    )
    if next_run is None:
        raise ValueError("Could not calculate the next reminder.")
    return next_run
