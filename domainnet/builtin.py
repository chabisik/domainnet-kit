"""Ready-made service templates.

Templates are *factories*: ``factory(domain) -> handler``. That lets each
deployment keep its own state (see ``counter``) and lets the dashboard / text
commands deploy services by name. Register your own with
``net.register_template(name, factory, description)``.
"""
from __future__ import annotations

import itertools
import random
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .core import Network


def install_builtins(net: "Network") -> None:
    reg = net.register_template

    reg("echo", lambda dom: (lambda ctx, msg: msg.payload),
        "Replies with whatever it receives")

    reg("upper", lambda dom: (lambda ctx, msg: str(msg.payload).upper()),
        "Replies with the payload in upper case")

    reg("ping", lambda dom: (lambda ctx, msg: "pong"),
        "Replies 'pong'")

    def counter(dom):
        n = itertools.count(1)
        return lambda ctx, msg: {"count": next(n)}
    reg("counter", counter, "Counts how many times it was called (per deployment)")

    reg("clock", lambda dom: (lambda ctx, msg: {"domain": dom.name, "time": time.strftime("%H:%M:%S")}),
        "Replies with the local time")

    reg("dice", lambda dom: (lambda ctx, msg: random.randint(1, 6)),
        "Replies with a random number from 1 to 6")

    def broadcast(dom):
        def handler(ctx, msg):
            sent = 0
            for nb in ctx.net.neighbors(ctx.domain.name):
                if nb != msg.src:
                    ctx.send(nb, msg.payload, kind="event")
                    sent += 1
            return {"forwarded_to": sent}
        return handler
    reg("broadcast", broadcast, "Forwards the payload to every neighbour as an event")

    def gateway(dom):
        def handler(ctx, msg):
            p = msg.payload
            if not (isinstance(p, dict) and "target" in p):
                return {"error": "payload must look like {'target': 'domain/service', 'payload': ...}"}
            return ctx.call(p["target"], p.get("payload"), timeout=5.0)
        return handler
    reg("gateway", gateway, "Relays {'target','payload'} to another domain's service and returns its answer")
