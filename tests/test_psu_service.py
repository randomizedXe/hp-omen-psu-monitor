"""Sensor and supervisor checks. These tests never initialize native HID."""

import io
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import psu_service as service


def snapshot():
    return {"event": "snapshot", "capabilities": [4, 1, 2, 3], "measurements": {
        "power": {"overall": 250, "unit": "W"},
        "temperature": {"overall": 38.5, "unit": "C"},
        "fan": {"overall": 700, "unit": "RPM"},
        "efficiency": {"overall": 92.5, "unit": "%"},
        "voltage": {"values": [12, 3.3, 5, 5]},
        "current": {"values": [10, 2, 1, 0.2]},
    }}


class SensorModelTests(unittest.TestCase):
    def test_capability_order_produces_stable_rail_ids_and_registry_names(self):
        readings = {reading.key: reading for reading in service.flatten_snapshot(snapshot())}
        self.assertEqual(len(readings), 12)
        self.assertEqual(readings["voltage.rail_4"].value, 12)
        self.assertEqual(readings["voltage.rail_4"].hwinfo_key, "Volt3")
        self.assertEqual(readings["current.rail_1"].value, 2)
        self.assertEqual(readings["current.rail_1"].hwinfo_key, "Current0")
        self.assertEqual(readings["temperature"].unit, "°C")
        self.assertEqual(readings["power"].hwinfo_key, "Power0")
        self.assertEqual(readings["fan"].hwinfo_key, "Fan0")
        self.assertEqual(readings["efficiency"].hwinfo_key, "Usage0")

    def test_unknown_rail_remains_visible_without_registry_key(self):
        event = {"capabilities": [42], "measurements": {"voltage": {"values": [1.25]}}}
        reading, = service.flatten_snapshot(event)
        self.assertEqual(reading.key, "voltage.rail_42")
        self.assertEqual(reading.hwinfo_key, "")

    def test_by_rail_fallback_and_mismatched_arrays_do_not_invent_rail_order(self):
        event = {"capabilities": [1, 2], "measurements": {"voltage": {
            "values": [12], "by_rail": {"12V total": 12}}}}
        reading, = service.flatten_snapshot(event)
        self.assertEqual(reading.key, "voltage.rail_4")
        del event["measurements"]["voltage"]["by_rail"]
        self.assertEqual(service.flatten_snapshot(event), [])

    def test_invalid_sensor_numbers_are_rejected(self):
        for value in (math.nan, math.inf, True, "12"):
            event = {"measurements": {"power": {"overall": value}}}
            with self.subTest(value=value), self.assertRaises(ValueError):
                service.flatten_snapshot(event)

    def test_stats_history_is_bounded_and_offline_has_no_current_value(self):
        stats = service.SensorStats()
        for index in range(650):
            stats.update(index, index)
        self.assertEqual(len(stats.history), 600)
        self.assertEqual(stats.history[0], (50, 50))
        self.assertEqual(stats.count, 650)
        self.assertEqual(stats.min, 0)
        self.assertEqual(stats.max, 649)
        self.assertAlmostEqual(stats.average, 324.5)
        stats.mark_offline()
        self.assertFalse(stats.online)
        self.assertIsNone(stats.current)
        self.assertEqual(stats.count, 650)
        stats.reset()
        self.assertEqual(stats.count, 0)
        self.assertFalse(stats.history)

    def test_reset_begins_new_aggregate_at_current_reading(self):
        stats = service.SensorStats()
        stats.update(10, 1)
        stats.update(20, 2)
        stats.reset()
        self.assertEqual((stats.current, stats.min, stats.max, stats.average, stats.count),
                         (20, 20, 20, 20, 1))
        self.assertEqual(list(stats.history), [(2, 20)])


