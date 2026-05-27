"""Shared live-log ring buffer for the Admin UI.

Imported by exp_stream_manager, twitch_bot, relic_hunt, and app.py so that
all subsystems write into the same buffer that the UI polls via
/exp-radio/stream/log.  Keeping it here avoids circular imports.
"""

import time
from collections import deque

_LOG_BUFFER_MAX = 1000
_LOG_BUFFER: deque = deque(maxlen=_LOG_BUFFER_MAX)


def log_event(line: str, level: str = "info", prefix: str = "") -> None:
    """Append *line* to the shared ring buffer and mirror to stdout.

    Args:
        line:   The message shown in the UI (no prefix).
        level:  'info' | 'error' | 'ffmpeg' — used by the UI for colouring.
        prefix: Tag prepended only for the stdout mirror (e.g. '[relic-hunt]').
    """
    ts = time.time()
    _LOG_BUFFER.append((ts, level, line))
    try:
        tag = f"{prefix} " if prefix else ""
        print(f"{tag}{line}", flush=True)
    except Exception:
        pass
