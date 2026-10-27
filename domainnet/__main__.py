"""``python -m domainnet`` - run the dashboard with a built-in demo."""
from __future__ import annotations

import argparse
import logging
import time

from .automation import Scenario
from .control import Controller
from .core import Network
from .demo import DEMO_SCENARIO
from .server import DashboardServer


def main() -> None:
    ap = argparse.ArgumentParser(prog="domainnet", description=__doc__)
    ap.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--empty", action="store_true", help="start with an empty network (no demo script)")
    ap.add_argument("--scenario", metavar="FILE", help="run your own timed scenario file instead of the demo")
    ap.add_argument("--traffic", type=float, metavar="RATE", help="start random traffic at RATE msgs/s")
    ap.add_argument("--churn", type=float, metavar="SECONDS", help="start random topology changes every SECONDS")
    ap.add_argument("--speed", type=float, default=1.0, help="initial time scale")
    ap.add_argument("--seed", type=int, help="random seed for repeatable runs")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(name)s: %(message)s")

    net = Network(speed=args.speed, seed=args.seed)
    ctl = Controller(net, seed=args.seed)
    server = DashboardServer(net, ctl, host=args.host, port=args.port)

    if args.scenario:
        Scenario.from_file(args.scenario).start(ctl)
    elif not args.empty:
        Scenario.from_text(DEMO_SCENARIO).start(ctl)
    if args.traffic:
        ctl.traffic.start(args.traffic)
    if args.churn:
        ctl.churn.start(args.churn)

    server.start()
    print(f"domainnet dashboard: {server.url}   (Ctrl+C to stop)")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        ctl.stop_automation()
        server.stop()
        net.close()


if __name__ == "__main__":
    main()
