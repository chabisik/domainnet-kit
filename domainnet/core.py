"""Core model: domains, links, services, and message routing.

Concepts
--------
* **Domain**   – an isolated participant. It owns services and an inbox.
* **Link**     – an undirected connection between two domains with latency/loss.
* **Service**  – a named handler deployed *inside* one domain.
* **Message**  – addressed to ``"domain"`` or ``"domain<service>"``; routed hop by
  hop over the links that exist *at that moment* (so topology can change while
  messages are in flight).

Everything that happens is published on ``net.bus`` as an event.
"""
from __future__ import annotations

import heapq
import inspect
import itertools
import logging
import random
import re
import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from .events import EventBus
from .scheduler import Scheduler

log = logging.getLogger("domainnet")

# Names (domains, services, templates) are free-form: any non-empty string without
# whitespace, control characters, or the reserved address delimiters '<' and '>'.
# Those two can never appear unescaped in a URL, so URLs are valid names as-is.
_NAME_RE = re.compile(r"[^\s<>\x00-\x1f\x7f]+")
_TARGET_RE = re.compile(r"([^<>]+)<([^<>]+)>")

_AUTO_NAMES = [
    "alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel",
    "india", "juliet", "kilo", "lima", "mike", "november", "oscar", "papa",
    "quebec", "romeo", "sierra", "tango", "uniform", "victor", "whiskey",
    "xray", "yankee", "zulu",
]


# ---------------------------------------------------------------- errors ---
class DomainNetError(Exception):
    """Base class for all framework errors."""


class InvalidName(DomainNetError):
    pass


class DomainExists(DomainNetError):
    pass


class UnknownDomain(DomainNetError):
    pass


class ServiceExists(DomainNetError):
    pass


class UnknownService(DomainNetError):
    pass


class UnknownTemplate(DomainNetError):
    pass


class LinkError(DomainNetError):
    pass


class DeliveryError(DomainNetError):
    """A request or its reply was dropped (no route, loss, domain removed...)."""


class RemoteError(DomainNetError):
    """The remote service raised an exception while handling a ``call()``."""


class CallTimeout(DomainNetError, TimeoutError):
    pass


_LABEL_BAD = re.compile(r"[\x00-\x1f\x7f\x85\u2028\u2029]")


def normalize_label(label: Any) -> Optional[str]:
    """``None`` / empty / whitespace-only -> ``None`` (= "not set", falls back to the
    name). Otherwise the stripped text. Labels are display-only: spaces, URLs and
    any Unicode are fine, but they must be single-line."""
    if label is None:
        return None
    if not isinstance(label, str):
        raise InvalidName(f"label must be text, got {type(label).__name__}")
    label = label.strip()
    if not label:
        return None
    if _LABEL_BAD.search(label):
        raise InvalidName("label must be a single line (no control characters)")
    return label


def check_name(name: str) -> None:
    """Raise :class:`InvalidName` unless ``name`` is a valid domain/service name."""
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise InvalidName(
            f"invalid name {name!r}: must be non-empty and contain no whitespace, "
            "control characters, '<' or '>'"
        )


def format_target(domain: str, service: Optional[str] = None) -> str:
    """Build an address: ``"domain"`` or ``"domain<service>"``."""
    check_name(domain)
    if service is None or service == "":
        return domain
    check_name(service)
    return f"{domain}<{service}>"


def parse_target(target: str) -> Tuple[str, Optional[str]]:
    """Split an address into ``(domain, service_or_None)``.

    ``"https://a.com"`` -> ``("https://a.com", None)``;
    ``"https://a.com<quote>"`` -> ``("https://a.com", "quote")``.
    The syntax is context-free: it never depends on which domains exist.
    """
    if not isinstance(target, str) or not target:
        raise InvalidName("target must be 'domain' or 'domain<service>'")
    if "<" not in target and ">" not in target:
        return target, None
    m = _TARGET_RE.fullmatch(target)
    if not m:
        raise InvalidName(f"bad target {target!r}: expected 'domain' or 'domain<service>'")
    return m.group(1), m.group(2)


# ----------------------------------------------------------------- model ---
@dataclass
class Message:
    id: str
    src: str
    dst: str
    service: Optional[str]
    payload: Any
    kind: str = "request"  # request | reply | event
    reply_to: Optional[str] = None
    error: bool = False
    ttl: int = 16
    created: float = field(default_factory=time.monotonic)
    path: List[str] = field(default_factory=list)

    @property
    def target(self) -> str:
        return f"{self.dst}<{self.service}>" if self.service else self.dst


