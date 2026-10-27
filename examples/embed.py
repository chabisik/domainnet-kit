"""How to embed domainnet in an existing project.

Run:  python examples/embed.py   then open the printed URL.

Shows the four integration points:
  1. build the network from your own code (domains, links, real services)
  2. register your own service *templates* so the dashboard can deploy them
  3. observe everything through the event bus (forward to logs/metrics/WebSocket)
  4. serve the dashboard next to your app
"""
import logging
import time

from domainnet import Controller, DashboardServer, Network

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

net = Network(default_latency_ms=120)
ctl = Controller(net)

# 1) Your domains and *your* business logic as services -----------------------
shop = net.add_domain("shop", owner="team-a")
bank = net.add_domain("bank", owner="team-b")
courier = net.add_domain("courier", owner="team-c")
net.link("shop", "bank", latency_ms=100)
net.link("shop", "courier", latency_ms=180)

PRICES = {"book": 12, "pen": 2}


@shop.service("quote", description="price of an item")
def quote(ctx, msg):
    return {"item": msg.payload, "price": PRICES.get(msg.payload)}


@bank.service("charge", description="charge a card")
def charge(ctx, msg):
    return {"ok": msg.payload["amount"] <= 100}


@shop.service("checkout", description="quote -> charge -> ship (chains calls across domains)")
def checkout(ctx, msg):
    item = msg.payload
    price = ctx.call("shop/quote", item)["price"]          # local call
    paid = ctx.call("bank/charge", {"amount": price})["ok"]   # crosses a link
    if paid:
        ctx.send("courier", {"ship": item}, kind="event")     # fire and forget
    return {"item": item, "paid": paid}


# 2) Custom templates: now deployable from the dashboard / `deploy` command ----
net.register_template(
    "audit",
    lambda domain: (lambda ctx, msg: f"{domain.name} audited {msg.payload!r}"),
    "Replies with an audit line",
)

# 3) Observe everything -------------------------------------------------------
net.bus.subscribe(
    lambda ev: logging.info("%s %s", ev.type, ev.data),
    types=["message.delivered", "message.dropped", "domain.", "service."],  # prefixes
)
courier.on_message = lambda msg: logging.info("courier got %r", msg.payload)

# 4) Dashboard + a bit of live traffic ---------------------------------------
server = DashboardServer(net, ctl, port=8765).start()
print("dashboard:", server.url)

print("checkout ->", net.call("shop", "shop/checkout", "book"))

ctl.run_line("traffic on 1.5")          # same as the toggle in the UI
try:
    while True:
        time.sleep(3600)
except KeyboardInterrupt:
    server.stop()
    net.close()
