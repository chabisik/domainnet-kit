"""One control surface for everything: Python, JSON over HTTP, and a text
command language. The dashboard buttons, the console box, scenario files and
your own code all go through :class:`Controller`.
"""
from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

from .automation import Churn, TrafficGenerator, strip_comment
from .core import DomainNetError, Network, format_target

HELP = """\
add [NAME ...]                  create domain(s); no name = auto-named
rm NAME                         remove a domain (its links and services go too)
link A B [LATENCY_MS] [LOSS]    connect two domains (or update the link)
unlink A B                      cut the link
deploy DOMAIN TEMPLATE [as SVC] deploy a service template inside a domain
undeploy DOMAIN SERVICE         remove a service
send SRC TARGET [PAYLOAD]       TARGET is 'domain' or 'domain<service>' (PAYLOAD may be JSON)
speed X                         time scale (0.25 slower ... 4 faster)
pause | resume                  freeze / thaw all in-flight messages
traffic on [RATE] | off | RATE  random message generator (msgs/second)
churn on [SECONDS] | off        random domain/link/service changes
reset                           remove everything
state                           print the current topology
templates                       list deployable service templates
"""


def _req(cmd: Dict[str, Any], key: str) -> Any:
    v = cmd.get(key)
    if v is None or v == "":
        raise ValueError(f"missing argument {key!r}")
    return v


def _parse_payload(text: str) -> Any:
    try:
        return json.loads(text)
    except ValueError:
        return text


def _num(tok: str, what: str) -> float:
    try:
        return float(tok)
    except ValueError:
        raise ValueError(f"{what} must be a number, got {tok!r}") from None


