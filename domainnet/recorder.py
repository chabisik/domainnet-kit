"""Record a running simulation into a scenario file that can be replayed.

The output is exactly the text format read by :class:`~domainnet.Scenario`
(``<seconds>  <command>``), so::

    with Recorder(net, "run.scn"):
        ...                                   # play with the dashboard, traffic, churn...
    Scenario.from_file("run.scn").start(ctl)  # later: replay it

What is recorded (everything needed to rebuild the *causes*, not the effects):

* the state that already exists when recording starts (written at ``t = 0``)
* domains added/removed, labels, links created/updated/cut, speed changes
* services deployed from a registered **template**, and their removal
* messages sent from outside any service handler (user code, console, traffic
  generator); messages sent by handlers (``ctx.send``) and all replies are *not*
  recorded because replaying the services regenerates them.

Pauses are not written as commands (a paused scheduler could never fire the
``resume`` step); the time spent paused is subtracted instead, so a replay
plays the same sequence without the gaps.

Limits: services deployed from a bare Python function (not a template) cannot be
written to a text file; they are noted as comments. Every recorded message replays
as a request (``send``), and domain ``meta`` is not recorded.
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import threading
from typing import Any, Callable, Optional, Set, TextIO, Tuple

from .core import Network, format_target
from .events import Event

log = logging.getLogger("domainnet.recorder")


def _json(value: Any) -> Tuple[str, bool]:
    """One-line, ASCII-only JSON. ``#`` is escaped so a comment-stripper can never
    cut a payload; U+2028 and friends are escaped so the line can't be split.
    Returns (text, lossless)."""
    try:
        text, ok = json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False), True
    except (TypeError, ValueError):
        text, ok = json.dumps(repr(value), ensure_ascii=True), False
    return text.replace("#", "\\u0023"), ok


class Recorder:
    """Write a replayable scenario file while the network runs.

    Parameters
    ----------
    net:       the network to record
    path:      output file (overwritten); must differ from any scenario you are replaying
    messages:  also record externally sent messages (default True)
    """

    def __init__(self, net: Network, path: "str | os.PathLike[str]", *, messages: bool = True) -> None:
        self.net = net
        self.path = os.fspath(path)
        self.messages = messages
        self.lines = 0
        self._fh: Optional[TextIO] = None
        self._unsub: Optional[Callable[[], None]] = None
        self._t0 = 0.0
        self._paused_total = 0.0
        self._paused_since: Optional[float] = None
        self._replayable: Set[Tuple[str, str]] = set()   # (domain, service) a replay can recreate

    # ------------------------------------------------------------ lifecycle
    @property
    def recording(self) -> bool:
        return self._fh is not None

    def start(self) -> "Recorder":
        if self._fh is not None:
            return self
        self._fh = open(self.path, "w", encoding="utf-8")
        try:
            now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
            self._fh.write(f"# domainnet recording, started {now}\n"
                           "# replay: Scenario.from_file(this_file).start(controller)"
                           "   or   python -m domainnet --scenario this_file\n")
            snap = self.net.snapshot()
            self._t0 = snap["time"]
            self._paused_since = self._t0 if snap["paused"] else None
            self._paused_total = 0.0
            self._write_initial_state(snap)
            self._unsub = self.net.bus.subscribe(self._on_event)
        except BaseException:
            self._fh.close()
            self._fh = None
            raise
        return self

    def stop(self) -> None:
        if self._fh is None:
            return
        if self._unsub:
            self._unsub()
            self._unsub = None
        try:
            self._fh.write(f"# end of recording: {self.lines} commands\n")
        finally:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "Recorder":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # -------------------------------------------------------------- writing
    def _emit(self, t: float, command: str) -> None:
        if self._fh is None:
            return
        self._fh.write(f"{max(0.0, t):9.3f}  {command}\n")
        self._fh.flush()
        self.lines += 1

    def _note(self, text: str) -> None:
        if self._fh is not None:
            self._fh.write(f"# {text}\n")
            self._fh.flush()

    def _label_cmd(self, name: str, label: Optional[str], custom: bool) -> str:
        return f"label {name} {_json(label)[0]}" if custom and label else f"label {name}"

    def _write_initial_state(self, snap: dict) -> None:
        if snap["speed"] != 1.0:
            self._emit(0.0, f"speed {snap['speed']!r}")
        for d in snap["domains"]:
            self._emit(0.0, f"add {d['name']}")
            if d["label_set"]:
                self._emit(0.0, self._label_cmd(d["name"], d["label"], True))
        for l in snap["links"]:
            self._emit(0.0, f"link {l['a']} {l['b']} {l['latency_ms']!r} {l['loss']!r}")
        for d in snap["domains"]:
            for s in d["services"]:
                self._record_deploy(0.0, d["name"], s["name"], s["template"])

    def _record_deploy(self, t: float, domain: str, service: str, template: Optional[str]) -> None:
        key = (domain, service)
        if key in self._replayable:        # redeployed over an existing service
            self._emit(t, f"undeploy {domain} {service}")
            self._replayable.discard(key)
        if template is None:
            self._note(f"NOT REPLAYABLE: custom service {format_target(domain, service)} was "
                       "deployed from code; register it as a template to make it replayable")
            return
        suffix = "" if service == template else f" as {service}"
        self._emit(t, f"deploy {domain} {template}{suffix}")
        self._replayable.add(key)

    # --------------------------------------------------------------- events
    def _clock(self, ts: float) -> float:
        paused = self._paused_total
        if self._paused_since is not None:     # frozen while paused
            paused += ts - self._paused_since
        return ts - self._t0 - paused

    def _on_event(self, ev: Event) -> None:
        try:
            self._handle(ev)
        except Exception:                      # recording must never break the network
            log.exception("recorder failed on %s", ev.type)

    def _handle(self, ev: Event) -> None:
        k, d = ev.type, ev.data
        if k == "net.paused":
            if d["paused"] and self._paused_since is None:
                self._paused_since = ev.ts
            elif not d["paused"] and self._paused_since is not None:
                self._paused_total += ev.ts - self._paused_since
                self._paused_since = None
            return
        t = self._clock(ev.ts)
        if k == "net.speed":
            self._emit(t, f"speed {d['speed']!r}")
        elif k == "domain.added":
            self._emit(t, f"add {d['name']}")
            if d.get("custom"):
                self._emit(t, self._label_cmd(d["name"], d["label"], True))
        elif k == "domain.label":
            self._emit(t, self._label_cmd(d["name"], d["label"], bool(d.get("custom"))))
        elif k == "domain.removed":
            self._emit(t, f"rm {d['name']}")
            self._replayable = {key for key in self._replayable if key[0] != d["name"]}
        elif k in ("link.added", "link.updated"):
            self._emit(t, f"link {d['a']} {d['b']} {d['latency_ms']!r} {d['loss']!r}")
        elif k == "link.removed":
            if d.get("reason") == "unlinked":  # links dropped by a domain removal follow from `rm`
                self._emit(t, f"unlink {d['a']} {d['b']}")
        elif k == "service.deployed":
            self._record_deploy(t, d["domain"], d["service"], d.get("template"))
        elif k == "service.removed":
            key = (d["domain"], d["service"])
            if key in self._replayable:
                self._replayable.discard(key)
                self._emit(t, f"undeploy {d['domain']} {d['service']}")
        elif k == "message.sent" and self.messages:
            if d.get("origin") != "external" or d.get("kind") not in ("request", "event"):
                return
            line = f"send {d['src']} {format_target(d['dst'], d.get('service'))}"
            if d.get("payload") is not None:
                text, lossless = _json(d["payload"])
                line += f" {text}"
                if not lossless:
                    self._note("next payload is not JSON-serializable; recorded as its repr()")
            self._emit(t, line)
