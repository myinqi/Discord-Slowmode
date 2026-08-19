"""Isolated live-log ring buffer for TrYa Stream."""

import time
from collections import deque

_LOG_BUFFER_MAX = 1000
_LOG_BUFFER: deque = deque(maxlen=_LOG_BUFFER_MAX)


def log_event(line: str, level: str = "info", prefix: str = "") -> None:
    ts = time.time()
    _LOG_BUFFER.append((ts, level, line))
    try:
        tag = f"{prefix} " if prefix else ""
        print(f"{tag}{line}", flush=True)
    except Exception:
        pass