class Controller:
    """Executes commands against a :class:`Network`.

    ``execute({"cmd": "link", "a": "x", "b": "y"})`` and
    ``run_line("link x y")`` are equivalent; both return
    ``{"ok": True, ...}`` or ``{"ok": False, "error": "..."}`` and never raise
    for user mistakes.
    """

    def __init__(self, net: Network, seed: Optional[int] = None) -> None:
        self.net = net
        self.traffic = TrafficGenerator(net, seed=seed)
        self.churn = Churn(net, seed=seed)
        self._commands: Dict[str, Callable[[Dict[str, Any]], Any]] = {
            "add_domain": self._add_domain,
            "remove_domain": self._remove_domain,
            "link": self._link,
            "unlink": self._unlink,
            "deploy": self._deploy,
            "undeploy": self._undeploy,
            "send": self._send,
            "speed": self._speed,
            "pause": lambda c: self.net.pause(),
            "resume": lambda c: self.net.resume(),
            "traffic": self._traffic,
            "churn": self._churn,
            "reset": self._reset,
            "state": lambda c: self.snapshot(),
            "templates": lambda c: {t: self.net.templates[t].description for t in self.net.templates},
            "help": lambda c: HELP,
        }

    # ------------------------------------------------------------ public
    def execute(self, cmd: Dict[str, Any]) -> Dict[str, Any]:
        fn = self._commands.get(str(cmd.get("cmd")))
        if fn is None:
            return {"ok": False, "error": f"unknown command {cmd.get('cmd')!r}; try 'help'"}
        try:
            result = fn(cmd)
        except (DomainNetError, ValueError, TypeError) as exc:
            return {"ok": False, "error": str(exc) or repr(exc)}
        return {"ok": True} if result is None else {"ok": True, "result": result}

    def run_line(self, line: str) -> Dict[str, Any]:
        try:
            cmd = self.parse_line(line)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        if cmd is None:
            return {"ok": True}
        return self.execute(cmd)

    def snapshot(self) -> Dict[str, Any]:
        snap = self.net.snapshot()
        snap["traffic"] = {"running": self.traffic.running, "rate": self.traffic.rate}
        snap["churn"] = {"running": self.churn.running, "interval": self.churn.interval}
        return snap

    def stop_automation(self) -> None:
        self.traffic.stop()
        self.churn.stop()

    # ---------------------------------------------------------- text form
    def parse_line(self, line: str) -> Optional[Dict[str, Any]]:
        line = strip_comment(line)
        if not line:
            return None
        parts = line.split()
        op, args = parts[0].lower(), parts[1:]

        def need(n: int, usage: str) -> None:
            if len(args) < n:
                raise ValueError(f"usage: {usage}")

        if op in ("add", "+"):
            return {"cmd": "add_domain", "names": args}
        if op in ("rm", "remove", "del", "-"):
            need(1, "rm NAME")
            return {"cmd": "remove_domain", "name": args[0]}
        if op in ("link", "connect"):
            need(2, "link A B [LATENCY_MS] [LOSS]")
            cmd: Dict[str, Any] = {"cmd": "link", "a": args[0], "b": args[1]}
            if len(args) > 2:
                cmd["latency_ms"] = _num(args[2], "latency")
            if len(args) > 3:
                cmd["loss"] = _num(args[3], "loss")
            return cmd
        if op in ("unlink", "disconnect"):
            need(2, "unlink A B")
            return {"cmd": "unlink", "a": args[0], "b": args[1]}
        if op == "deploy":
            need(2, "deploy DOMAIN TEMPLATE [as SERVICE]")
            cmd = {"cmd": "deploy", "domain": args[0], "template": args[1]}
            if len(args) >= 4 and args[2].lower() == "as":
                cmd["service"] = args[3]
            return cmd
        if op == "undeploy":
            need(2, "undeploy DOMAIN SERVICE")
            return {"cmd": "undeploy", "domain": args[0], "service": args[1]}
        if op == "send":
            need(2, "send SRC TARGET [PAYLOAD]")
            rest = line.split(None, 3)
            payload = _parse_payload(rest[3]) if len(rest) > 3 else None
            return {"cmd": "send", "src": args[0], "target": args[1], "payload": payload}
        if op == "speed":
            need(1, "speed X")
            return {"cmd": "speed", "value": _num(args[0], "speed")}
        if op in ("pause", "resume", "reset", "state", "templates", "help"):
            return {"cmd": op}
        if op == "traffic":
            need(1, "traffic on [RATE] | off | RATE")
            if args[0] == "off":
                return {"cmd": "traffic", "on": False}
            if args[0] == "on":
                return {"cmd": "traffic", "on": True, **({"rate": _num(args[1], "rate")} if len(args) > 1 else {})}
            return {"cmd": "traffic", "rate": _num(args[0], "rate")}
        if op == "churn":
            need(1, "churn on [SECONDS] | off")
            if args[0] == "off":
                return {"cmd": "churn", "on": False}
            if args[0] == "on":
                return {"cmd": "churn", "on": True, **({"interval": _num(args[1], "interval")} if len(args) > 1 else {})}
            raise ValueError("usage: churn on [SECONDS] | off")
        raise ValueError(f"unknown command {op!r}; try 'help'")

    # ----------------------------------------------------------- commands
    def _add_domain(self, c: Dict[str, Any]) -> Any:
        names: List[Optional[str]] = list(c.get("names") or [])
        if c.get("name"):
            names.append(c["name"])
        if not names:
            names = [None]
        created = [self.net.add_domain(n, **(c.get("meta") or {})).name for n in names]
        return {"created": created}

    def _remove_domain(self, c: Dict[str, Any]) -> Any:
        self.net.remove_domain(_req(c, "name"))

    def _link(self, c: Dict[str, Any]) -> Any:
        lk = self.net.link(_req(c, "a"), _req(c, "b"), c.get("latency_ms"), c.get("loss"))
        return {"a": lk.a, "b": lk.b, "latency_ms": lk.latency_ms, "loss": lk.loss}

    def _unlink(self, c: Dict[str, Any]) -> Any:
        self.net.unlink(_req(c, "a"), _req(c, "b"))

    def _deploy(self, c: Dict[str, Any]) -> Any:
        svc = self.net.deploy_template(_req(c, "domain"), _req(c, "template"), c.get("service") or None)
        return {"service": svc.name}

    def _undeploy(self, c: Dict[str, Any]) -> Any:
        self.net.undeploy(_req(c, "domain"), _req(c, "service"))

    def _send(self, c: Dict[str, Any]) -> Any:
        # either {"target": "domain<service>"} or the structured {"dst": ..., "service": ...}
        target = format_target(c["dst"], c.get("service")) if c.get("dst") else _req(c, "target")
        msg = self.net.send(_req(c, "src"), target, c.get("payload"))
        return {"id": msg.id}

    def _speed(self, c: Dict[str, Any]) -> Any:
        return {"speed": self.net.set_speed(float(_req(c, "value")))}

    def _traffic(self, c: Dict[str, Any]) -> Any:
        if "rate" in c and c["rate"] is not None:
            self.traffic.rate = max(0.05, float(c["rate"]))
        if c.get("on") is True:
            self.traffic.start()
        elif c.get("on") is False:
            self.traffic.stop()
        return {"running": self.traffic.running, "rate": self.traffic.rate}

    def _churn(self, c: Dict[str, Any]) -> Any:
        if c.get("interval") is not None:
            self.churn.interval = max(0.2, float(c["interval"]))
        if c.get("on") is True:
            self.churn.start()
        elif c.get("on") is False:
            self.churn.stop()
        return {"running": self.churn.running, "interval": self.churn.interval}

    def _reset(self, c: Dict[str, Any]) -> Any:
        self.net.reset()