class CollectorTests(unittest.TestCase):
    def wait_for(self, collector, predicate, timeout=4):
        deadline = time.monotonic() + timeout
        events = []
        while time.monotonic() < deadline:
            events.extend(collector.get_events())
            if predicate(events):
                return events
            time.sleep(0.02)
        self.fail("Timed out waiting for collector event: " + repr(events))

    def child(self, code, **options):
        return service.TelemetryCollector(
            worker_command=[sys.executable, "-u", "-c", code], **options)

    def test_snapshot_stream_and_stop_reap_child_and_clear_online(self):
        code = "import json,time; print(json.dumps(" + repr(snapshot()) + "),flush=True); time.sleep(10)"
        collector = self.child(code)
        try:
            collector.start()
            events = self.wait_for(collector, lambda es: any(e["event"] == "snapshot" for e in es))
            self.assertTrue(collector.online)
            received = next(event for event in events if event["event"] == "snapshot")
            self.assertTrue(received["online"])
            self.assertIn("received_monotonic", received)
            process = collector._process
        finally:
            collector.stop()
        self.assertFalse(collector.online)
        self.assertFalse(collector.running)
        self.assertIsNotNone(process.poll())
        self.assertEqual(collector.get_events()[-1]["state"], "stopped")

    def test_watchdog_clears_online_and_restarts_unresponsive_child(self):
        code = "import json,time; print(json.dumps(" + repr(snapshot()) + "),flush=True); time.sleep(10)"
        collector = self.child(code, watchdog_timeout=0.25, restart_delay=0.5)
        try:
            collector.start()
            events = self.wait_for(collector, lambda es: any(
                e.get("state") == "restarting" for e in es))
            self.assertFalse(collector.online)
            self.assertTrue(any("无响应" in e.get("message", "") for e in events))
        finally:
            collector.stop()

    def test_frozen_worker_routing_uses_same_executable(self):
        collector = service.TelemetryCollector(interval=3)
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", "Sensor.exe"):
            self.assertEqual(collector._command(),
                             ["Sensor.exe", "--collector-worker", "--interval", "3.0",
                              "--parent-pid", str(os.getpid())])

    def test_script_worker_command_carries_parent_pid(self):
        collector = service.TelemetryCollector()
        with patch.object(sys, "frozen", False, create=True):
            command = collector._command()
        self.assertIn("--worker", command)
        self.assertEqual(command[-2:], ["--parent-pid", str(os.getpid())])


class ParentDeathGuardTests(unittest.TestCase):
    class Link:
        def __init__(self, results):
            self.results = iter(results)
            self.closed = False

        def wait(self, timeout_ms):
            result = next(self.results)
            if isinstance(result, Exception):
                raise result
            return result

        def close(self):
            self.closed = True

    def test_parent_exit_triggers_exact_parent_cleanup_and_process_exit(self):
        link = self.Link([False, True])
        exited = threading.Event()
        exit_codes, cleaned = [], []
        guard = service._ParentDeathGuard(123, link_factory=lambda _: link,
            exit_process=lambda code: (exit_codes.append(code), exited.set()),
            cleanup=cleaned.append)
        try:
            self.assertTrue(guard.start())
            self.assertTrue(exited.wait(1))
        finally:
            guard.close()
        self.assertEqual(exit_codes, [0])
        self.assertEqual(cleaned, [123])
        self.assertTrue(link.closed)

    def test_already_dead_parent_exits_before_native_collector_runs(self):
        link = self.Link([True])
        exits, cleaned = [], []
        guard = service._ParentDeathGuard(123, link_factory=lambda _: link,
            exit_process=exits.append, cleanup=cleaned.append)
        with patch("psu_service._ParentDeathGuard", return_value=guard), \
                patch("psu_service._worker_run") as worker, \
                patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(service.worker_main(["--worker", "--parent-pid", "123"]), 0)
        worker.assert_not_called()
        self.assertEqual(exits, [0])
        self.assertEqual(cleaned, [123])

    def test_cleanup_failure_does_not_leave_an_orphan(self):
        link = self.Link([True])
        exits = []
        def failing_cleanup(pid):
            raise RuntimeError("registry unavailable")
        guard = service._ParentDeathGuard(123, link_factory=lambda _: link,
            exit_process=exits.append, cleanup=failing_cleanup)
        self.assertFalse(guard.start())
        self.assertEqual(exits, [0])
        self.assertTrue(link.closed)

    def test_blocked_cleanup_cannot_block_worker_termination(self):
        release = threading.Event()
        exits = []
        link = self.Link([True])
        guard = service._ParentDeathGuard(123, link_factory=lambda _: link,
            exit_process=exits.append, cleanup=lambda _: release.wait(2), cleanup_timeout=0.03)
        started = time.monotonic()
        try:
            self.assertFalse(guard.start())
            self.assertLess(time.monotonic() - started, 1)
            self.assertEqual(exits, [0])
        finally:
            release.set()
            guard.close()

    def test_failed_parent_wait_exits_without_cleaning_unconfirmed_owner(self):
        link = self.Link([False, OSError("wait failed")])
        exited = threading.Event()
        exits, cleaned = [], []
        guard = service._ParentDeathGuard(123, link_factory=lambda _: link,
            exit_process=lambda code: (exits.append(code), exited.set()), cleanup=cleaned.append)
        try:
            self.assertTrue(guard.start())
            self.assertTrue(exited.wait(1))
        finally:
            guard.close()
        self.assertEqual(exits, [2])
        self.assertEqual(cleaned, [])

    def test_real_process_link_signals_after_process_exit_without_device_access(self):
        options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                   "stderr": subprocess.DEVNULL}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.2)"], **options)
        link = None
        try:
            link = service._ParentProcessLink(process.pid)
            self.assertFalse(link.wait(0))
            process.wait(timeout=3)
            self.assertTrue(link.wait(1000))
        finally:
            if link is not None:
                link.close()
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)


