"""The scenario module, end to end: write -> run -> record -> replay.

    python examples/scenario_replay.py [recording.scn]

A *scenario* is a timed script of console commands. This example

  1. builds a Scenario in Python (a list of (seconds, command) steps),
  2. builds another from text (the same format as a .scn file, comments allowed),
  3. runs both while a Recorder writes everything that happens to a file,
  4. replays that recording into a brand-new network and checks it ends up identical.

No dashboard is needed. To watch it, add ``DashboardServer(net, ctl).start()``, or run the
recording later with ``python -m domainnet --scenario recording.scn``.
"""
import os
import sys
import tempfile

from domainnet import Controller, Network, Recorder, Scenario

# 1) Scenario from Python: (seconds, command) -----------------------------------------
BY_HAND = Scenario([
    (0.0, "add https://shop.example.com bank courier"),     # names can be URLs
    (0.0, 'label https://shop.example.com "Online shop"'),  # display label; the name stays the id
    (0.0, "deploy bank counter as accounts"),               # deploy a template, optionally renamed
    (0.0, "deploy courier upper"),
    (0.3, "link https://shop.example.com bank 40"),
    (0.3, "link https://shop.example.com courier 60 0.0"),  # LATENCY_MS then LOSS (0..1)
])

# 2) Scenario from text (what a .scn file contains) -----------------------------------
FROM_TEXT = Scenario.from_text("""
    # seconds  command
    0.8   send https://shop.example.com bank<accounts> "charge"
    1.0   send https://shop.example.com courier<upper> {"parcel": "books"}
    1.4   add warehouse
    1.4   label warehouse "Warehouse #2"
    1.5   link warehouse courier 30
    1.6   unlink https://shop.example.com courier
""")


def shape(net):
    """What the network looks like: domains (with label and services) and links."""
    snap = net.snapshot()
    return {
        "domains": {d["name"]: (d["label"], sorted(s["name"] for s in d["services"]))
                    for d in snap["domains"]},
        "links": sorted((l["a"], l["b"], l["latency_ms"], l["loss"]) for l in snap["links"]),
    }


def main(out_path):
    # 3) Run the scenarios while recording -------------------------------------------
    net = Network(default_latency_ms=30)
    ctl = Controller(net)
    print(f"{BY_HAND!r} and {FROM_TEXT!r}")

    with Recorder(net, out_path):                       # everything below ends up in the file
        BY_HAND.start(ctl)                              # steps run on the network's clock
        FROM_TEXT.start(ctl)                            # (they can overlap)
        assert BY_HAND.wait(timeout=10) and FROM_TEXT.wait(timeout=10)   # wait for the last step
        for sc in (BY_HAND, FROM_TEXT):
            for t, line, error in sc.errors:            # steps that failed are collected, not fatal
                print(f"  step at {t}s failed: {line!r}: {error}")

        net.set_label("bank", "Bank (EU)")              # things done by hand are recorded too
        net.call("https://shop.example.com", "bank<accounts>", "balance?")

    print(f"\nrecording written to {out_path}:\n")
    print(open(out_path, encoding="utf-8").read())

    # 4) Replay the recording into a fresh network -----------------------------------
    replay_net = Network(default_latency_ms=30)
    replay = Scenario.from_file(out_path).start(Controller(replay_net))
    replay.wait(timeout=replay.duration + 10)
    print(f"replayed {len(replay)} steps in {replay.duration:.1f}s, failed steps: {len(replay.errors)}")
    print("same network after replay:", shape(net) == shape(replay_net))

    net.close()
    replay_net.close()


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else os.path.join(tempfile.mkdtemp(), "recording.scn")
    main(target)
