"""Offline protocol tests.  The fake UI never starts a socket or uses a device."""
import importlib.util
from pathlib import Path
import socketserver
import sys
import types
import unittest
from unittest.mock import patch


def load_plugin():
    ui = types.ModuleType("joulescope_ui")
    ui.N_ = lambda text: text
    ui.register = lambda cls: cls
    ui.pubsub_singleton = types.SimpleNamespace(subscribe=lambda *a: None,
                                                query=lambda *a: [])
    styles = types.ModuleType("joulescope_ui.styles")
    styles.styled_widget = lambda name: lambda cls: cls
    qt = types.ModuleType("PySide6")
    qt.QtCore = types.SimpleNamespace()
    qt.QtWidgets = types.SimpleNamespace(QWidget=object)
    spec = importlib.util.spec_from_file_location(
        "bridge_under_test", Path(__file__).resolve().parents[1] / "plugin/__init__.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"joulescope_ui": ui, "joulescope_ui.styles": styles,
                                 "PySide6": qt}), \
         patch.object(socketserver.TCPServer, "__init__", side_effect=OSError("offline")), \
         patch("logging.Logger.exception"):
        spec.loader.exec_module(module)
    return module


class FreshnessTest(unittest.TestCase):
    def setUp(self):
        self.plugin = load_plugin()
        self.bridge = self.plugin._Bridge()
        self.plugin._bridge = self.bridge
        self.now = 100.0
        self.clock = patch.object(self.plugin.time, "monotonic", side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def send(self, v=4.0, charge=2.0, energy=3.0):
        event = {"signals": {name: {"avg": {"value": value}} for name, value in
                             (("v", v), ("i", 0.1), ("p", 0.4))},
                 "time": {"range": {"value": [10, 11]}},
                 "accumulators": {"charge": {"value": charge},
                                  "energy": {"value": energy}}}
        self.bridge._on_stats("fake", event)
        return event

    def request(self, cmd):
        return self.plugin._handle_request({"cmd": cmd})

    def test_fresh_window_and_cutoff(self):
        self.send(v=4.0)
        self.now = 100.5
        self.send(v=6.0)
        self.now = 101.0
        result = self.request("stats_1s")
        self.assertEqual((result["n"], result["voltage"], result["age_s"]), (2, 5.0, 0.5))
        self.now = 101.001
        result = self.request("stats_1s")
        self.assertEqual((result["n"], result["voltage"]), (1, 6.0))
        self.now = 101.5
        self.assertTrue(self.request("voltage")["ok"])
        self.now = 101.501
        self.assertFalse(self.request("voltage")["ok"])
        self.assertEqual(len(self.bridge.samples), 0)

    def test_age_of_raw_and_last_valid_accumulators(self):
        event = self.send()
        self.now = 102.0
        for cmd in ("accumulators", "stats_raw"):
            result = self.request(cmd)
            self.assertTrue(result["ok"])
            self.assertEqual(result["age_s"], 2.0)
        self.assertEqual(self.request("stats_raw")["stats"], event)
        self.assertEqual(self.request("accumulators")["sample_time_s"], 11.0)
        del event["accumulators"]
        self.bridge._on_stats("fake", event)
        self.now = 102.25
        self.assertEqual(self.request("stats_raw")["age_s"], 0.25)
        self.assertEqual(self.request("accumulators")["age_s"], 2.25)
        self.assertEqual(self.request("accumulators")["event_count"], 1)

    def test_no_sample_and_reattach(self):
        for cmd in ("stats_1s", "voltage", "current", "power", "stats_raw", "accumulators"):
            self.assertFalse(self.request(cmd)["ok"])
        self.send()
        self.bridge.attach("JS220-test")
        for cmd in ("stats_1s", "voltage", "current", "power", "stats_raw", "accumulators"):
            self.assertFalse(self.request(cmd)["ok"])

    def test_stall_rejects_and_resume_recovers(self):
        self.send()
        for elapsed in (2, 5, 15):
            self.now = 100.0 + elapsed
            for cmd in ("stats_1s", "voltage", "current", "power"):
                with self.subTest(elapsed=elapsed, cmd=cmd):
                    result = self.request(cmd)
                    self.assertFalse(result["ok"])
                    self.assertEqual(result["error"], "no fresh samples")
                    self.assertEqual(result["age_s"], elapsed)
        self.send(v=5.0)
        for cmd in ("stats_1s", "voltage", "current", "power"):
            result = self.request(cmd)
            self.assertTrue(result["ok"])
            self.assertEqual(result["age_s"], 0)
        self.assertEqual(self.request("stats_1s")["n"], 1)
        self.assertEqual(self.request("voltage")["value"], 5.0)


if __name__ == "__main__":
    unittest.main()