class WorkerTests(unittest.TestCase):
    def fake_native(self, revision="0200"):
        from hp_omen_psu_monitor import QUERIES

        class Native:
            def __init__(self):
                self.calls = []
                self.closed = []
                self.kernel = type("Kernel", (), {"CloseHandle": lambda _, h: self.closed.append(h)})()

            def paths(self):
                return ["test_vid_03f0&pid_04b0"]

            def open_telemetry(self, path):
                return 17

            def metadata(self, handle):
                return {"revision": revision, "usage_page": "0xFF00", "usage": "0x0001",
                        "input_report_bytes": 64, "output_report_bytes": 64,
                        "feature_report_bytes": 0}

            def query(self, handle, name, timeout):
                self.calls.append(name)
                opcode, selector, _, _ = QUERIES[name]
                payload = bytes([1, 2, 3, 4]) if name == "capability" else (
                    (b"\x01" if selector else b"") + (100).to_bytes(2, "little") *
                    (4 if name in ("voltage", "current") else 1))
                response = bytearray(64)
                response[3] = opcode
                response[5] = len(payload)
                response[6:6 + len(payload)] = payload
                return {"name": name, "request_hex": "", "response_hex": response.hex(),
                        "unrelated_responses_hex": []}
        return Native()

    def run_worker(self, native):
        with patch("hp_omen_psu_monitor.TelemetryHid", return_value=native), \
                patch("psu_service.time.sleep", side_effect=KeyboardInterrupt), \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(service.worker_main(["--worker", "--interval", "2"]), 0)
            return [json.loads(line) for line in output.getvalue().splitlines()]

    def test_worker_only_uses_capability_then_six_whitelisted_read_queries(self):
        native = self.fake_native()
        events = self.run_worker(native)
        self.assertEqual(native.calls, ["capability", "power", "efficiency", "temperature",
                                        "fan", "voltage", "current"])
        self.assertEqual(native.closed, [17])
        self.assertEqual(len([event for event in events if event["event"] == "snapshot"]), 1)

    def test_protocol_guard_prevents_queries_to_unverified_revision(self):
        native = self.fake_native(revision="0300")
        events = self.run_worker(native)
        self.assertEqual(native.calls, [])
        self.assertEqual(native.closed, [17])
        self.assertTrue(any(event.get("state") == "offline" for event in events))


if __name__ == "__main__":
    unittest.main()
