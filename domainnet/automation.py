"""Things that make a demo move on its own: random traffic, random churn
(domains/links/services appearing and disappearing), and timed scenarios."""
from __future__ import annotations

import logging
import random
import re
import threading
from typing import TYPE_CHECKING, Any, List, Optional, Sequence, Tuple

from .core import DomainNetError, Network, format_target

if TYPE_CHECKING:  # pragma: no cover
    from .control import Controller

log = logging.getLogger("domainnet.automation")

_COMMENT_RE = re.compile(r"(?:^|\s)#(?:\s|$)")


def strip_comment(line: str) -> str:
    """Remove a ``# comment`` and surrounding whitespace.

    A comment starts at a ``#`` that is preceded by whitespace (or starts the
    line) *and* followed by whitespace (or ends the line). So a URL fragment
    such as ``https://a.com/page#top`` or a payload like ``#1`` is left intact.
    """
    m = _COMMENT_RE.search(line)
    return (line[: m.start()] if m else line).strip()


class _Periodic:
    """Base for self-rescheduling generators. They run on the network's
    scheduler, so ``net.pause()`` pauses them too."""

    def __init__(self, net: Network, seed: Optional[int] = None) -> None:
        self.net = net
        self._rng = random.Random(seed)
        self._running = False
        self._handle: Optional[int] = None

    @property
    def running(self) -> bool:
        return self._running

    def _interval(self) -> float:  # pragma: no cover - overridden
        raise NotImplementedError

    def _step(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def _start(self) -> None:
        if self._running:
            return
        self._running = True
        self._handle = self.net.scheduler.call_later(0.05, self._tick)

    def stop(self) -> None:
        self._running = False
        if self._handle is not None:
            self.net.scheduler.cancel(self._handle)
            self._handle = None

    def _tick(self) -> None:
        if not self._running:
            return
        try:
            self._step()
        except DomainNetError:
            pass
        except Exception:
            log.exception("%s step failed", type(self).__name__)
        if self._running:
            self._handle = self.net.scheduler.call_later(self._interval(), self._tick)


class TrafficGenerator(_Periodic):
    """Sends random messages between random domains (multi-hop where needed).

    ``rate`` is messages per second (real time). ``service_bias`` is the
    probability of targeting a deployed service rather than the bare domain.
    """

    def __init__(self, net: Network, rate: float = 2.0, seed: Optional[int] = None,
                 service_bias: float = 0.8, payloads: Optional[Sequence[Any]] = None) -> None:
        super().__init__(net, seed)
        self.rate = rate
        self.service_bias = service_bias
        self.payloads: Sequence[Any] = payloads or ["hello", "status?", "sync", "ping", 42, "data"]

    def start(self, rate: Optional[float] = None) -> None:
        if rate is not None:
            self.rate = max(0.05, float(rate))
        self._start()

    def _interval(self) -> float:
        return (1.0 / max(self.rate, 0.05)) * self._rng.uniform(0.5, 1.5)

    def _step(self) -> None:
        names = self.net.domain_names()
        if len(names) < 2:
            return
        src = self._rng.choice(names)
        dst = self._rng.choice([n for n in names if n != src])
        services = self.net.services_of(dst)
        target = dst
        if services and self._rng.random() < self.service_bias:
            target = format_target(dst, self._rng.choice(services))
        self.net.send(src, target, self._rng.choice(list(self.payloads)))


class Churn(_Periodic):
    """Randomly adds/removes domains, links and services so the topology keeps
    evolving. Bounded by ``min_domains`` / ``max_domains``."""

    def __init__(self, net: Network, interval: float = 3.0, min_domains: int = 3,
                 max_domains: int = 9, seed: Optional[int] = None) -> None:
        super().__init__(net, seed)
        self.interval = interval
        self.min_domains = min_domains
        self.max_domains = max_domains

    def start(self, interval: Optional[float] = None) -> None:
        if interval is not None:
            self.interval = max(0.2, float(interval))
        self._start()

    def _interval(self) -> float:
        return self.interval * self._rng.uniform(0.7, 1.3)

    def _step(self) -> None:
        net, rng = self.net, self._rng
        names = net.domain_names()
        n = len(names)
        links = net.links()
        actions: List[Tuple[str, int]] = []
        if n < self.max_domains:
            actions.append(("add", 3))
        if n > self.min_domains:
            actions.append(("remove", 2))
        if n >= 2:
            actions.append(("link", 3))
            if len(links) > max(1, n - 1):
                actions.append(("unlink", 2))
        if n >= 1:
            actions += [("deploy", 3), ("undeploy", 1)]
        if not actions:
            return
        action = rng.choices([a for a, _ in actions], weights=[w for _, w in actions])[0]

        if action == "add":
            name = net.suggest_name()
            net.add_domain(name)
            tmpl = rng.choice(sorted(net.templates)) if net.templates else None
            if tmpl:
                net.deploy_template(name, tmpl)
            for other in rng.sample(names, k=min(len(names), rng.choice([1, 1, 2]))):
                net.link(name, other, latency_ms=rng.choice([60, 100, 150, 220, 300]))
        elif action == "remove":
            net.remove_domain(rng.choice(names))
        elif action == "link":
            existing = {(l.a, l.b) for l in links}
            pairs = [(a, b) for i, a in enumerate(sorted(names)) for b in sorted(names)[i + 1:]
                     if (a, b) not in existing]
            if pairs:
                a, b = rng.choice(pairs)
                net.link(a, b, latency_ms=rng.choice([60, 100, 150, 220, 300]))
        elif action == "unlink":
            lk = rng.choice(links)
            net.unlink(lk.a, lk.b)
        elif action == "deploy":
            dom = rng.choice(names)
            have = set(net.services_of(dom))
            free = [t for t in net.templates if t not in have]
            if free:
                net.deploy_template(dom, rng.choice(free))
        elif action == "undeploy":
            dom = rng.choice(names)
            svcs = net.services_of(dom)
            if svcs:
                net.undeploy(dom, rng.choice(svcs))


class Scenario:
    """A timed script of controller commands.

    Text format, one step per line (``#`` starts a comment)::

        0    add alpha bravo
        1.5  link alpha bravo 120
        4    deploy alpha echo
        10   rm bravo

    Times are seconds from ``start()`` and follow the network's pause state.
    """

    def __init__(self, steps: Sequence[Tuple[float, str]]) -> None:
        self.steps = sorted(((float(t), line) for t, line in steps), key=lambda s: s[0])
        self.errors: List[Tuple[float, str, str]] = []   # (time, line, message) of failed steps
        self.finished = threading.Event()                # set after the last step ran (or cancel())
        self._handles: List[int] = []
        self._net: Optional[Network] = None
        self._remaining = 0
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self.steps)

    def __repr__(self) -> str:
        return f"Scenario({len(self.steps)} steps, {self.duration:.1f}s)"

    @property
    def duration(self) -> float:
        """Time of the last step, in seconds."""
        return self.steps[-1][0] if self.steps else 0.0

    def to_text(self) -> str:
        """Serialize to the text format read by :meth:`from_text` (lossless round trip)."""
        return "".join(f"{t:.3f}  {line}\n" for t, line in self.steps)

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(self.to_text())

    def wait(self, timeout: Optional[float] = None) -> bool:
        """Block until every step has run (or :meth:`cancel` was called). Returns False on
        timeout. Remember that time follows ``net.pause()``: a paused network never finishes."""
        return self.finished.wait(timeout)

    @classmethod
    def from_text(cls, text: str) -> "Scenario":
        steps: List[Tuple[float, str]] = []
        for n, raw in enumerate(text.splitlines(), 1):
            line = strip_comment(raw)
            if not line:
                continue
            parts = line.split(None, 1)
            head, rest = parts[0], (parts[1] if len(parts) > 1 else "")
            try:
                t = float(head.lstrip("+"))
            except ValueError:
                raise ValueError(f"scenario line {n}: expected '<seconds> <command>', got {raw!r}") from None
            if not rest.strip():
                raise ValueError(f"scenario line {n}: missing command")
            steps.append((t, rest.strip()))
        return cls(steps)

    @classmethod
    def from_file(cls, path: str) -> "Scenario":
        with open(path, "r", encoding="utf-8") as fh:
            return cls.from_text(fh.read())

    def start(self, controller: "Controller") -> "Scenario":
        self.cancel()
        self._net = controller.net
        self.errors = []
        with self._lock:
            self._remaining = len(self.steps)
        self.finished.clear()
        if not self.steps:
            self.finished.set()
        for t, line in self.steps:
            self._handles.append(
                controller.net.scheduler.call_later(t, self._run_step, controller, t, line))
        return self

    def cancel(self) -> None:
        if self._net is not None:
            for h in self._handles:
                self._net.scheduler.cancel(h)
        self._handles = []
        self.finished.set()

    def _run_step(self, controller: "Controller", t: float, line: str) -> None:
        res = controller.run_line(line)
        if not res.get("ok"):
            log.warning("scenario step %r failed: %s", line, res.get("error"))
            self.errors.append((t, line, str(res.get("error"))))
        with self._lock:
            self._remaining -= 1
            if self._remaining <= 0:
                self.finished.set()
