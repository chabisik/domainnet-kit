import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

from domainnet import (
    Controller, DashboardServer, InvalidName, Network, Recorder, Scenario, UnknownDomain,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
URL = "https://shop.example.com/eu"


def wait_for(pred, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


def shape(net):
    snap = net.snapshot()
    return {
        "domains": {d["name"]: (d["label"], d["label_set"], sorted(s["name"] for s in d["services"]))
                    for d in snap["domains"]},
        "links": sorted((l["a"], l["b"], l["latency_ms"], l["loss"]) for l in snap["links"]),
    }


class TmpMixin:
    def tmp(self, name="rec.scn"):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: [os.remove(os.path.join(d, f)) for f in os.listdir(d)] and os.rmdir(d))
        return os.path.join(d, name)


# --------------------------------------------------------------------------- labels
class LabelTests(unittest.TestCase):
    def setUp(self):
        self.net = Network(default_latency_ms=5)
        self.ctl = Controller(self.net)
        self.events = []
        self.net.bus.subscribe(self.events.append)

    def tearDown(self):
        self.net.close()

    def test_defaults_to_name(self):
        d = self.net.add_domain(URL)
        self.assertEqual(d.label, URL)
        self.assertFalse(d.has_label)
        row = self.net.snapshot()["domains"][0]
        self.assertEqual((row["label"], row["label_set"]), (URL, False))

    def test_set_change_reset(self):
        d = self.net.add_domain("a", label="  Shop EU ")
        self.assertEqual((d.label, d.has_label), ("Shop EU", True))
        d.label = "Other"
        self.assertEqual(self.net.set_label("a", None), "a")        # None resets
        self.assertFalse(d.has_label)
        d.label = "x"
        d.label = ""                                                # so does empty text
        self.assertEqual(d.label, "a")
        d.label = "   "                                             # and whitespace
        self.assertFalse(d.has_label)

    def test_name_is_still_the_id(self):
        self.net.add_domain("a", label="Alpha")
        self.net.add_domain("b", label="Beta")
        self.net.link("a", "b")
        self.net["b"].deploy("echo", lambda m: m.payload)
        self.assertEqual(self.net.call("a", "b<echo>", 1), 1)       # addressing uses names
        with self.assertRaises(UnknownDomain):
            self.net.domain("Alpha")                                # a label is not an id
        self.assertEqual(self.net.domain_names(), ["a", "b"])

    def test_labels_are_free_text_and_may_repeat(self):
        for name, lab in [("a", "Shop EU"), ("b", "Shop EU"), ("c", "https://x.org/p#t"),
                          ("d", "日本語 <b>bold</b> \"q\" 'q'")]:
            self.net.add_domain(name, label=lab)
        self.assertEqual([d.label for d in (self.net[n] for n in "abcd")],
                         ["Shop EU", "Shop EU", "https://x.org/p#t", "日本語 <b>bold</b> \"q\" 'q'"])

    def test_invalid_labels(self):
        self.net.add_domain("a")
        for bad in ["two\nlines", "tab\there", "nul\x00", "ls x", 42, ["x"]]:
            with self.assertRaises(InvalidName, msg=repr(bad)):
                self.net.set_label("a", bad)
        with self.assertRaises(InvalidName):
            self.net.add_domain("b", label="bad\nlabel")
        self.assertNotIn("b", self.net)                             # nothing half-created
        with self.assertRaises(UnknownDomain):
            self.net.set_label("nope", "x")

    def test_events(self):
        self.net.add_domain("a", label="A")
        self.net.set_label("a", "A")                                # unchanged -> no event
        self.net.set_label("a", "B")
        self.net.set_label("a", None)
        added, *labels = [e for e in self.events if e.type.startswith("domain.")]
        self.assertEqual((added.data["label"], added.data["custom"]), ("A", True))
        self.assertEqual([(e.type, e.data["label"], e.data["custom"]) for e in labels],
                         [("domain.label", "B", True), ("domain.label", "a", False)])

    def test_console_commands(self):
        self.ctl.run_line("add a b")
        for line, expected in [
            ("label a Shop EU", "Shop EU"),                          # raw rest of line
            ('label a "Quoted  Name"', "Quoted  Name"),               # JSON string
            ('label a "Hash \\u0023 ok"', "Hash # ok"),               # escape for a bare ' # '
            ("label a Shop # a comment", "Shop"),                    # ' # ' starts a comment
            ("label a", "a"),                                        # no text = reset
        ]:
            res = self.ctl.run_line(line)
            self.assertTrue(res["ok"], (line, res))
            self.assertEqual(self.net["a"].label, expected, line)
        self.assertFalse(self.ctl.run_line("label")["ok"])
        self.assertFalse(self.ctl.run_line("label ghost x")["ok"])

    def test_json_commands(self):
        self.assertEqual(self.ctl.execute({"cmd": "add_domain", "name": "a", "label": "A  # 1"})["ok"], True)
        self.assertEqual(self.net["a"].label, "A  # 1")
        res = self.ctl.execute({"cmd": "label", "name": "a", "label": None})
        self.assertEqual(res["result"], {"label": "a"})
        self.assertFalse(self.ctl.execute({"cmd": "label", "name": "a", "label": "x\ny"})["ok"])

    def test_http_api(self):
        srv = DashboardServer(self.net, self.ctl, port=0).start()
        try:
            def post(body):
                req = urllib.request.Request(srv.url + "api/command", data=json.dumps(body).encode(),
                                             headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=3) as r:
                    return json.load(r)
            self.assertTrue(post({"cmd": "add_domain", "name": URL, "label": "Shop"})["ok"])
            self.assertTrue(post({"cmd": "label", "name": URL, "label": "Shop 2"})["ok"])
            with urllib.request.urlopen(srv.url + "api/state", timeout=3) as r:
                dom = json.load(r)["domains"][0]
            self.assertEqual((dom["name"], dom["label"], dom["label_set"]), (URL, "Shop 2", True))
        finally:
            srv.stop()


# ------------------------------------------------------------------------- scenario
class ScenarioApiTests(unittest.TestCase):
    def setUp(self):
        self.net = Network(default_latency_ms=5)
        self.ctl = Controller(self.net)

    def tearDown(self):
        self.net.close()

    def test_duration_len_wait_and_errors(self):
        sc = Scenario.from_text("0 add a\n0.2 link a nope 10\n0.3 add b")
        self.assertEqual((len(sc), sc.duration), (3, 0.3))
        self.assertFalse(sc.finished.is_set())
        self.assertIs(sc.start(self.ctl), sc)
        self.assertTrue(sc.wait(timeout=3))
        self.assertEqual(sorted(self.net.domain_names()), ["a", "b"])
        self.assertEqual([(t, line) for t, line, _ in sc.errors], [(0.2, "link a nope 10")])

    def test_wait_times_out_and_cancel_releases(self):
        sc = Scenario([(30, "add late")]).start(self.ctl)
        self.assertFalse(sc.wait(timeout=0.1))
        sc.cancel()
        self.assertTrue(sc.wait(timeout=0.1))
        time.sleep(0.1)
        self.assertEqual(self.net.domain_names(), [])

    def test_empty_scenario_finishes_immediately(self):
        self.assertTrue(Scenario([]).start(self.ctl).wait(timeout=0.1))

    def test_text_roundtrip(self):
        text = 'add a b # c\n0.5 label a "x y"\n 1.25   send a b {"k": [1, 2]}\n'
        sc = Scenario.from_text("0 " + text.splitlines()[0] + "\n" + "\n".join(text.splitlines()[1:]))
        again = Scenario.from_text(sc.to_text())
        self.assertEqual(sc.steps, again.steps)
        path = tempfile.mktemp(suffix=".scn")
        try:
            sc.save(path)
            self.assertEqual(Scenario.from_file(path).steps, sc.steps)
        finally:
            os.remove(path)

    def test_shipped_example_keeps_its_hash_escape(self):
        # regression: the # escape in examples/shop.scn must stay an escape, or the ' # '
        # that it decodes to would be cut off as a comment
        line = [s for t, s in Scenario.from_file(os.path.join(ROOT, "examples", "shop.scn")).steps
                if s.startswith("label warehouse")][0]
        net = Network()
        try:
            Controller(net).run_line("add warehouse")
            self.assertTrue(Controller(net).run_line(line)["ok"])
            self.assertEqual(net["warehouse"].label, "Warehouse # 2")
        finally:
            net.close()

    def test_shipped_example_scenario_parses_and_runs(self):
        sc = Scenario.from_file(os.path.join(ROOT, "examples", "shop.scn"))
        self.assertGreater(len(sc), 15)
        for t, line in sc.steps:
            self.assertIsNotNone(self.ctl.parse_line(line), line)   # every line is valid syntax
        # run just the first seconds so the test stays fast
        Scenario([s for s in sc.steps if s[0] <= 0.8]).start(self.ctl).wait(timeout=5)
        self.assertEqual(self.net["https://shop.example.com"].label, "Online shop")
        self.assertEqual(self.net["https://bank.example.org"].label, "Bank (EU)")
        self.assertEqual(self.net.services_of("https://bank.example.org"), ["accounts"])


# ------------------------------------------------------------------------- recorder
class RecorderTests(TmpMixin, unittest.TestCase):
    def setUp(self):
        self.net = Network(default_latency_ms=5)
        self.ctl = Controller(self.net)

    def tearDown(self):
        self.ctl.stop_automation()
        self.net.close()

    def lines(self, path):
        with open(path, encoding="utf-8") as fh:
            return fh.read().splitlines()

    def commands(self, path):
        return [l.split(None, 1)[1] for l in self.lines(path) if l and not l.startswith("#")]

    def replay(self, path):
        net2 = Network(default_latency_ms=5)
        self.addCleanup(net2.close)
        sc = Scenario.from_file(path).start(Controller(net2))
        self.assertTrue(sc.wait(timeout=sc.duration + 5))
        self.assertEqual(sc.errors, [])
        return net2

    def session(self):
        """A varied session driven through every kind of entry point."""
        net, ctl = self.net, self.ctl
        ctl.run_line(f"add {URL} bank courier")
        net.set_label(URL, "Shop # EU é")                          # ' # ' and non-ASCII
        net["courier"].label = 'quote " and \\ backslash'
        ctl.run_line(f"link {URL} bank 40 0.25")
        ctl.run_line(f"link {URL} courier 60")
        ctl.run_line(f"link {URL} courier 75 0.1")                     # update of an existing link
        ctl.run_line("deploy bank counter as accounts")
        ctl.run_line("deploy courier upper")
        ctl.run_line("deploy courier gateway")
        net.deploy_template("bank", "echo", name="echo2")
        ctl.run_line("undeploy bank echo2")
        net.send(URL, "courier<upper>", "line1\nline2 # not a comment   é \"q\"")
        net.send(URL, "bank<accounts>", {"nested": {"n": [1, 2.5, None, True]}, "k": "#"})
        net.send(URL, "bank", None)                                     # no payload
        net.send(URL, "courier<gateway>", {"target": "bank<accounts>", "payload": "via gateway"})
        time.sleep(0.2)                                                 # let the messages above settle
        net.set_speed(2)
        ctl.run_line("add extra")
        ctl.run_line(f"link extra {URL} 10")
        ctl.run_line("rm extra")
        ctl.run_line(f"unlink {URL} bank")
        net.set_label("courier", None)

    def test_roundtrip_reproduces_the_network(self):
        path = self.tmp()
        with Recorder(self.net, path):
            self.session()
            time.sleep(0.3)
        sc = Scenario.from_file(path)                                  # the file is a valid scenario
        self.assertGreater(len(sc), 20)
        net2 = self.replay(path)
        self.assertEqual(shape(self.net), shape(net2))
        self.assertEqual(net2["courier"].label, "courier")
        self.assertEqual(net2[URL].label, "Shop # EU é")
        self.assertEqual(net2.speed, 2.0)

    def test_file_is_pure_ascii_single_line_commands(self):
        path = self.tmp()
        with Recorder(self.net, path):
            self.session()
        for line in self.lines(path):
            if " send " in line or " label " in line:              # payloads/labels are escaped, never raw
                line.encode("ascii")
        raw = open(path, encoding="utf-8").read()
        self.assertNotIn(" ", raw)                              # would split the line in splitlines()
        # every non-comment line is exactly one "<seconds>  <command>" step
        steps = [l for l in raw.splitlines() if l and not l.startswith("#")]
        self.assertEqual(len(steps), len(Scenario.from_file(path)))
        self.assertEqual(len([l for l in raw.splitlines() if l.startswith("#")]), 3)  # 2 header + end

    def test_only_external_messages_are_recorded(self):
        path = self.tmp()
        self.net.add_domain("a")
        self.net.add_domain("b")
        self.net.add_domain("c")
        self.net.link("a", "b")
        self.net.link("b", "c")
        self.net.deploy_template("b", "gateway")
        self.net.deploy_template("c", "upper")
        with Recorder(self.net, path):
            self.assertEqual(self.net.call("a", "b<gateway>", {"target": "c<upper>", "payload": "x"}), "X")
            time.sleep(0.2)
        sends = [c for c in self.commands(path) if c.startswith("send ")]
        self.assertEqual(len(sends), 1, sends)                         # gateway's inner call and replies are not
        net2 = self.replay(path)
        self.assertTrue(wait_for(lambda: net2["c"].services["upper"].calls == 1))   # ...but the replay redoes them

    def test_messages_can_be_excluded(self):
        path = self.tmp()
        self.ctl.run_line("add a b")
        self.ctl.run_line("link a b 5")
        self.ctl.run_line("deploy b echo")
        with Recorder(self.net, path, messages=False):
            self.net.send("a", "b<echo>", "x")
            self.ctl.run_line("add c")
            time.sleep(0.1)
        self.assertFalse([c for c in self.commands(path) if c.startswith("send")])
        self.assertIn("add c", self.commands(path))

    def test_traffic_generator_is_recorded_as_sends(self):
        path = self.tmp()
        self.ctl.run_line("add a b")
        self.ctl.run_line("link a b 5")
        self.ctl.run_line("deploy b echo")
        with Recorder(self.net, path):
            self.ctl.run_line("traffic on 40")
            wait_for(lambda: self.net.snapshot()["stats"]["sent"] >= 8)
            self.ctl.run_line("traffic off")
            time.sleep(0.1)
        cmds = self.commands(path)
        self.assertGreaterEqual(len([c for c in cmds if c.startswith("send ")]), 4)
        self.assertFalse([c for c in cmds if c.startswith("traffic")])  # causes, not the generator

    def test_initial_state_is_written_at_t0(self):
        path = self.tmp()
        self.ctl.run_line("add a b")
        self.net.set_label("a", "Alpha")
        self.ctl.run_line("link a b 33 0.5")
        self.ctl.run_line("deploy a counter as c1")
        self.net.set_speed(0.5)
        with Recorder(self.net, path):
            pass
        rows = [l for l in self.lines(path) if not l.startswith("#")]
        self.assertTrue(all(float(r.split()[0]) == 0.0 for r in rows), rows)
        self.assertEqual(self.commands(path),
                         ["speed 0.5", "add a", 'label a "Alpha"', "add b", "link a b 33.0 0.5", "deploy a counter as c1"])
        self.assertEqual(shape(self.net), shape(self.replay(path)))

    def test_pause_time_is_subtracted(self):
        path = self.tmp()
        self.ctl.run_line("add a")
        with Recorder(self.net, path):
            self.ctl.run_line("add b")
            self.net.pause()
            time.sleep(0.7)
            self.ctl.run_line("add c")                                  # while paused: frozen clock
            self.net.resume()
            self.ctl.run_line("add d")
        times = {c: float(l.split()[0]) for l in self.lines(path) if not l.startswith("#")
                 for c in [l.split(None, 1)[1]]}
        self.assertLess(times["add d"] - times["add b"], 0.4)           # the 0.7 s pause is gone
        self.assertFalse([c for c in self.commands(path) if c in ("pause", "resume")])

    def test_recording_while_already_paused(self):
        path = self.tmp()
        self.net.pause()
        with Recorder(self.net, path):
            time.sleep(0.3)
            self.ctl.run_line("add a")
        self.net.resume()
        self.assertLess(float(self.lines(path)[2].split()[0]), 0.2)

    def test_custom_services_are_noted_not_replayed(self):
        path = self.tmp()
        self.ctl.run_line("add a")
        with Recorder(self.net, path):
            self.net["a"].deploy("mine", lambda m: 1)                   # a bare function, no template
            self.net["a"].undeploy("mine")
        text = "\n".join(self.lines(path))
        self.assertIn("NOT REPLAYABLE: custom service a<mine>", text)
        self.assertFalse([c for c in self.commands(path) if "mine" in c])   # no deploy/undeploy that would fail
        self.assertEqual(self.replay(path).domain_names(), ["a"])

    def test_redeploy_over_existing_service(self):
        path = self.tmp()
        self.ctl.run_line("add a")
        with Recorder(self.net, path):
            self.ctl.run_line("deploy a echo")
            self.net.deploy_template("a", "upper", name="echo", replace=True)
        self.assertEqual([c for c in self.commands(path) if "echo" in c or "upper" in c],
                         ["deploy a echo", "undeploy a echo", "deploy a upper as echo"])
        self.assertEqual(shape(self.net), shape(self.replay(path)))

    def test_domain_removal_drops_its_links_and_services(self):
        path = self.tmp()
        with Recorder(self.net, path):
            self.ctl.run_line("add a b")
            self.ctl.run_line("link a b 5")
            self.ctl.run_line("deploy b echo")
            self.ctl.run_line("rm b")
            self.ctl.run_line("add b")
            self.ctl.run_line("deploy b echo")                           # same name again: must not 'undeploy' first
        self.assertEqual(self.commands(path),
                         ["add a", "add b", "link a b 5.0 0.0", "deploy b echo", "rm b", "add b", "deploy b echo"])
        self.assertEqual(shape(self.net), shape(self.replay(path)))

    def test_unserializable_payload_is_recorded_as_repr(self):
        path = self.tmp()
        self.ctl.run_line("add a b")
        self.ctl.run_line("link a b 5")
        with Recorder(self.net, path):
            self.net.send("a", "b", {1, 2})                              # a set is not JSON
            self.net.send("a", "b", float("nan"))
        text = "\n".join(self.lines(path))
        self.assertIn("not JSON-serializable", text)
        self.replay(path)                                                # still a valid scenario

    def test_lifecycle(self):
        path = self.tmp()
        rec = Recorder(self.net, path)
        self.assertFalse(rec.recording)
        rec.start()
        self.assertIs(rec.start(), rec)                                  # idempotent
        self.assertTrue(rec.recording)
        self.ctl.run_line("add a")
        rec.stop()
        rec.stop()
        self.ctl.run_line("add after")                                   # nothing written after stop
        self.assertEqual(self.commands(path), ["add a"])
        self.assertEqual(rec.lines, 1)
        self.assertTrue(self.lines(path)[-1].startswith("# end of recording"))

    def test_file_is_flushed_while_recording(self):
        path = self.tmp()
        with Recorder(self.net, path):
            self.ctl.run_line("add live")
            self.assertIn("add live", self.commands(path))               # readable before stop (crash-safe)

    def test_bad_path_raises_and_leaves_no_subscription(self):
        with self.assertRaises(OSError):
            Recorder(self.net, os.path.join(self.tmp(), "no", "such", "dir", "x.scn")).start()
        self.assertEqual(len(self.net.bus._subs), 0)

    def test_overwrites_previous_recording(self):
        path = self.tmp()
        for name in ("first", "second"):
            self.net.reset()                                         # each run starts from an empty network
            with Recorder(self.net, path):
                self.ctl.run_line(f"add {name}")
        self.assertEqual(self.commands(path), ["add second"])
        self.assertNotIn("add first", self.commands(path))           # old content is gone


# ------------------------------------------------------------------------------ CLI
class CliTests(TmpMixin, unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.Popen([sys.executable, "-u", "-m", "domainnet", "--port", "0", *args],
                                cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def test_record_equal_to_scenario_is_refused(self):
        path = self.tmp("x.scn")
        with open(path, "w") as fh:
            fh.write("0 add a\n")
        p = self.run_cli("--scenario", path, "--record", path)
        _, err = p.communicate(timeout=20)
        self.assertEqual(p.returncode, 2)
        self.assertIn("overwrite", err)
        with open(path) as fh:
            self.assertEqual(fh.read(), "0 add a\n")                     # untouched

    def test_unwritable_record_path_gives_clean_error(self):
        p = self.run_cli("--empty", "--record", os.path.join(self.tmp(), "no", "dir", "r.scn"))
        _, err = p.communicate(timeout=20)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("cannot write recording file", err)
        self.assertNotIn("Traceback", err)

    def test_record_dashboard_session_then_replay_it(self):
        path = self.tmp("session.scn")
        p = self.run_cli("--empty", "--record", path)
        try:
            url = p.stdout.readline().split()[2]                         # "domainnet dashboard: http://127.0.0.1:PORT/ ..."
            self.assertTrue(url.startswith("http://127.0.0.1:"), url)

            def post(body):
                req = urllib.request.Request(url + "api/command", data=json.dumps(body).encode(),
                                             headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=5) as r:
                    return json.load(r)
            for body in [{"cmd": "add_domain", "name": URL, "label": "Shop"}, {"cmd": "add_domain", "name": "bank"},
                         {"cmd": "link", "a": URL, "b": "bank", "latency_ms": 50},
                         {"cmd": "deploy", "domain": "bank", "template": "echo"},
                         {"cmd": "send", "src": URL, "target": "bank<echo>", "payload": "hi # there"},
                         {"cmd": "label", "name": "bank", "label": "The Bank"}]:
                self.assertTrue(post(body)["ok"], body)
                time.sleep(0.05)
        finally:
            p.send_signal(signal.SIGINT)
            out, err = p.communicate(timeout=20)
        self.assertIn("recorded", out)
        net2 = Network(default_latency_ms=5)
        try:
            sc = Scenario.from_file(path).start(Controller(net2))
            self.assertTrue(sc.wait(timeout=10))
            self.assertEqual(sc.errors, [])
            self.assertEqual(net2[URL].label, "Shop")
            self.assertEqual(net2["bank"].label, "The Bank")
            self.assertEqual(net2.services_of("bank"), ["echo"])
            self.assertEqual(len(net2.links()), 1)
            self.assertTrue(wait_for(lambda: net2["bank"].services["echo"].calls == 1))
        finally:
            net2.close()


if __name__ == "__main__":
    unittest.main()
