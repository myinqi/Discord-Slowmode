import asyncio
import json
import os
import re
import functools
import hashlib
import hmac
import math
import secrets
import time
import threading
from datetime import datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo
import bcrypt
import aiohttp
from quart import (
    Quart, flash, make_response, redirect, render_template, request,
    session, url_for, websocket,
)
from bot.database import Database
from bot.backup import (
    create_full_data_archive,
    prune_restore_backups,
    validate_database_backup,
)
from bot.exp_radio_files import (
    cleanup_exp_radio_hook_files,
    cleanup_exp_radio_song_files,
    cleanup_orphan_exp_radio_hook_files,
    exp_radio_hook_cache_path,
)
from bot.trya_stream_files import (
    cleanup_trya_stream_hook_files,
    cleanup_trya_stream_song_files,
    cleanup_orphan_trya_stream_hook_files,
    trya_stream_hook_cache_path,
)
from bot.suno_urls import resolve_suno_uuid
from config import Config

SUNO_URL_PATTERN = re.compile(r'https://suno\.com/(?:s|song)/[\w-]+')
YOUTUBE_URL_RE   = re.compile(
    r'(?:https?://)?(?:www\.)?(?:youtu\.be/|youtube\.com/(?:watch\?v=|embed/|v/|shorts/))'
    r'([A-Za-z0-9_-]{11})'
)
ELEVENMUSIC_TRACK_RE = re.compile(
    r'(?:https?://)?(?:www\.)?elevenmusic\.io/tracks/([A-Fa-f0-9]{24})'
)

_SYSTEM_CPU_SAMPLE = {"timestamp": None, "usage_seconds": None}
_SYSTEM_CPU_LOCK = threading.Lock()


def _format_system_bytes(value: int | float | None) -> str:
    if value is None:
        return "Unknown"
    size = float(max(0, value))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            decimals = 0 if unit == "B" else 1
            return f"{size:.{decimals}f} {unit}"
        size /= 1024
    return "Unknown"


def _format_system_uptime(seconds: float) -> str:
    total = max(0, int(seconds))
    days, remainder = divmod(total, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _read_system_text(path: str) -> str | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return None


def _container_process_uptime() -> float:
    proc_path = "/proc/1/stat" if os.path.exists("/.dockerenv") else "/proc/self/stat"
    try:
        stat = _read_system_text(proc_path) or ""
        fields = stat.rsplit(")", 1)[1].split()
        start_ticks = int(fields[19])
        clock_ticks = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
        host_uptime = float((_read_system_text("/proc/uptime") or "0").split()[0])
        return max(0.0, host_uptime - (start_ticks / clock_ticks))
    except (IndexError, KeyError, TypeError, ValueError):
        return 0.0


def _container_memory_values() -> tuple[int | None, int | None]:
    current_raw = _read_system_text("/sys/fs/cgroup/memory.current")
    limit_raw = _read_system_text("/sys/fs/cgroup/memory.max")
    if current_raw is None:
        current_raw = _read_system_text("/sys/fs/cgroup/memory/memory.usage_in_bytes")
        limit_raw = _read_system_text("/sys/fs/cgroup/memory/memory.limit_in_bytes")

    try:
        current = int(current_raw) if current_raw is not None else None
    except ValueError:
        current = None
    try:
        limit = int(limit_raw) if limit_raw and limit_raw != "max" else None
    except ValueError:
        limit = None

    # cgroup v1 represents an unlimited value with a very large integer.
    if limit is not None and limit >= (1 << 60):
        limit = None
    if current is None:
        status = _read_system_text("/proc/self/status") or ""
        match = re.search(r"^VmRSS:\s+(\d+)\s+kB", status, re.M)
        current = int(match.group(1)) * 1024 if match else None
    return current, limit


def _container_cpu_values() -> tuple[float | None, float]:
    usage_seconds = None
    cpu_stat = _read_system_text("/sys/fs/cgroup/cpu.stat") or ""
    match = re.search(r"^usage_usec\s+(\d+)", cpu_stat, re.M)
    if match:
        usage_seconds = int(match.group(1)) / 1_000_000
    else:
        usage_raw = _read_system_text("/sys/fs/cgroup/cpuacct/cpuacct.usage")
        if usage_raw:
            try:
                usage_seconds = int(usage_raw) / 1_000_000_000
            except ValueError:
                pass
    if usage_seconds is None:
        usage_seconds = time.process_time()

    available_cores = float(os.cpu_count() or 1)
    cpu_max = (_read_system_text("/sys/fs/cgroup/cpu.max") or "").split()
    if len(cpu_max) == 2 and cpu_max[0] != "max":
        try:
            available_cores = max(0.01, int(cpu_max[0]) / int(cpu_max[1]))
        except (ValueError, ZeroDivisionError):
            pass
    else:
        quota_raw = _read_system_text("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
        period_raw = _read_system_text("/sys/fs/cgroup/cpu/cpu.cfs_period_us")
        try:
            quota = int(quota_raw or -1)
            period = int(period_raw or 0)
            if quota > 0 and period > 0:
                available_cores = max(0.01, quota / period)
        except ValueError:
            pass

    now = time.monotonic()
    cpu_percent = None
    with _SYSTEM_CPU_LOCK:
        previous_time = _SYSTEM_CPU_SAMPLE["timestamp"]
        previous_usage = _SYSTEM_CPU_SAMPLE["usage_seconds"]
        if previous_time is not None and previous_usage is not None and now > previous_time:
            cpu_percent = max(0.0, (usage_seconds - previous_usage) / (now - previous_time) * 100)
        _SYSTEM_CPU_SAMPLE.update(timestamp=now, usage_seconds=usage_seconds)
    return cpu_percent, available_cores


def _collect_container_stats(database_path: str) -> dict:
    import platform
    import shutil
    import socket

    data_dir = os.path.dirname(os.path.abspath(database_path)) or os.getcwd()
    disk_probe = data_dir
    while not os.path.exists(disk_probe):
        parent = os.path.dirname(disk_probe)
        if parent == disk_probe:
            disk_probe = os.getcwd()
            break
        disk_probe = parent
    try:
        disk = shutil.disk_usage(disk_probe)
        disk_percent = round((disk.used / disk.total) * 100, 1) if disk.total else 0.0
    except OSError:
        disk = None
        disk_percent = None

    memory_current, memory_limit = _container_memory_values()
    memory_percent = (
        round((memory_current / memory_limit) * 100, 1)
        if memory_current is not None and memory_limit
        else None
    )
    cpu_percent, cpu_cores = _container_cpu_values()
    cpu_capacity_percent = (
        round(min(100.0, cpu_percent / cpu_cores), 1)
        if cpu_percent is not None and cpu_cores > 0
        else None
    )

    database_size = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            database_size += os.path.getsize(database_path + suffix)
        except OSError:
            pass

    os_name = platform.system()
    os_release = platform.release()
    os_info = _read_system_text("/etc/os-release") or ""
    pretty_match = re.search(r'^PRETTY_NAME="?([^"\n]+)"?$', os_info, re.M)
    if pretty_match:
        os_name = pretty_match.group(1)

    return {
        "storage": {
            "used": _format_system_bytes(disk.used) if disk else "Unknown",
            "total": _format_system_bytes(disk.total) if disk else "Unknown",
            "free": _format_system_bytes(disk.free) if disk else "Unknown",
            "percent": disk_percent,
        },
        "memory": {
            "used": _format_system_bytes(memory_current),
            "limit": _format_system_bytes(memory_limit) if memory_limit else "No Docker limit",
            "percent": memory_percent,
        },
        "cpu": {
            "percent": round(cpu_percent, 1) if cpu_percent is not None else None,
            "capacity_percent": cpu_capacity_percent,
            "cores": round(cpu_cores, 2),
        },
        "uptime": _format_system_uptime(_container_process_uptime()),
        "database_size": _format_system_bytes(database_size),
        "hostname": socket.gethostname(),
        "python_version": platform.python_version(),
        "os_name": os_name,
        "kernel": os_release,
    }


def _decode_suno_json_string(value: str) -> str:
    import html as _html
    value = re.sub(r'\\\\u([0-9a-fA-F]{4})',
                   lambda m: chr(int(m.group(1), 16)), value)
    value = re.sub(r'\\u([0-9a-fA-F]{4})',
                   lambda m: chr(int(m.group(1), 16)), value)
    return _html.unescape(
        value.replace(r'\"', '"').replace(r"\/", "/").strip()
    )


def _valid_suno_display_name(name: str | None) -> bool:
    return bool(
        name
        and len(name) > 1
        and not re.match(r"^v\d", name)
        and name not in ("Cover", "Remix")
    )


def _extract_suno_clip_owner_display_name(page: str, song_id: str | None = None) -> str | None:
    id_part = re.escape(song_id) if song_id else r"[a-f0-9-]{8,36}"
    patterns = [
        rf'\\"id\\":\\"{id_part}\\".*?\\"user_id\\":\\"[^"\\]+\\".*?\\"display_name\\":\\"((?:(?!\\").)*)\\"',
        rf'"id"\s*:\s*"{id_part}".*?"user_id"\s*:\s*"[^"]+".*?"display_name"\s*:\s*"((?:[^"\\]|\\.)*)"',
    ]
    for pat in patterns:
        m = re.search(pat, page, re.S)
        if not m:
            continue
        name = _decode_suno_json_string(m.group(1))
        if _valid_suno_display_name(name):
            return name
    return None


async def _rpg_import_enemies(db, raw_json: str, _json) -> tuple[int, list[str]]:
    """Validate + bulk-upsert enemies from a JSON payload.

    Accepts either a JSON list or an object with `{"enemies":[...]}`.
    Returns (count_imported, errors).
    """
    errors: list[str] = []
    if not raw_json:
        return 0, ["JSON input is required."]
    try:
        payload = _json.loads(raw_json)
    except _json.JSONDecodeError as exc:
        return 0, [f"Invalid JSON: {exc.msg} at line {exc.lineno}."]
    if isinstance(payload, dict) and "enemies" in payload:
        items = payload["enemies"]
    elif isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict) and "enemy_key" in payload:
        items = [payload]
    else:
        return 0, ["JSON must be a list, an enemy object, or {\"enemies\":[...]}."]

    count = 0
    for idx, raw in enumerate(items, start=1):
        if not isinstance(raw, dict):
            errors.append(f"Enemy {idx}: must be an object.")
            continue
        key = str(raw.get("enemy_key") or "").strip()
        name = str(raw.get("name") or "").strip()
        if not key or not name:
            errors.append(f"Enemy {idx}: 'enemy_key' and 'name' are required.")
            continue
        ab_raw = raw.get("abilities", raw.get("abilities_json", []))
        loot_raw = raw.get("loot", raw.get("loot_json", []))
        try:
            abilities_json = (ab_raw if isinstance(ab_raw, str)
                              else _json.dumps(ab_raw))
            loot_json = (loot_raw if isinstance(loot_raw, str)
                         else _json.dumps(loot_raw))
            _json.loads(abilities_json)
            _json.loads(loot_json)
        except (TypeError, ValueError, _json.JSONDecodeError) as exc:
            errors.append(f"Enemy {idx} ({key}): invalid abilities/loot JSON ({exc}).")
            continue
        try:
            await db.rpg_upsert_enemy(
                enemy_key=key,
                name=name,
                description=str(raw.get("description") or ""),
                hp=int(raw.get("hp", 15)),
                attack=int(raw.get("attack", 4)),
                defense=int(raw.get("defense", 3)),
                agility=int(raw.get("agility", 4)),
                abilities_json=abilities_json,
                loot_json=loot_json,
                xp_reward=int(raw.get("xp_reward", 10)),
            )
            count += 1
        except Exception as exc:
            errors.append(f"Enemy {idx} ({key}): DB error — {exc}")
    return count, errors


async def _rpg_import_items(db, raw_json: str, _json) -> tuple[int, list[str]]:
    """Validate + bulk-upsert items from a JSON payload.

    Accepts either a JSON list or an object with `{"items":[...]}`.
    """
    errors: list[str] = []
    if not raw_json:
        return 0, ["JSON input is required."]
    try:
        payload = _json.loads(raw_json)
    except _json.JSONDecodeError as exc:
        return 0, [f"Invalid JSON: {exc.msg} at line {exc.lineno}."]
    if isinstance(payload, dict) and "items" in payload:
        items = payload["items"]
    elif isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict) and "item_key" in payload:
        items = [payload]
    else:
        return 0, ["JSON must be a list, an item object, or {\"items\":[...]}."]

    count = 0
    for idx, raw in enumerate(items, start=1):
        if not isinstance(raw, dict):
            errors.append(f"Item {idx}: must be an object.")
            continue
        key = str(raw.get("item_key") or "").strip()
        name = str(raw.get("name") or "").strip()
        if not key or not name:
            errors.append(f"Item {idx}: 'item_key' and 'name' are required.")
            continue
        eff_raw = raw.get("effect", raw.get("effect_json", {}))
        try:
            effect_json = (eff_raw if isinstance(eff_raw, str)
                           else _json.dumps(eff_raw))
            _json.loads(effect_json)
        except (TypeError, ValueError, _json.JSONDecodeError) as exc:
            errors.append(f"Item {idx} ({key}): invalid effect JSON ({exc}).")
            continue
        try:
            await db.rpg_upsert_item(
                item_key=key,
                name=name,
                description=str(raw.get("description") or ""),
                item_type=str(raw.get("item_type") or "misc"),
                effect_json=effect_json,
            )
            count += 1
        except Exception as exc:
            errors.append(f"Item {idx} ({key}): DB error — {exc}")
    return count, errors


def create_app(db: Database, bot=None) -> Quart:
    app = Quart(
        __name__,
        template_folder="templates",
        static_folder="static",
    )
    # Only Grapes accepts videos up to 300 MiB. The small multipart allowance
    # keeps a full-size upload from being rejected because of form boundaries.
    # Features with lower limits still enforce those limits after upload.
    app.config["MAX_CONTENT_LENGTH"] = 301 * 1024 * 1024
    app.config["BODY_TIMEOUT"]       = 600
    # Session cookies: Secure flag so browsers only send them over HTTPS;
    # SameSite=Lax prevents CSRF while keeping normal navigation working.
    app.config["SESSION_COOKIE_SECURE"]   = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.db = db
    app.bot = bot
    branding_dir = os.path.join(os.path.dirname(db.db_path), "branding")
    os.makedirs(branding_dir, exist_ok=True)
    card_image_dir = os.path.join(os.path.dirname(db.db_path), "card_images")
    os.makedirs(card_image_dir, exist_ok=True)
    event_image_dir = os.path.join(os.path.dirname(db.db_path), "event_images")
    os.makedirs(event_image_dir, exist_ok=True)
    file_share_dir = os.path.join(os.path.dirname(db.db_path), "file_sharing")
    os.makedirs(file_share_dir, exist_ok=True)
    only_grapes_dir = os.path.join(os.path.dirname(db.db_path), "only_grapes")
    only_grapes_video_dir = os.path.join(only_grapes_dir, "videos")
    only_grapes_asset_dir = os.path.join(only_grapes_dir, "assets")
    os.makedirs(only_grapes_video_dir, exist_ok=True)
    os.makedirs(only_grapes_asset_dir, exist_ok=True)
    for partial_name in os.listdir(only_grapes_video_dir):
        if partial_name.endswith((".part", ".optimized.mp4")):
            try:
                os.remove(os.path.join(only_grapes_video_dir, partial_name))
            except OSError:
                pass
    for partial_name in os.listdir(file_share_dir):
        if partial_name.endswith(".part"):
            try:
                os.remove(os.path.join(file_share_dir, partial_name))
            except OSError:
                pass
    app.scan_status = {"running": False, "progress": "", "result": ""}
    app.title_scan_status = {"running": False, "progress": "", "result": ""}
    app.reaction_scan_status = {"running": False, "progress": "", "result": ""}
    app.cleanup_status = {"running": False, "progress": "", "result": ""}
    app.database_restore_pending = False
    app.player_reaction_locks = {}
    app.file_share_lock = asyncio.Lock()
    app.songripper_playlist_jobs = {}
    app.songripper_playlist_lock = asyncio.Lock()
    app.song_rating_new_tokens = {}
    app.song_rating_sync_status = {
        "running": False,
        "total": 0,
        "processed": 0,
        "resolved": 0,
        "unresolved": 0,
    }
    app.song_rating_sync_task = None
    app.trya_dcs_chat_rate = {}
    app.trya_dcs_token_rate = {}
    app.trya_dcs_presence = {}
    # Serializes manual starts of both radio managers. The scheduled Exp.
    # Radio start additionally checks the legacy manager before it fires.
    app.radio_start_lock = asyncio.Lock()

    @app.template_filter("timestamp_to_date")
    def timestamp_to_date(ts):
        from datetime import datetime, timezone
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%d.%m.%Y")

    # --- Auth helpers ---

    def login_required(f):
        @functools.wraps(f)
        async def decorated(*args, **kwargs):
            if "user_id" not in session:
                return redirect(url_for("login"))
            user = await db.get_web_user_by_id(session["user_id"])
            if not user:
                session.clear()
                return redirect(url_for("login"))
            if user["must_change_password"] and request.endpoint != "change_password":
                return redirect(url_for("change_password"))
            return await f(*args, **kwargs)
        return decorated

    ALL_PERMISSIONS = [
        ('dashboard', 'Dashboard'),
        ('channels', 'Channels'),
        ('roles', 'Roles'),
        ('users', 'Users'),
        ('member_directory', 'Member Directory'),
        ('file_sharing', 'File Sharing'),
        ('only_grapes', 'Only Grapes'),
        ('welcome', 'Welcome'),
        ('party_playlist', 'Party Playlist'),
        ('playlist_search', 'Playlist Search'),
        ('player', 'Suno Player'),
        ('song_stats', 'Song Stats'),
        ('user_stats', 'User Stats'),
        ('reaction_stats', 'Reaction Stats'),
        ('song_rating_api', 'Song Rating API'),
        ('reaction_roles', 'Reaction Roles'),
        ('image_posting', 'Image Posting'),
        ('polls', 'Polls'),
        ('quiz', 'Quiz'),
        ('radio', 'Twitch Radio'),
        ('exp_radio', 'Experimental Radio'),
        ('trya_stream', 'TrYa Stream'),
        ('trya_dcs', 'TrYa DCS'),
        ('submission_bans', 'Submission Bans'),
        ('auto_translate', 'Auto Translate'),
        ('twitch_alerts', 'Twitch Alerts'),
        ('channel_moderation', 'Channel Moderation'),
        ('executioner', 'Executioner'),
        ('songripper', 'Songripper'),
        ('suno_analyzer', 'Suno Analyzer'),
        ('suno_promotion', 'Suno Promotion'),
        ('suno_info', 'Suno Info'),
        ('audit', 'Audit Log'),
        ('settings', 'Settings'),
        ('llm', 'Corax Chat'),
        ('relic_hunt', "Raven's Nest"),
        ('rpg', 'RPG Adventures'),
        ('card_collection', 'Card Collection'),
        ('event_registration', 'Event Registration'),
        ('birthday_calendar', 'Birthday Calendar'),
        ('reminders', 'Reminders'),
    ]

    SIDEBAR_NAV_ITEMS = [
        {"key": "channels", "endpoint": "channels", "icon": "📢", "label": "Channels", "perm": "channels"},
        {"key": "roles", "endpoint": "roles", "icon": "🛡️", "label": "Roles", "perm": "roles"},
        {"key": "users", "endpoint": "users", "icon": "👥", "label": "Users", "perm": "users"},
        {"key": "member_directory", "endpoint": "member_directory", "icon": "📇", "label": "Member Directory", "perm": "member_directory"},
        {"key": "file_sharing", "endpoint": "file_sharing_admin", "icon": "📦", "label": "File Sharing", "perm": "file_sharing"},
        {"key": "only_grapes", "endpoint": "only_grapes_admin", "icon": "🍇", "label": "Only Grapes", "perm": "only_grapes"},
        {"key": "welcome", "endpoint": "welcome", "icon": "👋", "label": "Welcome", "perm": "welcome"},
        {"key": "party_playlist", "endpoint": "party_playlist", "icon": "🎧", "label": "Party Playlist", "perm": "party_playlist"},
        {"key": "playlist_search", "endpoint": "playlist_search", "icon": "🔍", "label": "Playlist Search", "perm": "playlist_search"},
        {"key": "player", "endpoint": "player", "icon": "🎵", "label": "Suno Player", "perm": "player"},
        {"key": "song_stats", "endpoint": "song_stats", "icon": "📈", "label": "Song Stats", "perm": "song_stats"},
        {"key": "user_stats", "endpoint": "user_stats", "icon": "👤", "label": "User Stats", "perm": "user_stats"},
        {"key": "reaction_stats", "endpoint": "reaction_stats", "icon": "💬", "label": "Reaction Stats", "perm": "reaction_stats"},
        {"key": "song_rating_api", "endpoint": "song_rating_api_admin", "icon": "🔌", "label": "Song Rating API", "perm": "song_rating_api"},
        {"key": "reaction_roles", "endpoint": "reaction_roles", "icon": "🎭", "label": "Reaction Roles", "perm": "reaction_roles"},
        {"key": "image_posting", "endpoint": "image_posting", "icon": "🖼️", "label": "Image Posting", "perm": "image_posting"},
        {"key": "polls", "endpoint": "polls", "icon": "📊", "label": "Polls", "perm": "polls"},
        {"key": "quiz", "endpoint": "quiz_admin", "icon": "❓", "label": "Quiz", "perm": "quiz"},
        {"key": "rpg", "endpoint": "rpg_admin", "icon": "🎲", "label": "RPG", "perm": "rpg"},
        {"key": "card_collection", "endpoint": "card_collection_admin", "icon": "🃏", "label": "Card Collection", "perm": "card_collection"},
        {"key": "event_registration", "endpoint": "event_registration_admin", "icon": "🎟️", "label": "Events", "perm": "event_registration"},
        {"key": "birthday_calendar", "endpoint": "birthday_calendar_admin", "icon": "🎂", "label": "Birthday Calendar", "perm": "birthday_calendar"},
        {"key": "reminders", "endpoint": "reminders_admin", "icon": "⏰", "label": "Reminders", "perm": "reminders"},
        {"key": "radio", "endpoint": "radio_admin", "icon": "📻", "label": "Twitch", "perm": "radio"},
        {"key": "exp_radio", "endpoint": "exp_radio_admin", "icon": "🎙️", "label": "Exp. Radio", "perm": "exp_radio"},
        {"key": "trya_stream", "endpoint": "trya_stream_admin", "icon": "trya_logo", "label": "TrYa Stream", "perm": "trya_stream"},
        {"key": "trya_dcs", "endpoint": "trya_dcs_admin", "icon": "📡", "label": "TrYa DCS", "perm": "trya_dcs"},
        {"key": "submission_bans", "endpoint": "submission_bans", "icon": "⛔", "label": "Submission Bans", "perm": "submission_bans"},
        {"key": "relic_hunt", "endpoint": "relic_hunt_admin", "icon": "🪶", "label": "Raven's Nest", "perm": "relic_hunt"},
        {"key": "twitch_alerts", "endpoint": "twitch_alerts_admin", "icon": "📣", "label": "Twitch Alerts", "perm": "twitch_alerts"},
        {"key": "auto_translate", "endpoint": "auto_translate_admin", "icon": "🌐", "label": "Auto Translate", "perm": "auto_translate"},
        {"key": "channel_moderation", "endpoint": "channel_moderation", "icon": "🛡️", "label": "Channel Mod", "perm": "channel_moderation"},
        {"key": "executioner", "endpoint": "executioner", "icon": "🪓", "label": "Executioner", "perm": "executioner"},
        {"key": "songripper", "endpoint": "songripper", "icon": "💿", "label": "Songripper", "perm": "songripper"},
        {"key": "suno_analyzer", "endpoint": "suno_analyzer", "icon": "🔬", "label": "Suno Analyzer", "perm": "suno_analyzer"},
        {"key": "suno_promotion", "endpoint": "suno_promotion", "icon": "⭐", "label": "Suno Promotion", "perm": "suno_promotion"},
        {"key": "suno_info", "endpoint": "suno_info", "icon": "trya_logo", "label": "Suno Playlist Player", "perm": "suno_info"},
        {"key": "audit", "endpoint": "audit", "icon": "📋", "label": "Audit Log", "perm": "audit"},
        {"key": "llm", "endpoint": "llm", "icon": "🤖", "label": "Corax Chat", "perm": "llm"},
    ]

    def _parse_sidebar_visible(raw: str | None) -> set[str]:
        known = {item["key"] for item in SIDEBAR_NAV_ITEMS}
        if not raw:
            return set(known)
        if raw == "__none__":
            return set()
        selected = {part.strip() for part in raw.split(",") if part.strip()}
        selected = selected & known
        selected.add("twitch_alerts")
        return selected

    @app.route("/branding/<filename>")
    async def branding_asset(filename):
        from quart import send_from_directory
        return await send_from_directory(branding_dir, filename)

    @app.route("/card-images/<filename>")
    async def collectible_card_image(filename):
        from quart import abort, send_from_directory
        safe_name = os.path.basename(filename)
        if safe_name != filename:
            abort(404)
        return await send_from_directory(card_image_dir, safe_name)

    @app.route("/event-images/<filename>")
    async def community_event_image(filename):
        from quart import abort, send_from_directory
        safe_name = os.path.basename(filename)
        if safe_name != filename:
            abort(404)
        return await send_from_directory(event_image_dir, safe_name)

    @app.context_processor
    async def inject_sidebar_nav():
        visible = _parse_sidebar_visible(await db.get_setting("sidebar_visible_items"))
        custom_icon = os.path.basename(await db.get_setting("admin_bot_icon") or "")
        custom_icon_path = os.path.join(branding_dir, custom_icon) if custom_icon else ""
        if custom_icon and os.path.isfile(custom_icon_path):
            bot_icon_url = url_for("branding_asset", filename=custom_icon)
        else:
            custom_icon = ""
            bot_icon_url = url_for("static", filename="Bot_icon_small.png")
        return {
            "sidebar_nav_items": SIDEBAR_NAV_ITEMS,
            "sidebar_visible_items": visible,
            "admin_bot_icon_url": bot_icon_url,
            "admin_bot_icon_custom": bool(custom_icon),
        }

    def admin_required(f):
        @functools.wraps(f)
        async def decorated(*args, **kwargs):
            if "user_id" not in session:
                return redirect(url_for("login"))
            user = await db.get_web_user_by_id(session["user_id"])
            if not user:
                session.clear()
                return redirect(url_for("login"))
            if not user.get("is_admin"):
                await flash("Admin access required.", "error")
                return redirect(url_for("dashboard"))
            return await f(*args, **kwargs)
        return decorated

    def permission_required(perm):
        def decorator(f):
            @functools.wraps(f)
            async def decorated(*args, **kwargs):
                if "user_id" not in session:
                    return redirect(url_for("login"))
                user = await db.get_web_user_by_id(session["user_id"])
                if not user:
                    session.clear()
                    return redirect(url_for("login"))
                if user["must_change_password"] and request.endpoint != "change_password":
                    return redirect(url_for("change_password"))
                # admins always have access
                if user.get("is_admin"):
                    return await f(*args, **kwargs)
                import json
                perms = []
                try:
                    perms = json.loads(user.get("permissions") or "[]")
                except (json.JSONDecodeError, TypeError):
                    pass
                if perm not in perms:
                    await flash("You don't have permission to access this page.", "error")
                    return redirect(url_for("dashboard"))
                return await f(*args, **kwargs)
            return decorated
        return decorator

    def hash_password(password: str) -> str:
        return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    def check_password(password: str, hashed: str) -> bool:
        return bcrypt.checkpw(password.encode(), hashed.encode())

    def get_guild():
        if bot and bot.is_ready():
            from config import Config
            return bot.get_guild(Config.GUILD_ID)
        return None

    def _public_web_url() -> str:
        from config import Config
        configured = Config.WEB_URL.strip().rstrip("/")
        if configured:
            return configured
        try:
            return request.url_root.rstrip("/")
        except RuntimeError:
            scheme = "https" if websocket.scheme == "wss" else "http"
            return f"{scheme}://{websocket.host}".rstrip("/")

    def _player_discord_callback_url() -> str:
        return f"{_public_web_url()}/player/discord/callback"

    def _public_player_discord_callback_url() -> str:
        return f"{_public_web_url()}/public/player/discord/callback"

    async def _delete_temp_file_later(path: str, delay: int = 300) -> None:
        await asyncio.sleep(delay)
        try:
            os.remove(path)
        except OSError:
            pass

    async def _apply_database_restore(staged_path: str, marker: dict) -> None:
        """Quiesce background work, swap the DB, and let Docker restart us."""
        import contextlib

        await asyncio.sleep(2)
        try:
            for task_name in (
                "radio_cleanup_task",
                "exp_radio_cleanup_task",
                "exp_radio_schedule_task",
                "trya_stream_cleanup_task",
                "trya_stream_schedule_task",
            ):
                task = getattr(app, task_name, None)
                if task and not task.done():
                    task.cancel()

            with contextlib.suppress(Exception):
                await twitch_event_alerts.stop()
            with contextlib.suppress(Exception):
                await trya_stream_event_alerts.stop()
            with contextlib.suppress(Exception):
                await relic_hunt.stop()
            with contextlib.suppress(Exception):
                await trya_relic_hunt.stop()
            if bot and not bot.is_closed():
                with contextlib.suppress(Exception):
                    await bot.close()

            await db.close()
            for sidecar in (db.db_path + "-wal", db.db_path + "-shm"):
                with contextlib.suppress(OSError):
                    os.remove(sidecar)
            os.replace(staged_path, db.db_path)
            marker_path = os.path.join(
                os.path.dirname(os.path.abspath(db.db_path)),
                "database-restore-pending.json",
            )
            with open(marker_path, "w", encoding="utf-8") as handle:
                json.dump(marker, handle)
            print("[backup] Database restore applied; restarting process.", flush=True)
            os._exit(0)
        except Exception as exc:
            print(f"[backup] Database restore failed during swap: {exc}", flush=True)
            os._exit(1)

    async def _player_discord_oauth_credentials() -> tuple[str, str]:
        from config import Config
        client_id = (
            await db.get_setting("player_discord_client_id")
            or Config.DISCORD_CLIENT_ID
            or (str(bot.user.id) if bot and bot.user else "")
        )
        client_secret = (
            await db.get_setting("player_discord_client_secret")
            or Config.DISCORD_CLIENT_SECRET
        )
        return client_id.strip(), client_secret.strip()

    async def get_guild_members(guild):
        if not guild:
            return []
        members = list(getattr(guild, "members", []) or [])
        if len(members) <= 1:
            try:
                members = [m async for m in guild.fetch_members(limit=None)]
            except Exception:
                pass
        return sorted(
            [m for m in members if not getattr(m, "bot", False)],
            key=lambda m: ((m.display_name or m.name or "").casefold(), m.id),
        )

    async def get_all_guild_members(guild):
        """Return the complete Discord member list, including bot accounts."""
        if not guild:
            return [], False
        members = list(getattr(guild, "members", []) or [])
        expected = int(getattr(guild, "member_count", 0) or 0)
        complete = not expected or len(members) >= expected
        if not complete:
            try:
                members = [member async for member in guild.fetch_members(limit=None)]
                complete = not expected or len(members) >= expected
            except Exception as exc:
                print(f"[member-directory] Full member fetch failed: {exc}", flush=True)
        return members, complete

    def build_member_directory_row(member) -> dict:
        roles = [role for role in reversed(member.roles[1:])]
        timeout_until = getattr(member, "timed_out_until", None)
        if timeout_until is None:
            timeout_until = getattr(member, "communication_disabled_until", None)
        return {
            "id": member.id,
            "display_name": member.display_name or member.name,
            "username": member.name,
            "global_name": getattr(member, "global_name", None) or "",
            "nickname": member.nick or "",
            "avatar_url": str(member.display_avatar.url),
            "is_bot": bool(member.bot),
            "is_admin": bool(member.guild_permissions.administrator),
            "pending": bool(getattr(member, "pending", False)),
            "joined_at": member.joined_at,
            "created_at": member.created_at,
            "premium_since": member.premium_since,
            "timeout_until": timeout_until,
            "roles": [{"id": role.id, "name": role.name} for role in roles],
            "role_ids": {role.id for role in roles},
            "role_names": [role.name for role in roles],
            "top_role": roles[0].name if roles else "",
            "last_activity": None,
        }

    def add_member_directory_activity(
        rows: list[dict], activity_rows: list[dict]
    ) -> None:
        from datetime import datetime, timezone

        now_ts = time.time()
        activity_by_user = {
            int(activity["user_id"]): activity
            for activity in activity_rows
            if activity.get("user_id") is not None
        }
        for row in rows:
            activity = activity_by_user.get(row["id"])
            if not activity or activity.get("timestamp") is None:
                continue
            timestamp = float(activity["timestamp"])
            row["last_activity"] = {
                **activity,
                "timestamp": timestamp,
                "datetime": datetime.fromtimestamp(timestamp, tz=timezone.utc),
                "days_ago": max(0, int((now_ts - timestamp) // 86400)),
            }

    def filter_member_directory_rows(rows: list[dict], args) -> list[dict]:
        query = (args.get("q") or "").strip().casefold()
        kind = (args.get("kind") or "all").strip().lower()
        role_raw = (args.get("role") or "").strip()
        role_id = int(role_raw) if role_raw.isdigit() else None
        filtered = []
        for row in rows:
            if kind == "human" and row["is_bot"]:
                continue
            if kind == "bot" and not row["is_bot"]:
                continue
            if role_id is not None and role_id not in row["role_ids"]:
                continue
            if query:
                haystack = " ".join(
                    [
                        row["display_name"], row["username"], row["global_name"],
                        row["nickname"], str(row["id"]), *row["role_names"],
                        (row["last_activity"] or {}).get("channel_name", ""),
                    ]
                ).casefold()
                if query not in haystack:
                    continue
            filtered.append(row)

        sort_key = (args.get("sort") or "name").strip().lower()
        distant_future = 253402300799.0
        if sort_key == "joined_newest":
            filtered.sort(
                key=lambda row: row["joined_at"].timestamp() if row["joined_at"] else 0,
                reverse=True,
            )
        elif sort_key == "joined_oldest":
            filtered.sort(
                key=lambda row: row["joined_at"].timestamp() if row["joined_at"] else distant_future
            )
        elif sort_key == "account_newest":
            filtered.sort(key=lambda row: row["created_at"].timestamp(), reverse=True)
        elif sort_key == "account_oldest":
            filtered.sort(key=lambda row: row["created_at"].timestamp())
        elif sort_key == "activity_newest":
            filtered.sort(
                key=lambda row: (row["last_activity"] or {}).get("timestamp", 0),
                reverse=True,
            )
        elif sort_key == "activity_oldest":
            filtered.sort(
                key=lambda row: (
                    (row["last_activity"] or {}).get("timestamp", distant_future)
                )
            )
        else:
            filtered.sort(key=lambda row: (row["display_name"].casefold(), row["id"]))
        return filtered

    def build_member_history_charts(
        events: list[dict], tracking_started_at: float | None
    ) -> dict:
        from collections import defaultdict
        from datetime import datetime, timezone

        unique_events = []
        seen_events = set()
        for event in events:
            event_key = (
                event["event_type"],
                event.get("user_id"),
                round(float(event["occurred_at"])),
            )
            if event_key in seen_events:
                continue
            seen_events.add(event_key)
            unique_events.append(event)
        events = unique_events

        monthly = defaultdict(lambda: {"joins": 0, "leaves": 0})
        yearly = defaultdict(lambda: {"joins": 0, "leaves": 0})
        for event in events:
            occurred = datetime.fromtimestamp(event["occurred_at"], tz=timezone.utc)
            month_key = occurred.strftime("%Y-%m")
            year_key = occurred.strftime("%Y")
            field = "joins" if event["event_type"] == "join" else "leaves"
            monthly[month_key][field] += 1
            yearly[year_key][field] += 1

        now = datetime.now(timezone.utc)

        def shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
            absolute = year * 12 + month - 1 + delta
            return absolute // 12, absolute % 12 + 1

        all_month_keys = sorted(monthly)
        if all_month_keys:
            first_year, first_month = map(int, all_month_keys[0].split("-"))
        else:
            first_year, first_month = now.year, now.month
        month_keys = []
        year, month = first_year, first_month
        while (year, month) <= (now.year, now.month):
            month_keys.append(f"{year:04d}-{month:02d}")
            year, month = shift_month(year, month, 1)

        balance = 0
        growth_values = []
        for key in month_keys:
            balance += monthly[key]["joins"] - monthly[key]["leaves"]
            growth_values.append(balance)

        growth_limit = 48
        growth_keys = month_keys[-growth_limit:]
        growth_values = growth_values[-growth_limit:]
        monthly_keys = month_keys[-18:]
        year_keys = sorted(yearly)
        tracking_label = ""
        if tracking_started_at:
            tracking_label = datetime.fromtimestamp(
                tracking_started_at, tz=timezone.utc
            ).strftime("%d %B %Y")

        def recent_member_events(event_type: str, limit: int = 10) -> list[dict]:
            matching_events = sorted(
                (
                    event
                    for event in events
                    if event["event_type"] == event_type
                ),
                key=lambda event: event["occurred_at"],
                reverse=True,
            )
            result = []
            for event in matching_events[:limit]:
                display_name = (event.get("display_name") or "").strip()
                user_name = (event.get("user_name") or "").strip()
                result.append({
                    "display_name": display_name or user_name or "Unknown member",
                    "user_name": user_name,
                    "user_id": event.get("user_id"),
                    "occurred_at_label": datetime.fromtimestamp(
                        event["occurred_at"], tz=timezone.utc
                    ).strftime("%d.%m.%Y %H:%M UTC"),
                })
            return result

        join_events = [event for event in events if event["event_type"] == "join"]
        leave_events = [event for event in events if event["event_type"] == "leave"]
        recent_joins = recent_member_events("join")
        recent_departures = recent_member_events("leave")

        return {
            "growth": {
                "labels": growth_keys,
                "values": growth_values,
            },
            "monthly": {
                "labels": monthly_keys,
                "joins": [monthly[key]["joins"] for key in monthly_keys],
                "leaves": [monthly[key]["leaves"] for key in monthly_keys],
            },
            "yearly": {
                "labels": year_keys,
                "joins": [yearly[key]["joins"] for key in year_keys],
                "leaves": [yearly[key]["leaves"] for key in year_keys],
            },
            "tracked_leaves": sum(1 for event in events if event["event_type"] == "leave"),
            "recent_joins": recent_joins,
            "joins_truncated": len(join_events) > len(recent_joins),
            "recent_departures": recent_departures,
            "departures_truncated": len(leave_events) > len(recent_departures),
            "tracking_started_label": tracking_label,
        }

    def find_latest_cached_message(bot_obj, user_id: int):
        latest = None
        for message in getattr(bot_obj, "cached_messages", []) or []:
            if getattr(getattr(message, "author", None), "id", None) != user_id:
                continue
            if latest is None or message.created_at > latest.created_at:
                latest = message
        if not latest:
            return None
        channel = getattr(latest, "channel", None)
        return {
            "type": "Message",
            "timestamp": latest.created_at.timestamp(),
            "summary": f"Message in #{getattr(channel, 'name', 'unknown-channel')}",
        }

    # --- Routes ---

    @app.route("/login", methods=["GET", "POST"])
    async def login():
        if request.method == "POST":
            form = await request.form
            username = form.get("username", "").strip()
            password = form.get("password", "")
            user = await db.get_web_user(username)
            if user and check_password(password, user["password_hash"]):
                session["user_id"] = user["id"]
                session["username"] = user["username"]
                session["is_admin"] = bool(user.get("is_admin"))
                import json
                try:
                    session["permissions"] = json.loads(user.get("permissions") or "[]")
                except (json.JSONDecodeError, TypeError):
                    session["permissions"] = []
                if user["must_change_password"]:
                    return redirect(url_for("change_password"))
                return redirect(url_for("dashboard"))
            await flash("Invalid username or password.", "error")
        return await render_template("login.html")

    @app.route("/logout")
    async def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.route("/change-password", methods=["GET", "POST"])
    @login_required
    async def change_password():
        if request.method == "POST":
            form = await request.form
            current = form.get("current_password", "")
            new_pw = form.get("new_password", "")
            confirm = form.get("confirm_password", "")
            user = await db.get_web_user_by_id(session["user_id"])

            if not check_password(current, user["password_hash"]):
                await flash("Current password is incorrect.", "error")
            elif len(new_pw) < 6:
                await flash("New password must be at least 6 characters.", "error")
            elif new_pw != confirm:
                await flash("Passwords do not match.", "error")
            else:
                await db.update_web_user_password(user["id"], hash_password(new_pw))
                await flash("Password changed successfully.", "success")
                return redirect(url_for("dashboard"))
        return await render_template("change_password.html")

    @app.route("/")
    @login_required
    async def dashboard():
        channels = await db.get_monitored_channels()
        exempt_roles = await db.get_exempt_roles()
        command_roles = await db.get_command_roles()
        bot_name = await db.get_setting("bot_name") or "Slowmode Bot"
        guild_id = await db.get_setting("guild_id") or ""
        log_count = await db.get_audit_log_count()
        system_stats = _collect_container_stats(db.db_path)

        bot_connected = bot is not None and bot.is_ready()
        guild_name = None
        if bot_connected:
            guild = get_guild()
            if guild:
                guild_name = guild.name

        return await render_template(
            "dashboard.html",
            channels=channels,
            exempt_roles=exempt_roles,
            command_roles=command_roles,
            bot_name=bot_name,
            guild_id=guild_id,
            guild_name=guild_name,
            bot_connected=bot_connected,
            log_count=log_count,
            system_stats=system_stats,
        )

    @app.route("/api/dashboard/system-stats")
    @login_required
    async def dashboard_system_stats():
        from quart import jsonify
        return jsonify(_collect_container_stats(db.db_path))

    @app.route("/settings", methods=["GET", "POST"])
    @permission_required('settings')
    async def settings():
        if request.method == "POST":
            form = await request.form
            action = form.get("action", "")

            if action == "reset_bot_icon":
                current_icon = os.path.basename(await db.get_setting("admin_bot_icon") or "")
                await db.set_setting("admin_bot_icon", "")
                if current_icon:
                    current_path = os.path.join(branding_dir, current_icon)
                    if os.path.isfile(current_path):
                        os.remove(current_path)
                await db.add_audit_log(
                    event_type="bot_icon_reset",
                    details="Admin UI bot icon restored to default",
                    actor=session.get("username", "unknown"),
                )
                await flash("Default bot icon restored.", "success")
                return redirect(url_for("settings"))

            if action == "lp_toggle":
                lp_enabled = "1" if form.get("listening_party_enabled") else "0"
                await db.set_setting("listening_party_enabled", lp_enabled)
                await db.add_audit_log(
                    event_type="listening_party_toggled",
                    details=f"Random Song Listening Party {'enabled' if lp_enabled == '1' else 'disabled'}",
                    actor=session.get("username", "unknown"),
                )
                await flash(
                    f"Listening Party Random Song feature {'enabled' if lp_enabled == '1' else 'disabled'}.",
                    "success",
                )
                return redirect(url_for("settings"))

            if action == "lp_add":
                input_channel_id = form.get("input_channel_id", "").strip()
                output_channel_id = form.get("output_channel_id", "").strip()
                time_range = int(form.get("time_range_hours", "24"))
                if not input_channel_id.isdigit() or not output_channel_id.isdigit():
                    await flash("Invalid channel ID.", "error")
                else:
                    input_channel_id = int(input_channel_id)
                    output_channel_id = int(output_channel_id)
                    monitored = await db.get_monitored_channel(input_channel_id)
                    if not monitored:
                        await flash("Input channel must be a monitored channel.", "error")
                    elif input_channel_id == output_channel_id:
                        await flash("Input and output channel must be different.", "error")
                    else:
                        await db.add_listening_party_config(input_channel_id, output_channel_id, time_range)
                        await db.add_audit_log(
                            event_type="listening_party_added",
                            channel_id=input_channel_id,
                            details=f"Listening party config added: input={input_channel_id}, output={output_channel_id}, range={time_range}h",
                            actor=session.get("username", "unknown"),
                        )
                        await flash("Listening party config added.", "success")
                return redirect(url_for("settings"))

            elif action == "lp_update":
                config_id = int(form.get("config_id", "0"))
                output_channel_id = int(form.get("output_channel_id", "0"))
                time_range = int(form.get("time_range_hours", "24"))
                await db.update_listening_party_config(config_id, output_channel_id, time_range)
                await db.add_audit_log(
                    event_type="listening_party_updated",
                    details=f"Config {config_id} updated: output={output_channel_id}, range={time_range}h",
                    actor=session.get("username", "unknown"),
                )
                await flash("Config updated.", "success")
                return redirect(url_for("settings"))

            elif action == "lp_remove":
                config_id = int(form.get("config_id", "0"))
                await db.remove_listening_party_config(config_id)
                await db.add_audit_log(
                    event_type="listening_party_removed",
                    details=f"Config {config_id} removed",
                    actor=session.get("username", "unknown"),
                )
                await flash("Config removed.", "success")
                return redirect(url_for("settings"))

            bot_name = form.get("bot_name", "").strip()
            guild_id = form.get("guild_id", "").strip()
            new_channel = form.get("new_command_channel", "").strip()

            party_max_songs = form.get("party_max_songs", "2").strip()
            party_voice_channel = form.get("party_voice_channel", "").strip()
            player_url = form.get("player_url", "").strip()
            player_discord_client_id = form.get("player_discord_client_id", "").strip()
            player_discord_client_secret = form.get("player_discord_client_secret", "").strip()
            known_sidebar = {item["key"] for item in SIDEBAR_NAV_ITEMS}
            sidebar_visible = [
                item["key"] for item in SIDEBAR_NAV_ITEMS
                if form.get(f"sidebar_{item['key']}") and item["key"] in known_sidebar
            ]

            files = await request.files
            icon_file = files.get("bot_icon")
            if icon_file and icon_file.filename:
                import uuid
                original_name = icon_file.filename.strip()
                ext = original_name.rsplit(".", 1)[-1].lower() if "." in original_name else ""
                allowed_icons = {"png", "jpg", "jpeg", "webp", "gif"}
                if ext not in allowed_icons:
                    await flash("Invalid bot icon. Use PNG, JPEG, WebP or GIF.", "error")
                    return redirect(url_for("settings"))

                stored_ext = "jpg" if ext == "jpeg" else ext
                new_icon = f"bot_icon_{uuid.uuid4().hex}.{stored_ext}"
                new_path = os.path.join(branding_dir, new_icon)
                await icon_file.save(new_path)
                try:
                    if os.path.getsize(new_path) > 2 * 1024 * 1024:
                        raise ValueError("Bot icon is larger than 2 MB.")
                    with open(new_path, "rb") as icon_handle:
                        header = icon_handle.read(16)
                    valid_signature = (
                        (stored_ext == "png" and header.startswith(b"\x89PNG\r\n\x1a\n"))
                        or (stored_ext == "jpg" and header.startswith(b"\xff\xd8\xff"))
                        or (stored_ext == "gif" and header.startswith((b"GIF87a", b"GIF89a")))
                        or (
                            stored_ext == "webp"
                            and header.startswith(b"RIFF")
                            and header[8:12] == b"WEBP"
                        )
                    )
                    if not valid_signature:
                        raise ValueError("The uploaded file is not a valid image of the selected type.")
                except (OSError, ValueError) as exc:
                    if os.path.isfile(new_path):
                        os.remove(new_path)
                    await flash(str(exc), "error")
                    return redirect(url_for("settings"))

                old_icon = os.path.basename(await db.get_setting("admin_bot_icon") or "")
                await db.set_setting("admin_bot_icon", new_icon)
                if old_icon and old_icon != new_icon:
                    old_path = os.path.join(branding_dir, old_icon)
                    if os.path.isfile(old_path):
                        os.remove(old_path)

            if bot_name:
                await db.set_setting("bot_name", bot_name)
            if guild_id:
                await db.set_setting("guild_id", guild_id)
            await db.set_setting("new_command_channel", new_channel)
            await db.set_setting("party_max_songs", party_max_songs)
            await db.set_setting("party_voice_channel", party_voice_channel)
            await db.set_setting("player_url", player_url)
            await db.set_setting("player_discord_client_id", player_discord_client_id)
            if form.get("clear_player_discord_client_secret") == "1":
                await db.set_setting("player_discord_client_secret", "")
            elif player_discord_client_secret:
                await db.set_setting(
                    "player_discord_client_secret", player_discord_client_secret
                )
            await db.set_setting("sidebar_visible_items", ",".join(sidebar_visible) or "__none__")

            await db.add_audit_log(
                event_type="settings_changed",
                details=f"Bot name: {bot_name}, Guild ID: {guild_id}, /new channel: {new_channel}, party_max_songs: {party_max_songs}, party_voice_channel: {party_voice_channel}, player_url: {player_url}, sidebar_visible_items: {len(sidebar_visible)}, bot_icon_uploaded: {bool(icon_file and icon_file.filename)}",
                actor=session.get("username", "unknown"),
            )
            await flash("Settings saved.", "success")
            return redirect(url_for("settings"))

        bot_name = await db.get_setting("bot_name") or "Slowmode Bot"
        guild_id = await db.get_setting("guild_id") or ""
        new_command_channel = await db.get_setting("new_command_channel") or ""
        party_max_songs = await db.get_setting("party_max_songs") or "2"
        party_voice_channel = await db.get_setting("party_voice_channel") or ""
        player_url = await db.get_setting("player_url") or ""
        from config import Config
        player_discord_client_id = (
            await db.get_setting("player_discord_client_id")
            or Config.DISCORD_CLIENT_ID
            or (str(bot.user.id) if bot and bot.user else "")
        )
        player_discord_secret_configured = bool(
            await db.get_setting("player_discord_client_secret")
            or Config.DISCORD_CLIENT_SECRET
        )
        monitored = await db.get_monitored_channels()
        guild = get_guild()
        channel_options = []
        for ch in monitored:
            ch_name = f"channel-{ch['channel_id']}"
            if guild:
                gch = guild.get_channel(ch["channel_id"])
                if gch:
                    ch_name = gch.name
            channel_options.append({"id": ch["channel_id"], "name": ch_name})
        # All text + voice channels for party post channel selection
        all_text_channels = []
        if guild:
            for ch in sorted(guild.text_channels, key=lambda c: c.position):
                all_text_channels.append({"id": ch.id, "name": ch.name})
            for ch in sorted(guild.voice_channels, key=lambda c: c.position):
                all_text_channels.append({"id": ch.id, "name": f"🔊 {ch.name}"})
        # Enrich monitored channels with names for LP config form
        for ch in monitored:
            ch["channel_name"] = f"channel-{ch['channel_id']}"
            if guild:
                gch = guild.get_channel(ch["channel_id"])
                if gch:
                    ch["channel_name"] = gch.name
        # Listening party configs
        listening_party_enabled = (
            await db.get_setting("listening_party_enabled") or "1"
        )
        lp_configs = await db.get_listening_party_configs()
        available_output_channels = []
        if guild:
            for ch in guild.text_channels:
                available_output_channels.append({"id": ch.id, "name": ch.name})
        for cfg in lp_configs:
            cfg["input_name"] = f"channel-{cfg['input_channel_id']}"
            cfg["output_name"] = f"channel-{cfg['output_channel_id']}"
            if guild:
                inch = guild.get_channel(cfg["input_channel_id"])
                if inch:
                    cfg["input_name"] = inch.name
                outch = guild.get_channel(cfg["output_channel_id"])
                if outch:
                    cfg["output_name"] = outch.name

        return await render_template("settings.html", bot_name=bot_name, guild_id=guild_id,
                                     new_command_channel=new_command_channel, channel_options=channel_options,
                                     party_max_songs=party_max_songs, party_voice_channel=party_voice_channel,
                                     player_url=player_url,
                                     player_discord_client_id=player_discord_client_id,
                                     player_discord_secret_configured=player_discord_secret_configured,
                                     player_discord_callback_url=_player_discord_callback_url(),
                                     public_player_discord_callback_url=_public_player_discord_callback_url(),
                                     all_text_channels=all_text_channels,
                                     monitored_channels=monitored,
                                     available_output_channels=available_output_channels,
                                     listening_party_enabled=listening_party_enabled,
                                     lp_configs=lp_configs)

    @app.route("/settings/backup/database")
    @permission_required("settings")
    async def settings_backup_database():
        import tempfile
        from datetime import datetime, timezone
        from quart import send_file

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        fd, path = tempfile.mkstemp(prefix="corax-db-", suffix=".sqlite3")
        os.close(fd)
        try:
            await db.backup_to(path)
            await db.add_audit_log(
                event_type="database_backup_exported",
                details="Consistent SQLite database backup exported",
                actor=session.get("username", "unknown"),
            )
            asyncio.create_task(_delete_temp_file_later(path))
            return await send_file(
                path,
                mimetype="application/vnd.sqlite3",
                as_attachment=True,
                attachment_filename=f"corax-database-{stamp}.sqlite3",
            )
        except Exception:
            try:
                os.remove(path)
            except OSError:
                pass
            raise

    @app.route("/settings/backup/full")
    @permission_required("settings")
    async def settings_backup_full():
        import tempfile
        from datetime import datetime, timezone
        from quart import send_file

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        db_fd, db_snapshot = tempfile.mkstemp(prefix="corax-full-db-", suffix=".sqlite3")
        zip_fd, archive_path = tempfile.mkstemp(prefix="corax-full-", suffix=".zip")
        os.close(db_fd)
        os.close(zip_fd)
        try:
            await db.backup_to(db_snapshot)
            data_dir = os.path.dirname(os.path.abspath(db.db_path))
            await asyncio.to_thread(
                create_full_data_archive,
                data_dir,
                db.db_path,
                db_snapshot,
                archive_path,
            )
            await db.add_audit_log(
                event_type="full_data_backup_exported",
                details="Full persistent data archive exported",
                actor=session.get("username", "unknown"),
            )
            asyncio.create_task(_delete_temp_file_later(archive_path, delay=900))
            return await send_file(
                archive_path,
                mimetype="application/zip",
                as_attachment=True,
                attachment_filename=f"corax-full-data-{stamp}.zip",
            )
        except Exception:
            try:
                os.remove(archive_path)
            except OSError:
                pass
            raise
        finally:
            try:
                os.remove(db_snapshot)
            except OSError:
                pass

    @app.route("/settings/backup/restore", methods=["POST"])
    @permission_required("settings")
    async def settings_restore_database():
        import uuid
        from datetime import datetime, timezone

        if app.database_restore_pending:
            await flash("A database restore is already pending.", "error")
            return redirect(url_for("settings"))

        form = await request.form
        files = await request.files
        if (form.get("restore_confirmation") or "").strip() != "RESTORE":
            await flash("Type RESTORE to confirm the database replacement.", "error")
            return redirect(url_for("settings"))
        upload = files.get("database_backup")
        if not upload or not upload.filename:
            await flash("Select a SQLite database backup first.", "error")
            return redirect(url_for("settings"))

        app.database_restore_pending = True
        if stream_manager.is_running or getattr(stream_manager, "_loading", False):
            app.database_restore_pending = False
            await flash("Stop the classic Twitch Radio before restoring a backup.", "error")
            return redirect(url_for("settings"))
        if exp_stream_manager.is_running or app.radio_start_lock.locked():
            app.database_restore_pending = False
            await flash("Stop the Experimental Radio before restoring a backup.", "error")
            return redirect(url_for("settings"))
        if trya_stream_manager.is_running or getattr(trya_stream_manager, "_loading", False):
            app.database_restore_pending = False
            await flash("Stop TrYa Stream before restoring a backup.", "error")
            return redirect(url_for("settings"))
        if trya_dcs_manager.is_running:
            app.database_restore_pending = False
            await flash("Stop TrYa DCS before restoring a backup.", "error")
            return redirect(url_for("settings"))

        data_dir = os.path.dirname(os.path.abspath(db.db_path))
        staging_dir = os.path.join(data_dir, "restore_staging")
        os.makedirs(staging_dir, exist_ok=True)
        staged_path = os.path.join(staging_dir, f"restore-{uuid.uuid4().hex}.sqlite3")
        try:
            await upload.save(staged_path)
            validation = await asyncio.to_thread(validate_database_backup, staged_path)

            actor = session.get("username", "unknown")
            await db.add_audit_log(
                event_type="database_restore_requested",
                details=(
                    f"Validated backup with {validation['tables']} tables and "
                    f"{validation['size']} bytes"
                ),
                actor=actor,
            )

            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            safety_dir = os.path.join(data_dir, "restore_backups")
            os.makedirs(safety_dir, exist_ok=True)
            safety_path = os.path.join(safety_dir, f"pre-restore-{stamp}.sqlite3")
            await db.backup_to(safety_path)
            prune_restore_backups(safety_dir, keep=5)

            marker = {
                "restored_at": datetime.now(timezone.utc).isoformat(),
                "actor": actor,
                "safety_backup": os.path.relpath(safety_path, data_dir),
            }
            page = await render_template(
                "database_restore_restarting.html",
                safety_backup=os.path.basename(safety_path),
            )
            asyncio.create_task(_apply_database_restore(staged_path, marker))
            return page
        except ValueError as exc:
            app.database_restore_pending = False
            try:
                os.remove(staged_path)
            except OSError:
                pass
            await flash(str(exc), "error")
            return redirect(url_for("settings"))
        except Exception as exc:
            app.database_restore_pending = False
            try:
                os.remove(staged_path)
            except OSError:
                pass
            await flash(f"Database restore preparation failed: {exc}", "error")
            return redirect(url_for("settings"))

    @app.route("/welcome", methods=["GET", "POST"])
    @permission_required('welcome')
    async def welcome():
        if request.method == "POST":
            form = await request.form
            enabled = form.get("enabled") == "1"
            dm_enabled = form.get("dm_enabled") == "1"
            channel_id = form.get("channel_id", "").strip()
            message_text = form.get("message_text", "").strip()
            dm_text = form.get("dm_text", "").strip()

            # Convert channel_id to int or None
            channel_id_int = int(channel_id) if channel_id.isdigit() else None

            await db.set_welcome_config(
                enabled=enabled,
                channel_id=channel_id_int,
                message_text=message_text if message_text else None,
                dm_enabled=dm_enabled,
                dm_text=dm_text if dm_text else None,
            )

            await db.add_audit_log(
                event_type="welcome_config_updated",
                details=f"enabled={enabled}, channel={channel_id_int}, dm_enabled={dm_enabled}",
                actor=session.get("username", "unknown"),
            )
            await flash("Welcome configuration saved.", "success")
            return redirect(url_for("welcome"))

        config = await db.get_welcome_config()
        guild = get_guild()
        channels = []
        if guild:
            for ch in sorted(guild.text_channels, key=lambda c: c.position):
                channels.append({"id": ch.id, "name": ch.name})

        return await render_template("welcome.html", config=config, channels=channels)

    @app.route("/channels", methods=["GET", "POST"])
    @permission_required('channels')
    async def channels():
        if request.method == "POST":
            form = await request.form
            action = form.get("action")

            if action == "add":
                channel_id = form.get("channel_id", "").strip()
                cooldown = int(form.get("cooldown_minutes", "0"))

                if not channel_id.isdigit():
                    await flash("Invalid channel ID.", "error")
                else:
                    channel_id = int(channel_id)
                    channel_name = f"channel-{channel_id}"

                    guild = get_guild()
                    if guild:
                        ch = guild.get_channel(channel_id)
                        if ch:
                            channel_name = ch.name

                    await db.add_monitored_channel(channel_id, channel_name, cooldown)
                    await db.add_audit_log(
                        event_type="channel_added",
                        channel_id=channel_id,
                        channel_name=channel_name,
                        details=f"Added with {cooldown}min cooldown",
                        actor=session.get("username", "unknown"),
                    )
                    await flash(f"Channel #{channel_name} added.", "success")

            elif action == "update":
                channel_id = int(form.get("channel_id", "0"))
                cooldown = int(form.get("cooldown_minutes", "0"))
                await db.update_channel_cooldown(channel_id, cooldown)
                await db.add_audit_log(
                    event_type="channel_updated",
                    channel_id=channel_id,
                    details=f"Cooldown updated to {cooldown}min",
                    actor=session.get("username", "unknown"),
                )
                await flash("Channel updated.", "success")

            elif action == "toggle":
                channel_id = int(form.get("channel_id", "0"))
                enabled = form.get("enabled") == "1"
                await db.toggle_channel(channel_id, enabled)
                await flash("Channel toggled.", "success")

            elif action == "remove":
                channel_id = int(form.get("channel_id", "0"))
                await db.remove_monitored_channel(channel_id)
                await db.add_audit_log(
                    event_type="channel_removed",
                    channel_id=channel_id,
                    details="Channel removed",
                    actor=session.get("username", "unknown"),
                )
                await flash("Channel removed.", "success")

            elif action == "reset_user_cooldown":
                channel_id = int(form.get("channel_id", "0"))
                user_id = int(form.get("user_id", "0"))
                await db.clear_cooldown_record(user_id, channel_id)
                await db.add_audit_log(
                    event_type="cooldown_reset",
                    user_id=user_id,
                    channel_id=channel_id,
                    details=f"Cooldown manually reset via web interface",
                    actor=session.get("username", "unknown"),
                )
                await flash("User cooldown reset.", "success")

            return redirect(url_for("channels"))

        channel_list = await db.get_monitored_channels()

        guild = get_guild()
        available_channels = []
        if guild:
            monitored_ids = {c["channel_id"] for c in channel_list}
            for ch in guild.text_channels:
                if ch.id not in monitored_ids:
                    available_channels.append({"id": ch.id, "name": ch.name})

        channel_cooldowns = {}
        for ch in channel_list:
            if ch["cooldown_minutes"] > 0:
                records = await db.get_active_cooldowns(ch["channel_id"], ch["cooldown_minutes"])
                users = []
                for r in records:
                    elapsed = time.time() - r["timestamp"]
                    remaining = (ch["cooldown_minutes"] * 60) - elapsed
                    if remaining > 0:
                        user_name = f"User {r['user_id']}"
                        if guild:
                            member = guild.get_member(r["user_id"])
                            if member:
                                user_name = str(member)
                        hours = remaining / 3600
                        if hours >= 1:
                            time_str = f"{math.ceil(hours)}h remaining"
                        else:
                            time_str = f"{math.ceil(remaining / 60)}min remaining"
                        users.append({
                            "user_id": r["user_id"],
                            "user_name": user_name,
                            "time_remaining": time_str,
                        })
                channel_cooldowns[ch["channel_id"]] = users

        return await render_template(
            "channels.html",
            channels=channel_list,
            available_channels=available_channels,
            channel_cooldowns=channel_cooldowns,
        )

    @app.route("/roles", methods=["GET", "POST"])
    @permission_required('roles')
    async def roles():
        if request.method == "POST":
            form = await request.form
            action = form.get("action")
            role_type = form.get("role_type", "exempt")

            if action == "add":
                role_id = form.get("role_id", "").strip()
                if not role_id.isdigit():
                    await flash("Invalid role ID.", "error")
                else:
                    role_id = int(role_id)
                    role_name = f"role-{role_id}"

                    guild = get_guild()
                    if guild:
                        r = guild.get_role(role_id)
                        if r:
                            role_name = r.name

                    if role_type == "exempt":
                        await db.add_exempt_role(role_id, role_name)
                    else:
                        await db.add_command_role(role_id, role_name)

                    await db.add_audit_log(
                        event_type=f"{role_type}_role_added",
                        details=f"Role {role_name} ({role_id}) added as {role_type}",
                        actor=session.get("username", "unknown"),
                    )
                    await flash(f"Role {role_name} added.", "success")

            elif action == "remove":
                role_id = int(form.get("role_id", "0"))
                if role_type == "exempt":
                    await db.remove_exempt_role(role_id)
                else:
                    await db.remove_command_role(role_id)
                await db.add_audit_log(
                    event_type=f"{role_type}_role_removed",
                    details=f"Role {role_id} removed from {role_type}",
                    actor=session.get("username", "unknown"),
                )
                await flash("Role removed.", "success")

            return redirect(url_for("roles"))

        exempt_roles = await db.get_exempt_roles()
        command_roles = await db.get_command_roles()

        guild = get_guild()
        available_roles = []
        if guild:
            exempt_ids = {r["role_id"] for r in exempt_roles}
            command_ids = {r["role_id"] for r in command_roles}
            for r in guild.roles:
                if r.id != guild.default_role.id:
                    available_roles.append({
                        "id": r.id,
                        "name": r.name,
                        "is_exempt": r.id in exempt_ids,
                        "is_command": r.id in command_ids,
                    })

        return await render_template(
            "roles.html",
            exempt_roles=exempt_roles,
            command_roles=command_roles,
            available_roles=available_roles,
        )

    @app.route("/users", methods=["GET", "POST"])
    @permission_required('users')
    async def users():
        if request.method == "POST":
            form = await request.form
            action = form.get("action")

            if action == "add":
                username = form.get("username", "").strip()
                password = form.get("password", "").strip()
                if len(username) < 3:
                    await flash("Username must be at least 3 characters.", "error")
                elif len(password) < 6:
                    await flash("Password must be at least 6 characters.", "error")
                else:
                    success = await db.create_web_user(username, hash_password(password))
                    if success:
                        await db.add_audit_log(
                            event_type="user_created",
                            details=f"Web user '{username}' created",
                            actor=session.get("username", "unknown"),
                        )
                        await flash(f"User '{username}' created.", "success")
                    else:
                        await flash("Username already exists.", "error")

            elif action == "delete":
                user_id = int(form.get("user_id", "0"))
                if user_id == session.get("user_id"):
                    await flash("You cannot delete yourself.", "error")
                else:
                    target = await db.get_web_user_by_id(user_id)
                    if target:
                        await db.delete_web_user(user_id)
                        await db.add_audit_log(
                            event_type="user_deleted",
                            details=f"Web user '{target['username']}' deleted",
                            actor=session.get("username", "unknown"),
                        )
                        await flash("User deleted.", "success")

            elif action == "reset_password":
                user_id = int(form.get("user_id", "0"))
                new_pw = form.get("new_password", "").strip()
                if len(new_pw) < 6:
                    await flash("Password must be at least 6 characters.", "error")
                else:
                    await db.update_web_user_password(user_id, hash_password(new_pw))
                    # Force password change on next login
                    await db.db.execute(
                        "UPDATE web_users SET must_change_password = 1 WHERE id = ?",
                        (user_id,),
                    )
                    await db.db.commit()
                    await flash("Password reset. User must change it on next login.", "success")

            elif action == "set_permissions":
                user_id = int(form.get("user_id", "0"))
                target = await db.get_web_user_by_id(user_id)
                if target and not target.get("is_admin"):
                    import json
                    selected = [k for k, _ in ALL_PERMISSIONS if form.get(f"perm_{k}")]
                    await db.set_user_permissions(user_id, selected)
                    await db.add_audit_log(
                        event_type="permissions_updated",
                        details=f"Permissions for '{target['username']}' set to: {', '.join(selected) or 'none'}",
                        actor=session.get("username", "unknown"),
                    )
                    await flash(f"Permissions updated for '{target['username']}'.", "success")

            return redirect(url_for("users"))

        user_list = await db.get_all_web_users()
        import json
        for u in user_list:
            try:
                u["perms"] = json.loads(u.get("permissions") or "[]")
            except (json.JSONDecodeError, TypeError):
                u["perms"] = []
        return await render_template("users.html", users=user_list, current_user_id=session.get("user_id"), all_permissions=ALL_PERMISSIONS)

    @app.route("/member-directory")
    @permission_required("member_directory")
    async def member_directory():
        import math

        guild = get_guild()
        raw_members, complete = await get_all_guild_members(guild)
        all_rows = [build_member_directory_row(member) for member in raw_members]
        activity_rows = await db.get_all_user_activity()
        add_member_directory_activity(all_rows, activity_rows)
        chart_data = build_member_history_charts([], None)
        if guild:
            await db.backfill_discord_member_joins(
                guild.id,
                [
                    (member.id, member.joined_at.timestamp())
                    for member in raw_members
                    if member.joined_at is not None
                ],
            )
            member_events = await db.get_discord_member_events(guild.id)
            activity_by_user = {
                int(activity["user_id"]): activity
                for activity in activity_rows
                if activity.get("user_id") is not None
            }
            current_member_by_id = {row["id"]: row for row in all_rows}
            for event in member_events:
                current_member = current_member_by_id.get(event["user_id"], {})
                if not event.get("user_name"):
                    event["user_name"] = current_member.get("username", "")
                if not event.get("display_name"):
                    event["display_name"] = current_member.get("display_name", "")
                if event["event_type"] != "leave":
                    continue
                identity_was_missing = (
                    not event.get("user_name") or not event.get("display_name")
                )
                cached_user = bot.get_user(event["user_id"]) if bot else None
                activity = activity_by_user.get(event["user_id"], {})
                if (
                    bot
                    and cached_user is None
                    and not event.get("user_name")
                    and not activity.get("user_name")
                ):
                    try:
                        cached_user = await bot.fetch_user(event["user_id"])
                    except Exception:
                        cached_user = None
                if not event.get("user_name"):
                    event["user_name"] = (
                        getattr(cached_user, "name", "")
                        or activity.get("user_name", "")
                    )
                if not event.get("display_name"):
                    event["display_name"] = (
                        getattr(cached_user, "display_name", "")
                        or activity.get("user_name", "")
                    )
                if identity_was_missing and (
                    event.get("user_name") or event.get("display_name")
                ):
                    await db.update_discord_member_event_identity(
                        guild.id,
                        event["user_id"],
                        event.get("user_name", ""),
                        event.get("display_name", ""),
                    )
            chart_data = build_member_history_charts(
                member_events,
                await db.get_discord_member_tracking_started_at(guild.id),
            )
        filtered_rows = filter_member_directory_rows(all_rows, request.args)

        try:
            page = max(1, int(request.args.get("page", "1")))
        except (TypeError, ValueError):
            page = 1
        per_page = 100
        page_count = max(1, math.ceil(len(filtered_rows) / per_page))
        page = min(page, page_count)
        start = (page - 1) * per_page
        rows = filtered_rows[start:start + per_page]

        roles = []
        if guild:
            roles = [
                {"id": role.id, "name": role.name, "position": role.position}
                for role in guild.roles
                if role != guild.default_role
            ]
            roles.sort(key=lambda role: (-role["position"], role["name"].casefold()))

        return await render_template(
            "member_directory.html",
            guild=guild,
            members=rows,
            roles=roles,
            cache_complete=complete,
            total_count=len(all_rows),
            human_count=sum(1 for row in all_rows if not row["is_bot"]),
            bot_count=sum(1 for row in all_rows if row["is_bot"]),
            admin_count=sum(1 for row in all_rows if row["is_admin"]),
            filtered_count=len(filtered_rows),
            chart_data=chart_data,
            page=page,
            page_count=page_count,
            query=(request.args.get("q") or "").strip(),
            kind=(request.args.get("kind") or "all").strip(),
            role_filter=(request.args.get("role") or "").strip(),
            sort=(request.args.get("sort") or "name").strip(),
        )

    @app.route("/member-directory/export.csv")
    @permission_required("member_directory")
    async def member_directory_export():
        import csv
        import io
        from datetime import datetime, timezone
        from quart import Response

        guild = get_guild()
        raw_members, _ = await get_all_guild_members(guild)
        all_rows = [build_member_directory_row(member) for member in raw_members]
        add_member_directory_activity(all_rows, await db.get_all_user_activity())
        rows = filter_member_directory_rows(all_rows, request.args)

        def format_dt(value):
            return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if value else ""

        def csv_safe(value):
            text = str(value or "")
            if text.startswith(("=", "+", "-", "@", "\t", "\r")):
                return "'" + text
            return text

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "Display Name", "Username", "Global Name", "Nickname", "Discord User ID",
            "Account Type", "Administrator", "Pending Membership", "Joined Server (UTC)",
            "Account Created (UTC)", "Boosting Since (UTC)", "Timed Out Until (UTC)",
            "Last Interaction (UTC)", "Days Since Last Interaction",
            "Last Interaction Channel", "Top Role", "Roles",
        ])
        for row in rows:
            writer.writerow([
                csv_safe(row["display_name"]), csv_safe(row["username"]),
                csv_safe(row["global_name"]), csv_safe(row["nickname"]),
                row["id"], "Bot" if row["is_bot"] else "Human",
                "Yes" if row["is_admin"] else "No",
                "Yes" if row["pending"] else "No",
                format_dt(row["joined_at"]), format_dt(row["created_at"]),
                format_dt(row["premium_since"]), format_dt(row["timeout_until"]),
                format_dt(
                    row["last_activity"]["datetime"]
                    if row["last_activity"] else None
                ),
                row["last_activity"]["days_ago"] if row["last_activity"] else "",
                csv_safe(
                    row["last_activity"].get("channel_name", "")
                    if row["last_activity"] else ""
                ),
                csv_safe(row["top_role"]), csv_safe("; ".join(row["role_names"])),
            ])
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        await db.add_audit_log(
            event_type="member_directory_exported",
            details=f"Exported {len(rows)} Discord member record(s)",
            actor=session.get("username", "unknown"),
        )
        return Response(
            "\ufeff" + output.getvalue(),
            content_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition":
                    f"attachment; filename=discord_members_{timestamp}.csv"
            },
        )

    # --- File sharing ---

    FILE_SHARE_MAX_BYTES = 200 * 1024 * 1024
    FILE_SHARE_DEFAULT_LIMIT_GB = 5

    def format_file_share_size(size: int) -> str:
        value = float(size or 0)
        for unit in ("B", "KB", "MB", "GB"):
            if value < 1024 or unit == "GB":
                precision = 0 if unit == "B" else 1
                return f"{value:.{precision}f} {unit}"
            value /= 1024
        return f"{value:.1f} GB"

    def file_share_storage_usage() -> int:
        total = 0
        try:
            with os.scandir(file_share_dir) as entries:
                for entry in entries:
                    if entry.is_file(follow_symlinks=False) and not entry.name.endswith(".part"):
                        total += entry.stat(follow_symlinks=False).st_size
        except OSError:
            pass
        return total

    async def get_file_share_limit_gb() -> int:
        raw = await db.get_setting("file_sharing_storage_limit_gb")
        try:
            value = int(raw or FILE_SHARE_DEFAULT_LIMIT_GB)
        except (TypeError, ValueError):
            value = FILE_SHARE_DEFAULT_LIMIT_GB
        return max(1, min(10, value))

    @app.errorhandler(413)
    async def upload_too_large(_error):
        if request.path == "/file-share":
            enabled = await db.get_setting("file_sharing_enabled") == "1"
            return await render_template(
                "file_share_upload.html",
                closed=not enabled,
                error=(
                    "The selected file exceeds the 200 MB upload limit."
                    if enabled else None
                ),
            ), 413
        if request.path == "/only-grapes-admin":
            await flash("The selected video exceeds the 300 MB upload limit.", "error")
            return redirect(url_for("only_grapes_admin"))
        return "Upload exceeds the permitted size limit.", 413

    @app.route("/file-share", methods=["GET", "POST"])
    async def file_share_upload():
        enabled = await db.get_setting("file_sharing_enabled") == "1"
        if not enabled:
            return await render_template("file_share_upload.html", closed=True)

        if request.method == "POST":
            form = await request.form
            if (form.get("website") or "").strip():
                return redirect(url_for("file_share_upload"))

            files = await request.files
            upload = files.get("shared_file")
            if not upload or not upload.filename:
                await flash("Choose a file to upload.", "error")
                return redirect(url_for("file_share_upload"))

            # A generated storage name prevents path traversal and keeps
            # uploaded files unreachable except through the protected route.
            import uuid
            original_name = os.path.basename(
                upload.filename.replace("\\", "/")
            )
            original_name = "".join(
                char for char in original_name
                if ord(char) >= 32 and ord(char) != 127
            ).strip()
            if not original_name or original_name in {".", ".."}:
                await flash("The file name is invalid.", "error")
                return redirect(url_for("file_share_upload"))
            original_name = original_name[:240]
            stored_name = uuid.uuid4().hex
            temporary_path = os.path.join(file_share_dir, stored_name + ".part")
            final_path = os.path.join(file_share_dir, stored_name)
            try:
                await upload.save(temporary_path)
                size_bytes = os.path.getsize(temporary_path)
                if size_bytes <= 0:
                    raise ValueError("Empty files cannot be uploaded.")
                if size_bytes > FILE_SHARE_MAX_BYTES:
                    raise ValueError("The selected file exceeds the 200 MB upload limit.")
                forwarded = request.headers.get("X-Forwarded-For", "")
                uploader_ip = (
                    forwarded.split(",", 1)[0].strip()
                    or request.remote_addr
                    or "unknown"
                )
                async with app.file_share_lock:
                    if await db.get_setting("file_sharing_enabled") != "1":
                        raise ValueError(
                            "File sharing was disabled while the upload was running."
                        )
                    storage_limit = await get_file_share_limit_gb() * 1024 ** 3
                    if file_share_storage_usage() + size_bytes > storage_limit:
                        raise ValueError(
                            "The shared storage limit has been reached. "
                            "Please contact the administrator."
                        )

                    os.replace(temporary_path, final_path)
                    try:
                        await db.add_file_share_upload(
                            original_filename=original_name,
                            stored_filename=stored_name,
                            size_bytes=size_bytes,
                            mime_type=upload.mimetype or "application/octet-stream",
                            uploader_ip=uploader_ip,
                        )
                    except Exception:
                        if os.path.isfile(final_path):
                            os.remove(final_path)
                        raise
            except ValueError as exc:
                if os.path.isfile(temporary_path):
                    os.remove(temporary_path)
                await flash(str(exc), "error")
                return redirect(url_for("file_share_upload"))
            except Exception as exc:
                if os.path.isfile(temporary_path):
                    os.remove(temporary_path)
                print(f"[file-sharing] Upload failed: {exc}", flush=True)
                await flash("The file could not be stored. Please try again.", "error")
                return redirect(url_for("file_share_upload"))

            await flash(
                f"{original_name} was uploaded successfully.", "success"
            )
            return redirect(url_for("file_share_upload"))

        return await render_template("file_share_upload.html", closed=False)

    @app.route("/file-sharing", methods=["GET", "POST"])
    @permission_required("file_sharing")
    async def file_sharing_admin():
        if request.method == "POST":
            form = await request.form
            if form.get("action") == "set_enabled":
                enabled = form.get("enabled") == "1"
                async with app.file_share_lock:
                    await db.set_setting(
                        "file_sharing_enabled", "1" if enabled else "0"
                    )
                await db.add_audit_log(
                    event_type="file_sharing_toggled",
                    details=f"File sharing {'enabled' if enabled else 'disabled'}",
                    actor=session.get("username", "unknown"),
                )
                await flash(
                    f"File sharing {'enabled' if enabled else 'disabled'}.",
                    "success",
                )
            elif form.get("action") == "set_storage_limit":
                try:
                    storage_limit_gb = int(form.get("storage_limit_gb", "5"))
                except (TypeError, ValueError):
                    storage_limit_gb = FILE_SHARE_DEFAULT_LIMIT_GB
                storage_limit_gb = max(1, min(10, storage_limit_gb))
                async with app.file_share_lock:
                    await db.set_setting(
                        "file_sharing_storage_limit_gb", str(storage_limit_gb)
                    )
                await db.add_audit_log(
                    event_type="file_sharing_storage_limit_changed",
                    details=f"File sharing storage limit set to {storage_limit_gb} GB",
                    actor=session.get("username", "unknown"),
                )
                await flash(
                    f"Storage limit set to {storage_limit_gb} GB.", "success"
                )
            return redirect(url_for("file_sharing_admin"))

        uploads = await db.get_file_share_uploads()
        from datetime import datetime, timezone
        for upload in uploads:
            path = os.path.join(
                file_share_dir, os.path.basename(upload["stored_filename"])
            )
            upload["file_exists"] = os.path.isfile(path)
            upload["size_label"] = format_file_share_size(upload["size_bytes"])
            upload["uploaded_at_label"] = datetime.fromtimestamp(
                upload["uploaded_at"], tz=timezone.utc
            ).strftime("%d.%m.%Y %H:%M UTC")
        storage_limit_gb = await get_file_share_limit_gb()
        storage_limit_bytes = storage_limit_gb * 1024 ** 3
        total_size = file_share_storage_usage()
        storage_remaining = max(0, storage_limit_bytes - total_size)
        storage_percent = min(
            100, round((total_size / storage_limit_bytes) * 100, 1)
        )
        return await render_template(
            "file_sharing.html",
            enabled=await db.get_setting("file_sharing_enabled") == "1",
            uploads=uploads,
            total_size_label=format_file_share_size(total_size),
            storage_remaining_label=format_file_share_size(storage_remaining),
            storage_limit_gb=storage_limit_gb,
            storage_percent=storage_percent,
            public_upload_url=f"{_public_web_url()}/file-share",
        )

    @app.route("/file-sharing/<int:upload_id>/download")
    @permission_required("file_sharing")
    async def file_sharing_download(upload_id: int):
        from quart import Response, abort
        from urllib.parse import quote

        upload = await db.get_file_share_upload(upload_id)
        if not upload:
            abort(404)
        stored_name = os.path.basename(upload["stored_filename"])
        path = os.path.join(file_share_dir, stored_name)
        if stored_name != upload["stored_filename"] or not os.path.isfile(path):
            abort(404)
        await db.add_audit_log(
            event_type="file_share_downloaded",
            details=f"Downloaded file #{upload_id}: {upload['original_filename']}",
            actor=session.get("username", "unknown"),
        )

        file_size = os.path.getsize(path)
        start = 0
        end = file_size - 1
        status = 200
        range_header = (request.headers.get("Range") or "").strip()
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header)
            if not match or not any(match.groups()):
                return Response(
                    status=416,
                    headers={"Content-Range": f"bytes */{file_size}"},
                )
            start_text, end_text = match.groups()
            try:
                if not start_text:
                    suffix_length = int(end_text)
                    if suffix_length <= 0:
                        raise ValueError
                    start = max(0, file_size - suffix_length)
                else:
                    start = int(start_text)
                    if start >= file_size:
                        raise ValueError
                    if end_text:
                        end = min(int(end_text), file_size - 1)
                        if end < start:
                            raise ValueError
                status = 206
            except ValueError:
                return Response(
                    status=416,
                    headers={"Content-Range": f"bytes */{file_size}"},
                )

        content_length = max(0, end - start + 1)
        original_name = str(upload["original_filename"] or "download")
        ascii_name = re.sub(r"[^A-Za-z0-9._ -]+", "_", original_name)
        ascii_name = ascii_name.strip(" .") or "download"
        disposition = (
            f'attachment; filename="{ascii_name}"; '
            f"filename*=UTF-8''{quote(original_name, safe='')}"
        )

        async def stream_file():
            remaining = content_length
            with open(path, "rb") as handle:
                handle.seek(start)
                while remaining > 0:
                    chunk = await asyncio.to_thread(
                        handle.read,
                        min(1024 * 1024, remaining),
                    )
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        headers = {
            "Accept-Ranges": "bytes",
            "Content-Disposition": disposition,
            "Content-Length": str(content_length),
            "Cache-Control": "private, no-store",
        }
        if status == 206:
            headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
        return Response(
            stream_file(),
            status=status,
            mimetype="application/octet-stream",
            headers=headers,
        )

    @app.route("/file-sharing/<int:upload_id>/delete", methods=["POST"])
    @permission_required("file_sharing")
    async def file_sharing_delete(upload_id: int):
        upload = await db.get_file_share_upload(upload_id)
        if not upload:
            await flash("The upload no longer exists.", "error")
            return redirect(url_for("file_sharing_admin"))

        stored_name = os.path.basename(upload["stored_filename"])
        path = os.path.join(file_share_dir, stored_name)
        if stored_name == upload["stored_filename"] and os.path.isfile(path):
            os.remove(path)
        await db.delete_file_share_upload(upload_id)
        await db.add_audit_log(
            event_type="file_share_deleted",
            details=f"Deleted file #{upload_id}: {upload['original_filename']}",
            actor=session.get("username", "unknown"),
        )
        await flash(f"{upload['original_filename']} was deleted.", "success")
        return redirect(url_for("file_sharing_admin"))

    # --- Only Grapes ---

    ONLY_GRAPES_MAX_VIDEO_BYTES = 300 * 1024 * 1024
    ONLY_GRAPES_DEFAULTS = {
        "only_grapes_hero_eyebrow": "THE GRAPE EXPERIENCE",
        "only_grapes_hero_title": "Premium",
        "only_grapes_hero_accent": "Vineyard Content",
        "only_grapes_hero_intro": (
            "Original vineyard stories, fresh video drops and curious moments "
            "from behind the barrel."
        ),
        "only_grapes_about_title": "Rooted in imagination",
        "only_grapes_about_body": (
            "Only Grapes is a playful digital vineyard for cinematic stories, "
            "music and experiments grown with artificial intelligence."
        ),
        "only_grapes_membership_title": "Free membership",
        "only_grapes_membership_body": (
            "Membership is currently free. Create an account to enter the "
            "vineyard and watch every published drop."
        ),
        "only_grapes_shop_title": "The cellar shop",
        "only_grapes_shop_body": (
            "The shop is resting in the cellar for now. Future releases and "
            "membership options will appear here."
        ),
        "only_grapes_contact_title": "Contact the vineyard",
        "only_grapes_contact_body": (
            "Questions, ideas or collaborations? Use the community channels "
            "connected to this site to get in touch."
        ),
        "only_grapes_ai_notice": (
            "Transparency notice: Videos and audio on this site are "
            "AI-generated or AI-assisted content."
        ),
    }

    async def _only_grapes_enabled() -> bool:
        return await db.get_setting("only_grapes_enabled") == "1"

    async def _only_grapes_content() -> dict[str, str]:
        content = {}
        for key, default in ONLY_GRAPES_DEFAULTS.items():
            content[key] = await db.get_setting(key, default)
        hero_name = os.path.basename(
            await db.get_setting("only_grapes_hero_filename") or ""
        )
        hero_path = os.path.join(only_grapes_asset_dir, hero_name)
        content["hero_url"] = (
            url_for("only_grapes_asset", filename=hero_name)
            if hero_name and os.path.isfile(hero_path)
            else url_for("static", filename="only_grapes_hero.png")
        )
        return content

    def _only_grapes_logged_in() -> bool:
        return bool(session.get("only_grapes_user_id"))

    async def _only_grapes_current_user() -> dict | None:
        user_id = session.get("only_grapes_user_id")
        if not user_id:
            return None
        user = await db.get_only_grapes_user(int(user_id))
        if not user or not user.get("active"):
            session.pop("only_grapes_user_id", None)
            session.pop("only_grapes_display_name", None)
            return None
        return user

    async def _only_grapes_video_duration(path: str) -> float:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries",
            "format=format_name,duration:stream=codec_type",
            "-of", "json", path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await proc.communicate()
        if proc.returncode != 0:
            return 0.0
        try:
            metadata = json.loads(stdout.decode())
            container = str((metadata.get("format") or {}).get("format_name") or "")
            has_video = any(
                stream.get("codec_type") == "video"
                for stream in (metadata.get("streams") or [])
            )
            if "mp4" not in container.split(",") or not has_video:
                return 0.0
            return max(0.0, float((metadata.get("format") or {}).get("duration") or 0))
        except (TypeError, ValueError, json.JSONDecodeError):
            return 0.0

    def _only_grapes_valid_image(path: str) -> bool:
        if not os.path.isfile(path) or not 0 < os.path.getsize(path) <= 15 * 1024 * 1024:
            return False
        with open(path, "rb") as image_file:
            header = image_file.read(16)
        return bool(
            header.startswith(b"\x89PNG\r\n\x1a\n")
            or header.startswith(b"\xff\xd8\xff")
            or (header.startswith(b"RIFF") and header[8:12] == b"WEBP")
        )

    async def _only_grapes_create_poster(video_path: str, poster_path: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-v", "error", "-ss", "0.25", "-i", video_path,
            "-frames:v", "1", "-vf",
            "scale=720:720:force_original_aspect_ratio=increase,"
            "crop=720:720",
            poster_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

    async def _only_grapes_faststart(source_path: str, output_path: str) -> None:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-v", "error", "-i", source_path,
            "-map", "0", "-c", "copy", "-movflags", "+faststart", output_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await proc.communicate()
        if proc.returncode != 0 or not os.path.isfile(output_path):
            detail = stderr.decode(errors="replace").strip()
            raise ValueError(
                "The MP4 could not be prepared for web playback."
                + (f" {detail[-300:]}" if detail else "")
            )

    async def _only_grapes_encode_for_web(
        source_path: str, output_path: str
    ) -> None:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-v", "error", "-i", source_path,
            "-map", "0:v:0", "-map", "0:a:0?",
            "-vf",
            "scale='min(1080,iw)':'min(1080,ih)':"
            "force_original_aspect_ratio=decrease:force_divisible_by=2",
            "-c:v", "libx264", "-preset", "medium", "-crf", "23",
            "-maxrate", "4M", "-bufsize", "8M", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
            output_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await proc.communicate()
        if proc.returncode != 0 or not os.path.isfile(output_path):
            detail = stderr.decode(errors="replace").strip()
            raise ValueError(
                "The video could not be encoded for web playback."
                + (f" {detail[-300:]}" if detail else "")
            )

        probe = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,pix_fmt:format=duration",
            "-of", "json", output_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await probe.communicate()
        try:
            metadata = json.loads(stdout.decode())
            stream = (metadata.get("streams") or [{}])[0]
            duration = float((metadata.get("format") or {}).get("duration") or 0)
        except (IndexError, TypeError, ValueError, json.JSONDecodeError):
            stream, duration = {}, 0
        if (
            probe.returncode != 0
            or stream.get("codec_name") != "h264"
            or stream.get("pix_fmt") != "yuv420p"
            or duration <= 0
        ):
            raise ValueError("The optimized MP4 failed the browser compatibility check.")

    @app.route("/only-grapes/assets/<filename>")
    async def only_grapes_asset(filename):
        from quart import abort, send_from_directory
        safe_name = os.path.basename(filename)
        if safe_name != filename:
            abort(404)
        return await send_from_directory(only_grapes_asset_dir, safe_name)

    @app.route("/only-grapes", endpoint="only_grapes_home")
    async def only_grapes_home():
        enabled = await _only_grapes_enabled()
        return await render_template(
            "only_grapes.html",
            enabled=enabled,
            page="home",
            content=await _only_grapes_content(),
            grape_user=await _only_grapes_current_user(),
        ), (200 if enabled else 503)

    @app.route("/only-grapes/about")
    async def only_grapes_about():
        enabled = await _only_grapes_enabled()
        return await render_template(
            "only_grapes.html", enabled=enabled, page="about",
            content=await _only_grapes_content(),
            grape_user=await _only_grapes_current_user(),
        ), (200 if enabled else 503)

    @app.route("/only-grapes/membership")
    async def only_grapes_membership():
        enabled = await _only_grapes_enabled()
        return await render_template(
            "only_grapes.html", enabled=enabled, page="membership",
            content=await _only_grapes_content(),
            grape_user=await _only_grapes_current_user(),
        ), (200 if enabled else 503)

    @app.route("/only-grapes/shop")
    async def only_grapes_shop():
        enabled = await _only_grapes_enabled()
        return await render_template(
            "only_grapes.html", enabled=enabled, page="shop",
            content=await _only_grapes_content(),
            grape_user=await _only_grapes_current_user(),
        ), (200 if enabled else 503)

    @app.route("/only-grapes/contact")
    async def only_grapes_contact():
        enabled = await _only_grapes_enabled()
        return await render_template(
            "only_grapes.html", enabled=enabled, page="contact",
            content=await _only_grapes_content(),
            grape_user=await _only_grapes_current_user(),
        ), (200 if enabled else 503)

    @app.route("/only-grapes/content")
    async def only_grapes_content_page():
        from datetime import datetime
        from zoneinfo import ZoneInfo
        if not await _only_grapes_enabled():
            return await render_template(
                "only_grapes.html", enabled=False, page="content",
                content=await _only_grapes_content(), grape_user=None,
            ), 503
        grape_user = await _only_grapes_current_user()
        if not grape_user:
            session["only_grapes_after_login"] = url_for("only_grapes_content_page")
            return redirect(url_for("only_grapes_login"))
        videos = await db.get_only_grapes_videos(published_only=True)
        for video in videos:
            video["cache_version"] = int(
                float(video.get("updated_at") or video.get("created_at") or 0)
            )
        comments_by_video: dict[int, list[dict]] = {}
        for comment in await db.get_only_grapes_comments():
            comment["created_label"] = datetime.fromtimestamp(
                float(comment.get("created_at") or 0),
                tz=ZoneInfo("Europe/Berlin"),
            ).strftime("%d %b %Y, %H:%M")
            comments_by_video.setdefault(int(comment["video_id"]), []).append(comment)
        return await render_template(
            "only_grapes.html", enabled=True, page="content",
            content=await _only_grapes_content(), grape_user=grape_user,
            videos=videos, comments_by_video=comments_by_video,
        )

    @app.route("/only-grapes/video/<int:video_id>/comments", methods=["POST"])
    async def only_grapes_add_comment(video_id: int):
        from quart import abort
        if not await _only_grapes_enabled():
            abort(404)
        grape_user = await _only_grapes_current_user()
        if not grape_user:
            session["only_grapes_after_login"] = (
                url_for("only_grapes_content_page") + f"#video-{video_id}"
            )
            return redirect(url_for("only_grapes_login"))
        video = await db.get_only_grapes_video(video_id)
        if not video or not video.get("published"):
            abort(404)
        form = await request.form
        body = (form.get("body") or "").strip()
        if body:
            await db.add_only_grapes_comment(
                video_id=video_id, user_id=grape_user["id"], body=body[:1000]
            )
        return redirect(url_for("only_grapes_content_page") + f"#video-{video_id}")

    @app.route("/only-grapes/comments/<int:comment_id>/delete", methods=["POST"])
    async def only_grapes_delete_comment(comment_id: int):
        from quart import abort
        if not await _only_grapes_enabled():
            abort(404)
        grape_user = await _only_grapes_current_user()
        if not grape_user:
            abort(403)
        comment = await db.get_only_grapes_comment(comment_id)
        if not comment or int(comment["user_id"]) != int(grape_user["id"]):
            abort(403)
        await db.delete_only_grapes_comment(comment_id)
        return redirect(
            url_for("only_grapes_content_page") + f"#video-{comment['video_id']}"
        )

    @app.route("/only-grapes/login", methods=["GET", "POST"])
    async def only_grapes_login():
        if not await _only_grapes_enabled():
            return redirect(url_for("only_grapes_home"))
        error = ""
        if request.method == "POST":
            form = await request.form
            user = await db.get_only_grapes_user_by_email(form.get("email", ""))
            if (
                user and user.get("active")
                and check_password(form.get("password", ""), user["password_hash"])
            ):
                session["only_grapes_user_id"] = user["id"]
                session["only_grapes_display_name"] = user["display_name"]
                await db.mark_only_grapes_login(user["id"])
                target = session.pop("only_grapes_after_login", "")
                return redirect(target or url_for("only_grapes_content_page"))
            error = "The email address or password is incorrect."
        return await render_template(
            "only_grapes_auth.html", mode="login", error=error,
            content=await _only_grapes_content(),
        )

    @app.route("/only-grapes/register", methods=["GET", "POST"])
    async def only_grapes_register():
        if not await _only_grapes_enabled():
            return redirect(url_for("only_grapes_home"))
        error = ""
        if request.method == "POST":
            form = await request.form
            email = (form.get("email") or "").strip().lower()
            display_name = (form.get("display_name") or "").strip()
            password = form.get("password") or ""
            if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
                error = "Enter a valid email address."
            elif not 2 <= len(display_name) <= 60:
                error = "Your display name must contain 2 to 60 characters."
            elif len(password) < 8:
                error = "Use a password with at least 8 characters."
            else:
                created = await db.create_only_grapes_user(
                    email=email, display_name=display_name,
                    password_hash=hash_password(password),
                )
                if created:
                    user = await db.get_only_grapes_user_by_email(email)
                    session["only_grapes_user_id"] = user["id"]
                    session["only_grapes_display_name"] = user["display_name"]
                    await db.mark_only_grapes_login(user["id"])
                    return redirect(url_for("only_grapes_content_page"))
                error = "An account with this email address already exists."
        return await render_template(
            "only_grapes_auth.html", mode="register", error=error,
            content=await _only_grapes_content(),
        )

    @app.route("/only-grapes/logout", methods=["POST"])
    async def only_grapes_logout():
        session.pop("only_grapes_user_id", None)
        session.pop("only_grapes_display_name", None)
        return redirect(url_for("only_grapes_home"))

    @app.route("/only-grapes/video/<int:video_id>")
    async def only_grapes_video(video_id: int):
        from quart import abort, send_file
        if not await _only_grapes_enabled() or not await _only_grapes_current_user():
            abort(404)
        video = await db.get_only_grapes_video(video_id)
        if not video or not video.get("published"):
            abort(404)
        filename = os.path.basename(video["stored_filename"])
        path = os.path.join(only_grapes_video_dir, filename)
        if filename != video["stored_filename"] or not os.path.isfile(path):
            abort(404)
        response = await send_file(path, mimetype="video/mp4", conditional=True)
        response.headers["Cache-Control"] = "private, no-cache, max-age=0"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.route("/only-grapes/video/<int:video_id>/played", methods=["POST"])
    async def only_grapes_video_played(video_id: int):
        from quart import abort
        if not await _only_grapes_enabled() or not await _only_grapes_current_user():
            abort(404)
        video = await db.get_only_grapes_video(video_id)
        if not video or not video.get("published"):
            abort(404)
        await db.increment_only_grapes_video_play_count(video_id)
        return "", 204

    @app.route("/only-grapes/poster/<int:video_id>")
    async def only_grapes_poster(video_id: int):
        from quart import abort, send_file
        if not await _only_grapes_enabled() or not await _only_grapes_current_user():
            abort(404)
        video = await db.get_only_grapes_video(video_id)
        if not video or not video.get("published"):
            abort(404)
        stem = os.path.splitext(os.path.basename(video["stored_filename"]))[0]
        path = os.path.join(only_grapes_video_dir, stem + ".square.jpg")
        if not os.path.isfile(path):
            video_path = os.path.join(
                only_grapes_video_dir, os.path.basename(video["stored_filename"])
            )
            if not os.path.isfile(video_path):
                abort(404)
            await _only_grapes_create_poster(video_path, path)
            if not os.path.isfile(path):
                abort(404)
        return await send_file(path, mimetype="image/jpeg", conditional=True)

    @app.route("/only-grapes-admin", methods=["GET", "POST"])
    @permission_required("only_grapes")
    async def only_grapes_admin():
        from datetime import datetime
        from zoneinfo import ZoneInfo
        import uuid
        if request.method == "POST":
            form = await request.form
            actions = form.getlist("action")
            action = actions[-1] if actions else ""
            if action == "save_content":
                await db.set_setting(
                    "only_grapes_enabled", "1" if form.get("enabled") else "0"
                )
                for key, default in ONLY_GRAPES_DEFAULTS.items():
                    value = (form.get(key) or "").strip() or default
                    await db.set_setting(key, value[:4000])
                await db.add_audit_log(
                    event_type="only_grapes_settings_updated",
                    details="Updated website state and page content",
                    actor=session.get("username", "unknown"),
                )
                await flash("Only Grapes settings saved.", "success")
            elif action == "upload_hero":
                files = await request.files
                upload = files.get("hero_image")
                extension = os.path.splitext(upload.filename or "")[1].lower() if upload else ""
                if not upload or extension not in {".jpg", ".jpeg", ".png", ".webp"}:
                    await flash("Choose a JPG, PNG or WebP hero image.", "error")
                else:
                    filename = f"hero-{uuid.uuid4().hex}{extension}"
                    path = os.path.join(only_grapes_asset_dir, filename)
                    await upload.save(path)
                    if not _only_grapes_valid_image(path):
                        os.remove(path)
                        await flash(
                            "The hero must be a valid JPG, PNG or WebP up to 15 MB.",
                            "error",
                        )
                        return redirect(url_for("only_grapes_admin"))
                    old_name = os.path.basename(
                        await db.get_setting("only_grapes_hero_filename") or ""
                    )
                    await db.set_setting("only_grapes_hero_filename", filename)
                    if old_name and old_name != filename:
                        old_path = os.path.join(only_grapes_asset_dir, old_name)
                        if os.path.isfile(old_path):
                            os.remove(old_path)
                    await flash("Hero image updated.", "success")
            elif action == "reset_hero":
                old_name = os.path.basename(
                    await db.get_setting("only_grapes_hero_filename") or ""
                )
                await db.set_setting("only_grapes_hero_filename", "")
                old_path = os.path.join(only_grapes_asset_dir, old_name)
                if old_name and os.path.isfile(old_path):
                    os.remove(old_path)
                await flash("The default hero image was restored.", "success")
            elif action == "upload_video":
                files = await request.files
                upload = files.get("video_file")
                title = (form.get("title") or "").strip()
                description = (form.get("description") or "").strip()
                original_name = os.path.basename(upload.filename or "") if upload else ""
                if not title:
                    await flash("A video title is required.", "error")
                elif not upload or os.path.splitext(original_name)[1].lower() != ".mp4":
                    await flash("Choose an MP4 video.", "error")
                else:
                    stored_name = f"{uuid.uuid4().hex}.mp4"
                    temp_path = os.path.join(only_grapes_video_dir, stored_name + ".part")
                    optimized_path = os.path.join(
                        only_grapes_video_dir, stored_name + ".optimized.mp4"
                    )
                    final_path = os.path.join(only_grapes_video_dir, stored_name)
                    try:
                        await upload.save(temp_path)
                        size = os.path.getsize(temp_path)
                        if size <= 0 or size > ONLY_GRAPES_MAX_VIDEO_BYTES:
                            raise ValueError("Videos must be between 1 byte and 300 MB.")
                        duration = await _only_grapes_video_duration(temp_path)
                        if duration <= 0:
                            raise ValueError("The uploaded file is not a playable MP4 video.")
                        await _only_grapes_faststart(temp_path, optimized_path)
                        os.replace(optimized_path, final_path)
                        os.remove(temp_path)
                        size = os.path.getsize(final_path)
                        poster_path = os.path.join(
                            only_grapes_video_dir,
                            os.path.splitext(stored_name)[0] + ".square.jpg",
                        )
                        await _only_grapes_create_poster(final_path, poster_path)
                        try:
                            await db.add_only_grapes_video(
                                title=title[:200], description=description[:2000],
                                original_filename=original_name[:240],
                                stored_filename=stored_name, size_bytes=size,
                                duration_seconds=duration,
                                published=form.get("published") == "1",
                            )
                        except Exception:
                            os.remove(final_path)
                            if os.path.isfile(poster_path):
                                os.remove(poster_path)
                            raise
                        await db.add_audit_log(
                            event_type="only_grapes_video_uploaded",
                            details=f"Uploaded Only Grapes video: {title[:200]}",
                            actor=session.get("username", "unknown"),
                        )
                        await flash("Video uploaded successfully.", "success")
                    except ValueError as exc:
                        if os.path.isfile(temp_path):
                            os.remove(temp_path)
                        if os.path.isfile(optimized_path):
                            os.remove(optimized_path)
                        await flash(str(exc), "error")
                    except Exception as exc:
                        if os.path.isfile(temp_path):
                            os.remove(temp_path)
                        if os.path.isfile(optimized_path):
                            os.remove(optimized_path)
                        print(f"[only-grapes] Video upload failed: {exc}", flush=True)
                        await flash("The video could not be stored.", "error")
            elif action == "update_video":
                video_id = int(form.get("video_id") or 0)
                await db.update_only_grapes_video(
                    video_id, title=(form.get("title") or "Untitled")[:200],
                    description=(form.get("description") or "")[:2000],
                    published=form.get("published") == "1",
                )
                await flash("Video updated.", "success")
            elif action == "optimize_video":
                video_id = int(form.get("video_id") or 0)
                video = await db.get_only_grapes_video(video_id)
                if not video:
                    await flash("Video not found.", "error")
                else:
                    filename = os.path.basename(video["stored_filename"])
                    path = os.path.join(only_grapes_video_dir, filename)
                    optimized_path = path + ".optimized.mp4"
                    try:
                        if filename != video["stored_filename"] or not os.path.isfile(path):
                            raise ValueError("The stored video file is missing.")
                        await _only_grapes_encode_for_web(path, optimized_path)
                        os.replace(optimized_path, path)
                        await db.update_only_grapes_video_size(
                            video_id, os.path.getsize(path)
                        )
                        await flash(
                            "Video encoded and optimized for smoother web playback.",
                            "success",
                        )
                    except ValueError as exc:
                        if os.path.isfile(optimized_path):
                            os.remove(optimized_path)
                        await flash(str(exc), "error")
                    except Exception as exc:
                        if os.path.isfile(optimized_path):
                            os.remove(optimized_path)
                        print(
                            f"[only-grapes] Playback optimization failed: {exc}",
                            flush=True,
                        )
                        await flash("The video could not be optimized.", "error")
            elif action == "delete_video":
                video_id = int(form.get("video_id") or 0)
                video = await db.get_only_grapes_video(video_id)
                if video:
                    filename = os.path.basename(video["stored_filename"])
                    path = os.path.join(only_grapes_video_dir, filename)
                    if filename == video["stored_filename"] and os.path.isfile(path):
                        os.remove(path)
                    poster_path = os.path.join(
                        only_grapes_video_dir,
                        os.path.splitext(filename)[0] + ".square.jpg",
                    )
                    if os.path.isfile(poster_path):
                        os.remove(poster_path)
                    legacy_poster_path = os.path.join(
                        only_grapes_video_dir,
                        os.path.splitext(filename)[0] + ".jpg",
                    )
                    if os.path.isfile(legacy_poster_path):
                        os.remove(legacy_poster_path)
                    await db.delete_only_grapes_video(video_id)
                    await db.add_audit_log(
                        event_type="only_grapes_video_deleted",
                        details=f"Deleted Only Grapes video: {video.get('title', video_id)}",
                        actor=session.get("username", "unknown"),
                    )
                    await flash("Video permanently deleted.", "success")
            elif action == "delete_comment":
                comment_id = int(form.get("comment_id") or 0)
                comment = await db.get_only_grapes_comment(comment_id)
                if comment:
                    await db.delete_only_grapes_comment(comment_id)
                    await db.add_audit_log(
                        event_type="only_grapes_comment_deleted",
                        details=f"Deleted Only Grapes comment #{comment_id}",
                        actor=session.get("username", "unknown"),
                    )
                    await flash("Comment deleted.", "success")
            return redirect(url_for("only_grapes_admin"))

        videos = await db.get_only_grapes_videos()
        for video in videos:
            video["size_label"] = format_file_share_size(video["size_bytes"])
            seconds = int(video.get("duration_seconds") or 0)
            video["duration_label"] = f"{seconds // 60}:{seconds % 60:02d}"
        comments = await db.get_only_grapes_comments()
        for comment in comments:
            comment["created_label"] = datetime.fromtimestamp(
                float(comment.get("created_at") or 0),
                tz=ZoneInfo("Europe/Berlin"),
            ).strftime("%d.%m.%Y %H:%M")
        return await render_template(
            "only_grapes_admin.html",
            enabled=await _only_grapes_enabled(),
            content=await _only_grapes_content(), videos=videos, comments=comments,
            public_url=f"{_public_web_url()}/only-grapes",
        )

    @app.route("/executioner", methods=["GET", "POST"])
    @permission_required('executioner')
    async def executioner():
        guild = get_guild()
        selected_user_id = request.args.get("user_id", type=int)

        if request.method == "POST":
            form = await request.form
            raw_user_id = (form.get("user_id") or "").strip()
            reason = (form.get("reason") or "").strip()

            if not raw_user_id.isdigit():
                await flash("Please select a user.", "error")
                return redirect(url_for("executioner"))
            if not reason:
                await flash("A reason is required.", "error")
                return redirect(url_for("executioner", user_id=raw_user_id))
            if not guild:
                await flash("The bot is not connected to the configured server.", "error")
                return redirect(url_for("executioner", user_id=raw_user_id))

            user_id = int(raw_user_id)
            member = guild.get_member(user_id)
            if not member:
                try:
                    member = await guild.fetch_member(user_id)
                except Exception:
                    member = None
            if not member:
                await flash("The selected user is not a current server member.", "error")
                return redirect(url_for("executioner"))

            bot_member = guild.me
            if not bot_member or not bot_member.guild_permissions.kick_members:
                await flash("The bot does not have permission to kick members.", "error")
                return redirect(url_for("executioner", user_id=user_id))
            if member == guild.owner:
                await flash("The server owner cannot be kicked.", "error")
                return redirect(url_for("executioner", user_id=user_id))
            if member.top_role >= bot_member.top_role:
                await flash("The bot cannot kick this user because of the role hierarchy.", "error")
                return redirect(url_for("executioner", user_id=user_id))

            actor = session.get("username", "unknown")
            audit_reason = f"{reason} (requested by {actor} via Admin UI)"
            try:
                await member.kick(reason=audit_reason[:512])
            except Exception as exc:
                await flash(f"Kick failed: {exc}", "error")
                return redirect(url_for("executioner", user_id=user_id))

            await db.add_audit_log(
                event_type="user_kicked",
                user_id=user_id,
                user_name=str(member),
                details=f"Reason: {reason}",
                actor=actor,
            )
            await flash(f"{member} was kicked.", "success")
            return redirect(url_for("executioner"))

        members = await get_guild_members(guild)
        latest_activity = None
        if selected_user_id:
            latest_activity = await db.get_latest_user_activity(selected_user_id)
            cached_message = find_latest_cached_message(bot, selected_user_id) if bot else None
            if cached_message and (
                not latest_activity
                or cached_message["timestamp"] > float(latest_activity.get("timestamp") or 0)
            ):
                latest_activity = cached_message

        return await render_template(
            "executioner.html",
            members=members,
            selected_user_id=selected_user_id,
            latest_activity=latest_activity,
            bot_connected=bool(guild),
        )

    @app.route("/submission-bans", methods=["GET", "POST"])
    @permission_required('submission_bans')
    async def submission_bans():
        guild = get_guild()
        members = await get_guild_members(guild)
        member_by_id = {member.id: member for member in members}
        actor = session.get("username", "unknown")

        if request.method == "POST":
            form = await request.form
            action = (form.get("action") or "").strip()
            raw_user_id = (form.get("user_id") or "").strip()
            if not raw_user_id.isdigit():
                await flash("Please select a Discord user.", "error")
                return redirect(url_for("submission_bans"))
            user_id = int(raw_user_id)

            if action == "set_ban":
                member = member_by_id.get(user_id)
                if not member:
                    await flash("The selected Discord user could not be found.", "error")
                    return redirect(url_for("submission_bans"))
                try:
                    stream_count = int(form.get("streams_remaining") or "1")
                except (TypeError, ValueError):
                    stream_count = 0
                if not 1 <= stream_count <= 100:
                    await flash("The number of streams must be between 1 and 100.", "error")
                    return redirect(url_for("submission_bans"))
                await db.set_exp_radio_submission_ban(
                    user_id=member.id,
                    user_name=member.name,
                    display_name=member.display_name,
                    streams_remaining=stream_count,
                    created_by=actor,
                )
                await db.add_audit_log(
                    event_type="exp_radio_submission_ban_set",
                    user_id=member.id,
                    user_name=str(member),
                    details=f"Exp. Radio and TrYa Stream submissions blocked for {stream_count} stream start(s)",
                    actor=actor,
                )
                await flash(
                    f"{member.display_name} is blocked for the next {stream_count} stream(s).",
                    "success",
                )

            elif action == "update_ban":
                ban = await db.get_exp_radio_submission_ban(user_id)
                if not ban:
                    await flash("This submission ban no longer exists.", "error")
                    return redirect(url_for("submission_bans"))
                try:
                    stream_count = int(form.get("streams_remaining") or "1")
                except (TypeError, ValueError):
                    stream_count = 0
                if not 1 <= stream_count <= 100:
                    await flash("The number of streams must be between 1 and 100.", "error")
                    return redirect(url_for("submission_bans"))
                member = member_by_id.get(user_id)
                await db.set_exp_radio_submission_ban(
                    user_id=user_id,
                    user_name=member.name if member else ban["user_name"],
                    display_name=member.display_name if member else ban["display_name"],
                    streams_remaining=stream_count,
                    created_by=actor,
                )
                await db.add_audit_log(
                    event_type="exp_radio_submission_ban_updated",
                    user_id=user_id,
                    user_name=ban["user_name"],
                    details=f"Remaining blocked streams set to {stream_count}",
                    actor=actor,
                )
                await flash("Submission ban updated.", "success")

            elif action == "remove_ban":
                removed = await db.remove_exp_radio_submission_ban(user_id)
                if removed:
                    await db.add_audit_log(
                        event_type="exp_radio_submission_ban_removed",
                        user_id=user_id,
                        user_name=removed["user_name"],
                        details="Shared Exp. Radio and TrYa Stream submission ban removed manually",
                        actor=actor,
                    )
                    await flash("Submission ban removed.", "success")
                else:
                    await flash("This submission ban no longer exists.", "error")
            else:
                await flash("Unknown action.", "error")
            return redirect(url_for("submission_bans"))

        bans = await db.get_exp_radio_submission_bans()
        for ban in bans:
            member = member_by_id.get(int(ban["user_id"]))
            ban["avatar_url"] = str(member.display_avatar.url) if member else ""
            if member:
                ban["user_name"] = member.name
                ban["display_name"] = member.display_name

        member_options = [
            {
                "id": member.id,
                "user_name": member.name,
                "display_name": member.display_name,
            }
            for member in members
        ]
        return await render_template(
            "submission_bans.html",
            members=member_options,
            bans=bans,
            bot_connected=bool(guild),
        )

    @app.route("/audit")
    @permission_required('audit')
    async def audit():
        page = int(request.args.get("page", 1))
        per_page = 50
        offset = (page - 1) * per_page
        logs = await db.get_audit_logs(limit=per_page, offset=offset)
        total = await db.get_audit_log_count()
        total_pages = max(1, (total + per_page - 1) // per_page)

        return await render_template(
            "audit.html",
            logs=logs,
            page=page,
            total_pages=total_pages,
            total=total,
        )

    # --- Channel Moderation ---

    @app.route("/channel-moderation", methods=["GET", "POST"])
    @permission_required('channel_moderation')
    async def channel_moderation():
        if request.method == "POST":
            form = await request.form
            action = form.get("action", "")
            if action == "save_settings":
                enabled = "on" if form.get("enabled") else "off"
                report_ch = (form.get("report_channel_id") or "").strip()
                await db.set_setting("channel_moderation_enabled", enabled)
                await db.set_setting("channel_moderation_report_channel", report_ch)
                await flash("Channel moderation settings saved.", "success")
            elif action == "clear_log":
                await db.db.execute("DELETE FROM channel_moderation_log")
                await db.db.commit()
                await flash("Moderation log cleared.", "success")
            return redirect(url_for("channel_moderation"))

        enabled = await db.get_setting("channel_moderation_enabled") or "off"
        report_ch_id = await db.get_setting("channel_moderation_report_channel") or ""
        verdict_filter = (request.args.get("verdict") or "").strip().lower()
        if verdict_filter not in ("flagged", "passed", "pending", "skipped", "error"):
            verdict_filter = ""
        log_rows = await db.get_channel_moderation_log(
            limit=200, verdict=verdict_filter or None,
        )
        from datetime import datetime as _dt
        for r in log_rows:
            try:
                r["created_pretty"] = _dt.fromtimestamp(float(r["created_at"])).strftime("%d.%m. %H:%M")
            except Exception:
                r["created_pretty"] = ""

        # Channel pickers: monitored channels (source list, read-only display)
        # + all text channels for the report-channel dropdown.
        monitored = await db.get_monitored_channels()
        guild = get_guild()
        text_channels = []
        monitored_resolved = []
        if guild:
            import discord as _discord
            for ch in guild.channels:
                if isinstance(ch, _discord.TextChannel):
                    text_channels.append({"id": ch.id, "name": ch.name})
            text_channels.sort(key=lambda c: c["name"].lower())
            for m in monitored:
                ch = guild.get_channel(m["channel_id"])
                monitored_resolved.append({
                    "id": m["channel_id"],
                    "name": ch.name if ch else m.get("channel_name") or str(m["channel_id"]),
                    "enabled": bool(m.get("enabled")),
                })

        return await render_template(
            "channel_moderation.html",
            enabled=enabled,
            report_ch_id=report_ch_id,
            log_rows=log_rows,
            verdict_filter=verdict_filter,
            text_channels=text_channels,
            monitored=monitored_resolved,
        )

    # --- Quiz ---

    @app.route("/quiz", methods=["GET", "POST"])
    @permission_required('quiz')
    async def quiz_admin():
        def _category_key(value):
            value = re.sub(r"[^a-z0-9]+", "_", (value or "").strip().lower())
            return value.strip("_")

        categories = await db.get_quiz_categories()
        category_keys = {category["key"] for category in categories}

        def _clean_quiz_form(form):
            mode = (form.get("mode") or "").strip().lower()
            question = (form.get("question") or "").strip()
            answers = [
                (form.get(f"answer_{idx}") or "").strip()
                for idx in range(1, 6)
            ]
            correct_answer = (form.get("correct_answer") or "").strip()
            return mode, question, answers, correct_answer

        if request.method == "POST":
            form = await request.form
            action = form.get("action", "")

            if action == "add_category":
                name = (form.get("category_name") or "").strip()
                key = _category_key(form.get("category_key") or name)
                if not name or not key:
                    await flash("Category name is required.", "error")
                elif key == "mixed":
                    await flash("'mixed' is reserved for all categories.", "error")
                elif key in category_keys:
                    await flash("A category with this key already exists.", "error")
                else:
                    try:
                        await db.create_quiz_category(key, name)
                        await flash(f"Category '{name}' created.", "success")
                    except Exception:
                        await flash("Category name or key already exists.", "error")
                return redirect(url_for("quiz_admin"))

            if action == "delete_category":
                key = (form.get("category_key") or "").strip().lower()
                category = await db.get_quiz_category(key)
                if not category:
                    await flash("Category not found.", "error")
                elif not await db.delete_quiz_category(key):
                    await flash(
                        "This category still contains questions and cannot be deleted.",
                        "error",
                    )
                else:
                    if (await db.get_setting("quiz_mode") or "") == key:
                        await db.set_setting("quiz_mode", "mixed")
                    await flash(f"Category '{category['name']}' deleted.", "success")
                return redirect(url_for("quiz_admin"))

            if action == "save_settings":
                mode = (form.get("quiz_mode") or "mixed").strip().lower()
                if mode != "mixed" and mode not in category_keys:
                    mode = "mixed"
                channel_id = (form.get("quiz_channel_id") or "").strip()
                await db.set_setting("quiz_mode", mode)
                await db.set_setting("quiz_channel_id", channel_id)
                await flash("Quiz settings saved.", "success")
                return redirect(url_for("quiz_admin"))

            if action in ("create", "edit"):
                mode, question, answers, correct_answer = _clean_quiz_form(form)
                if mode not in category_keys:
                    await flash("Please select a valid quiz category.", "error")
                    return redirect(url_for("quiz_admin"))
                if not question:
                    await flash("Question is required.", "error")
                    return redirect(url_for("quiz_admin"))
                if any(not answer for answer in answers):
                    await flash("All five answers are required.", "error")
                    return redirect(url_for("quiz_admin"))
                if not correct_answer:
                    await flash("Correct answer is required.", "error")
                    return redirect(url_for("quiz_admin"))
                if correct_answer not in answers:
                    await flash("Correct answer must match one of the five answers exactly.", "error")
                    return redirect(url_for("quiz_admin"))

                if action == "create":
                    question_id = await db.create_quiz_question(mode, question, answers, correct_answer)
                    await db.add_audit_log(
                        event_type="quiz_question_created",
                        details=f"Quiz question #{question_id} created for mode={mode}",
                        actor=session.get("username", "unknown"),
                    )
                    await flash("Quiz question created.", "success")
                else:
                    question_id = int(form.get("question_id") or 0)
                    await db.update_quiz_question(question_id, mode, question, answers, correct_answer)
                    await db.add_audit_log(
                        event_type="quiz_question_updated",
                        details=f"Quiz question #{question_id} updated for mode={mode}",
                        actor=session.get("username", "unknown"),
                    )
                    await flash("Quiz question updated.", "success")
                return redirect(url_for("quiz_admin"))

            if action == "import_json":
                import json as _json
                raw_json = (form.get("quiz_json") or "").strip()
                if not raw_json:
                    await flash("JSON input is required.", "error")
                    return redirect(url_for("quiz_admin"))

                try:
                    payload = _json.loads(raw_json)
                except _json.JSONDecodeError as exc:
                    await flash(f"Invalid JSON: {exc.msg} at line {exc.lineno}.", "error")
                    return redirect(url_for("quiz_admin"))

                items = payload.get("questions") if isinstance(payload, dict) else payload
                if not isinstance(items, list):
                    await flash("JSON must be an array or an object with a questions array.", "error")
                    return redirect(url_for("quiz_admin"))

                prepared = []
                errors = []
                for idx, item in enumerate(items, start=1):
                    if not isinstance(item, dict):
                        errors.append(f"Item {idx}: expected an object.")
                        continue
                    mode = str(
                        item.get("category") or item.get("mode") or ""
                    ).strip().lower()
                    if mode not in category_keys:
                        errors.append(
                            f"Item {idx}: unknown category '{mode or '(empty)'}'."
                        )
                        continue
                    question = str(item.get("question") or "").strip()
                    answers = item.get("answers")
                    if not question:
                        errors.append(f"Item {idx}: question is required.")
                        continue
                    if not isinstance(answers, list) or len(answers) != 5:
                        errors.append(f"Item {idx}: answers must contain exactly 5 entries.")
                        continue
                    answers = [str(answer).strip() for answer in answers]
                    if any(not answer for answer in answers):
                        errors.append(f"Item {idx}: all answers must be non-empty.")
                        continue

                    correct_answer = str(
                        item.get("correct_answer")
                        or item.get("correctAnswer")
                        or ""
                    ).strip()
                    correct_index = item.get("correct_index", item.get("correctIndex"))
                    if not correct_answer and correct_index is not None:
                        try:
                            correct_idx = int(correct_index)
                            if 1 <= correct_idx <= 5:
                                correct_answer = answers[correct_idx - 1]
                        except (TypeError, ValueError):
                            pass
                    if not correct_answer:
                        errors.append(f"Item {idx}: correct_answer or correct_index is required.")
                        continue
                    if correct_answer not in answers:
                        errors.append(f"Item {idx}: correct answer must match one of the answers exactly.")
                        continue
                    prepared.append((mode, question, answers, correct_answer))

                if errors:
                    preview = " ".join(errors[:5])
                    if len(errors) > 5:
                        preview += f" ... and {len(errors) - 5} more."
                    await flash(preview, "error")
                    return redirect(url_for("quiz_admin"))

                if not prepared:
                    await flash("No quiz questions found in the JSON input.", "error")
                    return redirect(url_for("quiz_admin"))

                imported_count = await db.create_quiz_questions_bulk(prepared)

                await db.add_audit_log(
                    event_type="quiz_questions_imported",
                    details=f"Imported {imported_count} quiz question(s) from JSON",
                    actor=session.get("username", "unknown"),
                )
                await flash(f"Imported {imported_count} quiz question(s).", "success")
                return redirect(url_for("quiz_admin"))

            if action == "delete":
                question_id = int(form.get("question_id") or 0)
                await db.delete_quiz_question(question_id)
                await db.add_audit_log(
                    event_type="quiz_question_deleted",
                    details=f"Quiz question #{question_id} deleted",
                    actor=session.get("username", "unknown"),
                )
                await flash("Quiz question deleted.", "success")
                return redirect(url_for("quiz_admin"))

        categories = await db.get_quiz_categories()
        category_keys = {category["key"] for category in categories}
        category_names = {
            category["key"]: category["name"] for category in categories
        }
        quiz_mode = await db.get_setting("quiz_mode") or "mixed"
        if quiz_mode != "mixed" and quiz_mode not in category_keys:
            quiz_mode = "mixed"
        quiz_channel_id = await db.get_setting("quiz_channel_id") or ""
        questions = await db.get_quiz_questions()
        for question in questions:
            question["answers"] = [
                question["answer_1"],
                question["answer_2"],
                question["answer_3"],
                question["answer_4"],
                question["answer_5"],
            ]
            question["category_name"] = category_names.get(
                question["mode"], question["mode"]
            )

        guild = get_guild()
        text_channels = []
        if guild:
            for ch in sorted(guild.text_channels, key=lambda c: c.position):
                text_channels.append({"id": ch.id, "name": ch.name})

        return await render_template(
            "quiz.html",
            quiz_mode=quiz_mode,
            quiz_channel_id=quiz_channel_id,
            questions=questions,
            categories=categories,
            text_channels=text_channels,
        )

    # --- LLM / Corax chat ---

    @app.route("/llm", methods=["GET", "POST"])
    @permission_required('llm')
    async def llm():
        import json as _json
        from bot.llm import DEFAULT_PERSONA, AVAILABLE_TOOL_NAMES

        if request.method == "POST":
            form = await request.form
            action = form.get("action", "save")

            if action == "send_manual_dm":
                try:
                    target_user_id = int(form.get("target_user_id") or 0)
                except (TypeError, ValueError):
                    target_user_id = 0
                content = (form.get("message") or "").strip()
                guild = get_guild()
                member = guild.get_member(target_user_id) if guild and target_user_id else None
                if guild and target_user_id and member is None:
                    try:
                        member = await guild.fetch_member(target_user_id)
                    except Exception:
                        member = None
                if member is None or getattr(member, "bot", False):
                    await flash("Select a valid server member.", "error")
                elif not content:
                    await flash("Enter a message to send.", "error")
                else:
                    content = content[:1900]
                    try:
                        sent = await member.send(content)
                    except Exception as exc:
                        print(
                            f"[corax-dm] Send to {target_user_id} failed: "
                            f"{type(exc).__name__}: {exc}",
                            flush=True,
                        )
                        await flash(
                            "Corax could not deliver the DM. The member may have "
                            "disabled direct messages from this server.",
                            "error",
                        )
                    else:
                        actor = session.get("username", "unknown")
                        await db.add_corax_dm_message(
                            user_id=member.id,
                            user_name=str(member),
                            direction="outbound",
                            content=content,
                            timestamp=sent.created_at.timestamp(),
                            discord_message_id=sent.id,
                            admin_actor=actor,
                        )
                        await db.add_audit_log(
                            event_type="corax_manual_dm_sent",
                            user_id=member.id,
                            user_name=str(member),
                            details=f"Manual Corax DM ({len(content)} characters)",
                            actor=actor,
                        )
                        await flash(f"Message sent to {member.display_name} as Corax.", "success")
                return redirect(url_for("llm", user_id=target_user_id) + "#manual-chat")

            if action == "purge_audit":
                cfg = await db.get_llm_config()
                days = int((cfg or {}).get("retention_days") or 30)
                deleted = await db.purge_llm_audit_log(days)
                await flash(f"{deleted} old chat and audit entries purged.", "success")
                return redirect(url_for("llm"))

            if action == "reset_persona":
                await db.update_llm_config(persona=DEFAULT_PERSONA)
                await flash("Persona reset to the default prompt.", "success")
                return redirect(url_for("llm"))

            # save main config
            def _i(key, default):
                try:
                    return int(form.get(key, default))
                except Exception:
                    return default

            tools_selected = [
                name for name in AVAILABLE_TOOL_NAMES
                if form.get(f"tool_{name}")
            ]
            await db.update_llm_config(
                enabled=1 if form.get("enabled") else 0,
                model=(form.get("model") or "qwen2.5:7b-instruct").strip()[:64],
                tools_model=(form.get("tools_model") or "").strip()[:64],
                persona=(form.get("persona") or "").strip()[:16000],
                retention_days=max(1, min(365, _i("retention_days", 30))),
                rate_per_user_min=max(1, min(60, _i("rate_per_user_min", 3))),
                rate_per_channel_min=max(1, min(500, _i("rate_per_channel_min", 10))),
                max_tokens=max(64, min(2048, _i("max_tokens", 512))),
                default_result_limit=max(1, min(25, _i("default_result_limit", 10))),
                tools_enabled=_json.dumps(tools_selected),
            )

            # channels
            channel_ids = form.getlist("channels")
            guild = get_guild()
            ch_entries = []
            for cid in channel_ids:
                try:
                    cid_i = int(cid)
                except Exception:
                    continue
                cname = f"channel-{cid_i}"
                if guild:
                    ch = guild.get_channel(cid_i)
                    if ch:
                        cname = ch.name
                ch_entries.append((cid_i, cname))
            await db.set_llm_allowed_channels(ch_entries)

            # roles
            role_ids = form.getlist("roles")
            role_entries = []
            for rid in role_ids:
                try:
                    rid_i = int(rid)
                except Exception:
                    continue
                rname = f"role-{rid_i}"
                if guild:
                    r = guild.get_role(rid_i)
                    if r:
                        rname = r.name
                role_entries.append((rid_i, rname))
            await db.set_llm_allowed_roles(role_entries)

            await db.add_audit_log(
                event_type="llm_config_updated",
                actor=session.get("username", "unknown"),
                details=(
                    f"enabled={bool(form.get('enabled'))}, "
                    f"channels={len(ch_entries)}, roles={len(role_entries)}, "
                    f"tools={tools_selected}"
                ),
            )
            await flash("Corax settings saved.", "success")
            return redirect(url_for("llm"))

        cfg = await db.get_llm_config()
        try:
            tools_enabled = set(_json.loads(cfg.get("tools_enabled") or "[]"))
        except Exception:
            tools_enabled = set()
        allowed_channels = await db.get_llm_allowed_channels()
        allowed_role_ids = await db.get_llm_allowed_role_ids()
        allowed_channel_ids = {c["channel_id"] for c in allowed_channels}

        guild = get_guild()
        guild_channels = []
        guild_roles = []
        guild_members = []
        if guild:
            for ch in sorted(guild.text_channels, key=lambda c: c.name.lower()):
                guild_channels.append({"id": ch.id, "name": ch.name})
            for r in sorted(guild.roles, key=lambda r: r.name.lower()):
                if r.is_default():
                    continue
                guild_roles.append({"id": r.id, "name": r.name})
            all_members, _members_complete = await get_all_guild_members(guild)
            selectable_members = sorted(
                (member for member in all_members if not getattr(member, "bot", False)),
                key=lambda member: (
                    (member.display_name or member.name or "").casefold(),
                    member.id,
                ),
            )
            for member in selectable_members:
                guild_members.append({
                    "id": member.id,
                    "display_name": member.display_name or member.name,
                    "username": member.name,
                    "avatar_url": str(member.display_avatar.url),
                })

        # fall back to stored names for any channels/roles not visible in guild
        known_ids = {c["id"] for c in guild_channels}
        for c in allowed_channels:
            if c["channel_id"] not in known_ids:
                guild_channels.append({"id": c["channel_id"], "name": c.get("channel_name") or f"channel-{c['channel_id']}"})
        known_role_ids = {r["id"] for r in guild_roles}
        for r in await db.get_llm_allowed_roles():
            if r["role_id"] not in known_role_ids:
                guild_roles.append({"id": r["role_id"], "name": r.get("role_name") or f"role-{r['role_id']}"})

        audit_log = await db.get_llm_audit_log(limit=100)
        dm_contacts = await db.get_corax_dm_contacts(limit=100)
        dm_contact_times = {
            row["user_id"]: float(row["last_message_at"] or 0)
            for row in dm_contacts
        }
        guild_members.sort(key=lambda row: (
            0 if row["id"] in dm_contact_times else 1,
            -dm_contact_times.get(row["id"], 0),
            row["display_name"].casefold(),
        ))
        selected_dm_user_id = request.args.get("user_id", type=int)
        if selected_dm_user_id not in {row["id"] for row in guild_members}:
            selected_dm_user_id = None
        selected_dm_user = next(
            (row for row in guild_members if row["id"] == selected_dm_user_id),
            None,
        )
        dm_messages = (
            await db.get_corax_dm_messages(selected_dm_user_id, limit=150)
            if selected_dm_user_id else []
        )

        return await render_template(
            "llm.html",
            cfg=cfg,
            default_persona=DEFAULT_PERSONA,
            tools_all=sorted(AVAILABLE_TOOL_NAMES),
            tools_enabled=tools_enabled,
            guild_channels=guild_channels,
            guild_roles=guild_roles,
            allowed_channel_ids=allowed_channel_ids,
            allowed_role_ids=allowed_role_ids,
            audit_log=audit_log,
            guild_members=guild_members,
            selected_dm_user=selected_dm_user,
            dm_messages=dm_messages,
        )

    @app.route("/llm/manual-dm/<int:user_id>/messages")
    @permission_required('llm')
    async def llm_manual_dm_messages(user_id: int):
        from quart import jsonify

        guild = get_guild()
        member = guild.get_member(user_id) if guild else None
        if guild and member is None:
            try:
                member = await guild.fetch_member(user_id)
            except Exception:
                member = None
        if member is None or getattr(member, "bot", False):
            return jsonify({"error": "Server member not found"}), 404
        messages = await db.get_corax_dm_messages(user_id, limit=150)
        return jsonify({
            "user_id": str(user_id),
            "messages": [
                {
                    "id": row["id"],
                    "timestamp": row["timestamp"],
                    "direction": row["direction"],
                    "content": row["content"],
                    "admin_actor": row.get("admin_actor") or "",
                }
                for row in messages
            ],
        })

    async def _get_player_channels():
        guild = get_guild()
        channels = []
        monitored = await db.get_monitored_channels()
        for ch in monitored:
            ch_name = f"channel-{ch['channel_id']}"
            if guild:
                gch = guild.get_channel(ch["channel_id"])
                if gch:
                    ch_name = gch.name
            channels.append({"id": ch["channel_id"], "name": ch_name})
        return channels

    @app.route("/player")
    @permission_required('player')
    async def player():
        connection = await db.get_player_discord_connection(session["user_id"])
        client_id, client_secret = await _player_discord_oauth_credentials()
        return await render_template(
            "player.html",
            channels=await _get_player_channels(),
            discord_connection=connection,
            discord_oauth_ready=bool(client_id and client_secret),
        )

    @app.route("/player/discord/connect")
    @login_required
    async def player_discord_connect():
        client_id, client_secret = await _player_discord_oauth_credentials()
        if not client_id or not client_secret:
            await flash(
                "Discord connection is not configured yet. Add the OAuth2 credentials in Settings.",
                "error",
            )
            return redirect(url_for("player"))

        oauth_return = request.args.get("return", "")
        if oauth_return == "suno_info":
            session["player_discord_oauth_return"] = "suno_info"
        else:
            session.pop("player_discord_oauth_return", None)
        state = secrets.token_urlsafe(32)
        session["player_discord_oauth_state"] = state
        query = urlencode({
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": _player_discord_callback_url(),
            "scope": "identify",
            "state": state,
            "prompt": "consent",
        })
        return redirect(f"https://discord.com/oauth2/authorize?{query}")

    @app.route("/player/discord/callback")
    @login_required
    async def player_discord_callback():
        return_endpoint = session.pop("player_discord_oauth_return", "player")
        return_target = url_for(
            "suno_info" if return_endpoint == "suno_info" else "player"
        )
        expected_state = session.pop("player_discord_oauth_state", "")
        returned_state = request.args.get("state", "")
        if not expected_state or not secrets.compare_digest(expected_state, returned_state):
            await flash("Discord connection failed: invalid OAuth2 state.", "error")
            return redirect(return_target)
        if request.args.get("error"):
            await flash("Discord connection was cancelled.", "error")
            return redirect(return_target)

        code = request.args.get("code", "")
        client_id, client_secret = await _player_discord_oauth_credentials()
        if not code or not client_id or not client_secret:
            await flash("Discord connection failed: incomplete OAuth2 response.", "error")
            return redirect(return_target)

        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as http:
                async with http.post(
                    "https://discord.com/api/v10/oauth2/token",
                    data={
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": _player_discord_callback_url(),
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                ) as token_response:
                    token_body = await token_response.text()
                    try:
                        import json
                        token_data = json.loads(token_body)
                    except (TypeError, ValueError):
                        token_data = {}
                    if token_response.status != 200:
                        error_code = token_data.get("error") or "unknown_error"
                        error_description = (
                            token_data.get("error_description")
                            or token_data.get("message")
                            or token_body[:200]
                            or "Discord returned an empty response"
                        )
                        raise RuntimeError(
                            f"token exchange HTTP {token_response.status}: "
                            f"{error_code}: {error_description}"
                        )

                access_token = token_data.get("access_token", "")
                async with http.get(
                    "https://discord.com/api/v10/users/@me",
                    headers={"Authorization": f"Bearer {access_token}"},
                ) as user_response:
                    user_data = await user_response.json(content_type=None)
                    if user_response.status != 200:
                        raise RuntimeError(user_data.get("message") or "Discord user lookup failed")
        except Exception as exc:
            print(f"[player-oauth] Discord connection failed: {exc}", flush=True)
            await flash(
                "Discord rejected the connection. Check the application client secret "
                "and OAuth2 redirect URL, then try again.",
                "error",
            )
            return redirect(return_target)

        discord_user_id = int(user_data["id"])
        guild = get_guild()
        member = guild.get_member(discord_user_id) if guild else None
        if guild and member is None:
            try:
                member = await guild.fetch_member(discord_user_id)
            except Exception:
                member = None
        if member is None:
            await flash(
                "Discord connection failed: your account is not a member of this server.",
                "error",
            )
            return redirect(return_target)

        display_name = re.sub(
            r"\s+",
            " ",
            member.display_name or user_data.get("global_name") or user_data["username"],
        ).strip()
        avatar_url = str(member.display_avatar.url) if member.display_avatar else ""
        linked = await db.link_player_discord_account(
            web_user_id=session["user_id"],
            discord_user_id=discord_user_id,
            discord_username=user_data["username"],
            discord_display_name=display_name,
            discord_avatar=avatar_url,
        )
        if not linked:
            await flash(
                "This Discord account is already connected to another web user.",
                "error",
            )
            return redirect(return_target)

        await db.add_audit_log(
            event_type="player_discord_connected",
            user_id=discord_user_id,
            user_name=display_name,
            details=f"Linked to web user {session.get('username', 'unknown')}",
            actor=session.get("username", "unknown"),
        )
        await flash(f"Discord connected as {display_name}.", "success")
        return redirect(return_target)

    @app.route("/player/discord/disconnect", methods=["POST"])
    @login_required
    async def player_discord_disconnect():
        form = await request.form
        return_endpoint = form.get("return", "")
        connection = await db.get_player_discord_connection(session["user_id"])
        await db.unlink_player_discord_account(session["user_id"])
        if connection:
            await db.add_audit_log(
                event_type="player_discord_disconnected",
                user_id=connection["discord_user_id"],
                user_name=connection["discord_display_name"],
                actor=session.get("username", "unknown"),
            )
        await flash("Discord account disconnected from the Player.", "success")
        return redirect(url_for("suno_info" if return_endpoint == "suno_info" else "player"))

    # --- Public player (no login required) ---
    @app.route("/public/player")
    async def player_public():
        client_id, client_secret = await _player_discord_oauth_credentials()
        return await render_template(
            "player_public.html",
            channels=await _get_player_channels(),
            discord_connection=session.get("public_player_discord"),
            discord_oauth_ready=bool(client_id and client_secret),
        )

    @app.route("/public/player/discord/connect")
    async def public_player_discord_connect():
        client_id, client_secret = await _player_discord_oauth_credentials()
        if not client_id or not client_secret:
            await flash("Discord connection is not configured yet.", "error")
            return redirect(url_for("player_public"))

        state = secrets.token_urlsafe(32)
        session["public_player_discord_oauth_state"] = state
        query = urlencode({
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": _public_player_discord_callback_url(),
            "scope": "identify",
            "state": state,
            "prompt": "consent",
        })
        return redirect(f"https://discord.com/oauth2/authorize?{query}")

    @app.route("/public/player/discord/callback")
    async def public_player_discord_callback():
        expected_state = session.pop("public_player_discord_oauth_state", "")
        returned_state = request.args.get("state", "")
        if not expected_state or not secrets.compare_digest(expected_state, returned_state):
            await flash("Discord connection failed: invalid OAuth2 state.", "error")
            return redirect(url_for("player_public"))
        if request.args.get("error"):
            await flash("Discord connection was cancelled.", "error")
            return redirect(url_for("player_public"))

        code = request.args.get("code", "")
        client_id, client_secret = await _player_discord_oauth_credentials()
        if not code or not client_id or not client_secret:
            await flash("Discord connection failed: incomplete OAuth2 response.", "error")
            return redirect(url_for("player_public"))

        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as http:
                async with http.post(
                    "https://discord.com/api/v10/oauth2/token",
                    data={
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": _public_player_discord_callback_url(),
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                ) as token_response:
                    token_body = await token_response.text()
                    try:
                        import json
                        token_data = json.loads(token_body)
                    except (TypeError, ValueError):
                        token_data = {}
                    if token_response.status != 200:
                        error_code = token_data.get("error") or "unknown_error"
                        error_description = (
                            token_data.get("error_description")
                            or token_data.get("message")
                            or token_body[:200]
                            or "Discord returned an empty response"
                        )
                        raise RuntimeError(
                            f"token exchange HTTP {token_response.status}: "
                            f"{error_code}: {error_description}"
                        )

                access_token = token_data.get("access_token", "")
                async with http.get(
                    "https://discord.com/api/v10/users/@me",
                    headers={"Authorization": f"Bearer {access_token}"},
                ) as user_response:
                    user_data = await user_response.json(content_type=None)
                    if user_response.status != 200:
                        raise RuntimeError(
                            user_data.get("message") or "Discord user lookup failed"
                        )
        except Exception as exc:
            print(f"[public-player-oauth] Discord connection failed: {exc}", flush=True)
            await flash(
                "Discord rejected the connection. Please try again.",
                "error",
            )
            return redirect(url_for("player_public"))

        discord_user_id = int(user_data["id"])
        guild = get_guild()
        member = guild.get_member(discord_user_id) if guild else None
        if guild and member is None:
            try:
                member = await guild.fetch_member(discord_user_id)
            except Exception:
                member = None
        if member is None:
            await flash(
                "Discord connection failed: your account is not a member of this server.",
                "error",
            )
            return redirect(url_for("player_public"))

        display_name = re.sub(
            r"\s+",
            " ",
            member.display_name or user_data.get("global_name") or user_data["username"],
        ).strip()
        avatar_url = str(member.display_avatar.url) if member.display_avatar else ""
        session["public_player_discord"] = {
            "discord_user_id": str(discord_user_id),
            "discord_username": user_data["username"],
            "discord_display_name": display_name,
            "discord_avatar": avatar_url,
        }
        await db.add_audit_log(
            event_type="public_player_discord_connected",
            user_id=discord_user_id,
            user_name=display_name,
            details="Connected through the public Suno Player",
            actor="public-player",
        )
        await flash(f"Discord connected as {display_name}.", "success")
        return redirect(url_for("player_public"))

    @app.route("/public/player/discord/disconnect", methods=["POST"])
    async def public_player_discord_disconnect():
        connection = session.pop("public_player_discord", None)
        if connection:
            await db.add_audit_log(
                event_type="public_player_discord_disconnected",
                user_id=int(connection["discord_user_id"]),
                user_name=connection["discord_display_name"],
                actor="public-player",
            )
        await flash("Discord account disconnected from the Player.", "success")
        return redirect(url_for("player_public"))

    @app.route("/public/api/player-songs")
    async def api_player_songs_public():
        from quart import jsonify
        channel_id = request.args.get("channel_id", "").strip()
        limit = min(int(request.args.get("limit", "200")), 500)
        try:
            offset = max(0, int(request.args.get("offset", "0")))
        except (TypeError, ValueError):
            offset = 0
        ch_id = int(channel_id) if channel_id.isdigit() else None
        songs = await db.get_player_songs(channel_id=ch_id, limit=limit, offset=offset)
        return jsonify(songs)

    async def _resolve_suno_short_url(short_id: str):
        """Resolve a Suno short ID to a full song UUID. Returns uuid str or None."""
        import aiohttp as _aiohttp, re as _re
        print(f"[suno-resolve] Resolving short_id={short_id}", flush=True)
        try:
            async with _aiohttp.ClientSession() as sess:
                async with sess.get(
                    f"https://suno.com/s/{short_id}",
                    allow_redirects=True,
                    timeout=_aiohttp.ClientTimeout(total=10),
                ) as resp:
                    final_url = str(resp.url)
                    print(f"[suno-resolve] HTTP {resp.status} final_url={final_url}", flush=True)
                    # Direct song redirect
                    m = _re.search(r'/song/([a-f0-9-]{36})', final_url)
                    if m:
                        print(f"[suno-resolve] Resolved via URL song match: {m.group(1)}", flush=True)
                        return m.group(1)
                    html = await resp.text()
                    print(f"[suno-resolve] HTML length={len(html)}, snippet={html[:200]!r}", flush=True)
                    # Hook redirect — Suno hooks are client-side rendered;
                    # the parent song UUID is NOT in the initial HTML.
                    # We can only succeed if Suno ever embeds a /song/ link.
                    hook_m = _re.search(r'/hook/([a-f0-9-]{36})', final_url)
                    if hook_m:
                        hook_uuid = hook_m.group(1)
                        print(f"[suno-resolve] Hook detected: {hook_uuid} — searching for /song/ link", flush=True)
                        parent_m = _re.search(r'/song/([a-f0-9-]{36})', html)
                        if parent_m:
                            print(f"[suno-resolve] Parent song via /song/ in HTML: {parent_m.group(1)}", flush=True)
                            return parent_m.group(1)
                        print(f"[suno-resolve] Hook: no /song/ link found — cannot resolve", flush=True)
                        return None
                    # CDN audio URL in HTML
                    m = _re.search(r'cdn[12]\.suno\.ai/([a-f0-9-]{36})\.mp3', html)
                    if m:
                        print(f"[suno-resolve] Resolved via CDN URL in HTML: {m.group(1)}", flush=True)
                        return m.group(1)
                    m = _re.search(r'"audio_url"\s*:\s*"[^"]*?([a-f0-9-]{36})\.mp3"', html)
                    if m:
                        print(f"[suno-resolve] Resolved via audio_url in HTML: {m.group(1)}", flush=True)
                        return m.group(1)
                    print(f"[suno-resolve] Could not resolve {short_id} — no patterns matched", flush=True)
        except Exception as e:
            print(f"[suno-resolve] Exception for {short_id}: {e}", flush=True)
        return None

    async def _resolve_suno_hook_uuid(hook_uuid: str):
        """Fetch a Suno hook page and return the parent song UUID, or None."""
        import aiohttp as _aiohttp, re as _re
        print(f"[suno-hook] Resolving hook_uuid={hook_uuid}", flush=True)
        try:
            headers = {"User-Agent": "Mozilla/5.0 (compatible; Bot/1.0)"}
            async with _aiohttp.ClientSession(headers=headers) as sess:
                async with sess.get(
                    f"https://suno.com/hook/{hook_uuid}",
                    allow_redirects=True,
                    timeout=_aiohttp.ClientTimeout(total=10),
                ) as resp:
                    print(f"[suno-hook] HTTP {resp.status} for {hook_uuid}", flush=True)
                    if resp.status != 200:
                        return None
                    html = await resp.text()
                    print(f"[suno-hook] HTML length={len(html)} — searching for /song/ link", flush=True)
                    m = _re.search(r'/song/([a-f0-9-]{36})', html)
                    if m:
                        print(f"[suno-hook] Found parent via /song/: {m.group(1)}", flush=True)
                        return m.group(1)
                    print(f"[suno-hook] No /song/ link in hook HTML — cannot resolve {hook_uuid}", flush=True)
        except Exception as e:
            print(f"[suno-hook] Exception for {hook_uuid}: {e}", flush=True)
        return None

    @app.route("/public/api/suno-resolve/<short_id>")
    async def api_suno_resolve_public(short_id):
        from quart import jsonify
        uuid = await _resolve_suno_short_url(short_id)
        if uuid:
            return jsonify({"uuid": uuid})
        return jsonify({"uuid": None}), 404

    @app.route("/public/api/suno-hook/<hook_uuid>")
    async def api_suno_hook_resolve_public(hook_uuid):
        from quart import jsonify
        uuid = await _resolve_suno_hook_uuid(hook_uuid)
        if uuid:
            return jsonify({"uuid": uuid})
        return jsonify({"uuid": None}), 404

    @app.route("/public/api/suno-lyrics/<uuid>")
    async def api_suno_lyrics_public(uuid):
        from quart import jsonify
        return jsonify(await _fetch_suno_meta(uuid))

    @app.route("/public/api/verify-songs", methods=["POST"])
    async def api_verify_songs_public():
        """Public mirror — verify messages using DB-stored channel_id (never client-provided)."""
        from quart import jsonify
        import discord as _discord
        data = await request.get_json(silent=True) or {}
        items = data.get("items") or []
        # Hard cap batch size to limit DoS potential
        if len(items) > 100:
            items = items[:100]
        if not bot or not bot.is_ready():
            return jsonify({"missing": [], "ok": False, "reason": "bot_not_ready"})
        guild = get_guild()
        if not guild:
            return jsonify({"missing": [], "ok": False, "reason": "no_guild"})

        async def check(item):
            try:
                mid = int(item.get("message_id"))
            except (TypeError, ValueError):
                return None
            # Look up channel_id from DB — never trust client input for deletion decisions
            row = await db.get_song_post_by_message_id(mid)
            if not row:
                return None  # unknown to us, don't mark as missing
            cid = row.get("channel_id")
            if cid is None:
                return None
            ch = guild.get_channel(int(cid))
            if ch is None:
                return mid  # channel gone
            try:
                await ch.fetch_message(mid)
                return None
            except _discord.NotFound:
                return mid
            except Exception:
                return None

        sem = asyncio.Semaphore(5)
        async def bounded(i):
            async with sem:
                return await check(i)
        results = await asyncio.gather(*[bounded(i) for i in items])
        missing = [r for r in results if r is not None]
        for mid in missing:
            try:
                await db.delete_song_posts_by_message_id(mid)
            except Exception:
                pass
        return jsonify({"missing": [str(m) for m in missing], "ok": True})

    PLAYER_REACTION_EMOJIS = (
        "💜", "💯", "👏🏻", "❤️‍🔥", "🫶🏻", "🔥", "👍🏻", "🥰"
    )

    async def _remove_player_thread_notice(channel, thread, created_after: float):
        """Remove Discord's parent-channel notice for a Player reaction thread."""
        import discord

        # Discord creates the system message asynchronously, so give it a brief
        # moment to appear in the parent channel history.
        await asyncio.sleep(0.5)
        try:
            async for notice in channel.history(limit=15):
                if notice.created_at.timestamp() + 2 < created_after:
                    break
                if notice.type != discord.MessageType.thread_created:
                    continue
                if not bot.user or notice.author.id != bot.user.id:
                    continue
                linked_thread = getattr(notice, "thread", None)
                if linked_thread is not None and linked_thread.id != thread.id:
                    continue
                await notice.delete()
                return
        except (discord.Forbidden, discord.NotFound):
            pass
        except Exception as exc:
            print(f"[player-react] Could not remove thread notice: {exc}", flush=True)

    async def _update_player_reaction_summary(song_post: dict) -> tuple[bool, str | None]:
        """Create/reuse the song thread and maintain its single summary message."""
        import discord

        if not bot or not bot.is_ready():
            return False, "Discord bot is offline"
        guild = get_guild()
        if not guild:
            return False, "Discord server is unavailable"

        message_id = int(song_post["message_id"])
        channel_id = int(song_post["channel_id"])
        record = await db.get_player_reaction_thread(message_id)
        thread = None
        if record:
            thread = guild.get_thread(int(record["thread_id"]))
            if thread is None:
                try:
                    fetched = await bot.fetch_channel(int(record["thread_id"]))
                    if isinstance(fetched, discord.Thread):
                        thread = fetched
                except (discord.NotFound, discord.Forbidden):
                    thread = None

        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await bot.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden):
                return False, "Song channel is unavailable"

        try:
            starter = await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden):
            return False, "Song message is unavailable"

        if thread is None:
            thread = getattr(starter, "thread", None) or guild.get_thread(message_id)
        if thread is None:
            raw_title = (song_post.get("song_title") or "Song").strip()
            clean_title = re.sub(r"\s+", " ", raw_title)[:86]
            thread_created_at = time.time()
            try:
                thread = await starter.create_thread(
                    name=f"Reactions · {clean_title}",
                    auto_archive_duration=1440,
                    reason="Suno Player reaction summary",
                )
            except discord.HTTPException as exc:
                return False, f"Could not create reaction thread: {exc}"
            await _remove_player_thread_notice(channel, thread, thread_created_at)

        if thread.archived:
            try:
                await thread.edit(archived=False, reason="New Suno Player reaction")
            except discord.HTTPException as exc:
                return False, f"Could not reopen reaction thread: {exc}"

        reactions = await db.get_player_song_reactions(message_id)
        grouped = {emoji: [] for emoji in PLAYER_REACTION_EMOJIS}
        for reaction in reactions:
            display_name = re.sub(
                r"\s+", " ", reaction["discord_display_name"]
            ).strip()
            grouped.setdefault(reaction["emoji"], []).append(
                discord.utils.escape_markdown(display_name)
            )

        compact_parts = []
        for emoji in PLAYER_REACTION_EMOJIS:
            names = grouped.get(emoji) or []
            if names:
                more = len(names) - 1
                suffix = f" +{more} more" if more else ""
                compact_parts.append(f"{emoji} {names[0]}{suffix}")

        if compact_parts:
            lines = [f"**Player reactions** {' · '.join(compact_parts)}"]
            if any(len(names) > 1 for names in grouped.values()):
                lines.extend(["", "**All reactions**"])
                for emoji in PLAYER_REACTION_EMOJIS:
                    names = grouped.get(emoji) or []
                    if names:
                        lines.append(f"{emoji} {', '.join(names)}")
        else:
            lines = ["**Player reactions** No Player reactions yet."]
        summary = "\n".join(lines)
        if len(summary) > 1950:
            summary = summary[:1947].rstrip() + "..."

        summary_message = None
        summary_id = record.get("summary_message_id") if record else None
        if summary_id:
            try:
                summary_message = await thread.fetch_message(int(summary_id))
            except (discord.NotFound, discord.Forbidden):
                summary_message = None
        try:
            if summary_message:
                await summary_message.edit(
                    content=summary,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                summary_message = await thread.send(
                    summary,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except discord.HTTPException as exc:
            return False, f"Could not update reaction summary: {exc}"

        await db.set_player_reaction_thread(
            message_id=message_id,
            channel_id=channel_id,
            thread_id=thread.id,
            summary_message_id=summary_message.id,
        )
        try:
            await thread.edit(
                archived=True,
                reason="Player reaction summary updated",
            )
        except discord.HTTPException as exc:
            return False, f"Reaction summary was updated, but its thread could not be archived: {exc}"
        return True, None

    @app.route("/api/player-discord-status")
    @permission_required('player')
    async def api_player_discord_status():
        from quart import jsonify

        connection = await db.get_player_discord_connection(session["user_id"])
        emojis = []
        message_id = request.args.get("message_id", "")
        if connection and message_id.isdigit():
            emojis = await db.get_player_user_reactions(
                int(message_id), int(connection["discord_user_id"])
            )
        return jsonify({
            "connected": bool(connection),
            "display_name": connection["discord_display_name"] if connection else None,
            "avatar_url": connection["discord_avatar"] if connection else None,
            "emojis": emojis,
        })

    @app.route("/public/api/player-discord-status")
    async def api_public_player_discord_status():
        from quart import jsonify

        connection = session.get("public_player_discord")
        emojis = []
        message_id = request.args.get("message_id", "")
        if connection and message_id.isdigit():
            emojis = await db.get_public_player_user_reactions(
                int(message_id), int(connection["discord_user_id"])
            )
        return jsonify({
            "connected": bool(connection),
            "display_name": connection.get("discord_display_name") if connection else None,
            "avatar_url": connection.get("discord_avatar") if connection else None,
            "emojis": emojis,
        })

    @app.route("/api/player-songs")
    @permission_required('player')
    async def api_player_songs():
        from quart import jsonify
        channel_id = request.args.get("channel_id", "").strip()
        limit = min(int(request.args.get("limit", "200")), 500)
        offset = max(int(request.args.get("offset", "0")), 0)
        ch_id = int(channel_id) if channel_id.isdigit() else None
        songs = await db.get_player_songs(channel_id=ch_id, limit=limit, offset=offset)
        return jsonify(songs)

    @app.route("/api/player-react", methods=["POST"])
    @permission_required('player')
    async def api_player_react():
        from quart import jsonify
        data = await request.get_json()
        if not data:
            return jsonify({"error": "No data"}), 400
        message_id = str(data.get("message_id") or "")
        emoji = data.get("emoji", "")
        if not message_id.isdigit() or emoji not in PLAYER_REACTION_EMOJIS:
            return jsonify({"error": "Missing message_id or emoji"}), 400

        message_id_int = int(message_id)
        song_post = await db.get_song_post_by_message_id(message_id_int)
        if not song_post:
            return jsonify({"error": "Unknown song message"}), 404

        lock = app.player_reaction_locks.setdefault(message_id_int, asyncio.Lock())
        async with lock:
            connection = await db.get_player_discord_connection(session["user_id"])
            if connection:
                added = await db.toggle_player_song_reaction(
                    message_id=message_id_int,
                    channel_id=int(song_post["channel_id"]),
                    web_user_id=session["user_id"],
                    discord_user_id=int(connection["discord_user_id"]),
                    discord_display_name=connection["discord_display_name"],
                    emoji=emoji,
                )
                if added:
                    await db.add_song_reaction(
                        message_id=message_id_int,
                        channel_id=int(song_post["channel_id"]),
                        song_url=song_post["url"],
                        post_author_id=int(song_post["user_id"]),
                        reactor_user_id=int(connection["discord_user_id"]),
                        reactor_user_name=connection["discord_display_name"],
                        emoji=emoji,
                        song_title=song_post.get("song_title"),
                        source="player",
                    )
                elif not await db.has_player_song_reaction(
                    message_id_int, int(connection["discord_user_id"]), emoji
                ):
                    await db.remove_sourced_song_reaction(
                        message_id_int,
                        int(connection["discord_user_id"]),
                        emoji,
                        ("player", "public_player"),
                    )
                thread_ok, thread_error = await _update_player_reaction_summary(song_post)
                if not thread_ok:
                    print(f"[player-react] {thread_error}", flush=True)
                return jsonify({
                    "ok": True,
                    "mode": "thread",
                    "active": added,
                    "thread": thread_ok,
                    "warning": thread_error,
                })

            reactor_user_id = int(bot.user.id) if bot and bot.user else 0
            reactor_user_name = str(bot.user) if bot and bot.user else "player-bot"
            await db.add_song_reaction(
                message_id=message_id_int,
                channel_id=int(song_post["channel_id"]),
                song_url=song_post["url"],
                post_author_id=int(song_post["user_id"]),
                reactor_user_id=int(reactor_user_id),
                reactor_user_name=reactor_user_name,
                emoji=emoji,
                song_title=song_post.get("song_title"),
                source="bot_player",
            )

            discord_ok = False
            if bot and bot.is_ready():
                try:
                    guild = get_guild()
                    ch = guild.get_channel(int(song_post["channel_id"])) if guild else None
                    if ch:
                        msg = await ch.fetch_message(message_id_int)
                        await msg.add_reaction(emoji)
                        discord_ok = True
                except Exception as exc:
                    print(f"[player-react] Failed to add Discord reaction: {exc}", flush=True)
            return jsonify({
                "ok": True,
                "mode": "bot",
                "active": True,
                "discord": discord_ok,
            })

    @app.route("/public/api/player-react", methods=["POST"])
    async def api_public_player_react():
        from quart import jsonify

        connection = session.get("public_player_discord")
        if not connection:
            return jsonify({"error": "Connect Discord to use reactions"}), 401

        data = await request.get_json(silent=True) or {}
        message_id = str(data.get("message_id") or "")
        emoji = data.get("emoji", "")
        if not message_id.isdigit() or emoji not in PLAYER_REACTION_EMOJIS:
            return jsonify({"error": "Missing message_id or emoji"}), 400

        message_id_int = int(message_id)
        song_post = await db.get_song_post_by_message_id(message_id_int)
        if not song_post:
            return jsonify({"error": "Unknown song message"}), 404

        lock = app.player_reaction_locks.setdefault(message_id_int, asyncio.Lock())
        async with lock:
            discord_user_id = int(connection["discord_user_id"])
            display_name = connection["discord_display_name"]
            added = await db.toggle_public_player_song_reaction(
                message_id=message_id_int,
                channel_id=int(song_post["channel_id"]),
                discord_user_id=discord_user_id,
                discord_display_name=display_name,
                emoji=emoji,
            )
            if added:
                await db.add_song_reaction(
                    message_id=message_id_int,
                    channel_id=int(song_post["channel_id"]),
                    song_url=song_post["url"],
                    post_author_id=int(song_post["user_id"]),
                    reactor_user_id=discord_user_id,
                    reactor_user_name=display_name,
                    emoji=emoji,
                    song_title=song_post.get("song_title"),
                    source="public_player",
                )
            elif not await db.has_player_song_reaction(
                message_id_int, discord_user_id, emoji
            ):
                await db.remove_sourced_song_reaction(
                    message_id_int,
                    discord_user_id,
                    emoji,
                    ("player", "public_player"),
                )
            thread_ok, thread_error = await _update_player_reaction_summary(song_post)
            if not thread_ok:
                print(f"[public-player-react] {thread_error}", flush=True)
            return jsonify({
                "ok": True,
                "active": added,
                "thread": thread_ok,
                "warning": thread_error,
            })

    @app.route("/api/suno-resolve/<short_id>")
    @login_required
    async def api_suno_resolve(short_id):
        """Server-side proxy to resolve Suno short URLs to full UUIDs."""
        from quart import jsonify
        uuid = await _resolve_suno_short_url(short_id)
        if uuid:
            return jsonify({"uuid": uuid})
        return jsonify({"uuid": None, "error": "Could not resolve"}), 404

    @app.route("/api/suno-hook/<hook_uuid>")
    @login_required
    async def api_suno_hook_resolve(hook_uuid):
        """Resolve a Suno hook UUID to its parent song UUID."""
        from quart import jsonify
        uuid = await _resolve_suno_hook_uuid(hook_uuid)
        if uuid:
            return jsonify({"uuid": uuid})
        return jsonify({"uuid": None, "error": "Parent song not found"}), 404

    _RSC_REF_RE = re.compile(r'^\$[0-9a-f]+$')

    async def _fetch_suno_meta(uuid):
        """Shared helper to fetch song metadata from Suno embed page."""
        import aiohttp, re, html as _html
        lyrics = title = image_url = artist = video_url = karaoke_video_url = handle = None
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(
                    f"https://suno.com/embed/{uuid}",
                    timeout=aiohttp.ClientTimeout(total=10),
                    headers={"User-Agent": "Mozilla/5.0"},
                ) as resp:
                    html = await resp.text()

                    # Title + Artist from <title>: "Song Title by Artist | Suno"
                    m = re.search(r'<title>(.+?)\s*\|\s*Suno</title>', html)
                    if m:
                        raw = m.group(1).strip()
                        parts = raw.rsplit(' by ', 1)
                        if len(parts) == 2:
                            title = parts[0].strip()
                            artist = parts[1].strip()
                        else:
                            title = raw

                    # Fallback title from og:title
                    if not title:
                        m = re.search(r'<meta\s+property="og:title"\s+content="([^"]*)"', html)
                        if m:
                            title = m.group(1).strip()

                    # Image from og:image
                    m = re.search(r'<meta\s+property="og:image"\s+content="([^"]*)"', html)
                    if m:
                        image_url = m.group(1).strip()
                    if not image_url:
                        m = re.search(r'"image_url"\s*:\s*"([^"]*)"', html)
                        if m:
                            image_url = m.group(1)

                    # Artist fallback from display_name in JSON
                    if not artist:
                        artist = _extract_suno_clip_owner_display_name(html, uuid)
                    if not artist:
                        matches = re.findall(r'display_name\\":\\"([^"\\]+)\\"', html)
                        for dn in reversed(matches):
                            dn = _decode_suno_json_string(dn)
                            if _valid_suno_display_name(dn):
                                artist = dn
                                break

                    # Lyrics from prompt field
                    def _valid_lyrics(s):
                        if not s or len(s.strip()) < 10:
                            return False
                        if re.match(r'^\$\w+$', s.strip()):
                            return False
                        return True

                    idx = html.find(uuid)
                    if idx > -1:
                        chunk = html[max(0, idx-500):idx+5000]
                        m = re.search(r'"prompt"\s*:\s*"((?:[^"\\]|\\.)*)"', chunk)
                        if m:
                            candidate = m.group(1).replace("\\n", "\n").replace('\\"', '"')
                            if _valid_lyrics(candidate):
                                lyrics = candidate
                    if not lyrics:
                        m = re.search(r'prompt\\":\\"((?:[^\\]|\\.)*?)\\"', html)
                        if m:
                            candidate = m.group(1).replace("\\\\n", "\n").replace('\\\\"', '"').replace("\\n", "\n")
                            if _valid_lyrics(candidate):
                                lyrics = candidate

                    # RSC-reference lyrics: longer prompts (or those containing
                    # emojis / non-ASCII chars that Next.js splits out) are stored
                    # in a separate flight chunk like `3d:T1aef,<content>` and
                    # the song JSON only carries `"prompt":"$3d"`. Resolve that.
                    if not lyrics or _RSC_REF_RE.match(lyrics or ""):
                        try:
                            import json as _json
                            chunks = re.findall(
                                r'self\.__next_f\.push\(\[1,"(.*?)"\]\)',
                                html, re.DOTALL,
                            )
                            if chunks:
                                # Each chunk is a JS string literal — decode via JSON.
                                decoded = []
                                for c in chunks:
                                    try:
                                        decoded.append(_json.loads('"' + c + '"'))
                                    except Exception:
                                        decoded.append(
                                            c.replace('\\n', '\n')
                                             .replace('\\"', '"')
                                             .replace('\\\\', '\\')
                                        )
                                full = "".join(decoded)
                                mref = re.search(
                                    r'"prompt":"\$([0-9a-f]+)"', full
                                )
                                if mref:
                                    ref = mref.group(1)
                                    # RSC text chunks carry their exact length
                                    # in hex right after the `T`, e.g.
                                    # `3d:T1aef,<bytes of content>`. The hex
                                    # value is the **UTF-8 byte length** of
                                    # the payload, so we have to slice on a
                                    # bytes view — slicing on the decoded
                                    # str would over-capture (and bleed into
                                    # the next chunk) for emoji / special
                                    # characters.
                                    full_bytes = full.encode("utf-8")
                                    tpat_b = re.compile(
                                        rb'(?:^|\n)' + re.escape(ref).encode() +
                                        rb':T([0-9a-f]+),'
                                    )
                                    tfnd = tpat_b.search(full_bytes)
                                    if tfnd:
                                        length = int(tfnd.group(1), 16)
                                        b_start = tfnd.end()
                                        candidate = full_bytes[b_start:b_start + length] \
                                            .decode("utf-8", errors="replace").rstrip()
                                        if _valid_lyrics(candidate):
                                            lyrics = candidate
                        except Exception:
                            pass

                    # Video cover (user-uploaded video cover, typically 9:16)
                    m = re.search(r'"video_cover_url"\s*:\s*"([^"]+)"', html)
                    if not m:
                        m = re.search(r'video_cover_url\\":\\"([^"\\]+)\\"', html)
                    if m:
                        video_url = m.group(1).replace("\\/", "/")

                    # Full Suno-generated karaoke video with synchronized lyrics.
                    # Keep this separate from video_cover_url, which is the visual
                    # used by the Suno Info player.
                    m = re.search(r'(?<!cover_)"video_url"\s*:\s*"([^"]+)"', html)
                    if not m:
                        m = re.search(r'(?<!cover_)video_url\\":\\"([^"\\]+)\\"', html)
                    if m:
                        karaoke_video_url = m.group(1).replace("\\/", "/")

                    # Handle (artist URL slug) — first occurrence is the song owner
                    m = re.search(r'handle\\":\\"([^"\\]+)\\"', html)
                    if not m:
                        m = re.search(r'"handle"\s*:\s*"([^"]+)"', html)
                    if m:
                        handle = m.group(1)
        except Exception:
            pass
        if title:
            title = _html.unescape(title)
        if artist:
            artist = _html.unescape(artist)
        return {
            "lyrics": lyrics,
            "title": title,
            "image_url": image_url,
            "artist": artist,
            "video_url": video_url,
            "karaoke_video_url": karaoke_video_url,
            "handle": handle,
        }

    async def _fetch_elevenmusic_meta(track_id: str):
        """Fetch public ElevenMusic track metadata from the rendered track page."""
        import html as _html
        import json as _json

        url = f"https://elevenmusic.io/tracks/{track_id}"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "Chrome/126 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        result = {
            "uuid": track_id,
            "provider": "elevenmusic",
            "source_url": url,
            "title": track_id[:8],
            "artist": "",
            "image_url": "",
            "video_url": None,
            "audio_url": f"https://media.elevenmusic.io/tracks/{track_id}/hls/master.m3u8",
            "lyrics": "",
            "prompt": "",
            "tags": [],
            "model": "ElevenMusic",
            "type": "elevenmusic",
            "plays": None,
            "likes": None,
            "created_at": "",
            "duration": None,
            "handle": None,
        }

        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(
                    url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=12),
                ) as resp:
                    if resp.status != 200:
                        return result
                    page = await resp.text()
        except Exception:
            return result

        def _first(pattern):
            m = re.search(pattern, page, re.S)
            if not m:
                return ""
            return _html.unescape(m.group(1).replace(r"\/", "/").strip())

        og_title = _first(r'<meta\s+property="og:title"\s+content="([^"]+)"')
        if og_title:
            og_title = re.sub(r"\s*\|\s*ElevenMusic\s*$", "", og_title).strip()
            parts = og_title.rsplit(" - ", 1)
            if len(parts) == 2:
                result["title"], result["artist"] = parts[0].strip(), parts[1].strip()
            else:
                result["title"] = og_title

        image_url = _first(r'<meta\s+property="og:image"\s+content="([^"]+)"')
        if image_url:
            result["image_url"] = image_url

        audio_url = _first(r'<meta\s+property="og:audio"\s+content="([^"]+)"')
        if audio_url:
            result["audio_url"] = audio_url

        description = _first(r'<meta\s+property="og:description"\s+content="([^"]*)"')
        if description:
            result["prompt"] = description

        for raw in re.findall(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', page, re.S):
            try:
                data = _json.loads(_html.unescape(raw))
            except Exception:
                continue
            if isinstance(data, dict) and data.get("@type") == "MusicRecording":
                result["title"] = data.get("name") or result["title"]
                artist_data = data.get("byArtist") or {}
                if isinstance(artist_data, dict):
                    result["artist"] = artist_data.get("name") or result["artist"]
                if isinstance(data.get("image"), str):
                    result["image_url"] = data["image"]
                genres = data.get("genre")
                if isinstance(genres, list):
                    result["tags"] = [str(g) for g in genres if g]
                elif isinstance(genres, str) and genres:
                    result["tags"] = [genres]
                dur = data.get("duration") or ""
                m = re.match(r"PT(?:(\d+)M)?(?:(\d+)S)?", dur)
                if m:
                    result["duration"] = int(m.group(1) or 0) * 60 + int(m.group(2) or 0)
                break

        if result["duration"] is None:
            m = re.search(r'"duration_ms"\s*:\s*(\d+)', page)
            if m:
                result["duration"] = int(m.group(1)) / 1000

        if not result["artist"]:
            artist = _first(r'"display_name"\s*:\s*"([^"\\]+)"')
            if artist:
                result["artist"] = artist

        title = _first(r'"title"\s*:\s*"([^"\\]+)"')
        if title and result["title"] == track_id[:8]:
            result["title"] = title

        animated = re.search(
            r'"track"\s*:\s*\{.*?"id"\s*:\s*"' + re.escape(track_id) +
            r'".*?"cover"\s*:\s*\{.*?"animated"\s*:\s*\{.*?"url"\s*:\s*"([^"\\]+)"',
            page,
            re.S,
        )
        if animated:
            result["video_url"] = _html.unescape(animated.group(1).replace(r"\/", "/"))

        lyric_lines = []
        for line in re.findall(r'<p[^>]*class="[^"]*text-lg[^"]*"[^>]*>(.*?)</p>', page, re.S):
            clean = re.sub(r"<[^>]+>", "", line)
            clean = _html.unescape(clean).strip()
            if clean and clean not in lyric_lines:
                lyric_lines.append(clean)
        if lyric_lines:
            result["lyrics"] = "\n".join(lyric_lines)

        return result

    @app.route("/api/suno-lyrics/<uuid>")
    @login_required
    async def api_suno_lyrics(uuid):
        from quart import jsonify
        return jsonify(await _fetch_suno_meta(uuid))

    @app.route("/api/verify-songs", methods=["POST"])
    @login_required
    async def api_verify_songs():
        """Verify messages using DB-stored channel_id (never client-provided)."""
        from quart import jsonify
        import discord as _discord
        data = await request.get_json(silent=True) or {}
        items = data.get("items") or []
        if len(items) > 500:
            items = items[:500]
        if not bot or not bot.is_ready():
            return jsonify({"missing": [], "ok": False, "reason": "bot_not_ready"})
        guild = get_guild()
        if not guild:
            return jsonify({"missing": [], "ok": False, "reason": "no_guild"})

        async def check(item):
            try:
                mid = int(item.get("message_id"))
            except (TypeError, ValueError):
                return None
            row = await db.get_song_post_by_message_id(mid)
            if not row:
                return None
            cid = row.get("channel_id")
            if cid is None:
                return None
            ch = guild.get_channel(int(cid))
            if ch is None:
                return mid  # channel gone
            try:
                await ch.fetch_message(mid)
                return None
            except _discord.NotFound:
                return mid
            except Exception:
                return None

        # Limit concurrency to avoid rate-limit bursts
        sem = asyncio.Semaphore(5)
        async def bounded(i):
            async with sem:
                return await check(i)
        results = await asyncio.gather(*[bounded(i) for i in items])
        missing = [r for r in results if r is not None]
        for mid in missing:
            try:
                await db.delete_song_posts_by_message_id(mid)
            except Exception:
                pass
        return jsonify({"missing": [str(m) for m in missing], "ok": True})

    @app.route("/listening-party", methods=["GET", "POST"])
    @permission_required('party_playlist')
    async def listening_party():
        if request.method == "POST":
            form = await request.form
            action = form.get("action")

            if action == "add":
                input_channel_id = form.get("input_channel_id", "").strip()
                output_channel_id = form.get("output_channel_id", "").strip()
                time_range = int(form.get("time_range_hours", "24"))

                if not input_channel_id.isdigit() or not output_channel_id.isdigit():
                    await flash("Invalid channel ID.", "error")
                else:
                    input_channel_id = int(input_channel_id)
                    output_channel_id = int(output_channel_id)

                    monitored = await db.get_monitored_channel(input_channel_id)
                    if not monitored:
                        await flash("Input channel must be a monitored channel.", "error")
                    elif input_channel_id == output_channel_id:
                        await flash("Input and output channel must be different.", "error")
                    else:
                        await db.add_listening_party_config(input_channel_id, output_channel_id, time_range)
                        await db.add_audit_log(
                            event_type="listening_party_added",
                            channel_id=input_channel_id,
                            details=f"Listening party config added: input={input_channel_id}, output={output_channel_id}, range={time_range}h",
                            actor=session.get("username", "unknown"),
                        )
                        await flash("Listening party config added.", "success")

            elif action == "update":
                config_id = int(form.get("config_id", "0"))
                output_channel_id = int(form.get("output_channel_id", "0"))
                time_range = int(form.get("time_range_hours", "24"))
                await db.update_listening_party_config(config_id, output_channel_id, time_range)
                await db.add_audit_log(
                    event_type="listening_party_updated",
                    details=f"Config {config_id} updated: output={output_channel_id}, range={time_range}h",
                    actor=session.get("username", "unknown"),
                )
                await flash("Config updated.", "success")

            elif action == "remove":
                config_id = int(form.get("config_id", "0"))
                await db.remove_listening_party_config(config_id)
                await db.add_audit_log(
                    event_type="listening_party_removed",
                    details=f"Config {config_id} removed",
                    actor=session.get("username", "unknown"),
                )
                await flash("Config removed.", "success")

            return redirect(url_for("listening_party"))

        configs = await db.get_listening_party_configs()

        guild = get_guild()
        monitored_channels = await db.get_monitored_channels()
        available_output_channels = []
        if guild:
            for ch in guild.text_channels:
                available_output_channels.append({"id": ch.id, "name": ch.name})

        # Resolve channel names
        for cfg in configs:
            cfg["input_name"] = f"channel-{cfg['input_channel_id']}"
            cfg["output_name"] = f"channel-{cfg['output_channel_id']}"
            if guild:
                inch = guild.get_channel(cfg["input_channel_id"])
                if inch:
                    cfg["input_name"] = inch.name
                outch = guild.get_channel(cfg["output_channel_id"])
                if outch:
                    cfg["output_name"] = outch.name

        return await render_template(
            "listening_party.html",
            configs=configs,
            monitored_channels=monitored_channels,
            available_output_channels=available_output_channels,
        )

    @app.route("/playlist-search", methods=["GET", "POST"])
    @permission_required('playlist_search')
    async def playlist_search():
        if request.method == "POST":
            form = await request.form
            action = form.get("action")

            if action == "add":
                channel_id = form.get("channel_id", "").strip()
                if not channel_id.isdigit():
                    await flash("Invalid channel ID.", "error")
                else:
                    channel_id = int(channel_id)
                    await db.add_playlist_search_channel(channel_id)
                    channel_name = f"channel-{channel_id}"
                    guild = get_guild()
                    if guild:
                        ch = guild.get_channel(channel_id)
                        if ch:
                            channel_name = ch.name
                    await db.add_audit_log(
                        event_type="playlist_search_added",
                        channel_id=channel_id,
                        channel_name=channel_name,
                        details="Playlist search channel added",
                        actor=session.get("username", "unknown"),
                    )
                    await flash(f"Channel #{channel_name} added for playlist search.", "success")

            elif action == "remove":
                config_id = int(form.get("config_id", "0"))
                await db.remove_playlist_search_channel(config_id)
                await db.add_audit_log(
                    event_type="playlist_search_removed",
                    details=f"Playlist search config {config_id} removed",
                    actor=session.get("username", "unknown"),
                )
                await flash("Channel removed.", "success")

            return redirect(url_for("playlist_search"))

        configs = await db.get_playlist_search_channels()

        guild = get_guild()
        available_channels = []
        if guild:
            existing_ids = {c["channel_id"] for c in configs}
            for ch in guild.text_channels:
                if ch.id not in existing_ids:
                    available_channels.append({"id": ch.id, "name": ch.name})

        # Resolve channel names
        for cfg in configs:
            cfg["channel_name"] = f"channel-{cfg['channel_id']}"
            if guild:
                ch = guild.get_channel(cfg["channel_id"])
                if ch:
                    cfg["channel_name"] = ch.name

        return await render_template(
            "playlist_search.html",
            configs=configs,
            available_channels=available_channels,
        )

    async def _run_scan(actor: str):
        """Background task to scan all monitored channels for Suno URLs."""
        app.scan_status["running"] = True
        app.scan_status["progress"] = "Starting scan..."
        app.scan_status["result"] = ""
        try:
            guild = get_guild()
            if not guild:
                app.scan_status["result"] = "Bot not connected to guild."
                return

            channels = await db.get_monitored_channels()
            total_found = 0

            for i, ch_cfg in enumerate(channels):
                channel = guild.get_channel(ch_cfg["channel_id"])
                if not channel:
                    continue

                app.scan_status["progress"] = f"Scanning #{channel.name} ({i+1}/{len(channels)})..."
                rows = []
                msg_count = 0
                try:
                    async for message in channel.history(limit=None):
                        if message.author.bot:
                            continue
                        msg_count += 1
                        if msg_count % 500 == 0:
                            app.scan_status["progress"] = f"Scanning #{channel.name} ({i+1}/{len(channels)})... {msg_count} messages"
                        urls = SUNO_URL_PATTERN.findall(message.content)
                        for url in urls:
                            rows.append((
                                channel.id,
                                message.author.id,
                                str(message.author),
                                url,
                                message.created_at.timestamp(),
                                message.id,
                            ))
                except Exception as e:
                    app.scan_status["progress"] = f"Error scanning #{channel.name}: {e}"
                    continue

                if rows:
                    await db.add_song_posts_bulk(rows)
                    total_found += len(rows)

            app.scan_status["result"] = f"Scan complete. {total_found} song(s) found across {len(channels)} channel(s)."
            await db.add_audit_log(
                event_type="song_scan",
                details=f"History scan: {total_found} songs found",
                actor=actor,
            )
        except Exception as e:
            app.scan_status["result"] = f"Scan failed: {e}"
        finally:
            app.scan_status["running"] = False
            app.scan_status["progress"] = ""

    async def _run_orphan_cleanup():
        """Background task: verify each song_post message exists on Discord, delete orphans."""
        import discord as _discord
        app.cleanup_status["running"] = True
        app.cleanup_status["progress"] = "Loading song posts..."
        app.cleanup_status["result"] = ""
        try:
            guild = get_guild()
            if not guild:
                app.cleanup_status["result"] = "Bot not connected to guild."
                return
            posts = await db.get_song_posts_with_message_id()
            total = len(posts)
            if not total:
                app.cleanup_status["result"] = "No song posts with message IDs to verify."
                return
            # Group by channel for efficiency
            by_channel: dict[int, list[int]] = {}
            for p in posts:
                by_channel.setdefault(int(p["channel_id"]), []).append(int(p["message_id"]))
            removed = 0
            checked = 0
            errors = 0
            for ch_id, msg_ids in by_channel.items():
                channel = guild.get_channel(ch_id)
                if channel is None:
                    # Channel gone — remove all its posts
                    for mid in msg_ids:
                        await db.delete_song_posts_by_message_id(mid)
                        removed += 1
                        checked += 1
                    continue
                for mid in msg_ids:
                    checked += 1
                    try:
                        await channel.fetch_message(mid)
                    except _discord.NotFound:
                        await db.delete_song_posts_by_message_id(mid)
                        removed += 1
                    except _discord.Forbidden:
                        errors += 1
                    except Exception:
                        errors += 1
                    if checked % 25 == 0:
                        app.cleanup_status["progress"] = (
                            f"Verified {checked}/{total} — removed {removed} orphans"
                        )
                    # Small sleep to avoid rate limit bursts
                    await asyncio.sleep(0.05)
            app.cleanup_status["result"] = (
                f"Done. Verified {checked} song posts, removed {removed} orphans"
                + (f", {errors} errors" if errors else "") + "."
            )
            await db.add_audit_log(
                event_type="song_cleanup",
                details=f"Orphan cleanup: {removed}/{checked} removed",
                actor="system",
            )
        except Exception as e:
            app.cleanup_status["result"] = f"Cleanup failed: {e}"
        finally:
            app.cleanup_status["running"] = False
            app.cleanup_status["progress"] = ""

    @app.route("/song-stats", methods=["GET", "POST"])
    @permission_required('song_stats')
    async def song_stats():
        if request.method == "POST":
            form = await request.form
            action = form.get("action")

            if action == "scan":
                if app.scan_status["running"]:
                    await flash("A scan is already in progress.", "error")
                elif bot and bot.is_ready():
                    actor = session.get("username", "unknown")
                    asyncio.get_event_loop().create_task(_run_scan(actor))
                    await flash("Scan started in the background. Refresh this page to see progress.", "success")
                else:
                    await flash("Bot is not ready.", "error")
            elif action == "cleanup_orphans":
                if app.cleanup_status["running"]:
                    await flash("A cleanup is already in progress.", "error")
                elif bot and bot.is_ready():
                    asyncio.get_event_loop().create_task(_run_orphan_cleanup())
                    await flash("Orphan cleanup started. Refresh this page to see progress.", "success")
                else:
                    await flash("Bot is not ready.", "error")
            elif action == "delete_all_songs":
                count = await db.delete_all_songs()
                print(f"[song-stats] Deleted all {count} song posts")

            return redirect(url_for("song_stats"))

        # GET: gather stats
        import traceback
        try:
            guild = get_guild()
            monitored = await db.get_monitored_channels()

            # Per-channel stats overview
            channel_totals = await db.get_song_stats_all_channels()
            channel_map = {}
            for ct in channel_totals:
                channel_map[ct["channel_id"]] = ct["count"]

            channel_list = []
            for ch in monitored:
                ch_name = f"channel-{ch['channel_id']}"
                if guild:
                    gch = guild.get_channel(ch["channel_id"])
                    if gch:
                        ch_name = gch.name
                channel_list.append({
                    "channel_id": ch["channel_id"],
                    "channel_name": ch_name,
                    "count": channel_map.get(ch["channel_id"], 0),
                })

            # Selected channel filter
            filter_channel = request.args.get("channel", type=int)
            stats = await db.get_song_stats(channel_id=filter_channel)

            return await render_template(
                "song_stats.html",
                channel_list=channel_list,
                stats=stats,
                filter_channel=filter_channel,
                scan_status=app.scan_status,
                cleanup_status=app.cleanup_status,
            )
        except Exception as e:
            traceback.print_exc()
            return f"<pre>Error: {e}\n\n{traceback.format_exc()}</pre>", 500

    @app.route("/user-stats", methods=["GET", "POST"])
    @permission_required('user_stats')
    async def user_stats():
        if request.method == "POST":
            form = await request.form
            action = form.get("action")
            if action == "delete_all_songs":
                count = await db.delete_all_songs()
                print(f"[user-stats] Deleted all {count} song posts")
            return redirect(url_for("user_stats"))

        import traceback
        try:
            guild = get_guild()
            selected_user_id = request.args.get("user_id", type=int)

            # Leaderboard
            ranking = await db.get_all_users_ranking()

            # Resolve display names from guild
            for entry in ranking:
                entry["display_name"] = entry["user_name"] or f"User {entry['user_id']}"
                if guild:
                    member = guild.get_member(entry["user_id"])
                    if member:
                        entry["display_name"] = member.display_name

            # User detail stats
            user_detail = None
            user_display_name = None
            if selected_user_id:
                user_detail = await db.get_user_song_stats(selected_user_id)
                user_display_name = f"User {selected_user_id}"
                if guild:
                    member = guild.get_member(selected_user_id)
                    if member:
                        user_display_name = member.display_name

                # Resolve channel names in per_channel
                if user_detail and user_detail["per_channel"]:
                    for pc in user_detail["per_channel"]:
                        pc["channel_name"] = f"channel-{pc['channel_id']}"
                        if guild:
                            ch = guild.get_channel(pc["channel_id"])
                            if ch:
                                pc["channel_name"] = ch.name

            return await render_template(
                "user_stats.html",
                ranking=ranking,
                user_detail=user_detail,
                user_display_name=user_display_name,
                selected_user_id=selected_user_id,
            )
        except Exception as e:
            traceback.print_exc()
            return f"<pre>Error: {e}\n\n{traceback.format_exc()}</pre>", 500

    import re as _re
    _CUSTOM_EMOJI_RE = _re.compile(r'<a?:(\w+):\d+>')

    @app.template_filter('format_emoji')
    def format_emoji_filter(emoji_str):
        m = _CUSTOM_EMOJI_RE.match(emoji_str)
        if m:
            return f":{m.group(1)}:"
        return emoji_str

    async def _run_title_scan():
        """Background task to fetch song titles from Discord embeds for reactions missing them."""
        import discord as _discord
        app.title_scan_status["running"] = True
        app.title_scan_status["progress"] = "Starting title scan..."
        app.title_scan_status["result"] = ""
        try:
            guild = get_guild()
            if not guild:
                app.title_scan_status["result"] = "Bot not connected to guild."
                return

            missing = await db.get_reactions_missing_titles()
            total = len(missing)
            updated = 0
            skipped = 0

            for i, item in enumerate(missing):
                if (i + 1) % 10 == 0 or i == 0:
                    app.title_scan_status["progress"] = f"Fetching titles... {i+1}/{total} messages"

                channel = guild.get_channel(item["channel_id"])
                if not channel:
                    skipped += 1
                    continue

                try:
                    message = await channel.fetch_message(item["message_id"])
                    title = None
                    for embed in message.embeds:
                        if embed.title:
                            title = embed.title
                            break
                    if title:
                        await db.update_song_title(item["message_id"], title)
                        updated += 1
                    else:
                        skipped += 1
                except (_discord.NotFound, _discord.Forbidden):
                    skipped += 1
                except Exception:
                    skipped += 1

            app.title_scan_status["result"] = f"Done. {updated} titles updated, {skipped} skipped (of {total} messages)."
        except Exception as e:
            app.title_scan_status["result"] = f"Title scan failed: {e}"
        finally:
            app.title_scan_status["running"] = False
            app.title_scan_status["progress"] = ""

    async def _run_reaction_scan():
        """Background task to backfill reactions from Discord message history."""
        import discord as _discord
        app.reaction_scan_status["running"] = True
        app.reaction_scan_status["progress"] = "Loading already-scanned messages..."
        app.reaction_scan_status["result"] = ""
        try:
            guild = get_guild()
            if not guild:
                app.reaction_scan_status["result"] = "Bot not connected to guild."
                return

            # Pre-load message IDs that already have reactions in DB — skip them entirely
            scanned_ids = await db.get_scanned_reaction_message_ids()

            channels = await db.get_monitored_channels()
            total_added = 0
            total_messages = 0
            skipped = 0

            for i, ch_cfg in enumerate(channels):
                channel = guild.get_channel(ch_cfg["channel_id"])
                if not channel:
                    continue

                app.reaction_scan_status["progress"] = f"Scanning #{channel.name} ({i+1}/{len(channels)})..."
                msg_count = 0

                try:
                    batch = []
                    async for message in channel.history(limit=None):
                        if message.author.bot:
                            continue
                        msg_count += 1
                        if msg_count % 500 == 0:
                            app.reaction_scan_status["progress"] = (
                                f"Scanning #{channel.name} ({i+1}/{len(channels)})... "
                                f"{msg_count} msgs, {total_added} reactions, {skipped} skipped"
                            )

                        urls = SUNO_URL_PATTERN.findall(message.content)
                        if not urls:
                            continue

                        if not message.reactions:
                            continue

                        # Skip messages already fully scanned
                        if message.id in scanned_ids:
                            skipped += 1
                            continue

                        total_messages += 1
                        # Extract title from embed
                        song_title = None
                        for embed in message.embeds:
                            if embed.title:
                                song_title = embed.title
                                break

                        # Fetch all reactions on this message
                        for reaction in message.reactions:
                            emoji_str = str(reaction.emoji)
                            try:
                                async for user in reaction.users():
                                    if user.bot:
                                        continue
                                    for url in urls:
                                        batch.append((
                                            message.id, channel.id, url,
                                            message.author.id, user.id,
                                            str(user), emoji_str, song_title,
                                            message.created_at.timestamp(),
                                        ))
                            except (_discord.Forbidden, _discord.HTTPException):
                                pass

                        # Flush batch when large enough
                        if len(batch) >= 500:
                            await db.add_song_reactions_bulk(batch)
                            total_added += len(batch)
                            batch = []

                    # Flush remaining batch
                    if batch:
                        await db.add_song_reactions_bulk(batch)
                        total_added += len(batch)
                        batch = []

                except Exception as e:
                    app.reaction_scan_status["progress"] = f"Error scanning #{channel.name}: {e}"
                    continue

            app.reaction_scan_status["result"] = (
                f"Done. {total_added} reactions added, {skipped} messages skipped (already scanned), "
                f"{total_messages} new song messages processed in {len(channels)} channel(s)."
            )
            await db.add_audit_log(
                event_type="reaction_scan",
                details=f"Reaction scan: {total_added} reactions from {total_messages} messages, {skipped} skipped",
                actor="system",
            )
        except Exception as e:
            app.reaction_scan_status["result"] = f"Reaction scan failed: {e}"
        finally:
            app.reaction_scan_status["running"] = False
            app.reaction_scan_status["progress"] = ""

    @app.route("/reaction-stats", methods=["GET", "POST"])
    @permission_required('reaction_stats')
    async def reaction_stats():
        import traceback

        if request.method == "POST":
            form = await request.form
            action = form.get("action")
            if action == "refresh_titles":
                if app.title_scan_status["running"]:
                    pass  # already running
                elif bot and bot.is_ready():
                    import asyncio
                    asyncio.get_event_loop().create_task(_run_title_scan())
                return redirect(url_for("reaction_stats"))
            elif action == "scan_reactions":
                if app.reaction_scan_status["running"]:
                    pass  # already running
                elif bot and bot.is_ready():
                    import asyncio
                    asyncio.get_event_loop().create_task(_run_reaction_scan())
                return redirect(url_for("reaction_stats"))
            elif action == "delete_all_reactions":
                count = await db.delete_all_reactions()
                print(f"[reaction-stats] Deleted all {count} reactions")
                return redirect(url_for("reaction_stats"))

        try:
            guild = get_guild()
            filter_channel = request.args.get("channel", type=int)

            # Time filter
            chart_range = request.args.get("range", default="30", type=str)
            range_days = {"1": 1, "7": 7, "30": 30, "90": 90, "all": 0}.get(chart_range, 30)

            # All stats use the same time filter
            stats = await db.get_reaction_stats(channel_id=filter_channel, days=range_days)

            # Most Reacted Songs — uses same central time filter
            top_songs = await db.get_top_songs(channel_id=filter_channel, days=range_days)

            # Channel list with reaction counts
            reaction_channels = await db.get_reaction_channels()
            channel_list = []
            for rc in reaction_channels:
                ch_name = f"channel-{rc['channel_id']}"
                if guild:
                    ch = guild.get_channel(rc["channel_id"])
                    if ch:
                        ch_name = ch.name
                channel_list.append({
                    "channel_id": rc["channel_id"],
                    "channel_name": ch_name,
                    "count": rc["count"],
                })

            # Resolve display names for top reactors, most reacted authors, and top songs
            if guild:
                for entry in stats.get("top_reactors", []):
                    member = guild.get_member(entry["user_id"])
                    entry["display_name"] = member.display_name if member else (entry["user_name"] or f"User {entry['user_id']}")
                for entry in stats.get("most_reacted_authors", []):
                    member = guild.get_member(entry["user_id"])
                    entry["display_name"] = member.display_name if member else f"User {entry['user_id']}"
                for entry in top_songs:
                    if entry.get("post_author_id"):
                        member = guild.get_member(entry["post_author_id"])
                        entry["author_name"] = member.display_name if member else f"User {entry['post_author_id']}"
                    else:
                        entry["author_name"] = "Unknown"
            else:
                for entry in stats.get("top_reactors", []):
                    entry["display_name"] = entry["user_name"] or f"User {entry['user_id']}"
                for entry in stats.get("most_reacted_authors", []):
                    entry["display_name"] = f"User {entry['user_id']}"
                for entry in top_songs:
                    entry["author_name"] = f"User {entry.get('post_author_id', '?')}"

            return await render_template(
                "reaction_stats.html",
                stats=stats,
                channel_list=channel_list,
                filter_channel=filter_channel,
                chart_range=chart_range,
                title_scan_status=app.title_scan_status,
                reaction_scan_status=app.reaction_scan_status,
                top_songs=top_songs,
            )
        except Exception as e:
            traceback.print_exc()
            return f"<pre>Error: {e}\n\n{traceback.format_exc()}</pre>", 500

    async def _sync_song_rating_uuids(limit: int | None = None) -> tuple[int, int]:
        urls = await db.get_unresolved_song_urls(limit=limit)
        if not urls:
            return 0, 0
        status = app.song_rating_sync_status
        status.update({
            "running": True,
            "total": len(urls),
            "processed": 0,
            "resolved": 0,
            "unresolved": 0,
        })
        queue = asyncio.Queue()
        for url in urls:
            queue.put_nowait(url)

        async def worker():
            while True:
                try:
                    url = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    uuid = await resolve_suno_uuid(url)
                    if uuid:
                        await db.set_song_uuid(url, uuid)
                        status["resolved"] += 1
                    else:
                        status["unresolved"] += 1
                except Exception:
                    status["unresolved"] += 1
                finally:
                    status["processed"] += 1
                    queue.task_done()

        try:
            await asyncio.gather(*(worker() for _ in range(8)))
            return status["resolved"], status["unresolved"]
        finally:
            status["running"] = False

    async def _run_song_rating_uuid_sync():
        try:
            await _sync_song_rating_uuids()
            await _build_song_rating_snapshot_for_date(
                _song_rating_today(), force=True
            )
        except Exception as exc:
            app.song_rating_sync_status["running"] = False
            app.song_rating_sync_status["error"] = str(exc)

    def _song_rating_today() -> str:
        return datetime.now(ZoneInfo("Europe/Berlin")).date().isoformat()

    def _song_rating_day_bounds(snapshot_date: str) -> tuple[float, float]:
        day = datetime.strptime(snapshot_date, "%Y-%m-%d").date()
        timezone = ZoneInfo("Europe/Berlin")
        midnight = datetime.combine(day, datetime.min.time(), timezone)
        next_midnight = datetime.combine(day + timedelta(days=1), datetime.min.time(), timezone)
        return midnight.timestamp(), next_midnight.timestamp()

    async def _build_song_rating_snapshot_for_date(
        snapshot_date: str, force: bool = False
    ) -> list[dict]:
        if snapshot_date != _song_rating_today() and not force:
            existing = await db.get_song_rating_snapshot(snapshot_date)
            if existing:
                return existing
        start_timestamp, end_timestamp = _song_rating_day_bounds(snapshot_date)
        return await db.build_song_rating_snapshot(
            snapshot_date,
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
        )

    @app.route("/song-rating-api", methods=["GET", "POST"])
    @permission_required("song_rating_api")
    async def song_rating_api_admin():
        if request.method == "POST":
            form = await request.form
            action = (form.get("action") or "save").strip()
            if action == "save":
                await db.set_setting(
                    "song_rating_api_enabled", "1" if form.get("enabled") else "0"
                )
                await flash("Song Rating API settings saved.", "success")
            elif action == "generate_token":
                token = secrets.token_urlsafe(48)
                digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
                await db.set_setting("song_rating_api_token_hash", digest)
                app.song_rating_new_tokens[int(session["user_id"])] = token
                await db.add_audit_log(
                    event_type="song_rating_api_token_rotated",
                    details="Song Rating API access token generated or rotated",
                    actor=session.get("username", "unknown"),
                )
                await flash(
                    "A new API token was generated. Store it now; it is shown only once.",
                    "success",
                )
            elif action == "sync_uuids":
                if app.song_rating_sync_status["running"]:
                    await flash("Suno UUID resolution is already running.", "warning")
                else:
                    app.song_rating_sync_task = asyncio.create_task(
                        _run_song_rating_uuid_sync()
                    )
                    await flash(
                        "Suno UUID resolution started in the background.", "success"
                    )
            elif action == "refresh_snapshot":
                rows = await _build_song_rating_snapshot_for_date(
                    _song_rating_today(), force=True
                )
                await flash(f"Current snapshot refreshed with {len(rows)} songs.", "success")
            elif action == "build_history":
                try:
                    history_days = max(1, min(int(form.get("history_days", 30)), 365))
                except (TypeError, ValueError):
                    history_days = 30
                today_date = datetime.now(ZoneInfo("Europe/Berlin")).date()
                for days_ago in range(history_days, 0, -1):
                    snapshot_date = (today_date - timedelta(days=days_ago)).isoformat()
                    await _build_song_rating_snapshot_for_date(snapshot_date, force=True)
                await flash(
                    f"Generated {history_days} historical daily snapshots.", "success"
                )
            return redirect(url_for("song_rating_api_admin"))

        today = _song_rating_today()
        current = await _build_song_rating_snapshot_for_date(today, force=True)
        stats = await db.get_song_rating_api_stats()
        unresolved_count = await db.count_unresolved_song_urls()
        unresolved_urls = await db.get_unresolved_song_url_details(limit=100)
        endpoint_url = url_for("song_rating_api_feed", _external=True)
        if endpoint_url.startswith("http://") and not request.host.startswith(
            ("localhost", "127.0.0.1")
        ):
            endpoint_url = "https://" + endpoint_url.removeprefix("http://")
        response = await make_response(
            await render_template(
                "song_rating_api.html",
                enabled=(await db.get_setting("song_rating_api_enabled")) == "1",
                token_configured=bool(await db.get_setting("song_rating_api_token_hash")),
                new_token=app.song_rating_new_tokens.pop(int(session["user_id"]), None),
                endpoint_url=endpoint_url,
                today=today,
                current=current,
                stats=stats,
                unresolved_count=unresolved_count,
                unresolved_urls=unresolved_urls,
                sync_status=app.song_rating_sync_status,
            )
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/api/v1/song-ratings", methods=["GET"])
    async def song_rating_api_feed():
        from quart import jsonify

        if (await db.get_setting("song_rating_api_enabled")) != "1":
            return jsonify({"error": "Song Rating API is disabled"}), 503
        configured_hash = await db.get_setting("song_rating_api_token_hash")
        authorization = request.headers.get("Authorization", "")
        supplied_token = ""
        if authorization.lower().startswith("bearer "):
            supplied_token = authorization[7:].strip()
        if not supplied_token:
            supplied_token = request.headers.get("X-API-Key", "").strip()
        supplied_hash = hashlib.sha256(supplied_token.encode("utf-8")).hexdigest()
        if not configured_hash or not supplied_token or not hmac.compare_digest(
            configured_hash, supplied_hash
        ):
            return jsonify({"error": "Unauthorized"}), 401

        requested_date = (request.args.get("date") or _song_rating_today()).strip()
        try:
            parsed_date = datetime.strptime(requested_date, "%Y-%m-%d").date()
        except ValueError:
            return jsonify({"error": "Invalid date; use YYYY-MM-DD"}), 400

        today = _song_rating_today()
        if parsed_date > datetime.strptime(today, "%Y-%m-%d").date():
            return jsonify({"error": "Future dates are not available"}), 400
        rows = await _build_song_rating_snapshot_for_date(requested_date)
        await db.log_song_rating_api_request(requested_date, len(rows))
        response = jsonify({
            "date": requested_date,
            "generated_at": datetime.now(ZoneInfo("Europe/Berlin")).isoformat(),
            "songs": rows,
            "count": len(rows),
        })
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/api/v1/song-ratings/dates", methods=["GET"])
    async def song_rating_api_dates():
        from quart import jsonify

        if (await db.get_setting("song_rating_api_enabled")) != "1":
            return jsonify({"error": "Song Rating API is disabled"}), 503
        configured_hash = await db.get_setting("song_rating_api_token_hash")
        authorization = request.headers.get("Authorization", "")
        supplied_token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
        if not supplied_token:
            supplied_token = request.headers.get("X-API-Key", "").strip()
        supplied_hash = hashlib.sha256(supplied_token.encode("utf-8")).hexdigest()
        if not configured_hash or not supplied_token or not hmac.compare_digest(
            configured_hash, supplied_hash
        ):
            return jsonify({"error": "Unauthorized"}), 401
        stats = await db.get_song_rating_api_stats(days=365)
        response = jsonify({
            "dates": [row["date"] for row in stats["snapshots"]],
            "snapshots": stats["snapshots"],
        })
        response.headers["Cache-Control"] = "no-store"
        return response

    # --- Reaction Roles ---

    @app.route("/reaction-roles", methods=["GET", "POST"])
    @permission_required('reaction_roles')
    async def reaction_roles():
        guild = get_guild()

        if request.method == "POST":
            form = await request.form
            action = form.get("action", "")

            if action == "create":
                if not bot or not bot.is_ready() or not guild:
                    await flash("Discord-Bot ist nicht verbunden.", "error")
                    return redirect(url_for("reaction_roles"))

                channel_id_raw = form.get("channel_id", "").strip()
                role_id_raw    = form.get("role_id", "").strip()
                emoji_tag      = form.get("emoji", "").strip()
                content        = form.get("content", "").strip()

                if not (channel_id_raw.isdigit() and role_id_raw.isdigit()
                        and emoji_tag and content):
                    await flash("Bitte alle Felder ausfüllen.", "error")
                    return redirect(url_for("reaction_roles"))

                channel_id = int(channel_id_raw)
                role_id    = int(role_id_raw)
                channel    = guild.get_channel(channel_id) or guild.get_thread(channel_id)
                role       = guild.get_role(role_id)
                if not channel or not role:
                    await flash("Channel oder Rolle nicht gefunden.", "error")
                    return redirect(url_for("reaction_roles"))

                # Resolve emoji from <:name:id> tag back to a guild emoji we own
                import re as _re
                m = _re.match(r"<a?:([A-Za-z0-9_]+):(\d+)>", emoji_tag)
                if not m:
                    await flash("Ungültiges Emoji.", "error")
                    return redirect(url_for("reaction_roles"))
                emoji_name = m.group(1)
                emoji_id   = int(m.group(2))
                emoji_obj  = next((e for e in guild.emojis if e.id == emoji_id), None)
                if not emoji_obj:
                    await flash("Emoji ist nicht (mehr) auf diesem Server verfügbar.", "error")
                    return redirect(url_for("reaction_roles"))

                # --- Resolve #channel-name → <#id> mentions ---
                channel_map = {}
                if guild:
                    for ch in guild.text_channels:
                        channel_map[ch.name.lower()] = ch.id
                    for ch in guild.voice_channels:
                        channel_map[ch.name.lower()] = ch.id
                    for ch in guild.forums:
                        channel_map[ch.name.lower()] = ch.id

                def resolve_channel_mentions(text):
                    import re as _re2
                    def _repl(m):
                        name = m.group(1).lower()
                        cid = channel_map.get(name)
                        return f"<#{cid}>" if cid else m.group(0)
                    return _re2.sub(r'#([a-zA-Z0-9_-]+)', _repl, text)

                # --- Resolve :emoji_name: → <:name:id> custom emoji mentions ---
                emoji_map = {}
                if guild:
                    for e in guild.emojis:
                        emoji_map[e.name.lower()] = str(e)

                def resolve_emoji_mentions(text):
                    import re as _re3
                    def _repl(m):
                        name = m.group(1).lower()
                        tag = emoji_map.get(name)
                        return tag if tag else m.group(0)
                    return _re3.sub(r':([A-Za-z0-9_]+):', _repl, text)

                content = resolve_channel_mentions(content)
                content = resolve_emoji_mentions(content)

                # --- Split content into ≤2000 char chunks at paragraph boundaries ---
                def split_message(text, limit=2000):
                    if len(text) <= limit:
                        return [text]
                    chunks = []
                    while text:
                        if len(text) <= limit:
                            chunks.append(text)
                            break
                        # Find last double-newline within limit
                        cut = text.rfind('\n\n', 0, limit)
                        if cut <= 0:
                            # Fall back to single newline
                            cut = text.rfind('\n', 0, limit)
                        if cut <= 0:
                            # Hard cut at limit
                            cut = limit
                        chunks.append(text[:cut].rstrip())
                        text = text[cut:].lstrip('\n')
                    return chunks

                chunks = split_message(content)

                # Post all chunks; react only on the last one
                sent_messages = []
                try:
                    for i, chunk in enumerate(chunks):
                        msg = await channel.send(chunk)
                        sent_messages.append(msg)
                    await msg.add_reaction(emoji_obj)
                except Exception as ex:
                    # Cleanup already-sent messages on failure
                    for m in sent_messages:
                        try:
                            await m.delete()
                        except Exception:
                            pass
                    await flash(f"Konnte Beitrag nicht senden: {ex}", "error")
                    return redirect(url_for("reaction_roles"))

                # Store: message_id = last message (the one with the reaction)
                # all_message_ids = comma-separated list of ALL message IDs for cleanup
                stored_emoji = str(emoji_obj)
                all_ids = ",".join(str(m.id) for m in sent_messages)
                try:
                    await db.add_reaction_role(
                        channel_id=channel_id,
                        message_id=msg.id,
                        role_id=role_id,
                        role_name=role.name,
                        emoji=stored_emoji,
                        emoji_id=emoji_id,
                        content=content,
                        all_message_ids=all_ids,
                    )
                except Exception as ex:
                    for m in sent_messages:
                        try:
                            await m.delete()
                        except Exception:
                            pass
                    await flash(f"Konnte Eintrag nicht speichern: {ex}", "error")
                    return redirect(url_for("reaction_roles"))

                await db.add_audit_log(
                    event_type="reaction_role_created",
                    details=f"#{channel.name} message {msg.id} → {role.name} via {emoji_name}",
                    actor=session.get("username", "unknown"),
                )
                await flash("Reaction-Role erstellt.", "success")
                return redirect(url_for("reaction_roles"))

            elif action == "delete":
                entry_id_raw = form.get("entry_id", "").strip()
                if not entry_id_raw.isdigit():
                    return redirect(url_for("reaction_roles"))
                entry = await db.delete_reaction_role(int(entry_id_raw))
                if entry and bot and bot.is_ready() and guild:
                    ch = guild.get_channel(entry["channel_id"]) or guild.get_thread(entry["channel_id"])
                    if ch:
                        # Delete ALL messages that belong to this reaction-role
                        ids_to_delete = []
                        all_ids_str = entry.get("all_message_ids", "")
                        if all_ids_str:
                            ids_to_delete = [int(x) for x in all_ids_str.split(",") if x.strip().isdigit()]
                        if not ids_to_delete:
                            ids_to_delete = [entry["message_id"]]
                        for mid in ids_to_delete:
                            try:
                                m = await ch.fetch_message(mid)
                                await m.delete()
                            except Exception:
                                pass
                if entry:
                    await db.add_audit_log(
                        event_type="reaction_role_deleted",
                        details=f"message {entry['message_id']} → {entry['role_name']}",
                        actor=session.get("username", "unknown"),
                    )
                    await flash("Reaction-Role gelöscht.", "success")
                return redirect(url_for("reaction_roles"))

            return redirect(url_for("reaction_roles"))

        # --- GET ---
        text_channels   = []
        available_roles = []
        guild_emojis    = []
        if guild:
            for ch in sorted(guild.text_channels, key=lambda c: c.position):
                text_channels.append({"id": ch.id, "name": ch.name})
            for r in guild.roles:
                if r.id != guild.default_role.id and not r.managed:
                    available_roles.append({"id": r.id, "name": r.name})
            for e in guild.emojis:
                if e.available:
                    # str(e) → "<:name:id>" / "<a:name:id>"
                    guild_emojis.append({"name": e.name, "tag": str(e)})

        rows = await db.get_all_reaction_roles()
        # Decorate with channel name & jump URL for the UI
        entries = []
        for r in rows:
            ch_name = None
            jump_url = None
            if guild:
                ch = guild.get_channel(r["channel_id"]) or guild.get_thread(r["channel_id"])
                if ch:
                    ch_name = ch.name
                    jump_url = f"https://discord.com/channels/{guild.id}/{r['channel_id']}/{r['message_id']}"
            entries.append({
                **r,
                "channel_name": ch_name,
                "jump_url": jump_url,
                # For display: just the emoji tag as-is (Discord renders custom emojis in plain text fine).
                "emoji_display": r["emoji"],
            })

        return await render_template(
            "reaction_roles.html",
            text_channels=text_channels,
            available_roles=available_roles,
            guild_emojis=guild_emojis,
            entries=entries,
            bot_ready=bool(bot and bot.is_ready() and guild),
        )

    # --- Card Collection ---

    @app.route("/card-collection", methods=["GET", "POST"])
    @permission_required('card_collection')
    async def card_collection_admin():
        import uuid

        allowed_extensions = {"png", "jpg", "jpeg", "webp"}
        rarities = ["Common", "Uncommon", "Rare", "Epic", "Legendary"]
        allowed_rarities = set(rarities)

        if request.method == "POST":
            form = await request.form
            action = form.get("action", "")
            card_id_raw = form.get("card_id", "").strip()
            card_id = int(card_id_raw) if card_id_raw.isdigit() else None

            if action == "save_rarity_weights":
                weights = {}
                for rarity in rarities:
                    try:
                        weights[rarity] = max(
                            0.01,
                            min(
                                1_000_000.0,
                                float(form.get(f"rarity_weight_{rarity.lower()}", "")),
                            ),
                        )
                    except (TypeError, ValueError):
                        await flash(f"Enter a valid positive weight for {rarity}.", "error")
                        return redirect(url_for("card_collection_admin"))
                await db.save_collectible_rarity_weights(weights)
                await db.add_audit_log(
                    event_type="collectible_rarity_weights_saved",
                    details="Card rarity base weights updated",
                    actor=session.get("username", "unknown"),
                )
                await flash("Rarity weights saved.", "success")
                return redirect(url_for("card_collection_admin"))

            if action == "delete" and card_id:
                filename = await db.delete_collectible_card(card_id)
                if filename:
                    path = os.path.join(card_image_dir, os.path.basename(filename))
                    if os.path.isfile(path):
                        os.remove(path)
                    await db.add_audit_log(
                        event_type="collectible_card_deleted",
                        details=f"Card #{card_id} deleted",
                        actor=session.get("username", "unknown"),
                    )
                    await flash("Card deleted.", "success")
                return redirect(url_for("card_collection_admin"))

            if action == "toggle" and card_id:
                card = await db.get_collectible_card(card_id)
                if card:
                    active = 0 if card.get("active") else 1
                    await db.save_collectible_card(card_id, active=active)
                    await flash(
                        f"Card {'activated' if active else 'deactivated'}.", "success"
                    )
                return redirect(url_for("card_collection_admin"))

            if action == "save":
                existing = await db.get_collectible_card(card_id) if card_id else None
                if card_id and not existing:
                    await flash("Card not found.", "error")
                    return redirect(url_for("card_collection_admin"))

                name = form.get("name", "").strip()[:100]
                if not name:
                    await flash("Card name is required.", "error")
                    return redirect(url_for("card_collection_admin", edit=card_id or ""))

                image_filename = existing.get("image_filename", "") if existing else ""
                files = await request.files
                image_file = files.get("image")
                old_image = image_filename
                if image_file and image_file.filename:
                    original = image_file.filename.strip()
                    ext = original.rsplit(".", 1)[-1].lower() if "." in original else ""
                    if ext not in allowed_extensions:
                        await flash("Use a PNG, JPEG or WebP card image.", "error")
                        return redirect(url_for("card_collection_admin", edit=card_id or ""))
                    stored_ext = "jpg" if ext == "jpeg" else ext
                    image_filename = f"card_{uuid.uuid4().hex}.{stored_ext}"
                    image_path = os.path.join(card_image_dir, image_filename)
                    await image_file.save(image_path)
                    try:
                        if os.path.getsize(image_path) > 15 * 1024 * 1024:
                            raise ValueError("Card image is larger than 15 MB.")
                        with open(image_path, "rb") as handle:
                            header = handle.read(16)
                        valid = (
                            (stored_ext == "png" and header.startswith(b"\x89PNG\r\n\x1a\n"))
                            or (stored_ext == "jpg" and header.startswith(b"\xff\xd8\xff"))
                            or (
                                stored_ext == "webp"
                                and header.startswith(b"RIFF")
                                and header[8:12] == b"WEBP"
                            )
                        )
                        if not valid:
                            raise ValueError("The uploaded card image is invalid.")
                    except (OSError, ValueError) as exc:
                        if os.path.isfile(image_path):
                            os.remove(image_path)
                        await flash(str(exc), "error")
                        return redirect(url_for("card_collection_admin", edit=card_id or ""))

                if not image_filename:
                    await flash("A card image is required.", "error")
                    return redirect(url_for("card_collection_admin", edit=card_id or ""))

                rarity = form.get("rarity", "Common")
                if rarity not in allowed_rarities:
                    rarity = "Common"
                try:
                    draw_weight = max(
                        0.01,
                        min(100000.0, float(form.get("draw_weight", "1"))),
                    )
                except (TypeError, ValueError):
                    draw_weight = 1.0

                saved_id = await db.save_collectible_card(
                    card_id,
                    name=name,
                    rarity=rarity,
                    draw_weight=draw_weight,
                    deck=form.get("deck", "").strip()[:100],
                    image_filename=image_filename,
                    active=1 if form.get("active") else 0,
                )
                if old_image and old_image != image_filename:
                    old_path = os.path.join(card_image_dir, os.path.basename(old_image))
                    if os.path.isfile(old_path):
                        os.remove(old_path)
                await db.add_audit_log(
                    event_type="collectible_card_saved",
                    details=f"Card #{saved_id}: {name}",
                    actor=session.get("username", "unknown"),
                )
                await flash("Card saved.", "success")
                return redirect(url_for("card_collection_admin", edit=saved_id))

        cards = await db.get_collectible_cards(include_inactive=True)
        rarity_weights = await db.get_collectible_rarity_weights()
        total_effective_weight = sum(
            max(0.0, float(card.get("draw_weight") or 0))
            * rarity_weights.get(card.get("rarity"), 1.0)
            for card in cards
            if card.get("active")
        )
        for card in cards:
            effective_weight = (
                max(0.0, float(card.get("draw_weight") or 0))
                * rarity_weights.get(card.get("rarity"), 1.0)
            )
            card["effective_weight"] = effective_weight
            card["draw_probability"] = (
                effective_weight / total_effective_weight * 100.0
                if card.get("active") and total_effective_weight > 0
                else 0.0
            )
        edit_raw = request.args.get("edit", "").strip()
        edit_card = (
            await db.get_collectible_card(int(edit_raw)) if edit_raw.isdigit() else None
        )
        return await render_template(
            "card_collection.html",
            cards=cards,
            edit_card=edit_card,
            rarities=rarities,
            rarity_weights=rarity_weights,
        )

    # --- Community Event Registration ---

    @app.route("/event-registration", methods=["GET", "POST"])
    @permission_required("event_registration")
    async def event_registration_admin():
        allowed_extensions = {"png", "jpg", "jpeg", "webp"}

        if request.method == "POST":
            form = await request.form
            action = form.get("action", "")
            event_id_raw = form.get("event_id", "").strip()
            event_id = int(event_id_raw) if event_id_raw.isdigit() else None

            if action == "delete" and event_id:
                image_filename = await db.delete_community_event(event_id)
                if image_filename:
                    image_path = os.path.join(
                        event_image_dir, os.path.basename(image_filename)
                    )
                    if os.path.isfile(image_path):
                        os.remove(image_path)
                    await db.add_audit_log(
                        event_type="community_event_deleted",
                        details=f"Event #{event_id} deleted",
                        actor=session.get("username", "unknown"),
                    )
                    await flash("Event deleted.", "success")
                else:
                    await flash("Event not found.", "error")
                return redirect(url_for("event_registration_admin"))

            if action == "toggle" and event_id:
                event = await db.get_community_event(event_id)
                if not event:
                    await flash("Event not found.", "error")
                else:
                    active = 0 if event.get("active") else 1
                    await db.save_community_event(event_id, active=active)
                    await db.add_audit_log(
                        event_type="community_event_status_changed",
                        details=(
                            f"Event #{event_id} "
                            f"{'activated' if active else 'deactivated'}"
                        ),
                        actor=session.get("username", "unknown"),
                    )
                    await flash(
                        f"Event {'activated' if active else 'deactivated'}.",
                        "success",
                    )
                return redirect(url_for("event_registration_admin"))

            if action == "save":
                existing = await db.get_community_event(event_id) if event_id else None
                if event_id and not existing:
                    await flash("Event not found.", "error")
                    return redirect(url_for("event_registration_admin"))

                name = form.get("name", "").strip()[:100]
                description = form.get("description", "").strip()[:3500]
                event_at_raw = form.get("event_at", "").strip()
                if not name:
                    await flash("Event name is required.", "error")
                    return redirect(
                        url_for("event_registration_admin", edit=event_id or "")
                    )
                if not description:
                    await flash("Event description is required.", "error")
                    return redirect(
                        url_for("event_registration_admin", edit=event_id or "")
                    )
                try:
                    event_local = datetime.fromisoformat(event_at_raw)
                    if event_local.tzinfo is None:
                        event_local = event_local.replace(
                            tzinfo=ZoneInfo("Europe/Berlin")
                        )
                    event_at = event_local.timestamp()
                except (TypeError, ValueError):
                    await flash("Enter a valid event date and time.", "error")
                    return redirect(
                        url_for("event_registration_admin", edit=event_id or "")
                    )

                image_filename = (
                    existing.get("image_filename", "") if existing else ""
                )
                old_image = image_filename
                files = await request.files
                image_file = files.get("image")
                if image_file and image_file.filename:
                    original = image_file.filename.strip()
                    ext = original.rsplit(".", 1)[-1].lower() if "." in original else ""
                    if ext not in allowed_extensions:
                        await flash("Use a PNG, JPEG or WebP event image.", "error")
                        return redirect(
                            url_for("event_registration_admin", edit=event_id or "")
                        )
                    stored_ext = "jpg" if ext == "jpeg" else ext
                    image_filename = (
                        f"event_{secrets.token_hex(16)}.{stored_ext}"
                    )
                    image_path = os.path.join(event_image_dir, image_filename)
                    await image_file.save(image_path)
                    try:
                        if os.path.getsize(image_path) > 8 * 1024 * 1024:
                            raise ValueError("Event image is larger than 8 MB.")
                        with open(image_path, "rb") as handle:
                            header = handle.read(16)
                        valid = (
                            (stored_ext == "png" and header.startswith(b"\x89PNG\r\n\x1a\n"))
                            or (stored_ext == "jpg" and header.startswith(b"\xff\xd8\xff"))
                            or (
                                stored_ext == "webp"
                                and header.startswith(b"RIFF")
                                and header[8:12] == b"WEBP"
                            )
                        )
                        if not valid:
                            raise ValueError("The uploaded event image is invalid.")
                    except (OSError, ValueError) as exc:
                        if os.path.isfile(image_path):
                            os.remove(image_path)
                        await flash(str(exc), "error")
                        return redirect(
                            url_for("event_registration_admin", edit=event_id or "")
                        )

                if not image_filename:
                    await flash("An event image is required.", "error")
                    return redirect(
                        url_for("event_registration_admin", edit=event_id or "")
                    )

                saved_id = await db.save_community_event(
                    event_id,
                    name=name,
                    description=description,
                    event_at=event_at,
                    image_filename=image_filename,
                    active=1 if form.get("active") else 0,
                )
                if old_image and old_image != image_filename:
                    old_path = os.path.join(
                        event_image_dir, os.path.basename(old_image)
                    )
                    if os.path.isfile(old_path):
                        os.remove(old_path)
                await db.add_audit_log(
                    event_type="community_event_saved",
                    details=f"Event #{saved_id}: {name}",
                    actor=session.get("username", "unknown"),
                )
                await flash("Event saved.", "success")
                return redirect(url_for("event_registration_admin", edit=saved_id))

        events = await db.get_community_events(include_inactive=True)
        for event in events:
            event["event_local"] = datetime.fromtimestamp(
                float(event["event_at"]), tz=ZoneInfo("Europe/Berlin")
            )
            event["participants"] = await db.get_community_event_participants(
                event["id"]
            )
            for participant in event["participants"]:
                participant["joined_local"] = datetime.fromtimestamp(
                    float(participant["joined_at"]),
                    tz=ZoneInfo("Europe/Berlin"),
                )

        edit_raw = request.args.get("edit", "").strip()
        edit_event = (
            await db.get_community_event(int(edit_raw))
            if edit_raw.isdigit()
            else None
        )
        if edit_event:
            edit_event["event_input"] = datetime.fromtimestamp(
                float(edit_event["event_at"]), tz=ZoneInfo("Europe/Berlin")
            ).strftime("%Y-%m-%dT%H:%M")

        return await render_template(
            "event_registration.html",
            events=events,
            edit_event=edit_event,
            stats={
                "total": len(events),
                "active": sum(1 for event in events if event.get("active")),
                "participants": sum(
                    int(event.get("participant_count") or 0) for event in events
                ),
            },
        )

    # --- Birthday Calendar ---

    @app.route("/birthday-calendar", methods=["GET", "POST"])
    @permission_required("birthday_calendar")
    async def birthday_calendar_admin():
        from datetime import date, datetime
        from zoneinfo import ZoneInfo
        from bot.cogs.birthdays import next_birthday

        guild = get_guild()
        text_channels = []
        if guild:
            text_channels = sorted(
                ({"id": channel.id, "name": channel.name} for channel in guild.text_channels),
                key=lambda channel: channel["name"].lower(),
            )

        if request.method == "POST":
            form = await request.form
            action = form.get("action", "")

            if action == "save_settings":
                channel_id = form.get("notification_channel_id", "").strip()
                known_channels = {str(channel["id"]) for channel in text_channels}
                if channel_id and channel_id not in known_channels:
                    await flash("Select a valid notification channel.", "error")
                else:
                    await db.set_setting("birthday_notification_channel_id", channel_id)
                    await db.add_audit_log(
                        event_type="birthday_settings_saved",
                        details=f"Notification channel: {channel_id or 'disabled'}",
                        actor=session.get("username", "unknown"),
                    )
                    await flash("Birthday notification settings saved.", "success")
                return redirect(url_for("birthday_calendar_admin"))

            user_id_raw = form.get("user_id", "").strip()
            if not user_id_raw.isdigit():
                await flash("Birthday record not found.", "error")
                return redirect(url_for("birthday_calendar_admin"))
            user_id = int(user_id_raw)

            if action == "delete":
                if await db.delete_birthday(user_id):
                    await db.add_audit_log(
                        event_type="birthday_deleted",
                        user_id=user_id,
                        details="Birthday removed by administrator",
                        actor=session.get("username", "unknown"),
                    )
                    await flash("Birthday removed.", "success")
                return redirect(url_for("birthday_calendar_admin"))

            if action == "edit":
                birthday = await db.get_birthday(user_id)
                if not birthday:
                    await flash("Birthday record not found.", "error")
                    return redirect(url_for("birthday_calendar_admin"))
                try:
                    day = int(form.get("birth_day", ""))
                    month = int(form.get("birth_month", ""))
                    year_raw = form.get("birth_year", "").strip()
                    year = int(year_raw) if year_raw else None
                    current_year = datetime.now(ZoneInfo("Europe/Berlin")).year
                    if year is not None and not 1900 <= year <= current_year:
                        raise ValueError
                    date(year or 2000, month, day)
                except (TypeError, ValueError):
                    await flash("Enter a valid birthday.", "error")
                    return redirect(url_for("birthday_calendar_admin"))

                await db.save_birthday(
                    user_id=user_id,
                    user_name=form.get("user_name", birthday["user_name"]).strip()[:100]
                    or birthday["user_name"],
                    display_name=form.get(
                        "display_name", birthday["display_name"]
                    ).strip()[:100]
                    or birthday["display_name"],
                    birth_day=day,
                    birth_month=month,
                    birth_year=year,
                )
                await db.add_audit_log(
                    event_type="birthday_updated",
                    user_id=user_id,
                    details=f"Birthday set to {day:02d}.{month:02d}.",
                    actor=session.get("username", "unknown"),
                )
                await flash("Birthday updated.", "success")
                return redirect(url_for("birthday_calendar_admin"))

        today = datetime.now(ZoneInfo("Europe/Berlin")).date()
        birthdays = await db.get_birthdays()
        for birthday in birthdays:
            occurrence = next_birthday(
                birthday["birth_day"], birthday["birth_month"], today
            )
            birthday["next_date"] = occurrence
            birthday["days_until"] = (occurrence - today).days
            birthday["turning_age"] = (
                occurrence.year - birthday["birth_year"]
                if birthday.get("birth_year")
                else None
            )
        birthdays.sort(
            key=lambda birthday: (
                birthday["days_until"], birthday["display_name"].lower()
            )
        )

        return await render_template(
            "birthday_calendar.html",
            birthdays=birthdays,
            upcoming=birthdays[:6],
            text_channels=text_channels,
            current_year=today.year,
            notification_channel_id=await db.get_setting(
                "birthday_notification_channel_id"
            ),
        )

    # --- Personal Reminders ---

    @app.route("/reminders", methods=["GET", "POST"])
    @permission_required("reminders")
    async def reminders_admin():
        from datetime import datetime
        from bot.reminder_schedule import (
            BERLIN_TZ,
            RECURRENCE_LABELS,
            ensure_future_recurrence,
            parse_reminder_datetime,
            reminder_datetime_from_timestamp,
        )

        if request.method == "POST":
            form = await request.form
            action = form.get("action", "")
            reminder_id_raw = form.get("reminder_id", "").strip()
            if not reminder_id_raw.isdigit():
                await flash("Reminder not found.", "error")
                return redirect(url_for("reminders_admin"))
            reminder_id = int(reminder_id_raw)

            if action == "delete":
                if await db.delete_reminder(reminder_id):
                    await db.add_audit_log(
                        event_type="reminder_deleted",
                        details=f"Reminder #{reminder_id} deleted",
                        actor=session.get("username", "unknown"),
                    )
                    await flash("Reminder deleted.", "success")
                return redirect(url_for("reminders_admin"))

            if action == "edit":
                reminder = await db.get_reminder(reminder_id)
                if not reminder:
                    await flash("Reminder not found.", "error")
                    return redirect(url_for("reminders_admin"))
                reminder_text = form.get("reminder_text", "").strip()[:1000]
                recurrence = form.get("recurrence", "once")
                active = bool(form.get("active"))
                if not reminder_text:
                    await flash("Reminder text is required.", "error")
                    return redirect(url_for("reminders_admin"))
                if recurrence not in RECURRENCE_LABELS:
                    recurrence = "once"
                try:
                    scheduled = parse_reminder_datetime(
                        form.get("run_date", ""),
                        form.get("run_time", ""),
                    )
                    if active:
                        scheduled = ensure_future_recurrence(
                            scheduled,
                            recurrence,
                            now=datetime.now(BERLIN_TZ),
                        )
                except ValueError as exc:
                    await flash(str(exc), "error")
                    return redirect(url_for("reminders_admin"))

                await db.update_reminder(
                    reminder_id,
                    reminder_text=reminder_text,
                    next_run_at=scheduled.timestamp(),
                    recurrence=recurrence,
                    anchor_day=scheduled.day,
                    active=active,
                )
                await db.add_audit_log(
                    event_type="reminder_updated",
                    user_id=reminder["user_id"],
                    details=f"Reminder #{reminder_id} updated",
                    actor=session.get("username", "unknown"),
                )
                await flash("Reminder updated.", "success")
                return redirect(url_for("reminders_admin"))

        reminders = await db.get_reminders(include_inactive=True)
        for reminder in reminders:
            reminder["next_run"] = reminder_datetime_from_timestamp(
                reminder["next_run_at"]
            )
            reminder["recurrence_label"] = RECURRENCE_LABELS.get(
                reminder["recurrence"], reminder["recurrence"]
            )
        stats = {
            "total": len(reminders),
            "active": sum(1 for reminder in reminders if reminder["active"]),
            "recurring": sum(
                1
                for reminder in reminders
                if reminder["active"] and reminder["recurrence"] != "once"
            ),
            "errors": sum(
                1
                for reminder in reminders
                if reminder["active"] and reminder.get("last_error")
            ),
        }
        return await render_template(
            "reminders.html",
            reminders=reminders,
            recurrence_labels=RECURRENCE_LABELS,
            stats=stats,
        )

    # --- Image Posting ---

    UPLOAD_DIR = os.path.join(os.path.dirname(db.db_path), "uploads")
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}

    @app.route("/uploads/<filename>")
    @login_required
    async def uploaded_image(filename):
        from quart import send_from_directory
        return await send_from_directory(UPLOAD_DIR, filename)

    @app.route("/image-posting", methods=["GET", "POST"])
    @permission_required('image_posting')
    async def image_posting():
        if request.method == "POST":
            form = await request.form
            action = form.get("action")

            if action == "add_category":
                name = form.get("category_name", "").strip()
                if name:
                    try:
                        await db.add_image_category(name)
                    except Exception:
                        await flash("Category already exists.", "error")
                return redirect(url_for("image_posting"))

            elif action == "delete_category":
                cat_id = int(form.get("category_id", 0))
                # Delete associated files
                images = await db.get_image_posts(category_id=cat_id)
                for img in images:
                    filepath = os.path.join(UPLOAD_DIR, img["filename"])
                    if os.path.exists(filepath):
                        os.remove(filepath)
                await db.delete_image_category(cat_id)
                return redirect(url_for("image_posting"))

            elif action == "upload_image":
                files = await request.files
                image_file = files.get("image")
                title = form.get("title", "").strip()[:30]
                description = form.get("description", "").strip()[:400]
                category_id = int(form.get("category_id", 0))

                if not image_file or not title or not category_id:
                    await flash("Title, category and image are required.", "error")
                    return redirect(url_for("image_posting"))

                ext = image_file.filename.rsplit(".", 1)[-1].lower() if "." in image_file.filename else ""
                if ext not in ALLOWED_EXTENSIONS:
                    await flash(f"Invalid file type. Allowed: {', '.join(ALLOWED_EXTENSIONS)}", "error")
                    return redirect(url_for("image_posting"))

                import uuid
                filename = f"{uuid.uuid4().hex}.{ext}"
                filepath = os.path.join(UPLOAD_DIR, filename)
                await image_file.save(filepath)

                await db.add_image_post(title, description, category_id, filename)
                return redirect(url_for("image_posting", category=category_id))

            elif action == "delete_image":
                image_id = int(form.get("image_id", 0))
                filename = await db.delete_image_post(image_id)
                if filename:
                    filepath = os.path.join(UPLOAD_DIR, filename)
                    if os.path.exists(filepath):
                        os.remove(filepath)
                return redirect(request.referrer or url_for("image_posting"))

        # GET
        import traceback
        try:
            categories = await db.get_image_categories()
            # Add image count per category
            all_images = await db.get_image_posts()
            cat_counts = {}
            for img in all_images:
                cat_counts[img["category_id"]] = cat_counts.get(img["category_id"], 0) + 1
            for cat in categories:
                cat["count"] = cat_counts.get(cat["id"], 0)

            filter_category = request.args.get("category", type=int)
            filter_category_name = None
            if filter_category:
                for cat in categories:
                    if cat["id"] == filter_category:
                        filter_category_name = cat["name"]
                        break

            images = await db.get_image_posts(category_id=filter_category)

            return await render_template(
                "image_posting.html",
                categories=categories,
                images=images,
                filter_category=filter_category,
                filter_category_name=filter_category_name,
            )
        except Exception as e:
            traceback.print_exc()
            return f"<pre>Error: {e}\n\n{traceback.format_exc()}</pre>", 500

    # --- Polls ---

    NUMBER_EMOJIS = ["1\u20e3", "2\u20e3", "3\u20e3", "4\u20e3", "5\u20e3", "6\u20e3", "7\u20e3", "8\u20e3", "9\u20e3", "\U0001F51F"]

    @app.route("/polls", methods=["GET", "POST"])
    @permission_required('polls')
    async def polls():
        import json as _json
        if request.method == "POST":
            form = await request.form
            action = form.get("action", "")

            if action == "create":
                title = form.get("title", "").strip()
                description = form.get("description", "").strip()
                raw_options = form.get("options", "").strip()
                options_list = [o.strip() for o in raw_options.split("\n") if o.strip()]
                if not title:
                    await flash("Title is required.", "error")
                    return redirect(url_for("polls"))
                if len(options_list) < 2:
                    await flash("At least 2 options are required.", "error")
                    return redirect(url_for("polls"))
                if len(options_list) > 10:
                    await flash("Maximum 10 options allowed.", "error")
                    return redirect(url_for("polls"))

                image_filename = None
                files = await request.files
                image_file = files.get("image")
                if image_file and image_file.filename:
                    ext = image_file.filename.rsplit(".", 1)[-1].lower()
                    if ext in ALLOWED_EXTENSIONS:
                        import uuid
                        image_filename = f"poll_{uuid.uuid4().hex}.{ext}"
                        filepath = os.path.join(UPLOAD_DIR, image_filename)
                        await image_file.save(filepath)

                poll_id = await db.create_poll(
                    title,
                    description,
                    _json.dumps(options_list),
                    image_filename,
                    creator_id=session.get("user_id"),
                    creator_name=session.get("username", "unknown"),
                )
                await flash(f"Poll #{poll_id} created.", "success")
                return redirect(url_for("polls"))

            elif action == "edit":
                poll_id = int(form.get("poll_id", 0))
                title = form.get("title", "").strip()
                description = form.get("description", "").strip()
                raw_options = form.get("options", "").strip()
                options_list = [o.strip() for o in raw_options.split("\n") if o.strip()]
                if not title or len(options_list) < 2:
                    await flash("Title and at least 2 options are required.", "error")
                    return redirect(url_for("polls"))
                if len(options_list) > 10:
                    await flash("Maximum 10 options allowed.", "error")
                    return redirect(url_for("polls"))

                image_filename = None
                files = await request.files
                image_file = files.get("image")
                if image_file and image_file.filename:
                    ext = image_file.filename.rsplit(".", 1)[-1].lower()
                    if ext in ALLOWED_EXTENSIONS:
                        import uuid
                        image_filename = f"poll_{uuid.uuid4().hex}.{ext}"
                        filepath = os.path.join(UPLOAD_DIR, image_filename)
                        await image_file.save(filepath)
                        # Remove old image
                        old_poll = await db.get_poll(poll_id)
                        if old_poll and old_poll.get("image_filename"):
                            old_path = os.path.join(UPLOAD_DIR, old_poll["image_filename"])
                            if os.path.exists(old_path):
                                os.remove(old_path)

                await db.update_poll(poll_id, title, description, _json.dumps(options_list), image_filename)
                await flash("Poll updated.", "success")
                return redirect(url_for("polls"))

            elif action == "delete":
                poll_id = int(form.get("poll_id", 0))
                filename = await db.delete_poll(poll_id)
                if filename:
                    filepath = os.path.join(UPLOAD_DIR, filename)
                    if os.path.exists(filepath):
                        os.remove(filepath)
                await flash("Poll deleted.", "success")
                return redirect(url_for("polls"))

            elif action == "post":
                poll_id = int(form.get("poll_id", 0))
                channel_id_str = form.get("channel_id", "").strip()
                poll = await db.get_poll(poll_id)
                if not poll:
                    await flash("Poll not found.", "error")
                    return redirect(url_for("polls"))
                if not channel_id_str:
                    await flash("No channel selected.", "error")
                    return redirect(url_for("polls"))
                guild = get_guild()
                if not guild:
                    await flash("Bot is not connected.", "error")
                    return redirect(url_for("polls"))
                import discord
                ch_id = int(channel_id_str)
                channel = guild.get_channel(ch_id) or guild.get_thread(ch_id)
                if not channel:
                    await flash("Channel not found.", "error")
                    return redirect(url_for("polls"))

                options_list = _json.loads(poll["options"])
                options_text = "\n".join(f"{NUMBER_EMOJIS[i]}  {opt}" for i, opt in enumerate(options_list))
                embed = discord.Embed(
                    title=f"\U0001F4CA {poll['title']}",
                    description=f"{poll['description']}\n\n{options_text}" if poll["description"] else options_text,
                    color=discord.Color.blue(),
                )
                file = None
                if poll.get("image_filename"):
                    filepath = os.path.join(UPLOAD_DIR, poll["image_filename"])
                    if os.path.exists(filepath):
                        file = discord.File(filepath, filename=poll["image_filename"])
                        embed.set_image(url=f"attachment://{poll['image_filename']}")
                bot_name = await db.get_setting("bot_name") or "Slowmode Bot"
                embed.set_footer(text=f"{bot_name} — Poll")
                try:
                    send_kwargs = {"embed": embed}
                    if file:
                        send_kwargs["file"] = file
                    if isinstance(channel, discord.ForumChannel):
                        thread, msg = await channel.create_thread(
                            name=f"\U0001F4CA {poll['title']}",
                            **send_kwargs,
                        )
                    else:
                        msg = await channel.send(**send_kwargs)
                    for i in range(len(options_list)):
                        await msg.add_reaction(NUMBER_EMOJIS[i])
                    await db.update_poll_message(poll_id, channel.id, msg.id)
                    await flash(f"Poll posted to #{channel.name}.", "success")
                except Exception as post_err:
                    await flash(f"Failed to post: {post_err}", "error")
                return redirect(url_for("polls"))

            elif action == "close":
                poll_id = int(form.get("poll_id", 0))
                await db.close_poll(poll_id)
                await flash("Poll closed.", "success")
                return redirect(url_for("polls"))

        # GET
        all_polls = await db.get_all_polls()
        web_users = await db.get_all_web_users()
        web_user_names = {u["id"]: u["username"] for u in web_users}
        guild = get_guild()
        discord_creator_names = {}
        for p in all_polls:
            p["options_list"] = _json.loads(p["options"])
            creator_id = p.get("creator_id")
            stored_creator_name = (p.get("creator_name") or "").strip()
            if stored_creator_name:
                p["creator_name"] = stored_creator_name
            elif creator_id in web_user_names:
                p["creator_name"] = web_user_names[creator_id]
            elif creator_id:
                if creator_id not in discord_creator_names:
                    member = guild.get_member(creator_id) if guild else None
                    if not member and guild:
                        try:
                            member = await guild.fetch_member(creator_id)
                        except Exception:
                            member = None
                    if not member and bot:
                        user = bot.get_user(creator_id)
                        if not user:
                            try:
                                user = await bot.fetch_user(creator_id)
                            except Exception:
                                user = None
                        member = user
                    discord_creator_names[creator_id] = (
                        str(member) if member else f"Discord user {creator_id}"
                    )
                p["creator_name"] = discord_creator_names[creator_id]
            else:
                p["creator_name"] = "Unknown"
        text_channels = []
        if guild:
            for ch in sorted(guild.text_channels, key=lambda c: c.position):
                text_channels.append({"id": ch.id, "name": ch.name})
            import discord as _disc
            for ch in sorted([c for c in guild.channels if isinstance(c, _disc.ForumChannel)], key=lambda c: c.position):
                text_channels.append({"id": ch.id, "name": f"\U0001F4AC {ch.name}"})
                for thread in sorted(ch.threads, key=lambda t: t.created_at or t.id, reverse=True):
                    if not thread.archived:
                        text_channels.append({"id": thread.id, "name": f"  \u2514 {thread.name}"})
        return await render_template("polls.html", polls=all_polls, text_channels=text_channels)

    # --- Shared helpers ---

    async def _fetch_youtube_info(url: str) -> tuple[str | None, str | None, str | None]:
        """Fetch title, author and thumbnail from a YouTube URL via oEmbed.
        Returns (title, author, thumbnail_url)."""
        try:
            oembed = f"https://www.youtube.com/oembed?url={url}&format=json"
            async with aiohttp.ClientSession() as sess:
                async with sess.get(oembed, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return None, None, None
                    data = await resp.json()
                    title  = data.get("title")
                    author = data.get("author_name")
                    m = YOUTUBE_URL_RE.search(url)
                    thumb = f"https://img.youtube.com/vi/{m.group(1)}/hqdefault.jpg" if m else None
                    return title, author, thumb
        except Exception:
            return None, None, None

    async def _fetch_suno_info(url: str) -> tuple[str | None, str | None, str | None]:
        """Fetch song title, artist and image from a Suno URL. Returns (title, artist, image_url)."""
        import html as _html
        try:
            song_id = None
            m_id = re.search(r'suno\.com/(?:s|song)/([A-Za-z0-9_-]+)', url or "")
            if m_id:
                song_id = m_id.group(1)
            async with aiohttp.ClientSession() as sess:
                async with sess.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return None, None, None
                    page_html = await resp.text()
                    # Extract og:image
                    image_url = None
                    img_match = re.search(r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', page_html)
                    if not img_match:
                        img_match = re.search(r'<meta\s+content=["\']([^"\']+)["\']\s+property=["\']og:image["\']', page_html)
                    if img_match:
                        image_url = img_match.group(1).strip()
                    artist = _extract_suno_clip_owner_display_name(page_html, song_id)
                    # <title> format: "Song Title by Artist Name | Suno"
                    match = re.search(r'<title>([^<]+)</title>', page_html)
                    if match:
                        raw = _html.unescape(match.group(1).strip())
                        raw = re.sub(r'\s*[|\-\u2013]\s*Suno$', '', raw).strip()
                        by_match = re.search(r'^(.+?)\s+by\s+(.+)$', raw)
                        if by_match:
                            title = by_match.group(1).strip()
                            return title, artist or by_match.group(2).strip(), image_url
                        return raw, artist, image_url
        except Exception:
            pass
        return None, None, None

    # --- Radio ---

    RADIO_UPLOAD_DIR = os.path.join(os.path.dirname(db.db_path), "radio")
    os.makedirs(RADIO_UPLOAD_DIR, exist_ok=True)

    EXP_RADIO_DIR = os.path.join(os.path.dirname(db.db_path), "radio", "exp_radio")
    for _sub in ("mp3", "ass", "assets"):
        os.makedirs(os.path.join(EXP_RADIO_DIR, _sub), exist_ok=True)

    TRYA_STREAM_DIR = os.path.abspath(Config.TRYA_STREAM_DIR)
    for _sub in ("mp3", "ass", "assets"):
        os.makedirs(os.path.join(TRYA_STREAM_DIR, _sub), exist_ok=True)

    TRYA_DCS_DIR = os.path.abspath(Config.TRYA_DCS_DIR)
    for _sub in ("incoming", "originals", "mp3", "ass", "assets", "cover_cache", "hooks"):
        os.makedirs(os.path.join(TRYA_DCS_DIR, _sub), exist_ok=True)

    RIGHTS_DECLARATION_TEXT = (
        "I hereby confirm that I am the creator or rights holder of this audio track "
        "and grant a non-exclusive streaming license for a period of 14 days from the "
        "date of upload. This license covers live streaming on Twitch and the storage of "
        "VODs (Video on Demand) that include this track. After 14 days, the file will be "
        "automatically deleted from the server."
    )

    CONTENT_GUIDELINES_TEXT = (
        "By uploading, you also confirm that your track does not contain any of the following: "
        "extremist, radical, or politically motivated content (whether left-wing or right-wing); "
        "glorification or incitement of violence, hatred, or discrimination against individuals or groups; "
        "graphic, cruel, disturbing, or otherwise harmful material; "
        "content that violates applicable laws, Twitch Community Guidelines, or basic standards of decency. "
        "Submissions that violate these guidelines will be removed without notice and may result in a permanent upload ban."
    )

    MAX_UPLOAD_SIZE_MB = 20
    MAX_BITRATE_KBPS = 320
    MAX_UPLOADS_PER_IP = 3

    def _format_duration_limit(seconds: int) -> str:
        minutes, remainder = divmod(seconds, 60)
        if remainder:
            return f"{minutes} minute(s) {remainder} second(s)"
        return f"{minutes} minute(s)"

    async def _validate_mp3(filepath: str, max_duration_sec: int) -> dict:
        """Validate an MP3 file. Returns dict with info or 'error' key."""
        import asyncio, mimetypes, json as _json
        from bot.audio_utils import get_decoded_audio_duration
        # MIME type check
        mime, _ = mimetypes.guess_type(filepath)
        if mime not in ("audio/mpeg", "audio/mp3"):
            return {"error": "Invalid file type. Only MP3 files are allowed."}
        # File size
        size = os.path.getsize(filepath)
        if size > MAX_UPLOAD_SIZE_MB * 1024 * 1024:
            return {"error": f"File too large. Maximum {MAX_UPLOAD_SIZE_MB}MB."}
        # ffprobe
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_format", "-show_streams", filepath,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode != 0:
                return {"error": "Not a valid audio file."}
            info = _json.loads(stdout)
            fmt = info.get("format", {})
            bitrate = int(fmt.get("bit_rate", 0)) // 1000
            # Check for audio stream
            has_audio = any(s.get("codec_type") == "audio" for s in info.get("streams", []))
            if not has_audio:
                return {"error": "No audio stream found in file."}
            duration = await get_decoded_audio_duration(filepath)
            if duration <= 0:
                return {"error": "Could not determine the playable audio duration."}
            if duration > max_duration_sec:
                limit = _format_duration_limit(max_duration_sec)
                return {"error": f"Track too long. Maximum {limit}."}
            if duration < 5:
                return {"error": "Track too short. Minimum 5 seconds."}
            return {"duration": round(duration, 1), "bitrate": min(bitrate, MAX_BITRATE_KBPS), "size": size}
        except FileNotFoundError:
            return {"error": "Audio validation unavailable (ffprobe not found)."}
        except Exception as e:
            return {"error": f"Validation failed: {e}"}

    @app.route("/radio/upload", methods=["GET", "POST"])
    async def radio_upload():
        stream_name = (await db.get_setting("radio_stream_name") or "Twitch Radio").strip()
        try:
            max_per_user = max(1, int(await db.get_setting("radio_max_per_user") or "3"))
        except (TypeError, ValueError):
            max_per_user = 3
        try:
            max_duration_sec = max(60, int(await db.get_setting("radio_max_duration_seconds") or "360"))
        except (TypeError, ValueError):
            max_duration_sec = 360
        # Check if uploads are enabled
        upload_enabled = await db.get_setting("radio_upload_enabled")
        if upload_enabled == "0":
            return await render_template(
                "radio_upload.html", closed=True, stream_name=stream_name,
                max_per_user=max_per_user, max_duration_sec=max_duration_sec,
            )

        if request.method == "POST":
            import hashlib, tempfile, uuid, time
            from bot.exp_radio_worker import download_mp3, scrape_suno
            form = await request.form
            suno_url = form.get("suno_url", "").strip()
            rights_agreed = form.get("rights_agreed")

            # Honeypot check before making any outbound request.
            if form.get("website", ""):
                return redirect(url_for("radio_upload"))

            if not rights_agreed:
                await flash("You must agree to the streaming rights declaration.", "error")
                return redirect(url_for("radio_upload"))

            if not suno_url:
                await flash("Please provide the Suno URL.", "error")
                return redirect(url_for("radio_upload"))

            id_match = re.search(r'https?://(?:www\.)?suno\.com/(?:s|song)/([A-Za-z0-9_-]+)', suno_url)
            if not id_match:
                await flash("Please provide a valid Suno song URL or short link.", "error")
                return redirect(url_for("radio_upload"))

            # Rate limiting
            client_ip = request.headers.get("X-Forwarded-For", request.remote_addr) or "unknown"
            upload_count = await db.count_radio_uploads_by_ip(client_ip)
            if upload_count >= MAX_UPLOADS_PER_IP:
                await flash("Upload limit reached. Please try again later.", "error")
                return redirect(url_for("radio_upload"))

            submitted_id = id_match.group(1)
            meta = await scrape_suno(submitted_id)
            real_uuid = meta.get("real_uuid")
            title = (meta.get("title") or "").strip()
            artist = (meta.get("artist") or "").strip()
            if not real_uuid:
                await flash("Could not resolve this Suno song. It may be private or unavailable.", "error")
                return redirect(url_for("radio_upload"))
            if not title or not artist:
                fallback_title, fallback_artist, _ = await _fetch_suno_info(suno_url)
                title = title or (fallback_title or "").strip()
                artist = artist or (fallback_artist or "").strip()
            if not title:
                await flash("Could not fetch song information from this Suno URL.", "error")
                return redirect(url_for("radio_upload"))
            artist = artist or "Unknown Artist"

            # The public form has no account login, so the Suno profile owner
            # is the stable identity used for the per-user submission limit.
            artist_count = await db.count_active_radio_songs_by_artist(artist)
            if artist_count >= max_per_user:
                await flash(
                    f"'{artist}' already has {artist_count} active song(s) in the playlist "
                    f"(maximum {max_per_user}).",
                    "error",
                )
                return redirect(url_for("radio_upload"))

            unique_name = f"radio_{uuid.uuid4().hex}.mp3"
            filepath = os.path.join(RADIO_UPLOAD_DIR, unique_name)
            original_filename = f"{real_uuid}.mp3"
            with tempfile.TemporaryDirectory(prefix="radio_suno_", dir=RADIO_UPLOAD_DIR) as temp_dir:
                downloaded_path = await download_mp3(real_uuid, temp_dir, log_prefix="[radio]")
                if not downloaded_path or not os.path.exists(downloaded_path):
                    await flash("Could not download audio from Suno.", "error")
                    return redirect(url_for("radio_upload"))

                result = await _validate_mp3(downloaded_path, max_duration_sec)
                if "error" in result:
                    await flash(result["error"], "error")
                    return redirect(url_for("radio_upload"))
                os.replace(downloaded_path, filepath)

            # Strip cover art / non-audio streams to prevent concat stalls
            stripped_path = filepath + ".stripped.mp3"
            try:
                strip_proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-y", "-i", filepath, "-vn", "-acodec", "copy", stripped_path,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                await strip_proc.communicate()
                if strip_proc.returncode == 0 and os.path.exists(stripped_path):
                    os.replace(stripped_path, filepath)
                else:
                    # If stripping fails, keep the original
                    if os.path.exists(stripped_path):
                        os.remove(stripped_path)
            except Exception:
                if os.path.exists(stripped_path):
                    os.remove(stripped_path)

            # The normalized file may differ slightly in size from the CDN file.
            result["size"] = os.path.getsize(filepath)

            # Generate rights hash
            rights_hash = hashlib.sha256(
                f"{RIGHTS_DECLARATION_TEXT}|{time.time()}|{client_ip}|{original_filename}|{suno_url}".encode()
            ).hexdigest()

            try:
                song_id = await db.add_radio_song(
                    title=title, artist=artist, suno_url=suno_url,
                    filename=unique_name, original_filename=original_filename,
                    file_size=result["size"], duration=result["duration"],
                    bitrate=result["bitrate"], uploaded_by_ip=client_ip,
                    rights_declaration=RIGHTS_DECLARATION_TEXT, rights_hash=rights_hash,
                )
            except Exception:
                try:
                    os.remove(filepath)
                except OSError:
                    pass
                raise
            await flash(f"'{title}' by {artist} uploaded successfully! (#{song_id})", "success")
            return redirect(url_for("radio_upload"))

        return await render_template(
            "radio_upload.html", closed=False,
            rights_text=RIGHTS_DECLARATION_TEXT,
            content_guidelines=CONTENT_GUIDELINES_TEXT,
            stream_name=stream_name,
            max_per_user=max_per_user,
            max_duration_sec=max_duration_sec,
        )

    @app.route("/radio", methods=["GET", "POST"])
    @permission_required('radio')
    async def radio_admin():
        import json as _json
        if request.method == "POST":
            form = await request.form
            action = form.get("action", "")

            if action == "delete_song":
                song_id = int(form.get("song_id", 0))
                filename = await db.delete_radio_song(song_id)
                if filename:
                    filepath = os.path.join(RADIO_UPLOAD_DIR, filename)
                    if os.path.exists(filepath):
                        os.remove(filepath)
                await flash("Song deleted.", "success")

            elif action == "move_up":
                await db.move_radio_song(int(form.get("song_id", 0)), "up")

            elif action == "move_down":
                await db.move_radio_song(int(form.get("song_id", 0)), "down")

            elif action == "save_config":
                twitch_key = form.get("twitch_key", "").strip()
                stream_url = form.get("stream_url", "").strip()
                upload_enabled = "1" if form.get("upload_enabled") else "0"
                shuffle = "1" if form.get("shuffle") else "0"
                repeat_playlist = "1" if form.get("repeat_playlist") else "0"
                stream_name = form.get("stream_name", "").strip()[:100] or "Twitch Radio"
                disclaimer_enabled = "on" if form.get("disclaimer_enabled") else "off"
                disclaimer_text = (form.get("disclaimer_text") or "").strip()[:2000]
                try:
                    max_per_user = min(25, max(1, int(form.get("max_per_user", "3"))))
                except (TypeError, ValueError):
                    max_per_user = 3
                try:
                    max_duration_minutes = float(form.get("max_duration_minutes", "6"))
                    max_duration_sec = min(1800, max(60, round(max_duration_minutes * 60)))
                except (TypeError, ValueError):
                    max_duration_sec = 360
                expiry_ch = form.get("expiry_channel_id", "").strip()
                tw_client_id = form.get("twitch_client_id", "").strip()
                tw_client_secret = form.get("twitch_client_secret", "").strip()
                tw_refresh_token = form.get("twitch_refresh_token", "").strip()
                tw_broadcaster = form.get("twitch_broadcaster_login", "").strip()

                # Only update key if not masked placeholder
                if twitch_key and not twitch_key.startswith("****"):
                    await db.set_setting("radio_twitch_key", twitch_key)
                if stream_url:
                    await db.set_setting("radio_stream_url", stream_url)
                await db.set_setting("radio_upload_enabled", upload_enabled)
                await db.set_setting("radio_shuffle", shuffle)
                await db.set_setting("radio_repeat_playlist", repeat_playlist)
                await db.set_setting("radio_stream_name", stream_name)
                await db.set_setting("radio_disclaimer_enabled", disclaimer_enabled)
                await db.set_setting("radio_disclaimer_text", disclaimer_text)
                await db.set_setting("radio_max_per_user", str(max_per_user))
                await db.set_setting("radio_max_duration_seconds", str(max_duration_sec))
                await db.set_setting("radio_expiry_channel_id", expiry_ch)
                if tw_client_id:
                    await db.set_setting("radio_twitch_client_id", tw_client_id)
                if tw_client_secret and not tw_client_secret.startswith("****"):
                    await db.set_setting("radio_twitch_client_secret", tw_client_secret)
                if tw_refresh_token and not tw_refresh_token.startswith("****"):
                    await db.set_setting("radio_twitch_refresh_token", tw_refresh_token)
                    # Reset cached IDs so they get re-resolved on next start
                    await db.set_setting("radio_twitch_bot_login", "")
                    await db.set_setting("radio_twitch_bot_user_id", "")
                if tw_broadcaster:
                    # Accept "name", "#name", or full "https://twitch.tv/name" —
                    # always store just the bare login.
                    bn = tw_broadcaster.strip().rstrip("/").lstrip("#").lower()
                    if "twitch.tv/" in bn:
                        bn = bn.split("twitch.tv/", 1)[1].split("/")[0]
                    await db.set_setting("radio_twitch_broadcaster_login", bn)

                # Background upload
                files = await request.files
                bg_file = files.get("background")
                if bg_file and bg_file.filename:
                    ext = bg_file.filename.rsplit(".", 1)[-1].lower()
                    allowed_bg = {"png", "jpg", "jpeg", "gif", "webp", "mp4", "webm"}
                    if ext in allowed_bg:
                        import uuid
                        bg_type = "video" if ext in ("mp4", "webm") else "image"
                        bg_name = f"radio_bg_{uuid.uuid4().hex}.{ext}"
                        bg_path = os.path.join(RADIO_UPLOAD_DIR, bg_name)
                        await bg_file.save(bg_path)
                        # Remove old background
                        old_bg = await db.get_setting("radio_background_filename")
                        if old_bg:
                            old_path = os.path.join(RADIO_UPLOAD_DIR, old_bg)
                            if os.path.exists(old_path):
                                os.remove(old_path)
                        await db.set_setting("radio_background_filename", bg_name)
                        await db.set_setting("radio_background_type", bg_type)

                await flash("Configuration saved.", "success")

            elif action == "save_lyrics_config":
                try:
                    lyrics_width = str(max(40, min(80, int(form.get("lyrics_width", "80")))))
                except (TypeError, ValueError):
                    lyrics_width = "80"
                await db.set_setting("radio_lyrics_width", lyrics_width)
                await flash("Lyrics config saved.", "success")

            elif action == "save_song_pip_config":
                spip_enabled  = "on" if form.get("song_pip_enabled") == "on" else "off"
                spip_format   = form.get("song_pip_format", "9:16")
                try:
                    spip_scale = str(max(5, min(70, int(form.get("song_pip_scale", "20")))))
                except (TypeError, ValueError):
                    spip_scale = "20"
                spip_position = form.get("song_pip_position", "top-right")
                await db.set_setting("radio_song_pip_enabled",  spip_enabled)
                await db.set_setting("radio_song_pip_format",   spip_format)
                await db.set_setting("radio_song_pip_scale",    spip_scale)
                await db.set_setting("radio_song_pip_position", spip_position)
                if stream_manager.is_running:
                    await stream_manager.reload_pip()
                    await flash("Song Video PiP config saved & applied to running stream.", "success")
                else:
                    await flash("Song Video PiP config saved.", "success")

            elif action == "save_pip_config":
                pip_mode = form.get("pip_mode", "off")
                pip_format = form.get("pip_format", "16:9")
                pip_scale = form.get("pip_scale", "25")
                pip_position = form.get("pip_position", "center-right")
                pip_rtmp_key = form.get("pip_rtmp_key", "").strip()
                await db.set_setting("radio_pip_mode", pip_mode)
                await db.set_setting("radio_pip_format", pip_format)
                await db.set_setting("radio_pip_scale", pip_scale)
                await db.set_setting("radio_pip_position", pip_position)
                await db.set_setting("radio_pip_rtmp_key", pip_rtmp_key)
                # PiP file upload
                files = await request.files
                pip_file = files.get("pip_file")
                if pip_file and pip_file.filename:
                    ext = pip_file.filename.rsplit(".", 1)[-1].lower()
                    allowed = {"png", "jpg", "jpeg", "gif", "webp", "mp4", "webm"}
                    if ext in allowed:
                        import uuid
                        pip_type = "video" if ext in ("mp4", "webm") else "image"
                        pip_name = f"radio_pip_{uuid.uuid4().hex}.{ext}"
                        pip_path = os.path.join(RADIO_UPLOAD_DIR, pip_name)
                        await pip_file.save(pip_path)
                        old_pip = await db.get_setting("radio_pip_filename")
                        if old_pip:
                            old_path = os.path.join(RADIO_UPLOAD_DIR, old_pip)
                            if os.path.exists(old_path):
                                os.remove(old_path)
                        await db.set_setting("radio_pip_filename", pip_name)
                        await db.set_setting("radio_pip_file_type", pip_type)
                # Hot-reload PiP on running stream
                if stream_manager.is_running:
                    await stream_manager.reload_pip()
                    await flash("PiP configuration saved & applied to running stream.", "success")
                else:
                    await flash("PiP configuration saved.", "success")

            elif action == "set_source_mode":
                mode = form.get("source_mode", "submissions")
                await db.set_setting("radio_source_mode", mode)
                await flash(f"Radio source set to: {mode}", "success")

            elif action == "add_suno_playlist":
                pl_url = form.get("suno_playlist_url", "").strip()
                pl_desc = form.get("suno_playlist_desc", "").strip()
                if pl_url:
                    try:
                        await db.add_suno_playlist(pl_url, pl_desc)
                        await flash("Suno playlist added.", "success")
                    except Exception as e:
                        await flash(f"Could not add playlist: {e}", "error")

            elif action == "delete_suno_playlist":
                pl_id = int(form.get("playlist_id", 0))
                await db.delete_suno_playlist(pl_id)
                # If this was the active playlist, clear the setting
                active = await db.get_setting("radio_active_suno_playlist")
                if active and int(active) == pl_id:
                    await db.set_setting("radio_active_suno_playlist", "")
                await flash("Suno playlist deleted.", "success")

            elif action == "select_suno_playlist":
                pl_id = form.get("playlist_id", "").strip()
                await db.set_setting("radio_active_suno_playlist", pl_id)
                await flash("Active Suno playlist updated.", "success")

            return redirect(url_for("radio_admin"))

        # GET
        songs = await db.get_all_radio_songs(active_only=False)
        twitch_key = await db.get_setting("radio_twitch_key") or ""
        masked_key = f"****{twitch_key[-4:]}" if len(twitch_key) > 4 else ""
        stream_url = await db.get_setting("radio_stream_url") or ""
        upload_enabled = await db.get_setting("radio_upload_enabled") or "1"
        stream_name = await db.get_setting("radio_stream_name") or "Twitch Radio"
        disclaimer_enabled = await db.get_setting("radio_disclaimer_enabled") or "off"
        disclaimer_text = await db.get_setting("radio_disclaimer_text") or ""
        try:
            max_per_user = max(1, int(await db.get_setting("radio_max_per_user") or "3"))
        except (TypeError, ValueError):
            max_per_user = 3
        try:
            max_duration_sec = max(60, int(await db.get_setting("radio_max_duration_seconds") or "360"))
        except (TypeError, ValueError):
            max_duration_sec = 360
        bg_filename = await db.get_setting("radio_background_filename") or ""
        bg_type = await db.get_setting("radio_background_type") or "image"
        shuffle = await db.get_setting("radio_shuffle") or "0"
        repeat_playlist = await db.get_setting("radio_repeat_playlist") or "1"

        guild = get_guild()
        text_channels = []
        if guild:
            for ch in sorted(guild.text_channels, key=lambda c: c.position):
                text_channels.append({"id": ch.id, "name": ch.name})

        expiry_channel_id = await db.get_setting("radio_expiry_channel_id") or ""
        # Twitch chat-bot (modern Helix flow)
        tw_client_id     = await db.get_setting("radio_twitch_client_id") or ""
        _tw_secret       = await db.get_setting("radio_twitch_client_secret") or ""
        _tw_refresh      = await db.get_setting("radio_twitch_refresh_token") or ""
        tw_secret_masked = f"****{_tw_secret[-4:]}" if len(_tw_secret) > 4 else ""
        tw_refresh_masked = f"****{_tw_refresh[-4:]}" if len(_tw_refresh) > 4 else ""
        tw_broadcaster_login = await db.get_setting("radio_twitch_broadcaster_login") or ""
        tw_bot_login         = await db.get_setting("radio_twitch_bot_login") or ""

        source_mode = await db.get_setting("radio_source_mode") or "submissions"
        suno_playlists = await db.get_all_suno_playlists()
        active_suno_playlist = await db.get_setting("radio_active_suno_playlist") or ""

        pip_mode = await db.get_setting("radio_pip_mode") or "off"
        pip_format = await db.get_setting("radio_pip_format") or "16:9"
        pip_scale = await db.get_setting("radio_pip_scale") or "25"
        pip_position = await db.get_setting("radio_pip_position") or "center-right"
        pip_filename = await db.get_setting("radio_pip_filename") or ""
        pip_file_type = await db.get_setting("radio_pip_file_type") or "image"
        pip_rtmp_key = await db.get_setting("radio_pip_rtmp_key") or ""
        song_pip_enabled  = await db.get_setting("radio_song_pip_enabled")  or "off"
        song_pip_format   = await db.get_setting("radio_song_pip_format")   or "9:16"
        song_pip_scale    = await db.get_setting("radio_song_pip_scale")    or "30"
        song_pip_position = await db.get_setting("radio_song_pip_position") or "top-right"
        lyrics_width = await db.get_setting("radio_lyrics_width") or "80"

        return await render_template(
            "radio.html",
            songs=songs, masked_key=masked_key, stream_url=stream_url,
            upload_enabled=upload_enabled, bg_filename=bg_filename, bg_type=bg_type,
            stream_name=stream_name, max_per_user=max_per_user,
            disclaimer_enabled=disclaimer_enabled,
            disclaimer_text=disclaimer_text,
            max_duration_minutes=max_duration_sec / 60,
            text_channels=text_channels,
            expiry_channel_id=expiry_channel_id,
            shuffle=shuffle,
            repeat_playlist=repeat_playlist,
            tw_client_id=tw_client_id,
            tw_secret_masked=tw_secret_masked,
            tw_refresh_masked=tw_refresh_masked,
            tw_broadcaster_login=tw_broadcaster_login,
            tw_bot_login=tw_bot_login,
            tw_oauth_redirect_uri=_twitch_oauth_redirect_uri(),
            source_mode=source_mode,
            suno_playlists=suno_playlists,
            active_suno_playlist=active_suno_playlist,
            pip_mode=pip_mode, pip_format=pip_format, pip_scale=pip_scale,
            pip_position=pip_position, pip_filename=pip_filename,
            pip_file_type=pip_file_type, pip_rtmp_key=pip_rtmp_key,
            song_pip_enabled=song_pip_enabled, song_pip_format=song_pip_format,
            song_pip_scale=song_pip_scale, song_pip_position=song_pip_position,
            lyrics_width=lyrics_width,
        )

    @app.route("/radio/files/<filename>")
    async def radio_file(filename):
        from quart import send_from_directory
        return await send_from_directory(RADIO_UPLOAD_DIR, filename)

    @app.route("/radio/export-rights")
    @permission_required('radio')
    async def radio_export_rights():
        import csv, io
        from datetime import datetime, timezone
        from quart import Response
        songs = await db.get_all_radio_songs(active_only=False)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "ID", "Title", "Artist", "Suno URL", "Original Filename",
            "Uploaded At (UTC)", "Expires At (UTC)", "Uploader IP",
            "Rights Declaration", "Rights Hash (SHA256)", "Rights Agreed At (UTC)",
        ])
        for s in songs:
            fmt = lambda ts: datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if ts else ""
            writer.writerow([
                s["id"], s["title"], s["artist"], s.get("suno_url", ""),
                s.get("original_filename", ""),
                fmt(s.get("uploaded_at")), fmt(s.get("expires_at")),
                s.get("uploaded_by_ip", ""),
                s.get("rights_declaration", ""), s.get("rights_hash", ""),
                fmt(s.get("rights_agreed_at")),
            ])
        csv_data = output.getvalue()
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        return Response(
            csv_data,
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename=streaming_rights_{timestamp}.csv"},
        )

    from bot.stream_manager import StreamManager
    stream_manager = StreamManager(db, RADIO_UPLOAD_DIR)

    async def _legacy_radio_start_block_reason() -> str:
        """Explain why the legacy radio must not be started right now."""
        if exp_stream_manager.is_running:
            return "Experimental Radio is currently live."
        if trya_stream_manager.is_running:
            return "TrYa Stream is currently live."

        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("Europe/Berlin"))

        manager_enabled = await db.get_setting("exp_radio_enabled") or "on"
        enabled = await db.get_setting("exp_radio_schedule_enabled") or "off"
        if manager_enabled != "on":
            enabled = "off"
        days_csv = await db.get_setting("exp_radio_schedule_days") or ""
        days = {int(day) for day in days_csv.split(",") if day.strip().isdigit()}
        if enabled == "on" and days and now.weekday() in days:
            schedule_time = (await db.get_setting("exp_radio_schedule_time") or "").strip()
            time_note = f" at {schedule_time}" if schedule_time else ""
            return (
                "Experimental Radio is scheduled for today"
                f"{time_note}. The legacy radio is locked for the entire scheduled day."
            )

        trya_enabled = await db.get_setting("trya_stream_schedule_enabled") or "off"
        trya_days_csv = await db.get_setting("trya_stream_schedule_days") or ""
        trya_days = {
            int(day) for day in trya_days_csv.split(",") if day.strip().isdigit()
        }
        if trya_enabled == "on" and trya_days and now.weekday() in trya_days:
            schedule_time = (await db.get_setting("trya_stream_schedule_time") or "").strip()
            time_note = f" at {schedule_time}" if schedule_time else ""
            return (
                "TrYa Stream is scheduled for today"
                f"{time_note}. The legacy radio is locked for the entire scheduled day."
            )
        return ""

    @app.route("/radio/stream/status")
    @permission_required('radio')
    async def radio_stream_status():
        from quart import jsonify
        status = await stream_manager.get_status()
        reason = "" if status.get("running") else await _legacy_radio_start_block_reason()
        status["start_blocked"] = bool(reason)
        status["start_block_reason"] = reason
        return jsonify(status)

    @app.route("/admin/twitch-radio/test-connection", methods=["POST"])
    @permission_required('radio')
    async def radio_twitch_test():
        """One-shot health-check for the Twitch chat-bot credentials."""
        from quart import jsonify
        from bot.twitch_bot import TwitchBot
        bot = TwitchBot(db, key_prefix="radio_twitch")
        result = await bot.diagnose()
        return jsonify(result)

    @app.route("/radio/twitch-oauth-start")
    @permission_required('radio')
    async def radio_twitch_oauth_start():
        """Authorize the legacy radio chat bot through the public HTTPS callback."""
        import secrets as _sec
        from urllib.parse import urlencode

        client_id = await db.get_setting("radio_twitch_client_id")
        client_secret = await db.get_setting("radio_twitch_client_secret")
        if not client_id or not client_secret:
            await flash("Save the Twitch Client ID and Client Secret first.", "error")
            return redirect(url_for("radio_admin"))

        state = _sec.token_urlsafe(16)
        session[_TWITCH_OAUTH_STATE_KEY] = state
        session[_TWITCH_OAUTH_MODE_KEY] = "radio_bot"
        params = urlencode({
            "client_id": client_id,
            "redirect_uri": _twitch_oauth_redirect_uri(),
            "response_type": "code",
            "scope": _TWITCH_BOT_SCOPES,
            "state": state,
            "force_verify": "true",
        })
        return redirect(f"https://id.twitch.tv/oauth2/authorize?{params}")

    @app.route("/radio/stream/<action>", methods=["POST"])
    @permission_required('radio')
    async def radio_stream_action(action):
        from quart import jsonify
        if action == "start":
            if app.database_restore_pending:
                return jsonify({"ok": False, "error": "A database restore is in progress."}), 409
            async with app.radio_start_lock:
                if app.database_restore_pending:
                    return jsonify({"ok": False, "error": "A database restore is in progress."}), 409
                reason = await _legacy_radio_start_block_reason()
                if reason:
                    return jsonify({"ok": False, "error": reason, "start_blocked": True}), 409
                result = await stream_manager.start()
        elif action == "stop":
            result = await stream_manager.stop()
        elif action == "next":
            result = await stream_manager.skip_next()
        elif action == "prev":
            result = await stream_manager.skip_prev()
        elif action == "reload":
            result = await stream_manager.reload_playlist()
        elif action == "pip-reload":
            result = await stream_manager.reload_pip()
        else:
            result = {"error": "Unknown action."}
        return jsonify(result)

    # --- Auto-cleanup task ---

    async def _radio_cleanup_loop():
        """Periodically delete expired radio songs (every hour).
        Posts a notification to the configured Discord channel when songs expire."""
        while True:
            try:
                await asyncio.sleep(3600)
                filenames, expired_songs = await db.cleanup_expired_radio_songs()
                for fn in filenames:
                    filepath = os.path.join(RADIO_UPLOAD_DIR, fn)
                    if os.path.exists(filepath):
                        os.remove(filepath)
                if expired_songs:
                    print(f"[radio] Cleaned up {len(expired_songs)} expired songs.")
                    # Post notification to Discord
                    channel_id_str = await db.get_setting("radio_expiry_channel_id")
                    if channel_id_str and bot and bot.is_ready():
                        guild = get_guild()
                        if guild:
                            ch = guild.get_channel(int(channel_id_str))
                            if ch:
                                try:
                                    stream_name = (
                                        await db.get_setting("radio_stream_name") or "Twitch Radio"
                                    ).strip() or "Twitch Radio"
                                    lines = []
                                    for s in expired_songs:
                                        title = s.get("title", "Unknown")
                                        artist = s.get("artist", "Unknown")
                                        suno_url = s.get("suno_url", "")
                                        if suno_url:
                                            lines.append(f"- **{title}** - {artist}\n  {suno_url}")
                                        else:
                                            lines.append(f"- **{title}** - {artist}")
                                    msg = (
                                        f"🗑️ **{stream_name}: {len(expired_songs)} "
                                        f"song{'s' if len(expired_songs) != 1 else ''} "
                                        f"expired and {'were' if len(expired_songs) != 1 else 'was'} removed:**\n\n"
                                        + "\n".join(lines)
                                    )
                                    await ch.send(msg)
                                except Exception as notify_err:
                                    print(f"[radio] Expiry notification error: {notify_err}")
            except Exception as e:
                print(f"[radio] Cleanup error: {e}")

    async def _exp_radio_cleanup_loop():
        """Periodically soft-delete expired exp_radio songs (every hour).
        Also marks stale pending songs (no MP3 uploaded within 30 min) as failed.
        Notifies users via the configured Discord channel."""
        while True:
            try:
                await asyncio.sleep(3600)
                # Mark stale pending songs (submitted >30 min ago, still no MP3) as failed
                await db.db.execute(
                    "UPDATE exp_radio_songs SET analysis_status = 'failed' "
                    "WHERE analysis_status = 'pending' AND mp3_filename IS NULL "
                    "AND submitted_at < unixepoch() - 1800"
                )
                await db.db.commit()
                expired = await db.expire_old_exp_radio_songs()
                if not expired:
                    continue
                print(f"[exp-radio] Expired {len(expired)} song(s).", flush=True)
                # File cleanup — but skip songs still referenced by the
                # currently-running stream's in-memory playlist (FFmpeg would
                # crash on its next loop iteration if we removed those).
                in_use_mp3 = set()
                if exp_stream_manager.is_running:
                    in_use_mp3 = {
                        s.get("mp3_filename")
                        for s in (exp_stream_manager.playlist or [])
                        if s.get("mp3_filename")
                    }
                removed_files = 0
                protected_songs = await db.get_all_exp_radio_songs(active_only=True)
                for s in expired:
                    mp3_fn = s.get("mp3_filename")
                    if mp3_fn and mp3_fn in in_use_mp3:
                        print(
                            f"[exp-radio] Keeping files for #{s['id']} ({s.get('title')!r}) "
                            f"— still in active stream playlist.", flush=True,
                        )
                        continue
                    removed_files += cleanup_exp_radio_song_files(
                        EXP_RADIO_DIR, s, protected_songs
                    )

                # Files retained while FFmpeg was using an expired song are
                # collected on a later pass once that stream has ended.
                inactive = await db.get_all_exp_radio_songs(active_only=False)
                in_use_ids = {
                    int(s.get("id")) for s in (exp_stream_manager.playlist or [])
                    if exp_stream_manager.is_running and s.get("id")
                }
                for s in inactive:
                    if s.get("active") or int(s.get("id") or 0) in in_use_ids:
                        continue
                    removed_files += cleanup_exp_radio_song_files(
                        EXP_RADIO_DIR, s, protected_songs
                    )
                if removed_files:
                    print(f"[exp-radio] Removed {removed_files} expired file(s) from disk.", flush=True)
                channel_id_str = (
                    await db.get_setting("exp_radio_expiry_channel_id")
                    or await db.get_setting("radio_expiry_channel_id")
                )
                if channel_id_str and bot and bot.is_ready():
                    guild = get_guild()
                    if guild:
                        ch = guild.get_channel(int(channel_id_str))
                        if ch:
                            try:
                                lines = []
                                for s in expired:
                                    t = s.get("title") or "Untitled"
                                    a = s.get("artist") or s.get("user_name") or ""
                                    u = s.get("suno_url") or ""
                                    lines.append(f"- **{t}** by {a}  {u}")
                                msg = (
                                    f"🗑️ **{len(expired)} Experimental Radio song(s) expired:**\n"
                                    + "\n".join(lines)
                                )
                                await ch.send(msg)
                            except Exception as e:
                                print(f"[exp-radio] Expiry notify error: {e}", flush=True)
            except Exception as e:
                print(f"[exp-radio] Cleanup error: {e}", flush=True)

    async def _post_exp_stream_announcement(ch_id: str, stream_url: str) -> tuple[bool, str]:
        """Send the “📺 Live Stream” embed to the given Discord channel id.

        Returns (True, channel_name) on success, (False, error_message) on
        failure. Called both from the manual “Post Stream Link” buttons on
        the admin page and from the auto-start scheduler.
        """
        if not ch_id or not stream_url:
            return False, "missing channel id or stream URL"
        guild = get_guild()
        if not guild:
            return False, "Discord guild unavailable"
        try:
            ch_int = int(ch_id)
        except Exception:
            return False, f"invalid channel id {ch_id!r}"
        channel = guild.get_channel(ch_int) or guild.get_thread(ch_int)
        if not channel:
            return False, f"channel {ch_id} not found"
        try:
            import discord
            embed = discord.Embed(
                title="\U0001F4FA Live Stream",
                description=f"Watch the stream now!\n\n**[Tune in]({stream_url})**",
                color=discord.Color.purple(),
            )
            await channel.send(embed=embed)
            return True, channel.name
        except Exception as e:
            return False, f"send failed: {e}"

    async def _exp_radio_schedule_loop():
        """Auto-start the exp_radio stream on configured weekdays + time.

        A 15-minute catch-up window prevents a container restart or briefly
        busy event loop at the configured minute from losing the scheduled
        run. Accepted occurrences are persisted so a later process restart
        cannot fire the same schedule twice.
        """
        from bot.exp_stream_manager import log_event
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        schedule_tz = ZoneInfo("Europe/Berlin")
        catch_up_seconds = 15 * 60
        retry_seconds = 60
        app.exp_schedule_last_attempt_key = ""
        app.exp_schedule_last_attempt_at = 0.0
        last_config_signature = None
        log_event(
            "Scheduler loop started (Europe/Berlin, 15-minute catch-up window).",
            prefix="[exp-schedule]",
        )
        while True:
            try:
                manager_enabled = await db.get_setting("exp_radio_enabled") or "on"
                enabled = await db.get_setting("exp_radio_schedule_enabled") or "off"
                if manager_enabled != "on":
                    enabled = "off"
                days_csv = await db.get_setting("exp_radio_schedule_days") or ""
                hhmm = (await db.get_setting("exp_radio_schedule_time") or "").strip()
                config_signature = (enabled, days_csv, hhmm)
                if config_signature != last_config_signature:
                    last_config_signature = config_signature
                    if enabled == "on":
                        day_names = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
                        configured_days = [
                            day_names[int(day)]
                            for day in days_csv.split(",")
                            if day.strip().isdigit() and 0 <= int(day) <= 6
                        ]
                        if configured_days and hhmm:
                            log_event(
                                f"Scheduler armed: {', '.join(configured_days)} at {hhmm} Europe/Berlin.",
                                prefix="[exp-schedule]",
                            )
                        else:
                            log_event(
                                "Scheduler enabled but weekdays or start time are missing.",
                                level="error", prefix="[exp-schedule]",
                            )
                    else:
                        log_event("Scheduler disabled.", prefix="[exp-schedule]")
                if enabled != "on":
                    await asyncio.sleep(30)
                    continue
                days = {int(d) for d in days_csv.split(",") if d.strip().isdigit()}
                if not days or not hhmm or ":" not in hhmm:
                    await asyncio.sleep(30)
                    continue
                try:
                    h_str, m_str = hhmm.split(":", 1)
                    target_h, target_m = int(h_str), int(m_str)
                except Exception:
                    await asyncio.sleep(30)
                    continue
                if not (0 <= target_h <= 23 and 0 <= target_m <= 59):
                    await asyncio.sleep(30)
                    continue

                now = datetime.now(schedule_tz)
                target = now.replace(
                    hour=target_h, minute=target_m, second=0, microsecond=0,
                )
                if target > now:
                    previous_target = target - timedelta(days=1)
                    if (now - previous_target).total_seconds() <= catch_up_seconds:
                        target = previous_target
                seconds_late = (now - target).total_seconds()
                occurrence_key = target.strftime("%Y-%m-%dT%H:%M%z")
                last_handled = (
                    await db.get_setting("exp_radio_schedule_last_handled") or ""
                )
                due = (
                    target.weekday() in days
                    and 0 <= seconds_late <= catch_up_seconds
                    and occurrence_key != last_handled
                )
                retry_ready = (
                    occurrence_key != app.exp_schedule_last_attempt_key
                    or time.monotonic() - app.exp_schedule_last_attempt_at >= retry_seconds
                )
                if due and retry_ready:
                    app.exp_schedule_last_attempt_key = occurrence_key
                    app.exp_schedule_last_attempt_at = time.monotonic()
                    late_note = (
                        "on time" if seconds_late < 60
                        else f"{int(seconds_late // 60)} minute(s) late"
                    )
                    if app.database_restore_pending:
                        log_event(
                            "Scheduler: database restore in progress - retrying shortly.",
                            level="error", prefix="[exp-schedule]",
                        )
                    elif stream_manager.is_running or stream_manager._loading:
                        log_event(
                            "Scheduler: legacy Twitch Radio is running or starting - retrying shortly.",
                            level="error", prefix="[exp-schedule]",
                        )
                    elif trya_stream_manager.is_running:
                        log_event(
                            "Scheduler: TrYa Stream is running - retrying shortly.",
                            level="error", prefix="[exp-schedule]",
                        )
                    elif exp_stream_manager.is_running:
                        await db.set_setting(
                            "exp_radio_schedule_last_handled", occurrence_key,
                        )
                        log_event(
                            "Scheduler: stream already running - scheduled occurrence marked as handled.",
                            prefix="[exp-schedule]",
                        )
                    else:
                        twitch_key = await db.get_setting("exp_radio_twitch_key") or ""
                        if not twitch_key:
                            log_event(
                                "Scheduler: no Twitch stream key configured - retrying shortly.",
                                level="error", prefix="[exp-schedule]",
                            )
                        else:
                            log_event(
                                f"Scheduler: triggering auto-start for {target.strftime('%a %H:%M')} "
                                f"Europe/Berlin ({late_note}) with fresh cache.",
                                prefix="[exp-schedule]",
                            )
                            try:
                                async with app.radio_start_lock:
                                    if app.database_restore_pending:
                                        result = {
                                            "ok": False,
                                            "error": "database restore in progress",
                                        }
                                    elif stream_manager.is_running or trya_stream_manager.is_running:
                                        result = {
                                            "ok": False,
                                            "error": "another radio stream started first",
                                        }
                                    else:
                                        result = await exp_stream_manager.start(
                                            twitch_key, fresh_cache=True, scheduled=True,
                                        )
                                if result.get("ok"):
                                    log_event(
                                        f"Scheduler: start accepted with {result.get('song_count')} song(s); waiting for FFmpeg to go live.",
                                        prefix="[exp-schedule]",
                                    )
                                    live_ok = await exp_stream_manager.wait_until_live(timeout=900)
                                    if not live_ok:
                                        log_event(
                                            "Scheduler: stream did not become live within 15 minutes — skipping Discord announcement.",
                                            level="error", prefix="[exp-schedule]",
                                        )
                                        continue
                                    await db.set_setting(
                                        "exp_radio_schedule_last_handled", occurrence_key,
                                    )
                                    log_event(
                                        "Scheduler: stream is live; posting Discord announcement.",
                                        prefix="[exp-schedule]",
                                    )
                                    # Auto-post stream URL to configured
                                    # Discord channels (same embed as the
                                    # manual “Post Stream Link” buttons).
                                    stream_url = await db.get_setting("exp_radio_stream_url") or ""
                                    if stream_url:
                                        for slot in ("1", "2", "3"):
                                            ch_id = await db.get_setting(f"exp_radio_post_channel_{slot}_id") or ""
                                            if not ch_id:
                                                continue
                                            ok, info = await _post_exp_stream_announcement(ch_id, stream_url)
                                            if ok:
                                                log_event(
                                                    f"Scheduler: announced in #{info}.",
                                                    prefix="[exp-schedule]",
                                                )
                                            else:
                                                log_event(
                                                    f"Scheduler: announcement to channel {ch_id} failed \u2014 {info}",
                                                    level="error", prefix="[exp-schedule]",
                                                )
                                    else:
                                        log_event(
                                            "Scheduler: no stream URL configured \u2014 skipping Discord announcement.",
                                            prefix="[exp-schedule]",
                                        )
                                else:
                                    log_event(
                                        f"Scheduler: start failed \u2014 {result.get('error')}",
                                        level="error", prefix="[exp-schedule]",
                                    )
                            except Exception as e:
                                log_event(
                                    f"Scheduler: start exception: {e}",
                                    level="error", prefix="[exp-schedule]",
                                )
            except Exception as e:
                print(f"[exp-radio] Scheduler loop error: {e}", flush=True)
            await asyncio.sleep(30)

    async def _trya_stream_cleanup_loop():
        """Remove due submissions from the active playlist while retaining evidence."""
        while True:
            try:
                await asyncio.sleep(3600)
                removed = await db.deactivate_due_trya_stream_submissions(
                    reason="submission_retention_elapsed"
                )
                if not removed:
                    continue
                print(
                    f"[trya-stream] Removed {len(removed)} due submission(s) from the playlist; "
                    "originals and working files retained.",
                    flush=True,
                )
                channel_id_str = await db.get_setting("trya_stream_expiry_channel_id")
                if channel_id_str and bot and bot.is_ready():
                    guild = get_guild()
                    channel = guild.get_channel(int(channel_id_str)) if guild else None
                    if channel:
                        lines = [
                            f"- **{song.get('title') or 'Untitled'}** by "
                            f"{song.get('artist') or song.get('user_name') or ''}  "
                            f"{song.get('suno_url') or ''}"
                            for song in removed
                        ]
                        await channel.send(
                            f"🗂️ **{len(removed)} TrYa Stream submission(s) left the active playlist:**\n"
                            + "\n".join(lines)
                            + "\n\nOriginal uploads and consent evidence remain archived."
                        )
            except Exception as e:
                print(f"[trya-stream] Playlist retention error: {e}", flush=True)

    async def _post_trya_stream_announcement(ch_id: str, stream_url: str) -> tuple[bool, str]:
        """Send the “📺 Live Stream” embed to the given Discord channel id.

        Returns (True, channel_name) on success, (False, error_message) on
        failure. Called both from the manual “Post Stream Link” buttons on
        the admin page and from the auto-start scheduler.
        """
        if not ch_id or not stream_url:
            return False, "missing channel id or stream URL"
        guild = get_guild()
        if not guild:
            return False, "Discord guild unavailable"
        try:
            ch_int = int(ch_id)
        except Exception:
            return False, f"invalid channel id {ch_id!r}"
        channel = guild.get_channel(ch_int) or guild.get_thread(ch_int)
        if not channel:
            return False, f"channel {ch_id} not found"
        try:
            import discord
            embed = discord.Embed(
                title="\U0001F4FA Live Stream",
                description=f"Watch the stream now!\n\n**[Tune in]({stream_url})**",
                color=discord.Color.purple(),
            )
            await channel.send(embed=embed)
            return True, channel.name
        except Exception as e:
            return False, f"send failed: {e}"

    async def _trya_stream_schedule_loop():
        """Auto-start the trya_stream stream on configured weekdays + time.

        A 15-minute catch-up window prevents a container restart or briefly
        busy event loop at the configured minute from losing the scheduled
        run. Accepted occurrences are persisted so a later process restart
        cannot fire the same schedule twice.
        """
        from bot.trya_stream_manager import log_event
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo

        schedule_tz = ZoneInfo("Europe/Berlin")
        catch_up_seconds = 15 * 60
        retry_seconds = 60
        app.trya_schedule_last_attempt_key = ""
        app.trya_schedule_last_attempt_at = 0.0
        last_config_signature = None
        log_event(
            "Scheduler loop started (Europe/Berlin, 15-minute catch-up window).",
            prefix="[trya-schedule]",
        )
        while True:
            try:
                enabled = await db.get_setting("trya_stream_schedule_enabled") or "off"
                days_csv = await db.get_setting("trya_stream_schedule_days") or ""
                hhmm = (await db.get_setting("trya_stream_schedule_time") or "").strip()
                config_signature = (enabled, days_csv, hhmm)
                if config_signature != last_config_signature:
                    last_config_signature = config_signature
                    if enabled == "on":
                        day_names = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
                        configured_days = [
                            day_names[int(day)]
                            for day in days_csv.split(",")
                            if day.strip().isdigit() and 0 <= int(day) <= 6
                        ]
                        if configured_days and hhmm:
                            log_event(
                                f"Scheduler armed: {', '.join(configured_days)} at {hhmm} Europe/Berlin.",
                                prefix="[trya-schedule]",
                            )
                        else:
                            log_event(
                                "Scheduler enabled but weekdays or start time are missing.",
                                level="error", prefix="[trya-schedule]",
                            )
                    else:
                        log_event("Scheduler disabled.", prefix="[trya-schedule]")
                if enabled != "on":
                    await asyncio.sleep(30)
                    continue
                days = {int(d) for d in days_csv.split(",") if d.strip().isdigit()}
                if not days or not hhmm or ":" not in hhmm:
                    await asyncio.sleep(30)
                    continue
                try:
                    h_str, m_str = hhmm.split(":", 1)
                    target_h, target_m = int(h_str), int(m_str)
                except Exception:
                    await asyncio.sleep(30)
                    continue
                if not (0 <= target_h <= 23 and 0 <= target_m <= 59):
                    await asyncio.sleep(30)
                    continue

                now = datetime.now(schedule_tz)
                target = now.replace(
                    hour=target_h, minute=target_m, second=0, microsecond=0,
                )
                if target > now:
                    previous_target = target - timedelta(days=1)
                    if (now - previous_target).total_seconds() <= catch_up_seconds:
                        target = previous_target
                seconds_late = (now - target).total_seconds()
                occurrence_key = target.strftime("%Y-%m-%dT%H:%M%z")
                last_handled = (
                    await db.get_setting("trya_stream_schedule_last_handled") or ""
                )
                due = (
                    target.weekday() in days
                    and 0 <= seconds_late <= catch_up_seconds
                    and occurrence_key != last_handled
                )
                retry_ready = (
                    occurrence_key != app.trya_schedule_last_attempt_key
                    or time.monotonic() - app.trya_schedule_last_attempt_at >= retry_seconds
                )
                if due and retry_ready:
                    app.trya_schedule_last_attempt_key = occurrence_key
                    app.trya_schedule_last_attempt_at = time.monotonic()
                    late_note = (
                        "on time" if seconds_late < 60
                        else f"{int(seconds_late // 60)} minute(s) late"
                    )
                    if app.database_restore_pending:
                        log_event(
                            "Scheduler: database restore in progress - retrying shortly.",
                            level="error", prefix="[trya-schedule]",
                        )
                    elif stream_manager.is_running or stream_manager._loading:
                        log_event(
                            "Scheduler: legacy Twitch Radio is running or starting - retrying shortly.",
                            level="error", prefix="[trya-schedule]",
                        )
                    elif exp_stream_manager.is_running:
                        log_event(
                            "Scheduler: Experimental Radio is running - retrying shortly.",
                            level="error", prefix="[trya-schedule]",
                        )
                    elif trya_stream_manager.is_running:
                        await db.set_setting(
                            "trya_stream_schedule_last_handled", occurrence_key,
                        )
                        log_event(
                            "Scheduler: stream already running - scheduled occurrence marked as handled.",
                            prefix="[trya-schedule]",
                        )
                    else:
                        twitch_key = await db.get_setting("trya_stream_twitch_key") or ""
                        if not twitch_key:
                            log_event(
                                "Scheduler: no Twitch stream key configured - retrying shortly.",
                                level="error", prefix="[trya-schedule]",
                            )
                        else:
                            log_event(
                                f"Scheduler: triggering auto-start for {target.strftime('%a %H:%M')} "
                                f"Europe/Berlin ({late_note}) with fresh cache.",
                                prefix="[trya-schedule]",
                            )
                            try:
                                async with app.radio_start_lock:
                                    if app.database_restore_pending:
                                        result = {
                                            "ok": False,
                                            "error": "database restore in progress",
                                        }
                                    elif stream_manager.is_running or exp_stream_manager.is_running:
                                        result = {
                                            "ok": False,
                                            "error": "another radio stream started first",
                                        }
                                    else:
                                        result = await trya_stream_manager.start(
                                            twitch_key, fresh_cache=True, scheduled=True,
                                        )
                                if result.get("ok"):
                                    log_event(
                                        f"Scheduler: start accepted with {result.get('song_count')} song(s); waiting for FFmpeg to go live.",
                                        prefix="[trya-schedule]",
                                    )
                                    live_ok = await trya_stream_manager.wait_until_live(timeout=900)
                                    if not live_ok:
                                        log_event(
                                            "Scheduler: stream did not become live within 15 minutes — skipping Discord announcement.",
                                            level="error", prefix="[trya-schedule]",
                                        )
                                        continue
                                    await db.set_setting(
                                        "trya_stream_schedule_last_handled", occurrence_key,
                                    )
                                    log_event(
                                        "Scheduler: stream is live; posting Discord announcement.",
                                        prefix="[trya-schedule]",
                                    )
                                    # Auto-post stream URL to configured
                                    # Discord channels (same embed as the
                                    # manual “Post Stream Link” buttons).
                                    stream_url = await db.get_setting("trya_stream_stream_url") or ""
                                    if stream_url:
                                        for slot in ("1", "2", "3"):
                                            ch_id = await db.get_setting(f"trya_stream_post_channel_{slot}_id") or ""
                                            if not ch_id:
                                                continue
                                            ok, info = await _post_exp_stream_announcement(ch_id, stream_url)
                                            if ok:
                                                log_event(
                                                    f"Scheduler: announced in #{info}.",
                                                    prefix="[trya-schedule]",
                                                )
                                            else:
                                                log_event(
                                                    f"Scheduler: announcement to channel {ch_id} failed \u2014 {info}",
                                                    level="error", prefix="[trya-schedule]",
                                                )
                                    else:
                                        log_event(
                                            "Scheduler: no stream URL configured \u2014 skipping Discord announcement.",
                                            prefix="[trya-schedule]",
                                        )
                                else:
                                    log_event(
                                        f"Scheduler: start failed \u2014 {result.get('error')}",
                                        level="error", prefix="[trya-schedule]",
                                    )
                            except Exception as e:
                                log_event(
                                    f"Scheduler: start exception: {e}",
                                    level="error", prefix="[trya-schedule]",
                                )
            except Exception as e:
                print(f"[trya-stream] Scheduler loop error: {e}", flush=True)
            await asyncio.sleep(30)

    @app.before_serving
    async def start_cleanup_task():
        active_exp_songs = await db.get_all_exp_radio_songs(active_only=True)
        orphan_hooks = cleanup_orphan_exp_radio_hook_files(
            EXP_RADIO_DIR, active_exp_songs
        )
        if orphan_hooks:
            print(
                f"[exp-radio] Startup cleanup removed {orphan_hooks} orphan Hook file(s).",
                flush=True,
            )
        retained_trya_songs = await db.get_all_trya_stream_songs(active_only=False)
        trya_orphan_hooks = cleanup_orphan_trya_stream_hook_files(
            TRYA_STREAM_DIR, retained_trya_songs
        )
        if trya_orphan_hooks:
            print(
                f"[trya-stream] Startup cleanup removed {trya_orphan_hooks} orphan Hook file(s).",
                flush=True,
            )
        app.radio_cleanup_task = asyncio.create_task(_radio_cleanup_loop())
        app.exp_radio_cleanup_task = asyncio.create_task(_exp_radio_cleanup_loop())
        app.exp_radio_schedule_task = asyncio.create_task(_exp_radio_schedule_loop())
        app.trya_stream_cleanup_task = asyncio.create_task(_trya_stream_cleanup_loop())
        app.trya_stream_schedule_task = asyncio.create_task(_trya_stream_schedule_loop())
        await twitch_event_alerts.start()
        await trya_stream_event_alerts.start()
        asyncio.create_task(_relic_hunt_autostart())
        asyncio.create_task(_trya_relic_hunt_autostart())

    # ── Experimental Radio ─────────────────────────────────────────────────────

    from bot.exp_stream_manager import ExpStreamManager
    exp_stream_manager = ExpStreamManager(db, EXP_RADIO_DIR)
    from bot.trya_stream_manager import TryaStreamManager
    trya_stream_manager = TryaStreamManager(db, TRYA_STREAM_DIR)
    from bot.trya_dcs_manager import TryaDcsManager
    trya_dcs_manager = TryaDcsManager(db, TRYA_DCS_DIR)
    if bot is not None:
        bot.exp_stream_manager = exp_stream_manager
        bot.trya_stream_manager = trya_stream_manager
        bot.trya_dcs_manager = trya_dcs_manager

    @app.after_serving
    async def stop_trya_dcs_publisher():
        if trya_dcs_manager.is_running:
            await trya_dcs_manager.stop()

    from bot.relic_hunt import RelicHunt
    from bot.twitch_bot import TwitchBot as _TwitchBot
    from bot.twitch_event_alerts import DEFAULT_ALERT_SETTINGS, TwitchEventAlerts
    from bot.live_log import log_event as _rh_log
    from bot.trya_live_log import log_event as _trya_log
    relic_hunt = RelicHunt(db, stream_kind="exp")
    trya_relic_hunt = RelicHunt(db, stream_kind="trya")
    twitch_event_alerts = TwitchEventAlerts(db)
    trya_stream_event_alerts = TwitchEventAlerts(
        db,
        settings_prefix="trya_stream_twitch_alerts",
        chat_prefix="trya_stream_twitch",
        eventsub_prefix="trya_stream_twitch_alerts_eventsub",
        log_prefix="[trya-stream-alerts]",
        logger=_trya_log,
    )

    def _fmt_exp_duration(seconds) -> str:
        try:
            total = int(round(float(seconds)))
        except (TypeError, ValueError):
            return "unknown"
        if total <= 0:
            return "unknown"
        mins, secs = divmod(total, 60)
        hours, mins = divmod(mins, 60)
        if hours:
            return f"{hours:d}:{mins:02d}:{secs:02d}"
        return f"{mins:d}:{secs:02d}"

    async def _exp_radio_stream_relevant_songs() -> list[dict]:
        active_pl = (await db.get_setting("exp_radio_active_playlist")) or "submission"
        if active_pl == "both":
            songs = await db.get_all_exp_radio_songs(active_only=True, source="submission")
            songs += await db.get_all_exp_radio_songs(active_only=True, source="admin")
        else:
            songs = await db.get_all_exp_radio_songs(active_only=True, source=active_pl)

        intro_enabled = (await db.get_setting("exp_radio_intro_enabled")) or "off"
        if intro_enabled == "on":
            songs += await db.get_all_exp_radio_songs(active_only=True, source="intro")

        outro_enabled = (await db.get_setting("exp_radio_outro_enabled")) or "off"
        if outro_enabled == "on":
            songs += await db.get_all_exp_radio_songs(active_only=True, source="outro")

        seen: set[int] = set()
        unique: list[dict] = []
        for song in songs:
            song_id = int(song.get("id") or 0)
            if song_id and song_id in seen:
                continue
            if song_id:
                seen.add(song_id)
            unique.append(song)
        return unique

    async def _check_exp_radio_durations() -> tuple[int, int, int, int]:
        from bot.exp_stream_manager import log_event

        songs = await _exp_radio_stream_relevant_songs()
        checked = corrected = skipped = errors = 0
        log_event(
            f"Manual duration check started for {len(songs)} active stream song(s).",
            prefix="[duration]",
        )

        for song in songs:
            title = song.get("title") or song.get("mp3_filename") or f"#{song.get('id')}"
            if not song.get("mp3_filename"):
                skipped += 1
                log_event(f"Skipped #{song.get('id')} ({title!r}): no MP3 file.", prefix="[duration]")
                continue

            checked += 1
            try:
                probed = await exp_stream_manager._probe_audio_duration(song)
            except Exception as e:
                errors += 1
                log_event(
                    f"Duration probe failed for #{song.get('id')} ({title!r}): {e}",
                    level="error",
                    prefix="[duration]",
                )
                continue
            if probed is None:
                errors += 1
                log_event(
                    f"Duration probe failed for #{song.get('id')} ({title!r}): no duration detected.",
                    level="error",
                    prefix="[duration]",
                )
                continue

            try:
                stored = float(song.get("duration") or 0)
            except (TypeError, ValueError):
                stored = 0.0

            if not stored or abs(probed - stored) > 1.0:
                try:
                    await db.update_exp_radio_song(song["id"], duration=probed)
                    corrected += 1
                    log_event(
                        f"Corrected #{song.get('id')} ({title!r}): "
                        f"{_fmt_exp_duration(stored)} -> {_fmt_exp_duration(probed)} "
                        f"({stored:.1f}s -> {probed:.1f}s).",
                        prefix="[duration]",
                    )
                except Exception as e:
                    errors += 1
                    log_event(
                        f"Duration DB update failed for #{song.get('id')} ({title!r}): {e}",
                        level="error",
                        prefix="[duration]",
                    )

        log_event(
            f"Manual duration check done: {checked} checked, {corrected} corrected, "
            f"{skipped} skipped, {errors} error(s).",
            level="error" if errors else "info",
            prefix="[duration]",
        )
        return checked, corrected, skipped, errors

    async def _relic_hunt_autostart():
        """Auto-start the Relic Hunt IRC listener on app boot if the game is enabled
        and the exp_radio_twitch credentials are configured."""
        await asyncio.sleep(3)  # brief delay so DB is fully ready
        try:
            await db.ensure_relic_tables()
            enabled = (await db.relic_get_setting("enabled")) != "false"
            if not enabled:
                return
            client_id   = await db.get_setting("exp_radio_twitch_client_id")
            refresh_tok = await db.get_setting("exp_radio_twitch_refresh_token")
            broadcaster = await db.get_setting("exp_radio_twitch_broadcaster_login")
            if not (client_id and refresh_tok and broadcaster):
                _rh_log("Twitch credentials not configured, skipping auto-start", "error", "[relic-hunt]")
                return
            bot = _TwitchBot(db, key_prefix="exp_radio_twitch")
            ok, msg = await bot.start()
            if not ok:
                _rh_log(f"Auto-start failed: {msg}", "error", "[relic-hunt]")
                return
            await relic_hunt.start(bot)
            _rh_log("Auto-started successfully", "info", "[relic-hunt]")
        except Exception as e:
            _rh_log(f"Auto-start error: {e}", "error", "[relic-hunt]")

    async def _trya_relic_hunt_autostart():
        await asyncio.sleep(3)
        try:
            await db.ensure_relic_tables()
            enabled = (await db.relic_get_setting("enabled")) != "false"
            listener_enabled = (
                await db.get_setting("trya_stream_relic_hunt_enabled") or "on"
            ) == "on"
            if not enabled or not listener_enabled:
                return
            client_id = await db.get_setting("trya_stream_twitch_client_id")
            refresh_token = await db.get_setting("trya_stream_twitch_refresh_token")
            broadcaster = await db.get_setting("trya_stream_twitch_broadcaster_login")
            exp_broadcaster = await db.get_setting("exp_radio_twitch_broadcaster_login")
            exp_refresh_token = await db.get_setting("exp_radio_twitch_refresh_token")
            if (
                broadcaster
                and exp_broadcaster
                and exp_refresh_token
                and broadcaster.strip().lower() == exp_broadcaster.strip().lower()
            ):
                _trya_log(
                    "TrYa and Exp. Radio use the same Twitch channel; the existing Exp. listener owns Raven's Nest to prevent duplicate command handling",
                    "info",
                    "[trya-relic-hunt]",
                )
                return
            if not (client_id and refresh_token and broadcaster):
                _trya_log(
                    "Twitch credentials not configured, skipping TrYa Raven's Nest listener",
                    "error",
                    "[trya-relic-hunt]",
                )
                return
            twitch = _TwitchBot(db, key_prefix="trya_stream_twitch")
            ok, message = await twitch.start()
            if not ok:
                _trya_log(
                    f"Auto-start failed: {message}", "error", "[trya-relic-hunt]"
                )
                return
            await trya_relic_hunt.start(twitch)
            _trya_log("Auto-started successfully", "info", "[trya-relic-hunt]")
        except Exception as exc:
            _trya_log(f"Auto-start error: {exc}", "error", "[trya-relic-hunt]")

    async def _trya_stream_relevant_songs() -> list[dict]:
        active_playlist = (await db.get_setting("trya_stream_active_playlist")) or "submission"
        if active_playlist == "both":
            songs = await db.get_all_trya_stream_songs(active_only=True, source="submission")
            songs += await db.get_all_trya_stream_songs(active_only=True, source="admin")
        else:
            songs = await db.get_all_trya_stream_songs(active_only=True, source=active_playlist)

        if (await db.get_setting("trya_stream_intro_enabled") or "off") == "on":
            songs += await db.get_all_trya_stream_songs(active_only=True, source="intro")
        if (await db.get_setting("trya_stream_outro_enabled") or "off") == "on":
            songs += await db.get_all_trya_stream_songs(active_only=True, source="outro")

        seen: set[int] = set()
        unique: list[dict] = []
        for song in songs:
            song_id = int(song.get("id") or 0)
            if song_id and song_id in seen:
                continue
            if song_id:
                seen.add(song_id)
            unique.append(song)
        return unique

    async def _check_trya_stream_durations() -> tuple[int, int, int, int]:
        from bot.trya_stream_manager import log_event

        songs = await _trya_stream_relevant_songs()
        checked = corrected = skipped = errors = 0
        log_event(
            f"Manual duration check started for {len(songs)} active stream song(s).",
            prefix="[duration]",
        )
        for song in songs:
            title = song.get("title") or song.get("mp3_filename") or f"#{song.get('id')}"
            if not song.get("mp3_filename"):
                skipped += 1
                log_event(f"Skipped #{song.get('id')} ({title!r}): no MP3 file.", prefix="[duration]")
                continue
            checked += 1
            try:
                probed = await trya_stream_manager._probe_audio_duration(song)
            except Exception as exc:
                probed = None
                log_event(
                    f"Duration probe failed for #{song.get('id')} ({title!r}): {exc}",
                    level="error",
                    prefix="[duration]",
                )
            if probed is None:
                errors += 1
                continue
            try:
                stored = float(song.get("duration") or 0)
            except (TypeError, ValueError):
                stored = 0.0
            if not stored or abs(probed - stored) > 1.0:
                try:
                    await db.update_trya_stream_song(song["id"], duration=probed)
                    corrected += 1
                    log_event(
                        f"Corrected #{song.get('id')} ({title!r}): "
                        f"{_fmt_exp_duration(stored)} -> {_fmt_exp_duration(probed)} "
                        f"({stored:.1f}s -> {probed:.1f}s).",
                        prefix="[duration]",
                    )
                except Exception as exc:
                    errors += 1
                    log_event(
                        f"Duration DB update failed for #{song.get('id')} ({title!r}): {exc}",
                        level="error",
                        prefix="[duration]",
                    )
        log_event(
            f"Manual duration check done: {checked} checked, {corrected} corrected, "
            f"{skipped} skipped, {errors} error(s).",
            level="error" if errors else "info",
            prefix="[duration]",
        )
        return checked, corrected, skipped, errors

    @app.route("/trya-dcs", methods=["GET", "POST"])
    @permission_required("trya_dcs")
    async def trya_dcs_admin():
        """Configuration boundary for the private Discord Community Stream."""
        admin_csrf = session.get("trya_dcs_admin_csrf")
        if not admin_csrf:
            admin_csrf = secrets.token_urlsafe(32)
            session["trya_dcs_admin_csrf"] = admin_csrf
        defaults = {
            "trya_dcs_enabled": "off",
            "trya_dcs_guild_id": str(Config.GUILD_ID or ""),
            "trya_dcs_chat_channel_id": "",
            "trya_dcs_public_url": f"{_public_web_url()}/trya-dcs/player",
            "trya_dcs_stream_path": "trya-dcs",
            "trya_dcs_video_bitrate_kbps": "2500",
            "trya_dcs_audio_bitrate_kbps": "192",
            "trya_dcs_stream_token_ttl_seconds": "600",
            "trya_dcs_membership_recheck_seconds": "300",
            "trya_dcs_rtmp_ingest_url": "rtmp://mediamtx:1935/trya-dcs",
            "trya_dcs_disclaimer": "AI-generated audio and visuals.",
            "trya_dcs_max_per_user": "4",
            "trya_dcs_max_duration_seconds": "360",
            "trya_dcs_max_upload_mib": "20",
            "trya_dcs_loop_mode": "stop",
            "trya_dcs_bg_filename": "",
            "trya_dcs_bg_type": "image",
            "trya_dcs_media_corners_enabled": "off",
            "trya_dcs_media_corner_radius": "28",
            "trya_dcs_media_border_enabled": "off",
            "trya_dcs_media_border_width": "3",
            "trya_dcs_media_border_color": "#A855F7",
            "trya_dcs_stream_title": "TrYa Discord Community Stream",
            "trya_dcs_moderation_enabled": "off",
            "trya_dcs_intro_enabled": "off",
            "trya_dcs_intro_selection": "random",
            "trya_dcs_outro_enabled": "off",
            "trya_dcs_outro_selection": "random",
            "trya_dcs_obs_enabled": "off",
            "trya_dcs_obs_stream_key": "",
            "trya_dcs_obs_fps": "20",
            "trya_dcs_relic_hunt_enabled": "on",
            "trya_dcs_info_top_hunters": "on",
            "trya_dcs_info_commands": "on",
            "trya_dcs_info_recent_finds": "on",
            "trya_dcs_info_recent_combines": "on",
            "trya_dcs_info_ritual": "on",
            "trya_dcs_info_phrase": "on",
            "trya_dcs_info_custom_enabled": "off",
            "trya_dcs_info_custom_title": "Community information",
            "trya_dcs_info_custom_text": "",
            "trya_dcs_info_rotation_seconds": "12",
        }

        async def get_dcs_loop_videos() -> list[dict]:
            try:
                raw = json.loads(await db.get_setting("trya_dcs_loop_videos") or "[]")
            except (TypeError, json.JSONDecodeError):
                raw = []
            videos = []
            for item in raw if isinstance(raw, list) else []:
                if not isinstance(item, dict):
                    continue
                filename = os.path.basename(str(item.get("filename") or ""))
                if filename and os.path.isfile(os.path.join(TRYA_DCS_DIR, "assets", filename)):
                    videos.append({
                        "filename": filename,
                        "label": str(item.get("label") or filename)[:100],
                    })
            return videos

        def invalidate_dcs_loop_cache() -> None:
            assets_dir = os.path.join(TRYA_DCS_DIR, "assets")
            for filename in (
                "_concat_all.mp4", "_concat_all.hash",
                "_concat_all_random.mp4", "_concat_all_random.hash",
            ):
                try:
                    os.remove(os.path.join(assets_dir, filename))
                except FileNotFoundError:
                    pass

        if request.method == "POST":
            form = await request.form
            submitted_csrf = str(form.get("csrf_token") or "")
            if not submitted_csrf or not hmac.compare_digest(
                submitted_csrf, admin_csrf
            ):
                await flash("The DCS form expired. Please try again.", "error")
                return redirect(request.url)
            action = form.get("action", "save_settings")
            if action == "save_dcs_relic_display":
                try:
                    rotation_seconds = max(5, min(60, int(form.get("info_rotation_seconds") or "12")))
                except (TypeError, ValueError):
                    rotation_seconds = 12
                values = {
                    "trya_dcs_relic_hunt_enabled": "on" if form.get("relic_hunt_enabled") else "off",
                    "trya_dcs_info_top_hunters": "on" if form.get("info_top_hunters") else "off",
                    "trya_dcs_info_commands": "on" if form.get("info_commands") else "off",
                    "trya_dcs_info_recent_finds": "on" if form.get("info_recent_finds") else "off",
                    "trya_dcs_info_recent_combines": "on" if form.get("info_recent_combines") else "off",
                    "trya_dcs_info_ritual": "on" if form.get("info_ritual") else "off",
                    "trya_dcs_info_phrase": "on" if form.get("info_phrase") else "off",
                    "trya_dcs_info_custom_enabled": "on" if form.get("info_custom_enabled") else "off",
                    "trya_dcs_info_custom_title": str(form.get("info_custom_title") or "Community information").strip()[:100],
                    "trya_dcs_info_custom_text": str(form.get("info_custom_text") or "").strip()[:3000],
                    "trya_dcs_info_rotation_seconds": str(rotation_seconds),
                }
                for key, value in values.items():
                    await db.set_setting(key, value)
                from bot.trya_dcs_manager import log_dcs_event
                log_dcs_event("DCS Raven's Nest display settings saved.")
                await flash("DCS Raven's Nest display settings saved.", "success")
                return redirect(request.url)
            if action == "upload_dcs_loop_video":
                if trya_dcs_manager.is_running:
                    await flash("Stop TrYa DCS before changing overlay videos.", "error")
                    return redirect(request.url)
                files = await request.files
                uploaded = files.get("dcs_loop_file")
                if not uploaded or not uploaded.filename:
                    await flash("Select an MP4 or WebM overlay video.", "error")
                    return redirect(request.url)
                extension = os.path.splitext(uploaded.filename)[1].lower()
                if extension not in {".mp4", ".webm"}:
                    await flash("Only MP4 and WebM overlay videos are accepted.", "error")
                    return redirect(request.url)
                assets_dir = os.path.join(TRYA_DCS_DIR, "assets")
                os.makedirs(assets_dir, exist_ok=True)
                import tempfile
                fd, staged_path = tempfile.mkstemp(prefix="dcs_loop_upload_", suffix=extension, dir=assets_dir)
                os.close(fd)
                try:
                    await uploaded.save(staged_path)
                    size = os.path.getsize(staged_path)
                    if size <= 0 or size > 200 * 1024 * 1024:
                        raise ValueError("Overlay videos must be between 1 byte and 200 MiB.")
                    with open(staged_path, "rb") as handle:
                        signature = handle.read(16)
                    if extension == ".mp4" and signature[4:8] != b"ftyp":
                        raise ValueError("The uploaded file does not have a valid MP4 signature.")
                    if extension == ".webm" and not signature.startswith(b"\x1aE\xdf\xa3"):
                        raise ValueError("The uploaded file does not have a valid WebM signature.")
                    probe = await asyncio.create_subprocess_exec(
                        "ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=width,height:format=duration",
                        "-of", "json", staged_path,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    stdout, _ = await asyncio.wait_for(probe.communicate(), timeout=30)
                    metadata = json.loads(stdout.decode("utf-8") or "{}") if probe.returncode == 0 else {}
                    stream = (metadata.get("streams") or [{}])[0]
                    width = int(stream.get("width") or 0)
                    height = int(stream.get("height") or 0)
                    duration = float((metadata.get("format") or {}).get("duration") or 0)
                    if not width or not height or width > 7680 or height > 4320:
                        raise ValueError("The overlay video could not be decoded or exceeds 7680×4320 pixels.")
                    if duration <= 0 or duration > 3600:
                        raise ValueError("Overlay video duration must be between 0 and 60 minutes.")
                    filename = f"dcs_loop_{secrets.token_hex(12)}{extension}"
                    os.replace(staged_path, os.path.join(assets_dir, filename))
                    videos = await get_dcs_loop_videos()
                    videos.append({
                        "filename": filename,
                        "label": (form.get("dcs_loop_label") or uploaded.filename).strip()[:100],
                    })
                    await db.set_setting("trya_dcs_loop_videos", json.dumps(videos))
                    await db.set_setting("trya_dcs_loop_random_rotation", "{}")
                    if len(videos) == 1:
                        await db.set_setting("trya_dcs_loop_selection", filename)
                    invalidate_dcs_loop_cache()
                    from bot.trya_dcs_manager import log_dcs_event
                    log_dcs_event(
                        f"DCS overlay uploaded: {filename} ({width}x{height}, {duration:.1f}s)."
                    )
                    await flash("DCS overlay video uploaded.", "success")
                except (ValueError, OSError, asyncio.TimeoutError, json.JSONDecodeError) as exc:
                    await flash(str(exc) or "The overlay video could not be validated.", "error")
                finally:
                    try:
                        os.remove(staged_path)
                    except FileNotFoundError:
                        pass
                return redirect(request.url)
            if action == "delete_dcs_loop_video":
                if trya_dcs_manager.is_running:
                    await flash("Stop TrYa DCS before deleting overlay videos.", "error")
                    return redirect(request.url)
                filename = os.path.basename(str(form.get("loop_filename") or ""))
                videos = await get_dcs_loop_videos()
                if not filename or not any(video["filename"] == filename for video in videos):
                    await flash("The DCS overlay video no longer exists.", "error")
                    return redirect(request.url)
                videos = [video for video in videos if video["filename"] != filename]
                await db.set_setting("trya_dcs_loop_videos", json.dumps(videos))
                await db.set_setting("trya_dcs_loop_random_rotation", "{}")
                selection = await db.get_setting("trya_dcs_loop_selection") or "shuffle"
                if selection == filename:
                    await db.set_setting("trya_dcs_loop_selection", "shuffle")
                try:
                    os.remove(os.path.join(TRYA_DCS_DIR, "assets", filename))
                except FileNotFoundError:
                    pass
                invalidate_dcs_loop_cache()
                from bot.trya_dcs_manager import log_dcs_event
                log_dcs_event(f"DCS overlay deleted: {filename}.")
                await flash("DCS overlay video deleted.", "success")
                return redirect(request.url)
            if action == "set_dcs_loop_selection":
                if trya_dcs_manager.is_running:
                    await flash("Stop TrYa DCS before changing the overlay mode.", "error")
                    return redirect(request.url)
                videos = await get_dcs_loop_videos()
                selection = str(form.get("dcs_loop_selection") or "shuffle")
                modes = {"shuffle", "concat_all", "concat_all_random", "concat_random_subset"}
                filenames = {video["filename"] for video in videos}
                if selection not in modes and selection not in filenames:
                    selection = "shuffle"
                try:
                    count = int(form.get("dcs_loop_random_count") or "10")
                except (TypeError, ValueError):
                    count = 10
                count = max(1, min(count, len(videos))) if videos else 1
                await db.set_setting("trya_dcs_loop_selection", selection)
                await db.set_setting("trya_dcs_loop_random_count", str(count))
                await db.set_setting("trya_dcs_loop_random_rotation", "{}")
                from bot.trya_dcs_manager import log_dcs_event
                log_dcs_event(f"DCS overlay mode saved: {selection} (count={count}).")
                await flash("DCS overlay selection saved.", "success")
                return redirect(request.url)
            if action == "save_dcs_visuals":
                if trya_dcs_manager.is_running:
                    await flash("Stop TrYa DCS before changing background or media-frame settings.", "error")
                    return redirect(request.url)
                def visual_int(name: str, default: int, low: int, high: int) -> int:
                    try:
                        return max(low, min(high, int(form.get(name) or default)))
                    except (TypeError, ValueError):
                        return default
                radius = visual_int("media_corner_radius", 28, 1, 120)
                border_width = visual_int("media_border_width", 3, 1, 20)
                border_color = str(form.get("media_border_color") or "#A855F7").strip().upper()
                if not re.fullmatch(r"#[0-9A-F]{6}", border_color):
                    await flash("Media border color must use #RRGGBB format.", "error")
                    return redirect(request.url)
                files = await request.files
                uploaded = files.get("dcs_background")
                staged_path = ""
                new_filename = ""
                new_type = ""
                if uploaded and uploaded.filename:
                    extension = os.path.splitext(uploaded.filename)[1].lower()
                    allowed = {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".webm"}
                    if extension not in allowed:
                        await flash("Background must be PNG, JPEG, WebP, MP4 or WebM.", "error")
                        return redirect(request.url)
                    new_type = "video" if extension in {".mp4", ".webm"} else "image"
                    assets_dir = os.path.join(TRYA_DCS_DIR, "assets")
                    os.makedirs(assets_dir, exist_ok=True)
                    import tempfile
                    fd, staged_path = tempfile.mkstemp(prefix="dcs_bg_upload_", suffix=extension, dir=assets_dir)
                    os.close(fd)
                    try:
                        await uploaded.save(staged_path)
                        size = os.path.getsize(staged_path)
                        maximum = 200 * 1024 * 1024 if new_type == "video" else 20 * 1024 * 1024
                        if size <= 0 or size > maximum:
                            raise ValueError(
                                f"Background must be between 1 byte and {maximum // (1024 * 1024)} MiB."
                            )
                        with open(staged_path, "rb") as handle:
                            signature = handle.read(16)
                        valid_signature = (
                            extension in {".jpg", ".jpeg"} and signature.startswith(b"\xff\xd8\xff")
                            or extension == ".png" and signature.startswith(b"\x89PNG\r\n\x1a\n")
                            or extension == ".webp" and signature[:4] == b"RIFF" and signature[8:12] == b"WEBP"
                            or extension == ".mp4" and signature[4:8] == b"ftyp"
                            or extension == ".webm" and signature.startswith(b"\x1aE\xdf\xa3")
                        )
                        if not valid_signature:
                            raise ValueError("Background file signature does not match its extension.")
                        probe = await asyncio.create_subprocess_exec(
                            "ffprobe", "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=width,height:format=duration",
                            "-of", "json", staged_path,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                        )
                        stdout, _ = await asyncio.wait_for(probe.communicate(), timeout=30)
                        metadata = json.loads(stdout.decode("utf-8") or "{}") if probe.returncode == 0 else {}
                        stream = (metadata.get("streams") or [{}])[0]
                        width = int(stream.get("width") or 0)
                        height = int(stream.get("height") or 0)
                        if not width or not height or width > 8192 or height > 8192:
                            raise ValueError("Background could not be decoded or exceeds 8192×8192 pixels.")
                        if new_type == "video":
                            duration = float((metadata.get("format") or {}).get("duration") or 0)
                            if duration <= 0 or duration > 3600:
                                raise ValueError("Background video duration must be between 0 and 60 minutes.")
                        new_filename = f"dcs_bg_{secrets.token_hex(12)}{extension}"
                        os.replace(staged_path, os.path.join(assets_dir, new_filename))
                    except (ValueError, OSError, asyncio.TimeoutError, json.JSONDecodeError) as exc:
                        await flash(str(exc) or "The background could not be validated.", "error")
                        return redirect(request.url)
                    finally:
                        if staged_path:
                            try:
                                os.remove(staged_path)
                            except FileNotFoundError:
                                pass
                old_filename = os.path.basename(
                    await db.get_setting("trya_dcs_bg_filename") or ""
                )
                remove_background = bool(form.get("remove_background"))
                if new_filename:
                    await db.set_setting("trya_dcs_bg_filename", new_filename)
                    await db.set_setting("trya_dcs_bg_type", new_type)
                elif remove_background:
                    await db.set_setting("trya_dcs_bg_filename", "")
                    await db.set_setting("trya_dcs_bg_type", "image")
                await db.set_setting(
                    "trya_dcs_media_corners_enabled",
                    "on" if form.get("media_corners_enabled") else "off",
                )
                await db.set_setting("trya_dcs_media_corner_radius", str(radius))
                await db.set_setting(
                    "trya_dcs_media_border_enabled",
                    "on" if form.get("media_border_enabled") else "off",
                )
                await db.set_setting("trya_dcs_media_border_width", str(border_width))
                await db.set_setting("trya_dcs_media_border_color", border_color)
                if old_filename and (new_filename or remove_background):
                    try:
                        os.remove(os.path.join(TRYA_DCS_DIR, "assets", old_filename))
                    except FileNotFoundError:
                        pass
                from bot.trya_dcs_manager import log_dcs_event
                log_dcs_event(
                    f"DCS visuals saved: background={new_filename or ('none' if remove_background else old_filename or 'none')}, corners={bool(form.get('media_corners_enabled'))}, border={bool(form.get('media_border_enabled'))}."
                )
                await flash("DCS background and media-frame settings saved.", "success")
                return redirect(request.url)
            if action == "upload_offline_image":
                files = await request.files
                uploaded = files.get("offline_image")
                if not uploaded or not uploaded.filename:
                    await flash("Select a PNG, JPEG or WebP offline image.", "error")
                    return redirect(request.url)
                assets_dir = os.path.join(TRYA_DCS_DIR, "assets")
                os.makedirs(assets_dir, exist_ok=True)
                import tempfile
                fd, staged_path = tempfile.mkstemp(prefix="offline_", dir=assets_dir)
                os.close(fd)
                try:
                    await uploaded.save(staged_path)
                    size = os.path.getsize(staged_path)
                    if size <= 0 or size > 10 * 1024 * 1024:
                        raise ValueError("Offline images must be between 1 byte and 10 MiB.")
                    with open(staged_path, "rb") as handle:
                        signature = handle.read(16)
                    if signature.startswith(b"\x89PNG\r\n\x1a\n"):
                        extension = ".png"
                    elif signature.startswith(b"\xff\xd8\xff"):
                        extension = ".jpg"
                    elif signature[:4] == b"RIFF" and signature[8:12] == b"WEBP":
                        extension = ".webp"
                    else:
                        raise ValueError("The file signature is not PNG, JPEG or WebP.")
                    process = await asyncio.create_subprocess_exec(
                        "ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=codec_name,width,height",
                        "-of", "json", staged_path,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    stdout, _ = await asyncio.wait_for(process.communicate(), timeout=20)
                    probe = json.loads(stdout.decode("utf-8") or "{}") if process.returncode == 0 else {}
                    stream = (probe.get("streams") or [{}])[0]
                    width = int(stream.get("width") or 0)
                    height = int(stream.get("height") or 0)
                    if not width or not height or width > 8192 or height > 8192:
                        raise ValueError("The image could not be decoded or exceeds 8192×8192 pixels.")
                    filename = f"offline_{secrets.token_hex(8)}{extension}"
                    final_path = os.path.join(assets_dir, filename)
                    os.replace(staged_path, final_path)
                    old_filename = os.path.basename(
                        await db.get_setting("trya_dcs_offline_image_filename") or ""
                    )
                    await db.set_setting("trya_dcs_offline_image_filename", filename)
                    if old_filename and old_filename != filename:
                        try:
                            os.remove(os.path.join(assets_dir, old_filename))
                        except FileNotFoundError:
                            pass
                    from bot.trya_dcs_manager import log_dcs_event
                    log_dcs_event(f"Offline image updated: {filename} ({width}x{height}).")
                    await flash("DCS offline image saved.", "success")
                except (ValueError, OSError, asyncio.TimeoutError, json.JSONDecodeError) as exc:
                    await flash(str(exc) or "The offline image could not be validated.", "error")
                finally:
                    try:
                        os.remove(staged_path)
                    except FileNotFoundError:
                        pass
                return redirect(request.url)
            if action == "remove_offline_image":
                filename = os.path.basename(
                    await db.get_setting("trya_dcs_offline_image_filename") or ""
                )
                await db.set_setting("trya_dcs_offline_image_filename", "")
                if filename:
                    try:
                        os.remove(os.path.join(TRYA_DCS_DIR, "assets", filename))
                    except FileNotFoundError:
                        pass
                from bot.trya_dcs_manager import log_dcs_event
                log_dcs_event("Offline image removed.")
                await flash("DCS offline image removed.", "success")
                return redirect(request.url)
            if action == "start_stream":
                actor = session.get("username") or "admin"
                result = await trya_dcs_manager.start(created_by=actor)
                await flash(
                    f"DCS publisher started with {result.get('song_count')} songs."
                    if result.get("ok") else result.get("error", "Could not start DCS."),
                    "success" if result.get("ok") else "error",
                )
                return redirect(request.url)
            if action == "safe_stop_stream":
                result = await trya_dcs_manager.safe_stop()
                await flash(
                    "DCS will stop after the current song."
                    if result.get("ok") else result.get("error", "Could not request safe stop."),
                    "success" if result.get("ok") else "error",
                )
                return redirect(request.url)
            if action == "stop_stream":
                result = await trya_dcs_manager.stop()
                await flash(
                    "DCS publisher stopped." if result.get("ok") else result.get("error", "Could not stop DCS."),
                    "success" if result.get("ok") else "error",
                )
                return redirect(request.url)
            if action in {"remove_song", "retry_whisper", "retry_moderation", "import_transcript"}:
                try:
                    song_id = int(form.get("song_id") or 0)
                except (TypeError, ValueError):
                    song_id = 0
                song = await db.get_trya_dcs_song(song_id)
                if not song:
                    await flash("The DCS submission no longer exists.", "error")
                    return redirect(request.url)
                if trya_dcs_manager.is_running:
                    await flash("Stop TrYa DCS before changing or reprocessing playlist songs.", "error")
                    return redirect(request.url)
                from bot.trya_dcs_manager import log_dcs_event
                if action == "remove_song":
                    if not song.get("active"):
                        await flash("This song is already outside the active playlists.", "error")
                    else:
                        await db.delete_trya_dcs_song(song_id)
                        log_dcs_event(f"Admin removed #{song_id} from the active playlists.")
                        await flash("Song removed from the active playlists; evidence and files were retained.", "success")
                    return redirect(request.url)
                if not song.get("active") or not song.get("uploaded_at") or not song.get("mp3_filename"):
                    await flash("Only active, completed uploads can be reprocessed.", "error")
                    return redirect(request.url)
                from bot.trya_dcs_worker import (
                    import_dcs_transcript,
                    moderate_dcs_song,
                    process_dcs_song,
                )
                if action == "retry_whisper":
                    try:
                        max_duration = max(
                            60,
                            min(1200, int(await db.get_setting("trya_dcs_max_duration_seconds") or "360")),
                        )
                    except (TypeError, ValueError):
                        max_duration = 360
                    await db.update_trya_dcs_song(
                        song_id,
                        analysis_status="pending",
                        approval_status="pending",
                        approved_at=None,
                        approved_by=None,
                    )
                    log_dcs_event(f"Admin queued Whisper retry for #{song_id}.")
                    asyncio.create_task(
                        process_dcs_song(
                            db, song_id, TRYA_DCS_DIR,
                            max_duration_seconds=max_duration,
                            run_moderation=False,
                        )
                    )
                    await flash("Whisper transcription queued again.", "success")
                elif action == "retry_moderation":
                    if (await db.get_setting("trya_dcs_moderation_enabled") or "off") != "on":
                        await flash("Enable automated LLM lyric review before retrying moderation.", "error")
                        return redirect(request.url)
                    if not str(song.get("lyrics") or "").strip():
                        await flash("This song has no lyrics to moderate. Import or retry its transcription first.", "error")
                        return redirect(request.url)
                    log_dcs_event(f"Admin queued moderation retry for #{song_id}.")
                    asyncio.create_task(moderate_dcs_song(db, song_id))
                    await flash("Lyric moderation queued again.", "success")
                else:
                    transcript_json = str(form.get("transcript_json") or "")
                    try:
                        word_count = await import_dcs_transcript(
                            db, song_id, TRYA_DCS_DIR, transcript_json
                        )
                    except ValueError as exc:
                        log_dcs_event(f"External transcript for #{song_id} rejected: {exc}", "error")
                        await flash(str(exc), "error")
                    else:
                        await flash(f"External transcript imported with {word_count} words.", "success")
                return redirect(request.url)
            if action == "save_wlm_url":
                try:
                    song_id = int(form.get("song_id") or 0)
                except (TypeError, ValueError):
                    song_id = 0
                song = await db.get_trya_dcs_song(song_id)
                wlm_url = (form.get("wlm_url") or "").strip()
                if not song:
                    await flash("The DCS submission no longer exists.", "error")
                    return redirect(request.url)
                if wlm_url:
                    wlm_match = re.fullmatch(
                        r"https://www\.welovemusic\.ai/track/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})/?",
                        wlm_url,
                    )
                    if not wlm_match:
                        await flash("Enter a valid WeLoveMusic track URL or leave it empty.", "error")
                        return redirect(request.url)
                    wlm_url = f"https://www.welovemusic.ai/track/{wlm_match.group(1).lower()}"
                await db.update_trya_dcs_song(song_id, wlm_url=wlm_url)
                for playlist_song in trya_dcs_manager.playlist:
                    if int(playlist_song.get("id") or 0) == song_id:
                        playlist_song["wlm_url"] = wlm_url
                if (
                    trya_dcs_manager.current_song
                    and int(trya_dcs_manager.current_song.get("id") or 0) == song_id
                ):
                    trya_dcs_manager.current_song["wlm_url"] = wlm_url
                await flash(
                    "WeLoveMusic URL saved." if wlm_url else "WeLoveMusic URL removed.",
                    "success",
                )
                return redirect(request.url)
            if action == "purge_failed_upload":
                try:
                    song_id = int(form.get("song_id") or 0)
                except (TypeError, ValueError):
                    song_id = 0
                if trya_dcs_manager.is_running:
                    await flash("Stop TrYa DCS before permanently removing an upload.", "error")
                    return redirect(request.url)
                removed = await db.purge_failed_trya_dcs_song(song_id)
                if not removed:
                    await flash("Only failed or incomplete DCS uploads can be permanently removed.", "error")
                    return redirect(request.url)
                import glob
                targets = []
                for directory, field in (
                    ("originals", "original_archive_filename"),
                    ("mp3", "mp3_filename"),
                    ("ass", "ass_filename"),
                ):
                    filename = os.path.basename(str(removed.get(field) or ""))
                    if filename:
                        targets.append(os.path.join(TRYA_DCS_DIR, directory, filename))
                targets.extend(glob.glob(os.path.join(TRYA_DCS_DIR, "incoming", f"dcs_{song_id}_*")))
                targets.extend(glob.glob(os.path.join(TRYA_DCS_DIR, "cover_cache", f"hook_{song_id}_*")))
                remaining = await db.get_trya_dcs_songs(active_only=False)
                song_uuid = str(removed.get("suno_uuid") or "").strip()
                if song_uuid and not any(
                    str(item.get("suno_uuid") or "").strip().lower() == song_uuid.lower()
                    for item in remaining
                ):
                    for extension in (".jpg", ".mp4"):
                        targets.append(os.path.join(TRYA_DCS_DIR, "cover_cache", f"{song_uuid}{extension}"))
                deleted_files = 0
                allowed_root = os.path.abspath(TRYA_DCS_DIR)
                for path in targets:
                    absolute = os.path.abspath(path)
                    if os.path.commonpath((absolute, allowed_root)) != allowed_root:
                        continue
                    try:
                        os.remove(absolute)
                        deleted_files += 1
                    except FileNotFoundError:
                        pass
                    except OSError as exc:
                        print(f"[trya-dcs] Failed to remove {absolute}: {exc}", flush=True)
                await flash(
                    f"Permanently removed failed upload #{song_id} and {deleted_files} file(s).",
                    "success",
                )
                return redirect(request.url)
            if action == "set_playlist_source":
                try:
                    song_id = int(form.get("song_id") or 0)
                except (TypeError, ValueError):
                    song_id = 0
                source = (form.get("playlist_source") or "submission").strip()
                if source not in {"submission", "intro", "outro"}:
                    await flash("Invalid DCS playlist source.", "error")
                elif not await db.get_trya_dcs_song(song_id):
                    await flash("The DCS submission no longer exists.", "error")
                elif trya_dcs_manager.is_running:
                    await flash("Stop TrYa DCS before changing playlist assignments.", "error")
                else:
                    await db.update_trya_dcs_song(song_id, playlist_source=source)
                    await flash(f"Song assigned to the {source} playlist.", "success")
                return redirect(request.url)
            if action in {"approve_song", "reject_song"}:
                if (await db.get_setting("trya_dcs_moderation_enabled") or "off") != "on":
                    await flash("Manual approval is disabled because automated moderation is off.", "error")
                    return redirect(request.url)
                try:
                    song_id = int(form.get("song_id") or 0)
                except (TypeError, ValueError):
                    song_id = 0
                song = await db.get_trya_dcs_song(song_id)
                if not song:
                    await flash("The DCS submission no longer exists.", "error")
                    return redirect(request.url)
                if action == "approve_song" and song.get("analysis_status") != "done":
                    await flash("Wait for the song analysis to finish before approving it.", "error")
                    return redirect(request.url)
                actor = (session.get("username") or "admin")[:100]
                if action == "approve_song":
                    await db.update_trya_dcs_song(
                        song_id,
                        approval_status="approved",
                        approved_at=time.time(),
                        approved_by=actor,
                    )
                    await flash(f"Approved {song.get('title') or f'Song #{song_id}' }.", "success")
                else:
                    await db.update_trya_dcs_song(
                        song_id,
                        approval_status="rejected",
                        approved_at=None,
                        approved_by=actor,
                    )
                    await flash(f"Rejected {song.get('title') or f'Song #{song_id}' }.", "success")
                return redirect(request.url)
            if action == "save_settings":
                guild_id = (form.get("guild_id") or "").strip()
                chat_channel_id = (form.get("chat_channel_id") or "").strip()
                if guild_id and not guild_id.isdigit():
                    await flash("Discord Guild ID must contain digits only.", "error")
                    return redirect(request.url)
                if chat_channel_id and not chat_channel_id.isdigit():
                    await flash("Chat channel ID must contain digits only.", "error")
                    return redirect(request.url)

                def bounded_int(name: str, default: int, low: int, high: int) -> int:
                    try:
                        return max(low, min(high, int(form.get(name, default))))
                    except (TypeError, ValueError):
                        return default

                public_url = (form.get("public_url") or defaults["trya_dcs_public_url"]).strip()[:500]
                rtmp_url = (form.get("rtmp_ingest_url") or defaults["trya_dcs_rtmp_ingest_url"]).strip()[:500]
                disclaimer = (form.get("disclaimer") or "").strip()[:2000]
                obs_key = (
                    form.get("obs_stream_key")
                    or await db.get_setting("trya_dcs_obs_stream_key")
                    or ""
                ).strip()[:200]
                if form.get("obs_enabled") and not obs_key:
                    obs_key = secrets.token_urlsafe(24)
                values = {
                    "trya_dcs_enabled": "on" if form.get("enabled") else "off",
                    "trya_dcs_guild_id": guild_id,
                    "trya_dcs_chat_channel_id": chat_channel_id,
                    "trya_dcs_public_url": public_url,
                    "trya_dcs_stream_path": "trya-dcs",
                    "trya_dcs_video_bitrate_kbps": str(
                        bounded_int("video_bitrate_kbps", 2500, 1000, 6000)
                    ),
                    "trya_dcs_audio_bitrate_kbps": str(
                        bounded_int("audio_bitrate_kbps", 192, 96, 320)
                    ),
                    "trya_dcs_stream_token_ttl_seconds": str(
                        bounded_int("stream_token_ttl_seconds", 600, 300, 900)
                    ),
                    "trya_dcs_membership_recheck_seconds": str(
                        bounded_int("membership_recheck_seconds", 300, 60, 900)
                    ),
                    "trya_dcs_rtmp_ingest_url": rtmp_url,
                    "trya_dcs_disclaimer": disclaimer,
                    "trya_dcs_max_per_user": str(
                        bounded_int("max_per_user", 4, 1, 20)
                    ),
                    "trya_dcs_max_duration_seconds": str(
                        bounded_int("max_duration_seconds", 360, 60, 1200)
                    ),
                    "trya_dcs_max_upload_mib": str(
                        bounded_int("max_upload_mib", 20, 5, 100)
                    ),
                    "trya_dcs_loop_mode": (
                        "reshuffle" if form.get("loop_mode") == "reshuffle" else "stop"
                    ),
                    "trya_dcs_stream_title": (
                        form.get("stream_title") or ""
                    ).strip()[:200],
                    "trya_dcs_moderation_enabled": (
                        "on" if form.get("moderation_enabled") else "off"
                    ),
                    "trya_dcs_intro_enabled": (
                        "on" if form.get("intro_enabled") else "off"
                    ),
                    "trya_dcs_intro_selection": (
                        form.get("intro_selection") or "random"
                    ).strip(),
                    "trya_dcs_outro_enabled": (
                        "on" if form.get("outro_enabled") else "off"
                    ),
                    "trya_dcs_outro_selection": (
                        form.get("outro_selection") or "random"
                    ).strip(),
                    "trya_dcs_obs_enabled": (
                        "on" if form.get("obs_enabled") else "off"
                    ),
                    "trya_dcs_obs_stream_key": obs_key,
                    "trya_dcs_obs_fps": str(
                        bounded_int("obs_fps", 20, 15, 24)
                    ),
                }
                for key, value in values.items():
                    await db.set_setting(key, value)
                if values["trya_dcs_moderation_enabled"] != "on":
                    await db.db.execute(
                        """UPDATE trya_dcs_songs
                           SET approval_status = 'approved', approved_at = unixepoch(),
                               approved_by = 'moderation-disabled', moderation_status = NULL,
                               moderation_reason = ''
                           WHERE active = 1 AND analysis_status = 'done'"""
                    )
                    await db.db.commit()
                else:
                    await db.db.execute(
                        """UPDATE trya_dcs_songs
                           SET approval_status = 'pending', approved_at = NULL,
                               approved_by = NULL, moderation_status = 'pending',
                               moderation_reason = 'Moderation was enabled; review is required.'
                           WHERE active = 1 AND analysis_status = 'done'
                             AND approved_by = 'moderation-disabled'"""
                    )
                    await db.db.commit()
                if values["trya_dcs_enabled"] != "on" and trya_dcs_manager.is_running:
                    await trya_dcs_manager.stop()
                await flash("TrYa DCS settings saved.", "success")
            return redirect(request.url)

        settings = {}
        for key, default in defaults.items():
            settings[key] = await db.get_setting(key) or default

        guild = get_guild()
        text_channels = []
        if guild:
            text_channels = [
                {"id": str(channel.id), "name": channel.name}
                for channel in sorted(guild.text_channels, key=lambda item: item.position)
            ]

        mediamtx_online = False
        mediamtx_detail = "MediaMTX service is not reachable yet."
        try:
            timeout = aiohttp.ClientTimeout(total=1.5)
            async with aiohttp.ClientSession(timeout=timeout) as client:
                async with client.get("http://mediamtx:9997/v3/paths/list") as response:
                    mediamtx_online = response.status < 500
                    mediamtx_detail = f"API responded with HTTP {response.status}."
        except Exception:
            pass

        stats = await db.get_trya_dcs_admin_stats()
        dcs_loop_videos = await get_dcs_loop_videos()
        dcs_background_filename = os.path.basename(settings["trya_dcs_bg_filename"])
        dcs_background_configured = bool(
            dcs_background_filename
            and os.path.isfile(os.path.join(TRYA_DCS_DIR, "assets", dcs_background_filename))
        )
        dcs_loop_selection = await db.get_setting("trya_dcs_loop_selection") or "shuffle"
        try:
            dcs_loop_random_count = int(
                await db.get_setting("trya_dcs_loop_random_count") or "10"
            )
        except (TypeError, ValueError):
            dcs_loop_random_count = 10
        if dcs_loop_videos:
            dcs_loop_random_count = max(1, min(dcs_loop_random_count, len(dcs_loop_videos)))
        else:
            dcs_loop_random_count = 1
        offline_image_filename = os.path.basename(
            await db.get_setting("trya_dcs_offline_image_filename") or ""
        )
        offline_image_configured = bool(
            offline_image_filename
            and os.path.isfile(os.path.join(TRYA_DCS_DIR, "assets", offline_image_filename))
        )
        songs = await db.get_trya_dcs_songs(active_only=False)
        songs_desc = list(reversed(songs))
        for song in songs_desc:
            song["public_suno_url"] = trya_dcs_manager._public_suno_url(song)
        submission_songs = [
            song for song in songs_desc
            if (song.get("playlist_source") or "submission") not in {"intro", "outro"}
            and song.get("remove_reason") != "removed_by_owner"
        ]
        intro_songs = [
            song for song in songs_desc
            if song.get("active") and song.get("playlist_source") == "intro"
        ]
        outro_songs = [
            song for song in songs_desc
            if song.get("active") and song.get("playlist_source") == "outro"
        ]
        stream_status = await trya_dcs_manager.get_status()
        presence_now = time.monotonic()
        app.trya_dcs_presence = {
            member_id: seen_at
            for member_id, seen_at in app.trya_dcs_presence.items()
            if presence_now - seen_at < 15
        }
        stream_status["listener_count"] = len(app.trya_dcs_presence)
        oauth_ready = bool(
            Config.DISCORD_CLIENT_ID
            and Config.DISCORD_CLIENT_SECRET
            and settings["trya_dcs_guild_id"]
        )
        stream_path = settings["trya_dcs_stream_path"]
        return await render_template(
            "trya_dcs.html",
            settings=settings,
            stats=stats,
            text_channels=text_channels,
            mediamtx_online=mediamtx_online,
            mediamtx_detail=mediamtx_detail,
            oauth_ready=oauth_ready,
            oauth_callback_url=f"{_public_web_url()}/trya-dcs/oauth/callback",
            protected_hls_url=f"{_public_web_url()}/dcs-stream/{stream_path}/index.m3u8",
            data_directory=TRYA_DCS_DIR,
            offline_image_configured=offline_image_configured,
            dcs_loop_videos=dcs_loop_videos,
            dcs_background_configured=dcs_background_configured,
            dcs_loop_selection=dcs_loop_selection,
            dcs_loop_random_count=dcs_loop_random_count,
            songs=songs_desc,
            submission_songs=submission_songs,
            intro_songs=intro_songs,
            outro_songs=outro_songs,
            stream_status=stream_status,
            stream_log=trya_dcs_manager.get_log(max_age_secs=900),
            admin_csrf=admin_csrf,
        )

    @app.route("/trya-dcs/stream/log")
    @permission_required("trya_dcs")
    async def trya_dcs_stream_log():
        try:
            since = float(request.args.get("since") or 0)
        except (TypeError, ValueError):
            since = 0
        from bot.trya_dcs_manager import get_dcs_log
        return {"entries": get_dcs_log(since_ts=since, max_age_secs=3600)}

    async def _trya_dcs_relic_payload() -> dict:
        await db.ensure_relic_tables()
        leaderboard = await db.relic_get_leaderboard(5)
        recent = await db.relic_get_recent_log(30)
        ritual = await db.relic_get_ritual()
        phrase = await db.relic_get_phrase_puzzle()
        from bot.relic_hunt import _phrase_progress
        phrase_display = (
            _phrase_progress(
                str(phrase.get("phrase") or ""),
                str(phrase.get("revealed_mask") or ""),
            )
            if phrase.get("enabled") else ""
        )
        active_events = await db.relic_get_active_events()
        event_defs = {
            event["id"]: event for event in await db.relic_get_all_events()
        }
        village = await db.relic_get_village_areas()
        prefix = (await db.relic_get_setting("command_prefix")) or "!"
        try:
            rotation_seconds = max(
                5,
                min(60, int(await db.get_setting("trya_dcs_info_rotation_seconds") or "12")),
            )
        except (TypeError, ValueError):
            rotation_seconds = 12
        return {
            "enabled": (
                (await db.relic_get_setting("enabled")) != "false"
                and (await db.get_setting("trya_dcs_relic_hunt_enabled") or "on") == "on"
            ),
            "updated_at": time.time(),
            "top_hunters": [
                {
                    "username": row.get("username") or "Unknown",
                    "points": int(row.get("points") or 0),
                    "level": int(row.get("level") or 1),
                }
                for row in leaderboard
            ],
            "recent_finds": [
                {
                    "username": row.get("username") or "Unknown",
                    "item_name": row.get("item_name") or "Unknown relic",
                    "rarity": row.get("rarity") or "common",
                    "created_at": float(row.get("created_at") or 0),
                }
                for row in recent if row.get("result_type") == "found"
            ][:6],
            "recent_combines": [
                {
                    "username": row.get("username") or "Unknown",
                    "activity": row.get("item_name") or "Unknown combination",
                    "rarity": row.get("rarity") or "common",
                    "created_at": float(row.get("created_at") or 0),
                }
                for row in recent if row.get("result_type") == "combine"
            ][:6],
            "recent_activity": [
                {
                    "username": row.get("username") or "Unknown",
                    "message": row.get("message") or row.get("item_name") or "Relic Hunt activity",
                    "type": row.get("result_type") or "activity",
                    "rarity": row.get("rarity") or "",
                    "points": int(row.get("points_awarded") or 0),
                    "xp": int(row.get("xp_awarded") or 0),
                    "created_at": float(row.get("created_at") or 0),
                }
                for row in recent
            ][:10],
            "commands": [
                f"{prefix}{command}" for command in (
                    "raven", "nest", "items", "top", "rank", "daily",
                    "ritual", "combine", "village", "phrase", "solve", "relichelp",
                )
            ],
            "ritual": {
                "energy": int(ritual.get("energy") or 0),
                "goal": max(1, int(ritual.get("goal") or 1)),
            },
            "phrase": {
                "enabled": bool(phrase.get("enabled")),
                "progress": phrase_display,
            },
            "events": [
                {
                    "id": active.get("event_id"),
                    "name": event_defs.get(active.get("event_id"), {}).get("name")
                    or active.get("event_id") or "Event",
                    "ends_at": float(active.get("ends_at") or 0),
                }
                for active in active_events
            ],
            "village": [
                {
                    "name": area.get("name") or area.get("area_id"),
                    "level": int(area.get("level") or 0),
                    "progress": int(area.get("progress") or 0),
                    "max_level": int(area.get("max_level") or 5),
                }
                for area in village
            ],
            "display": {
                "top_hunters": (await db.get_setting("trya_dcs_info_top_hunters") or "on") == "on",
                "commands": (await db.get_setting("trya_dcs_info_commands") or "on") == "on",
                "recent_finds": (await db.get_setting("trya_dcs_info_recent_finds") or "on") == "on",
                "recent_combines": (await db.get_setting("trya_dcs_info_recent_combines") or "on") == "on",
                "ritual": (await db.get_setting("trya_dcs_info_ritual") or "on") == "on",
                "phrase": (await db.get_setting("trya_dcs_info_phrase") or "on") == "on",
                "custom_enabled": (await db.get_setting("trya_dcs_info_custom_enabled") or "off") == "on",
                "custom_title": await db.get_setting("trya_dcs_info_custom_title") or "Community information",
                "custom_text": await db.get_setting("trya_dcs_info_custom_text") or "",
                "rotation_seconds": rotation_seconds,
            },
        }

    @app.route("/trya-dcs/api/relic-status")
    async def trya_dcs_relic_status():
        user_id = session.get("trya_dcs_discord_user_id")
        guild_id = int(await db.get_setting("trya_dcs_guild_id") or Config.GUILD_ID or 0)
        if not user_id or not await _trya_dcs_membership_valid(int(user_id), guild_id):
            return {"error": "forbidden"}, 403
        return await _trya_dcs_relic_payload()

    @app.route("/trya-dcs/api/status")
    async def trya_dcs_public_status():
        user_id = session.get("trya_dcs_discord_user_id")
        guild_id = int(await db.get_setting("trya_dcs_guild_id") or Config.GUILD_ID or 0)
        if not user_id or not await _trya_dcs_membership_valid(int(user_id), guild_id):
            return {"error": "forbidden"}, 403
        now = time.monotonic()
        app.trya_dcs_presence[int(user_id)] = now
        app.trya_dcs_presence = {
            member_id: seen_at
            for member_id, seen_at in app.trya_dcs_presence.items()
            if now - seen_at < 15
        }
        status = await trya_dcs_manager.get_status()
        return {
            "running": status["running"],
            "safe_stop_pending": status["safe_stop_pending"],
            "song": status["song"],
            "song_index": status["song_index"],
            "playlist_length": status["playlist_length"],
            "playlist": status.get("playlist", []),
            "song_remaining_seconds": status.get("song_remaining_seconds", 0),
            "stream_remaining_seconds": status.get("stream_remaining_seconds", 0),
            "listener_count": len(app.trya_dcs_presence),
            "ffmpeg": status["ffmpeg"],
        }

    @app.route("/trya-dcs/upload/<token>", methods=["GET", "POST"])
    async def trya_dcs_upload(token: str):
        """One-time upload form issued by a Discord DCS slash command."""
        import hashlib
        import tempfile

        from bot.trya_dcs_worker import (
            DCS_RIGHTS_DECLARATION,
            DCS_RIGHTS_VERSION,
            ingest_dcs_audio,
            process_dcs_song,
        )

        song = await db.get_trya_dcs_song_by_token(token)
        if not song:
            return await render_template(
                "trya_dcs_upload.html", error="This upload link is invalid or expired."
            ), 404
        if time.time() - float(song.get("submitted_at") or 0) > 86400:
            await db.delete_trya_dcs_song(int(song["id"]), user_id=int(song["user_id"]))
            return await render_template(
                "trya_dcs_upload.html",
                error="This private upload slot expired after 24 hours. Run the Discord command again.",
            ), 410
        authenticated_user = session.get("trya_dcs_discord_user_id")
        if not authenticated_user:
            session["trya_dcs_oauth_next"] = request.path
            return redirect(url_for("trya_dcs_oauth_start"))
        if int(authenticated_user) != int(song["user_id"]):
            return await render_template(
                "trya_dcs_unavailable.html",
                reason="This submission link belongs to a different Discord member.",
            ), 403
        if song.get("uploaded_at"):
            return await render_template(
                "trya_dcs_upload.html", done=True,
                title=song.get("title") or "Your song",
            )
        if await db.get_setting("trya_dcs_enabled") != "on":
            return await render_template("trya_dcs_upload.html", disabled=True), 503

        configured_guild = int(await db.get_setting("trya_dcs_guild_id") or 0)
        if not configured_guild or not await _trya_dcs_membership_valid(
            int(authenticated_user), configured_guild
        ):
            return await render_template(
                "trya_dcs_upload.html",
                error="Your Discord account is no longer a member of the configured server.",
            ), 403
        if request.method == "GET":
            return await render_template(
                "trya_dcs_upload.html", song=song, token=token
            )
        origin = (request.headers.get("Origin") or "").rstrip("/")
        if origin and origin != _public_web_url().rstrip("/"):
            return await render_template(
                "trya_dcs_upload.html",
                song=song,
                token=token,
                error="The upload request origin was rejected.",
            ), 403

        form = await request.form
        suno_url = (form.get("suno_url") or "").strip()
        wlm_url = (form.get("wlm_url") or "").strip()
        hook_value = (form.get("hook_value") or "").strip()
        content_kind = (form.get("content_kind") or "").strip().lower()
        plan_status = (form.get("suno_plan_status") or "").strip().lower()

        async def upload_error(message: str, status: int = 400):
            return await render_template(
                "trya_dcs_upload.html", song=song, token=token,
                form_error=message, submitted_suno_url=suno_url,
                submitted_wlm_url=wlm_url, submitted_hook=hook_value,
                submitted_content_kind=content_kind,
                submitted_plan_status=plan_status,
            ), status

        if not suno_url:
            return await upload_error("Enter the Suno song URL.")
        resolved_uuid = await resolve_suno_uuid(suno_url)
        if not resolved_uuid:
            return await upload_error(
                "Suno could not resolve that song URL. Check the link and try again."
            )
        if wlm_url:
            wlm_match = re.fullmatch(
                r"https://www\.welovemusic\.ai/track/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})/?",
                wlm_url,
            )
            if not wlm_match:
                return await upload_error(
                    "Enter a valid WeLoveMusic track URL or leave the field empty."
                )
            wlm_url = f"https://www.welovemusic.ai/track/{wlm_match.group(1).lower()}"
        if content_kind not in {"original", "cover", "remix"}:
            return await upload_error("Choose Original, Cover or Remix.")
        if plan_status not in {"free", "paid", "unknown"}:
            return await upload_error("Choose the documented Suno plan status.")

        duplicate = next((
            existing for existing in await db.get_trya_dcs_songs(active_only=True)
            if int(existing["id"]) != int(song["id"])
            and str(existing.get("suno_uuid") or "").lower() == resolved_uuid.lower()
        ), None)
        if duplicate:
            return await upload_error(
                "That Suno song is already active in TrYa DCS.", 409
            )

        try:
            maximum = max(1, min(20, int(await db.get_setting("trya_dcs_max_per_user") or "4")))
        except (TypeError, ValueError):
            maximum = 4
        active_count = sum(
            1 for existing in await db.get_trya_dcs_songs_by_user(int(song["user_id"]))
            if int(existing["id"]) != int(song["id"])
        )
        if not song.get("replacement_song_id") and active_count >= maximum:
            return await upload_error(
                f"You already have the maximum of {maximum} active DCS songs.", 409
            )

        attestation_names = (
            "sharing_attested", "official_download_attested",
            "material_rights_attested", "technical_processing_attested",
            "private_playback_attested",
        )
        if any(not form.get(name) for name in attestation_names):
            return await upload_error("Every rights confirmation is required.")

        hook = None
        if hook_value:
            from bot.suno_hook import SunoHookError, resolve_suno_hook
            try:
                hook = await resolve_suno_hook(hook_value)
                if hook["original_clip_id"].lower() != resolved_uuid.lower():
                    raise SunoHookError("This Hook belongs to a different Suno song.")
            except SunoHookError as exc:
                return await upload_error(str(exc))
            except Exception:
                return await upload_error(
                    "Suno could not validate the Hook right now. Try again shortly.", 502
                )

        accepted_at = time.time()
        rights_hash = hashlib.sha256(
            (
                f"{DCS_RIGHTS_VERSION}\n{DCS_RIGHTS_DECLARATION}\n"
                f"user={song['user_id']}\nurl={suno_url}\nuuid={resolved_uuid}\n"
                f"wlm_url={wlm_url}\ncontent_kind={content_kind}\n"
                f"plan={plan_status}\naccepted={accepted_at:.6f}"
            ).encode("utf-8")
        ).hexdigest()
        update = {
            "suno_url": suno_url,
            "suno_uuid": resolved_uuid,
            "wlm_url": wlm_url,
            "content_kind": content_kind,
            "suno_plan_status": plan_status,
            "rights_version": DCS_RIGHTS_VERSION,
            "rights_declaration": DCS_RIGHTS_DECLARATION,
            "rights_hash": rights_hash,
        }
        if hook:
            update.update(
                hook_id=hook["hook_id"],
                hook_share_url=hook["hook_share_url"],
                hook_video_url=hook["hook_video_url"],
            )
        await db.update_trya_dcs_song(song["id"], **update)

        files = await request.files
        uploaded = files.get("original_audio")
        if not uploaded or not uploaded.filename:
            return await upload_error(
                "Select the MP3 or M4A obtained through Suno's official Download action."
            )
        try:
            max_upload_mib = max(
                5, min(100, int(await db.get_setting("trya_dcs_max_upload_mib") or "20"))
            )
        except (TypeError, ValueError):
            max_upload_mib = 20
        try:
            max_duration = max(
                60,
                min(1200, int(await db.get_setting("trya_dcs_max_duration_seconds") or "360")),
            )
        except (TypeError, ValueError):
            max_duration = 360
        incoming_dir = os.path.join(TRYA_DCS_DIR, "incoming")
        os.makedirs(incoming_dir, exist_ok=True)
        suffix = os.path.splitext(uploaded.filename)[1].lower()
        if suffix not in {".mp3", ".m4a"}:
            return await upload_error("Only .mp3 and .m4a audio uploads are accepted.")
        fd, staged_path = tempfile.mkstemp(
            prefix=f"dcs_{song['id']}_", suffix=suffix, dir=incoming_dir
        )
        os.close(fd)
        try:
            await uploaded.save(staged_path)
            finalized = await ingest_dcs_audio(
                db, song["id"], staged_path, TRYA_DCS_DIR,
                original_filename=uploaded.filename,
                max_upload_bytes=max_upload_mib * 1024 * 1024,
                max_duration_seconds=max_duration,
                content_kind=content_kind,
                suno_plan_status=plan_status,
                rights_hash=rights_hash,
                accepted_at=accepted_at,
            )
        except Exception as exc:
            return await upload_error(str(exc))
        finally:
            try:
                os.remove(staged_path)
            except OSError:
                pass

        asyncio.create_task(process_dcs_song(
            db, song["id"], TRYA_DCS_DIR,
            max_duration_seconds=max_duration,
        ))
        return await render_template(
            "trya_dcs_upload.html", done=True,
            title=finalized.get("title") or "Your song",
        )

    @app.route("/trya-dcs/consent-csv")
    @permission_required("trya_dcs")
    async def trya_dcs_consent_csv():
        import csv
        import io

        rows = await db.get_trya_dcs_consent_csv_rows()
        columns = list(rows[0].keys()) if rows else [
            "id", "user_id", "user_name", "suno_url", "suno_uuid", "wlm_url", "title",
            "artist", "content_kind", "suno_plan_status", "rights_version",
            "rights_declaration", "rights_hash", "rights_accepted_at",
            "original_filename", "original_mime", "original_size",
            "original_sha256", "duration", "submitted_at", "uploaded_at",
            "active", "removed_at", "remove_reason",
        ]
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
        return await make_response(
            output.getvalue(),
            200,
            {
                "Content-Type": "text/csv; charset=utf-8",
                "Content-Disposition": "attachment; filename=trya_dcs_consent.csv",
            },
        )

    def _trya_dcs_callback_url() -> str:
        return f"{_public_web_url()}/trya-dcs/oauth/callback"

    async def _trya_dcs_membership_valid(user_id: int, guild_id: int) -> bool:
        guild = get_guild()
        if not guild or guild.id != guild_id:
            return False
        member = guild.get_member(user_id)
        if member is not None:
            return True
        try:
            await guild.fetch_member(user_id)
            return True
        except Exception:
            return False

    @app.route("/trya-dcs/oauth/start")
    async def trya_dcs_oauth_start():
        if (await db.get_setting("trya_dcs_enabled") or "off") != "on":
            return await render_template("trya_dcs_unavailable.html", reason="The community stream is currently disabled."), 503
        if not Config.DISCORD_CLIENT_ID or not Config.DISCORD_CLIENT_SECRET:
            return await render_template("trya_dcs_unavailable.html", reason="Discord OAuth is not configured."), 503
        state = secrets.token_urlsafe(32)
        session["trya_dcs_oauth_state"] = state
        params = {
            "client_id": Config.DISCORD_CLIENT_ID,
            "redirect_uri": _trya_dcs_callback_url(),
            "response_type": "code",
            "scope": "identify guilds",
            "state": state,
            "prompt": "none" if session.get("trya_dcs_discord_user_id") else "consent",
        }
        return redirect(f"https://discord.com/oauth2/authorize?{urlencode(params)}")

    @app.route("/trya-dcs/oauth/callback")
    async def trya_dcs_oauth_callback():
        state = request.args.get("state", "")
        expected_state = session.pop("trya_dcs_oauth_state", None)
        if not expected_state or not hmac.compare_digest(state, expected_state):
            return await render_template("trya_dcs_unavailable.html", reason="The Discord login state was invalid. Please try again."), 400
        code = request.args.get("code", "")
        if not code:
            return await render_template("trya_dcs_unavailable.html", reason="Discord did not return an authorization code."), 400

        token_payload = {
            "client_id": Config.DISCORD_CLIENT_ID,
            "client_secret": Config.DISCORD_CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": _trya_dcs_callback_url(),
        }
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            async with aiohttp.ClientSession(timeout=timeout) as client:
                async with client.post(
                    "https://discord.com/api/v10/oauth2/token", data=token_payload
                ) as response:
                    token_data = await response.json(content_type=None)
                    if response.status != 200 or not token_data.get("access_token"):
                        raise RuntimeError(f"token exchange returned HTTP {response.status}")
                headers = {"Authorization": f"Bearer {token_data['access_token']}"}
                async with client.get("https://discord.com/api/v10/users/@me", headers=headers) as response:
                    identity = await response.json(content_type=None)
                    if response.status != 200:
                        raise RuntimeError("Discord identity lookup failed")
                async with client.get("https://discord.com/api/v10/users/@me/guilds", headers=headers) as response:
                    guilds = await response.json(content_type=None)
                    if response.status != 200 or not isinstance(guilds, list):
                        raise RuntimeError("Discord guild membership lookup failed")
        except Exception as exc:
            print(f"[trya-dcs-oauth] Login failed: {exc}", flush=True)
            return await render_template("trya_dcs_unavailable.html", reason="Discord login failed. Please try again."), 502

        try:
            user_id = int(identity["id"])
            guild_id = int(await db.get_setting("trya_dcs_guild_id") or Config.GUILD_ID)
        except (KeyError, TypeError, ValueError):
            return await render_template("trya_dcs_unavailable.html", reason="The configured Discord guild is invalid."), 503
        if not any(str(item.get("id")) == str(guild_id) for item in guilds):
            await db.revoke_trya_dcs_user_tokens(user_id)
            return await render_template("trya_dcs_unavailable.html", reason="This private stream is available only to members of the configured Discord server."), 403
        if not await _trya_dcs_membership_valid(user_id, guild_id):
            return await render_template("trya_dcs_unavailable.html", reason="Your current server membership could not be verified."), 403

        avatar_hash = identity.get("avatar")
        session["trya_dcs_discord_user_id"] = user_id
        session["trya_dcs_discord_name"] = identity.get("global_name") or identity.get("username") or "Discord member"
        session["trya_dcs_discord_avatar"] = (
            f"https://cdn.discordapp.com/avatars/{user_id}/{avatar_hash}.png?size=128"
            if avatar_hash else ""
        )
        session["trya_dcs_guild_id"] = guild_id
        session["trya_dcs_membership_checked_at"] = time.time()
        next_path = session.pop("trya_dcs_oauth_next", "")
        if not re.fullmatch(r"/trya-dcs/upload/[A-Za-z0-9_-]{20,100}", next_path):
            next_path = url_for("trya_dcs_player")
        return redirect(next_path)

    @app.route("/trya-dcs/logout", methods=["POST"])
    async def trya_dcs_logout():
        form = await request.form
        expected_csrf = str(session.get("trya_dcs_player_csrf") or "")
        submitted_csrf = str(form.get("csrf_token") or "")
        if not expected_csrf or not hmac.compare_digest(
            submitted_csrf, expected_csrf
        ):
            return await render_template(
                "trya_dcs_unavailable.html",
                reason="The sign-out request expired. Please reload the player.",
            ), 403
        user_id = session.get("trya_dcs_discord_user_id")
        if user_id:
            await db.revoke_trya_dcs_user_tokens(int(user_id))
        for key in list(session.keys()):
            if key.startswith("trya_dcs_"):
                session.pop(key, None)
        response = redirect(url_for("trya_dcs_player"))
        response.delete_cookie("trya_dcs_stream_token", path="/dcs-stream/")
        return response

    @app.route("/trya-dcs/offline-image")
    async def trya_dcs_offline_image():
        from quart import send_file
        user_id = session.get("trya_dcs_discord_user_id")
        guild_id = int(await db.get_setting("trya_dcs_guild_id") or Config.GUILD_ID or 0)
        if not user_id or not await _trya_dcs_membership_valid(int(user_id), guild_id):
            return "", 403
        filename = os.path.basename(
            await db.get_setting("trya_dcs_offline_image_filename") or ""
        )
        path = os.path.join(TRYA_DCS_DIR, "assets", filename) if filename else ""
        if not path or not os.path.isfile(path):
            return "", 404
        response = await send_file(path, conditional=True)
        response.headers["Cache-Control"] = "private, max-age=300"
        return response

    @app.route("/trya-dcs/player")
    async def trya_dcs_player():
        if (await db.get_setting("trya_dcs_enabled") or "off") != "on":
            return await render_template("trya_dcs_unavailable.html", reason="The community stream is currently disabled."), 503
        user_id = session.get("trya_dcs_discord_user_id")
        guild_id = int(await db.get_setting("trya_dcs_guild_id") or Config.GUILD_ID or 0)
        if not user_id:
            return redirect(url_for("trya_dcs_oauth_start"))
        last_check = float(session.get("trya_dcs_membership_checked_at") or 0)
        recheck = int(await db.get_setting("trya_dcs_membership_recheck_seconds") or "300")
        if time.time() - last_check >= recheck:
            if not await _trya_dcs_membership_valid(int(user_id), guild_id):
                await db.revoke_trya_dcs_user_tokens(int(user_id))
                return await render_template("trya_dcs_unavailable.html", reason="Discord server membership is required."), 403
            session["trya_dcs_membership_checked_at"] = time.time()
        player_csrf = secrets.token_urlsafe(32)
        session["trya_dcs_player_csrf"] = player_csrf
        offline_filename = os.path.basename(
            await db.get_setting("trya_dcs_offline_image_filename") or ""
        )
        offline_image_url = (
            url_for("trya_dcs_offline_image")
            if offline_filename and os.path.isfile(
                os.path.join(TRYA_DCS_DIR, "assets", offline_filename)
            ) else ""
        )
        return await render_template(
            "trya_dcs_player.html",
            discord_name=session.get("trya_dcs_discord_name"),
            discord_avatar=session.get("trya_dcs_discord_avatar"),
            disclaimer=await db.get_setting("trya_dcs_disclaimer") or "AI-generated audio and visuals.",
            player_csrf=player_csrf,
            offline_image_url=offline_image_url,
        )

    @app.route("/trya-dcs/api/events")
    async def trya_dcs_event_stream():
        from quart import Response
        from bot.trya_dcs_events import trya_dcs_events

        if (await db.get_setting("trya_dcs_enabled") or "off") != "on":
            return {"error": "disabled"}, 503
        origin = (request.headers.get("Origin") or "").rstrip("/")
        if origin and origin != _public_web_url().rstrip("/"):
            return {"error": "invalid_origin"}, 403
        user_id = session.get("trya_dcs_discord_user_id")
        try:
            guild_id = int(await db.get_setting("trya_dcs_guild_id") or Config.GUILD_ID or 0)
        except (TypeError, ValueError):
            return {"error": "invalid_configuration"}, 503
        if not user_id or not await _trya_dcs_membership_valid(int(user_id), guild_id):
            return {"error": "forbidden"}, 403

        async def events():
            async with trya_dcs_events.subscribe() as queue:
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=30)
                    except asyncio.TimeoutError:
                        if not await _trya_dcs_membership_valid(int(user_id), guild_id):
                            return
                        yield ": heartbeat\n\n"
                        continue
                    yield f"data: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n"

        return Response(
            events(),
            content_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    @app.websocket("/trya-dcs/ws")
    async def trya_dcs_websocket():
        """Authenticated DCS state and Discord-backed chat transport."""
        from bot.cogs.trya_dcs_chat import (
            guild_emoji_payload,
            send_web_chat_message,
            serialize_discord_message,
        )
        from bot.trya_dcs_events import trya_dcs_events

        async def reject(reason: str) -> None:
            print(f"[trya-dcs-ws] Rejected: {reason}", flush=True)
            await websocket.close(4403)

        if (await db.get_setting("trya_dcs_enabled") or "off") != "on":
            await reject("DCS is disabled")
            return
        origin = (websocket.headers.get("Origin") or "").rstrip("/")
        if origin and origin != _public_web_url().rstrip("/"):
            await reject(f"unexpected origin {origin!r}")
            return
        user_id = session.get("trya_dcs_discord_user_id")
        try:
            guild_id = int(await db.get_setting("trya_dcs_guild_id") or Config.GUILD_ID or 0)
            channel_id = int(await db.get_setting("trya_dcs_chat_channel_id") or 0)
        except (TypeError, ValueError):
            await reject("invalid guild or channel configuration")
            return
        if not user_id:
            await reject("Discord session is missing")
            return
        if not await _trya_dcs_membership_valid(int(user_id), guild_id):
            await reject("Discord membership validation failed")
            return

        guild = get_guild()
        channel = bot.get_channel(channel_id) if bot and channel_id else None
        if bot and channel_id and channel is None:
            try:
                channel = await bot.fetch_channel(channel_id)
            except Exception:
                channel = None

        print(
            f"[trya-dcs-ws] Connected user={user_id} channel={channel_id} available={bool(channel)}",
            flush=True,
        )
        await websocket.send(json.dumps({
            "version": 1,
            "type": "session.ready",
            "timestamp": time.time(),
            "data": {
                "user_id": str(user_id),
                "chat_enabled": bool(channel_id and channel),
                "emojis": await guild_emoji_payload(guild),
                "messages": [],
            },
        }))

        if channel is not None and hasattr(channel, "history"):
            try:
                history = [message async for message in channel.history(limit=50, oldest_first=True)]
                for message in history:
                    await websocket.send(json.dumps({
                        "version": 1,
                        "type": "chat.message",
                        "timestamp": time.time(),
                        "data": await serialize_discord_message(message),
                    }))
                print(f"[trya-dcs-ws] Sent {len(history)} history message(s).", flush=True)
            except Exception as exc:
                print(f"[trya-dcs-chat] History load failed: {exc}", flush=True)

        async with trya_dcs_events.subscribe() as event_queue:
            async def push_events():
                while True:
                    event = await event_queue.get()
                    await websocket.send(json.dumps(event))

            push_task = asyncio.create_task(push_events())

            async def membership_guard():
                interval = max(
                    60,
                    min(
                        900,
                        int(
                            await db.get_setting(
                                "trya_dcs_membership_recheck_seconds"
                            )
                            or "300"
                        ),
                    ),
                )
                while True:
                    await asyncio.sleep(interval)
                    if not await _trya_dcs_membership_valid(int(user_id), guild_id):
                        await db.revoke_trya_dcs_user_tokens(int(user_id))
                        await websocket.close(4403)
                        return

            membership_task = asyncio.create_task(membership_guard())
            try:
                while True:
                    raw = await websocket.receive()
                    try:
                        incoming = json.loads(raw)
                    except (TypeError, json.JSONDecodeError):
                        continue
                    event_type = incoming.get("type")
                    if event_type == "ping":
                        await websocket.send(json.dumps({"version": 1, "type": "pong", "timestamp": time.time(), "data": {}}))
                        continue
                    if event_type != "chat.send" or not channel_id or bot is None:
                        continue

                    content = str((incoming.get("data") or {}).get("content") or "").strip()
                    if not content:
                        continue
                    now = time.monotonic()
                    key = int(user_id)
                    recent = [stamp for stamp in app.trya_dcs_chat_rate.get(key, []) if now - stamp < 10]
                    if len(recent) >= 5:
                        await websocket.send(json.dumps({
                            "version": 1, "type": "chat.error", "timestamp": time.time(),
                            "data": {"message": "Please wait before sending another message."},
                        }))
                        app.trya_dcs_chat_rate[key] = recent
                        continue
                    if not await _trya_dcs_membership_valid(int(user_id), guild_id):
                        await websocket.close(4403)
                        return
                    recent.append(now)
                    app.trya_dcs_chat_rate[key] = recent
                    try:
                        await send_web_chat_message(bot, channel_id, int(user_id), content)
                        await websocket.send(json.dumps({
                            "version": 1, "type": "chat.sent", "timestamp": time.time(), "data": {},
                        }))
                    except Exception as exc:
                        print(f"[trya-dcs-chat] Web message failed: {exc}", flush=True)
                        await websocket.send(json.dumps({
                            "version": 1, "type": "chat.error", "timestamp": time.time(),
                            "data": {"message": "The message could not be delivered to Discord."},
                        }))
            finally:
                push_task.cancel()
                membership_task.cancel()
                try:
                    await push_task
                except asyncio.CancelledError:
                    pass
                try:
                    await membership_task
                except asyncio.CancelledError:
                    pass
                print(f"[trya-dcs-ws] Disconnected user={user_id}", flush=True)

    @app.route("/trya-dcs/api/chat", methods=["GET", "POST"])
    async def trya_dcs_chat_fallback():
        from bot.cogs.trya_dcs_chat import (
            guild_emoji_payload,
            send_web_chat_message,
            serialize_discord_message,
        )

        if (await db.get_setting("trya_dcs_enabled") or "off") != "on":
            return {"error": "disabled"}, 503
        user_id = session.get("trya_dcs_discord_user_id")
        try:
            guild_id = int(await db.get_setting("trya_dcs_guild_id") or Config.GUILD_ID or 0)
            channel_id = int(await db.get_setting("trya_dcs_chat_channel_id") or 0)
        except (TypeError, ValueError):
            return {"error": "invalid_configuration"}, 503
        if not user_id or not await _trya_dcs_membership_valid(int(user_id), guild_id):
            return {"error": "forbidden"}, 403
        channel = bot.get_channel(channel_id) if bot and channel_id else None
        if bot and channel_id and channel is None:
            try:
                channel = await bot.fetch_channel(channel_id)
            except Exception:
                channel = None
        if request.method == "POST":
            csrf = str(request.headers.get("X-CSRF-Token") or "")
            expected = str(session.get("trya_dcs_player_csrf") or "")
            if not expected or not hmac.compare_digest(csrf, expected):
                return {"error": "invalid_csrf"}, 403
            payload = await request.get_json(silent=True) or {}
            content = str(payload.get("content") or "").strip()[:1800]
            if not content or not channel_id or bot is None:
                return {"error": "invalid_message"}, 400
            now = time.monotonic()
            key = int(user_id)
            recent = [stamp for stamp in app.trya_dcs_chat_rate.get(key, []) if now - stamp < 10]
            if len(recent) >= 5:
                app.trya_dcs_chat_rate[key] = recent
                return {"error": "rate_limited"}, 429
            recent.append(now)
            app.trya_dcs_chat_rate[key] = recent
            try:
                await send_web_chat_message(bot, channel_id, int(user_id), content)
            except Exception as exc:
                print(f"[trya-dcs-chat] Fallback message failed: {exc}", flush=True)
                return {"error": "delivery_failed"}, 502
            return {"ok": True}

        messages = []
        if channel is not None and hasattr(channel, "history"):
            try:
                after_id = int(request.args.get("after") or 0)
            except (TypeError, ValueError):
                after_id = 0
            try:
                if after_id:
                    import discord
                    history = [
                        message async for message in channel.history(
                            limit=50, after=discord.Object(id=after_id), oldest_first=True
                        )
                    ]
                else:
                    history = [message async for message in channel.history(limit=50)]
                    history.reverse()
                messages = [await serialize_discord_message(message) for message in history]
            except Exception as exc:
                print(f"[trya-dcs-chat] Fallback history failed: {exc}", flush=True)
        return {
            "chat_enabled": bool(channel_id and channel),
            "emojis": await guild_emoji_payload(get_guild()),
            "messages": messages,
        }

    @app.route("/trya-dcs/api/stream-token", methods=["POST"])
    async def trya_dcs_stream_token():
        if (await db.get_setting("trya_dcs_enabled") or "off") != "on":
            return {"error": "disabled"}, 503
        origin = (request.headers.get("Origin") or "").rstrip("/")
        if origin and origin != _public_web_url().rstrip("/"):
            return {"error": "invalid_origin"}, 403
        user_id = session.get("trya_dcs_discord_user_id")
        guild_id = int(await db.get_setting("trya_dcs_guild_id") or Config.GUILD_ID or 0)
        if not user_id or not await _trya_dcs_membership_valid(int(user_id), guild_id):
            return {"error": "forbidden"}, 403
        now_mono = time.monotonic()
        token_key = int(user_id)
        recent = [
            stamp
            for stamp in app.trya_dcs_token_rate.get(token_key, [])
            if now_mono - stamp < 60
        ]
        if len(recent) >= 12:
            app.trya_dcs_token_rate[token_key] = recent
            return {"error": "rate_limited"}, 429
        recent.append(now_mono)
        app.trya_dcs_token_rate[token_key] = recent
        await db.purge_expired_trya_dcs_tokens()
        raw_token = secrets.token_urlsafe(48)
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        now = time.time()
        ttl = max(300, min(900, int(await db.get_setting("trya_dcs_stream_token_ttl_seconds") or "600")))
        forwarded_for = request.headers.get("X-Forwarded-For", request.remote_addr or "")
        fingerprint = hashlib.sha256(
            f"{forwarded_for.split(',')[0].strip()}|{request.headers.get('User-Agent', '')}".encode("utf-8")
        ).hexdigest()
        await db.issue_trya_dcs_stream_token(
            token_hash=token_hash,
            discord_user_id=int(user_id),
            discord_guild_id=guild_id,
            issued_at=now,
            expires_at=now + ttl,
            remote_fingerprint=fingerprint,
        )
        response = await make_response(
            {"hls_url": "/dcs-stream/trya-dcs/index.m3u8", "expires_in": ttl}
        )
        response.set_cookie(
            "trya_dcs_stream_token",
            raw_token,
            max_age=ttl,
            secure=True,
            httponly=True,
            samesite="Lax",
            path="/dcs-stream/",
        )
        return response

    @app.route("/trya-dcs/internal/media-auth", methods=["GET", "HEAD"])
    async def trya_dcs_media_auth():
        raw_token = request.cookies.get("trya_dcs_stream_token", "")
        if not raw_token or (await db.get_setting("trya_dcs_enabled") or "off") != "on":
            return "", 401
        token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
        token = await db.get_trya_dcs_stream_token(token_hash)
        if not token:
            return "", 401
        forwarded_for = request.headers.get("X-Forwarded-For", request.remote_addr or "")
        fingerprint = hashlib.sha256(
            f"{forwarded_for.split(',')[0].strip()}|{request.headers.get('User-Agent', '')}".encode("utf-8")
        ).hexdigest()
        if token.get("remote_fingerprint") and not hmac.compare_digest(
            token["remote_fingerprint"], fingerprint
        ):
            return "", 401
        recheck = max(60, min(900, int(await db.get_setting("trya_dcs_membership_recheck_seconds") or "300")))
        if time.time() - float(token["last_membership_check_at"]) >= recheck:
            valid = await _trya_dcs_membership_valid(
                int(token["discord_user_id"]), int(token["discord_guild_id"])
            )
            if not valid:
                await db.revoke_trya_dcs_user_tokens(int(token["discord_user_id"]))
                return "", 403
            await db.touch_trya_dcs_token_membership(int(token["id"]))
        return "", 204

    @app.route("/trya-stream", methods=["GET", "POST"])
    @permission_required('trya_stream')
    async def trya_stream_admin():
        import csv, io, uuid as _uuid
        from datetime import datetime, timezone
        from quart import jsonify, send_file

        if request.method == "POST":
            form = await request.form
            files = await request.files
            action = form.get("action", "")

            if action == "delete_song":
                song_id = int(form.get("song_id", 0))
                removed = await db.delete_trya_stream_song(song_id)
                if removed:
                    protected_songs = await db.get_all_trya_stream_songs(active_only=True)
                    cleanup_trya_stream_song_files(
                        TRYA_STREAM_DIR, removed, protected_songs
                    )
                    await flash(
                        "Song removed from the active playlist; original, working copy and consent evidence retained.",
                        "success",
                    )
                else:
                    await flash("Song not found.", "error")

            elif action == "set_hook_video":
                import bot.trya_stream_manager as _esm
                from bot.suno_hook import SunoHookError, resolve_suno_hook

                song_id = int(form.get("song_id", 0) or 0)
                hook_value = (form.get("hook_value") or "").strip()
                song = await db.get_trya_stream_song(song_id) if song_id else None
                if _esm.stream_is_live:
                    await flash("Stop the stream before changing a Hook video.", "error")
                elif not song or song.get("playlist_source") not in ("submission", "admin"):
                    await flash("Song not found in the submission or admin playlist.", "error")
                else:
                    try:
                        hook = await resolve_suno_hook(hook_value)
                        if hook["original_clip_id"].lower() != str(song.get("suno_uuid") or "").lower():
                            raise SunoHookError(
                                "This Hook belongs to a different Suno song."
                            )
                        candidate = dict(song)
                        candidate.update(hook)
                        cached_path = await trya_stream_manager._get_video(
                            candidate, allow_hook_fallback=False
                        )
                        if not cached_path or not os.path.exists(cached_path):
                            raise SunoHookError("The Hook video could not be downloaded.")
                        old_hook_id = (song.get("hook_id") or "").strip()
                        await db.update_trya_stream_song(
                            song_id,
                            hook_id=hook["hook_id"],
                            hook_share_url=hook["hook_share_url"],
                            hook_video_url=hook["hook_video_url"],
                        )
                        if old_hook_id and old_hook_id != hook["hook_id"]:
                            old_path = trya_stream_hook_cache_path(
                                TRYA_STREAM_DIR, song_id, old_hook_id
                            )
                            try:
                                os.remove(old_path)
                            except FileNotFoundError:
                                pass
                        await flash(
                            f"Hook video set for “{song.get('title') or song_id}”.",
                            "success",
                        )
                    except SunoHookError as exc:
                        await flash(str(exc), "error")
                    except Exception as exc:
                        print(
                            f"[trya-stream] Hook setup failed for song #{song_id}: {exc}",
                            flush=True,
                        )
                        await flash("Hook setup failed. Check the server log.", "error")

            elif action == "remove_hook_video":
                import bot.trya_stream_manager as _esm

                song_id = int(form.get("song_id", 0) or 0)
                song = await db.get_trya_stream_song(song_id) if song_id else None
                if _esm.stream_is_live:
                    await flash("Stop the stream before removing a Hook video.", "error")
                elif not song:
                    await flash("Song not found.", "error")
                else:
                    await db.update_trya_stream_song(
                        song_id,
                        hook_id=None,
                        hook_share_url=None,
                        hook_video_url=None,
                    )
                    cleanup_trya_stream_hook_files(TRYA_STREAM_DIR, song)
                    await flash(
                        f"Hook video removed from “{song.get('title') or song_id}”.",
                        "success",
                    )

            elif action == "upload_local_song":
                import hashlib
                import tempfile
                from bot.trya_stream_worker import (
                    TRYA_RIGHTS_DECLARATION,
                    TRYA_RIGHTS_VERSION,
                    ingest_uploaded_audio,
                    process_exp_song,
                )

                suno_url = (form.get("suno_url") or "").strip()
                playlist_source = (form.get("playlist_source") or "submission").strip()
                uploaded = files.get("original_audio")
                required_attestations = (
                    "official_download_attested", "paid_download_attested",
                    "not_suno_remix_attested", "third_party_rights_attested",
                    "commercial_rights_attested",
                )
                if playlist_source not in {"submission", "admin", "intro", "outro"}:
                    await flash("Invalid playlist destination.", "error")
                elif not suno_url or not uploaded or not uploaded.filename:
                    await flash("A Suno URL and official MP3/M4A download are required.", "error")
                elif any(not form.get(name) for name in required_attestations):
                    await flash("Every rights confirmation is required.", "error")
                else:
                    suno_uuid = await resolve_suno_uuid(suno_url)
                    if not suno_uuid:
                        await flash("Could not resolve a valid Suno song URL.", "error")
                    else:
                        user_ref = (form.get("submission_user_ref") or "").strip()
                        submitter_id = int(user_ref) if user_ref.isdigit() else 0
                        submitter_name = user_ref or session.get("username", "admin-ui")
                        rights_hash = hashlib.sha256(
                            (
                                f"{TRYA_RIGHTS_VERSION}\n{TRYA_RIGHTS_DECLARATION}\n"
                                f"user={submitter_id}\nurl={suno_url}\nuuid={suno_uuid}\n"
                                f"destination={playlist_source}"
                            ).encode()
                        ).hexdigest()
                        song_id, _ = await db.add_trya_stream_song(
                            user_id=submitter_id, user_name=submitter_name,
                            suno_url=suno_url, suno_uuid=suno_uuid,
                            rights_declaration=TRYA_RIGHTS_DECLARATION,
                            rights_hash=rights_hash, rights_version=TRYA_RIGHTS_VERSION,
                            playlist_source=playlist_source,
                        )
                        incoming_dir = os.path.join(TRYA_STREAM_DIR, "incoming")
                        os.makedirs(incoming_dir, exist_ok=True)
                        suffix = os.path.splitext(uploaded.filename)[1].lower()
                        fd, staged_path = tempfile.mkstemp(
                            prefix=f"admin_{song_id}_", suffix=suffix, dir=incoming_dir
                        )
                        os.close(fd)
                        try:
                            await uploaded.save(staged_path)
                            await ingest_uploaded_audio(
                                db, song_id, staged_path, TRYA_STREAM_DIR,
                                original_filename=uploaded.filename,
                                rights_version=TRYA_RIGHTS_VERSION,
                                official_download_attested=True,
                                paid_download_attested=True, is_suno_remix=False,
                                third_party_rights_attested=True,
                                commercial_rights_attested=True,
                                rights_accepted_at=time.time(),
                            )
                        except Exception as exc:
                            await db.update_trya_stream_song(
                                song_id, analysis_status="failed",
                                playlist_remove_reason=str(exc)[:500],
                            )
                            await flash(f"Upload rejected: {exc}", "error")
                        else:
                            asyncio.create_task(process_exp_song(
                                db, song_id, TRYA_STREAM_DIR, bot=bot,
                                skip_moderation=playlist_source in {"admin", "intro", "outro"},
                                max_duration=None if playlist_source in {"admin", "intro", "outro"} else 360,
                            ))
                            note = " It is waiting for admin approval." if playlist_source in {"intro", "outro"} else ""
                            await flash(
                                f"Original archived and working copy queued for analysis.{note}",
                                "success",
                            )
                        finally:
                            try:
                                os.remove(staged_path)
                            except OSError:
                                pass

            elif action == "approve_special_song":
                song_id = int(form.get("song_id", 0) or 0)
                approved = await db.approve_trya_stream_song(
                    song_id, session.get("username", "admin")
                )
                if approved:
                    from bot.trya_stream_worker import retry_whisper_year_anomaly_if_needed
                    asyncio.create_task(
                        retry_whisper_year_anomaly_if_needed(db, song_id, TRYA_STREAM_DIR)
                    )
                await flash(
                    "Intro/outro proposal approved." if approved else "Proposal could not be approved.",
                    "success" if approved else "error",
                )

            elif action == "reject_special_song":
                song_id = int(form.get("song_id", 0) or 0)
                rejected = await db.reject_trya_stream_song(
                    song_id, session.get("username", "admin"), reason="admin_rejected"
                )
                await flash(
                    "Proposal rejected; archive and consent evidence retained."
                    if rejected else "Proposal could not be rejected.",
                    "success" if rejected else "error",
                )

            elif action == "delete_all_songs":
                import bot.trya_stream_manager as _esm
                if _esm.stream_is_live:
                    await flash("Cannot clear the playlist while the stream is live.", "error")
                else:
                    removed = await db.get_all_trya_stream_songs(
                        active_only=True, source="submission"
                    )
                    count = await db.delete_all_trya_stream_songs(source="submission")
                    protected_songs = await db.get_all_trya_stream_songs(active_only=True)
                    for song in removed:
                        cleanup_trya_stream_song_files(
                            TRYA_STREAM_DIR, song, protected_songs
                        )
                    await flash(
                        f"Playlist cleared — {count} song(s) removed; archives and consent evidence retained.",
                        "success",
                    )

            elif action == "delete_all_admin_songs":
                import bot.trya_stream_manager as _esm
                if _esm.stream_is_live:
                    await flash("Cannot clear the admin playlist while the stream is live.", "error")
                else:
                    removed = await db.get_all_trya_stream_songs(
                        active_only=True, source="admin"
                    )
                    count = await db.delete_all_trya_stream_songs(source="admin")
                    protected_songs = await db.get_all_trya_stream_songs(active_only=True)
                    for song in removed:
                        cleanup_trya_stream_song_files(
                            TRYA_STREAM_DIR, song, protected_songs
                        )
                    await flash(
                        f"Admin playlist cleared — {count} song(s) removed; archives and consent evidence retained.",
                        "success",
                    )

            elif action == "reanalyze_whisper":
                import bot.trya_stream_manager as _esm
                if _esm.stream_is_live:
                    await flash("Cannot run Whisper while the stream is live.", "error")
                else:
                    from bot.trya_stream_worker import process_exp_song
                    songs_all = await db.get_all_trya_stream_songs(active_only=True)
                    queued = 0
                    for s in songs_all:
                        if not s.get("mp3_filename"):
                            continue  # MP3 never finished uploading — skip
                        # Drop stale ASS so the player won't pick it up mid-rebuild
                        await db.update_trya_stream_song(
                            s["id"], analysis_status="processing", ass_filename=None,
                        )
                        asyncio.create_task(
                            process_exp_song(db, s["id"], TRYA_STREAM_DIR, bot=bot)
                        )
                        queued += 1
                    await flash(
                        f"Queued {queued} song(s) for re-analysis. "
                        "Whisper runs in the background — refresh the page to see status updates.",
                        "success",
                    )

            elif action == "check_durations":
                import bot.trya_stream_manager as _esm
                if _esm.stream_is_live:
                    await flash("Cannot check durations while the stream is live.", "error")
                else:
                    checked, corrected, skipped, errors = await _check_trya_stream_durations()
                    msg = (
                        f"Duration check complete: {checked} checked, "
                        f"{corrected} corrected, {skipped} skipped"
                    )
                    if errors:
                        msg += f", {errors} error(s). Check the Live Log."
                    else:
                        msg += ". Details are in the Live Log."
                    await flash(msg, "error" if errors else "success")

            elif action == "rescrape_metadata_one":
                from bot.trya_stream_worker import scrape_suno
                from bot.trya_stream_manager import log_event
                sid = int(form.get("song_id", "0") or 0)
                s = await db.get_trya_stream_song(sid) if sid else None
                if not s or not s.get("suno_uuid"):
                    await flash("Song not found.", "error")
                else:
                    uuid = s["suno_uuid"]
                    log_event(f"Refreshing metadata for #{sid} (uuid={uuid})…", prefix="[meta]")
                    meta = await scrape_suno(uuid)
                    fields = {}
                    if meta.get("title"):     fields["title"]     = meta["title"]
                    if meta.get("artist"):    fields["artist"]    = meta["artist"]
                    if meta.get("video_url"): fields["video_url"] = meta["video_url"]
                    if meta.get("cover_url"): fields["cover_url"] = meta["cover_url"]
                    if fields:
                        await db.update_trya_stream_song(sid, **fields)
                        for ext in (".jpg", ".mp4"):
                            cached = os.path.join(TRYA_STREAM_DIR, "cover_cache", f"{uuid}{ext}")
                            if os.path.exists(cached):
                                try: os.remove(cached)
                                except Exception: pass
                        log_event(
                            f"Refreshed metadata for #{sid} ({fields.get('title') or s.get('title')!r}) "
                            f"— updated: {', '.join(sorted(fields.keys()))}",
                            prefix="[meta]",
                        )
                        await flash(f"Refreshed metadata for “{fields.get('title') or s.get('title')}”.", "success")
                    else:
                        log_event(
                            f"No usable metadata returned for #{sid} (uuid={uuid})",
                            level="error", prefix="[meta]",
                        )
                        await flash("Nothing to update — Suno returned no usable metadata.", "warning")

            elif action == "rebuild_square_media":
                sid = int(form.get("song_id", "0") or 0)
                song = await db.get_trya_stream_song(sid) if sid else None
                if not song:
                    await flash("Song not found.", "error")
                elif trya_stream_manager.is_running:
                    await flash("Stop TrYa Stream before rebuilding square media.", "error")
                else:
                    async def _rebuild_square(target=dict(song)):
                        await trya_stream_manager.prepare_song_square_media(target, force=True)
                    asyncio.create_task(_rebuild_square())
                    await flash(
                        f"Square media rebuild queued for “{song.get('title') or sid}”. Watch the Live Log for progress.",
                        "success",
                    )

            elif action == "renormalize_cover_one":
                sid = int(form.get("song_id", "0") or 0)
                s = await db.get_trya_stream_song(sid) if sid else None
                if not s:
                    await flash("Song not found.", "error")
                else:
                    ok, msg = await trya_stream_manager.renormalize_cover(s)
                    await flash(
                        f"{'✅' if ok else '❌'} {s.get('title') or sid}: {msg}",
                        "success" if ok else "error",
                    )

            elif action == "renormalize_cover_all":
                from bot.trya_stream_manager import log_event
                songs_all = await db.get_all_trya_stream_songs(active_only=True)
                log_event(
                    f"Bulk cover normalize started for {len(songs_all)} song(s)\u2026",
                    prefix="[cover]",
                )
                # Run in the background so the HTTP request returns immediately
                # (each individual normalization spawns ffprobe + ffmpeg and
                # would otherwise block the request for tens of seconds).
                async def _bulk_renorm(songs):
                    ok_n = fail_n = skip_n = 0
                    for s in songs:
                        title = s.get("title") or s.get("id")
                        try:
                            ok, msg = await trya_stream_manager.renormalize_cover(s)
                        except Exception as e:
                            log_event(
                                f"  #{s.get('id')} ({title!r}) error: {e}",
                                level="error", prefix="[cover]",
                            )
                            fail_n += 1
                            continue
                        if ok:
                            log_event(
                                f"  #{s.get('id')} ({title!r}): {msg}",
                                prefix="[cover]",
                            )
                            ok_n += 1
                        else:
                            # 'No usable cover URL' / 'failed' — distinguish
                            level = "error" if "fail" in msg.lower() else "info"
                            log_event(
                                f"  #{s.get('id')} ({title!r}): {msg}",
                                level=level, prefix="[cover]",
                            )
                            if level == "error":
                                fail_n += 1
                            else:
                                skip_n += 1
                    log_event(
                        f"Bulk cover normalize done: {ok_n} ok, {fail_n} failed, "
                        f"{skip_n} skipped (no cached cover).",
                        prefix="[cover]",
                    )
                asyncio.create_task(_bulk_renorm(songs_all))
                await flash(
                    f"Queued cover normalization for {len(songs_all)} song(s) \u2014 "
                    "watch the Live Log for progress.",
                    "success",
                )

            elif action == "approve_moderation_one":
                sid = int(form.get("song_id", "0") or 0)
                s = await db.get_trya_stream_song(sid) if sid else None
                if not s:
                    await flash("Song not found.", "error")
                else:
                    await db.update_trya_stream_song(
                        sid,
                        moderation_status="approved",
                        moderation_at=time.time(),
                    )
                    from bot.trya_stream_worker import retry_whisper_year_anomaly_if_needed
                    asyncio.create_task(
                        retry_whisper_year_anomaly_if_needed(db, sid, TRYA_STREAM_DIR)
                    )
                    await flash(
                        f"✅ Approved “{s.get('title') or sid}” for the stream playlist.",
                        "success",
                    )

            elif action == "approve_admin_whisper_bypass":
                from bot.trya_stream_manager import log_event
                sid = int(form.get("song_id", "0") or 0)
                s = await db.get_trya_stream_song(sid) if sid else None
                if not s:
                    await flash("Song not found.", "error")
                elif s.get("playlist_source") != "admin":
                    await flash("This bypass is only available for admin playlist songs.", "error")
                elif not s.get("mp3_filename"):
                    await flash("Cannot approve yet: MP3 download is not complete.", "error")
                else:
                    await db.update_trya_stream_song(
                        sid,
                        analysis_status="done",
                        word_timestamps="[]",
                        ass_filename=None,
                        moderation_status="approved",
                        moderation_reason="Admin playlist: Whisper transcript bypassed.",
                        moderation_at=time.time(),
                    )
                    log_event(
                        f"Admin bypassed Whisper transcript for #{sid} "
                        f"({s.get('title') or s.get('suno_url') or sid!r}); marked stream-ready.",
                        prefix="[admin-pl]",
                    )
                    await flash(
                        f"✅ Marked “{s.get('title') or sid}” as ready without Whisper transcript.",
                        "success",
                    )

            elif action == "remoderate_one":
                import bot.trya_stream_manager as _esm
                if _esm.stream_is_live:
                    await flash("Cannot run LLM moderation while the stream is live.", "error")
                else:
                    from bot.exp_moderation import moderate_lyrics
                    from bot.llm import OllamaClient
                    from config import Config
                    sid = int(form.get("song_id", "0") or 0)
                    s = await db.get_trya_stream_song(sid) if sid else None
                    if not s or not s.get("lyrics"):
                        await flash("Song not found or has no lyrics yet.", "error")
                    else:
                        from bot.trya_stream_manager import log_event
                        async def _run_remoderation(song_id, snap):
                            title_s = snap.get("title") or f"#{song_id}"
                            try:
                                client = OllamaClient(
                                    base_url=Config.OLLAMA_URL,
                                    model=Config.LLM_MODEL,
                                    timeout=Config.LLM_REQUEST_TIMEOUT,
                                )
                                log_event(
                                    f"Re-moderation start for #{song_id} ({title_s!r})",
                                    prefix="[mod]",
                                )
                                verdict = await moderate_lyrics(
                                    client,
                                    lyrics=snap.get("lyrics") or "",
                                    title=snap.get("title") or "",
                                    artist=snap.get("artist") or "",
                                )
                                await db.update_trya_stream_song(
                                    song_id,
                                    moderation_status=verdict["status"],
                                    moderation_reason=verdict.get("reason") or "",
                                    moderation_at=time.time(),
                                )
                                level = "error" if verdict["status"] in ("flagged", "pending") else "info"
                                summary = (
                                    f"Re-moderation #{song_id} → {verdict['status']}"
                                    f"{' (translated)' if verdict.get('translated') else ''}"
                                )
                                if verdict.get("reason"):
                                    summary += f": {verdict['reason']}"
                                log_event(summary, level=level, prefix="[mod]")
                            except Exception as e:
                                log_event(
                                    f"Re-moderation error #{song_id}: {e}",
                                    level="error", prefix="[mod]",
                                )
                                await db.update_trya_stream_song(
                                    song_id,
                                    moderation_status="pending",
                                    moderation_reason=f"Re-moderation error: {e!s}",
                                    moderation_at=time.time(),
                                )
                        await db.update_trya_stream_song(
                            sid,
                            moderation_status="pending",
                            moderation_reason="Re-moderation in progress…",
                            moderation_at=time.time(),
                        )
                        asyncio.create_task(_run_remoderation(sid, s))
                        await flash(
                            f"Queued “{s.get('title') or sid}” for LLM re-moderation. "
                            "Refresh the page in a few seconds.",
                            "success",
                        )

            elif action == "reanalyze_whisper_one":
                import bot.trya_stream_manager as _esm
                if _esm.stream_is_live:
                    await flash("Cannot run Whisper while the stream is live.", "error")
                else:
                    from bot.trya_stream_worker import process_exp_song
                    sid = int(form.get("song_id", "0") or 0)
                    s = await db.get_trya_stream_song(sid) if sid else None
                    if not s or not s.get("mp3_filename"):
                        await flash("Song not found or MP3 missing.", "error")
                    else:
                        await db.update_trya_stream_song(
                            sid,
                            analysis_status="processing",
                            ass_filename=None,
                            # A deliberate manual re-analysis starts a fresh
                            # anomaly audit. Automatic pipeline runs remain
                            # limited to one large-model retry per audit.
                            whisper_anomaly_retry_count=0,
                            whisper_anomaly_retry_at=None,
                            whisper_anomaly_retry_trigger=None,
                        )
                        asyncio.create_task(
                            process_exp_song(db, sid, TRYA_STREAM_DIR, bot=bot)
                        )
                        await flash(
                            f"Queued “{s.get('title') or sid}” for Whisper re-analysis. "
                            "Refresh the page for status.",
                            "success",
                        )

            elif action == "rescrape_metadata":
                from bot.trya_stream_worker import scrape_suno
                from bot.trya_stream_manager import log_event
                songs_all = await db.get_all_trya_stream_songs(active_only=True)
                log_event(
                    f"Bulk metadata refresh started for {len(songs_all)} song(s)…",
                    prefix="[meta]",
                )
                updated = 0
                for s in songs_all:
                    uuid = s.get("suno_uuid")
                    if not uuid:
                        continue
                    meta = await scrape_suno(uuid)
                    real_uuid = meta.get("real_uuid") or uuid
                    fields = {}
                    if meta.get("title"):     fields["title"]     = meta["title"]
                    if meta.get("artist"):    fields["artist"]    = meta["artist"]
                    if meta.get("video_url"): fields["video_url"] = meta["video_url"]
                    if meta.get("cover_url"): fields["cover_url"] = meta["cover_url"]
                    if fields:
                        await db.update_trya_stream_song(s["id"], **fields)
                        updated += 1
                        log_event(
                            f"  #{s['id']} ({fields.get('title') or s.get('title')!r}) updated: "
                            f"{', '.join(sorted(fields.keys()))}",
                            prefix="[meta]",
                        )
                        # Invalidate cached media so next stream run downloads fresh
                        for ext in (".jpg", ".mp4"):
                            cached = os.path.join(TRYA_STREAM_DIR, "cover_cache", f"{uuid}{ext}")
                            if os.path.exists(cached):
                                try: os.remove(cached)
                                except Exception: pass
                log_event(f"Bulk metadata refresh done: {updated}/{len(songs_all)} updated.", prefix="[meta]")
                await flash(f"Refreshed metadata for {updated} song(s).", "success")

            elif action == "save_stream_key":
                key = form.get("exp_twitch_key", "").strip()
                await db.set_setting("trya_stream_twitch_key", key)
                await flash("Stream key saved.", "success")

            elif action == "save_trya_alert_settings":
                alert_checkbox_suffixes = (
                    "enabled", "follow_enabled", "sub_enabled", "resub_enabled",
                    "gift_enabled", "cheer_enabled", "raid_enabled",
                    "watch_streak_enabled",
                )
                for suffix in alert_checkbox_suffixes:
                    key = f"trya_stream_twitch_alerts_{suffix}"
                    await db.set_setting(key, "on" if form.get(key) else "off")
                for suffix in (
                    "follow_template", "sub_template", "resub_template",
                    "gift_template", "cheer_template", "raid_template",
                    "watch_streak_template",
                ):
                    key = f"trya_stream_twitch_alerts_{suffix}"
                    await db.set_setting(key, (form.get(key) or "")[:500])
                await trya_stream_event_alerts.restart()
                await flash("TrYa Stream Twitch alert settings saved.", "success")

            elif action == "restart_trya_alerts":
                await trya_stream_event_alerts.restart()
                await flash("TrYa Stream EventSub listener restarted.", "success")

            elif action == "test_trya_alert":
                alert_bot = _TwitchBot(db, key_prefix="trya_stream_twitch")
                ok, message = await alert_bot.start()
                if ok:
                    ok, message = await alert_bot.send_chat(
                        "TrYa Stream alert test: EventSub chat delivery is ready."
                    )
                await flash(message, "success" if ok else "error")

            elif action == "save_exp_settings":
                ch1 = form.get("exp_post_channel_1_id", "").strip()
                ch2 = form.get("exp_post_channel_2_id", "").strip()
                ch3 = form.get("exp_post_channel_3_id", "").strip()
                expiry_ch = form.get("exp_expiry_channel_id", "").strip()
                announcement_ch = form.get("exp_announcement_channel_id", "").strip()
                announcement_msg = (form.get("exp_announcement_message") or "").strip()
                stream_url_v = form.get("exp_stream_url", "").strip()
                moderation_en = "on" if form.get("exp_moderation_enabled") else "off"
                loop_mode_v = form.get("exp_loop_mode", "reshuffle").strip()
                if loop_mode_v not in ("stop", "reshuffle"):
                    loop_mode_v = "reshuffle"
                # Auto-start scheduler
                sched_en = "on" if form.get("exp_schedule_enabled") else "off"
                # Day checkboxes named exp_schedule_day_0..6 (Mon=0 .. Sun=6)
                sched_days = ",".join(
                    str(i) for i in range(7) if form.get(f"exp_schedule_day_{i}")
                )
                sched_time = (form.get("exp_schedule_time") or "").strip()
                # Validate HH:MM
                import re as _re
                if not _re.match(r"^\d{1,2}:\d{2}$", sched_time):
                    sched_time = ""
                await db.set_setting("trya_stream_post_channel_1_id", ch1)
                await db.set_setting("trya_stream_post_channel_2_id", ch2)
                await db.set_setting("trya_stream_post_channel_3_id", ch3)
                await db.set_setting("trya_stream_expiry_channel_id", expiry_ch)
                await db.set_setting("trya_stream_announcement_channel_id", announcement_ch)
                await db.set_setting("trya_stream_announcement_message", announcement_msg)
                progress_overlay_en = "on" if form.get("exp_progress_overlay") else "off"
                ravenveil_early_boost_en = "on" if form.get("exp_ravenveil_early_boost") else "off"
                try:
                    max_per_user_v = max(1, min(20, int(form.get("exp_max_per_user", "4") or "4")))
                except (ValueError, TypeError):
                    max_per_user_v = 4
                try:
                    submission_playlist_days = max(
                        1,
                        min(3650, int(form.get("trya_submission_playlist_days", "14") or "14")),
                    )
                except (ValueError, TypeError):
                    submission_playlist_days = 14
                try:
                    video_bitrate_v = int(form.get("exp_video_bitrate_kbps", "2500") or "2500")
                except (ValueError, TypeError):
                    video_bitrate_v = 2500
                if video_bitrate_v not in (1800, 2000, 2500):
                    video_bitrate_v = 2500
                await db.set_setting("trya_stream_max_per_user", str(max_per_user_v))
                await db.set_setting("trya_stream_submission_playlist_days", str(submission_playlist_days))
                await db.set_setting("trya_stream_video_bitrate_kbps", str(video_bitrate_v))
                await db.set_setting("trya_stream_moderation_enabled", moderation_en)
                await db.set_setting("trya_stream_loop_mode", loop_mode_v)
                await db.set_setting("trya_stream_schedule_enabled", sched_en)
                await db.set_setting("trya_stream_schedule_days", sched_days)
                await db.set_setting("trya_stream_schedule_time", sched_time)
                await db.set_setting("trya_stream_progress_overlay", progress_overlay_en)
                stream_title_enabled = "on" if form.get("exp_stream_title_enabled") else "off"
                stream_title_text = (form.get("exp_stream_title_text") or "").strip()[:200]
                await db.set_setting("trya_stream_title_enabled", stream_title_enabled)
                await db.set_setting("trya_stream_title_text", stream_title_text)
                disclaimer_enabled = "on" if form.get("exp_disclaimer_enabled") else "off"
                disclaimer_text = (form.get("exp_disclaimer_text") or "").strip()[:2000]
                await db.set_setting("trya_stream_disclaimer_enabled", disclaimer_enabled)
                await db.set_setting("trya_stream_disclaimer_text", disclaimer_text)
                media_corners_enabled = "on" if form.get("exp_media_corners_enabled") else "off"
                media_border_enabled = "on" if form.get("exp_media_border_enabled") else "off"
                try:
                    media_corner_radius = max(
                        1, min(120, int(form.get("exp_media_corner_radius", "28") or "28"))
                    )
                except (TypeError, ValueError):
                    media_corner_radius = 28
                try:
                    media_border_width = max(
                        1, min(20, int(form.get("exp_media_border_width", "3") or "3"))
                    )
                except (TypeError, ValueError):
                    media_border_width = 3
                media_border_color = (form.get("exp_media_border_color") or "#A855F7").strip().upper()
                if not _re.fullmatch(r"#[0-9A-F]{6}", media_border_color):
                    media_border_color = "#A855F7"
                await db.set_setting("trya_stream_media_corners_enabled", media_corners_enabled)
                await db.set_setting("trya_stream_media_corner_radius", str(media_corner_radius))
                await db.set_setting("trya_stream_media_border_enabled", media_border_enabled)
                await db.set_setting("trya_stream_media_border_width", str(media_border_width))
                await db.set_setting("trya_stream_media_border_color", media_border_color)
                await db.set_setting("trya_stream_ravenveil_early_boost", ravenveil_early_boost_en)
                active_pl_v = form.get("exp_active_playlist", "submission")
                if active_pl_v not in ("submission", "admin", "both"):
                    active_pl_v = "submission"
                await db.set_setting("trya_stream_active_playlist", active_pl_v)
                intro_en = "on" if form.get("exp_intro_enabled") else "off"
                outro_en = "on" if form.get("exp_outro_enabled") else "off"
                intro_selection = (form.get("exp_intro_selection") or "random").strip()
                outro_selection = (form.get("exp_outro_selection") or "random").strip()
                await db.set_setting("trya_stream_intro_enabled", intro_en)
                await db.set_setting("trya_stream_outro_enabled", outro_en)
                await db.set_setting("trya_stream_intro_selection", intro_selection)
                await db.set_setting("trya_stream_outro_selection", outro_selection)
                if stream_url_v:
                    await db.set_setting("trya_stream_stream_url", stream_url_v)
                await flash("Settings saved.", "success")

            elif action == "save_exp_twitch_settings":
                exp_tw_cid = form.get("exp_twitch_client_id", "").strip()
                exp_tw_sec = form.get("exp_twitch_client_secret", "").strip()
                exp_tw_rt  = form.get("exp_twitch_refresh_token", "").strip()
                exp_tw_bc  = form.get("exp_twitch_broadcaster_login", "").strip()
                chat_en    = "on" if form.get("exp_twitch_chat_enabled") else "off"
                relic_en   = "on" if form.get("trya_relic_hunt_enabled") else "off"
                if exp_tw_cid:
                    await db.set_setting("trya_stream_twitch_client_id", exp_tw_cid)
                if exp_tw_sec and not exp_tw_sec.startswith("****"):
                    await db.set_setting("trya_stream_twitch_client_secret", exp_tw_sec)
                if exp_tw_rt and not exp_tw_rt.startswith("****"):
                    await db.set_setting("trya_stream_twitch_refresh_token", exp_tw_rt)
                    await db.set_setting("trya_stream_twitch_bot_login", "")
                    await db.set_setting("trya_stream_twitch_bot_user_id", "")
                if exp_tw_bc:
                    bn = exp_tw_bc.strip().rstrip("/").lstrip("#").lower()
                    if "twitch.tv/" in bn:
                        bn = bn.split("twitch.tv/", 1)[1].split("/")[0]
                    await db.set_setting("trya_stream_twitch_broadcaster_login", bn)
                await db.set_setting("trya_stream_twitch_chat_enabled", chat_en)
                await db.set_setting("trya_stream_relic_hunt_enabled", relic_en)
                if relic_en == "on":
                    await trya_relic_hunt.stop()
                    asyncio.create_task(_trya_relic_hunt_autostart())
                else:
                    await trya_relic_hunt.stop()
                await flash("Twitch Chat Bot and Raven's Nest settings saved.", "success")

            elif action == "restart_trya_relic_hunt":
                await trya_relic_hunt.stop()
                asyncio.create_task(_trya_relic_hunt_autostart())
                await flash("TrYa Raven's Nest listener restart queued.", "success")

            elif action == "stop_trya_relic_hunt":
                await trya_relic_hunt.stop()
                await flash("TrYa Raven's Nest listener stopped.", "success")

            elif action == "post_exp_stream_url":
                ch_id = form.get("post_channel_id_select", "")
                exp_stream_url_v = await db.get_setting("trya_stream_stream_url") or ""
                if not exp_stream_url_v:
                    await flash("No stream URL configured. Open Settings to add one.", "danger")
                else:
                    ok, name_or_err = await _post_trya_stream_announcement(ch_id, exp_stream_url_v)
                    if ok:
                        await flash(f"Stream link posted to #{name_or_err}.", "success")
                    else:
                        await flash(f"Could not post: {name_or_err}", "danger")

            elif action == "post_exp_announcement":
                ch_id = await db.get_setting("trya_stream_announcement_channel_id") or ""
                message_text = (await db.get_setting("trya_stream_announcement_message") or "").strip()
                if not ch_id:
                    await flash("No announcement channel configured. Open Settings to add one.", "danger")
                elif not message_text:
                    await flash("No announcement message configured. Open Settings to add one.", "danger")
                else:
                    guild = get_guild()
                    channel = None
                    if guild:
                        try:
                            ch_int = int(ch_id)
                            channel = guild.get_channel(ch_int) or guild.get_thread(ch_int)
                        except Exception:
                            channel = None
                    if not channel:
                        await flash("Announcement channel not found.", "danger")
                    else:
                        try:
                            await channel.send(message_text)
                            await flash(f"Announcement posted to #{channel.name}.", "success")
                        except Exception as e:
                            await flash(f"Could not post announcement: {e}", "danger")

            elif action == "upload_background":
                bg_file = files.get("bg_file")
                if bg_file and bg_file.filename:
                    ext = bg_file.filename.rsplit(".", 1)[-1].lower()
                    if ext in ("jpg", "jpeg", "png", "mp4", "webm"):
                        bg_type = "video" if ext in ("mp4", "webm") else "image"
                        fn = f"exp_bg_{_uuid.uuid4().hex}.{ext}"
                        await bg_file.save(os.path.join(TRYA_STREAM_DIR, "assets", fn))
                        old = await db.get_setting("trya_stream_bg_filename")
                        if old:
                            old_path = os.path.join(TRYA_STREAM_DIR, "assets", old)
                            if os.path.exists(old_path):
                                os.remove(old_path)
                        await db.set_setting("trya_stream_bg_filename", fn)
                        await db.set_setting("trya_stream_bg_type", bg_type)
                        await flash("Background uploaded.", "success")
                    else:
                        await flash("Unsupported file type.", "danger")

            elif action == "upload_loop_video":
                import json as _jl
                lv_file = files.get("lv_file")
                if lv_file and lv_file.filename:
                    ext = lv_file.filename.rsplit(".", 1)[-1].lower()
                    if ext in ("mp4", "webm"):
                        fn = f"exp_loop_{_uuid.uuid4().hex}.{ext}"
                        label = form.get("lv_label", "").strip() or lv_file.filename
                        await lv_file.save(os.path.join(TRYA_STREAM_DIR, "assets", fn))
                        raw = await db.get_setting("trya_stream_loop_videos") or "[]"
                        vids = _jl.loads(raw) if raw else []
                        vids.append({"filename": fn, "label": label})
                        await db.set_setting("trya_stream_loop_videos", _jl.dumps(vids))
                        # If this is the first video, auto-select it
                        if len(vids) == 1:
                            await db.set_setting("trya_stream_loop_selection", fn)
                        await flash("Loop video uploaded.", "success")
                    else:
                        await flash("Only MP4/WebM supported.", "danger")

            elif action == "save_exp_loop_source":
                obs_overlay_enabled_v = "on" if form.get("exp_obs_overlay_enabled") else "off"
                loop_rtmp_key_v = (form.get("exp_loop_rtmp_key") or "").strip()
                try:
                    obs_overlay_fps_v = int(form.get("exp_obs_overlay_fps", "20") or "20")
                except (TypeError, ValueError):
                    obs_overlay_fps_v = 20
                if obs_overlay_fps_v not in (15, 20, 24):
                    obs_overlay_fps_v = 20
                if obs_overlay_enabled_v == "on" and not loop_rtmp_key_v:
                    import secrets as _secrets
                    loop_rtmp_key_v = _secrets.token_urlsafe(18)
                await db.set_setting("trya_stream_obs_overlay_enabled", obs_overlay_enabled_v)
                await db.set_setting("trya_stream_obs_overlay_fps", str(obs_overlay_fps_v))
                await db.set_setting("trya_stream_loop_source", "local")
                await db.set_setting("trya_stream_loop_rtmp_key", loop_rtmp_key_v)
                await flash("OBS overlay settings saved.", "success")

            elif action == "delete_loop_video":
                import json as _jl
                del_fn = form.get("loop_filename", "")
                if del_fn:
                    raw = await db.get_setting("trya_stream_loop_videos") or "[]"
                    vids = _jl.loads(raw) if raw else []
                    vids = [v for v in vids if v.get("filename") != del_fn]
                    await db.set_setting("trya_stream_loop_videos", _jl.dumps(vids))
                    del_path = os.path.join(TRYA_STREAM_DIR, "assets", del_fn)
                    if os.path.exists(del_path):
                        os.remove(del_path)
                    # If the deleted video was selected, fall back to shuffle
                    sel = await db.get_setting("trya_stream_loop_selection") or "shuffle"
                    if sel == del_fn:
                        await db.set_setting("trya_stream_loop_selection", "shuffle")
                    await flash("Loop video removed.", "success")

            elif action == "set_loop_selection":
                sel = form.get("loop_selection", "shuffle")
                await db.set_setting("trya_stream_loop_selection", sel)
                if sel == "concat_random_subset":
                    import json as _loop_json
                    raw = await db.get_setting("trya_stream_loop_videos") or "[]"
                    videos = _loop_json.loads(raw) if raw else []
                    try:
                        count = int(form.get("loop_concat_random_count", "10") or "10")
                    except (TypeError, ValueError):
                        count = 10
                    count = max(2, min(count, len(videos))) if len(videos) > 1 else 1
                    await db.set_setting("trya_stream_loop_random_count", str(count))
                loop_selection_labels = {
                    "shuffle": "Shuffle",
                    "concat_all": "Concatenate all videos",
                    "concat_all_random": "Concatenate all videos in random order",
                    "concat_random_subset": "Concatenate a random video selection",
                }
                await flash(f"Loop video selection: {loop_selection_labels.get(sel, sel)}", "success")

            return redirect(request.url)

        songs = await db.get_all_trya_stream_songs(active_only=True, source="submission")
        # Enrich each song with parsed analysis info for the admin UI:
        # word_count, coverage span and a transcript preview reconstructed
        # from the stored word_timestamps JSON.
        import json as _json
        for s in songs:
            wt_raw = s.get("word_timestamps")
            s["word_count"] = 0
            s["transcript_preview"] = ""
            s["transcript_span"] = None
            if wt_raw:
                try:
                    wt = _json.loads(wt_raw) if isinstance(wt_raw, str) else wt_raw
                    if isinstance(wt, list) and wt:
                        s["word_count"] = len(wt)
                        s["transcript_span"] = (
                            float(wt[0].get("start", 0)),
                            float(wt[-1].get("end", 0)),
                        )
                        # Reconstructed transcript with [mm:ss] markers every ~10s
                        parts, last_mark = [], -10.0
                        for w in wt:
                            st = float(w.get("start", 0))
                            if st - last_mark >= 10:
                                m, sec = divmod(int(st), 60)
                                parts.append(f"\n[{m}:{sec:02d}] ")
                                last_mark = st
                            parts.append(w.get("word", ""))
                            parts.append(" ")
                        s["transcript_preview"] = "".join(parts).strip()
                except Exception:
                    pass
        status = await trya_stream_manager.get_status()
        masked_key = "*" * 20 if await db.get_setting("trya_stream_twitch_key") else ""
        bg_filename  = await db.get_setting("trya_stream_bg_filename") or ""
        import json as _jlv
        _loop_raw = await db.get_setting("trya_stream_loop_videos") or "[]"
        loop_videos = _jlv.loads(_loop_raw) if _loop_raw else []
        # Auto-migrate legacy single-video setting
        if not loop_videos:
            _old_lv = await db.get_setting("trya_stream_loop_filename") or ""
            if _old_lv:
                loop_videos = [{"filename": _old_lv, "label": _old_lv}]
                await db.set_setting("trya_stream_loop_videos", _jlv.dumps(loop_videos))
                await db.set_setting("trya_stream_loop_selection", _old_lv)
                await db.set_setting("trya_stream_loop_filename", "")
        loop_selection = await db.get_setting("trya_stream_loop_selection") or "shuffle"
        try:
            loop_concat_random_count = int(
                await db.get_setting("trya_stream_loop_random_count") or "10"
            )
        except (TypeError, ValueError):
            loop_concat_random_count = 10
        if loop_videos:
            minimum_count = 2 if len(loop_videos) > 1 else 1
            loop_concat_random_count = max(
                minimum_count, min(loop_concat_random_count, len(loop_videos))
            )
        else:
            loop_concat_random_count = 1
        exp_stream_url = await db.get_setting("trya_stream_stream_url") or ""
        exp_post_channel_1_id = await db.get_setting("trya_stream_post_channel_1_id") or ""
        exp_post_channel_2_id = await db.get_setting("trya_stream_post_channel_2_id") or ""
        exp_post_channel_3_id = await db.get_setting("trya_stream_post_channel_3_id") or ""
        exp_expiry_channel_id = await db.get_setting("trya_stream_expiry_channel_id") or ""
        exp_announcement_channel_id = await db.get_setting("trya_stream_announcement_channel_id") or ""
        exp_announcement_message = await db.get_setting("trya_stream_announcement_message") or ""
        exp_twitch_chat_enabled = await db.get_setting("trya_stream_twitch_chat_enabled") or "off"
        trya_relic_hunt_enabled = await db.get_setting("trya_stream_relic_hunt_enabled") or "on"
        exp_moderation_enabled  = await db.get_setting("trya_stream_moderation_enabled") or "off"
        exp_loop_mode           = await db.get_setting("trya_stream_loop_mode") or "reshuffle"
        exp_progress_overlay    = await db.get_setting("trya_stream_progress_overlay") or "off"
        exp_stream_title_enabled = await db.get_setting("trya_stream_title_enabled") or "off"
        exp_stream_title_text   = await db.get_setting("trya_stream_title_text") or ""
        exp_disclaimer_enabled  = await db.get_setting("trya_stream_disclaimer_enabled") or "off"
        exp_disclaimer_text     = await db.get_setting("trya_stream_disclaimer_text") or ""
        exp_media_corners_enabled = await db.get_setting("trya_stream_media_corners_enabled") or "off"
        exp_media_border_enabled = await db.get_setting("trya_stream_media_border_enabled") or "off"
        try:
            exp_media_corner_radius = max(
                1, min(120, int(await db.get_setting("trya_stream_media_corner_radius") or "28"))
            )
        except (TypeError, ValueError):
            exp_media_corner_radius = 28
        try:
            exp_media_border_width = max(
                1, min(20, int(await db.get_setting("trya_stream_media_border_width") or "3"))
            )
        except (TypeError, ValueError):
            exp_media_border_width = 3
        exp_media_border_color = (
            await db.get_setting("trya_stream_media_border_color") or "#A855F7"
        ).strip().upper()
        if not re.fullmatch(r"#[0-9A-F]{6}", exp_media_border_color):
            exp_media_border_color = "#A855F7"
        exp_ravenveil_early_boost = await db.get_setting("trya_stream_ravenveil_early_boost") or "off"
        exp_max_per_user        = int(await db.get_setting("trya_stream_max_per_user") or "4")
        try:
            trya_submission_playlist_days = max(
                1,
                min(3650, int(await db.get_setting("trya_stream_submission_playlist_days") or "14")),
            )
        except (TypeError, ValueError):
            trya_submission_playlist_days = 14
        try:
            exp_video_bitrate_kbps = int(await db.get_setting("trya_stream_video_bitrate_kbps") or "2500")
        except (TypeError, ValueError):
            exp_video_bitrate_kbps = 2500
        if exp_video_bitrate_kbps not in (1800, 2000, 2500):
            exp_video_bitrate_kbps = 2500
        exp_loop_source        = await db.get_setting("trya_stream_loop_source") or "local"
        if exp_loop_source not in ("local", "rtmp"):
            exp_loop_source = "local"
        exp_obs_overlay_enabled = await db.get_setting("trya_stream_obs_overlay_enabled") or "off"
        if exp_loop_source == "rtmp":
            exp_obs_overlay_enabled = "on"
        try:
            exp_obs_overlay_fps = int(await db.get_setting("trya_stream_obs_overlay_fps") or "20")
        except (TypeError, ValueError):
            exp_obs_overlay_fps = 20
        if exp_obs_overlay_fps not in (15, 20, 24):
            exp_obs_overlay_fps = 20
        exp_loop_rtmp_key      = await db.get_setting("trya_stream_loop_rtmp_key") or ""
        exp_schedule_enabled    = await db.get_setting("trya_stream_schedule_enabled") or "off"
        exp_schedule_days       = await db.get_setting("trya_stream_schedule_days") or ""
        exp_schedule_time       = await db.get_setting("trya_stream_schedule_time") or ""
        exp_schedule_days_set   = set(d for d in exp_schedule_days.split(",") if d)
        exp_active_playlist     = await db.get_setting("trya_stream_active_playlist") or "submission"
        exp_intro_enabled       = await db.get_setting("trya_stream_intro_enabled") or "off"
        exp_outro_enabled       = await db.get_setting("trya_stream_outro_enabled") or "off"
        exp_intro_selection     = await db.get_setting("trya_stream_intro_selection") or "random"
        exp_outro_selection     = await db.get_setting("trya_stream_outro_selection") or "random"
        exp_tw_client_id = await db.get_setting("trya_stream_twitch_client_id") or ""
        _exp_tw_secret   = await db.get_setting("trya_stream_twitch_client_secret") or ""
        _exp_tw_refresh  = await db.get_setting("trya_stream_twitch_refresh_token") or ""
        exp_tw_secret_masked  = f"****{_exp_tw_secret[-4:]}"  if len(_exp_tw_secret)  > 4 else ""
        exp_tw_refresh_masked = f"****{_exp_tw_refresh[-4:]}" if len(_exp_tw_refresh) > 4 else ""
        exp_tw_broadcaster = await db.get_setting("trya_stream_twitch_broadcaster_login") or ""
        exp_tw_bot_login   = await db.get_setting("trya_stream_twitch_bot_login") or ""
        # Quick scope check for UI badge (uses cached access token if available)
        exp_tw_scopes_ok = False
        try:
            import aiohttp as _ah
            _rt = await db.get_setting("trya_stream_twitch_refresh_token") or ""
            _cid = await db.get_setting("trya_stream_twitch_client_id") or ""
            _cs  = await db.get_setting("trya_stream_twitch_client_secret") or ""
            if _rt and _cid and _cs:
                async with _ah.ClientSession() as _hs:
                    async with _hs.post(
                        "https://id.twitch.tv/oauth2/token",
                        data={"client_id": _cid, "client_secret": _cs,
                              "grant_type": "refresh_token", "refresh_token": _rt},
                        timeout=_ah.ClientTimeout(total=8),
                    ) as _hr:
                        _hd = await _hr.json()
                        _tok = _hd.get("access_token", "")
                    if _tok:
                        async with _hs.get(
                            "https://id.twitch.tv/oauth2/validate",
                            headers={"Authorization": f"OAuth {_tok}"},
                            timeout=_ah.ClientTimeout(total=8),
                        ) as _hr:
                            _hv = await _hr.json()
                            exp_tw_scopes_ok = "chat:read" in (_hv.get("scopes") or [])
        except Exception:
            pass
        exp_guild = get_guild()
        exp_text_channels = []
        if exp_guild:
            for _ch in sorted(exp_guild.text_channels, key=lambda c: c.position):
                exp_text_channels.append({"id": _ch.id, "name": _ch.name})
        admin_songs = await db.get_all_trya_stream_songs(active_only=False, source="admin")
        intro_songs = await db.get_all_trya_stream_songs(active_only=False, source="intro")
        outro_songs = await db.get_all_trya_stream_songs(active_only=False, source="outro")
        active_intro_songs = [s for s in intro_songs if s.get("active") and s.get("analysis_status") == "done" and s.get("mp3_filename")]
        active_outro_songs = [s for s in outro_songs if s.get("active") and s.get("analysis_status") == "done" and s.get("mp3_filename")]
        trya_alert_settings = {}
        for default_key, default_value in DEFAULT_ALERT_SETTINGS.items():
            key = default_key.replace("twitch_alerts", "trya_stream_twitch_alerts", 1)
            trya_alert_settings[key] = await db.get_setting(key) or default_value
        trya_eventsub_diag = await _TwitchBot(
            db, key_prefix="trya_stream_twitch_alerts_eventsub"
        ).diagnose()
        return await render_template(
            "trya_stream.html",
            songs=songs, status=status,
            admin_songs=admin_songs,
            intro_songs=intro_songs,
            outro_songs=outro_songs,
            exp_active_playlist=exp_active_playlist,
            exp_intro_enabled=exp_intro_enabled,
            exp_outro_enabled=exp_outro_enabled,
            exp_intro_selection=exp_intro_selection,
            exp_outro_selection=exp_outro_selection,
            active_intro_songs=active_intro_songs,
            active_outro_songs=active_outro_songs,
            masked_key=masked_key,
            bg_filename=bg_filename,
            loop_videos=loop_videos, loop_selection=loop_selection,
            loop_concat_random_count=loop_concat_random_count,
            exp_stream_url=exp_stream_url,
            exp_post_channel_1_id=exp_post_channel_1_id,
            exp_post_channel_2_id=exp_post_channel_2_id,
            exp_post_channel_3_id=exp_post_channel_3_id,
            exp_expiry_channel_id=exp_expiry_channel_id,
            exp_announcement_channel_id=exp_announcement_channel_id,
            exp_announcement_message=exp_announcement_message,
            exp_twitch_chat_enabled=exp_twitch_chat_enabled,
            trya_relic_hunt_enabled=trya_relic_hunt_enabled,
            trya_relic_hunt_running=trya_relic_hunt._running,
            exp_moderation_enabled=exp_moderation_enabled,
            exp_loop_mode=exp_loop_mode,
            exp_loop_source=exp_loop_source,
            exp_obs_overlay_enabled=exp_obs_overlay_enabled,
            exp_obs_overlay_fps=exp_obs_overlay_fps,
            exp_loop_rtmp_key=exp_loop_rtmp_key,
            exp_progress_overlay=exp_progress_overlay,
            exp_stream_title_enabled=exp_stream_title_enabled,
            exp_stream_title_text=exp_stream_title_text,
            exp_disclaimer_enabled=exp_disclaimer_enabled,
            exp_disclaimer_text=exp_disclaimer_text,
            exp_media_corners_enabled=exp_media_corners_enabled,
            exp_media_corner_radius=exp_media_corner_radius,
            exp_media_border_enabled=exp_media_border_enabled,
            exp_media_border_width=exp_media_border_width,
            exp_media_border_color=exp_media_border_color,
            exp_ravenveil_early_boost=exp_ravenveil_early_boost,
            exp_max_per_user=exp_max_per_user,
            exp_video_bitrate_kbps=exp_video_bitrate_kbps,
            exp_schedule_enabled=exp_schedule_enabled,
            exp_schedule_time=exp_schedule_time,
            exp_schedule_days_set=exp_schedule_days_set,
            exp_tw_client_id=exp_tw_client_id,
            exp_tw_secret_masked=exp_tw_secret_masked,
            exp_tw_refresh_masked=exp_tw_refresh_masked,
            exp_tw_broadcaster=exp_tw_broadcaster,
            exp_tw_bot_login=exp_tw_bot_login,
            exp_tw_scopes_ok=exp_tw_scopes_ok,
            trya_submission_playlist_days=trya_submission_playlist_days,
            trya_alert_settings=trya_alert_settings,
            trya_alert_status=trya_stream_event_alerts.status,
            trya_eventsub_diag=trya_eventsub_diag,
            text_channels=exp_text_channels,
        )

    @app.route("/trya-stream/upload/<token>", methods=["GET", "POST"])
    async def trya_stream_upload(token: str):
        from bot.trya_stream_manager import is_submissions_locked
        from bot.trya_stream_worker import (
            TRYA_RIGHTS_DECLARATION,
            TRYA_RIGHTS_VERSION,
            ingest_uploaded_audio,
            process_exp_song,
        )

        song = await db.get_trya_stream_song_by_token(token)
        if not song:
            return await render_template(
                "trya_stream_upload.html", error="This upload link is invalid."
            )
        if song.get("original_uploaded_at"):
            return await render_template(
                "trya_stream_upload.html",
                done=True,
                title=song.get("title") or "Your song",
            )

        locked, _ = await is_submissions_locked(db)
        if locked:
            return await render_template("trya_stream_upload.html", stream_live=True)
        if request.method == "GET":
            return await render_template(
                "trya_stream_upload.html", song=song, token=token
            )

        import hashlib
        form = await request.form
        submitted_suno_url = (form.get("suno_url") or "").strip()
        submitted_hook = (form.get("hook_value") or "").strip()

        async def render_upload_error(message: str):
            return await render_template(
                "trya_stream_upload.html",
                song=song,
                token=token,
                form_error=message,
                submitted_suno_url=submitted_suno_url,
                submitted_hook=submitted_hook,
            )

        submission_ban = await db.get_exp_radio_submission_ban(int(song.get("user_id") or 0))
        if submission_ban:
            return await render_upload_error(
                "Your shared Exp. Radio / TrYa Stream submission ban is currently active."
            ), 403
        if not submitted_suno_url:
            return await render_upload_error("Enter the Suno song URL."), 400
        resolved_uuid = await resolve_suno_uuid(submitted_suno_url)
        if not resolved_uuid:
            return await render_upload_error(
                "Suno could not resolve that song URL. Check the link and try again."
            ), 400

        duplicate = next(
            (
                existing
                for existing in await db.get_all_trya_stream_songs(active_only=False)
                if int(existing.get("id") or 0) != int(song["id"])
                and str(existing.get("suno_uuid") or "").lower() == resolved_uuid.lower()
                and existing.get("analysis_status") != "failed"
                and existing.get("active")
            ),
            None,
        )
        if duplicate:
            return await render_upload_error(
                "That Suno song is already active or awaiting upload in TrYa Stream."
            ), 409

        if song.get("playlist_source") == "submission":
            max_per_user = int(await db.get_setting("trya_stream_max_per_user") or "4")
            active_count = sum(
                1
                for existing in await db.get_all_trya_stream_songs(active_only=False)
                if int(existing.get("id") or 0) != int(song["id"])
                and int(existing.get("user_id") or 0) == int(song.get("user_id") or 0)
                and (existing.get("playlist_source") or "submission") == "submission"
                and existing.get("analysis_status") != "failed"
                and existing.get("active")
            )
            if not song.get("replacement_song_id") and active_count >= max_per_user:
                return await render_upload_error(
                    f"You already have the maximum of {max_per_user} active submissions."
                ), 409

        resolved_hook = None
        if submitted_hook:
            from bot.suno_hook import SunoHookError, resolve_suno_hook
            try:
                resolved_hook = await resolve_suno_hook(submitted_hook)
                if resolved_hook["original_clip_id"].lower() != resolved_uuid.lower():
                    raise SunoHookError("This Hook belongs to a different Suno song.")
            except SunoHookError as exc:
                return await render_upload_error(str(exc)), 400
            except Exception:
                return await render_upload_error(
                    "Suno could not validate the Hook right now. Try again shortly."
                ), 502

        rights_hash = hashlib.sha256(
            (
                f"{TRYA_RIGHTS_VERSION}\n{TRYA_RIGHTS_DECLARATION}\n"
                f"user={song.get('user_id')}\nurl={submitted_suno_url}\n"
                f"uuid={resolved_uuid}\ndestination={song.get('playlist_source') or 'submission'}"
            ).encode()
        ).hexdigest()
        await db.update_trya_stream_song(
            song["id"],
            suno_url=submitted_suno_url,
            suno_uuid=resolved_uuid,
            rights_declaration=TRYA_RIGHTS_DECLARATION,
            rights_hash=rights_hash,
            rights_version=TRYA_RIGHTS_VERSION,
        )
        song = await db.get_trya_stream_song(song["id"])

        if resolved_hook:
            candidate = dict(song)
            candidate.update(resolved_hook)
            try:
                cached_path = await trya_stream_manager._get_video(
                    candidate, allow_hook_fallback=False
                )
                if not cached_path or not os.path.exists(cached_path):
                    raise RuntimeError("The Hook video could not be downloaded.")
                await db.update_trya_stream_song(
                    song["id"],
                    hook_id=resolved_hook["hook_id"],
                    hook_share_url=resolved_hook["hook_share_url"],
                    hook_video_url=resolved_hook["hook_video_url"],
                )
            except Exception as exc:
                return await render_upload_error(f"Hook validation failed: {exc}"), 400

        required_attestations = (
            "official_download_attested",
            "paid_download_attested",
            "not_suno_remix_attested",
            "third_party_rights_attested",
            "commercial_rights_attested",
        )
        if any(not form.get(name) for name in required_attestations):
            return await render_upload_error("Every rights confirmation is required."), 400
        files = await request.files
        uploaded = files.get("original_audio")
        if not uploaded or not uploaded.filename:
            return await render_upload_error(
                "Select the MP3 or M4A downloaded through Suno's official Download action."
            ), 400

        import tempfile
        incoming_dir = os.path.join(TRYA_STREAM_DIR, "incoming")
        os.makedirs(incoming_dir, exist_ok=True)
        suffix = os.path.splitext(uploaded.filename)[1].lower()
        fd, staged_path = tempfile.mkstemp(prefix=f"song_{song['id']}_", suffix=suffix, dir=incoming_dir)
        os.close(fd)
        try:
            await uploaded.save(staged_path)
            finalized = await ingest_uploaded_audio(
                db,
                song["id"],
                staged_path,
                TRYA_STREAM_DIR,
                original_filename=uploaded.filename,
                rights_version=song.get("rights_version") or TRYA_RIGHTS_VERSION,
                official_download_attested=True,
                paid_download_attested=True,
                is_suno_remix=False,
                third_party_rights_attested=True,
                commercial_rights_attested=True,
                rights_accepted_at=time.time(),
            )
        except Exception as exc:
            return await render_upload_error(str(exc)), 400
        finally:
            try:
                os.remove(staged_path)
            except OSError:
                pass

        asyncio.create_task(
            process_exp_song(
                db,
                song["id"],
                TRYA_STREAM_DIR,
                bot=bot,
                skip_moderation=(song.get("playlist_source") in {"intro", "outro", "admin"}),
                max_duration=None if song.get("playlist_source") in {"intro", "outro", "admin"} else 360,
            )
        )
        return await render_template(
            "trya_stream_upload.html",
            done=True,
            title=finalized.get("title") or "Your song",
        )

    @app.route("/trya-stream/stream/<action>", methods=["POST"])
    @permission_required('trya_stream')
    async def trya_stream_stream_action(action):
        from quart import jsonify
        if action in ("start", "start_legacy"):
            if app.database_restore_pending:
                return jsonify({"ok": False, "error": "A database restore is in progress."}), 409
            twitch_key = await db.get_setting("trya_stream_twitch_key") or ""
            if not twitch_key:
                return jsonify({"ok": False, "error": "No Twitch stream key configured."}), 400
            async with app.radio_start_lock:
                if app.database_restore_pending:
                    return jsonify({"ok": False, "error": "A database restore is in progress."}), 409
                if stream_manager.is_running or stream_manager._loading:
                    return jsonify({
                        "ok": False,
                        "error": "The legacy Twitch Radio is currently running or starting.",
                    }), 409
                if exp_stream_manager.is_running:
                    return jsonify({
                        "ok": False,
                        "error": "Experimental Radio is currently running.",
                    }), 409
                result = await trya_stream_manager.start(
                    twitch_key, legacy_pipeline=(action == "start_legacy"),
                )
        elif action == "stop":
            result = await trya_stream_manager.stop()
        elif action == "safe_stop":
            result = await trya_stream_manager.safe_stop()
        else:
            return jsonify({"ok": False, "error": "Unknown action"}), 400
        return jsonify(result)

    @app.route("/trya-stream/stream/status")
    @permission_required('trya_stream')
    async def trya_stream_stream_status():
        from quart import jsonify
        return jsonify(await trya_stream_manager.get_status())

    @app.route("/trya-stream/cover-preview/<int:song_id>")
    @permission_required('trya_stream')
    async def trya_stream_cover_preview(song_id):
        """Serve the locally cached cover MP4 for admin preview.

        Triggers an on-demand download (and normalization) via the stream
        manager if the file hasn't been cached yet, so an admin can verify
        what will actually be streamed without starting the full stream.
        """
        from quart import send_file, abort
        s = await db.get_trya_stream_song(song_id)
        if not s:
            return abort(404)
        # Lazily download + normalize through the stream manager so this
        # follows the same Hook > regular video priority as the live stream.
        path = await trya_stream_manager._get_video(s)
        if not path or not os.path.exists(path):
            return abort(404)
        return await send_file(path, mimetype="video/mp4")

    @app.route("/trya-stream/stream/log")
    @permission_required('trya_stream')
    async def trya_stream_stream_log():
        """Return live-log entries for the admin UI panel.

        Query params:
          since   – unix timestamp (float). If given, only return entries
                    strictly newer than this. Used for incremental polling.
          window  – fallback time window in seconds (default 300 = 5 min).
        """
        from quart import jsonify, request
        try:
            since = float(request.args.get("since", "0") or 0)
        except ValueError:
            since = 0.0
        try:
            window = float(request.args.get("window", "300") or 300)
        except ValueError:
            window = 300.0
        return jsonify({
            "running": trya_stream_manager.is_running,
            "entries": trya_stream_manager.get_log(since_ts=since, max_age_secs=window),
        })

    @app.route("/trya-stream/twitch-oauth-start")
    @permission_required('trya_stream')
    async def trya_stream_twitch_oauth_start():
        import secrets as _sec
        from urllib.parse import urlencode
        client_id = await db.get_setting("trya_stream_twitch_client_id")
        if not client_id:
            await flash("Client ID not configured — save it first.", "error")
            return redirect(url_for("trya_stream_admin"))
        state = _sec.token_urlsafe(16)
        session[_TWITCH_OAUTH_STATE_KEY] = state
        session[_TWITCH_OAUTH_MODE_KEY] = "trya_bot"
        redirect_uri = _twitch_oauth_redirect_uri()
        params = urlencode({
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": _TWITCH_BOT_SCOPES,
            "state": state,
        })
        return redirect(f"https://id.twitch.tv/oauth2/authorize?{params}")

    @app.route("/exp-radio", methods=["GET", "POST"])
    @permission_required('exp_radio')
    async def exp_radio_admin():
        import csv, io, uuid as _uuid
        from datetime import datetime, timezone
        from quart import jsonify, send_file

        if request.method == "POST":
            form = await request.form
            files = await request.files
            action = form.get("action", "")

            if action == "delete_song":
                song_id = int(form.get("song_id", 0))
                removed = await db.delete_exp_radio_song(song_id)
                if removed:
                    protected_songs = await db.get_all_exp_radio_songs(active_only=True)
                    cleanup_exp_radio_song_files(
                        EXP_RADIO_DIR, removed, protected_songs
                    )
                    await flash("Song and cached media removed.", "success")
                else:
                    await flash("Song not found.", "error")

            elif action == "set_hook_video":
                import bot.exp_stream_manager as _esm
                from bot.suno_hook import SunoHookError, resolve_suno_hook

                song_id = int(form.get("song_id", 0) or 0)
                hook_value = (form.get("hook_value") or "").strip()
                song = await db.get_exp_radio_song(song_id) if song_id else None
                if _esm.stream_is_live:
                    await flash("Stop the stream before changing a Hook video.", "error")
                elif not song or song.get("playlist_source") not in ("submission", "admin"):
                    await flash("Song not found in the submission or admin playlist.", "error")
                else:
                    try:
                        hook = await resolve_suno_hook(hook_value)
                        if hook["original_clip_id"].lower() != str(song.get("suno_uuid") or "").lower():
                            raise SunoHookError(
                                "This Hook belongs to a different Suno song."
                            )
                        candidate = dict(song)
                        candidate.update(hook)
                        cached_path = await exp_stream_manager._get_video(
                            candidate, allow_hook_fallback=False
                        )
                        if not cached_path or not os.path.exists(cached_path):
                            raise SunoHookError("The Hook video could not be downloaded.")
                        old_hook_id = (song.get("hook_id") or "").strip()
                        await db.update_exp_radio_song(
                            song_id,
                            hook_id=hook["hook_id"],
                            hook_share_url=hook["hook_share_url"],
                            hook_video_url=hook["hook_video_url"],
                        )
                        if old_hook_id and old_hook_id != hook["hook_id"]:
                            old_path = exp_radio_hook_cache_path(
                                EXP_RADIO_DIR, song_id, old_hook_id
                            )
                            try:
                                os.remove(old_path)
                            except FileNotFoundError:
                                pass
                        await flash(
                            f"Hook video set for “{song.get('title') or song_id}”.",
                            "success",
                        )
                    except SunoHookError as exc:
                        await flash(str(exc), "error")
                    except Exception as exc:
                        print(
                            f"[exp-radio] Hook setup failed for song #{song_id}: {exc}",
                            flush=True,
                        )
                        await flash("Hook setup failed. Check the server log.", "error")

            elif action == "remove_hook_video":
                import bot.exp_stream_manager as _esm

                song_id = int(form.get("song_id", 0) or 0)
                song = await db.get_exp_radio_song(song_id) if song_id else None
                if _esm.stream_is_live:
                    await flash("Stop the stream before removing a Hook video.", "error")
                elif not song:
                    await flash("Song not found.", "error")
                else:
                    await db.update_exp_radio_song(
                        song_id,
                        hook_id=None,
                        hook_share_url=None,
                        hook_video_url=None,
                    )
                    cleanup_exp_radio_hook_files(EXP_RADIO_DIR, song)
                    await flash(
                        f"Hook video removed from “{song.get('title') or song_id}”.",
                        "success",
                    )

            elif action == "add_intro_song":
                suno_url = (form.get("suno_url") or "").strip()
                if not suno_url:
                    await flash("Please enter a Suno URL.", "error")
                else:
                    from bot.exp_radio_worker import process_intro_outro_song
                    async def _bg_intro(url=suno_url):
                        ok, msg = await process_intro_outro_song(db, url, "intro", EXP_RADIO_DIR)
                        from bot.exp_stream_manager import log_event
                        log_event(msg, "info" if ok else "error", "[intro]")
                    asyncio.create_task(_bg_intro())
                    await flash("Intro song queued for download and Whisper analysis.", "success")

            elif action == "add_outro_song":
                suno_url = (form.get("suno_url") or "").strip()
                if not suno_url:
                    await flash("Please enter a Suno URL.", "error")
                else:
                    from bot.exp_radio_worker import process_intro_outro_song
                    async def _bg_outro(url=suno_url):
                        ok, msg = await process_intro_outro_song(db, url, "outro", EXP_RADIO_DIR)
                        from bot.exp_stream_manager import log_event
                        log_event(msg, "info" if ok else "error", "[outro]")
                    asyncio.create_task(_bg_outro())
                    await flash("Outro song queued for download and Whisper analysis.", "success")

            elif action == "add_admin_song":
                suno_url = (form.get("suno_url") or "").strip()
                if not suno_url:
                    await flash("Please enter a Suno URL.", "error")
                else:
                    from bot.exp_radio_worker import process_admin_song
                    async def _bg_add(url=suno_url):
                        ok, msg = await process_admin_song(db, url, EXP_RADIO_DIR)
                        from bot.exp_stream_manager import log_event
                        log_event(msg, "info" if ok else "error", "[admin-pl]")
                    asyncio.create_task(_bg_add())
                    await flash(
                        "Song queued for download and Whisper analysis. "
                        "Check the Live Log for progress.", "success"
                    )

            elif action == "add_submission_song":
                suno_url = (form.get("suno_url") or "").strip()
                user_ref = (form.get("submission_user_ref") or "").strip()
                if not suno_url:
                    await flash("Please enter a Suno URL.", "error")
                else:
                    submitter_user_id = 0
                    submitter_user_name = "admin-ui"
                    if user_ref:
                        user = None
                        if user_ref.isdigit() and bot is not None:
                            submitter_user_id = int(user_ref)
                            try:
                                user = bot.get_user(submitter_user_id) or await bot.fetch_user(submitter_user_id)
                            except Exception:
                                user = None
                        elif bot is not None:
                            guild = get_guild()
                            query = user_ref.lower().lstrip("@").strip()
                            if guild:
                                members = list(getattr(guild, "members", []) or [])
                                def _names(member):
                                    return [
                                        str(getattr(member, "name", "") or ""),
                                        str(getattr(member, "display_name", "") or ""),
                                        str(getattr(member, "global_name", "") or ""),
                                    ]
                                exact = [
                                    m for m in members
                                    if any(n.lower() == query for n in _names(m) if n)
                                ]
                                matches = exact or [
                                    m for m in members
                                    if any(query in n.lower() for n in _names(m) if n)
                                ]
                                if len(matches) == 1:
                                    user = matches[0]
                                elif len(matches) > 1:
                                    names = ", ".join(str(m) for m in matches[:5])
                                    await flash(f"Discord user name is ambiguous. Use the numeric user ID. Matches: {names}", "error")
                                    return redirect(request.url)
                        if user is None:
                            await flash("Discord user not found. Use the numeric user ID or leave the field empty.", "error")
                            return redirect(request.url)
                        submitter_user_id = int(user.id)
                        submitter_user_name = str(user)
                    from bot.exp_radio_worker import process_admin_submission_song
                    async def _bg_add_submission(
                        url=suno_url,
                        uid=submitter_user_id,
                        uname=submitter_user_name,
                    ):
                        ok, msg = await process_admin_submission_song(
                            db, url, EXP_RADIO_DIR, bot=bot,
                            submitter_user_id=uid,
                            submitter_user_name=uname,
                        )
                        from bot.exp_stream_manager import log_event
                        log_event(msg, "info" if ok else "error", "[submit-admin]")
                    asyncio.create_task(_bg_add_submission())
                    await flash(
                        "Song queued for the regular submission playlist. "
                        "It will run through the normal Whisper and moderation pipeline.",
                        "success",
                    )

            elif action == "delete_all_songs":
                import bot.exp_stream_manager as _esm
                if _esm.stream_is_live:
                    await flash("Cannot clear the playlist while the stream is live.", "error")
                else:
                    removed = await db.get_all_exp_radio_songs(
                        active_only=True, source="submission"
                    )
                    count = await db.delete_all_exp_radio_songs(source="submission")
                    protected_songs = await db.get_all_exp_radio_songs(active_only=True)
                    for song in removed:
                        cleanup_exp_radio_song_files(
                            EXP_RADIO_DIR, song, protected_songs
                        )
                    await flash(f"Playlist cleared — {count} song(s) removed.", "success")

            elif action == "delete_all_admin_songs":
                import bot.exp_stream_manager as _esm
                if _esm.stream_is_live:
                    await flash("Cannot clear the admin playlist while the stream is live.", "error")
                else:
                    removed = await db.get_all_exp_radio_songs(
                        active_only=True, source="admin"
                    )
                    count = await db.delete_all_exp_radio_songs(source="admin")
                    protected_songs = await db.get_all_exp_radio_songs(active_only=True)
                    for song in removed:
                        cleanup_exp_radio_song_files(
                            EXP_RADIO_DIR, song, protected_songs
                        )
                    await flash(f"Admin playlist cleared — {count} song(s) removed.", "success")

            elif action == "reanalyze_whisper":
                import bot.exp_stream_manager as _esm
                if _esm.stream_is_live:
                    await flash("Cannot run Whisper while the stream is live.", "error")
                else:
                    from bot.exp_radio_worker import process_exp_song
                    songs_all = await db.get_all_exp_radio_songs(active_only=True)
                    queued = 0
                    for s in songs_all:
                        if not s.get("mp3_filename"):
                            continue  # MP3 never finished uploading — skip
                        # Drop stale ASS so the player won't pick it up mid-rebuild
                        await db.update_exp_radio_song(
                            s["id"], analysis_status="processing", ass_filename=None,
                        )
                        asyncio.create_task(
                            process_exp_song(db, s["id"], EXP_RADIO_DIR, bot=bot)
                        )
                        queued += 1
                    await flash(
                        f"Queued {queued} song(s) for re-analysis. "
                        "Whisper runs in the background — refresh the page to see status updates.",
                        "success",
                    )

            elif action == "check_durations":
                import bot.exp_stream_manager as _esm
                if _esm.stream_is_live:
                    await flash("Cannot check durations while the stream is live.", "error")
                else:
                    checked, corrected, skipped, errors = await _check_exp_radio_durations()
                    msg = (
                        f"Duration check complete: {checked} checked, "
                        f"{corrected} corrected, {skipped} skipped"
                    )
                    if errors:
                        msg += f", {errors} error(s). Check the Live Log."
                    else:
                        msg += ". Details are in the Live Log."
                    await flash(msg, "error" if errors else "success")

            elif action == "rescrape_metadata_one":
                from bot.exp_radio_worker import scrape_suno
                from bot.exp_stream_manager import log_event
                sid = int(form.get("song_id", "0") or 0)
                s = await db.get_exp_radio_song(sid) if sid else None
                if not s or not s.get("suno_uuid"):
                    await flash("Song not found.", "error")
                else:
                    uuid = s["suno_uuid"]
                    log_event(f"Refreshing metadata for #{sid} (uuid={uuid})…", prefix="[meta]")
                    meta = await scrape_suno(uuid)
                    fields = {}
                    if meta.get("title"):     fields["title"]     = meta["title"]
                    if meta.get("artist"):    fields["artist"]    = meta["artist"]
                    if meta.get("video_url"): fields["video_url"] = meta["video_url"]
                    if meta.get("cover_url"): fields["cover_url"] = meta["cover_url"]
                    if fields:
                        await db.update_exp_radio_song(sid, **fields)
                        for ext in (".jpg", ".mp4"):
                            cached = os.path.join(EXP_RADIO_DIR, "cover_cache", f"{uuid}{ext}")
                            if os.path.exists(cached):
                                try: os.remove(cached)
                                except Exception: pass
                        log_event(
                            f"Refreshed metadata for #{sid} ({fields.get('title') or s.get('title')!r}) "
                            f"— updated: {', '.join(sorted(fields.keys()))}",
                            prefix="[meta]",
                        )
                        await flash(f"Refreshed metadata for “{fields.get('title') or s.get('title')}”.", "success")
                    else:
                        log_event(
                            f"No usable metadata returned for #{sid} (uuid={uuid})",
                            level="error", prefix="[meta]",
                        )
                        await flash("Nothing to update — Suno returned no usable metadata.", "warning")

            elif action == "renormalize_cover_one":
                sid = int(form.get("song_id", "0") or 0)
                s = await db.get_exp_radio_song(sid) if sid else None
                if not s:
                    await flash("Song not found.", "error")
                else:
                    ok, msg = await exp_stream_manager.renormalize_cover(s)
                    await flash(
                        f"{'✅' if ok else '❌'} {s.get('title') or sid}: {msg}",
                        "success" if ok else "error",
                    )

            elif action == "renormalize_cover_all":
                from bot.exp_stream_manager import log_event
                songs_all = await db.get_all_exp_radio_songs(active_only=True)
                log_event(
                    f"Bulk cover normalize started for {len(songs_all)} song(s)\u2026",
                    prefix="[cover]",
                )
                # Run in the background so the HTTP request returns immediately
                # (each individual normalization spawns ffprobe + ffmpeg and
                # would otherwise block the request for tens of seconds).
                async def _bulk_renorm(songs):
                    ok_n = fail_n = skip_n = 0
                    for s in songs:
                        title = s.get("title") or s.get("id")
                        try:
                            ok, msg = await exp_stream_manager.renormalize_cover(s)
                        except Exception as e:
                            log_event(
                                f"  #{s.get('id')} ({title!r}) error: {e}",
                                level="error", prefix="[cover]",
                            )
                            fail_n += 1
                            continue
                        if ok:
                            log_event(
                                f"  #{s.get('id')} ({title!r}): {msg}",
                                prefix="[cover]",
                            )
                            ok_n += 1
                        else:
                            # 'No usable cover URL' / 'failed' — distinguish
                            level = "error" if "fail" in msg.lower() else "info"
                            log_event(
                                f"  #{s.get('id')} ({title!r}): {msg}",
                                level=level, prefix="[cover]",
                            )
                            if level == "error":
                                fail_n += 1
                            else:
                                skip_n += 1
                    log_event(
                        f"Bulk cover normalize done: {ok_n} ok, {fail_n} failed, "
                        f"{skip_n} skipped (no cached cover).",
                        prefix="[cover]",
                    )
                asyncio.create_task(_bulk_renorm(songs_all))
                await flash(
                    f"Queued cover normalization for {len(songs_all)} song(s) \u2014 "
                    "watch the Live Log for progress.",
                    "success",
                )

            elif action == "approve_moderation_one":
                sid = int(form.get("song_id", "0") or 0)
                s = await db.get_exp_radio_song(sid) if sid else None
                if not s:
                    await flash("Song not found.", "error")
                else:
                    await db.update_exp_radio_song(
                        sid,
                        moderation_status="approved",
                        moderation_at=time.time(),
                    )
                    await flash(
                        f"✅ Approved “{s.get('title') or sid}” for the stream playlist.",
                        "success",
                    )

            elif action == "approve_admin_whisper_bypass":
                from bot.exp_stream_manager import log_event
                sid = int(form.get("song_id", "0") or 0)
                s = await db.get_exp_radio_song(sid) if sid else None
                if not s:
                    await flash("Song not found.", "error")
                elif s.get("playlist_source") != "admin":
                    await flash("This bypass is only available for admin playlist songs.", "error")
                elif not s.get("mp3_filename"):
                    await flash("Cannot approve yet: MP3 download is not complete.", "error")
                else:
                    await db.update_exp_radio_song(
                        sid,
                        analysis_status="done",
                        word_timestamps="[]",
                        ass_filename=None,
                        moderation_status="approved",
                        moderation_reason="Admin playlist: Whisper transcript bypassed.",
                        moderation_at=time.time(),
                    )
                    log_event(
                        f"Admin bypassed Whisper transcript for #{sid} "
                        f"({s.get('title') or s.get('suno_url') or sid!r}); marked stream-ready.",
                        prefix="[admin-pl]",
                    )
                    await flash(
                        f"✅ Marked “{s.get('title') or sid}” as ready without Whisper transcript.",
                        "success",
                    )

            elif action == "remoderate_one":
                import bot.exp_stream_manager as _esm
                if _esm.stream_is_live:
                    await flash("Cannot run LLM moderation while the stream is live.", "error")
                else:
                    from bot.exp_moderation import moderate_lyrics
                    from bot.llm import OllamaClient
                    from config import Config
                    sid = int(form.get("song_id", "0") or 0)
                    s = await db.get_exp_radio_song(sid) if sid else None
                    if not s or not s.get("lyrics"):
                        await flash("Song not found or has no lyrics yet.", "error")
                    else:
                        from bot.exp_stream_manager import log_event
                        async def _run_remoderation(song_id, snap):
                            title_s = snap.get("title") or f"#{song_id}"
                            try:
                                client = OllamaClient(
                                    base_url=Config.OLLAMA_URL,
                                    model=Config.LLM_MODEL,
                                    timeout=Config.LLM_REQUEST_TIMEOUT,
                                )
                                log_event(
                                    f"Re-moderation start for #{song_id} ({title_s!r})",
                                    prefix="[mod]",
                                )
                                verdict = await moderate_lyrics(
                                    client,
                                    lyrics=snap.get("lyrics") or "",
                                    title=snap.get("title") or "",
                                    artist=snap.get("artist") or "",
                                )
                                await db.update_exp_radio_song(
                                    song_id,
                                    moderation_status=verdict["status"],
                                    moderation_reason=verdict.get("reason") or "",
                                    moderation_at=time.time(),
                                )
                                level = "error" if verdict["status"] in ("flagged", "pending") else "info"
                                summary = (
                                    f"Re-moderation #{song_id} → {verdict['status']}"
                                    f"{' (translated)' if verdict.get('translated') else ''}"
                                )
                                if verdict.get("reason"):
                                    summary += f": {verdict['reason']}"
                                log_event(summary, level=level, prefix="[mod]")
                            except Exception as e:
                                log_event(
                                    f"Re-moderation error #{song_id}: {e}",
                                    level="error", prefix="[mod]",
                                )
                                await db.update_exp_radio_song(
                                    song_id,
                                    moderation_status="pending",
                                    moderation_reason=f"Re-moderation error: {e!s}",
                                    moderation_at=time.time(),
                                )
                        await db.update_exp_radio_song(
                            sid,
                            moderation_status="pending",
                            moderation_reason="Re-moderation in progress…",
                            moderation_at=time.time(),
                        )
                        asyncio.create_task(_run_remoderation(sid, s))
                        await flash(
                            f"Queued “{s.get('title') or sid}” for LLM re-moderation. "
                            "Refresh the page in a few seconds.",
                            "success",
                        )

            elif action == "reanalyze_whisper_one":
                import bot.exp_stream_manager as _esm
                if _esm.stream_is_live:
                    await flash("Cannot run Whisper while the stream is live.", "error")
                else:
                    from bot.exp_radio_worker import process_exp_song
                    sid = int(form.get("song_id", "0") or 0)
                    s = await db.get_exp_radio_song(sid) if sid else None
                    if not s or not s.get("mp3_filename"):
                        await flash("Song not found or MP3 missing.", "error")
                    else:
                        await db.update_exp_radio_song(
                            sid, analysis_status="processing", ass_filename=None,
                        )
                        asyncio.create_task(
                            process_exp_song(db, sid, EXP_RADIO_DIR, bot=bot)
                        )
                        await flash(
                            f"Queued “{s.get('title') or sid}” for Whisper re-analysis. "
                            "Refresh the page for status.",
                            "success",
                        )

            elif action == "rescrape_metadata":
                from bot.exp_radio_worker import scrape_suno
                from bot.exp_stream_manager import log_event
                songs_all = await db.get_all_exp_radio_songs(active_only=True)
                log_event(
                    f"Bulk metadata refresh started for {len(songs_all)} song(s)…",
                    prefix="[meta]",
                )
                updated = 0
                for s in songs_all:
                    uuid = s.get("suno_uuid")
                    if not uuid:
                        continue
                    meta = await scrape_suno(uuid)
                    real_uuid = meta.get("real_uuid") or uuid
                    fields = {}
                    if meta.get("title"):     fields["title"]     = meta["title"]
                    if meta.get("artist"):    fields["artist"]    = meta["artist"]
                    if meta.get("video_url"): fields["video_url"] = meta["video_url"]
                    if meta.get("cover_url"): fields["cover_url"] = meta["cover_url"]
                    if fields:
                        await db.update_exp_radio_song(s["id"], **fields)
                        updated += 1
                        log_event(
                            f"  #{s['id']} ({fields.get('title') or s.get('title')!r}) updated: "
                            f"{', '.join(sorted(fields.keys()))}",
                            prefix="[meta]",
                        )
                        # Invalidate cached media so next stream run downloads fresh
                        for ext in (".jpg", ".mp4"):
                            cached = os.path.join(EXP_RADIO_DIR, "cover_cache", f"{uuid}{ext}")
                            if os.path.exists(cached):
                                try: os.remove(cached)
                                except Exception: pass
                log_event(f"Bulk metadata refresh done: {updated}/{len(songs_all)} updated.", prefix="[meta]")
                await flash(f"Refreshed metadata for {updated} song(s).", "success")

            elif action == "save_stream_key":
                key = form.get("exp_twitch_key", "").strip()
                await db.set_setting("exp_radio_twitch_key", key)
                await flash("Stream key saved.", "success")

            elif action == "save_exp_settings":
                enabled_v = "on" if form.get("exp_radio_enabled") else "off"
                ch1 = form.get("exp_post_channel_1_id", "").strip()
                ch2 = form.get("exp_post_channel_2_id", "").strip()
                ch3 = form.get("exp_post_channel_3_id", "").strip()
                expiry_ch = form.get("exp_expiry_channel_id", "").strip()
                announcement_ch = form.get("exp_announcement_channel_id", "").strip()
                announcement_msg = (form.get("exp_announcement_message") or "").strip()
                stream_url_v = form.get("exp_stream_url", "").strip()
                moderation_en = "on" if form.get("exp_moderation_enabled") else "off"
                loop_mode_v = form.get("exp_loop_mode", "reshuffle").strip()
                if loop_mode_v not in ("stop", "reshuffle"):
                    loop_mode_v = "reshuffle"
                # Auto-start scheduler
                sched_en = "on" if form.get("exp_schedule_enabled") else "off"
                if enabled_v != "on":
                    sched_en = "off"
                # Day checkboxes named exp_schedule_day_0..6 (Mon=0 .. Sun=6)
                sched_days = ",".join(
                    str(i) for i in range(7) if form.get(f"exp_schedule_day_{i}")
                )
                sched_time = (form.get("exp_schedule_time") or "").strip()
                # Validate HH:MM
                import re as _re
                if not _re.match(r"^\d{1,2}:\d{2}$", sched_time):
                    sched_time = ""
                await db.set_setting("exp_radio_enabled", enabled_v)
                await db.set_setting("exp_radio_post_channel_1_id", ch1)
                await db.set_setting("exp_radio_post_channel_2_id", ch2)
                await db.set_setting("exp_radio_post_channel_3_id", ch3)
                await db.set_setting("exp_radio_expiry_channel_id", expiry_ch)
                await db.set_setting("exp_radio_announcement_channel_id", announcement_ch)
                await db.set_setting("exp_radio_announcement_message", announcement_msg)
                progress_overlay_en = "on" if form.get("exp_progress_overlay") else "off"
                ravenveil_early_boost_en = "on" if form.get("exp_ravenveil_early_boost") else "off"
                try:
                    max_per_user_v = max(1, min(20, int(form.get("exp_max_per_user", "4") or "4")))
                except (ValueError, TypeError):
                    max_per_user_v = 4
                try:
                    expiry_days_v = int(form.get("exp_expiry_days", "14") or "14")
                except (ValueError, TypeError):
                    expiry_days_v = 14
                if expiry_days_v not in (7, 14):
                    expiry_days_v = 14
                try:
                    video_bitrate_v = int(form.get("exp_video_bitrate_kbps", "2500") or "2500")
                except (ValueError, TypeError):
                    video_bitrate_v = 2500
                if video_bitrate_v not in (1800, 2000, 2500):
                    video_bitrate_v = 2500
                await db.set_setting("exp_radio_max_per_user", str(max_per_user_v))
                await db.set_setting("exp_radio_expiry_days", str(expiry_days_v))
                await db.set_setting("exp_radio_video_bitrate_kbps", str(video_bitrate_v))
                await db.set_setting("exp_radio_moderation_enabled", moderation_en)
                await db.set_setting("exp_radio_loop_mode", loop_mode_v)
                await db.set_setting("exp_radio_schedule_enabled", sched_en)
                await db.set_setting("exp_radio_schedule_days", sched_days)
                await db.set_setting("exp_radio_schedule_time", sched_time)
                await db.set_setting("exp_radio_progress_overlay", progress_overlay_en)
                disclaimer_enabled = "on" if form.get("exp_disclaimer_enabled") else "off"
                disclaimer_text = (form.get("exp_disclaimer_text") or "").strip()[:2000]
                await db.set_setting("exp_radio_disclaimer_enabled", disclaimer_enabled)
                await db.set_setting("exp_radio_disclaimer_text", disclaimer_text)
                await db.set_setting("exp_radio_ravenveil_early_boost", ravenveil_early_boost_en)
                active_pl_v = form.get("exp_active_playlist", "submission")
                if active_pl_v not in ("submission", "admin", "both"):
                    active_pl_v = "submission"
                await db.set_setting("exp_radio_active_playlist", active_pl_v)
                intro_en = "on" if form.get("exp_intro_enabled") else "off"
                outro_en = "on" if form.get("exp_outro_enabled") else "off"
                intro_selection = (form.get("exp_intro_selection") or "random").strip()
                outro_selection = (form.get("exp_outro_selection") or "random").strip()
                await db.set_setting("exp_radio_intro_enabled", intro_en)
                await db.set_setting("exp_radio_outro_enabled", outro_en)
                await db.set_setting("exp_radio_intro_selection", intro_selection)
                await db.set_setting("exp_radio_outro_selection", outro_selection)
                if stream_url_v:
                    await db.set_setting("exp_radio_stream_url", stream_url_v)
                if enabled_v != "on" and exp_stream_manager.is_running:
                    await exp_stream_manager.stop()
                await flash(
                    "Settings saved. Experimental Radio is disabled."
                    if enabled_v != "on" else "Settings saved.",
                    "success",
                )

            elif action == "save_exp_twitch_settings":
                exp_tw_cid = form.get("exp_twitch_client_id", "").strip()
                exp_tw_sec = form.get("exp_twitch_client_secret", "").strip()
                exp_tw_rt  = form.get("exp_twitch_refresh_token", "").strip()
                exp_tw_bc  = form.get("exp_twitch_broadcaster_login", "").strip()
                chat_en    = "on" if form.get("exp_twitch_chat_enabled") else "off"
                if exp_tw_cid:
                    await db.set_setting("exp_radio_twitch_client_id", exp_tw_cid)
                if exp_tw_sec and not exp_tw_sec.startswith("****"):
                    await db.set_setting("exp_radio_twitch_client_secret", exp_tw_sec)
                if exp_tw_rt and not exp_tw_rt.startswith("****"):
                    await db.set_setting("exp_radio_twitch_refresh_token", exp_tw_rt)
                    await db.set_setting("exp_radio_twitch_bot_login", "")
                    await db.set_setting("exp_radio_twitch_bot_user_id", "")
                if exp_tw_bc:
                    bn = exp_tw_bc.strip().rstrip("/").lstrip("#").lower()
                    if "twitch.tv/" in bn:
                        bn = bn.split("twitch.tv/", 1)[1].split("/")[0]
                    await db.set_setting("exp_radio_twitch_broadcaster_login", bn)
                await db.set_setting("exp_radio_twitch_chat_enabled", chat_en)
                await flash("Twitch Chat Bot settings saved.", "success")

            elif action == "post_exp_stream_url":
                ch_id = form.get("post_channel_id_select", "")
                exp_stream_url_v = await db.get_setting("exp_radio_stream_url") or ""
                if not exp_stream_url_v:
                    await flash("No stream URL configured. Open Settings to add one.", "danger")
                else:
                    ok, name_or_err = await _post_exp_stream_announcement(ch_id, exp_stream_url_v)
                    if ok:
                        await flash(f"Stream link posted to #{name_or_err}.", "success")
                    else:
                        await flash(f"Could not post: {name_or_err}", "danger")

            elif action == "post_exp_announcement":
                ch_id = await db.get_setting("exp_radio_announcement_channel_id") or ""
                message_text = (await db.get_setting("exp_radio_announcement_message") or "").strip()
                if not ch_id:
                    await flash("No announcement channel configured. Open Settings to add one.", "danger")
                elif not message_text:
                    await flash("No announcement message configured. Open Settings to add one.", "danger")
                else:
                    guild = get_guild()
                    channel = None
                    if guild:
                        try:
                            ch_int = int(ch_id)
                            channel = guild.get_channel(ch_int) or guild.get_thread(ch_int)
                        except Exception:
                            channel = None
                    if not channel:
                        await flash("Announcement channel not found.", "danger")
                    else:
                        try:
                            await channel.send(message_text)
                            await flash(f"Announcement posted to #{channel.name}.", "success")
                        except Exception as e:
                            await flash(f"Could not post announcement: {e}", "danger")

            elif action == "upload_background":
                bg_file = files.get("bg_file")
                if bg_file and bg_file.filename:
                    ext = bg_file.filename.rsplit(".", 1)[-1].lower()
                    if ext in ("jpg", "jpeg", "png", "mp4", "webm"):
                        bg_type = "video" if ext in ("mp4", "webm") else "image"
                        fn = f"exp_bg_{_uuid.uuid4().hex}.{ext}"
                        await bg_file.save(os.path.join(EXP_RADIO_DIR, "assets", fn))
                        old = await db.get_setting("exp_radio_bg_filename")
                        if old:
                            old_path = os.path.join(EXP_RADIO_DIR, "assets", old)
                            if os.path.exists(old_path):
                                os.remove(old_path)
                        await db.set_setting("exp_radio_bg_filename", fn)
                        await db.set_setting("exp_radio_bg_type", bg_type)
                        await flash("Background uploaded.", "success")
                    else:
                        await flash("Unsupported file type.", "danger")

            elif action == "upload_loop_video":
                import json as _jl
                lv_file = files.get("lv_file")
                if lv_file and lv_file.filename:
                    ext = lv_file.filename.rsplit(".", 1)[-1].lower()
                    if ext in ("mp4", "webm"):
                        fn = f"exp_loop_{_uuid.uuid4().hex}.{ext}"
                        label = form.get("lv_label", "").strip() or lv_file.filename
                        await lv_file.save(os.path.join(EXP_RADIO_DIR, "assets", fn))
                        raw = await db.get_setting("exp_radio_loop_videos") or "[]"
                        vids = _jl.loads(raw) if raw else []
                        vids.append({"filename": fn, "label": label})
                        await db.set_setting("exp_radio_loop_videos", _jl.dumps(vids))
                        # If this is the first video, auto-select it
                        if len(vids) == 1:
                            await db.set_setting("exp_radio_loop_selection", fn)
                        await flash("Loop video uploaded.", "success")
                    else:
                        await flash("Only MP4/WebM supported.", "danger")

            elif action == "save_exp_loop_source":
                obs_overlay_enabled_v = "on" if form.get("exp_obs_overlay_enabled") else "off"
                loop_rtmp_key_v = (form.get("exp_loop_rtmp_key") or "").strip()
                try:
                    obs_overlay_fps_v = int(form.get("exp_obs_overlay_fps", "20") or "20")
                except (TypeError, ValueError):
                    obs_overlay_fps_v = 20
                if obs_overlay_fps_v not in (15, 20, 24):
                    obs_overlay_fps_v = 20
                if obs_overlay_enabled_v == "on" and not loop_rtmp_key_v:
                    import secrets as _secrets
                    loop_rtmp_key_v = _secrets.token_urlsafe(18)
                await db.set_setting("exp_radio_obs_overlay_enabled", obs_overlay_enabled_v)
                await db.set_setting("exp_radio_obs_overlay_fps", str(obs_overlay_fps_v))
                await db.set_setting("exp_radio_loop_source", "local")
                await db.set_setting("exp_radio_loop_rtmp_key", loop_rtmp_key_v)
                await flash("OBS overlay settings saved.", "success")

            elif action == "delete_loop_video":
                import json as _jl
                del_fn = form.get("loop_filename", "")
                if del_fn:
                    raw = await db.get_setting("exp_radio_loop_videos") or "[]"
                    vids = _jl.loads(raw) if raw else []
                    vids = [v for v in vids if v.get("filename") != del_fn]
                    await db.set_setting("exp_radio_loop_videos", _jl.dumps(vids))
                    del_path = os.path.join(EXP_RADIO_DIR, "assets", del_fn)
                    if os.path.exists(del_path):
                        os.remove(del_path)
                    # If the deleted video was selected, fall back to shuffle
                    sel = await db.get_setting("exp_radio_loop_selection") or "shuffle"
                    if sel == del_fn:
                        await db.set_setting("exp_radio_loop_selection", "shuffle")
                    await flash("Loop video removed.", "success")

            elif action == "set_loop_selection":
                sel = form.get("loop_selection", "shuffle")
                await db.set_setting("exp_radio_loop_selection", sel)
                loop_selection_labels = {
                    "shuffle": "Shuffle",
                    "concat_all": "Concatenate all videos",
                    "concat_all_random": "Concatenate all videos in random order",
                }
                await flash(f"Loop video selection: {loop_selection_labels.get(sel, sel)}", "success")

            return redirect(request.url)

        songs = await db.get_all_exp_radio_songs(active_only=True, source="submission")
        # Enrich each song with parsed analysis info for the admin UI:
        # word_count, coverage span and a transcript preview reconstructed
        # from the stored word_timestamps JSON.
        import json as _json
        for s in songs:
            wt_raw = s.get("word_timestamps")
            s["word_count"] = 0
            s["transcript_preview"] = ""
            s["transcript_span"] = None
            if wt_raw:
                try:
                    wt = _json.loads(wt_raw) if isinstance(wt_raw, str) else wt_raw
                    if isinstance(wt, list) and wt:
                        s["word_count"] = len(wt)
                        s["transcript_span"] = (
                            float(wt[0].get("start", 0)),
                            float(wt[-1].get("end", 0)),
                        )
                        # Reconstructed transcript with [mm:ss] markers every ~10s
                        parts, last_mark = [], -10.0
                        for w in wt:
                            st = float(w.get("start", 0))
                            if st - last_mark >= 10:
                                m, sec = divmod(int(st), 60)
                                parts.append(f"\n[{m}:{sec:02d}] ")
                                last_mark = st
                            parts.append(w.get("word", ""))
                            parts.append(" ")
                        s["transcript_preview"] = "".join(parts).strip()
                except Exception:
                    pass
        status = await exp_stream_manager.get_status()
        masked_key = "*" * 20 if await db.get_setting("exp_radio_twitch_key") else ""
        bg_filename  = await db.get_setting("exp_radio_bg_filename") or ""
        import json as _jlv
        _loop_raw = await db.get_setting("exp_radio_loop_videos") or "[]"
        loop_videos = _jlv.loads(_loop_raw) if _loop_raw else []
        # Auto-migrate legacy single-video setting
        if not loop_videos:
            _old_lv = await db.get_setting("exp_radio_loop_filename") or ""
            if _old_lv:
                loop_videos = [{"filename": _old_lv, "label": _old_lv}]
                await db.set_setting("exp_radio_loop_videos", _jlv.dumps(loop_videos))
                await db.set_setting("exp_radio_loop_selection", _old_lv)
                await db.set_setting("exp_radio_loop_filename", "")
        loop_selection = await db.get_setting("exp_radio_loop_selection") or "shuffle"
        exp_radio_enabled = await db.get_setting("exp_radio_enabled") or "on"
        exp_stream_url = await db.get_setting("exp_radio_stream_url") or ""
        exp_post_channel_1_id = await db.get_setting("exp_radio_post_channel_1_id") or ""
        exp_post_channel_2_id = await db.get_setting("exp_radio_post_channel_2_id") or ""
        exp_post_channel_3_id = await db.get_setting("exp_radio_post_channel_3_id") or ""
        exp_expiry_channel_id = await db.get_setting("exp_radio_expiry_channel_id") or ""
        exp_announcement_channel_id = await db.get_setting("exp_radio_announcement_channel_id") or ""
        exp_announcement_message = await db.get_setting("exp_radio_announcement_message") or ""
        exp_twitch_chat_enabled = await db.get_setting("exp_radio_twitch_chat_enabled") or "off"
        exp_moderation_enabled  = await db.get_setting("exp_radio_moderation_enabled") or "off"
        exp_loop_mode           = await db.get_setting("exp_radio_loop_mode") or "reshuffle"
        exp_progress_overlay    = await db.get_setting("exp_radio_progress_overlay") or "off"
        exp_disclaimer_enabled  = await db.get_setting("exp_radio_disclaimer_enabled") or "off"
        exp_disclaimer_text     = await db.get_setting("exp_radio_disclaimer_text") or ""
        exp_ravenveil_early_boost = await db.get_setting("exp_radio_ravenveil_early_boost") or "off"
        exp_max_per_user        = int(await db.get_setting("exp_radio_max_per_user") or "4")
        exp_expiry_days         = int(await db.get_setting("exp_radio_expiry_days") or "14")
        if exp_expiry_days not in (7, 14):
            exp_expiry_days = 14
        try:
            exp_video_bitrate_kbps = int(await db.get_setting("exp_radio_video_bitrate_kbps") or "2500")
        except (TypeError, ValueError):
            exp_video_bitrate_kbps = 2500
        if exp_video_bitrate_kbps not in (1800, 2000, 2500):
            exp_video_bitrate_kbps = 2500
        exp_loop_source        = await db.get_setting("exp_radio_loop_source") or "local"
        if exp_loop_source not in ("local", "rtmp"):
            exp_loop_source = "local"
        exp_obs_overlay_enabled = await db.get_setting("exp_radio_obs_overlay_enabled") or "off"
        if exp_loop_source == "rtmp":
            exp_obs_overlay_enabled = "on"
        try:
            exp_obs_overlay_fps = int(await db.get_setting("exp_radio_obs_overlay_fps") or "20")
        except (TypeError, ValueError):
            exp_obs_overlay_fps = 20
        if exp_obs_overlay_fps not in (15, 20, 24):
            exp_obs_overlay_fps = 20
        exp_loop_rtmp_key      = await db.get_setting("exp_radio_loop_rtmp_key") or ""
        exp_schedule_enabled    = await db.get_setting("exp_radio_schedule_enabled") or "off"
        exp_schedule_days       = await db.get_setting("exp_radio_schedule_days") or ""
        exp_schedule_time       = await db.get_setting("exp_radio_schedule_time") or ""
        exp_schedule_days_set   = set(d for d in exp_schedule_days.split(",") if d)
        exp_active_playlist     = await db.get_setting("exp_radio_active_playlist") or "submission"
        exp_intro_enabled       = await db.get_setting("exp_radio_intro_enabled") or "off"
        exp_outro_enabled       = await db.get_setting("exp_radio_outro_enabled") or "off"
        exp_intro_selection     = await db.get_setting("exp_radio_intro_selection") or "random"
        exp_outro_selection     = await db.get_setting("exp_radio_outro_selection") or "random"
        exp_tw_client_id = await db.get_setting("exp_radio_twitch_client_id") or ""
        _exp_tw_secret   = await db.get_setting("exp_radio_twitch_client_secret") or ""
        _exp_tw_refresh  = await db.get_setting("exp_radio_twitch_refresh_token") or ""
        exp_tw_secret_masked  = f"****{_exp_tw_secret[-4:]}"  if len(_exp_tw_secret)  > 4 else ""
        exp_tw_refresh_masked = f"****{_exp_tw_refresh[-4:]}" if len(_exp_tw_refresh) > 4 else ""
        exp_tw_broadcaster = await db.get_setting("exp_radio_twitch_broadcaster_login") or ""
        exp_tw_bot_login   = await db.get_setting("exp_radio_twitch_bot_login") or ""
        # Quick scope check for UI badge (uses cached access token if available)
        exp_tw_scopes_ok = False
        try:
            import aiohttp as _ah
            _rt = await db.get_setting("exp_radio_twitch_refresh_token") or ""
            _cid = await db.get_setting("exp_radio_twitch_client_id") or ""
            _cs  = await db.get_setting("exp_radio_twitch_client_secret") or ""
            if _rt and _cid and _cs:
                async with _ah.ClientSession() as _hs:
                    async with _hs.post(
                        "https://id.twitch.tv/oauth2/token",
                        data={"client_id": _cid, "client_secret": _cs,
                              "grant_type": "refresh_token", "refresh_token": _rt},
                        timeout=_ah.ClientTimeout(total=8),
                    ) as _hr:
                        _hd = await _hr.json()
                        _tok = _hd.get("access_token", "")
                    if _tok:
                        async with _hs.get(
                            "https://id.twitch.tv/oauth2/validate",
                            headers={"Authorization": f"OAuth {_tok}"},
                            timeout=_ah.ClientTimeout(total=8),
                        ) as _hr:
                            _hv = await _hr.json()
                            exp_tw_scopes_ok = "chat:read" in (_hv.get("scopes") or [])
        except Exception:
            pass
        exp_guild = get_guild()
        exp_text_channels = []
        if exp_guild:
            for _ch in sorted(exp_guild.text_channels, key=lambda c: c.position):
                exp_text_channels.append({"id": _ch.id, "name": _ch.name})
        admin_songs = await db.get_all_exp_radio_songs(active_only=False, source="admin")
        intro_songs = await db.get_all_exp_radio_songs(active_only=False, source="intro")
        outro_songs = await db.get_all_exp_radio_songs(active_only=False, source="outro")
        active_intro_songs = [s for s in intro_songs if s.get("active") and s.get("analysis_status") == "done" and s.get("mp3_filename")]
        active_outro_songs = [s for s in outro_songs if s.get("active") and s.get("analysis_status") == "done" and s.get("mp3_filename")]
        return await render_template(
            "exp_radio.html",
            songs=songs, status=status,
            exp_radio_enabled=exp_radio_enabled,
            admin_songs=admin_songs,
            intro_songs=intro_songs,
            outro_songs=outro_songs,
            exp_active_playlist=exp_active_playlist,
            exp_intro_enabled=exp_intro_enabled,
            exp_outro_enabled=exp_outro_enabled,
            exp_intro_selection=exp_intro_selection,
            exp_outro_selection=exp_outro_selection,
            active_intro_songs=active_intro_songs,
            active_outro_songs=active_outro_songs,
            masked_key=masked_key,
            bg_filename=bg_filename,
            loop_videos=loop_videos, loop_selection=loop_selection,
            exp_stream_url=exp_stream_url,
            exp_post_channel_1_id=exp_post_channel_1_id,
            exp_post_channel_2_id=exp_post_channel_2_id,
            exp_post_channel_3_id=exp_post_channel_3_id,
            exp_expiry_channel_id=exp_expiry_channel_id,
            exp_announcement_channel_id=exp_announcement_channel_id,
            exp_announcement_message=exp_announcement_message,
            exp_twitch_chat_enabled=exp_twitch_chat_enabled,
            exp_moderation_enabled=exp_moderation_enabled,
            exp_loop_mode=exp_loop_mode,
            exp_loop_source=exp_loop_source,
            exp_obs_overlay_enabled=exp_obs_overlay_enabled,
            exp_obs_overlay_fps=exp_obs_overlay_fps,
            exp_loop_rtmp_key=exp_loop_rtmp_key,
            exp_progress_overlay=exp_progress_overlay,
            exp_disclaimer_enabled=exp_disclaimer_enabled,
            exp_disclaimer_text=exp_disclaimer_text,
            exp_ravenveil_early_boost=exp_ravenveil_early_boost,
            exp_max_per_user=exp_max_per_user,
            exp_expiry_days=exp_expiry_days,
            exp_video_bitrate_kbps=exp_video_bitrate_kbps,
            exp_schedule_enabled=exp_schedule_enabled,
            exp_schedule_time=exp_schedule_time,
            exp_schedule_days_set=exp_schedule_days_set,
            exp_tw_client_id=exp_tw_client_id,
            exp_tw_secret_masked=exp_tw_secret_masked,
            exp_tw_refresh_masked=exp_tw_refresh_masked,
            exp_tw_broadcaster=exp_tw_broadcaster,
            exp_tw_bot_login=exp_tw_bot_login,
            exp_tw_scopes_ok=exp_tw_scopes_ok,
            text_channels=exp_text_channels,
        )

    @app.route("/exp-radio/upload/<token>/resolve")
    async def exp_radio_upload_resolve(token: str):
        """Resolve the real Suno UUID (full UUID) for the upload page JS."""
        from quart import jsonify
        if (await db.get_setting("exp_radio_enabled") or "on") != "on":
            return jsonify({"error": "Experimental Radio is disabled."}), 503
        import aiohttp, re as _re
        song = await db.get_exp_radio_song_by_token(token)
        if not song:
            return jsonify({"error": "Invalid token"}), 403
        suno_uuid = song["suno_uuid"]
        # Try to get the real full UUID by scraping the Suno song page
        is_full_uuid = bool(_re.match(
            r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$', suno_uuid
        ))
        if is_full_uuid:
            return jsonify({"real_uuid": suno_uuid})
        # Short ID — scrape the song page to get the real UUID
        url = f"https://suno.com/s/{suno_uuid}"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(url, headers=headers,
                                    timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return jsonify({"error": f"Suno returned {resp.status}"}), 502
                    html = await resp.text()
            m = _re.search(
                r'"id"\s*:\s*"([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})"',
                html,
            )
            if not m:
                m = _re.search(
                    r'song/([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})',
                    html,
                )
            real_uuid = m.group(1) if m else None
            if not real_uuid:
                return jsonify({"error": "Could not resolve real UUID from Suno page"}), 502
            return jsonify({"real_uuid": real_uuid})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/exp-radio/upload/<token>")
    async def exp_radio_upload_page(token: str):
        """Public page (no login) — browser fetches MP3 from Suno CDN and POSTs it here."""
        if (await db.get_setting("exp_radio_enabled") or "on") != "on":
            return await render_template(
                "exp_radio_upload.html",
                error="Experimental Radio is currently disabled.",
            ), 503
        from bot.exp_stream_manager import is_submissions_locked as _is_locked
        song = await db.get_exp_radio_song_by_token(token)
        if not song:
            return await render_template("exp_radio_upload.html", error="Link invalid or expired.")
        if song.get("mp3_filename"):
            return await render_template("exp_radio_upload.html", done=True,
                                         title=song.get("title") or "Your song")
        _locked, _lock_reason = await _is_locked(db)
        if _locked:
            return await render_template("exp_radio_upload.html", stream_live=True)
        expiry_days = 14
        try:
            expiry_days = max(1, round((float(song.get("expires_at") or 0) - time.time()) / 86400))
        except Exception:
            pass
        return await render_template("exp_radio_upload.html", song=song, token=token, expiry_days=expiry_days)

    @app.route("/exp-radio/upload/<token>", methods=["POST"])
    async def exp_radio_upload_receive(token: str):
        """Receive the MP3 posted by the browser upload page."""
        from quart import jsonify
        if (await db.get_setting("exp_radio_enabled") or "on") != "on":
            return jsonify({"ok": False, "error": "Experimental Radio is disabled."}), 503
        import bot.exp_stream_manager as _esm
        song = await db.get_exp_radio_song_by_token(token)
        if not song:
            return jsonify({"ok": False, "error": "Invalid token"}), 403
        if song.get("mp3_filename"):
            return jsonify({"ok": True, "already": True})

        # Reject uploads while the stream is live or within 60 min of
        # a scheduled start — Whisper + LLM compete with FFmpeg for CPU/RAM.
        from bot.exp_stream_manager import is_submissions_locked as _is_locked
        _locked, _lock_reason = await _is_locked(db)
        if _locked:
            return jsonify({
                "ok": False,
                "error": "Submissions are currently closed (stream live or starting soon). Please try again later.",
            }), 503

        files = await request.files
        mp3_file = files.get("mp3")
        if not mp3_file:
            return jsonify({"ok": False, "error": "No file received"}), 400

        mp3_dir = os.path.join(EXP_RADIO_DIR, "mp3")
        os.makedirs(mp3_dir, exist_ok=True)
        mp3_filename = f"{song['suno_uuid']}.mp3"
        mp3_path     = os.path.join(mp3_dir, mp3_filename)

        # Detect upload extension. Suno is migrating some songs from .mp3 to
        # .m4a (Opus codec). The browser tries .mp3 first and falls back to
        # .m4a, sending the filename with the right extension.
        upload_name = (mp3_file.filename or "").lower()
        is_m4a = upload_name.endswith(".m4a")
        if is_m4a:
            # Save the m4a temporarily, then transcode to mp3 so the rest of
            # the pipeline (FFmpeg concat demuxer, Whisper, cover normalize)
            # keeps working unchanged.
            tmp_path = os.path.join(mp3_dir, f"{song['suno_uuid']}.m4a")
            await mp3_file.save(tmp_path)
            try:
                _proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-y", "-i", tmp_path,
                    "-vn", "-acodec", "libmp3lame", "-b:a", "192k",
                    mp3_path,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, _err = await _proc.communicate()
                if _proc.returncode != 0:
                    try: os.remove(tmp_path)
                    except OSError: pass
                    return jsonify({
                        "ok": False,
                        "error": f"M4A→MP3 transcode failed: {(_err or b'').decode('utf-8', 'replace')[:200]}",
                    }), 500
            finally:
                try: os.remove(tmp_path)
                except OSError: pass
        else:
            await mp3_file.save(mp3_path)

        await db.update_exp_radio_song(song["id"], mp3_filename=mp3_filename)

        from bot.exp_radio_worker import process_exp_song
        asyncio.create_task(
            process_exp_song(db, song["id"], EXP_RADIO_DIR, bot=bot)
        )
        return jsonify({"ok": True})

    @app.route("/exp-radio/stream/<action>", methods=["POST"])
    @permission_required('exp_radio')
    async def exp_radio_stream_action(action):
        from quart import jsonify
        if action in ("start", "start_legacy"):
            if (await db.get_setting("exp_radio_enabled") or "on") != "on":
                return jsonify({"ok": False, "error": "Experimental Radio is disabled."}), 409
            if app.database_restore_pending:
                return jsonify({"ok": False, "error": "A database restore is in progress."}), 409
            twitch_key = await db.get_setting("exp_radio_twitch_key") or ""
            if not twitch_key:
                return jsonify({"ok": False, "error": "No Twitch stream key configured."}), 400
            async with app.radio_start_lock:
                if app.database_restore_pending:
                    return jsonify({"ok": False, "error": "A database restore is in progress."}), 409
                if stream_manager.is_running or stream_manager._loading:
                    return jsonify({
                        "ok": False,
                        "error": "The legacy Twitch Radio is currently running or starting.",
                    }), 409
                if trya_stream_manager.is_running:
                    return jsonify({
                        "ok": False,
                        "error": "TrYa Stream is currently running.",
                    }), 409
                result = await exp_stream_manager.start(
                    twitch_key, legacy_pipeline=(action == "start_legacy"),
                )
        elif action == "stop":
            result = await exp_stream_manager.stop()
        elif action == "safe_stop":
            result = await exp_stream_manager.safe_stop()
        else:
            return jsonify({"ok": False, "error": "Unknown action"}), 400
        return jsonify(result)

    @app.route("/exp-radio/stream/status")
    @permission_required('exp_radio')
    async def exp_radio_stream_status():
        from quart import jsonify
        return jsonify(await exp_stream_manager.get_status())

    @app.route("/exp-radio/cover-preview/<int:song_id>")
    @permission_required('exp_radio')
    async def exp_radio_cover_preview(song_id):
        """Serve the locally cached cover MP4 for admin preview.

        Triggers an on-demand download (and normalization) via the stream
        manager if the file hasn't been cached yet, so an admin can verify
        what will actually be streamed without starting the full stream.
        """
        from quart import send_file, abort
        s = await db.get_exp_radio_song(song_id)
        if not s:
            return abort(404)
        # Lazily download + normalize through the stream manager so this
        # follows the same Hook > regular video priority as the live stream.
        path = await exp_stream_manager._get_video(s)
        if not path or not os.path.exists(path):
            return abort(404)
        return await send_file(path, mimetype="video/mp4")

    @app.route("/exp-radio/stream/log")
    @permission_required('exp_radio')
    async def exp_radio_stream_log():
        """Return live-log entries for the admin UI panel.

        Query params:
          since   – unix timestamp (float). If given, only return entries
                    strictly newer than this. Used for incremental polling.
          window  – fallback time window in seconds (default 300 = 5 min).
        """
        from quart import jsonify, request
        try:
            since = float(request.args.get("since", "0") or 0)
        except ValueError:
            since = 0.0
        try:
            window = float(request.args.get("window", "300") or 300)
        except ValueError:
            window = 300.0
        return jsonify({
            "running": exp_stream_manager.is_running,
            "entries": exp_stream_manager.get_log(since_ts=since, max_age_secs=window),
        })

    # ── Twitch Bot OAuth re-authorization ──────────────────────────────────────
    _TWITCH_BOT_SCOPES = "user:bot user:write:chat user:read:chat chat:read"
    _TWITCH_EVENTSUB_SCOPES = "user:read:chat moderator:read:followers channel:read:subscriptions bits:read"
    _TWITCH_OAUTH_STATE_KEY = "twitch_oauth_state"
    _TWITCH_OAUTH_MODE_KEY = "twitch_oauth_mode"

    def _twitch_oauth_redirect_uri() -> str:
        from config import Config
        public_base = Config.WEB_URL.strip().rstrip("/")
        if not public_base:
            public_base = request.url_root.rstrip("/")
        return public_base + url_for("exp_radio_twitch_oauth_callback")

    @app.route("/exp-radio/twitch-oauth-start")
    @permission_required('exp_radio')
    async def exp_radio_twitch_oauth_start():
        import secrets as _sec
        from urllib.parse import urlencode
        client_id = await db.get_setting("exp_radio_twitch_client_id")
        if not client_id:
            await flash("Client ID not configured — save it first.", "error")
            return redirect(url_for("exp_radio_admin"))
        state = _sec.token_urlsafe(16)
        session[_TWITCH_OAUTH_STATE_KEY] = state
        session[_TWITCH_OAUTH_MODE_KEY] = "bot"
        redirect_uri = _twitch_oauth_redirect_uri()
        params = urlencode({
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": _TWITCH_BOT_SCOPES,
            "state": state,
        })
        return redirect(f"https://id.twitch.tv/oauth2/authorize?{params}")

    @app.route("/exp-radio/twitch-oauth-callback")
    @login_required
    async def exp_radio_twitch_oauth_callback():
        import aiohttp as _aio
        code      = request.args.get("code", "")
        state_got = request.args.get("state", "")
        error     = request.args.get("error_description") or request.args.get("error", "")
        oauth_mode = session.pop(_TWITCH_OAUTH_MODE_KEY, "bot")
        return_endpoint = {
            "radio_bot": "radio_admin",
            "trya_bot": "trya_stream_admin",
            "trya_eventsub": "trya_stream_admin",
        }.get(oauth_mode, "exp_radio_admin")
        if error:
            await flash(f"Twitch authorization denied: {error}", "error")
            return redirect(url_for(return_endpoint))
        state_exp = session.pop(_TWITCH_OAUTH_STATE_KEY, None)
        if not state_exp or state_got != state_exp:
            await flash("OAuth state mismatch — possible CSRF. Try again.", "error")
            return redirect(url_for(return_endpoint))
        credential_prefix = {
            "radio_bot": "radio_twitch",
            "trya_bot": "trya_stream_twitch",
            "trya_eventsub": "trya_stream_twitch",
        }.get(oauth_mode, "exp_radio_twitch")
        client_id     = await db.get_setting(f"{credential_prefix}_client_id") or ""
        client_secret = await db.get_setting(f"{credential_prefix}_client_secret") or ""
        redirect_uri  = _twitch_oauth_redirect_uri()
        try:
            async with _aio.ClientSession() as _s:
                async with _s.post(
                    "https://id.twitch.tv/oauth2/token",
                    data={
                        "client_id":     client_id,
                        "client_secret": client_secret,
                        "code":          code,
                        "grant_type":    "authorization_code",
                        "redirect_uri":  redirect_uri,
                    },
                    timeout=_aio.ClientTimeout(total=15),
                ) as _r:
                    _d = await _r.json()
                    if _r.status != 200 or "refresh_token" not in _d:
                        await flash(f"Token exchange failed: {_d.get('message', _d)}", "error")
                        return redirect(url_for(return_endpoint))
                    access_token  = _d["access_token"]
                    refresh_token = _d["refresh_token"]
                # Resolve bot login from the new token
                async with _aio.ClientSession() as _s:
                    async with _s.get(
                        "https://id.twitch.tv/oauth2/validate",
                        headers={"Authorization": f"OAuth {access_token}"},
                        timeout=_aio.ClientTimeout(total=10),
                    ) as _r:
                        _v = await _r.json()
                        bot_login = _v.get("login", "")
                        new_scopes = _v.get("scopes", [])
            if oauth_mode == "radio_bot":
                await db.set_setting("radio_twitch_refresh_token", refresh_token)
                await db.set_setting("radio_twitch_bot_login", bot_login)
                await db.set_setting("radio_twitch_bot_user_id", "")
                await flash(
                    f"Twitch Radio bot authorized as {bot_login} with scopes: {new_scopes}.",
                    "success",
                )
                return redirect(url_for("radio_admin"))

            if oauth_mode == "trya_eventsub":
                expected_login = (await db.get_setting("trya_stream_twitch_broadcaster_login") or "").strip().lower()
                if expected_login and bot_login.lower() != expected_login:
                    await flash(
                        f"Broadcaster authorization must use {expected_login}, but Twitch authorized {bot_login}.",
                        "error",
                    )
                    return redirect(url_for("trya_stream_admin"))
                prefix = "trya_stream_twitch_alerts_eventsub"
                await db.set_setting(f"{prefix}_client_id", client_id)
                await db.set_setting(f"{prefix}_client_secret", client_secret)
                await db.set_setting(f"{prefix}_refresh_token", refresh_token)
                await db.set_setting(f"{prefix}_broadcaster_login", expected_login or bot_login)
                await db.set_setting(f"{prefix}_bot_login", bot_login)
                await db.set_setting(f"{prefix}_bot_user_id", "")
                await db.set_setting(f"{prefix}_broadcaster_user_id", "")
                await trya_stream_event_alerts.restart()
                await flash(
                    f"TrYa Stream EventSub authorized as broadcaster {bot_login} with scopes: {new_scopes}.",
                    "success",
                )
                return redirect(url_for("trya_stream_admin"))

            if oauth_mode == "eventsub":
                expected_login = (await db.get_setting("exp_radio_twitch_broadcaster_login") or "").strip().lower()
                if expected_login and bot_login.lower() != expected_login:
                    await flash(
                        f"Broadcaster authorization must use {expected_login}, but Twitch authorized {bot_login}.",
                        "error",
                    )
                    return redirect(url_for("twitch_alerts_admin"))
                await db.set_setting("twitch_alerts_eventsub_client_id", client_id)
                await db.set_setting("twitch_alerts_eventsub_client_secret", client_secret)
                await db.set_setting("twitch_alerts_eventsub_refresh_token", refresh_token)
                await db.set_setting("twitch_alerts_eventsub_broadcaster_login", expected_login or bot_login)
                await db.set_setting("twitch_alerts_eventsub_bot_login", bot_login)
                await db.set_setting("twitch_alerts_eventsub_bot_user_id", "")
                await db.set_setting("twitch_alerts_eventsub_broadcaster_user_id", "")
                await twitch_event_alerts.restart()
                await flash(
                    f"EventSub authorized as broadcaster {bot_login} with scopes: {new_scopes}.",
                    "success",
                )
                return redirect(url_for("twitch_alerts_admin"))

            if oauth_mode == "trya_bot":
                await db.set_setting("trya_stream_twitch_refresh_token", refresh_token)
                await db.set_setting("trya_stream_twitch_bot_login", bot_login)
                await db.set_setting("trya_stream_twitch_bot_user_id", "")
                await trya_stream_event_alerts.restart()
                await trya_relic_hunt.stop()
                asyncio.create_task(_trya_relic_hunt_autostart())
                await flash(
                    f"TrYa Stream bot authorized as {bot_login} with scopes: {new_scopes}.",
                    "success",
                )
                return redirect(url_for("trya_stream_admin"))

            await db.set_setting("exp_radio_twitch_refresh_token", refresh_token)
            await db.set_setting("exp_radio_twitch_bot_login", bot_login)
            await db.set_setting("exp_radio_twitch_bot_user_id", "")
            scope_ok = "chat:read" in new_scopes
            msg = (
                f"✅ Bot re-authorized as {bot_login} with scopes: {new_scopes}. "
                + ("chat:read present — Relic Hunt will work!" if scope_ok
                   else "⚠️ chat:read still missing — check the app's Twitch scopes.")
            )
            await flash(msg, "success" if scope_ok else "error")
            # Auto-restart the relic hunt listener with the new token
            if scope_ok:
                await relic_hunt.stop()
                asyncio.create_task(_relic_hunt_autostart())
                await twitch_event_alerts.restart()
        except Exception as _e:
            await flash(f"OAuth callback error: {_e}", "error")
        return redirect(url_for(return_endpoint))

    @app.route("/trya-stream/twitch-alerts/broadcaster-oauth-start")
    @permission_required('trya_stream')
    async def trya_stream_alerts_broadcaster_oauth_start():
        import secrets as _sec
        from urllib.parse import urlencode
        client_id = await db.get_setting("trya_stream_twitch_client_id")
        broadcaster_login = await db.get_setting("trya_stream_twitch_broadcaster_login")
        if not client_id or not broadcaster_login:
            await flash("Configure the TrYa Stream Twitch Client ID and broadcaster login first.", "error")
            return redirect(url_for("trya_stream_admin"))
        state = _sec.token_urlsafe(16)
        session[_TWITCH_OAUTH_STATE_KEY] = state
        session[_TWITCH_OAUTH_MODE_KEY] = "trya_eventsub"
        params = urlencode({
            "client_id": client_id,
            "redirect_uri": _twitch_oauth_redirect_uri(),
            "response_type": "code",
            "scope": _TWITCH_EVENTSUB_SCOPES,
            "state": state,
            "force_verify": "true",
        })
        return redirect(f"https://id.twitch.tv/oauth2/authorize?{params}")

    @app.route("/twitch-alerts/broadcaster-oauth-start")
    @permission_required('twitch_alerts')
    async def twitch_alerts_broadcaster_oauth_start():
        import secrets as _sec
        from urllib.parse import urlencode
        client_id = await db.get_setting("exp_radio_twitch_client_id")
        broadcaster_login = await db.get_setting("exp_radio_twitch_broadcaster_login")
        if not client_id or not broadcaster_login:
            await flash("Configure the Twitch Client ID and broadcaster login in Exp. Radio first.", "error")
            return redirect(url_for("twitch_alerts_admin"))
        state = _sec.token_urlsafe(16)
        session[_TWITCH_OAUTH_STATE_KEY] = state
        session[_TWITCH_OAUTH_MODE_KEY] = "eventsub"
        params = urlencode({
            "client_id": client_id,
            "redirect_uri": _twitch_oauth_redirect_uri(),
            "response_type": "code",
            "scope": _TWITCH_EVENTSUB_SCOPES,
            "state": state,
            "force_verify": "true",
        })
        return redirect(f"https://id.twitch.tv/oauth2/authorize?{params}")

    @app.route("/exp-radio/consent-csv")
    @permission_required('exp_radio')
    async def exp_radio_consent_csv():
        import csv, io
        from datetime import datetime, timezone
        rows = await db.get_exp_radio_consent_csv_rows()
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "ID", "User ID", "Username", "Suno URL", "Title", "Artist",
            "Status", "Rights Hash", "Agreed At", "Submitted At", "Expires At", "Active",
        ])
        for r in rows:
            def _dt(ts):
                if not ts:
                    return ""
                return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            writer.writerow([
                r["id"], r["user_id"], r["user_name"], r["suno_url"],
                r["title"] or "", r["artist"] or "",
                r["analysis_status"], r["rights_hash"],
                _dt(r["rights_agreed_at"]), _dt(r["submitted_at"]), _dt(r["expires_at"]),
                "Yes" if r["active"] else "No",
            ])
        buf.seek(0)
        from quart import Response
        return Response(
            buf.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=exp_radio_consent.csv"},
        )

    @app.route("/trya-stream/consent-csv")
    @permission_required('trya_stream')
    async def trya_stream_consent_csv():
        import csv, io
        from datetime import datetime, timezone
        rows = await db.get_trya_stream_consent_csv_rows()
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            "ID", "User ID", "Username", "Playlist", "Suno URL", "Suno UUID",
            "Title", "Artist", "Analysis Status", "Active In Playlist",
            "Rights Version", "Rights Declaration", "Rights Hash", "Rights Accepted At",
            "Official Suno Download Attested", "Paid Commercial Download Attested",
            "Is Suno Remix", "Third-Party Rights Attested", "Commercial Use Attested",
            "Original SHA-256", "Original Filename", "Original MIME", "Original Size Bytes",
            "Original Uploaded At", "Original Archive Filename", "Working MP3 Filename",
            "Submitted At", "Playlist Leaves At", "Playlist Removed At",
            "Playlist Removal Reason", "Replacement Song ID", "Approval Status",
            "Approved At", "Approved By", "Whisper Anomaly Retry Count",
            "Whisper Anomaly Retry At", "Whisper Anomaly Retry Trigger",
        ])
        for r in rows:
            def _dt(ts):
                if not ts:
                    return ""
                return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
            writer.writerow([
                r.get("id"), r.get("user_id"), r.get("user_name"), r.get("playlist_source"),
                r.get("suno_url"), r.get("suno_uuid"), r.get("title") or "", r.get("artist") or "",
                r.get("analysis_status"), "Yes" if r.get("active") else "No",
                r.get("rights_version") or "", r.get("rights_declaration") or "", r.get("rights_hash") or "",
                _dt(r.get("rights_accepted_at")), "Yes" if r.get("official_download_attested") else "No",
                "Yes" if r.get("paid_download_attested") else "No", "Yes" if r.get("is_suno_remix") else "No",
                "Yes" if r.get("third_party_rights_attested") else "No",
                "Yes" if r.get("commercial_rights_attested") else "No",
                r.get("original_sha256") or "", r.get("original_filename") or "",
                r.get("original_mime") or "", r.get("original_size") or 0,
                _dt(r.get("original_uploaded_at")), r.get("original_archive_filename") or "",
                r.get("mp3_filename") or "", _dt(r.get("submitted_at")),
                _dt(r.get("playlist_expires_at")), _dt(r.get("playlist_removed_at")),
                r.get("playlist_remove_reason") or "", r.get("replacement_song_id") or "",
                r.get("approval_status") or "", _dt(r.get("approved_at")),
                r.get("approved_by") or "", r.get("whisper_anomaly_retry_count") or 0,
                _dt(r.get("whisper_anomaly_retry_at")),
                r.get("whisper_anomaly_retry_trigger") or "",
            ])
        buf.seek(0)
        from quart import Response
        return Response(
            buf.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=trya_stream_consent.csv"},
        )

    # --- Twitch Event Chat Alerts ---
    @app.route("/twitch-alerts", methods=["GET", "POST"])
    @permission_required('twitch_alerts')
    async def twitch_alerts_admin():
        if request.method == "POST":
            form = await request.form
            action = form.get("action", "")

            if action == "save_settings":
                checkbox_keys = {
                    "twitch_alerts_enabled",
                    "twitch_alerts_follow_enabled",
                    "twitch_alerts_sub_enabled",
                    "twitch_alerts_resub_enabled",
                    "twitch_alerts_gift_enabled",
                    "twitch_alerts_cheer_enabled",
                    "twitch_alerts_raid_enabled",
                    "twitch_alerts_watch_streak_enabled",
                }
                for key in checkbox_keys:
                    await db.set_setting(key, "on" if form.get(key) else "off")
                for key in (
                    "twitch_alerts_follow_template",
                    "twitch_alerts_sub_template",
                    "twitch_alerts_resub_template",
                    "twitch_alerts_gift_template",
                    "twitch_alerts_cheer_template",
                    "twitch_alerts_raid_template",
                    "twitch_alerts_watch_streak_template",
                ):
                    await db.set_setting(key, (form.get(key) or "")[:500])
                await twitch_event_alerts.restart()
                await flash("Twitch alert settings saved.", "success")

            elif action == "restart_listener":
                await twitch_event_alerts.restart()
                await flash("Twitch EventSub listener restarted.", "success")

            elif action == "test_message":
                bot = _TwitchBot(db, key_prefix="exp_radio_twitch")
                ok, msg = await bot.start()
                if ok:
                    sent = await bot.send("📣 Twitch alert test message from Corax.")
                    await flash("Test message sent." if sent else "Could not send test message.", "success" if sent else "error")
                else:
                    await flash(f"Twitch bot error: {msg}", "error")

            return redirect(url_for("twitch_alerts_admin"))

        settings = {}
        for key, default in DEFAULT_ALERT_SETTINGS.items():
            settings[key] = await db.get_setting(key) or default

        required_bot_scopes = [
            "user:write:chat",
            "user:bot",
            "user:read:chat",
            "chat:read",
        ]
        required_eventsub_scopes = [
            "user:read:chat",
            "moderator:read:followers",
            "channel:read:subscriptions",
            "bits:read",
        ]

        tw_diag = {}
        try:
            bot = _TwitchBot(db, key_prefix="exp_radio_twitch")
            tw_diag = await bot.diagnose(required_scopes=required_bot_scopes)
        except Exception as exc:
            tw_diag = {"ok": False, "message": str(exc), "scopes": []}

        eventsub_diag = {}
        try:
            eventsub_bot = _TwitchBot(db, key_prefix="twitch_alerts_eventsub")
            eventsub_diag = await eventsub_bot.diagnose(
                required_scopes=required_eventsub_scopes
            )
        except Exception as exc:
            eventsub_diag = {"ok": False, "message": str(exc), "scopes": []}

        return await render_template(
            "twitch_alerts.html",
            settings=settings,
            alert_status=twitch_event_alerts.status,
            tw_diag=tw_diag,
            eventsub_diag=eventsub_diag,
            required_bot_scopes=required_bot_scopes,
            required_eventsub_scopes=required_eventsub_scopes,
        )

    # --- Raven's Nest: Relic Hunt ---
    @app.route("/relic-hunt", methods=["GET", "POST"])
    @permission_required('relic_hunt')
    async def relic_hunt_admin():
        import json as _json
        from quart import jsonify

        if request.method == "POST":
            form = await request.form
            action = form.get("action", "")

            if action == "save_settings":
                _bool_keys = {"announce_level_ups", "announce_rank_ups",
                              "mods_bypass_cooldowns", "auto_event_enabled",
                              "village_payout_enabled"}
                _auto_event_interval_keys = {"auto_event_min_interval_minutes",
                                             "auto_event_max_interval_minutes"}
                _village_interval_keys = {"village_payout_interval_minutes"}
                for key in (
                    "enabled", "command_prefix", "timezone",
                    "raven_cooldown_seconds", "ritual_cooldown_seconds",
                    "leaderboard_size", "announce_level_ups", "announce_rank_ups",
                    "mods_bypass_cooldowns", "subscriber_cooldown_multiplier",
                    "vip_cooldown_multiplier", "ritual_reward_points",
                    "ritual_reward_xp", "ritual_legendary_chance",
                    "ritual_active_window_minutes",
                    "access_mode",
                    "auto_event_enabled",
                    "auto_event_min_interval_minutes",
                    "auto_event_max_interval_minutes",
                    "shiny_per_find",
                    "shiny_per_combine",
                    "shiny_per_ritual",
                    "village_progress_cost_shinies",
                    "village_next_cost_shinies",
                    "village_payout_enabled",
                    "village_payout_interval_minutes",
                    "village_active_window_minutes",
                    "village_points_per_level",
                    "village_xp_per_level",
                    "village_items_per_level",
                    "village_shinies_per_level",
                ):
                    if key in _bool_keys:
                        val = "true" if form.get(key) else "false"
                    elif key == "access_mode":
                        val = form.get(key, "everyone")
                        if val not in ("everyone", "subscribers"):
                            val = "everyone"
                    else:
                        val = form.get(key, "")
                    await db.relic_set_setting(key, val)
                # Reset the next-event timer whenever interval settings change
                # so the new values take effect immediately.
                if _auto_event_interval_keys & set(form.keys()):
                    await db.relic_set_setting("auto_event_next_at", "0")
                if _village_interval_keys & set(form.keys()):
                    await db.relic_set_setting("village_next_payout_at", "0")
                await flash("Settings saved.", "success")

            elif action == "toggle_game":
                enabled = (await db.relic_get_setting("enabled")) != "false"
                await db.relic_set_setting("enabled", "false" if enabled else "true")
                await flash(f"Game {'disabled' if enabled else 'enabled'}.", "success")

            elif action == "start_listener":
                await db.ensure_relic_tables()
                if relic_hunt._running:
                    await relic_hunt.stop()
                client_id   = await db.get_setting("exp_radio_twitch_client_id")
                refresh_tok = await db.get_setting("exp_radio_twitch_refresh_token")
                broadcaster = await db.get_setting("exp_radio_twitch_broadcaster_login")
                if not (client_id and refresh_tok and broadcaster):
                    await flash("Twitch credentials not configured. Set them in Exp. Radio \u2192 Settings.", "error")
                else:
                    bot = _TwitchBot(db, key_prefix="exp_radio_twitch")
                    ok, msg = await bot.start()
                    if ok:
                        await relic_hunt.start(bot)
                        await flash("Relic Hunt listener started.", "success")
                    else:
                        await flash(f"Twitch bot error: {msg}", "error")

            elif action == "stop_listener":
                await relic_hunt.stop()
                await flash("Relic Hunt listener stopped.", "success")

            elif action == "upsert_item":
                item_id = form.get("item_id", "").strip().replace(" ", "_").lower()
                if not item_id:
                    await flash("Item ID is required.", "error")
                else:
                    item = {
                        "id": item_id,
                        "name": form.get("name", ""),
                        "rarity": form.get("rarity", "common"),
                        "enabled": 1 if form.get("enabled") else 0,
                        "drop_weight": float(form.get("drop_weight") or 1),
                        "min_points": int(form.get("min_points") or 0),
                        "max_points": int(form.get("max_points") or 0),
                        "min_xp": int(form.get("min_xp") or 0),
                        "max_xp": int(form.get("max_xp") or 0),
                        "flavor_text": form.get("flavor_text", ""),
                        "announce_globally": 1 if form.get("announce_globally") else 0,
                        "can_be_used_in_ritual": 1 if form.get("can_be_used_in_ritual") else 0,
                        "ritual_energy": int(form.get("ritual_energy") or 0),
                        "icon": form.get("icon", ""),
                        "category": form.get("category", ""),
                        "seasonal_tag": form.get("seasonal_tag") or None,
                        "required_event": form.get("required_event") or None,
                    }
                    await db.relic_upsert_item(item)
                    await flash(f"Item '{item['name']}' saved.", "success")

            elif action == "delete_item":
                item_id = form.get("item_id", "")
                await db.relic_delete_item(item_id)
                await flash("Item deleted.", "success")

            elif action == "toggle_item":
                item_id = form.get("item_id", "")
                item = await db.relic_get_item(item_id)
                if item:
                    item["enabled"] = 0 if item["enabled"] else 1
                    await db.relic_upsert_item(item)

            elif action == "import_items":
                raw = form.get("items_json", "")
                try:
                    items = _json.loads(raw)
                    if not isinstance(items, list):
                        raise ValueError("Expected a JSON array")
                    count = 0
                    for it in items:
                        it.setdefault("enabled", True)
                        it.setdefault("announce_globally", it.get("rarity") in ("rare","epic","legendary","mythic"))
                        it["enabled"] = 1 if it["enabled"] else 0
                        it["announce_globally"] = 1 if it["announce_globally"] else 0
                        it["can_be_used_in_ritual"] = 1 if it.get("can_be_used_in_ritual") else 0
                        await db.relic_upsert_item(it)
                        count += 1
                    await flash(f"Imported {count} item(s).", "success")
                except Exception as e:
                    await flash(f"Import failed: {e}", "error")

            elif action == "reset_items":
                from bot.relic_hunt import DEFAULT_ITEMS
                import time as _time
                for item in DEFAULT_ITEMS:
                    row = {
                        "id": item["id"], "name": item["name"],
                        "rarity": item.get("rarity","common"), "enabled": 1,
                        "drop_weight": item.get("drop_weight",1),
                        "min_points": item.get("min_points",0), "max_points": item.get("max_points",0),
                        "min_xp": item.get("min_xp",0), "max_xp": item.get("max_xp",0),
                        "flavor_text": item.get("flavor_text",""),
                        "announce_globally": 1 if item.get("announce_globally") else 0,
                        "can_be_used_in_ritual": 1 if item.get("can_be_used_in_ritual") else 0,
                        "ritual_energy": item.get("ritual_energy",0),
                        "icon": item.get("icon",""), "category": item.get("category",""),
                        "seasonal_tag": None, "required_event": None,
                    }
                    await db.relic_upsert_item(row)
                await flash(f"Default item library restored ({len(DEFAULT_ITEMS)} items).", "success")

            elif action == "upsert_rank":
                rank_id = form.get("rank_id", "").strip().replace(" ", "_").lower()
                if not rank_id:
                    await flash("Rank ID is required.", "error")
                elif not form.get("name", "").strip():
                    await flash("Rank name is required.", "error")
                else:
                    await db.relic_upsert_rank({
                        "id": rank_id,
                        "name": form.get("name", "").strip(),
                        "icon": form.get("icon", "").strip(),
                        "min_points": max(0, int(form.get("min_points") or 0)),
                        "enabled": 1 if form.get("enabled") else 0,
                    })
                    await flash("Rank saved.", "success")

            elif action == "toggle_rank":
                rank_id = form.get("rank_id", "")
                rank = await db.relic_get_rank(rank_id)
                if rank:
                    rank["enabled"] = 0 if rank["enabled"] else 1
                    await db.relic_upsert_rank(rank)

            elif action == "delete_rank":
                await db.relic_delete_rank(form.get("rank_id", ""))
                await flash("Rank deleted.", "success")

            elif action == "reset_ranks":
                from bot.relic_hunt import DEFAULT_RANKS
                for rank in DEFAULT_RANKS:
                    await db.relic_upsert_rank({**rank, "enabled": 1})
                await flash(f"Default ranks restored ({len(DEFAULT_RANKS)} ranks).", "success")

            elif action == "upsert_combine_recipe":
                recipe_id = form.get("recipe_id", "").strip().replace(" ", "_").lower()
                ingredient_a_id = form.get("ingredient_a_id", "")
                ingredient_b_id = form.get("ingredient_b_id", "")
                result_item_id = form.get("result_item_id", "")
                item_ids = {item["id"] for item in await db.relic_get_all_items()}
                if not recipe_id:
                    await flash("Recipe ID is required.", "error")
                elif not all((ingredient_a_id, ingredient_b_id, result_item_id)):
                    await flash("Both ingredients and a result item are required.", "error")
                elif not {ingredient_a_id, ingredient_b_id, result_item_id}.issubset(item_ids):
                    await flash("Every recipe item must exist in the item library.", "error")
                else:
                    await db.relic_upsert_combine_recipe({
                        "id": recipe_id,
                        "ingredient_a_id": ingredient_a_id,
                        "ingredient_b_id": ingredient_b_id,
                        "result_item_id": result_item_id,
                        "bonus_points": max(0, int(form.get("bonus_points") or 0)),
                        "priority": max(0, int(form.get("priority") or 100)),
                        "enabled": 1 if form.get("enabled") else 0,
                    })
                    await flash("Combine recipe saved.", "success")

            elif action == "toggle_combine_recipe":
                recipe = await db.relic_get_combine_recipe(form.get("recipe_id", ""))
                if recipe:
                    recipe["enabled"] = 0 if recipe["enabled"] else 1
                    await db.relic_upsert_combine_recipe(recipe)

            elif action == "delete_combine_recipe":
                await db.relic_delete_combine_recipe(form.get("recipe_id", ""))
                await flash("Combine recipe deleted.", "success")

            elif action == "reset_combine_recipes":
                from bot.relic_hunt import DEFAULT_COMBINE_RECIPES
                item_ids = {item["id"] for item in await db.relic_get_all_items()}
                restored = 0
                for recipe in DEFAULT_COMBINE_RECIPES:
                    recipe_items = {
                        recipe["ingredient_a_id"],
                        recipe["ingredient_b_id"],
                        recipe["result_item_id"],
                    }
                    if recipe_items.issubset(item_ids):
                        await db.relic_upsert_combine_recipe({**recipe, "enabled": 1})
                        restored += 1
                await flash(
                    f"Default combine recipes restored ({restored} recipes).",
                    "success",
                )

            elif action == "upsert_custom_command":
                command = (form.get("command") or "").strip().lstrip("!").lower()
                response = (form.get("response") or "").strip()
                reserved_commands = {
                    "raven", "nest", "items", "top", "rank", "daily",
                    "ritual", "combine", "village", "entertain", "teach",
                    "trade", "invest", "nextvillage", "phrase", "solve",
                    "relichelp", "relic",
                }
                if not re.fullmatch(r"[a-z0-9_][a-z0-9_-]{0,31}", command):
                    await flash("Command must use 1-32 letters, numbers, underscores or hyphens.", "error")
                elif command in reserved_commands:
                    await flash(f"'!{command}' is a built-in Raven's Nest command.", "error")
                elif not response:
                    await flash("Response text is required.", "error")
                else:
                    await db.relic_upsert_custom_command(
                        command,
                        response[:500],
                        enabled=bool(form.get("enabled")),
                    )
                    await flash(f"Command '!{command}' saved.", "success")

            elif action == "toggle_custom_command":
                await db.relic_toggle_custom_command(form.get("command", ""))

            elif action == "delete_custom_command":
                command = (form.get("command") or "").strip().lstrip("!")
                await db.relic_delete_custom_command(command)
                await flash(f"Command '!{command}' deleted.", "success")

            elif action == "save_phrase_puzzle":
                enabled = bool(form.get("phrase_enabled"))
                loop_queue = bool(form.get("phrase_loop_queue"))
                try:
                    chance_percent = float(
                        form.get("letter_find_chance_percent") or 5
                    )
                    reward_xp = max(
                        0, int(form.get("winner_xp_reward") or 500)
                    )
                except ValueError:
                    await flash("Chance and XP reward must be valid numbers.", "error")
                else:
                    await db.relic_save_phrase_puzzle(
                        enabled=enabled,
                        loop_queue=loop_queue,
                        letter_find_chance=min(
                            100.0, max(0.0, chance_percent)
                        ) / 100.0,
                        winner_xp_reward=reward_xp,
                    )
                    await flash("Phrase puzzle settings saved.", "success")

            elif action == "add_phrase_to_queue":
                phrase = form.get("phrase", "").strip()
                if not any(char.isalpha() for char in phrase):
                    await flash(
                        "A queued phrase must contain at least one letter.",
                        "error",
                    )
                else:
                    await db.relic_add_phrase_to_queue(phrase)
                    await flash("Phrase added to the queue.", "success")

            elif action == "generate_phrase_suggestion":
                import bot.exp_stream_manager as _esm
                if exp_stream_manager.is_running or _esm.stream_is_live:
                    await flash("Phrase generation is disabled while the stream is running.", "error")
                elif session.get("relic_phrase_generation_running"):
                    await flash("Phrase generation is already running. Please wait for it to finish.", "error")
                else:
                    session["relic_phrase_generation_running"] = True
                    try:
                        from bot.llm import OllamaClient
                        from config import Config

                        phrase_puzzle = await db.relic_get_phrase_puzzle()
                        phrase_queue = await db.relic_get_phrase_queue()
                        existing = []
                        if phrase_puzzle.get("phrase"):
                            existing.append(phrase_puzzle["phrase"])
                        for queued in phrase_queue:
                            phrase = (queued.get("phrase") or "").strip()
                            if phrase and phrase not in existing:
                                existing.append(phrase)
                        examples = "\n".join(
                            f"- {phrase}" for phrase in existing[:40]
                        ) or "- The raven waits in the moonlit nest"
                        prompt = (
                            "Generate exactly 10 new phrases for a Twitch chat "
                            "word/phrase puzzle in a game called Raven's Nest: Relic Hunt.\n"
                            "Match the vibe of these existing phrases: dark fantasy, ravens, relics, rituals, "
                            "mist, moonlight, playful mystery. The phrase should be memorable and solvable.\n\n"
                            f"Existing phrases:\n{examples}\n\n"
                            "Rules:\n"
                            "- Output exactly 10 lines, one phrase per line.\n"
                            "- No quotes, no numbering, no explanation.\n"
                            "- Each phrase must be 4 to 10 words.\n"
                            "- Do not use apostrophes or contractions. Write phrases without possessive forms like raven's or relic's.\n"
                            "- Separate words with normal spaces only. Do not use hyphens or dashes between words.\n"
                            "- Use only letters, numbers, spaces and simple commas if needed.\n"
                            "- Avoid duplicating any existing phrase.\n"
                            "- English only.\n"
                        )
                        client = OllamaClient(
                            base_url=Config.OLLAMA_URL,
                            model=Config.LLM_MODEL,
                            timeout=Config.LLM_REQUEST_TIMEOUT,
                        )
                        data = await client.chat(
                            [
                                {
                                    "role": "system",
                                    "content": "You create concise phrase puzzle answers. Output only the requested phrases, one per line.",
                                },
                                {"role": "user", "content": prompt},
                            ],
                            max_tokens=220,
                            temperature=0.85,
                            top_p=0.9,
                        )
                        content = (
                            ((data.get("message") or {}).get("content") or "")
                            .strip()
                        )
                        suggestions = []
                        seen = {phrase.casefold() for phrase in existing}
                        for line in content.splitlines():
                            suggestion = line.strip().strip('"“”').strip()
                            suggestion = re.sub(r"^[\-\d\.\)\s]+", "", suggestion).strip()
                            suggestion = re.sub(r"[-‐‑‒–—―]+", " ", suggestion)
                            suggestion = re.sub(r"\s+", " ", suggestion)
                            if not any(char.isalpha() for char in suggestion):
                                continue
                            if "'" in suggestion or "’" in suggestion:
                                continue
                            key = suggestion.casefold()
                            if key in seen:
                                continue
                            seen.add(key)
                            suggestions.append(suggestion[:160])
                            if len(suggestions) >= 10:
                                break
                        if not suggestions:
                            await flash("LLM did not return usable phrases.", "error")
                        else:
                            session["relic_phrase_suggestions"] = suggestions
                            session["relic_phrase_suggestion"] = suggestions[0]
                            await flash(f"Generated {len(suggestions)} phrase suggestions. Pick one and review it before adding.", "success")
                    except Exception as e:
                        await flash(f"Phrase generation failed: {e}", "error")
                    finally:
                        session.pop("relic_phrase_generation_running", None)

            elif action == "confirm_phrase_suggestion":
                phrase = (
                    (form.get("suggested_phrase") or "")
                    or (form.get("suggested_phrase_fallback") or "")
                    or (session.get("relic_phrase_suggestion") or "")
                ).strip()
                phrase = re.sub(r"[-‐‑‒–—―]+", " ", phrase)
                phrase = re.sub(r"\s+", " ", phrase).strip()
                if not any(char.isalpha() for char in phrase):
                    await flash(
                        "A suggested phrase must contain at least one letter.",
                        "error",
                    )
                else:
                    suggestions = session.get("relic_phrase_suggestions", [])
                    if not isinstance(suggestions, list):
                        suggestions = []
                    phrases_to_add = []
                    seen = set()
                    for candidate in [phrase, *suggestions]:
                        candidate = (candidate or "").strip()
                        candidate = re.sub(r"[-‐‑‒–—―]+", " ", candidate)
                        candidate = re.sub(r"\s+", " ", candidate).strip()
                        if not any(char.isalpha() for char in candidate):
                            continue
                        key = candidate.casefold()
                        if key in seen:
                            continue
                        seen.add(key)
                        phrases_to_add.append(candidate)
                    added = 0
                    for candidate in phrases_to_add:
                        await db.relic_add_phrase_to_queue(candidate)
                        added += 1
                    session.pop("relic_phrase_suggestion", None)
                    session.pop("relic_phrase_suggestions", None)
                    await flash(f"Added {added} suggested phrase(s) to the queue.", "success")

            elif action == "clear_phrase_suggestion":
                session.pop("relic_phrase_suggestion", None)
                session.pop("relic_phrase_suggestions", None)
                await flash("Phrase suggestion discarded.", "success")

            elif action == "start_phrase":
                phrase_id = int(form.get("phrase_id") or 0)
                if await db.relic_activate_phrase(phrase_id):
                    await flash("Phrase started.", "success")
                else:
                    await flash("Phrase not found.", "error")

            elif action == "delete_phrase":
                phrase_id = int(form.get("phrase_id") or 0)
                await db.relic_delete_phrase_from_queue(phrase_id)
                await flash("Phrase removed from the queue.", "success")

            elif action == "skip_phrase":
                next_phrase = await db.relic_activate_next_phrase()
                if next_phrase:
                    await flash("Skipped to the next queued phrase.", "success")
                else:
                    await flash(
                        "No queued phrase remains. Phrase Puzzle disabled.",
                        "success",
                    )

            elif action == "reset_phrase_progress":
                await db.relic_reset_phrase_progress()
                await flash("Phrase puzzle progress reset.", "success")

            elif action == "edit_user_points":
                uid = form.get("twitch_user_id", "")
                user = await db.relic_get_user(uid)
                if user:
                    user["points"] = max(0, int(form.get("points") or 0))
                    if "shinies" in form:
                        user["shinies"] = max(0, int(form.get("shinies") or 0))
                    await db.relic_upsert_user(user)
                    await flash(f"Updated user resources for {user['username']}.", "success")

            elif action == "reset_user":
                uid = form.get("twitch_user_id", "")
                await db.relic_delete_user(uid)
                await flash("User reset.", "success")

            elif action == "give_item":
                uid = form.get("twitch_user_id", "")
                item_id = form.get("item_id", "")
                amount = int(form.get("amount") or 1)
                for _ in range(amount):
                    await db.relic_add_item_to_user(uid, item_id)
                await flash(f"Gave {amount}x {item_id} to user.", "success")

            elif action == "upsert_event":
                eid = form.get("event_id", "").strip().replace(" ","_").lower()
                if not eid:
                    await flash("Event ID required.", "error")
                else:
                    cfg = {
                        "durationMinutes": int(form.get("duration_minutes") or 10),
                        "rareDropMultiplier": float(form.get("rare_drop_multiplier") or 1.0),
                        "epicDropMultiplier": float(form.get("epic_drop_multiplier") or 1.0),
                        "pointsMultiplier": float(form.get("points_multiplier") or 1.0),
                        "xpMultiplier": float(form.get("xp_multiplier") or 1.0),
                        "ritualEnergyMultiplier": float(form.get("ritual_energy_multiplier") or 1.0),
                        "startMessage": form.get("start_message",""),
                        "endMessage": form.get("end_message",""),
                    }
                    await db.relic_upsert_event({
                        "id": eid, "name": form.get("event_name",eid),
                        "enabled": 1 if form.get("event_enabled") else 0,
                        "config_json": _json.dumps(cfg),
                    })
                    await flash(f"Event '{eid}' saved.", "success")

            elif action == "delete_event":
                await db.relic_delete_event(form.get("event_id",""))
                await flash("Event deleted.", "success")

            elif action == "start_event":
                eid = form.get("event_id","")
                events = {e["id"]: e for e in await db.relic_get_all_events()}
                if eid in events:
                    cfg = _json.loads(events[eid]["config_json"])
                    dur = int(form.get("duration_minutes") or cfg.get("durationMinutes",10))
                    await db.relic_start_event(eid, dur * 60, "admin")
                    msg = cfg.get("startMessage", f"Event {eid} started!")
                    if relic_hunt._bot:
                        await relic_hunt._bot.send(msg)
                    await flash(f"Event started: {msg}", "success")

            elif action == "stop_event":
                eid = form.get("event_id","")
                events = {e["id"]: e for e in await db.relic_get_all_events()}
                if eid in events:
                    cfg = _json.loads(events[eid]["config_json"])
                    await db.relic_stop_event(eid)
                    msg = cfg.get("endMessage", f"Event {eid} stopped.")
                    if relic_hunt._bot:
                        await relic_hunt._bot.send(msg)
                    await flash(f"Event stopped.", "success")

            elif action == "ritual_add_energy":
                ritual = await db.relic_get_ritual()
                add = int(form.get("add_energy") or 0)
                await db.relic_update_ritual(ritual["energy"] + add)
                await flash(f"Added {add} ritual energy.", "success")

            elif action == "ritual_reset":
                await db.relic_update_ritual(0)
                await flash("Ritual reset.", "success")

            elif action == "ritual_save_settings":
                for key in ("ritual_goal", "ritual_reward_points", "ritual_reward_xp",
                             "ritual_legendary_chance", "ritual_active_window_minutes",
                             "ritual_cooldown_seconds"):
                    await db.relic_set_setting(key.replace("ritual_","",1), form.get(key,""))
                goal = int(form.get("ritual_goal") or 500)
                ritual = await db.relic_get_ritual()
                await db.relic_update_ritual(ritual["energy"], goal)
                await flash("Ritual settings saved.", "success")

            elif action == "village_add_progress":
                area_id = form.get("area_id", "")
                amount = max(0, int(form.get("progress") or 0))
                area = await db.relic_add_village_progress(area_id, amount)
                if area:
                    await flash(f"Added {amount} progress to {area['name']}.", "success")
                else:
                    await flash("Village area not found.", "error")

            elif action == "village_reset_progress":
                await db.relic_reset_village()
                await db.relic_set_setting("village_next_payout_at", "0")
                await flash("Village progress reset.", "success")

            elif action == "village_set_count":
                village_count = max(1, int(form.get("village_count") or 1))
                await db.relic_set_setting("village_count", str(village_count))
                await db.relic_set_setting("village_next_payout_at", "0")
                await flash(f"Village count set to {village_count}.", "success")

            return redirect(request.url)

        # GET
        await db.ensure_relic_tables()
        from bot.relic_hunt import DEFAULT_RANKS
        ranks = await db.relic_get_all_ranks()
        if not ranks:
            for rank in DEFAULT_RANKS:
                await db.relic_upsert_rank({**rank, "enabled": 1})
            ranks = await db.relic_get_all_ranks()
        items   = await db.relic_get_all_items()
        from bot.relic_hunt import (
            DEFAULT_COMBINE_RECIPES,
            DEFAULT_COMBINE_RECIPES_VERSION,
        )
        recipe_version = int(
            (await db.relic_get_setting("combine_recipes_version")) or 0
        )
        if recipe_version < DEFAULT_COMBINE_RECIPES_VERSION:
            item_ids = {item["id"] for item in items}
            for recipe in DEFAULT_COMBINE_RECIPES:
                recipe_items = {
                    recipe["ingredient_a_id"],
                    recipe["ingredient_b_id"],
                    recipe["result_item_id"],
                }
                if recipe_items.issubset(item_ids):
                    await db.relic_insert_combine_recipe_if_missing(
                        {**recipe, "enabled": 1}
                    )
            await db.relic_set_setting(
                "combine_recipes_version",
                str(DEFAULT_COMBINE_RECIPES_VERSION),
            )
        recipes = await db.relic_get_all_combine_recipes()
        custom_commands = await db.relic_get_all_custom_commands()
        users   = await db.relic_get_all_users()
        events  = await db.relic_get_all_events()
        active_events = await db.relic_get_active_events()
        active_event_ids = {ae["event_id"] for ae in active_events}
        ritual  = await db.relic_get_ritual()
        village_areas = await db.relic_get_village_areas()
        phrase_puzzle = await db.relic_get_phrase_puzzle()
        phrase_queue = await db.relic_get_phrase_queue()
        from bot.relic_hunt import _phrase_progress
        phrase_progress = _phrase_progress(
            phrase_puzzle.get("phrase", ""),
            phrase_puzzle.get("revealed_mask", ""),
        )
        phrase_total_letters = sum(
            char.isalpha() for char in phrase_puzzle.get("phrase", "")
        )
        phrase_found_letters = sum(
            1
            for index, char in enumerate(phrase_puzzle.get("phrase", ""))
            if char.isalpha()
            and index < len(phrase_puzzle.get("revealed_mask", ""))
            and phrase_puzzle["revealed_mask"][index] == "1"
        )
        log     = await db.relic_get_recent_log(30)
        game_enabled = (await db.relic_get_setting("enabled")) != "false"
        listener_running = relic_hunt._running
        import bot.exp_stream_manager as _esm
        exp_stream_running = bool(exp_stream_manager.is_running or _esm.stream_is_live)
        phrase_suggestion = session.get("relic_phrase_suggestion", "")
        phrase_suggestions = session.get("relic_phrase_suggestions", [])
        if not isinstance(phrase_suggestions, list):
            phrase_suggestions = []

        # Load settings
        import json as _jrh
        settings = {}
        for key in (
            "command_prefix", "timezone", "raven_cooldown_seconds",
            "ritual_cooldown_seconds", "leaderboard_size", "announce_level_ups",
            "announce_rank_ups", "mods_bypass_cooldowns",
            "subscriber_cooldown_multiplier", "vip_cooldown_multiplier",
            "ritual_reward_points", "ritual_reward_xp", "ritual_legendary_chance",
            "ritual_active_window_minutes",
            "access_mode",
            "auto_event_enabled",
            "auto_event_min_interval_minutes",
            "auto_event_max_interval_minutes",
            "shiny_per_find",
            "shiny_per_combine",
            "shiny_per_ritual",
            "village_progress_cost_shinies",
            "village_next_cost_shinies",
            "village_payout_enabled",
            "village_payout_interval_minutes",
            "village_active_window_minutes",
            "village_points_per_level",
            "village_xp_per_level",
            "village_items_per_level",
            "village_shinies_per_level",
            "village_count",
        ):
            settings[key] = await db.relic_get_setting(key) or ""

        # Enrich events with parsed config
        events_parsed = []
        for ev in events:
            try:
                cfg = _jrh.loads(ev["config_json"])
            except Exception:
                cfg = {}
            events_parsed.append({**ev, "cfg": cfg, "is_active": ev["id"] in active_event_ids})

        # Stats
        total_hunts = sum(u.get("commands_used", 0) for u in users)
        total_legendary = sum(u.get("legendary_finds", 0) for u in users)
        total_mythic = sum(u.get("mythic_finds", 0) for u in users)
        total_shinies = sum(int(u.get("shinies") or 0) for u in users)

        return await render_template(
            "relic_hunt.html",
            items=items, users=users[:50],
            events=events_parsed, ritual=ritual,
            village_areas=village_areas,
            log=log, game_enabled=game_enabled,
            listener_running=listener_running,
            exp_stream_running=exp_stream_running,
            settings=settings,
            total_hunts=total_hunts,
            total_legendary=total_legendary,
            total_mythic=total_mythic,
            total_shinies=total_shinies,
            ranks=ranks,
            recipes=recipes,
            custom_commands=custom_commands,
            phrase_puzzle=phrase_puzzle,
            phrase_queue=phrase_queue,
            phrase_suggestion=phrase_suggestion,
            phrase_suggestions=phrase_suggestions,
            phrase_progress=phrase_progress,
            phrase_found_letters=phrase_found_letters,
            phrase_total_letters=phrase_total_letters,
            active_event_ids=active_event_ids,
        )

    @app.route("/relic-hunt/export-items")
    @permission_required('relic_hunt')
    async def relic_hunt_export_items():
        import json as _json
        from quart import Response
        items = await db.relic_get_all_items()
        return Response(
            _json.dumps(items, indent=2),
            mimetype="application/json",
            headers={"Content-Disposition": "attachment; filename=relic_items.json"},
        )

    # --- Suno Audio Analyzer ---
    @app.route("/suno-analyzer")
    @permission_required('suno_analyzer')
    async def suno_analyzer():
        return await render_template("suno_analyzer.html")

    @app.route("/songripper")
    @permission_required('songripper')
    async def songripper():
        return await render_template("songripper.html")

    async def _songripper_conversion_block_status() -> dict:
        """Return whether CPU-heavy Songripper work must yield to Exp. Radio."""
        import bot.exp_stream_manager as _esm

        if (
            exp_stream_manager.is_running
            or _esm.stream_is_live
            or app.radio_start_lock.locked()
        ):
            return {
                "blocked": True,
                "reason": "stream_live",
                "message": (
                    "Conversions are temporarily unavailable while "
                    "Experimental Radio is running. Direct downloads remain available."
                ),
                "until": None,
            }

        try:
            enabled = await db.get_setting("exp_radio_schedule_enabled") or "off"
            days_csv = await db.get_setting("exp_radio_schedule_days") or ""
            schedule_time = (
                await db.get_setting("exp_radio_schedule_time") or ""
            ).strip()
            if enabled != "on" or ":" not in schedule_time:
                return {"blocked": False, "reason": "", "message": "", "until": None}

            days = {
                int(day)
                for day in days_csv.split(",")
                if day.strip().isdigit() and 0 <= int(day) <= 6
            }
            hour_text, minute_text = schedule_time.split(":", 1)
            hour, minute = int(hour_text), int(minute_text)
            if not days or not 0 <= hour <= 23 or not 0 <= minute <= 59:
                return {"blocked": False, "reason": "", "message": "", "until": None}

            now = datetime.now(ZoneInfo("Europe/Berlin"))
            for day_offset in (0, -1):
                candidate_day = (now + timedelta(days=day_offset)).date()
                if candidate_day.weekday() not in days:
                    continue
                starts_at = datetime.combine(
                    candidate_day,
                    datetime.min.time(),
                    tzinfo=now.tzinfo,
                ).replace(hour=hour, minute=minute)
                ends_at = starts_at + timedelta(minutes=150)
                if starts_at <= now < ends_at:
                    return {
                        "blocked": True,
                        "reason": "scheduled_stream_window",
                        "message": (
                            "Conversions are reserved for Experimental Radio until "
                            f"{ends_at.strftime('%H:%M')} Europe/Berlin. "
                            "Direct downloads remain available."
                        ),
                        "until": ends_at.isoformat(),
                    }
        except (TypeError, ValueError):
            pass

        return {"blocked": False, "reason": "", "message": "", "until": None}

    async def _songripper_conversion_block_response():
        status = await _songripper_conversion_block_status()
        if not status["blocked"]:
            return None
        from quart import jsonify
        return jsonify({"error": status["message"], **status}), 423

    async def _wait_for_songripper_ffmpeg(process, timeout: float = 900):
        """Wait for FFmpeg while yielding immediately to an Exp. Radio run."""
        communicate_task = asyncio.create_task(process.communicate())
        deadline = time.monotonic() + timeout
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("Songripper conversion timed out.")
                try:
                    return await asyncio.wait_for(
                        asyncio.shield(communicate_task),
                        timeout=min(2.0, remaining),
                    )
                except asyncio.TimeoutError:
                    block_status = await _songripper_conversion_block_status()
                    if block_status["blocked"]:
                        raise RuntimeError(block_status["message"])
        finally:
            if not communicate_task.done():
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await communicate_task

    @app.route("/songripper/conversion-status")
    @permission_required('songripper')
    async def songripper_conversion_status():
        from quart import jsonify
        return jsonify(await _songripper_conversion_block_status())

    async def _songripper_playlist_name(url: str) -> str:
        """Resolve the public playlist title without making it a hard dependency."""
        import aiohttp
        import html as _html

        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(
                    url,
                    headers={"User-Agent": "Mozilla/5.0 (X11; Linux) AppleWebKit/537.36"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        return ""
                    page = await resp.text()
            match = re.search(
                r'<meta\s+property="og:title"\s+content="([^"]+)"', page
            ) or re.search(r'<title>([^<]+)</title>', page)
            if not match:
                return ""
            name = _html.unescape(match.group(1).strip())
            return re.sub(r'\s*[|\-\u2013]\s*Suno\s*$', '', name).strip()
        except Exception:
            return ""

    async def _resolve_songripper_playlist_metadata(songs: list[dict]) -> list[dict]:
        """Replace playlist UUID placeholders with metadata from each song page."""
        semaphore = asyncio.Semaphore(5)

        async def resolve_song(song: dict) -> dict:
            resolved = dict(song)
            song_uuid = str(song.get("uuid") or "").strip()
            title = str(song.get("title") or "").strip()
            artist = str(song.get("artist") or "").strip()
            title_is_placeholder = (
                not title
                or title == song_uuid
                or title == song_uuid[:12]
            )
            if not song_uuid or (not title_is_placeholder and artist):
                return resolved
            async with semaphore:
                meta = await _fetch_suno_meta(song_uuid)
            if title_is_placeholder and meta.get("title"):
                resolved["title"] = meta["title"]
            if not artist and meta.get("artist"):
                resolved["artist"] = meta["artist"]
            if meta.get("image_url"):
                resolved["image_url"] = meta["image_url"]
            return resolved

        return list(await asyncio.gather(*(resolve_song(song) for song in songs)))

    def _songripper_archive_name(value: str, fallback: str) -> str:
        """Keep international titles while removing unsafe path characters."""
        value = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', ' ', value or '')
        value = re.sub(r'\s+', ' ', value).strip(' .')
        return value[:100] or fallback

    def _cleanup_songripper_playlist_jobs(max_age: int = 6 * 60 * 60) -> None:
        cutoff = time.time() - max_age
        for job_id, job in list(app.songripper_playlist_jobs.items()):
            if job.get("created_at", 0) >= cutoff or job.get("state") in {"queued", "running"}:
                continue
            path = job.get("archive_path")
            if path:
                try:
                    os.remove(path)
                except OSError:
                    pass
            app.songripper_playlist_jobs.pop(job_id, None)

    async def _run_songripper_playlist_job(job_id: str, url: str, output_format: str) -> None:
        """Download and convert one playlist at a time, then package it as ZIP."""
        import aiohttp
        import shutil
        import tempfile
        import zipfile
        from bot.stream_manager import parse_suno_playlist

        job = app.songripper_playlist_jobs[job_id]
        work_dir = tempfile.mkdtemp(prefix="songripper_playlist_work_")
        archive_fd, archive_path = tempfile.mkstemp(
            prefix="songripper_playlist_", suffix=".zip"
        )
        os.close(archive_fd)
        job["archive_path"] = archive_path

        try:
            async with app.songripper_playlist_lock:
                block_status = await _songripper_conversion_block_status()
                if block_status["blocked"]:
                    raise RuntimeError(block_status["message"])
                job.update(state="running", message="Resolving playlist...", percent=1)
                songs = await parse_suno_playlist(url)
                if not songs:
                    raise RuntimeError("No songs were found in this playlist.")
                job.update(message="Resolving track names...", percent=2)
                songs = await _resolve_songripper_playlist_metadata(songs)

                job["total"] = len(songs)
                playlist_name = await _songripper_playlist_name(url) or "Suno Playlist"
                job["playlist_name"] = playlist_name
                archive_filename = _songripper_archive_name(playlist_name, "Suno Playlist")
                job["archive_name"] = f"{archive_filename} - {output_format.upper()}.zip"

                await asyncio.to_thread(
                    lambda: zipfile.ZipFile(archive_path, "w").close()
                )
                errors = []
                used_names = set()
                headers = {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                }

                async with aiohttp.ClientSession(headers=headers) as sess:
                    for index, song in enumerate(songs, start=1):
                        block_status = await _songripper_conversion_block_status()
                        if block_status["blocked"]:
                            raise RuntimeError(block_status["message"])
                        song_uuid = str(song.get("uuid") or "").strip()
                        title = str(song.get("title") or "").strip()
                        title = title or song_uuid[:12]
                        safe_title = _songripper_archive_name(title, song_uuid[:12])
                        base_name = f"{index:03d} - {safe_title}"
                        unique_name = base_name
                        duplicate = 2
                        while unique_name.casefold() in used_names:
                            unique_name = f"{base_name} ({duplicate})"
                            duplicate += 1
                        used_names.add(unique_name.casefold())

                        job.update(
                            current=index,
                            current_title=title,
                            message=f"Processing {index}/{len(songs)}: {title}",
                            percent=max(2, int(((index - 1) / len(songs)) * 96)),
                        )
                        src_path = os.path.join(work_dir, "source.audio")
                        out_path = os.path.join(work_dir, f"converted.{output_format}")
                        for path in (src_path, out_path):
                            try:
                                os.remove(path)
                            except OSError:
                                pass

                        last_status = None
                        try:
                            for ext in ("mp3", "m4a"):
                                audio_url = f"https://cdn1.suno.ai/{song_uuid}.{ext}"
                                async with sess.get(
                                    audio_url,
                                    timeout=aiohttp.ClientTimeout(total=120),
                                ) as resp:
                                    last_status = resp.status
                                    if resp.status != 200:
                                        continue
                                    with open(src_path, "wb") as fh:
                                        async for chunk in resp.content.iter_chunked(1024 * 256):
                                            if chunk:
                                                fh.write(chunk)
                                    break
                            else:
                                raise RuntimeError(f"audio download returned HTTP {last_status}")

                            codec_args = [
                                "-c:a", "libmp3lame", "-b:a", "320k", "-write_xing", "1"
                            ]
                            proc = await asyncio.create_subprocess_exec(
                                "ffmpeg", "-y", "-hide_banner", "-v", "error",
                                "-i", src_path,
                                "-map", "0:a:0", "-vn",
                                *codec_args,
                                out_path,
                                stdout=asyncio.subprocess.DEVNULL,
                                stderr=asyncio.subprocess.PIPE,
                            )
                            _, err = await _wait_for_songripper_ffmpeg(proc)
                            if proc.returncode != 0 or not os.path.isfile(out_path):
                                detail = err.decode("utf-8", errors="replace").strip()
                                raise RuntimeError(detail or "unknown ffmpeg error")

                            archive_member = f"{unique_name}.{output_format}"

                            def add_to_archive():
                                with zipfile.ZipFile(
                                    archive_path, "a", compression=zipfile.ZIP_DEFLATED
                                ) as archive:
                                    archive.write(out_path, archive_member)

                            await asyncio.to_thread(add_to_archive)
                            job["completed"] += 1
                        except Exception as exc:
                            errors.append(f"{index:03d} - {title}: {exc}")
                            job["failed"] += 1

                if errors:
                    error_text = (
                        "The following playlist tracks could not be exported:\n\n"
                        + "\n".join(errors)
                        + "\n"
                    )

                    def add_error_report():
                        with zipfile.ZipFile(
                            archive_path, "a", compression=zipfile.ZIP_DEFLATED
                        ) as archive:
                            archive.writestr("EXPORT_ERRORS.txt", error_text)

                    await asyncio.to_thread(add_error_report)

                if not job["completed"]:
                    raise RuntimeError("Every track failed to download or convert.")

                job.update(
                    state="ready",
                    message=(
                        f"Archive ready: {job['completed']} track(s)"
                        + (f", {job['failed']} failed" if job["failed"] else "")
                    ),
                    percent=100,
                    download_url=f"/songripper/playlist/download/{job_id}",
                )
                job["cleanup_task"] = asyncio.create_task(
                    _delete_temp_file_later(archive_path, delay=6 * 60 * 60)
                )
        except Exception as exc:
            job.update(state="error", message=str(exc), percent=0)
            try:
                os.remove(archive_path)
            except OSError:
                pass
            job["archive_path"] = ""
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    @app.route("/songripper/resolve-playlist")
    @permission_required('songripper')
    async def songripper_resolve_playlist():
        """Return the ordered contents of a public Suno playlist."""
        from quart import jsonify
        from bot.stream_manager import parse_suno_playlist

        url = (request.args.get("url") or "").strip()
        if not re.search(r'https?://(?:www\.)?suno\.com/playlist/[a-f0-9-]{36}', url, re.I):
            return jsonify({"error": "Enter a valid Suno playlist URL."}), 400
        try:
            songs = await parse_suno_playlist(url)
            if not songs:
                return jsonify({"error": "No songs were found in this playlist."}), 404
            songs = await _resolve_songripper_playlist_metadata(songs)
            name = await _songripper_playlist_name(url)
            return jsonify({
                "name": name or "Suno Playlist",
                "songs": [
                    {
                        "uuid": song.get("uuid"),
                        "title": song.get("title") or str(song.get("uuid") or "")[:12],
                        "artist": song.get("artist") or "",
                        "duration": song.get("duration"),
                    }
                    for song in songs
                ],
            })
        except Exception as exc:
            return jsonify({"error": f"Playlist could not be loaded: {exc}"}), 502

    @app.route("/songripper/playlist/prepare", methods=["POST"])
    @permission_required('songripper')
    async def songripper_prepare_playlist():
        """Start a serial playlist export in the background."""
        from quart import jsonify

        blocked = await _songripper_conversion_block_response()
        if blocked is not None:
            return blocked

        data = await request.get_json(silent=True) or {}
        url = str(data.get("url") or "").strip()
        output_format = str(data.get("format") or "mp3").strip().lower()
        if not re.search(r'https?://(?:www\.)?suno\.com/playlist/[a-f0-9-]{36}', url, re.I):
            return jsonify({"error": "Enter a valid Suno playlist URL."}), 400
        if output_format != "mp3":
            return jsonify({"error": "Only MP3 export is available."}), 400

        _cleanup_songripper_playlist_jobs()
        job_id = secrets.token_urlsafe(24)
        app.songripper_playlist_jobs[job_id] = {
            "created_at": time.time(),
            "state": "queued",
            "message": "Waiting for the playlist exporter...",
            "percent": 0,
            "current": 0,
            "total": 0,
            "completed": 0,
            "failed": 0,
            "current_title": "",
            "archive_path": "",
            "archive_name": "Suno Playlist.zip",
            "download_url": "",
        }
        app.songripper_playlist_jobs[job_id]["task"] = asyncio.create_task(
            _run_songripper_playlist_job(job_id, url, output_format)
        )
        return jsonify({"job_id": job_id})

    @app.route("/songripper/playlist/status/<job_id>")
    @permission_required('songripper')
    async def songripper_playlist_status(job_id):
        from quart import jsonify

        job = app.songripper_playlist_jobs.get(job_id)
        if not job:
            return jsonify({"error": "Playlist export job not found."}), 404
        return jsonify({
            key: job.get(key)
            for key in (
                "state", "message", "percent", "current", "total",
                "completed", "failed", "current_title", "download_url",
            )
        })

    @app.route("/songripper/playlist/download/<job_id>")
    @permission_required('songripper')
    async def songripper_download_playlist(job_id):
        from quart import jsonify, send_file

        job = app.songripper_playlist_jobs.get(job_id)
        if not job:
            return jsonify({"error": "Playlist export job not found."}), 404
        archive_path = job.get("archive_path")
        if job.get("state") != "ready" or not archive_path or not os.path.isfile(archive_path):
            return jsonify({"error": "Playlist archive is not ready."}), 409
        asyncio.create_task(_delete_temp_file_later(archive_path, delay=900))
        return await send_file(
            archive_path,
            mimetype="application/zip",
            as_attachment=True,
            attachment_filename=job.get("archive_name") or "Suno Playlist.zip",
        )

    @app.route("/songripper/resolve")
    @permission_required('songripper')
    async def songripper_resolve():
        """Resolve Suno song/share URLs for the lightweight audio ripper."""
        from quart import jsonify
        import aiohttp, re as _re
        song_id = request.args.get("id", "").strip()
        if not song_id:
            return jsonify({"error": "No id provided"}), 400
        is_uuid = bool(_re.match(r'^[a-f0-9]{8}-[a-f0-9]{4}', song_id))
        url = f"https://suno.com/song/{song_id}" if is_uuid else f"https://suno.com/s/{song_id}"
        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            async with aiohttp.ClientSession() as sess:
                async with sess.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return jsonify({"error": f"Suno returned {resp.status}"}), 502
                    html = await resp.text()
            result = {}
            m = _re.search(r'"id"\s*:\s*"([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})"', html)
            if not m:
                m = _re.search(r'song/([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})', html)
            result["realId"] = m.group(1) if m else (song_id if is_uuid else None)
            m = _re.search(r'<title>([^<|]+)', html)
            result["title"] = m.group(1).strip() if m else ""
            m = _re.search(r'cdn2\.suno\.ai/[^"]+\.jpeg', html)
            result["artwork"] = "https://" + m.group(0) if m else ""
            author = ""
            title_by = _re.search(r'\bby\s+(.+?)(?:\s*\||\s*-\s*Suno|$)', result.get("title", ""))
            if title_by:
                author = title_by.group(1).strip()
            if not author:
                m = _re.search(r'\\"display_name\\":\\"([^\\]+)\\"', html)
                if not m:
                    m = _re.search(r'"display_name":"([^"]+)"', html)
                if m:
                    author = m.group(1)
            result["author"] = author
            if result["realId"]:
                rich_meta = await _fetch_suno_meta(result["realId"])
                result["title"] = rich_meta.get("title") or result["title"]
                result["author"] = rich_meta.get("artist") or result["author"]
                result["artwork"] = (
                    rich_meta.get("image_url")
                    or result["artwork"]
                    or f"https://cdn1.suno.ai/image_large_{result['realId']}.jpeg"
                )
                result["video"] = rich_meta.get("video_url") or ""
                result["karaokeVideo"] = rich_meta.get("karaoke_video_url") or ""
            return jsonify(result)
        except Exception as e:
            return jsonify({"error": str(e)}), 502

    @app.route("/songripper/download-asset")
    @permission_required('songripper')
    async def songripper_download_asset():
        """Download a song's Suno-hosted video or cover image."""
        from quart import jsonify, send_file
        from urllib.parse import urlparse
        import aiohttp, re as _re, tempfile

        song_id = (request.args.get("id") or "").strip()
        kind = (request.args.get("kind") or "").strip().lower()
        uuid_match = _re.fullmatch(
            r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}",
            song_id,
            _re.I,
        )
        if not uuid_match:
            return jsonify({"error": "A resolved Suno song UUID is required."}), 400
        if kind not in {"video", "karaoke", "cover"}:
            return jsonify({"error": "Asset type must be video, karaoke, or cover."}), 400

        song_uuid = uuid_match.group(0)
        meta = await _fetch_suno_meta(song_uuid)
        if kind == "video":
            asset_url = meta.get("video_url")
        elif kind == "karaoke":
            asset_url = meta.get("karaoke_video_url")
        else:
            asset_url = meta.get("image_url")
        if not asset_url and kind == "cover":
            asset_url = f"https://cdn1.suno.ai/image_large_{song_uuid}.jpeg"
        if not asset_url:
            return jsonify({"error": "No Suno video is available for this song."}), 404

        def trusted_suno_url(value):
            parsed = urlparse(value)
            host = (parsed.hostname or "").lower()
            return parsed.scheme == "https" and (host == "suno.ai" or host.endswith(".suno.ai"))

        if not trusted_suno_url(asset_url):
            return jsonify({"error": "Suno returned an unsupported asset URL."}), 502

        requested_name = (request.args.get("filename") or "suno_song").strip()
        requested_name = _re.sub(r"[^a-zA-Z0-9_. -]+", "", requested_name).strip(" .") or "suno_song"
        stem = os.path.splitext(requested_name)[0].strip(" .") or "suno_song"
        size_limit = 350 * 1024 * 1024 if kind in {"video", "karaoke"} else 30 * 1024 * 1024
        temp_path = ""
        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            async with aiohttp.ClientSession(headers=headers) as sess:
                async with sess.get(
                    asset_url,
                    timeout=aiohttp.ClientTimeout(total=180),
                ) as resp:
                    if resp.status != 200:
                        return jsonify({"error": f"Suno asset download returned HTTP {resp.status}."}), 502
                    if not trusted_suno_url(str(resp.url)):
                        return jsonify({"error": "Suno redirected to an unsupported asset host."}), 502
                    try:
                        declared_size = int(resp.headers.get("Content-Length") or 0)
                    except (TypeError, ValueError):
                        declared_size = 0
                    if declared_size > size_limit:
                        return jsonify({"error": f"The {kind} exceeds the download size limit."}), 413

                    content_type = (resp.headers.get("Content-Type") or "").split(";", 1)[0].lower()
                    valid_content_type = (
                        kind in {"video", "karaoke"}
                        and (content_type.startswith("video/") or content_type == "application/octet-stream")
                    ) or (
                        kind == "cover" and content_type.startswith("image/")
                    )
                    if not valid_content_type:
                        return jsonify({"error": f"Suno returned an invalid {kind} file."}), 502
                    extension_map = {
                        "video/mp4": ".mp4",
                        "video/webm": ".webm",
                        "video/quicktime": ".mov",
                        "image/jpeg": ".jpg",
                        "image/png": ".png",
                        "image/webp": ".webp",
                    }
                    default_extension = ".mp4" if kind in {"video", "karaoke"} else ".jpg"
                    extension = extension_map.get(content_type, default_extension)
                    fd, temp_path = tempfile.mkstemp(prefix="songripper_asset_", suffix=extension)
                    os.close(fd)
                    downloaded = 0
                    with open(temp_path, "wb") as fh:
                        async for chunk in resp.content.iter_chunked(1024 * 256):
                            downloaded += len(chunk)
                            if downloaded > size_limit:
                                raise ValueError(f"The {kind} exceeds the download size limit.")
                            fh.write(chunk)

            attachment_name = f"{stem}{extension}"
            asyncio.create_task(_delete_temp_file_later(temp_path, delay=900))
            return await send_file(
                temp_path,
                mimetype=content_type or ("video/mp4" if kind in {"video", "karaoke"} else "image/jpeg"),
                as_attachment=True,
                attachment_filename=attachment_name,
            )
        except ValueError as exc:
            if temp_path:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            return jsonify({"error": str(exc)}), 413
        except Exception as exc:
            if temp_path:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
            return jsonify({"error": f"Asset download failed: {exc}"}), 502

    @app.route("/songripper/download-square-video")
    @permission_required('songripper')
    async def songripper_download_square_video():
        """Download a Suno video and render a normalized 1080x1080 MP4."""
        from quart import jsonify, send_file
        from urllib.parse import urlparse
        import aiohttp, re as _re, tempfile

        blocked = await _songripper_conversion_block_response()
        if blocked is not None:
            return blocked

        song_id = (request.args.get("id") or "").strip()
        mode = (request.args.get("mode") or "pad").strip().lower()
        if mode not in {"pad", "crop", "blur"}:
            return jsonify({"error": "Square mode must be pad, crop, or blur."}), 400
        uuid_match = _re.fullmatch(
            r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}",
            song_id,
            _re.I,
        )
        if not uuid_match:
            return jsonify({"error": "A resolved Suno song UUID is required."}), 400

        song_uuid = uuid_match.group(0)
        meta = await _fetch_suno_meta(song_uuid)
        video_url = str(meta.get("video_url") or "").strip()
        if not video_url:
            return jsonify({"error": "No Suno video is available for this song."}), 404

        def trusted_suno_url(value):
            parsed = urlparse(value)
            host = (parsed.hostname or "").lower()
            return parsed.scheme == "https" and (
                host == "suno.ai" or host.endswith(".suno.ai")
            )

        if not trusted_suno_url(video_url):
            return jsonify({"error": "Suno returned an unsupported video URL."}), 502

        requested_name = (request.args.get("filename") or "suno_song_video.mp4").strip()
        requested_name = _re.sub(
            r"[^a-zA-Z0-9_. -]+", "", requested_name
        ).strip(" .") or "suno_song_video.mp4"
        stem = os.path.splitext(requested_name)[0].strip(" .") or "suno_song_video"
        attachment_name = f"{stem}.mp4"
        source_path = ""
        output_path = ""
        process = None
        size_limit = 350 * 1024 * 1024

        try:
            source_fd, source_path = tempfile.mkstemp(
                prefix="songripper_video_source_", suffix=".video"
            )
            os.close(source_fd)
            output_fd, output_path = tempfile.mkstemp(
                prefix="songripper_square_video_", suffix=".mp4"
            )
            os.close(output_fd)

            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36"
                )
            }
            async with aiohttp.ClientSession(headers=headers) as sess:
                async with sess.get(
                    video_url,
                    timeout=aiohttp.ClientTimeout(total=300),
                ) as resp:
                    if resp.status != 200:
                        return jsonify({
                            "error": f"Suno video download returned HTTP {resp.status}."
                        }), 502
                    if not trusted_suno_url(str(resp.url)):
                        return jsonify({
                            "error": "Suno redirected to an unsupported video host."
                        }), 502
                    content_type = (
                        (resp.headers.get("Content-Type") or "")
                        .split(";", 1)[0]
                        .lower()
                    )
                    if not (
                        content_type.startswith("video/")
                        or content_type == "application/octet-stream"
                    ):
                        return jsonify({"error": "Suno returned an invalid video file."}), 502
                    try:
                        declared_size = int(resp.headers.get("Content-Length") or 0)
                    except (TypeError, ValueError):
                        declared_size = 0
                    if declared_size > size_limit:
                        return jsonify({"error": "The Suno video exceeds 350 MB."}), 413

                    downloaded = 0
                    with open(source_path, "wb") as handle:
                        async for chunk in resp.content.iter_chunked(1024 * 1024):
                            downloaded += len(chunk)
                            if downloaded > size_limit:
                                raise ValueError("The Suno video exceeds 350 MB.")
                            handle.write(chunk)
            if os.path.getsize(source_path) < 1024:
                raise ValueError("The downloaded Suno video is empty.")

            if mode == "crop":
                video_args = [
                    "-vf",
                    "scale=1080:1080:force_original_aspect_ratio=increase,"
                    "crop=1080:1080,setsar=1",
                    "-map", "0:v:0", "-map", "0:a?",
                ]
            elif mode == "blur":
                video_args = [
                    "-filter_complex",
                    "[0:v]split=2[bg][fg];"
                    "[bg]scale=1080:1080:force_original_aspect_ratio=increase,"
                    "crop=1080:1080,boxblur=30:10[blurred];"
                    "[fg]scale=1080:1080:force_original_aspect_ratio=decrease[front];"
                    "[blurred][front]overlay=(W-w)/2:(H-h)/2,setsar=1[vout]",
                    "-map", "[vout]", "-map", "0:a?",
                ]
            else:
                video_args = [
                    "-vf",
                    "scale=1080:1080:force_original_aspect_ratio=decrease,"
                    "pad=1080:1080:(ow-iw)/2:(oh-ih)/2:black,setsar=1",
                    "-map", "0:v:0", "-map", "0:a?",
                ]

            process = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-hide_banner", "-v", "error",
                "-i", source_path,
                *video_args,
                "-c:v", "libx264", "-preset", "medium", "-crf", "18",
                "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "256k",
                "-movflags", "+faststart",
                output_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await _wait_for_songripper_ffmpeg(process)
            if process.returncode != 0:
                error_text = stderr.decode("utf-8", "replace")[-1800:].strip()
                raise RuntimeError(error_text or "FFmpeg video conversion failed.")
            if not os.path.isfile(output_path) or os.path.getsize(output_path) < 1024:
                raise RuntimeError("FFmpeg did not create a valid output video.")

            try:
                os.remove(source_path)
            except OSError:
                pass
            source_path = ""
            asyncio.create_task(_delete_temp_file_later(output_path, delay=900))
            return await send_file(
                output_path,
                mimetype="video/mp4",
                as_attachment=True,
                attachment_filename=attachment_name,
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 413
        except FileNotFoundError:
            return jsonify({"error": "FFmpeg is not installed in the container."}), 500
        except Exception as exc:
            return jsonify({"error": f"Video conversion failed: {exc}"}), 502
        finally:
            if source_path:
                try:
                    os.remove(source_path)
                except OSError:
                    pass
            if output_path and (not os.path.isfile(output_path) or process is None or process.returncode != 0):
                try:
                    os.remove(output_path)
                except OSError:
                    pass

    @app.route("/songripper/download-mp3")
    @permission_required('songripper')
    async def songripper_download_mp3():
        """Download Suno audio server-side and re-encode it to a clean MP3."""
        from quart import Response, jsonify
        import aiohttp, re as _re, tempfile

        blocked = await _songripper_conversion_block_response()
        if blocked is not None:
            return blocked

        song_id = request.args.get("id", "").strip()
        filename = request.args.get("filename", "suno_song").strip()
        filename = _re.sub(r"[^a-zA-Z0-9_. -]+", "", filename).strip(" .") or "suno_song"
        if not filename.lower().endswith(".mp3"):
            filename += ".mp3"
        if not song_id:
            return jsonify({"error": "No id provided"}), 400

        uuid_match = _re.search(
            r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}",
            song_id,
            _re.I,
        )
        if not uuid_match:
            return jsonify({"error": "A resolved Suno song UUID is required."}), 400
        audio_id = uuid_match.group(0)

        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        try:
            with tempfile.TemporaryDirectory(prefix="songripper_") as tmpdir:
                src_path = os.path.join(tmpdir, "source.audio")
                out_path = os.path.join(tmpdir, "clean.mp3")
                last_status = None

                async with aiohttp.ClientSession(headers=headers) as sess:
                    for ext in ("mp3", "m4a"):
                        url = f"https://cdn1.suno.ai/{audio_id}.{ext}"
                        async with sess.get(url, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                            last_status = resp.status
                            if resp.status != 200:
                                continue
                            with open(src_path, "wb") as fh:
                                async for chunk in resp.content.iter_chunked(1024 * 256):
                                    if chunk:
                                        fh.write(chunk)
                            break
                    else:
                        return jsonify({"error": f"Audio not available from Suno ({last_status})."}), 502

                proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-y", "-hide_banner", "-v", "error",
                    "-i", src_path,
                    "-map", "0:a:0",
                    "-vn",
                    "-c:a", "libmp3lame",
                    "-b:a", "320k",
                    "-write_xing", "1",
                    out_path,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, err = await _wait_for_songripper_ffmpeg(proc)
                if proc.returncode != 0 or not os.path.exists(out_path):
                    detail = err.decode("utf-8", errors="replace").strip()
                    return jsonify({"error": f"MP3 re-encode failed: {detail or 'unknown ffmpeg error'}"}), 500

                with open(out_path, "rb") as fh:
                    payload = fh.read()

            return Response(
                payload,
                mimetype="audio/mpeg",
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}"',
                    "Content-Length": str(len(payload)),
                },
            )
        except Exception as e:
            return jsonify({"error": str(e)}), 502

    @app.route("/songripper/download-square")
    @permission_required('songripper')
    async def songripper_download_square():
        """Convert Suno audio to a mono square signal with a configurable dead zone."""
        from quart import Response, jsonify
        import aiohttp, re as _re, tempfile

        blocked = await _songripper_conversion_block_response()
        if blocked is not None:
            return blocked

        song_id = request.args.get("id", "").strip()
        output_format = request.args.get("format", "mp3").strip().lower()
        if output_format != "mp3":
            return jsonify({"error": "Only MP3 export is available."}), 400
        try:
            threshold = float(request.args.get("threshold", "0.10"))
        except (TypeError, ValueError):
            return jsonify({"error": "Threshold must be a number between 0 and 0.5."}), 400
        if not 0.0 <= threshold <= 0.5:
            return jsonify({"error": "Threshold must be between 0 and 0.5."}), 400

        filename = request.args.get("filename", "suno_song_square").strip()
        filename = _re.sub(r"[^a-zA-Z0-9_. -]+", "", filename).strip(" .") or "suno_song_square"
        extension = f".{output_format}"
        if not filename.lower().endswith(extension):
            filename += extension
        if not song_id:
            return jsonify({"error": "No id provided"}), 400

        uuid_match = _re.search(
            r"[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}",
            song_id,
            _re.I,
        )
        if not uuid_match:
            return jsonify({"error": "A resolved Suno song UUID is required."}), 400
        audio_id = uuid_match.group(0)

        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        try:
            with tempfile.TemporaryDirectory(prefix="songripper_square_") as tmpdir:
                src_path = os.path.join(tmpdir, "source.audio")
                out_path = os.path.join(tmpdir, f"square{extension}")
                last_status = None

                async with aiohttp.ClientSession(headers=headers) as sess:
                    for ext in ("mp3", "m4a"):
                        url = f"https://cdn1.suno.ai/{audio_id}.{ext}"
                        async with sess.get(url, timeout=aiohttp.ClientTimeout(total=120)) as resp:
                            last_status = resp.status
                            if resp.status != 200:
                                continue
                            with open(src_path, "wb") as fh:
                                async for chunk in resp.content.iter_chunked(1024 * 256):
                                    if chunk:
                                        fh.write(chunk)
                            break
                    else:
                        return jsonify({"error": f"Audio not available from Suno ({last_status})."}), 502

                # First mix to mono. Samples inside the configurable dead zone
                # become zero; all others become either -0.8 or +0.8. This keeps
                # low-level noise from becoming a full-amplitude interrupter pulse.
                threshold_text = f"{threshold:.4f}"
                square_filter = (
                    "aformat=channel_layouts=mono,"
                    "aeval='if(lt(abs(val(0)),"
                    + threshold_text
                    + "),0,if(gte(val(0),0),0.8,-0.8))':c=mono"
                )
                codec_args = [
                    "-c:a", "libmp3lame", "-b:a", "320k", "-write_xing", "1"
                ]
                proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-y", "-hide_banner", "-v", "error",
                    "-i", src_path,
                    "-map", "0:a:0",
                    "-vn",
                    "-ac", "1",
                    "-af", square_filter,
                    *codec_args,
                    out_path,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE,
                )
                _, err = await _wait_for_songripper_ffmpeg(proc)
                if proc.returncode != 0 or not os.path.exists(out_path):
                    detail = err.decode("utf-8", errors="replace").strip()
                    return jsonify({"error": f"Square-wave conversion failed: {detail or 'unknown ffmpeg error'}"}), 500

                with open(out_path, "rb") as fh:
                    payload = fh.read()

            return Response(
                payload,
                mimetype="audio/mpeg",
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}"',
                    "Content-Length": str(len(payload)),
                },
            )
        except Exception as e:
            return jsonify({"error": str(e)}), 502

    @app.route("/suno-analyzer/resolve")
    @permission_required('suno_analyzer')
    async def suno_analyzer_resolve():
        """Server-side proxy to resolve Suno share links and fetch metadata (avoids CORS)."""
        from quart import jsonify
        import aiohttp, re as _re
        song_id = request.args.get("id", "").strip()
        if not song_id:
            return jsonify({"error": "No id provided"}), 400
        # Determine URL
        is_uuid = bool(_re.match(r'^[a-f0-9]{8}-[a-f0-9]{4}', song_id))
        url = f"https://suno.com/song/{song_id}" if is_uuid else f"https://suno.com/s/{song_id}"
        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            async with aiohttp.ClientSession() as sess:
                async with sess.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return jsonify({"error": f"Suno returned {resp.status}"}), 502
                    html = await resp.text()
            result = {}
            # Extract real UUID
            m = _re.search(r'"id"\s*:\s*"([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})"', html)
            if not m:
                m = _re.search(r'song/([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})', html)
            result["realId"] = m.group(1) if m else None
            # Title
            m = _re.search(r'<title>([^<|]+)', html)
            result["title"] = m.group(1).strip() if m else ""
            # Artwork
            m = _re.search(r'cdn2\.suno\.ai/[^"]+\.jpeg', html)
            result["artwork"] = "https://" + m.group(0) if m else ""
            # Author — try title "by Author" first (most reliable), then display_name
            author = ""
            title_by = _re.search(r'\bby\s+(.+?)(?:\s*\||\s*-\s*Suno|$)', result.get("title", ""))
            if title_by:
                author = title_by.group(1).strip()
            if not author:
                m = _re.search(r'\\"display_name\\":\\"([^\\]+)\\"', html)
                if not m:
                    m = _re.search(r'"display_name":"([^"]+)"', html)
                if m:
                    author = m.group(1)
            result["author"] = author
            # Tags
            m = _re.search(r'\\"tags\\":\\"([^\\]+)\\"', html)
            if not m:
                m = _re.search(r'"tags":"([^"]+)"', html)
            result["tags"] = [t.strip() for t in m.group(1).split(",") if t.strip()] if m else []
            # Stats
            for key, pat in [("plays", r'\\"play_count\\":(\d+)'), ("likes", r'\\"upvote_count\\":(\d+)'), ("comments", r'\\"comment_count\\":(\d+)')]:
                m = _re.search(pat, html)
                result[key] = int(m.group(1)) if m else None
            # Created at
            m = _re.search(r'\\"created_at\\":\\"([^\\]+)\\"', html)
            result["created_at"] = m.group(1) if m else ""
            # Model
            m = _re.search(r'\\"major_model_version\\":\\"([^\\]+)\\"', html)
            result["model"] = m.group(1) if m else ""
            # Lyrics — extract from Next.js RSC payload (same method as stream_manager)
            lyrics_text = ""
            rsc_match = _re.search(
                r'self\.__next_f\.push\(\[1,"[0-9a-f]+:T[0-9a-f]+,"\]\)</script>'
                r'<script>self\.__next_f\.push\(\[1,"(.*?)"\]\)</script>',
                html, _re.DOTALL
            )
            if rsc_match:
                raw = rsc_match.group(1)
                lyrics_text = raw.replace("\\n", "\n").replace("\\t", " ").replace('\\"', '"').strip()
                print(f"[suno-resolve] Lyrics from RSC payload, length={len(lyrics_text)}", flush=True)
            else:
                # Fallback: try _fetch_suno_meta helper
                real_id = result.get("realId") or song_id
                try:
                    meta = await _fetch_suno_meta(real_id)
                    if meta.get("lyrics"):
                        lyrics_text = meta["lyrics"]
                        print(f"[suno-resolve] Lyrics from embed helper, length={len(lyrics_text)}", flush=True)
                    if not result.get("author") and meta.get("artist"):
                        result["author"] = meta["artist"]
                except Exception as e:
                    print(f"[suno-resolve] Helper failed: {e}", flush=True)
            if not lyrics_text:
                print(f"[suno-resolve] No lyrics found for {song_id}", flush=True)
            result["lyrics"] = lyrics_text
            # GPT description prompt (style/genre prompt used for generation)
            m = _re.search(r'\\"gpt_description_prompt\\":\\"((?:[^\\]|\\.)*)(?:\\"|")', html)
            if not m:
                m = _re.search(r'"gpt_description_prompt":"((?:[^"\\]|\\.)*)"', html)
            result["prompt"] = m.group(1).replace('\\n', '\n').replace('\\"', '"') if m else ""
            # Type: cover, remix, or original
            m = _re.search(r'\\"type\\":\\"([^\\]+)\\"', html)
            if not m:
                m = _re.search(r'"type":"([^"]+)"', html)
            result["type"] = m.group(1) if m else ""
            return jsonify(result)
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # --- Party Playlist ---

    @app.route("/party-playlist", methods=["GET", "POST"])
    @permission_required('party_playlist')
    async def party_playlist():
        if request.method == "POST":
            form = await request.form
            action = form.get("action")

            if action == "submit_song":
                url = form.get("url", "").strip()
                if SUNO_URL_PATTERN.search(url):
                    song_title, artist, image_url = await _fetch_suno_info(url)
                elif YOUTUBE_URL_RE.search(url):
                    song_title, artist, image_url = await _fetch_youtube_info(url)
                elif ELEVENMUSIC_TRACK_RE.search(url):
                    track_id = ELEVENMUSIC_TRACK_RE.search(url).group(1).lower()
                    meta = await _fetch_elevenmusic_meta(track_id)
                    song_title = meta.get("title")
                    artist = meta.get("artist")
                    image_url = meta.get("image_url")
                else:
                    await flash("Invalid URL. Please provide a valid Suno, ElevenMusic or YouTube link.", "error")
                    return redirect(url_for("party_playlist"))
                await db.party_submit_song(
                    user_id=0,
                    user_name=artist or "Unknown Artist",
                    url=url,
                    song_title=song_title,
                    image_url=image_url,
                )
                return redirect(url_for("party_playlist"))

            elif action == "mark_heard":
                song_id = int(form.get("song_id", 0))
                await db.party_mark_heard(song_id)
                return redirect(request.referrer or url_for("party_playlist"))

            elif action == "mark_unheard":
                song_id = int(form.get("song_id", 0))
                await db.party_mark_unheard(song_id)
                return redirect(request.referrer or url_for("party_playlist"))

            elif action == "delete_song":
                song_id = int(form.get("song_id", 0))
                await db.db.execute("DELETE FROM party_playlist WHERE id = ?", (song_id,))
                await db.db.commit()
                return redirect(request.referrer or url_for("party_playlist"))

            elif action == "post_song":
                song_id = int(form.get("song_id", 0))
                channel_id_str = await db.get_setting("party_voice_channel")
                if not channel_id_str:
                    await flash("No post channel configured. Set it in Settings.", "error")
                    return redirect(url_for("party_playlist"))
                guild = get_guild()
                if not guild:
                    await flash("Bot is not connected to the guild.", "error")
                    return redirect(url_for("party_playlist"))
                channel = guild.get_channel(int(channel_id_str))
                if not channel:
                    await flash("Configured channel not found.", "error")
                    return redirect(url_for("party_playlist"))
                # Fetch song from DB
                songs = await db.party_get_all_songs()
                song = next((s for s in songs if s["id"] == song_id), None)
                if not song:
                    await flash("Song not found.", "error")
                    return redirect(url_for("party_playlist"))
                title = song.get("song_title") or "Unknown Title"
                artist = song.get("user_name") or "Unknown Artist"
                import discord
                embed = discord.Embed(
                    title=title,
                    url=song["url"],
                    description=f"by **{artist}**",
                    color=discord.Color.purple(),
                )
                if song.get("image_url"):
                    embed.set_thumbnail(url=song["image_url"])
                bot_name = await db.get_setting("bot_name") or "Slowmode Bot"
                embed.set_footer(text=f"{bot_name} — Listening Party")
                try:
                    await channel.send(embed=embed)
                    await flash(f"Posted \"{title}\" to #{channel.name}.", "success")
                except Exception as post_err:
                    await flash(f"Failed to post to #{channel.name}: {post_err}", "error")
                return redirect(url_for("party_playlist"))

            elif action == "save_playlist_url":
                playlist_url = form.get("playlist_url", "").strip()
                await db.set_setting("party_playlist_url", playlist_url)
                # Try to fetch playlist cover image
                playlist_image = ""
                if playlist_url:
                    try:
                        async with aiohttp.ClientSession() as sess:
                            async with sess.get(playlist_url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                                if resp.status == 200:
                                    html = await resp.text()
                                    img_match = re.search(r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\'>]+)["\']', html)
                                    if not img_match:
                                        img_match = re.search(r'<meta\s+content=["\']([^"\'>]+)["\']\s+property=["\']og:image["\']', html)
                                    if img_match:
                                        playlist_image = img_match.group(1).strip()
                    except Exception:
                        pass
                await db.set_setting("party_playlist_image", playlist_image)
                if playlist_url:
                    await flash("Playlist URL saved.", "success")
                else:
                    await flash("Playlist URL cleared.", "success")
                return redirect(url_for("party_playlist"))

            elif action == "post_playlist_url":
                playlist_url = await db.get_setting("party_playlist_url")
                if not playlist_url:
                    await flash("No playlist URL set.", "error")
                    return redirect(url_for("party_playlist"))
                channel_id_str = await db.get_setting("party_voice_channel")
                if not channel_id_str:
                    await flash("No post channel configured. Set it in Settings.", "error")
                    return redirect(url_for("party_playlist"))
                guild = get_guild()
                if not guild:
                    await flash("Bot is not connected to the guild.", "error")
                    return redirect(url_for("party_playlist"))
                channel = guild.get_channel(int(channel_id_str))
                if not channel:
                    await flash("Configured channel not found.", "error")
                    return redirect(url_for("party_playlist"))
                import discord
                embed = discord.Embed(
                    title="\U0001F3B6 Listening Party Playlist",
                    url=playlist_url,
                    description=f"Check out the full playlist and give it a like!\n{playlist_url}",
                    color=discord.Color.purple(),
                )
                playlist_image = await db.get_setting("party_playlist_image")
                if playlist_image:
                    embed.set_thumbnail(url=playlist_image)
                bot_name = await db.get_setting("bot_name") or "Slowmode Bot"
                embed.set_footer(text=f"{bot_name} \u2014 Listening Party")
                try:
                    await channel.send(embed=embed)
                    await flash(f"Playlist posted to #{channel.name}.", "success")
                except Exception as post_err:
                    await flash(f"Failed to post to #{channel.name}: {post_err}", "error")
                return redirect(url_for("party_playlist"))

            elif action == "reset":
                await db.party_reset()
                await db.set_setting("party_playlist_url", "")
                await db.set_setting("party_playlist_image", "")
                return redirect(url_for("party_playlist"))

        # GET
        import traceback
        try:
            songs = await db.party_get_all_songs()
            party_playlist_url = await db.get_setting("party_playlist_url") or ""

            heard_count = sum(1 for s in songs if s["heard"])
            unheard_count = len(songs) - heard_count
            unheard_songs = [s for s in songs if not s["heard"]]

            filter_status = request.args.get("filter")
            if filter_status == "heard":
                display_songs = [s for s in songs if s["heard"]]
            elif filter_status == "unheard":
                display_songs = [s for s in songs if not s["heard"]]
            else:
                display_songs = songs
                filter_status = None

            return await render_template(
                "party_playlist.html",
                songs=songs,
                display_songs=display_songs,
                unheard_songs=unheard_songs,
                heard_count=heard_count,
                unheard_count=unheard_count,
                filter_status=filter_status,
                party_playlist_url=party_playlist_url,
            )
        except Exception as e:
            traceback.print_exc()
            return f"<pre>Error: {e}\n\n{traceback.format_exc()}</pre>", 500

    # --- Party Playlist JSON API (used by suno_info.html) ---

    @app.route("/api/party-playlist/songs")
    @permission_required('party_playlist')
    async def api_party_playlist_songs():
        from quart import jsonify
        songs = await db.party_get_all_songs()
        return jsonify([dict(s) for s in songs])

    @app.route("/api/party-playlist/heard/<int:song_id>", methods=["POST"])
    @permission_required('party_playlist')
    async def api_party_mark_heard(song_id):
        from quart import jsonify
        await db.party_mark_heard(song_id)
        return jsonify({"ok": True})

    @app.route("/api/party-playlist/unheard/<int:song_id>", methods=["POST"])
    @permission_required('party_playlist')
    async def api_party_mark_unheard(song_id):
        from quart import jsonify
        await db.party_mark_unheard(song_id)
        return jsonify({"ok": True})

    @app.route("/api/party-playlist/song/<int:song_id>", methods=["DELETE"])
    @permission_required('party_playlist')
    async def api_party_delete_song(song_id):
        from quart import jsonify
        await db.db.execute("DELETE FROM party_playlist WHERE id = ?", (song_id,))
        await db.db.commit()
        return jsonify({"ok": True})

    @app.route("/api/party-playlist/submit", methods=["POST"])
    @permission_required('party_playlist')
    async def api_party_submit_song():
        from quart import jsonify
        data = await request.get_json(silent=True) or {}
        url = (data.get("url") or "").strip()
        if not url:
            return jsonify({"ok": False, "error": "Please enter a URL"}), 400
        if SUNO_URL_PATTERN.search(url):
            song_title, artist, image_url = await _fetch_suno_info(url)
        elif YOUTUBE_URL_RE.search(url):
            song_title, artist, image_url = await _fetch_youtube_info(url)
        elif ELEVENMUSIC_TRACK_RE.search(url):
            track_id = ELEVENMUSIC_TRACK_RE.search(url).group(1).lower()
            meta = await _fetch_elevenmusic_meta(track_id)
            song_title = meta.get("title")
            artist = meta.get("artist")
            image_url = meta.get("image_url")
        else:
            return jsonify({"ok": False, "error": "Please enter a valid Suno, ElevenMusic or YouTube URL"}), 400
        await db.party_submit_song(
            user_id=0,
            user_name=artist or "Unknown Artist",
            url=url,
            song_title=song_title,
            image_url=image_url,
        )
        return jsonify({"ok": True, "song_title": song_title, "artist": artist})

    @app.route("/api/party-playlist/reset", methods=["POST"])
    @permission_required('party_playlist')
    async def api_party_reset():
        from quart import jsonify
        await db.party_reset()
        await db.set_setting("party_playlist_url", "")
        await db.set_setting("party_playlist_image", "")
        return jsonify({"ok": True})

    @app.route("/api/party-playlist/post/<int:song_id>", methods=["POST"])
    @permission_required('party_playlist')
    async def api_party_post_song(song_id):
        from quart import jsonify
        import discord as _discord
        songs = await db.party_get_all_songs()
        song = next((s for s in songs if s["id"] == song_id), None)
        if not song:
            return jsonify({"ok": False, "error": "Song not found"}), 404
        channel_id_str = await db.get_setting("party_voice_channel")
        if not channel_id_str:
            return jsonify({"ok": False, "error": "No post channel configured"}), 400
        guild = get_guild()
        if not guild:
            return jsonify({"ok": False, "error": "Bot not connected"}), 503
        channel = guild.get_channel(int(channel_id_str))
        if not channel:
            return jsonify({"ok": False, "error": "Channel not found"}), 404
        title = song.get("song_title") or "Unknown Title"
        artist = song.get("user_name") or "Unknown Artist"
        embed = _discord.Embed(
            title=title, url=song["url"],
            description=f"by **{artist}**",
            color=_discord.Color.purple(),
        )
        if song.get("image_url"):
            embed.set_thumbnail(url=song["image_url"])
        bot_name = await db.get_setting("bot_name") or "Slowmode Bot"
        embed.set_footer(text=f"{bot_name} — Listening Party")
        try:
            await channel.send(embed=embed)
            return jsonify({"ok": True, "channel": channel.name})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    # --- Suno Promotion (per logged-in web user) ---

    SUNO_PROFILE_PATTERN = re.compile(r'^https?://(?:www\.)?suno\.com/@([A-Za-z0-9_.\-]+)/?$')

    def _normalize_suno_profile_url(raw: str) -> tuple[str | None, str | None]:
        """Returns (canonical_url, handle_lower) or (None, None) if invalid."""
        if not raw:
            return None, None
        raw = raw.strip()
        # Allow bare handle input "@tarja_ravenveil" or "tarja_ravenveil"
        if raw.startswith("@"):
            raw = "https://suno.com/" + raw
        elif not raw.startswith("http") and re.fullmatch(r'[A-Za-z0-9_.\-]+', raw):
            raw = f"https://suno.com/@{raw}"
        m = SUNO_PROFILE_PATTERN.match(raw)
        if not m:
            return None, None
        handle = m.group(1)
        canonical = f"https://suno.com/@{handle}"
        return canonical, handle.lower()

    def _extract_suno_escaped_field(block: str, key: str) -> str | None:
        marker = f'\\"{key}\\":\\"'
        start = block.find(marker)
        if start < 0:
            return None
        pos = start + len(marker)
        chars: list[str] = []
        while pos < len(block):
            char = block[pos]
            if char == "\\":
                nxt = block[pos + 1] if pos + 1 < len(block) else ""
                chars.append(char)
                if nxt:
                    chars.append(nxt)
                    pos += 2
                    continue
            if char == '"':
                backslashes = 0
                idx = pos - 1
                while idx >= 0 and block[idx] == "\\":
                    backslashes += 1
                    idx -= 1
                if backslashes % 2 == 1:
                    chars.append(char)
                    pos += 1
                    continue
                return "".join(chars)
            chars.append(char)
            pos += 1
        return None

    def _extract_profile_song_cards(page: str, limit: int = 3) -> list[dict]:
        """Extract the real profile song feed, excluding pinned/profile hero songs."""
        start = page.find(r'\"content_id\":\"songs_feed\"')
        if start < 0:
            start = page.find("songs_feed")
        if start < 0:
            return []

        sub = page[start : start + 260000]
        marker = r'\"content_type\":\"clip\",\"content_item\":{'
        idx = 0
        songs: list[dict] = []
        seen: set[str] = set()
        while len(songs) < limit:
            pos = sub.find(marker, idx)
            if pos < 0:
                break
            block = sub[pos : pos + 22000]
            entity = re.search(r'\\"entity_type\\":\\"([^\\"]+)\\"', block)
            status = re.search(r'\\"status\\":\\"([^\\"]+)\\"', block)
            title = _extract_suno_escaped_field(block, "title")
            song_id = re.search(r'\\"id\\":\\"([a-f0-9-]{36})\\"', block, flags=re.I)
            created = re.search(r'\\"created_at\\":\\"([^\\"]+)\\"', block)
            if (
                title
                and song_id
                and song_id.group(1) not in seen
                and (not entity or entity.group(1) == "song_schema")
                and (not status or status.group(1) == "complete")
            ):
                sid = song_id.group(1)
                seen.add(sid)
                songs.append(
                    {
                        "id": sid,
                        "title": _decode_suno_json_string(title),
                        "url": f"https://suno.com/song/{sid}",
                        "created_at": created.group(1) if created else "",
                    }
                )
            idx = pos + len(marker)
        return songs

    async def _fetch_suno_profile(profile_url: str) -> dict:
        """Fetch a Suno profile page and extract display name, avatar and latest real song.

        Returns dict with keys:
          - display_name, avatar_url
          - latest_song_url, latest_song_title
        Best-effort; missing fields are None.
        """
        import html as _html
        out = {
            "display_name": None,
            "avatar_url": None,
            "pinned_song_url": None,
            "pinned_song_title": None,
            "latest_song_url": None,
            "latest_song_title": None,
        }
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(profile_url, timeout=aiohttp.ClientTimeout(total=20)) as response:
                if response.status != 200:
                    print(f"[suno_promotion] HTTP {response.status} for {profile_url}")
                    return out
                html = await response.text()
            if not html:
                return out

        # og:title — typically "Display Name | Suno" or just "Display Name"
        m = re.search(
            r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']+)["\']', html
        )
        if not m:
            m = re.search(
                r'<meta\s+content=["\']([^"\']+)["\']\s+property=["\']og:title["\']', html
            )
        if m:
            raw = _html.unescape(m.group(1).strip())
            raw = re.sub(r'\s*[|\-–]\s*Suno\s*$', '', raw).strip()
            # Some pages format as "Display Name | Suno AI" or "Display Name's profile"
            raw = re.sub(r"\s*'?s\s+(?:Suno\s+)?profile\s*$", '', raw, flags=re.IGNORECASE)
            if raw:
                out["display_name"] = raw

        # Fallback: <title>
        if not out["display_name"]:
            m = re.search(r'<title>([^<]+)</title>', html)
            if m:
                raw = _html.unescape(m.group(1).strip())
                raw = re.sub(r'\s*[|\-–]\s*Suno\s*$', '', raw).strip()
                if raw:
                    out["display_name"] = raw

        # og:image — avatar
        m = re.search(
            r'<meta\s+property=["\']og:image["\']\s+content=["\']([^"\']+)["\']', html
        )
        if not m:
            m = re.search(
                r'<meta\s+content=["\']([^"\']+)["\']\s+property=["\']og:image["\']', html
            )
        if m:
            out["avatar_url"] = m.group(1).strip()

        # Avatar fallback: first cdn2.suno.ai/<uuid>.jpeg image referenced in HTML
        if not out["avatar_url"]:
            m = re.search(r'https://cdn[12]\.suno\.ai/([a-f0-9-]{36})\.jpeg', html)
            if m:
                out["avatar_url"] = m.group(0)

        songs = _extract_profile_song_cards(html, 3)
        if songs:
            latest = songs[0]
            out["latest_song_url"] = latest["url"]
            out["latest_song_title"] = latest["title"]
            out["latest_songs"] = songs
            print(f"[suno_promotion] Latest real song for {profile_url}: {latest['title']!r}")
        else:
            out["latest_songs"] = []
            print(f"[suno_promotion] No songs_feed songs found for {profile_url}")

        return out

    async def _refresh_suno_promotion_entries(owner_id: int, entries: list[dict], mode: str = "sequential") -> tuple[int, int]:
        parallel = mode == "parallel3"
        semaphore = asyncio.Semaphore(3 if parallel else 1)

        async def fetch_entry(entry: dict) -> tuple[dict, dict | None, Exception | None]:
            async with semaphore:
                try:
                    return entry, await _fetch_suno_profile(entry["profile_url"]), None
                except Exception as exc:
                    return entry, None, exc

        refreshed = 0
        errors = 0
        results = await asyncio.gather(*(fetch_entry(entry) for entry in entries))
        for entry, info, exc in results:
            if exc or not info:
                print(f"[suno_promotion] Refresh failed for {entry.get('handle')}: {exc}")
                errors += 1
                continue
            latest_song_url = info.get("latest_song_url") or entry.get("latest_song_url")
            latest_song_title = info.get("latest_song_title") or entry.get("latest_song_title")
            handled_song_url = entry.get("last_song_url")
            is_done = bool(latest_song_url and handled_song_url and latest_song_url == handled_song_url)
            await db.suno_userlist_update_meta(
                owner_user_id=owner_id,
                entry_id=entry["id"],
                display_name=info.get("display_name") or entry.get("display_name"),
                avatar_url=info.get("avatar_url") or entry.get("avatar_url"),
                last_song_url=entry.get("last_song_url"),
                last_song_title=entry.get("last_song_title"),
                pinned_song_url=None,
                pinned_song_title=None,
                latest_song_url=latest_song_url,
                latest_song_title=latest_song_title,
                done=is_done,
            )
            refreshed += 1
        return refreshed, errors

    @app.route("/suno-promotion", methods=["GET", "POST"])
    @permission_required('suno_promotion')
    async def suno_promotion():
        owner_id = session["user_id"]
        if request.method == "POST":
            form = await request.form
            action = form.get("action")

            if action == "add":
                raw_url = (form.get("profile_url") or "").strip()
                priority = form.get("priority", "medium")
                if priority not in ("high", "medium", "low"):
                    priority = "medium"
                canonical, handle = _normalize_suno_profile_url(raw_url)
                if not canonical:
                    await flash("Invalid Suno profile URL. Expected: https://suno.com/@handle", "error")
                else:
                    info = await _fetch_suno_profile(canonical)
                    new_id = await db.suno_userlist_add(
                        owner_user_id=owner_id,
                        profile_url=canonical,
                        handle=handle,
                        display_name=info.get("display_name"),
                        avatar_url=info.get("avatar_url"),
                        last_song_url=None,
                        last_song_title=None,
                        pinned_song_url=None,
                        pinned_song_title=None,
                        latest_song_url=info.get("latest_song_url"),
                        latest_song_title=info.get("latest_song_title"),
                        priority=priority,
                    )
                    if new_id is None:
                        await flash(f"@{handle} is already in your list.", "error")
                    else:
                        await flash(f"Added @{handle}.", "success")

            elif action == "delete":
                entry_id = int(form.get("entry_id", "0"))
                if await db.suno_userlist_delete(owner_id, entry_id):
                    await flash("Entry removed.", "success")

            elif action == "set_priority":
                entry_id = int(form.get("entry_id", "0"))
                priority = form.get("priority", "medium")
                await db.suno_userlist_set_priority(owner_id, entry_id, priority)

            elif action == "mark_done":
                entry_id = int(form.get("entry_id", "0"))
                await db.suno_userlist_set_done(owner_id, entry_id, True)

            elif action == "mark_undone":
                entry_id = int(form.get("entry_id", "0"))
                await db.suno_userlist_set_done(owner_id, entry_id, False)

            elif action == "pause":
                entry_id = int(form.get("entry_id", "0"))
                await db.suno_userlist_set_paused(owner_id, entry_id, True)

            elif action == "unpause":
                entry_id = int(form.get("entry_id", "0"))
                await db.suno_userlist_set_paused(owner_id, entry_id, False)

            elif action == "edit_url":
                entry_id = int(form.get("entry_id", "0"))
                raw_url = (form.get("profile_url") or "").strip()
                canonical, handle = _normalize_suno_profile_url(raw_url)
                if not canonical:
                    await flash("Invalid Suno profile URL.", "error")
                else:
                    info = await _fetch_suno_profile(canonical)
                    ok = await db.suno_userlist_update_url(
                        owner_user_id=owner_id,
                        entry_id=entry_id,
                        profile_url=canonical,
                        handle=handle,
                        display_name=info.get("display_name"),
                        avatar_url=info.get("avatar_url"),
                        last_song_url=None,
                        last_song_title=None,
                        pinned_song_url=None,
                        pinned_song_title=None,
                        latest_song_url=info.get("latest_song_url"),
                        latest_song_title=info.get("latest_song_title"),
                    )
                    if ok:
                        await db.suno_userlist_set_done(owner_id, entry_id, False)
                        await flash(f"Entry updated: @{handle}.", "success")
                    else:
                        await flash("Update failed (handle already in list?).", "error")

            elif action == "reset_cycle":
                refresh_mode = form.get("refresh_mode", "sequential")
                if refresh_mode not in ("sequential", "parallel3"):
                    refresh_mode = "sequential"
                affected = await db.suno_userlist_reset_done(owner_id)
                all_entries = await db.suno_userlist_list(owner_id)
                refreshed, errors = await _refresh_suno_promotion_entries(owner_id, all_entries, refresh_mode)
                if affected or refreshed:
                    await flash(
                        f"Cycle ended — {affected} entr{'y' if affected == 1 else 'ies'} reopened; "
                        f"refreshed {refreshed} latest songs ({errors} errors).",
                        "success",
                    )
                else:
                    await flash("No entries to reset or refresh.", "error")

            elif action == "set_latest":
                entry_id = int(form.get("entry_id", "0"))
                raw_url = (form.get("latest_song_url") or "").strip()
                # Accept full suno.com/song/UUID or bare UUID
                m = re.search(r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}', raw_url)
                if not m:
                    await flash("Invalid song URL.", "error")
                else:
                    song_uuid = m.group(0)
                    canonical_song_url = f"https://suno.com/song/{song_uuid}"
                    # Try to fetch title from song page
                    try:
                        meta = await _fetch_suno_meta(song_uuid)
                        song_title = meta.get("title") if meta else None
                    except Exception:
                        song_title = None
                    entry = await db.suno_userlist_get(owner_id, entry_id)
                    if entry:
                        await db.suno_userlist_update_meta(
                            owner_user_id=owner_id,
                            entry_id=entry_id,
                            display_name=entry.get("display_name"),
                            avatar_url=entry.get("avatar_url"),
                            last_song_url=entry.get("last_song_url"),
                            last_song_title=entry.get("last_song_title"),
                            pinned_song_url=None,
                            pinned_song_title=None,
                            latest_song_url=canonical_song_url,
                            latest_song_title=song_title,
                            done=bool(entry.get("last_song_url") and entry.get("last_song_url") == canonical_song_url),
                        )
                        await flash(f"Latest song set to: {song_title or song_uuid}", "success")

            elif action == "refresh":
                entry_id = int(form.get("entry_id", "0"))
                entry = await db.suno_userlist_get(owner_id, entry_id)
                if entry:
                    await _refresh_suno_promotion_entries(owner_id, [entry], "sequential")

            elif action == "refresh_all":
                all_entries = await db.suno_userlist_list(owner_id)
                refreshed, errors = await _refresh_suno_promotion_entries(owner_id, all_entries, "parallel3")
                await flash(f"Refreshed {refreshed} entries ({errors} errors).", "success")

            # Preserve current filter state
            qs = {k: v for k, v in (await request.form).items()
                  if k in ("filter_priority", "filter_status", "hide_paused")}
            return redirect(url_for("suno_promotion", **qs))

        # GET — apply filters
        all_entries = await db.suno_userlist_list(owner_id)
        filter_priority = request.args.get("filter_priority", "all")
        filter_status = request.args.get("filter_status", "open")  # open | done | paused | all
        hide_paused = request.args.get("hide_paused", "0") == "1"

        def keep(e):
            if filter_priority in ("high", "medium", "low") and e["priority"] != filter_priority:
                return False
            if filter_status == "open" and (e["done"] or e["paused"]):
                return False
            if filter_status == "done" and not e["done"]:
                return False
            if filter_status == "paused" and not e["paused"]:
                return False
            if filter_status == "all" and hide_paused and e["paused"]:
                return False
            return True

        entries = [e for e in all_entries if keep(e)]
        total = len(all_entries)
        open_count = sum(1 for e in all_entries if not e["done"])
        done_count = total - open_count
        paused_count = sum(1 for e in all_entries if e["paused"])

        return await render_template(
            "suno_promotion.html",
            entries=entries,
            total=total,
            open_count=open_count,
            done_count=done_count,
            paused_count=paused_count,
            shown_count=len(entries),
            filter_priority=filter_priority,
            filter_status=filter_status,
            hide_paused=hide_paused,
        )

    # --- Suno Info -----------------------------------------------------------
    #
    # Loads a public Suno playlist URL and shows every song in a table, with
    # an integrated player (audio + optional video cover), full lyrics, and
    # the style prompt / tags used to generate the song.

    @app.route("/suno-info")
    @permission_required('suno_info')
    async def suno_info():
        import json as _json
        _user = await db.get_web_user_by_id(session["user_id"])
        _perms = []
        try: _perms = _json.loads(_user.get("permissions") or "[]")
        except (ValueError, TypeError): pass
        has_party = bool(_user.get("is_admin")) or "party_playlist" in _perms
        discord_connection = await db.get_player_discord_connection(session["user_id"])
        client_id, client_secret = await _player_discord_oauth_credentials()
        return await render_template("suno_info.html",
            channels=await _get_player_channels(), has_party=has_party,
            discord_connection=discord_connection,
            discord_oauth_ready=bool(client_id and client_secret))

    @app.route("/api/suno-info/player-discord-status")
    @permission_required('suno_info')
    async def api_suno_info_player_discord_status():
        from quart import jsonify

        connection = await db.get_player_discord_connection(session["user_id"])
        message_id = request.args.get("message_id", "")
        emojis = []
        if connection and message_id.isdigit():
            emojis = await db.get_player_user_reactions(
                int(message_id), int(connection["discord_user_id"])
            )
        return jsonify({
            "connected": bool(connection),
            "emojis": emojis,
        })

    @app.route("/api/suno-info/player-react", methods=["POST"])
    @permission_required('suno_info')
    async def api_suno_info_player_react():
        from quart import jsonify

        connection = await db.get_player_discord_connection(session["user_id"])
        if not connection:
            return jsonify({"error": "Connect Discord to use reactions"}), 401

        data = await request.get_json(silent=True) or {}
        message_id = str(data.get("message_id") or "")
        emoji = data.get("emoji", "")
        if not message_id.isdigit() or emoji not in PLAYER_REACTION_EMOJIS:
            return jsonify({"error": "Missing message_id or emoji"}), 400

        message_id_int = int(message_id)
        song_post = await db.get_song_post_by_message_id(message_id_int)
        if not song_post:
            return jsonify({"error": "Unknown song message"}), 404

        lock = app.player_reaction_locks.setdefault(message_id_int, asyncio.Lock())
        async with lock:
            added = await db.toggle_player_song_reaction(
                message_id=message_id_int,
                channel_id=int(song_post["channel_id"]),
                web_user_id=session["user_id"],
                discord_user_id=int(connection["discord_user_id"]),
                discord_display_name=connection["discord_display_name"],
                emoji=emoji,
            )
            if added:
                await db.add_song_reaction(
                    message_id=message_id_int,
                    channel_id=int(song_post["channel_id"]),
                    song_url=song_post["url"],
                    post_author_id=int(song_post["user_id"]),
                    reactor_user_id=int(connection["discord_user_id"]),
                    reactor_user_name=connection["discord_display_name"],
                    emoji=emoji,
                    song_title=song_post.get("song_title"),
                    source="player",
                )
            elif not await db.has_player_song_reaction(
                message_id_int, int(connection["discord_user_id"]), emoji
            ):
                await db.remove_sourced_song_reaction(
                    message_id_int,
                    int(connection["discord_user_id"]),
                    emoji,
                    ("player", "public_player"),
                )
            thread_ok, thread_error = await _update_player_reaction_summary(song_post)
            if not thread_ok:
                print(f"[suno-info-player-react] {thread_error}", flush=True)
            return jsonify({
                "ok": True,
                "active": added,
                "thread": thread_ok,
                "warning": thread_error,
            })

    @app.route("/api/suno-info/exp-radio-playlists")
    @permission_required('suno_info')
    async def api_suno_info_exp_radio_playlists():
        """List or load historical Experimental Radio playlist snapshots."""
        import json
        from quart import jsonify

        legacy_keys = {
            "legacy-scheduled": "exp_radio_last_scheduled_playlist_snapshot",
            "legacy-latest": "exp_radio_last_playlist_snapshot",
        }

        async def _legacy_snapshot(snapshot_ref: str) -> dict | None:
            key = legacy_keys.get(snapshot_ref)
            if not key:
                return None
            raw = await db.get_setting(key, "")
            if not raw:
                return None
            try:
                payload = json.loads(raw)
            except (TypeError, ValueError):
                return None
            urls = [
                str(url).strip()
                for url in (payload.get("urls") or [])
                if url and str(url).strip()
            ]
            return {
                "id": snapshot_ref,
                "created_at": float(payload.get("created_at") or 0),
                "source": str(payload.get("source") or ""),
                "scheduled": bool(payload.get("scheduled")),
                "song_count": len(urls),
                "urls": urls,
            }

        snapshot_ref = (request.args.get("snapshot") or "").strip()
        if snapshot_ref:
            snapshot = None
            if snapshot_ref.startswith("db-") and snapshot_ref[3:].isdigit():
                snapshot = await db.get_exp_radio_playlist_snapshot(int(snapshot_ref[3:]))
                if snapshot:
                    snapshot["id"] = snapshot_ref
            elif snapshot_ref in legacy_keys:
                snapshot = await _legacy_snapshot(snapshot_ref)
            if not snapshot:
                return jsonify({"error": "Playlist snapshot not found"}), 404
            return jsonify(snapshot)

        stored = await db.get_exp_radio_playlist_snapshots(limit=100)
        snapshots = [
            {
                "id": f"db-{row['id']}",
                "created_at": float(row.get("created_at") or 0),
                "source": row.get("source") or "",
                "scheduled": bool(row.get("scheduled")),
                "song_count": int(row.get("song_count") or 0),
            }
            for row in stored
        ]
        known = {
            (
                int(item["created_at"]), item["source"],
                item["scheduled"], item["song_count"],
            )
            for item in snapshots
        }
        for legacy_ref in ("legacy-scheduled", "legacy-latest"):
            legacy = await _legacy_snapshot(legacy_ref)
            if not legacy:
                continue
            identity = (
                int(legacy["created_at"]), legacy["source"],
                legacy["scheduled"], legacy["song_count"],
            )
            if identity in known:
                continue
            legacy.pop("urls", None)
            snapshots.append(legacy)
            known.add(identity)
        snapshots.sort(key=lambda item: item["created_at"], reverse=True)
        return jsonify({"snapshots": snapshots})

    @app.route("/api/suno-info/playlist")
    @permission_required('suno_info')
    async def api_suno_info_playlist():
        """Parse a Suno playlist URL or ElevenMusic track URL."""
        import aiohttp as _aiohttp, html as _html
        from quart import jsonify
        from bot.stream_manager import parse_suno_playlist
        url = (request.args.get("url") or "").strip()
        if not url:
            return jsonify({"error": "missing url"}), 400

        eleven_match = ELEVENMUSIC_TRACK_RE.search(url)
        if eleven_match:
            track_id = eleven_match.group(1).lower()
            meta = await _fetch_elevenmusic_meta(track_id)
            song = {
                "uuid": track_id,
                "type": "elevenmusic",
                "title": meta.get("title") or track_id[:8],
                "artist": meta.get("artist") or "",
                "image_url": meta.get("image_url") or "",
                "audio_url": meta.get("audio_url") or "",
                "duration": meta.get("duration"),
                "source_url": meta.get("source_url") or url,
                "_meta": meta,
            }
            return jsonify({"songs": [song], "name": "ElevenMusic"})

        try:
            songs = await parse_suno_playlist(url)
        except Exception as e:
            return jsonify({"error": f"parse failed: {e}"}), 502
        if not songs:
            return jsonify({"error": "no songs found in playlist"}), 404

        # Best-effort: fetch the playlist's display name from the page's
        # og:title — independent of which scraping path returned the songs.
        name = ""
        try:
            async with _aiohttp.ClientSession() as sess:
                async with sess.get(
                    url,
                    headers={"User-Agent": "Mozilla/5.0 (X11; Linux) AppleWebKit/537.36"},
                    timeout=_aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        page = await resp.text()
                        m = re.search(
                            r'<meta\s+property="og:title"\s+content="([^"]+)"',
                            page,
                        )
                        if not m:
                            m = re.search(r'<title>([^<]+)</title>', page)
                        if m:
                            raw = _html.unescape(m.group(1).strip())
                            # strip trailing "| Suno"
                            raw = re.sub(r'\s*[|\-\u2013]\s*Suno\s*$', '', raw).strip()
                            name = raw
        except Exception:
            pass
        return jsonify({"songs": songs, "name": name})

    @app.route("/api/suno-info/eleven-song/<track_id>")
    @permission_required('suno_info')
    async def api_suno_info_eleven_song(track_id):
        """Return public metadata for a single ElevenMusic track."""
        from quart import jsonify

        if not re.fullmatch(r"[A-Fa-f0-9]{24}", track_id or ""):
            return jsonify({"error": "invalid track id"}), 400
        return jsonify(await _fetch_elevenmusic_meta(track_id.lower()))

    @app.route("/api/suno-info/song/<uuid>")
    @permission_required('suno_info')
    async def api_suno_info_song(uuid):
        """Return rich metadata for a single Suno song: title, artist, cover,
        video_url, lyrics, style prompt, tags, model, stats."""
        import aiohttp as _aiohttp, re as _re, html as _html
        from quart import jsonify

        # Reuse the embed-based helper for the basics (lyrics, image, video).
        base = await _fetch_suno_meta(uuid)

        # Now fetch the public song page for richer fields (gpt_description_prompt,
        # tags, model, stats). This is the same scraping strategy used in the
        # Suno Analyzer route.
        url = f"https://suno.com/song/{uuid}"
        headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"}
        result: dict = {
            "uuid": uuid,
            "title": base.get("title"),
            "artist": base.get("artist"),
            "image_url": base.get("image_url"),
            "video_url": base.get("video_url"),
            "lyrics": base.get("lyrics"),
            "handle": base.get("handle"),
            "tags": [],
            "prompt": "",
            "model": "",
            "type": "",
            "plays": None,
            "likes": None,
            "created_at": "",
            "duration": None,
        }
        try:
            async with _aiohttp.ClientSession() as sess:
                async with sess.get(url, headers=headers,
                                    timeout=_aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        html = await resp.text()
                    else:
                        html = ""
        except Exception:
            html = ""

        if html:
            # Tags / genre — comma-separated string in JSON
            m = _re.search(r'\\"tags\\":\\"([^\\]+)\\"', html)
            if not m:
                m = _re.search(r'"tags":"([^"]+)"', html)
            if m:
                result["tags"] = [
                    t.strip() for t in m.group(1).split(",") if t.strip()
                ]
            # Style prompt — usually inline, but for longer prompts (or with
            # emojis) Suno stores it as an RSC chunk reference like `$3d`.
            m = _re.search(
                r'\\"gpt_description_prompt\\":\\"((?:[^\\]|\\.)*?)\\"', html
            )
            if not m:
                m = _re.search(
                    r'"gpt_description_prompt":"((?:[^"\\]|\\.)*)"', html
                )
            if m:
                raw_prompt = m.group(1).replace('\\n', '\n').replace('\\"', '"')
            else:
                raw_prompt = ""
            if raw_prompt and not _RSC_REF_RE.match(raw_prompt):
                result["prompt"] = raw_prompt
            else:
                # Resolve RSC reference from the flight payload
                try:
                    import json as _json
                    chunks = _re.findall(
                        r'self\.__next_f\.push\(\[1,"(.*?)"\]\)',
                        html, _re.DOTALL,
                    )
                    decoded = []
                    for c in chunks:
                        try:
                            decoded.append(_json.loads('"' + c + '"'))
                        except Exception:
                            decoded.append(
                                c.replace('\\n', '\n')
                                 .replace('\\"', '"')
                                 .replace('\\\\', '\\')
                            )
                    full = "".join(decoded)
                    mref = _re.search(
                        r'"gpt_description_prompt":"\$([0-9a-f]+)"', full
                    )
                    if mref:
                        ref = mref.group(1)
                        # The hex prefix is the UTF-8 byte length of the
                        # payload — slice on a bytes view to avoid
                        # over-capturing on emoji / non-ASCII characters.
                        full_bytes = full.encode("utf-8")
                        tpat_b = _re.compile(
                            rb'(?:^|\n)' + _re.escape(ref).encode() +
                            rb':T([0-9a-f]+),'
                        )
                        tfnd = tpat_b.search(full_bytes)
                        if tfnd:
                            length = int(tfnd.group(1), 16)
                            b_start = tfnd.end()
                            result["prompt"] = full_bytes[b_start:b_start + length] \
                                .decode("utf-8", errors="replace").rstrip()
                except Exception:
                    pass
            # Stats
            for key, pat in [
                ("plays", r'\\"play_count\\":(\d+)'),
                ("likes", r'\\"upvote_count\\":(\d+)'),
            ]:
                m = _re.search(pat, html)
                if m:
                    result[key] = int(m.group(1))
            # Model + type + created_at
            m = _re.search(r'\\"major_model_version\\":\\"([^\\]+)\\"', html)
            if m:
                result["model"] = m.group(1)
            m = _re.search(r'\\"type\\":\\"([^\\]+)\\"', html)
            if m:
                result["type"] = m.group(1)
            m = _re.search(r'\\"created_at\\":\\"([^\\]+)\\"', html)
            if m:
                result["created_at"] = m.group(1)
            # Duration in seconds (float). Stored as `audio_duration` or `duration`.
            for pat in (
                r'\\"audio_duration\\":([0-9.]+)',
                r'\\"duration\\":([0-9.]+)',
                r'"audio_duration":([0-9.]+)',
                r'"duration":([0-9.]+)',
            ):
                m = _re.search(pat, html)
                if m:
                    try:
                        result["duration"] = float(m.group(1))
                    except ValueError:
                        pass
                    break
            # Author fallback if og:description didn't have it
            if not result.get("artist"):
                m = _re.search(r'\\"display_name\\":\\"([^\\]+)\\"', html)
                if m:
                    result["artist"] = m.group(1)

        # Final image fallback to the standard Suno CDN path
        if not result.get("image_url"):
            result["image_url"] = f"https://cdn1.suno.ai/image_large_{uuid}.jpeg"
        return jsonify(result)

    @app.route("/api/translate-lyrics", methods=["POST"])
    @login_required
    async def api_translate_lyrics():
        from quart import jsonify
        import asyncio
        data = await request.get_json(silent=True) or {}
        text = (data.get("text") or "").strip()
        lang = (data.get("lang") or "").strip().upper()
        if not text or lang not in ("DE", "EN", "FR", "ES", "JA", "IT", "PT"):
            return jsonify({"ok": False, "error": "Invalid input"}), 400
        try:
            from deep_translator import GoogleTranslator
            loop = asyncio.get_event_loop()

            # Split at paragraph/stanza boundaries (blank lines) so each stanza
            # gets independent language detection — essential for mixed-language
            # lyrics (e.g. English intro + Finnish verses in the same song).
            import re as _re
            # Preserve blank lines as empty entries so we can reassemble later
            raw_paragraphs = _re.split(r'(\n\s*\n)', text)
            MAX_CHUNK = 4000
            chunks = []
            separators = []  # parallel list of separators between chunks
            cur_text = ''
            cur_sep = ''
            for i, part in enumerate(raw_paragraphs):
                if _re.fullmatch(r'\n\s*\n', part):
                    cur_sep = part  # this is a separator, hold it
                    continue
                candidate = cur_text + (cur_sep if cur_text else '') + part
                if cur_text and len(candidate) > MAX_CHUNK:
                    chunks.append(cur_text)
                    separators.append(cur_sep)
                    cur_text = part
                    cur_sep = ''
                else:
                    cur_text = candidate
                    cur_sep = ''
            if cur_text:
                chunks.append(cur_text)

            # Helpers for per-chunk translation
            import unicodedata as _ud
            _LETTER_RE = _re.compile(
                r'[a-zA-ZÀ-ÿ\u0100-\u024F\u0370-\u03FF\u0400-\u04FF\u0600-\u06FF]'
            )
            _SEC_RE = _re.compile(r'^\s*\[[^\]]+\]\s*$')

            def _clean(text: str) -> str:
                """NFKC-normalize and strip invisible Unicode format characters
                (zero-width joiners, BOM, soft-hyphens, etc.) that can confuse
                Google Translate's language detector."""
                text = _ud.normalize("NFKC", text)
                return ''.join(
                    ch for ch in text
                    if ch in ' \t\n\r' or not _ud.category(ch).startswith('C')
                )

            def _is_structural(line: str) -> bool:
                s = line.strip()
                if not s or _SEC_RE.match(s):
                    return True
                return len(_LETTER_RE.findall(s)) / len(s) < 0.45

            async def _do_translate(text: str) -> str:
                """Translate text; if result is unchanged, retry with source=de."""
                result = await loop.run_in_executor(
                    None,
                    lambda c=text: GoogleTranslator(source="auto", target=lang.lower()).translate(c),
                )
                result = result or ''
                if result.strip() == text.strip():
                    # Auto-detect returned source unchanged — retry with explicit German
                    result = await loop.run_in_executor(
                        None,
                        lambda c=text: GoogleTranslator(source="de", target=lang.lower()).translate(c),
                    )
                    result = result or text
                return result

            async def _translate_chunk(raw: str) -> str:
                plain = _clean(raw)
                lines = plain.split('\n')
                struct_idx = {i for i, l in enumerate(lines) if _is_structural(l)}
                content = [lines[i] for i in range(len(lines)) if i not in struct_idx]
                if not content:
                    return plain
                content_text = '\n'.join(content)
                try:
                    translated = await _do_translate(content_text)
                    t_lines = translated.split('\n')
                except Exception:
                    t_lines = content
                t_iter = iter(t_lines)
                return '\n'.join(
                    lines[i] if i in struct_idx else next(t_iter, lines[i])
                    for i in range(len(lines))
                )

            # Translate each stanza independently (auto-detect per stanza)
            translated_parts = []
            for chunk in chunks:
                stripped = chunk.strip()
                if not stripped:
                    translated_parts.append('')
                    continue
                translated_parts.append(await _translate_chunk(stripped))

            return jsonify({"ok": True, "translated": '\n\n'.join(translated_parts)})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.route("/api/suno-info/comments/<uuid>")
    @permission_required('suno_info')
    async def api_suno_info_comments(uuid):
        """Proxy Suno's public comments endpoint for a clip.

        Suno exposes `GET /api/gen/{clip_id}/comments` on `studio-api.prod.suno.com`
        without authentication. Response shape:
            {"next_cursor": "<base64>"|null, "results": [ {comment...}, ... ]}
        We pass through the cursor for pagination.
        """
        import aiohttp as _aiohttp
        from quart import jsonify
        cursor = (request.args.get("cursor") or "").strip()
        api_url = (
            f"https://studio-api.prod.suno.com/api/gen/{uuid}/comments"
        )
        params = {"cursor": cursor} if cursor else {}
        headers = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
        try:
            async with _aiohttp.ClientSession() as sess:
                async with sess.get(api_url, params=params, headers=headers,
                                    timeout=_aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        return jsonify({"error": f"upstream HTTP {resp.status}",
                                        "results": [], "next_cursor": None}), resp.status
                    data = await resp.json()
        except Exception as e:
            return jsonify({"error": str(e), "results": [],
                            "next_cursor": None}), 502
        # Also fetch total count (cheap, separate small endpoint).
        count = None
        try:
            async with _aiohttp.ClientSession() as sess:
                async with sess.get(
                    f"https://studio-api.prod.suno.com/api/gen/{uuid}/comments/count",
                    headers=headers, timeout=_aiohttp.ClientTimeout(total=8),
                ) as r:
                    if r.status == 200:
                        cdata = await r.json()
                        count = cdata.get("count")
        except Exception:
            pass
        return jsonify({
            "results": data.get("results") or [],
            "next_cursor": data.get("next_cursor"),
            "count": count,
        })

    # --- User Preferences API -------------------------------------------------

    @app.route("/api/user/preferences", methods=["GET"])
    async def api_get_user_preferences():
        """Get current user's UI preferences."""
        from quart import jsonify
        user_id = session.get("user_id")
        if not user_id:
            return jsonify({"error": "not authenticated"}), 401
        prefs = await db.get_all_user_preferences(user_id)
        return jsonify(prefs)

    @app.route("/api/user/preferences", methods=["POST"])
    async def api_set_user_preferences():
        """Save current user's UI preferences."""
        from quart import jsonify
        user_id = session.get("user_id")
        if not user_id:
            return jsonify({"error": "not authenticated"}), 401
        data = await request.get_json()
        if not data:
            return jsonify({"error": "no data"}), 400
        try:
            saved = {}
            if "suno_player_split" in data:
                val = max(0.2, min(0.8, float(data["suno_player_split"])))
                await db.set_user_preference(user_id, "suno_player_split", val)
                saved["suno_player_split"] = val
            if "dc_channel" in data:
                await db.set_user_preference(user_id, "dc_channel", str(data["dc_channel"] or ""))
                saved["dc_channel"] = data["dc_channel"]
            if "dc_limit" in data:
                val = max(1, min(500, int(data["dc_limit"])))
                await db.set_user_preference(user_id, "dc_limit", val)
                saved["dc_limit"] = val
            if "dc_days" in data:
                val = max(0, int(data["dc_days"]))
                await db.set_user_preference(user_id, "dc_days", val)
                saved["dc_days"] = val
            if saved:
                return jsonify(saved)
            return jsonify({"error": "no known keys"}), 400
        except Exception as e:
            import traceback
            print(f"[api_set_user_preferences] Error: {e}")
            print(traceback.format_exc())
            return jsonify({"error": str(e)}), 500

    # --- Auto Translate ---

    @app.route("/auto-translate", methods=["GET", "POST"])
    @permission_required('auto_translate')
    async def auto_translate_admin():
        if request.method == "POST":
            form = await request.form
            enabled = "on" if form.get("enabled") else "off"
            channel_id = (form.get("channel_id") or "").strip()
            langs = form.getlist("languages")
            engine = form.get("engine", "google")
            if engine not in ("google", "llm", "openai", "deepl"):
                engine = "google"
            output_mode = form.get("output_mode", "separate")
            if output_mode not in ("separate", "combined"):
                output_mode = "separate"
            openai_model = (form.get("openai_model") or "gpt-4o-mini").strip()
            openai_daily_token_limit = "".join(
                ch for ch in (form.get("openai_daily_token_limit") or "0")
                if ch.isdigit()
            ) or "0"
            openai_api_key = (form.get("openai_api_key") or "").strip()
            deepl_api_key = (form.get("deepl_api_key") or "").strip()
            deepl_api_url = (form.get("deepl_api_url") or "https://api-free.deepl.com/v2/translate").strip()
            if deepl_api_url not in (
                "https://api-free.deepl.com/v2/translate",
                "https://api.deepl.com/v2/translate",
            ):
                deepl_api_url = "https://api-free.deepl.com/v2/translate"
            skip_open  = (form.get("skip_open")  or "").strip()[:4]
            skip_close = (form.get("skip_close") or "").strip()[:4]
            await db.set_setting("auto_translate_enabled", enabled)
            await db.set_setting("auto_translate_channel_id", channel_id)
            await db.set_setting("auto_translate_languages", ",".join(langs))
            await db.set_setting("auto_translate_engine", engine)
            await db.set_setting("auto_translate_output_mode", output_mode)
            await db.set_setting("auto_translate_openai_model", openai_model)
            await db.set_setting("auto_translate_openai_daily_token_limit", openai_daily_token_limit)
            await db.set_setting("auto_translate_deepl_api_url", deepl_api_url)
            if form.get("clear_openai_api_key"):
                await db.set_setting("auto_translate_openai_api_key", "")
            elif openai_api_key:
                if openai_api_key.startswith("sk-") and len(openai_api_key) >= 20:
                    await db.set_setting("auto_translate_openai_api_key", openai_api_key)
                else:
                    await flash(
                        "Ignored invalid OpenAI API key value. Existing key was kept.",
                        "warning",
                    )
            if form.get("clear_deepl_api_key"):
                await db.set_setting("auto_translate_deepl_api_key", "")
            elif deepl_api_key:
                await db.set_setting("auto_translate_deepl_api_key", deepl_api_key)
            await db.set_setting("auto_translate_skip_open", skip_open)
            await db.set_setting("auto_translate_skip_close", skip_close)
            await flash("Auto-translate settings saved.", "success")
            return redirect(url_for("auto_translate_admin"))

        enabled = (await db.get_setting("auto_translate_enabled")) == "on"
        channel_id = str(await db.get_setting("auto_translate_channel_id") or "")
        langs_str = await db.get_setting("auto_translate_languages") or ""
        selected_langs = [l.strip() for l in langs_str.split(",") if l.strip()]
        engine     = await db.get_setting("auto_translate_engine") or "google"
        output_mode = await db.get_setting("auto_translate_output_mode") or "separate"
        openai_model = await db.get_setting("auto_translate_openai_model") or "gpt-4o-mini"
        openai_daily_token_limit = await db.get_setting("auto_translate_openai_daily_token_limit") or "0"
        openai_api_key_configured = bool((await db.get_setting("auto_translate_openai_api_key") or "").strip())
        deepl_api_key_configured = bool((await db.get_setting("auto_translate_deepl_api_key") or "").strip())
        deepl_api_url = await db.get_setting("auto_translate_deepl_api_url") or "https://api-free.deepl.com/v2/translate"
        skip_open  = await db.get_setting("auto_translate_skip_open")  or ""
        skip_close = await db.get_setting("auto_translate_skip_close") or ""
        usage_rows = await db.get_auto_translate_monthly_usage()
        usage_totals: dict[str, dict] = {}
        for row in usage_rows:
            bucket = usage_totals.setdefault(
                row["month"],
                {"requests": 0, "source_chars": 0, "translated_chars": 0, "tokens": 0},
            )
            bucket["requests"] += int(row.get("requests") or 0)
            bucket["source_chars"] += int(row.get("source_chars") or 0)
            bucket["translated_chars"] += int(row.get("translated_chars") or 0)
            bucket["tokens"] += int(row.get("tokens") or 0)
        guild = get_guild()
        text_channels = []
        if guild:
            import discord as _discord
            for ch in sorted(guild.text_channels, key=lambda c: c.position):
                text_channels.append({"id": ch.id, "name": ch.name})
        from config import Config as _Cfg
        return await render_template(
            "auto_translate.html",
            enabled=enabled,
            channel_id=channel_id,
            selected_langs=selected_langs,
            engine=engine,
            output_mode=output_mode,
            openai_model=openai_model,
            openai_daily_token_limit=openai_daily_token_limit,
            openai_api_key_configured=openai_api_key_configured,
            deepl_api_key_configured=deepl_api_key_configured,
            deepl_api_url=deepl_api_url,
            usage_rows=usage_rows,
            usage_totals=usage_totals,
            skip_open=skip_open,
            skip_close=skip_close,
            llm_model=_Cfg.LLM_MODEL,
            text_channels=text_channels,
        )

    # --- RPG Admin ---

    @app.route("/rpg", methods=["GET", "POST"])
    @permission_required('rpg')
    async def rpg_admin():
        import json as _json
        await db.ensure_rpg_tables()

        tab = request.args.get("tab", "adventures")
        adventure_id_arg = request.args.get("adventure_id", type=int)

        if request.method == "POST":
            form = await request.form
            action = form.get("action", "")

            # --- Adventures ---
            if action == "adv_create":
                name = (form.get("name") or "").strip()
                if not name:
                    await flash("Adventure name is required.", "error")
                else:
                    new_id = await db.rpg_create_adventure(
                        name=name,
                        description=(form.get("description") or "").strip(),
                        intro_text=(form.get("intro_text") or "").strip(),
                        llm_system_prompt=(form.get("llm_system_prompt") or "").strip(),
                        start_scene_key=(form.get("start_scene_key") or "").strip(),
                    )
                    await flash(f"Adventure '{name}' created.", "success")
                    return redirect(url_for("rpg_admin", tab="adventures",
                                            adventure_id=new_id))

            elif action == "adv_update":
                aid = int(form.get("adventure_id") or 0)
                if aid:
                    await db.rpg_update_adventure(
                        aid,
                        name=(form.get("name") or "").strip(),
                        description=(form.get("description") or "").strip(),
                        intro_text=(form.get("intro_text") or "").strip(),
                        llm_system_prompt=(form.get("llm_system_prompt") or "").strip(),
                        start_scene_key=(form.get("start_scene_key") or "").strip(),
                        is_active=1 if form.get("is_active") else 0,
                    )
                    await flash("Adventure updated.", "success")
                return redirect(url_for("rpg_admin", tab="adventures",
                                        adventure_id=aid))

            elif action == "adv_delete":
                aid = int(form.get("adventure_id") or 0)
                if aid:
                    await db.rpg_delete_adventure(aid)
                    await flash("Adventure deleted.", "success")
                return redirect(url_for("rpg_admin", tab="adventures"))

            elif action == "adv_import":
                raw_json = (form.get("adventures_json") or "").strip()
                if not raw_json:
                    await flash("JSON input is required.", "error")
                    return redirect(url_for("rpg_admin", tab="adventures"))
                try:
                    payload = _json.loads(raw_json)
                except _json.JSONDecodeError as exc:
                    await flash(
                        f"Invalid JSON: {exc.msg} at line {exc.lineno}.", "error"
                    )
                    return redirect(url_for("rpg_admin", tab="adventures"))

                if isinstance(payload, dict) and "adventures" in payload:
                    items = payload["adventures"]
                elif isinstance(payload, dict) and "name" in payload:
                    items = [payload]
                elif isinstance(payload, list):
                    items = payload
                else:
                    await flash(
                        "JSON must be an adventure object, a list of "
                        "adventures, or {\"adventures\":[...]}.", "error",
                    )
                    return redirect(url_for("rpg_admin", tab="adventures"))

                imported = 0
                errors: list[str] = []
                last_id = None
                for idx, raw_adv in enumerate(items, start=1):
                    if not isinstance(raw_adv, dict):
                        errors.append(f"Adventure {idx}: expected an object.")
                        continue
                    name = str(raw_adv.get("name") or "").strip()
                    if not name:
                        errors.append(f"Adventure {idx}: 'name' is required.")
                        continue
                    raw_scenes = raw_adv.get("scenes") or []
                    if not isinstance(raw_scenes, list):
                        errors.append(
                            f"Adventure {idx} ({name}): 'scenes' must be an array."
                        )
                        continue
                    prepared_scenes: list[dict] = []
                    keys_seen: set[str] = set()
                    bad = False
                    for sidx, raw_scene in enumerate(raw_scenes, start=1):
                        if not isinstance(raw_scene, dict):
                            errors.append(
                                f"Adventure {idx} scene {sidx}: must be an object."
                            )
                            bad = True
                            break
                        skey = str(raw_scene.get("scene_key") or "").strip()
                        if not skey:
                            errors.append(
                                f"Adventure {idx} scene {sidx}: 'scene_key' required."
                            )
                            bad = True
                            break
                        if skey in keys_seen:
                            errors.append(
                                f"Adventure {idx}: duplicate scene_key '{skey}'."
                            )
                            bad = True
                            break
                        keys_seen.add(skey)
                        # Accept either inline object under "data" or string "data_json"
                        if "data_json" in raw_scene:
                            data_str = raw_scene["data_json"]
                            if not isinstance(data_str, str):
                                data_str = _json.dumps(data_str)
                            try:
                                _json.loads(data_str or "{}")
                            except _json.JSONDecodeError as exc:
                                errors.append(
                                    f"Adventure {idx} scene '{skey}': "
                                    f"invalid data_json ({exc.msg})."
                                )
                                bad = True
                                break
                        else:
                            data_obj = raw_scene.get("data") or {}
                            if not isinstance(data_obj, (dict, list)):
                                errors.append(
                                    f"Adventure {idx} scene '{skey}': "
                                    "'data' must be an object or array."
                                )
                                bad = True
                                break
                            data_str = _json.dumps(data_obj)
                        prepared_scenes.append({
                            "scene_key": skey,
                            "title": str(raw_scene.get("title") or ""),
                            "narration": str(raw_scene.get("narration") or ""),
                            "scene_type": str(raw_scene.get("scene_type") or "story"),
                            "data_json": data_str or "{}",
                        })
                    if bad:
                        continue
                    # Validate start_scene_key references an actual scene if given
                    start_key = str(raw_adv.get("start_scene_key") or "").strip()
                    if start_key and start_key not in keys_seen:
                        errors.append(
                            f"Adventure {idx} ({name}): start_scene_key "
                            f"'{start_key}' not found among the imported scenes."
                        )
                        continue
                    # Validate that every choice/next/after_combat reference exists
                    ref_errors: list[str] = []
                    for ps in prepared_scenes:
                        try:
                            data_obj = _json.loads(ps["data_json"])
                        except _json.JSONDecodeError:
                            continue
                        if not isinstance(data_obj, dict):
                            continue
                        for choice in (data_obj.get("choices") or []):
                            nxt = choice.get("next") if isinstance(choice, dict) else None
                            if nxt and nxt not in keys_seen:
                                ref_errors.append(
                                    f"scene '{ps['scene_key']}' choice -> '{nxt}' missing"
                                )
                        for ref_key in ("next", "after_combat"):
                            ref = data_obj.get(ref_key)
                            if ref and ref not in keys_seen:
                                ref_errors.append(
                                    f"scene '{ps['scene_key']}' {ref_key} -> '{ref}' missing"
                                )
                    if ref_errors:
                        errors.append(
                            f"Adventure {idx} ({name}): unresolved scene refs — "
                            + "; ".join(ref_errors[:5])
                            + (" …" if len(ref_errors) > 5 else "")
                        )
                        continue
                    try:
                        last_id = await db.rpg_import_adventure(
                            {
                                "name": name,
                                "description": str(raw_adv.get("description") or ""),
                                "intro_text": str(raw_adv.get("intro_text") or ""),
                                "llm_system_prompt": str(
                                    raw_adv.get("llm_system_prompt") or ""
                                ),
                                "start_scene_key": start_key,
                                "is_active": bool(raw_adv.get("is_active", True)),
                            },
                            prepared_scenes,
                        )
                        imported += 1
                    except Exception as e:
                        errors.append(f"Adventure {idx} ({name}): DB error — {e}")

                if errors:
                    preview = " · ".join(errors[:5])
                    if len(errors) > 5:
                        preview += f" (+{len(errors) - 5} more)"
                    if imported:
                        await flash(
                            f"Imported {imported} adventure(s) with errors: {preview}",
                            "error",
                        )
                    else:
                        await flash(f"Import failed: {preview}", "error")
                else:
                    await flash(
                        f"Imported {imported} adventure(s) successfully.", "success"
                    )
                if imported == 1 and last_id:
                    return redirect(url_for("rpg_admin", tab="adventures",
                                            adventure_id=last_id))
                return redirect(url_for("rpg_admin", tab="adventures"))

            # --- Scenes ---
            elif action == "scene_create":
                aid = int(form.get("adventure_id") or 0)
                key = (form.get("scene_key") or "").strip()
                if not aid or not key:
                    await flash("Adventure and scene key are required.", "error")
                else:
                    data_raw = (form.get("data_json") or "{}").strip() or "{}"
                    try:
                        _json.loads(data_raw)
                    except _json.JSONDecodeError as e:
                        await flash(f"Invalid scene JSON: {e.msg}", "error")
                        return redirect(url_for("rpg_admin", tab="scenes",
                                                adventure_id=aid))
                    await db.rpg_create_scene(
                        aid, key,
                        (form.get("title") or "").strip(),
                        (form.get("narration") or "").strip(),
                        (form.get("scene_type") or "story").strip(),
                        data_raw,
                    )
                    await flash(f"Scene '{key}' created.", "success")
                return redirect(url_for("rpg_admin", tab="scenes",
                                        adventure_id=aid))

            elif action == "scene_update":
                sid = int(form.get("scene_id") or 0)
                aid = int(form.get("adventure_id") or 0)
                if sid:
                    data_raw = (form.get("data_json") or "{}").strip() or "{}"
                    try:
                        _json.loads(data_raw)
                    except _json.JSONDecodeError as e:
                        await flash(f"Invalid scene JSON: {e.msg}", "error")
                        return redirect(url_for("rpg_admin", tab="scenes",
                                                adventure_id=aid))
                    await db.rpg_update_scene(
                        sid,
                        scene_key=(form.get("scene_key") or "").strip(),
                        title=(form.get("title") or "").strip(),
                        narration=(form.get("narration") or "").strip(),
                        scene_type=(form.get("scene_type") or "story").strip(),
                        data_json=data_raw,
                    )
                    await flash("Scene updated.", "success")
                return redirect(url_for("rpg_admin", tab="scenes",
                                        adventure_id=aid))

            elif action == "scene_delete":
                sid = int(form.get("scene_id") or 0)
                aid = int(form.get("adventure_id") or 0)
                if sid:
                    await db.rpg_delete_scene(sid)
                    await flash("Scene deleted.", "success")
                return redirect(url_for("rpg_admin", tab="scenes",
                                        adventure_id=aid))

            # --- Classes ---
            elif action == "class_upsert":
                abilities_raw = (form.get("abilities_json") or "[]").strip() or "[]"
                try:
                    _json.loads(abilities_raw)
                except _json.JSONDecodeError as e:
                    await flash(f"Invalid abilities JSON: {e.msg}", "error")
                    return redirect(url_for("rpg_admin", tab="classes"))
                await db.rpg_upsert_class(
                    class_key=(form.get("class_key") or "").strip(),
                    name=(form.get("name") or "").strip(),
                    description=(form.get("description") or "").strip(),
                    base_hp=int(form.get("base_hp") or 20),
                    base_attack=int(form.get("base_attack") or 5),
                    base_defense=int(form.get("base_defense") or 5),
                    base_agility=int(form.get("base_agility") or 5),
                    base_mana=int(form.get("base_mana") or 10),
                    abilities_json=abilities_raw,
                )
                await flash("Class saved.", "success")
                return redirect(url_for("rpg_admin", tab="classes"))

            elif action == "class_delete":
                key = (form.get("class_key") or "").strip()
                if key:
                    await db.rpg_delete_class(key)
                    await flash("Class deleted.", "success")
                return redirect(url_for("rpg_admin", tab="classes"))

            # --- Enemies ---
            elif action == "enemy_upsert":
                abilities_raw = (form.get("abilities_json") or "[]").strip() or "[]"
                loot_raw = (form.get("loot_json") or "[]").strip() or "[]"
                try:
                    _json.loads(abilities_raw)
                    _json.loads(loot_raw)
                except _json.JSONDecodeError as e:
                    await flash(f"Invalid JSON: {e.msg}", "error")
                    return redirect(url_for("rpg_admin", tab="enemies"))
                await db.rpg_upsert_enemy(
                    enemy_key=(form.get("enemy_key") or "").strip(),
                    name=(form.get("name") or "").strip(),
                    description=(form.get("description") or "").strip(),
                    hp=int(form.get("hp") or 15),
                    attack=int(form.get("attack") or 4),
                    defense=int(form.get("defense") or 3),
                    agility=int(form.get("agility") or 4),
                    abilities_json=abilities_raw,
                    loot_json=loot_raw,
                    xp_reward=int(form.get("xp_reward") or 10),
                )
                await flash("Enemy saved.", "success")
                return redirect(url_for("rpg_admin", tab="enemies"))

            elif action == "enemy_delete":
                key = (form.get("enemy_key") or "").strip()
                if key:
                    await db.rpg_delete_enemy(key)
                    await flash("Enemy deleted.", "success")
                return redirect(url_for("rpg_admin", tab="enemies"))

            elif action == "enemy_import":
                count, errors = await _rpg_import_enemies(
                    db, (form.get("enemies_json") or "").strip(), _json
                )
                if errors:
                    preview = " · ".join(errors[:5])
                    if len(errors) > 5:
                        preview += f" (+{len(errors) - 5} more)"
                    if count:
                        await flash(
                            f"Imported {count} enemy/enemies with errors: {preview}",
                            "error",
                        )
                    else:
                        await flash(f"Import failed: {preview}", "error")
                else:
                    await flash(
                        f"Imported {count} enemy/enemies successfully.", "success"
                    )
                return redirect(url_for("rpg_admin", tab="enemies"))

            # --- Items ---
            elif action == "item_upsert":
                effect_raw = (form.get("effect_json") or "{}").strip() or "{}"
                try:
                    _json.loads(effect_raw)
                except _json.JSONDecodeError as e:
                    await flash(f"Invalid effect JSON: {e.msg}", "error")
                    return redirect(url_for("rpg_admin", tab="items"))
                await db.rpg_upsert_item(
                    item_key=(form.get("item_key") or "").strip(),
                    name=(form.get("name") or "").strip(),
                    description=(form.get("description") or "").strip(),
                    item_type=(form.get("item_type") or "misc").strip(),
                    effect_json=effect_raw,
                )
                await flash("Item saved.", "success")
                return redirect(url_for("rpg_admin", tab="items"))

            elif action == "item_delete":
                key = (form.get("item_key") or "").strip()
                if key:
                    await db.rpg_delete_item(key)
                    await flash("Item deleted.", "success")
                return redirect(url_for("rpg_admin", tab="items"))

            elif action == "item_import":
                count, errors = await _rpg_import_items(
                    db, (form.get("items_json") or "").strip(), _json
                )
                if errors:
                    preview = " · ".join(errors[:5])
                    if len(errors) > 5:
                        preview += f" (+{len(errors) - 5} more)"
                    if count:
                        await flash(
                            f"Imported {count} item(s) with errors: {preview}",
                            "error",
                        )
                    else:
                        await flash(f"Import failed: {preview}", "error")
                else:
                    await flash(f"Imported {count} item(s) successfully.", "success")
                return redirect(url_for("rpg_admin", tab="items"))

            # --- Characters / Parties (admin override) ---
            elif action == "char_delete":
                cid = int(form.get("character_id") or 0)
                if cid:
                    await db.rpg_delete_character(cid)
                    await flash("Character deleted.", "success")
                return redirect(url_for("rpg_admin", tab="characters"))

            elif action == "party_delete":
                pid = int(form.get("party_id") or 0)
                if pid:
                    await db.rpg_delete_party(pid)
                    await flash("Party deleted.", "success")
                return redirect(url_for("rpg_admin", tab="parties"))

            elif action == "save_settings":
                await db.set_setting(
                    "rpg_channel_id",
                    (form.get("rpg_channel_id") or "").strip(),
                )
                await db.set_setting(
                    "rpg_enabled",
                    "true" if form.get("rpg_enabled") else "false",
                )
                await db.set_setting(
                    "rpg_block_during_stream",
                    "true" if form.get("rpg_block_during_stream") else "false",
                )
                await flash("RPG settings saved.", "success")
                return redirect(url_for("rpg_admin", tab="settings"))

            return redirect(url_for("rpg_admin", tab=tab))

        # GET
        adventures = await db.rpg_list_adventures()
        selected_adv = None
        scenes = []
        if adventure_id_arg:
            selected_adv = await db.rpg_get_adventure(adventure_id_arg)
            if selected_adv:
                scenes = await db.rpg_list_scenes(adventure_id_arg)
        elif adventures and tab == "scenes":
            selected_adv = adventures[0]
            scenes = await db.rpg_list_scenes(selected_adv["id"])

        classes_ = await db.rpg_list_classes()
        enemies = await db.rpg_list_enemies()
        items = await db.rpg_list_items()
        characters = await db.rpg_list_characters()
        parties = await db.rpg_list_parties()

        guild = None
        try:
            if app.bot and getattr(app.bot, "guilds", None):
                guild = next(iter(app.bot.guilds), None)
        except Exception:
            guild = None
        text_channels = []
        if guild is not None:
            for ch in sorted(guild.text_channels, key=lambda c: c.position):
                text_channels.append({"id": ch.id, "name": ch.name})

        rpg_channel_id = await db.get_setting("rpg_channel_id") or ""
        rpg_enabled = (await db.get_setting("rpg_enabled")) != "false"
        rpg_block_during_stream = (
            (await db.get_setting("rpg_block_during_stream")) != "false"
        )
        stream_is_live = False
        try:
            from bot.exp_stream_manager import stream_is_live as _live
            stream_is_live = bool(_live)
        except Exception:
            stream_is_live = False

        return await render_template(
            "rpg.html",
            tab=tab,
            adventures=adventures,
            selected_adv=selected_adv,
            scenes=scenes,
            classes=classes_,
            enemies=enemies,
            items=items,
            characters=characters,
            parties=parties,
            text_channels=text_channels,
            rpg_channel_id=rpg_channel_id,
            rpg_enabled=rpg_enabled,
            rpg_block_during_stream=rpg_block_during_stream,
            stream_is_live=stream_is_live,
        )

    return app
