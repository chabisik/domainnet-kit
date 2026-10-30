"""``python -m domainnet`` - run the dashboard with a built-in demo."""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time

from .automation import Scenario
from .control import Controller
from .core import Network
from .demo import DEMO_SCENARIO
from .recorder import Recorder
from .server import DashboardServer


def main() -> None:
    ap = argparse.ArgumentParser(prog="domainnet", description=__doc__)
    ap.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--empty", action="store_true", help="start with an empty network (no demo script)")
    ap.add_argument("--scenario", metavar="FILE", help="run your own timed scenario file instead of the demo")
    ap.add_argument("--record", metavar="FILE",
                    help="record everything that happens (dashboard clicks, console, traffic, churn) "
                         "into FILE, in scenario format, so it can be replayed with --scenario")
    ap.add_argument("--traffic", type=float, metavar="RATE", help="start random traffic at RATE msgs/s")
    ap.add_argument("--churn", type=float, metavar="SECONDS", help="start random topology changes every SECONDS")
    ap.add_argument("--speed", type=float, default=1.0, help="initial time scale")
    ap.add_argument("--seed", type=int, help="random seed for repeatable runs")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if args.record and args.scenario and os.path.abspath(args.record) == os.path.abspath(args.scenario):
        ap.error("--record would overwrite the --scenario file being replayed; use a different file")

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(name)s: %(message)s")

    net = Network(speed=args.speed, seed=args.seed)
    ctl = Controller(net, seed=args.seed)
    server = DashboardServer(net, ctl, host=args.host, port=args.port)

    recorder = None
    if args.record:
        try:
            recorder = Recorder(net, args.record).start()   # before anything happens
        except OSError as exc:
            sys.exit(f"domainnet: cannot write recording file {args.record!r}: {exc}")

    if args.scenario:
        try:
            Scenario.from_file(args.scenario).start(ctl)
        except (OSError, ValueError) as exc:
            sys.exit(f"domainnet: cannot load scenario {args.scenario!r}: {exc}")
    elif not args.empty:
        Scenario.from_text(DEMO_SCENARIO).start(ctl)
    if args.traffic:
        ctl.traffic.start(args.traffic)
    if args.churn:
        ctl.churn.start(args.churn)

    server.start()
    print(f"domainnet dashboard: {server.url}   (Ctrl+C to stop)")
    if recorder:
        print(f"recording to {args.record}")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        ctl.stop_automation()
        server.stop()
        if recorder:
            recorder.stop()
            print(f"recorded {recorder.lines} commands to {args.record}")
        net.close()


if __name__ == "__main__":
    main()
