# domainnet

A small, **dependency-free** Python framework (standard library only, Python ≥ 3.9) to simulate
**many independent domains that exchange messages and each host their own services**, with a live
web dashboard that shows:

- messages flowing hop by hop (requests, replies, events, drops)
- each domain's internal services (they flash when called)
- domains being **added** (grow in) and **removed** (fade out in red)
- links being created, updated and cut (with latency and loss)

Everything is controllable from Python, a text console, JSON over HTTP, timed scenario files, or the
buttons in the dashboard.

```bash
pip install -e .            # or just add this folder to PYTHONPATH
python -m domainnet         # built-in demo → open http://127.0.0.1:8765
python -m domainnet --empty # blank network, build it live
python -m domainnet --churn 3 --traffic 3   # let it evolve by itself
python -m unittest discover -s tests        # 27 tests
```

## Concepts

| Concept | Meaning |
|---|---|
| **Domain** | An isolated participant: owns services, an inbox, free-form `meta`. |
| **Link** | Undirected connection with `latency_ms` and `loss` (0–1). |
| **Service** | A handler deployed *inside* a domain. Return a value → it is sent back as a reply. |
| **Message** | Addressed to `"domain"` or `"domain/service"`. Routed hop by hop over the links that exist *right now* (lowest total latency), so topology can change mid-flight. |
| **Template** | A named service factory the dashboard / text commands can deploy (`echo`, `counter`, …). |

A message is dropped (and a `message.dropped` event emitted with the reason) on: `no_route`,
`no_service`, `loss`, `link_removed`, `domain_removed`, `ttl_exceeded`.

## Use it from your project

```python
from domainnet import Network, Controller, DashboardServer

net = Network(default_latency_ms=120)
shop, bank = net.add_domain("shop"), net.add_domain("bank")
net.link("shop", "bank", latency_ms=100)

@bank.service("charge")
def charge(ctx, msg):                  # or: def charge(msg)
    return {"ok": msg.payload["amount"] <= 100}

print(shop.call("bank/charge", {"amount": 40}))      # blocking request/reply → {'ok': True}
shop.send("bank/charge", {"amount": 5})              # fire and forget
net["bank"].deploy("audit", lambda ctx, msg: "ok")   # deploy a service at any time
net.remove_domain("bank")                            # …or remove a whole domain

DashboardServer(net, Controller(net), port=8765).start()   # optional, runs in a thread
```

Inside a handler, `ctx.call(...)` / `ctx.send(...)` talk to other domains (see
`examples/embed.py` for a checkout → bank → courier chain). Handlers run on a worker pool
(`Network(workers=8)`); a handler that blocks in `ctx.call` occupies one worker while it waits.

**Observe everything** (log, metrics, your own WebSocket):

```python
net.bus.subscribe(lambda ev: print(ev.type, ev.data), types=["message.", "domain."])  # prefixes
```

Event types: `domain.added/removed`, `link.added/updated/removed`, `service.deployed/removed/error`,
`message.sent/hop/delivered/dropped`, `net.paused`, `net.speed`.

**Your own templates** (so the dashboard can deploy them by name):

```python
net.register_template("audit", lambda domain: (lambda ctx, msg: f"{domain.name} saw {msg.payload}"), "audit line")
```

## Controlling the demo

All of these are the same commands (`Controller.execute(dict)` / `Controller.run_line(str)`):

```
add [NAME ...]                  create domain(s); no name = auto-named
rm NAME                         remove a domain
link A B [LATENCY_MS] [LOSS]    connect / update a link
unlink A B
deploy DOMAIN TEMPLATE [as SVC]
undeploy DOMAIN SERVICE
send SRC TARGET [PAYLOAD]       TARGET = domain | domain/service ; PAYLOAD may be JSON
speed X | pause | resume        time scale; pause freezes in-flight messages
traffic on [RATE] | off | RATE  random messages (msgs/s)
churn on [SECONDS] | off        random domains/links/services appearing and disappearing
reset | state | templates | help
```

- **Dashboard:** click a domain → deploy/undeploy services, link/unlink, send a message, remove it.
  Top bar: add domain, pause, speed, traffic, churn, reset. The console box takes the commands above.
- **Scenario file** (`python -m domainnet --scenario my.txt`, or `Scenario.from_text(...).start(ctl)`):

  ```
  # seconds  command
  0    add alpha bravo
  1    link alpha bravo 120
  3    deploy bravo counter
  8    rm bravo
  ```
- **HTTP:** `POST /api/command` with `{"cmd": "link", "a": "x", "b": "y"}` or `{"line": "link x y"}`
  (must be `Content-Type: application/json`). `GET /api/state`, `GET /api/events?since=N`,
  `GET /api/stream` (Server-Sent Events).

## Embedding the dashboard elsewhere

The server is plain `http.server`, bound to `127.0.0.1` by default. It has no authentication, so do
not expose it on an untrusted network. To integrate with an existing web stack instead, ignore
`DashboardServer` and forward `net.bus` events to your own WebSocket/SSE endpoint, calling
`Controller.execute()` from your own routes; `domainnet/static/index.html` talks only to
`/api/state`, `/api/stream` and `/api/command`, so it can be served from anywhere that provides them.

## Scope and limits

- It is an in-process **simulation**: domains are objects, not OS processes or hosts. Latency/loss are
  modelled, not measured. To drive real containers, call Containernet/Containerlab from your own
  service handlers and keep this as the visualization/control layer.
- Links are undirected; routing is lowest-latency (Dijkstra) recomputed at every hop.
- A service that returns `None` sends no reply (a blocking `call()` then times out).
- One process, thread-safe; not designed for thousands of domains (the canvas view is comfortable up to
  roughly 30).
