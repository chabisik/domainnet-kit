# domainnet

A small, **dependency-free** Python framework (standard library only, Python ≥ 3.9) to simulate
**many independent domains that exchange messages and each host their own services**, with a live
web dashboard that shows:

- messages flowing hop by hop (requests, replies, events, drops)
- each domain, under a **label** you choose, and its internal services (they flash when called)
- domains being **added** (grow in) and **removed** (fade out in red)
- links being created, updated and cut (with latency and loss)

Everything is controllable from Python, a text console, JSON over HTTP, timed **scenario files**, or
the buttons in the dashboard, and any session can be **recorded to a scenario file and replayed**.

```bash
pip install -e .            # or just add this folder to PYTHONPATH
python -m domainnet         # built-in demo → open http://127.0.0.1:8765
python -m domainnet --empty # blank network, build it live
python -m domainnet --churn 3 --traffic 3          # let it evolve by itself
python -m domainnet --empty --record my.scn        # record what you do…
python -m domainnet --scenario my.scn              # …and replay it later
python examples/scenario_replay.py                 # scenario + recorder walkthrough
python -m unittest discover -s tests               # run the test suite
```

| Option | Meaning |
|---|---|
| `--host`, `--port` | where the dashboard listens (default `127.0.0.1:8765`; port `0` = any free port) |
| `--empty` | start with no domains (otherwise the built-in demo scenario runs) |
| `--scenario FILE` | run your own scenario file instead of the demo |
| `--record FILE` | record the whole session into `FILE` (scenario format; overwritten) |
| `--traffic RATE`, `--churn SECONDS` | start the random message / topology generators |
| `--speed X`, `--seed N`, `-v` | initial time scale, random seed, logging |

## Concepts

| Concept | Meaning |
|---|---|
| **Domain** | An isolated participant: owns services, an inbox, free-form `meta`. Identified by its **name**; shown under its **label**. |
| **Link** | Undirected connection with `latency_ms` and `loss` (0–1). |
| **Service** | A handler deployed *inside* a domain. Return a value → it is sent back as a reply. |
| **Message** | Addressed to `"domain"` or `"domain<service>"`. Routed hop by hop over the links that exist *right now* (lowest total latency), so topology can change mid-flight. |
| **Template** | A named service factory the dashboard / text commands can deploy (`echo`, `counter`, …). |
| **Scenario** | A timed script of console commands (`<seconds>  <command>`), run on the network's clock. |
| **Recorder** | Writes what happens in a running network as a scenario file. |

A message is dropped (and a `message.dropped` event emitted with the reason) on: `no_route`,
`no_service`, `loss`, `link_removed`, `domain_removed`, `ttl_exceeded`.

## Names, labels and addresses

**Names** (domains, services, templates) are the ids. They are free-form: any non-empty string with
no whitespace or control characters and no `<` or `>`. No length limit, Unicode is fine, and
**URLs work as-is** (`https://shop.example.com/eu`, `http://Bank.example.org:8080/v1/?q=1#frag`).
Names are used exactly as typed (no case or trailing-slash normalization) and cannot be renamed.

**Labels** are what the dashboard *shows* for a domain. A label is optional display text: spaces, URLs
and any Unicode are fine (single line), and several domains may share one. **When no label is set it
is the name** (`domain.label == domain.name`; for a URL name the dashboard draws just the host).
Commands, addresses, logs and the API always use the name.

```python
shop = net.add_domain("https://shop.example.com/eu", label="Online shop")   # at creation…
shop.label = "Shop (EU)"                                                    # …or later
net.set_label("https://shop.example.com/eu", None)                          # None or "" resets to the name
shop.label, shop.has_label                                                  # effective text, is-it-set
```

Console / scenario: `label NAME TEXT` (rest of the line, or a JSON string such as `"Shop EU"`);
`label NAME` resets. JSON: `{"cmd": "label", "name": "x", "label": "Shop EU"}`, or pass `"label"` to
`add_domain`. In the dashboard use the *label* box in the side panel or the label field next to
"+ Domain". Labels longer than the circle wrap onto two lines; hover a domain to see
`label (name)`.

A message **address** is either a domain, or a service inside a domain:

```
https://shop.example.com/eu            → the domain
https://shop.example.com/eu<quote>     → service "quote" in that domain
```