@dataclass
class Link:
    a: str
    b: str
    latency_ms: float
    loss: float


@dataclass
class Template:
    name: str
    factory: Callable[["Domain"], Callable[..., Any]]
    description: str = ""


def _arity(fn: Callable[..., Any]) -> int:
    try:
        params = [
            p for p in inspect.signature(fn).parameters.values()
            if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD) and p.default is p.empty
        ]
        if any(p.kind is p.VAR_POSITIONAL for p in inspect.signature(fn).parameters.values()):
            return 2
        return len(params)
    except (TypeError, ValueError):
        return 2


class Service:
    """A handler deployed in a domain.

    Handlers may be ``fn(ctx, msg)`` or just ``fn(msg)``. Return a non-``None``
    value to send a reply back to the caller.
    """

    def __init__(self, name: str, handler: Callable[..., Any], description: str = "",
                 template: Optional[str] = None) -> None:
        self.name = name
        self.handler = handler
        self.description = description
        self.template = template  # set when deployed from a registered template (replayable)
        self.calls = 0
        self.errors = 0
        self._arity = _arity(handler)

    def invoke(self, ctx: "ServiceContext", msg: Message) -> Any:
        return self.handler(ctx, msg) if self._arity >= 2 else self.handler(msg)


class Domain:
    def __init__(self, net: "Network", name: str, meta: Dict[str, Any]) -> None:
        self.net = net
        self.name = name
        self.meta = dict(meta)
        self._label: Optional[str] = None
        self.services: Dict[str, Service] = {}
        self.inbox: Deque[Message] = deque(maxlen=100)
        self.on_message: Optional[Callable[[Message], None]] = None
        self.received = 0

    @property
    def label(self) -> str:
        """Display text shown in the dashboard. Defaults to :attr:`name` (the
        immutable id). Assigning ``None`` or ``""`` resets it."""
        return self._label if self._label is not None else self.name

    @label.setter
    def label(self, value: Optional[str]) -> None:
        self.net.set_label(self.name, value)

    @property
    def has_label(self) -> bool:
        return self._label is not None

    # convenience wrappers -------------------------------------------------
    def deploy(self, name: str, handler: Callable[..., Any], description: str = "",
               replace: bool = False) -> Service:
        return self.net.deploy(self.name, name, handler, description, replace)

    def service(self, name: Optional[str] = None, description: str = ""):
        """Decorator form of :meth:`deploy`."""
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            doc = (fn.__doc__ or "").strip().splitlines()
            self.deploy(name or fn.__name__, fn, description or (doc[0] if doc else ""))
            return fn
        return deco

    def undeploy(self, name: str) -> None:
        self.net.undeploy(self.name, name)

    def send(self, target: str, payload: Any = None, **kw: Any) -> Message:
        return self.net.send(self.name, target, payload, **kw)

    def call(self, target: str, payload: Any = None, timeout: float = 5.0) -> Any:
        return self.net.call(self.name, target, payload, timeout)

    def neighbors(self) -> List[str]:
        return self.net.neighbors(self.name)


@dataclass
class ServiceContext:
    """Passed to service handlers so they can talk to other domains."""
    net: "Network"
    domain: Domain
    message: Message

    def send(self, target: str, payload: Any = None, **kw: Any) -> Message:
        return self.net.send(self.domain.name, target, payload, **kw)

    def call(self, target: str, payload: Any = None, timeout: float = 5.0) -> Any:
        return self.net.call(self.domain.name, target, payload, timeout)


def _lk(a: str, b: str) -> Tuple[str, str]:
    return (a, b) if a <= b else (b, a)


def _preview(payload: Any, limit: int = 60) -> str:
    try:
        s = repr(payload)
    except Exception:
        s = "<unprintable>"
    return s if len(s) <= limit else s[: limit - 1] + "…"


