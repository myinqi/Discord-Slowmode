"""Serialising priority queue for all LLM moderation calls.

The local Ollama instance runs on CPU and can only efficiently handle ONE
inference at a time.  When multiple moderation jobs arrive simultaneously
(e.g. several radio submissions + a channel post) they must be serialised so
that they don't all compete for the same model and race each other into a
timeout.

Jobs are pulled in priority order, FIFO within each priority level:

  PRIO_RADIO   = 0   highest — stream submissions; a user is waiting
  PRIO_CHANNEL = 10  lower   — background channel monitoring

Usage
-----
    from bot.llm_mod_queue import enqueue_moderation, PRIO_RADIO, PRIO_CHANNEL

    verdict = await enqueue_moderation(
        PRIO_RADIO,
        lambda: moderate_lyrics(client, lyrics=lyrics, title=title, artist=artist),
    )

The returned value is whatever the callable returns (the verdict dict).
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine

PRIO_RADIO   = 0   # stream submissions — user is waiting for review result
PRIO_CHANNEL = 10  # channel-posted songs — background, can wait

_job_seq: int = 0
_queue: asyncio.PriorityQueue | None = None
_worker_task: asyncio.Task | None = None


@dataclass(order=True)
class _Job:
    priority: int
    seq: int                                               # FIFO tiebreaker
    fn: Callable[[], Coroutine[Any, Any, Any]] = field(compare=False)
    fut: asyncio.Future = field(compare=False)


def _get_queue() -> asyncio.PriorityQueue:
    global _queue
    if _queue is None:
        _queue = asyncio.PriorityQueue()
    return _queue


async def _worker_loop() -> None:
    """Single serial consumer — runs exactly one LLM moderation call at a time."""
    q = _get_queue()
    waiting = 0
    while True:
        job: _Job = await q.get()
        waiting = q.qsize()
        try:
            if waiting:
                print(
                    f"[llm-queue] processing prio={job.priority} seq={job.seq}"
                    f" ({waiting} job(s) still waiting)",
                    flush=True,
                )
            result = await job.fn()
            if not job.fut.done():
                job.fut.set_result(result)
        except Exception as exc:
            if not job.fut.done():
                job.fut.set_exception(exc)
        finally:
            q.task_done()


def ensure_worker() -> None:
    """Start the background worker task if it isn't already running."""
    global _worker_task
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(_worker_loop())


async def enqueue_moderation(
    priority: int,
    fn: Callable[[], Coroutine[Any, Any, Any]],
) -> Any:
    """Submit an LLM moderation coroutine and await its result.

    Parameters
    ----------
    priority:
        Use ``PRIO_RADIO`` for stream submissions, ``PRIO_CHANNEL`` for
        channel monitoring jobs.
    fn:
        A *zero-argument* async callable (lambda / functools.partial) whose
        return value is the moderation verdict dict.

    Returns
    -------
    Whatever ``fn()`` returns when it completes.
    """
    global _job_seq
    ensure_worker()
    _job_seq += 1
    fut: asyncio.Future = asyncio.get_running_loop().create_future()
    job = _Job(priority=priority, seq=_job_seq, fn=fn, fut=fut)
    await _get_queue().put(job)
    return await fut


def queue_depth() -> int:
    """Return the number of jobs currently waiting (not counting the active one)."""
    if _queue is None:
        return 0
    return _queue.qsize()