`<` and `>` can never appear unescaped in a URL, so this syntax is unambiguous and never depends on
which domains exist (`https://a.com` and `https://a.com/x` can coexist). Helpers:
`parse_target("a<s>") -> ("a", "s")` and `format_target("a", "s") -> "a<s>"`. In JSON commands you
can also pass the parts separately: `{"cmd": "send", "src": "x", "dst": "https://a.com", "service": "quote"}`.
Quote addresses in a terminal (`<` and `>` are shell redirections). The old `domain/service` syntax
is gone: `a/b` is simply a domain called `a/b`.

**Comments:** in console and scenario lines a `#` starts a comment only when whitespace surrounds it
(or it is the whole line), so URL fragments (`…/page#top`) and payloads like `#1` are safe. To put
a bare ` # ` inside a label or payload, write it as `\u0023` in a JSON string (`"Warehouse \u0023 2"`).

## Use it from your project

```python
from domainnet import Network, Controller, DashboardServer

net = Network(default_latency_ms=120)
shop, bank = net.add_domain("shop", label="Online shop"), net.add_domain("bank")
net.link("shop", "bank", latency_ms=100)

@bank.service("charge")
def charge(ctx, msg):                  # or: def charge(msg)
    return {"ok": msg.payload["amount"] <= 100}

print(shop.call("bank<charge>", {"amount": 40}))     # blocking request/reply → {'ok': True}
shop.send("bank<charge>", {"amount": 5})             # fire and forget
net["bank"].deploy("audit", lambda ctx, msg: "ok")   # deploy a service at any time
net.remove_domain("bank")                            # …or remove a whole domain

DashboardServer(net, Controller(net), port=8765).start()   # optional, runs in a thread
```

Inside a handler, `ctx.call(...)` / `ctx.send(...)` talk to other domains (see
`examples/embed.py` for a checkout → bank → courier chain). Handlers run on a worker pool
(`Network(workers=8)`); a handler that blocks in `ctx.call` occupies one worker while it waits.
`net.close()` stops the network and makes any `call()` still waiting fail immediately.

**Observe everything** (log, metrics, your own WebSocket):

```python
net.bus.subscribe(lambda ev: print(ev.type, ev.data), types=["message.", "domain."])  # prefixes
```

Event types: `domain.added/removed/label`, `link.added/updated/removed`,
`service.deployed/removed/error`, `message.sent/hop/delivered/dropped`, `net.paused`, `net.speed`.
`message.sent` carries the raw `payload` and an `origin` (`"external"` for messages sent by user code,
the console or the traffic generator, `"service"` for messages sent from inside a handler).

**Your own templates** (so the dashboard, console and scenarios can deploy them by name):

```python
net.register_template("audit", lambda domain: (lambda ctx, msg: f"{domain.name} saw {msg.payload}"), "audit line")
```

## Controlling the demo

All of these are the same commands (`Controller.execute(dict)` / `Controller.run_line(str)`):

```
add [NAME ...]                  create domain(s); no name = auto-named
label NAME [TEXT]               set the dashboard label of a domain (no TEXT = reset to name)
rm NAME                         remove a domain
link A B [LATENCY_MS] [LOSS]    connect / update a link
unlink A B
deploy DOMAIN TEMPLATE [as SVC]
undeploy DOMAIN SERVICE
send SRC TARGET [PAYLOAD]       TARGET = domain | domain<service> ; PAYLOAD may be JSON
speed X | pause | resume        time scale; pause freezes in-flight messages
traffic on [RATE] | off | RATE  random messages (msgs/s)
churn on [SECONDS] | off        random domains/links/services appearing and disappearing
reset | state | templates | help
```

- **Dashboard:** click a domain → set its label, deploy/undeploy services, link/unlink, send a message,
  remove it. Top bar: add domain (name and label optional), pause, speed, traffic, churn, reset. The
  console box takes the commands above. The side panel can be resized (drag the divider, arrow keys
  when it is focused, double-click to reset) or hidden with the button at the top right of the graph;
  width and state are remembered in the browser.
- **HTTP:** `POST /api/command` with `{"cmd": "link", "a": "x", "b": "y"}` or `{"line": "link x y"}`
  (must be `Content-Type: application/json`). `GET /api/state`, `GET /api/events?since=N`,
  `GET /api/stream` (Server-Sent Events).