# --------------------------------------------------------------- network ---
class Network:
    """The whole simulated network. Thread-safe; create one per demo/app."""

    def __init__(
        self,
        *,
        speed: float = 1.0,
        default_latency_ms: float = 150.0,
        default_loss: float = 0.0,
        max_hops: int = 16,
        workers: int = 8,
        seed: Optional[int] = None,
        history: int = 2000,
        builtins: bool = True,
    ) -> None:
        self.bus = EventBus(history)
        self.scheduler = Scheduler()
        self.default_latency_ms = float(default_latency_ms)
        self.default_loss = float(default_loss)
        self.max_hops = int(max_hops)
        self.speed = 1.0
        self.paused = False
        self.templates: Dict[str, Template] = {}

        self._lock = threading.RLock()
        self._domains: Dict[str, Domain] = {}
        self._links: Dict[Tuple[str, str], Link] = {}
        self._adj: Dict[str, Dict[str, Link]] = {}
        self._pending: Dict[str, Future] = {}
        self._tls = threading.local()  # marks "this thread is running a service handler"
        self._rng = random.Random(seed)
        self._msg_ids = itertools.count(1)
        self._name_idx = itertools.count(0)
        self._stats = {"sent": 0, "delivered": 0, "dropped": 0, "in_flight": 0}
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="domainnet-svc")
        self.set_speed(speed, _quiet=True)

        if builtins:
            from .builtin import install_builtins
            install_builtins(self)

    # ----------------------------------------------------------- lifecycle
    def close(self) -> None:
        """Stop the network. Blocking ``call()``s still waiting for a reply fail right away
        with :class:`DeliveryError` instead of hanging until their timeout."""
        self.scheduler.close()
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for fut in pending:
            if not fut.done():
                fut.set_exception(DeliveryError("network closed"))
        self._pool.shutdown(wait=False, cancel_futures=True)

    def __enter__(self) -> "Network":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------ templates
    def register_template(self, name: str, factory: Callable[[Domain], Callable[..., Any]],
                          description: str = "") -> None:
        """Register a service *template*: ``factory(domain) -> handler``.
        Templates are what the dashboard / text commands can deploy by name."""
        self._check_name(name)
        self.templates[name] = Template(name, factory, description)

    # -------------------------------------------------------------- domains
    def suggest_name(self) -> str:
        with self._lock:
            while True:
                i = next(self._name_idx)
                base = _AUTO_NAMES[i % len(_AUTO_NAMES)]
                name = base if i < len(_AUTO_NAMES) else f"{base}-{i // len(_AUTO_NAMES) + 1}"
                if name not in self._domains:
                    return name

    def add_domain(self, name: Optional[str] = None, label: Optional[str] = None,
                   **meta: Any) -> Domain:
        """Create a domain. ``name`` is its immutable id; ``label`` is optional display
        text (defaults to the name). Extra keyword arguments are stored as ``meta``."""
        new_label = normalize_label(label)
        with self._lock:
            name = name or self.suggest_name()
            self._check_name(name)
            if name in self._domains:
                raise DomainExists(f"domain {name!r} already exists")
            dom = Domain(self, name, meta)
            dom._label = new_label
            self._domains[name] = dom
            self._adj[name] = {}
        self.bus.emit("domain.added", name=name, meta=dom.meta, label=dom.label,
                      custom=dom.has_label)
        return dom

    def set_label(self, name: str, label: Optional[str]) -> str:
        """Set the display label of a domain; ``None`` or ``""`` resets it to the name.
        Returns the effective label."""
        new_label = normalize_label(label)
        with self._lock:
            dom = self._get(name)
            changed = dom._label != new_label
            dom._label = new_label
            effective = dom.label
        if changed:
            self.bus.emit("domain.label", name=name, label=effective, custom=new_label is not None)
        return effective

    def remove_domain(self, name: str) -> None:
        with self._lock:
            dom = self._get(name)
            gone = [l for l in self._links.values() if name in (l.a, l.b)]
            for l in gone:
                self._drop_link_locked(l)
        for l in gone:
            self.bus.emit("link.removed", a=l.a, b=l.b, reason="domain_removed")
        with self._lock:
            del self._domains[name]
            self._adj.pop(name, None)
        self.bus.emit("domain.removed", name=name, services=sorted(dom.services))

    def reset(self) -> None:
        for name in list(self._domains):
            try:
                self.remove_domain(name)
            except UnknownDomain:
                pass

    def domain(self, name: str) -> Domain:
        with self._lock:
            return self._get(name)

    def __getitem__(self, name: str) -> Domain:
        return self.domain(name)

    def __contains__(self, name: str) -> bool:
        return name in self._domains

    def domain_names(self) -> List[str]:
        with self._lock:
            return list(self._domains)

    def services_of(self, name: str) -> List[str]:
        with self._lock:
            return list(self._get(name).services)

    # ---------------------------------------------------------------- links
    def link(self, a: str, b: str, latency_ms: Optional[float] = None,
             loss: Optional[float] = None) -> Link:
        """Create a link, or update latency/loss if it already exists."""
        if a == b:
            raise LinkError("cannot link a domain to itself")
        with self._lock:
            self._get(a)
            self._get(b)
            key = _lk(a, b)
            lk = self._links.get(key)
            created = lk is None
            if lk is None:
                lk = Link(key[0], key[1], self.default_latency_ms, self.default_loss)
                self._links[key] = lk
                self._adj[a][b] = lk
                self._adj[b][a] = lk
            if latency_ms is not None:
                lk.latency_ms = max(0.0, float(latency_ms))
            if loss is not None:
                lk.loss = min(1.0, max(0.0, float(loss)))
        self.bus.emit("link.added" if created else "link.updated",
                      a=lk.a, b=lk.b, latency_ms=lk.latency_ms, loss=lk.loss)
        return lk

    def unlink(self, a: str, b: str) -> None:
        with self._lock:
            lk = self._links.get(_lk(a, b))
            if lk is None:
                raise LinkError(f"no link between {a!r} and {b!r}")
            self._drop_link_locked(lk)
        self.bus.emit("link.removed", a=lk.a, b=lk.b, reason="unlinked")

    def links(self) -> List[Link]:
        with self._lock:
            return list(self._links.values())

    def neighbors(self, name: str) -> List[str]:
        with self._lock:
            self._get(name)
            return list(self._adj[name])

    def route(self, a: str, b: str) -> Optional[List[str]]:
        """Lowest-latency path from ``a`` to ``b`` over the current links."""
        with self._lock:
            return self._route_locked(a, b)

    # ------------------------------------------------------------- services
    def deploy(self, domain: str, name: str, handler: Callable[..., Any],
               description: str = "", replace: bool = False,
               template: Optional[str] = None) -> Service:
        self._check_name(name)
        with self._lock:
            dom = self._get(domain)
            if name in dom.services and not replace:
                raise ServiceExists(f"{domain}<{name}> already deployed")
            svc = Service(name, handler, description, template)
            dom.services[name] = svc
        self.bus.emit("service.deployed", domain=domain, service=name, description=description,
                      template=template)
        return svc

    def deploy_template(self, domain: str, template: str, name: Optional[str] = None,
                        replace: bool = False) -> Service:
        tmpl = self.templates.get(template)
        if tmpl is None:
            raise UnknownTemplate(f"unknown template {template!r} (have: {', '.join(sorted(self.templates)) or 'none'})")
        dom = self.domain(domain)
        return self.deploy(domain, name or template, tmpl.factory(dom), tmpl.description, replace,
                           template=template)

    def undeploy(self, domain: str, name: str) -> None:
        with self._lock:
            dom = self._get(domain)
            if dom.services.pop(name, None) is None:
                raise UnknownService(f"{domain}<{name}> is not deployed")
        self.bus.emit("service.removed", domain=domain, service=name)

    # ------------------------------------------------------------ messaging
    def send(self, src: str, target: str, payload: Any = None, *, kind: str = "request",
             ttl: Optional[int] = None) -> Message:
        """Fire-and-forget. ``target`` is ``"domain"`` or ``"domain<service>"``.
        If the service returns a value, a reply message is routed back to ``src``."""
        msg = self._make_message(src, target, payload, kind, None, ttl)
        self._dispatch(msg)
        return msg

    def call(self, src: str, target: str, payload: Any = None, timeout: float = 5.0) -> Any:
        """Blocking request/reply. Returns the reply payload, or raises
        :class:`DeliveryError`, :class:`RemoteError` or :class:`CallTimeout`."""
        msg = self._make_message(src, target, payload, "request", None, None)
        fut: Future = Future()
        with self._lock:
            self._pending[msg.id] = fut
        try:
            self._dispatch(msg)
            reply: Message = fut.result(timeout)
        except FutureTimeout:
            raise CallTimeout(f"no reply from {target!r} within {timeout}s") from None
        finally:
            with self._lock:
                self._pending.pop(msg.id, None)
        if reply.error:
            err = reply.payload.get("error") if isinstance(reply.payload, dict) else reply.payload
            raise RemoteError(str(err))
        return reply.payload

    # -------------------------------------------------------------- control
    def set_speed(self, speed: float, _quiet: bool = False) -> float:
        """Time scale: 2.0 makes every hop twice as fast, 0.5 half as fast."""
        self.speed = min(50.0, max(0.05, float(speed)))
        if not _quiet:
            self.bus.emit("net.speed", speed=self.speed)
        return self.speed

    def pause(self) -> None:
        self.paused = True
        self.scheduler.pause()
        self.bus.emit("net.paused", paused=True)

    def resume(self) -> None:
        self.paused = False
        self.scheduler.resume()
        self.bus.emit("net.paused", paused=False)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "seq": self.bus.last_seq,
                "time": time.time(),
                "speed": self.speed,
                "paused": self.paused,
                "domains": [
                    {
                        "name": d.name,
                        "meta": d.meta,
                        "label": d.label,
                        "label_set": d.has_label,
                        "received": d.received,
                        "services": [
                            {"name": s.name, "description": s.description,
                             "template": s.template, "calls": s.calls, "errors": s.errors}
                            for s in d.services.values()
                        ],
                    }
                    for d in self._domains.values()
                ],
                "links": [
                    {"a": l.a, "b": l.b, "latency_ms": l.latency_ms, "loss": l.loss}
                    for l in self._links.values()
                ],
                "templates": [{"name": t.name, "description": t.description}
                              for t in self.templates.values()],
                "stats": dict(self._stats),
            }

    # ============================================================ internals
    @staticmethod
    def _check_name(name: str) -> None:
        check_name(name)

    def _get(self, name: str) -> Domain:
        dom = self._domains.get(name)
        if dom is None:
            raise UnknownDomain(f"unknown domain {name!r}")
        return dom

    def _drop_link_locked(self, lk: Link) -> None:
        self._links.pop(_lk(lk.a, lk.b), None)
        self._adj.get(lk.a, {}).pop(lk.b, None)
        self._adj.get(lk.b, {}).pop(lk.a, None)

    def _route_locked(self, a: str, b: str) -> Optional[List[str]]:
        if a not in self._domains or b not in self._domains:
            return None
        if a == b:
            return [a]
        dist = {a: 0.0}
        prev: Dict[str, str] = {}
        heap: List[Tuple[float, str]] = [(0.0, a)]
        while heap:
            d, u = heapq.heappop(heap)
            if u == b:
                break
            if d > dist.get(u, float("inf")):
                continue
            for v, lk in self._adj.get(u, {}).items():
                nd = d + lk.latency_ms + 1e-6  # +epsilon: prefer fewer hops on ties
                if nd < dist.get(v, float("inf")):
                    dist[v] = nd
                    prev[v] = u
                    heapq.heappush(heap, (nd, v))
        if b not in prev:
            return None
        path = [b]
        while path[-1] != a:
            path.append(prev[path[-1]])
        return path[::-1]

    @staticmethod
    def _parse_target(target: str) -> Tuple[str, Optional[str]]:
        return parse_target(target)

    def _make_message(self, src: str, target: str, payload: Any, kind: str,
                      reply_to: Optional[str], ttl: Optional[int], error: bool = False) -> Message:
        dst, service = self._parse_target(target)
        with self._lock:
            self._get(src)
            mid = f"m{next(self._msg_ids)}"
        return Message(id=mid, src=src, dst=dst, service=service, payload=payload, kind=kind,
                       reply_to=reply_to, error=error,
                       ttl=self.max_hops if ttl is None else int(ttl), path=[src])

    def _dispatch(self, msg: Message) -> None:
        with self._lock:
            self._stats["sent"] += 1
            self._stats["in_flight"] += 1
        # origin tells the recorder which messages must be replayed ("external": sent by user
        # code, the traffic generator, the console...) and which a replayed service will
        # regenerate by itself ("service": sent from inside a service handler).
        origin = "service" if getattr(self._tls, "depth", 0) > 0 else "external"
        self.bus.emit("message.sent", id=msg.id, src=msg.src, dst=msg.dst, service=msg.service,
                      kind=msg.kind, preview=_preview(msg.payload), payload=msg.payload,
                      origin=origin)
        self._forward(msg, msg.src)

    def _forward(self, msg: Message, at: str) -> None:
        if at == msg.dst:
            self._deliver(msg)
            return
        with self._lock:
            path = self._route_locked(at, msg.dst)
            link = self._adj[at][path[1]] if path and len(path) > 1 else None
            nxt = path[1] if link else None
            if link:
                msg.ttl -= 1
            lost = bool(link) and self._rng.random() < link.loss
            latency = link.latency_ms if link else 0.0
        if link is None:
            self._drop(msg, at, "no_route")
            return
        if msg.ttl < 0:
            self._drop(msg, at, "ttl_exceeded")
            return
        dur_ms = latency / self.speed
        self.bus.emit("message.hop", id=msg.id, frm=at, to=nxt, kind=msg.kind,
                      duration_ms=dur_ms, src=msg.src, dst=msg.dst)
        self.scheduler.call_later(dur_ms / 1000.0, self._arrive, msg, at, nxt, lost)

    def _arrive(self, msg: Message, frm: str, to: str, lost: bool) -> None:
        with self._lock:
            dom_ok = to in self._domains
            link_ok = to in self._adj.get(frm, {})
        if not dom_ok:
            self._drop(msg, frm, "domain_removed", near=to)
        elif not link_ok:
            self._drop(msg, frm, "link_removed", near=to)
        elif lost:
            self._drop(msg, to, "loss", near=frm)
        else:
            msg.path.append(to)
            self._forward(msg, to)

    def _drop(self, msg: Message, at: str, reason: str, near: Optional[str] = None) -> None:
        with self._lock:
            self._stats["dropped"] += 1
            self._stats["in_flight"] -= 1
            key = msg.reply_to if msg.kind == "reply" else msg.id
            fut = self._pending.pop(key, None) if key else None
        self.bus.emit("message.dropped", id=msg.id, at=at, near=near, reason=reason,
                      kind=msg.kind, src=msg.src, dst=msg.dst, service=msg.service)
        if fut is not None and not fut.done():
            fut.set_exception(DeliveryError(f"{msg.kind} {msg.id} to {msg.target} dropped: {reason}"))

    def _deliver(self, msg: Message) -> None:
        with self._lock:
            dom = self._domains.get(msg.dst)
            svc = dom.services.get(msg.service) if (dom and msg.service) else None
        if dom is None:
            self._drop(msg, msg.dst, "domain_removed")
            return
        if msg.service and svc is None:
            self._drop(msg, msg.dst, "no_service")
            return
        fut = None
        with self._lock:
            self._stats["delivered"] += 1
            self._stats["in_flight"] -= 1
            dom.received += 1
            if msg.kind == "reply" and msg.reply_to:
                fut = self._pending.pop(msg.reply_to, None)
        self.bus.emit("message.delivered", id=msg.id, domain=dom.name, service=msg.service,
                      kind=msg.kind, src=msg.src, hops=max(0, len(msg.path) - 1),
                      latency_ms=(time.monotonic() - msg.created) * 1000.0,
                      preview=_preview(msg.payload))
        if fut is not None:
            if not fut.done():
                fut.set_result(msg)
            return
        if svc is not None:
            self._pool.submit(self._run_service, dom, svc, msg)
        else:
            dom.inbox.append(msg)
            if dom.on_message is not None:
                self._pool.submit(self._run_hook, dom, msg)

    def _run_service(self, dom: Domain, svc: Service, msg: Message) -> None:
        svc.calls += 1
        self._tls.depth = getattr(self._tls, "depth", 0) + 1
        try:
            result = svc.invoke(ServiceContext(self, dom, msg), msg)
        except Exception as exc:
            svc.errors += 1
            log.warning("service %s<%s> failed: %r", dom.name, svc.name, exc)
            self.bus.emit("service.error", domain=dom.name, service=svc.name, id=msg.id,
                          error=repr(exc))
            if msg.kind == "request":
                self._reply(dom, msg, {"error": str(exc) or repr(exc)}, error=True)
            return
        finally:
            self._tls.depth -= 1
        if result is not None and msg.kind == "request":
            self._reply(dom, msg, result)

    def _run_hook(self, dom: Domain, msg: Message) -> None:
        try:
            dom.on_message(msg)  # type: ignore[misc]
        except Exception as exc:
            self.bus.emit("service.error", domain=dom.name, service="<on_message>", id=msg.id,
                          error=repr(exc))

    def _reply(self, dom: Domain, msg: Message, payload: Any, error: bool = False) -> None:
        try:
            reply = self._make_message(dom.name, msg.src, payload, "reply", msg.id, None, error)
        except DomainNetError:
            return  # the replying domain was removed meanwhile
        self._dispatch(reply)
