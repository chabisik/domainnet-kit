import json
import threading
import time
import unittest
import urllib.error
import urllib.request

from domainnet import (
    CallTimeout, Controller, DashboardServer, DeliveryError, DomainExists, LinkError,
    Network, RemoteError, Scenario, UnknownDomain,
)


def wait_for(pred, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


class Base(unittest.TestCase):
    def setUp(self):
        self.net = Network(default_latency_ms=10)
        self.events = []
        self.net.bus.subscribe(self.events.append)

    def tearDown(self):
        self.net.close()

    def types(self):
        return [e.type for e in self.events]


class TopologyTests(Base):
    def test_add_remove_domains_and_links(self):
        self.net.add_domain("a")
        self.net.add_domain("b")
        self.net.link("a", "b", latency_ms=5)
        self.assertEqual(self.net.neighbors("a"), ["b"])
        self.net.remove_domain("b")
        self.assertEqual(self.net.links(), [])
        self.assertNotIn("b", self.net)
        self.assertEqual(self.types(), ["domain.added", "domain.added", "link.added",
                                        "link.removed", "domain.removed"])

    def test_errors(self):
        self.net.add_domain("a")
        with self.assertRaises(DomainExists):
            self.net.add_domain("a")
        with self.assertRaises(UnknownDomain):
            self.net.link("a", "zzz")
        with self.assertRaises(LinkError):
            self.net.link("a", "a")

    def test_auto_names_unique(self):
        names = {self.net.add_domain().name for _ in range(30)}
        self.assertEqual(len(names), 30)

    def test_route_prefers_low_latency(self):
        for n in "abcd":
            self.net.add_domain(n)
        self.net.link("a", "d", latency_ms=500)
        self.net.link("a", "b", latency_ms=10)
        self.net.link("b", "c", latency_ms=10)
        self.net.link("c", "d", latency_ms=10)
        self.assertEqual(self.net.route("a", "d"), ["a", "b", "c", "d"])
        self.net.unlink("b", "c")
        self.assertEqual(self.net.route("a", "d"), ["a", "d"])


class MessagingTests(Base):
    def chain(self):
        for n in "abc":
            self.net.add_domain(n)
        self.net.link("a", "b")
        self.net.link("b", "c")

    def test_multi_hop_call_and_reply(self):
        self.chain()
        self.net["c"].deploy("upper", lambda ctx, msg: str(msg.payload).upper())
        self.assertEqual(self.net.call("a", "c<upper>", "hi"), "HI")
        hops = [e for e in self.events if e.type == "message.hop"]
        self.assertEqual([(h.data["frm"], h.data["to"]) for h in hops],
                         [("a", "b"), ("b", "c"), ("c", "b"), ("b", "a")])

    def test_single_arg_handler_and_decorator(self):
        self.chain()

        @self.net["c"].service("twice")
        def twice(msg):
            """Doubles a number"""
            return msg.payload * 2

        self.assertEqual(self.net.call("a", "c<twice>", 21), 42)
        self.assertEqual(self.net["c"].services["twice"].description, "Doubles a number")

    def test_service_chaining(self):
        self.chain()
        self.net["c"].deploy("upper", lambda ctx, msg: str(msg.payload).upper())
        self.net.deploy_template("b", "gateway")
        out = self.net.call("a", "b<gateway>", {"target": "c<upper>", "payload": "x"})
        self.assertEqual(out, "X")

    def test_no_route_drops_and_fails_call_fast(self):
        self.net.add_domain("a")
        self.net.add_domain("b")
        t0 = time.time()
        with self.assertRaises(DeliveryError):
            self.net.call("a", "b<x>", 1, timeout=5)
        self.assertLess(time.time() - t0, 1.0)
        self.assertIn("message.dropped", self.types())
        self.assertEqual(self.net.snapshot()["stats"]["in_flight"], 0)

    def test_missing_service_dropped(self):
        self.chain()
        with self.assertRaises(DeliveryError):
            self.net.call("a", "c<nope>", 1)

    def test_remote_exception(self):
        self.chain()

        def boom(ctx, msg):
            raise ValueError("kaboom")
        self.net["c"].deploy("boom", boom)
        with self.assertRaises(RemoteError):
            self.net.call("a", "c<boom>")
        self.assertIn("service.error", self.types())

    def test_timeout_when_service_returns_none(self):
        self.chain()
        self.net["c"].deploy("silent", lambda ctx, msg: None)
        with self.assertRaises(CallTimeout):
            self.net.call("a", "c<silent>", timeout=0.2)

    def test_event_to_domain_goes_to_inbox_and_hook(self):
        self.chain()
        got = []
        self.net["c"].on_message = got.append
        self.net.send("a", "c", "ping")
        self.assertTrue(wait_for(lambda: got))
        self.assertEqual(got[0].payload, "ping")
        self.assertEqual(len(self.net["c"].inbox), 1)

    def test_link_removed_in_flight_drops(self):
        net = Network(default_latency_ms=300)
        try:
            ev = []
            net.bus.subscribe(ev.append)
            for n in "ab":
                net.add_domain(n)
            net.link("a", "b")
            net.send("a", "b", "x")
            net.unlink("a", "b")
            self.assertTrue(wait_for(lambda: any(e.type == "message.dropped" for e in ev)))
            self.assertEqual([e.data["reason"] for e in ev if e.type == "message.dropped"], ["link_removed"])
        finally:
            net.close()

    def test_domain_removed_in_flight_drops(self):
        net = Network(default_latency_ms=300)
        try:
            ev = []
            net.bus.subscribe(ev.append)
            for n in "ab":
                net.add_domain(n)
            net.link("a", "b")
            net.send("a", "b", "x")
            net.remove_domain("b")
            self.assertTrue(wait_for(lambda: any(e.type == "message.dropped" for e in ev)))
        finally:
            net.close()

    def test_total_loss(self):
        for n in "ab":
            self.net.add_domain(n)
        self.net.link("a", "b", loss=1.0)
        self.net["b"].deploy("echo", lambda m: m.payload)
        with self.assertRaises(DeliveryError):
            self.net.call("a", "b<echo>", 1)

    def test_close_fails_pending_calls_immediately(self):
        net = Network(default_latency_ms=2000)
        for n in "ab":
            net.add_domain(n)
        net.link("a", "b")
        net["b"].deploy("echo", lambda m: m.payload)
        threading.Timer(0.2, net.close).start()
        t0 = time.time()
        with self.assertRaises(DeliveryError):
            net.call("a", "b<echo>", 1, timeout=10)
        self.assertLess(time.time() - t0, 2.0)

    def test_local_delivery_has_no_hops(self):
        self.net.add_domain("a")
        self.net["a"].deploy("echo", lambda m: m.payload)
        self.assertEqual(self.net.call("a", "a<echo>", 7), 7)
        self.assertNotIn("message.hop", self.types())

    def test_pause_freezes_inflight_and_resume_delivers(self):
        net = Network(default_latency_ms=150)
        try:
            for n in "ab":
                net.add_domain(n)
            net.link("a", "b")
            net.send("a", "b", "x")
            net.pause()
            time.sleep(0.4)
            self.assertEqual(net.snapshot()["stats"]["delivered"], 0)
            net.resume()
            self.assertTrue(wait_for(lambda: net.snapshot()["stats"]["delivered"] == 1))
        finally:
            net.close()

    def test_speed_scales_hop_duration(self):
        for n in "ab":
            self.net.add_domain(n)
        self.net.link("a", "b", latency_ms=100)
        self.net.set_speed(4)
        self.net.send("a", "b", 1)
        hop = next(e for e in self.events if e.type == "message.hop")
        self.assertAlmostEqual(hop.data["duration_ms"], 25.0)


class ControllerTests(Base):
    def setUp(self):
        super().setUp()
        self.ctl = Controller(self.net)

    def tearDown(self):
        self.ctl.stop_automation()
        super().tearDown()

    def test_text_commands(self):
        for line in ["add a b c", "link a b 20", "link b c 20", "deploy c echo", "deploy c upper as shout"]:
            self.assertTrue(self.ctl.run_line(line)["ok"], line)
        self.assertEqual(sorted(self.net.services_of("c")), ["echo", "shout"])
        self.assertEqual(self.net.call("a", "c<shout>", "yo"), "YO")
        res = self.ctl.run_line('send a c<echo> {"k": 1}')
        self.assertTrue(res["ok"])
        self.assertTrue(self.ctl.run_line("rm b")["ok"])
        self.assertEqual(self.net.links(), [])

    def test_errors_are_returned_not_raised(self):
        for line in ["link x y", "bogus", "deploy a nothing", "send", "speed fast", "add a", "add a"]:
            res = self.ctl.run_line(line)
            if line == "add a":
                continue
            self.assertFalse(res["ok"], line)
            self.assertTrue(res["error"])
        self.assertFalse(self.ctl.execute({"cmd": "link"})["ok"])
        self.assertFalse(self.ctl.execute({"nope": 1})["ok"])

    def test_json_commands_and_state(self):
        self.assertTrue(self.ctl.execute({"cmd": "add_domain", "name": "z"})["ok"])
        self.assertEqual(self.ctl.execute({"cmd": "add_domain"})["result"]["created"], ["alpha"])
        st = self.ctl.execute({"cmd": "state"})["result"]
        self.assertEqual([d["name"] for d in st["domains"]], ["z", "alpha"])
        self.assertIn("traffic", st)

    def test_traffic_generates_messages_and_stops(self):
        self.ctl.run_line("add a b")
        self.ctl.run_line("link a b 5")
        self.ctl.run_line("deploy b echo")
        self.ctl.run_line("traffic on 50")
        self.assertTrue(wait_for(lambda: self.net.snapshot()["stats"]["delivered"] >= 3))
        self.ctl.run_line("traffic off")
        time.sleep(0.15)
        n = self.net.snapshot()["stats"]["sent"]
        time.sleep(0.3)
        self.assertEqual(self.net.snapshot()["stats"]["sent"], n)

    def test_churn_keeps_bounds(self):
        self.ctl.churn.min_domains, self.ctl.churn.max_domains = 2, 5
        self.ctl.run_line("add a b")
        self.ctl.run_line("churn on 0.2")
        time.sleep(2.5)
        self.ctl.run_line("churn off")
        self.assertLessEqual(len(self.net.domain_names()), 5)
        self.assertGreaterEqual(len(self.net.domain_names()), 2)
        self.assertTrue({"domain.added", "link.added"} <= set(self.types()))

    def test_scenario(self):
        sc = Scenario.from_text("""
            # comment
            0    add a b
            0.1  link a b 5
            0.2  deploy b echo
        """)
        sc.start(self.ctl)
        self.assertTrue(wait_for(lambda: "b" in self.net and self.net.links() and self.net.services_of("b") == ["echo"]))
        with self.assertRaises(ValueError):
            Scenario.from_text("oops add a")


class ServerTests(Base):
    def setUp(self):
        super().setUp()
        self.ctl = Controller(self.net)
        self.srv = DashboardServer(self.net, self.ctl, port=0).start()

    def tearDown(self):
        self.srv.stop()
        super().tearDown()

    def post(self, body, ctype="application/json"):
        req = urllib.request.Request(self.srv.url + "api/command", data=json.dumps(body).encode(),
                                     headers={"Content-Type": ctype}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=3) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def test_index_state_and_commands(self):
        with urllib.request.urlopen(self.srv.url, timeout=3) as r:
            self.assertIn(b"domainnet", r.read())
        self.assertEqual(self.post({"line": "add a b"})[0], 200)
        self.assertEqual(self.post({"cmd": "link", "a": "a", "b": "b"})[0], 200)
        status, body = self.post({"line": "link a nope"})
        self.assertEqual(status, 400)
        self.assertFalse(body["ok"])
        with urllib.request.urlopen(self.srv.url + "api/state", timeout=3) as r:
            st = json.load(r)
        self.assertEqual(len(st["domains"]), 2)
        self.assertEqual(len(st["links"]), 1)
        with urllib.request.urlopen(self.srv.url + "api/events?since=0", timeout=3) as r:
            self.assertIn("domain.added", [e["type"] for e in json.load(r)])

    def test_rejects_non_json_content_type(self):
        status, _ = self.post({"line": "add a"}, ctype="text/plain")
        self.assertEqual(status, 415)
        self.assertEqual(self.net.domain_names(), [])

    def test_sse_stream_snapshot_then_live_events(self):
        resp = urllib.request.urlopen(self.srv.url + "api/stream", timeout=5)
        try:
            first = json.loads(resp.readline().decode().split("data: ", 1)[1])
            self.assertEqual(first["type"], "snapshot")
            resp.readline()
            threading.Timer(0.1, lambda: self.ctl.run_line("add live")).start()
            ev = json.loads(resp.readline().decode().split("data: ", 1)[1])
            self.assertEqual((ev["type"], ev["data"]["name"]), ("domain.added", "live"))
        finally:
            resp.close()


if __name__ == "__main__":
    unittest.main()
