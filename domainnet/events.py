"""Thread-safe event bus.

Every state change and every message movement in a :class:`~domainnet.Network`
is published here as an :class:`Event`. The dashboard, your tests, and your own
code all consume the same stream.
"""
from __future__ import annotations

import collections
import itertools
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional, Tuple

log = logging.getLogger("domainnet.events")


@dataclass(frozen=True)
class Event:
    seq: int
    ts: float
    type: str
    data: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {"seq": self.seq, "ts": self.ts, "type": self.type, "data": self.data}


class EventBus:
    """Ordered, in-process publish/subscribe with a bounded replay history.

    Subscriber callbacks run synchronously on the emitting thread, in event
    order. Keep them fast and non-blocking (push to a queue if you need to do
    real work).
    """

    def __init__(self, history: int = 2000) -> None:
        self._lock = threading.RLock()
        self._seq = 0
        self._ids = itertools.count(1)
        self._subs: Dict[int, Tuple[Callable[[Event], None], Optional[Tuple[str, ...]]]] = {}
        self._history: Deque[Event] = collections.deque(maxlen=history)

    @property
    def last_seq(self) -> int:
        return self._seq

    def emit(self, etype: str, /, **data: Any) -> Event:
        with self._lock:
            self._seq += 1
            ev = Event(self._seq, time.time(), etype, data)
            self._history.append(ev)
            for fn, prefixes in list(self._subs.values()):
                if prefixes and not etype.startswith(prefixes):
                    continue
                try:
                    fn(ev)
                except Exception:  # a bad subscriber must never break the network
                    log.exception("event subscriber failed on %s", etype)
        return ev

    def subscribe(
        self,
        fn: Callable[[Event], None],
        types: Optional[Iterable[str]] = None,
    ) -> Callable[[], None]:
        """Register ``fn``; ``types`` is an optional list of type prefixes
        (``["message."]`` matches ``message.sent``, ``message.hop``, ...).
        Returns an ``unsubscribe()`` function."""
        prefixes = tuple(types) if types else None
        with self._lock:
            sid = next(self._ids)
            self._subs[sid] = (fn, prefixes)

        def unsubscribe() -> None:
            with self._lock:
                self._subs.pop(sid, None)

        return unsubscribe

    def history(self, since: int = 0, limit: Optional[int] = None) -> List[Event]:
        with self._lock:
            out = [e for e in self._history if e.seq > since]
        return out[-limit:] if limit else out