## Scenarios

A scenario is a text file (or a Python list) of timed console commands:

```
# seconds  command
0     add https://shop.example.com bank
0     label https://shop.example.com "Online shop"
0.4   link https://shop.example.com bank 120
3     deploy bank counter
5     send https://shop.example.com bank<counter> {"hello": "world"}
8     rm bank
```

Times count from `start()`, are in seconds, and follow `net.pause()` (a paused network pauses its
scenarios too). `#` comments and blank lines are allowed (see the comment rule above). A failing step
is logged and collected in `scenario.errors`; the rest keeps running.

```python
from domainnet import Network, Controller, Scenario

net = Network(); ctl = Controller(net)
sc = Scenario.from_file("examples/shop.scn")          # or Scenario.from_text("..."), or Scenario([(0, "add a"), ...])
sc.start(ctl)                                          # runs on the network's scheduler, returns immediately
sc.wait(timeout=sc.duration + 5)                       # optional: block until the last step ran
print(len(sc), sc.duration, sc.errors)                 # steps, seconds, [(time, line, message), ...]
sc.cancel()                                            # stop early (also releases wait())
sc.to_text(); sc.save("copy.scn")                      # serialize back to the same format
```

From the CLI: `python -m domainnet --scenario examples/shop.scn`.

- `examples/shop.scn` – a ready-to-run scenario with URL names, labels, services, traffic and removals.
- `examples/scenario_replay.py` – the whole workflow in one script: build scenarios in Python and
  from text, run them while recording, replay the recording into a fresh network and check it matches.

## Recording and replaying a session

Give the CLI a file name and everything that happens is written to it as a scenario, flushed line
by line (so a crash loses nothing):

```bash
python -m domainnet --empty --record session.scn     # click around, use the console, turn traffic on…
# Ctrl+C
python -m domainnet --scenario session.scn           # replay it
```

or from Python:

```python
from domainnet import Recorder

with Recorder(net, "session.scn"):                   # or rec = Recorder(...).start(); ...; rec.stop()
    ...                                              # whatever happens to `net` in this block is recorded
Scenario.from_file("session.scn").start(Controller(other_net))
```

```
# domainnet recording, started 2026-10-30T09:39:13+01:00
    0.000  add https://shop.example.com
    0.000  label https://shop.example.com "Online shop"
    0.301  link bank https://shop.example.com 40.0 0.0
    0.800  send https://shop.example.com bank<accounts> "charge"
    1.600  unlink courier https://shop.example.com
# end of recording: 16 commands
```

**What is recorded** is what *caused* things, so a replay re-creates the effects:

- the state that already exists when recording starts, written at `t = 0`
- domains added/removed, labels, links created/updated/cut, speed changes
- services deployed from a **template**, and their removal
- messages sent from outside any handler: your code, the console, the dashboard, **the traffic
  generator** (so random traffic replays exactly) and churn (as the adds/links/deploys it caused)
- not recorded: replies, and messages sent by handlers via `ctx.send/ctx.call` (replaying the
  services regenerates them), and `traffic`/`churn` toggles themselves

**Pauses** are not recorded as commands (a paused clock could never fire the `resume` step); the paused
time is subtracted so the replay plays the same sequence without the gaps.

**Limits**

- A service deployed from a bare Python function (not a registered template) cannot be written to a
  text file. It is noted as a `# NOT REPLAYABLE` comment; register the handler as a template to make
  it replayable. Messages sent *to* it are still recorded, and are dropped (`no_service`) on replay
  unless you register the same template in the replaying program and deploy it.
- Every recorded message replays as a request (`send`); domain `meta` is not recorded.
- Replay is deterministic for topology, services and external messages, but not for link loss
  (random) or for handler side effects outside the network.
- `--record` overwrites its file, and refuses to run if it is the same as `--scenario`.
- Start the recorder before activity begins. Anything already in the network is captured as the
  initial state, but changes made at the very instant `start()` runs may be recorded twice (a replay
  then logs a harmless "already exists" failure for that step).
- Recording is a Python/CLI feature on purpose: it is **not** exposed through the HTTP API, because
  the server has no authentication and letting a request choose a file path would be arbitrary file
  writing.

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
