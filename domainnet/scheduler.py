"""A tiny pausable timer thread.

All message latency in the network goes through this scheduler, which is what
makes ``pause()`` / ``resume()`` freeze and thaw messages that are in flight.
"""
from __future__ import annotations

import heapq
import itertools
import logging
import threading
import time
from typing import Any, Callable, List, Set, Tuple

log = logging.getLogger("domainnet.scheduler")


class Scheduler:
    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._heap: List[Tuple[float, int, Callable[..., Any], tuple]] = []
        self._ids = itertools.count(1)
        self._cancelled: Set[int] = set()
        self._paused = False
        self._paused_at = 0.0
        self._closed = False
        self._thread = threading.Thread(target=self._run, name="domainnet-scheduler", daemon=True)
        self._thread.start()

    # -- public ---------------------------------------------------------
    def call_later(self, delay: float, fn: Callable[..., Any], *args: Any) -> int:
        with self._cond:
            handle = next(self._ids)
            due = time.monotonic() + max(0.0, delay)
            if self._paused:
                # time is frozen: keep the delay relative to the pause instant
                due = self._paused_at + max(0.0, delay)
            heapq.heappush(self._heap, (due, handle, fn, args))
            self._cond.notify()
            return handle

    def cancel(self, handle: int) -> None:
        with self._cond:
            self._cancelled.add(handle)

    def pause(self) -> None:
        with self._cond:
            if not self._paused:
                self._paused = True
                self._paused_at = time.monotonic()

    def resume(self) -> None:
        with self._cond:
            if self._paused:
                shift = time.monotonic() - self._paused_at
                self._heap = [(d + shift, h, f, a) for (d, h, f, a) in self._heap]
                heapq.heapify(self._heap)
                self._paused = False
                self._cond.notify()

    @property
    def paused(self) -> bool:
        return self._paused

    def pending(self) -> int:
        with self._cond:
            return len(self._heap)

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    # -- worker ---------------------------------------------------------
    def _run(self) -> None:
        while True:
            with self._cond:
                while True:
                    if self._closed:
                        return
                    if self._paused or not self._heap:
                        self._cond.wait()
                        continue
                    delay = self._heap[0][0] - time.monotonic()
                    if delay <= 0:
                        break
                    self._cond.wait(delay)
                _, handle, fn, args = heapq.heappop(self._heap)
                if handle in self._cancelled:
                    self._cancelled.discard(handle)
                    continue
            try:
                fn(*args)
            except Exception:
                log.exception("scheduled callback failed")
