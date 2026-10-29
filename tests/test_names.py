import json
import time
import unittest
import urllib.request

from domainnet import (
    Controller, DashboardServer, DeliveryError, InvalidName, Network, Scenario,
    format_target, parse_target,
)
from domainnet.automation import strip_comment

URL_A = "https://shop.example.com/eu"
URL_B = "https://shop.example.com"          # a prefix of URL_A on purpose
URL_C = "http://Bank.example.org:8080/v1/?q=1&x=a|b#frag"


def wait_for(pred, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


class AddressSyntax(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_target("a"), ("a", None))
        self.assertEqual(parse_target("a<s>"), ("a", "s"))
        self.assertEqual(parse_target(URL_A), (URL_A, None))
        self.assertEqual(parse_target(URL_A + "<quote>"), (URL_A, "quote"))
        self.assertEqual(parse_target(URL_C + "<svc>"), (URL_C, "svc"))

    def test_parse_rejects_malformed(self):
        for bad in ["", "a<", "a<>", "<s>", "a<s", "a>s<", "a<s>x", "a<<s>>", "a<s><t>"]:
            with self.assertRaises(InvalidName, msg=bad):
                parse_target(bad)

    def test_format_roundtrip(self):
        for dom in ["a", URL_A, URL_C, "日本語", "café"]:
            self.assertEqual(parse_target(format_target(dom, "svc")), (dom, "svc"))
            self.assertEqual(parse_target(format_target(dom)), (dom, None))
        with self.assertRaises(InvalidName):
            format_target("a<b>", "s")

    def test_comment_stripping_keeps_url_fragments(self):
        self.assertEqual(strip_comment("add a  # note"), "add a")
        self.assertEqual(strip_comment("# whole line"), "")
        self.assertEqual(strip_comment("add https://a.com/p#top"), "add https://a.com/p#top")
        self.assertEqual(strip_comment("send a b #1"), "send a b #1")
        self.assertEqual(strip_comment("add a #"), "add a")


class NameRules(unittest.TestCase):
    def setUp(self):
        self.net = Network(default_latency_ms=5)

    def tearDown(self):
        self.net.close()

    def test_any_length_and_unicode_and_urls_accepted(self):
        for name in ["x" * 500, "Société", "东京", URL_A, URL_C, "a/b", "a|b", "a'b\"c", "ünï:cödé"]:
            self.assertEqual(self.net.add_domain(name).name, name)
        self.assertEqual(len(self.net.domain_names()), 9)

    def test_rejected_names(self):
        for bad in ["a b", "a\tb", "a\nb", "a\x00b", "a<b", "a>b", "a b", "a b"]:
            with self.assertRaises(InvalidName, msg=repr(bad)):
                self.net.add_domain(bad)
        with self.assertRaises(InvalidName):
            self.net.add_domain("ok").deploy("bad<svc>", lambda m: 1)
        self.assertEqual(self.net.domain_names(), ["ok"])

    def test_legacy_slash_syntax_is_just_a_name(self):
        self.net.add_domain("c")
        self.net.add_domain("a")
        self.net.link("a", "c")
        self.net["c"].deploy("echo", lambda m: m.payload)
        with self.assertRaises(DeliveryError):          # 'c/echo' is now an (unknown) domain
            self.net.call("a", "c/echo", 1)


class UrlNamedNetwork(unittest.TestCase):
    def setUp(self):
        self.net = Network(default_latency_ms=5)
        self.events = []
        self.net.bus.subscribe(self.events.append)
        for n in (URL_A, URL_B, URL_C):
            self.net.add_domain(n)
        self.net.link(URL_A, URL_B)
        self.net.link(URL_B, URL_C)

    def tearDown(self):
        self.net.close()

    def test_nested_names_are_not_ambiguous(self):
        self.net[URL_A].deploy("who", lambda m: "A")
        self.net[URL_B].deploy("who", lambda m: "B")
        self.net.add_domain("https://shop.example.com/eu/who")    # even a nasty look-alike
        self.assertEqual(self.net.call(URL_C, URL_A + "<who>"), "A")
        self.assertEqual(self.net.call(URL_C, URL_B + "<who>"), "B")

    def test_multi_hop_between_url_domains(self):
        self.net[URL_A].deploy("upper", lambda ctx, m: str(m.payload).upper())
        self.assertEqual(self.net.call(URL_C, format_target(URL_A, "upper"), "hi"), "HI")
        hops = [(e.data["frm"], e.data["to"]) for e in self.events if e.type == "message.hop"]
        self.assertEqual(hops, [(URL_C, URL_B), (URL_B, URL_A), (URL_A, URL_B), (URL_B, URL_C)])

    def test_service_chaining_with_url_targets(self):
        self.net[URL_C].deploy("upper", lambda ctx, m: str(m.payload).upper())
        self.net.deploy_template(URL_B, "gateway")
        out = self.net.call(URL_A, URL_B + "<gateway>", {"target": URL_C + "<upper>", "payload": "x"})
        self.assertEqual(out, "X")

    def test_remove_url_domain_in_flight(self):
        net = Network(default_latency_ms=300)
        try:
            ev = []
            net.bus.subscribe(ev.append)
            net.add_domain(URL_A)
            net.add_domain(URL_B)
            net.link(URL_A, URL_B)
            net.send(URL_A, URL_B, "x")
            net.remove_domain(URL_B)
            self.assertTrue(wait_for(lambda: any(e.type == "message.dropped" for e in ev)))
        finally:
            net.close()


class ControllerWithUrls(unittest.TestCase):
    def setUp(self):
        self.net = Network(default_latency_ms=5)
        self.ctl = Controller(self.net)

    def tearDown(self):
        self.ctl.stop_automation()
        self.net.close()

    def test_console_commands_with_urls_and_fragments(self):
        frag = "https://a.example.com/page#top"
        for line in [f"add {frag} {URL_B}", f"link {frag} {URL_B} 10", f"deploy {URL_B} echo",
                     f'send {frag} {URL_B}<echo> {{"k": 1}}  # trailing comment']:
            res = self.ctl.run_line(line)
            self.assertTrue(res["ok"], (line, res))
        self.assertEqual(sorted(self.net.domain_names()), sorted([frag, URL_B]))
        self.assertEqual(self.net.call(frag, URL_B + "<echo>", "yo"), "yo")

    def test_structured_send(self):
        self.ctl.run_line(f"add {URL_A} {URL_B}")
        self.ctl.run_line(f"link {URL_A} {URL_B} 5")
        self.ctl.run_line(f"deploy {URL_B} counter")
        res = self.ctl.execute({"cmd": "send", "src": URL_A, "dst": URL_B, "service": "counter"})
        self.assertTrue(res["ok"], res)
        self.assertTrue(wait_for(lambda: self.net[URL_B].services["counter"].calls == 1))
        bad = self.ctl.execute({"cmd": "send", "src": URL_A, "dst": "x<y>", "service": "s"})
        self.assertFalse(bad["ok"])

    def test_scenario_with_urls(self):
        Scenario.from_text(f"""
            # header comment
            0    add {URL_A} {URL_B}   # two url domains
            0.05 link {URL_A} {URL_B} 5
            0.1  deploy {URL_B} echo
        """).start(self.ctl)
        self.assertTrue(wait_for(lambda: URL_B in self.net and self.net.services_of(URL_B) == ["echo"]))

    def test_traffic_and_churn_with_url_names(self):
        self.ctl.run_line(f"add {URL_A} {URL_B} {URL_C}")
        self.ctl.run_line(f"link {URL_A} {URL_B} 5")
        self.ctl.run_line(f"link {URL_B} {URL_C} 5")
        for d in (URL_A, URL_B, URL_C):
            self.ctl.run_line(f"deploy {d} echo")
        self.ctl.run_line("traffic on 50")
        self.assertTrue(wait_for(lambda: self.net.snapshot()["stats"]["delivered"] >= 5))
        self.ctl.run_line("traffic off")


class ServerWithUrls(unittest.TestCase):
    def test_http_api_with_url_names(self):
        net = Network(default_latency_ms=5)
        srv = DashboardServer(net, Controller(net), port=0).start()
        try:
            def post(body):
                req = urllib.request.Request(srv.url + "api/command", data=json.dumps(body).encode(),
                                             headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=3) as r:
                    return json.load(r)
            self.assertEqual(post({"cmd": "add_domain", "name": URL_C})["result"]["created"], [URL_C])
            self.assertTrue(post({"cmd": "add_domain", "name": URL_A})["ok"])
            self.assertTrue(post({"cmd": "deploy", "domain": URL_A, "template": "echo"})["ok"])
            with urllib.request.urlopen(srv.url + "api/state", timeout=3) as r:
                st = json.load(r)
            self.assertEqual(sorted(d["name"] for d in st["domains"]), sorted([URL_A, URL_C]))
        finally:
            srv.stop()
            net.close()


if __name__ == "__main__":
    unittest.main()
