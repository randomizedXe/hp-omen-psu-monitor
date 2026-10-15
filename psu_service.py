"""Sensor models and a supervised HP OMEN PSU telemetry collector.

Importing this module, models, and the GUI-side collector never opens a device.
Only the child worker imports the verified, fixed-query HID implementation.
"""

from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass
import io
import json
import math
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from typing import Callable, Sequence


@dataclass(frozen=True)
class SensorReading:
    key: str
    name: str
    group: str
    unit: str
    value: float
    hwinfo_key: str


_OVERALL = (
    ("power", "功率", "W", "Power0"),
    ("temperature", "温度", "°C", "Temp0"),
    ("fan", "风扇转速", "RPM", "Fan0"),
    ("efficiency", "效率", "%", "Usage0"),
)
_RAIL_NAMES = {
    1: "3.3V", 2: "5V", 3: "5V待机", 4: "12V总路",
    5: "12V主板", 6: "12V CPU", 7: "12V显卡",
}
_VENDOR_RAIL_NAMES = {
    "3.3V": 1, "5V": 2, "5V standby": 3, "12V total": 4,
    "12V motherboard": 5, "12V CPU": 6, "12V graphics": 7,
}


def _number(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Sensor value must be numeric.")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("Sensor value must be finite.")
    return value


def flatten_snapshot(snapshot_event: dict) -> list[SensorReading]:
    """Convert a decoded snapshot into sensors with stable capability-based IDs.

    Rail order is taken from capability, never assumed from array positions.
    Unknown rail IDs remain visible but receive no registry publication key.
    """
    measurements = snapshot_event.get("measurements", {})
    readings = []
    for key, name, unit, hwinfo_key in _OVERALL:
        measurement = measurements.get(key)
        if measurement is not None and measurement.get("overall") is not None:
            readings.append(SensorReading(key, name, "电源", unit,
                                          _number(measurement["overall"]), hwinfo_key))
    capabilities = snapshot_event.get("capabilities", ())
    for key, name, unit, prefix in (
            ("voltage", "电压", "V", "Volt"),
            ("current", "电流", "A", "Current")):
        measurement = measurements.get(key)
        if measurement is None:
            continue
        values = measurement.get("values", ())
        if capabilities and len(capabilities) == len(values):
            rail_values = zip(capabilities, values)
        else:
            rail_values = ((_VENDOR_RAIL_NAMES[rail], value)
                           for rail, value in measurement.get("by_rail", {}).items()
                           if rail in _VENDOR_RAIL_NAMES)
        seen = set()
        for rail, value in rail_values:
            if isinstance(rail, bool) or not isinstance(rail, int) or not 1 <= rail <= 255:
                raise ValueError("Invalid capability rail ID.")
            if rail in seen:
                raise ValueError("Duplicate capability rail ID.")
            seen.add(rail)
            readings.append(SensorReading(
                f"{key}.rail_{rail}", name, _RAIL_NAMES.get(rail, f"未知供电路 {rail}"),
                unit, _number(value), f"{prefix}{rail - 1}" if rail in _RAIL_NAMES else ""))
    return readings


class SensorStats:
    """Current availability, lifetime aggregates and at most 600 recent points."""

    def __init__(self, history_limit: int = 600):
        if not isinstance(history_limit, int) or not 1 <= history_limit <= 600:
            raise ValueError("history_limit must be 1–600")
        self.history = deque(maxlen=history_limit)
        self.current = None
        self.min = None
        self.max = None
        self.average = None
        self.count = 0
        self.online = False
        self.last_updated = None

    @property
    def minimum(self):
        return self.min

    @property
    def maximum(self):
        return self.max

    def update(self, value: float, timestamp: float | None = None):
        value = _number(value)
        timestamp = time.monotonic() if timestamp is None else _number(timestamp)
        self.current = value
        self.min = value if self.min is None else min(self.min, value)
        self.max = value if self.max is None else max(self.max, value)
        self.count += 1
        self.average = value if self.average is None else (
            self.average * ((self.count - 1) / self.count) + value / self.count)
        self.online = True
        self.last_updated = timestamp
        self.history.append((timestamp, value))

    def mark_offline(self):
        # Keep historical aggregates, but a stale number must never be current.
        self.current = None
        self.online = False

    def reset(self, keep_current: bool = True):
        current = self.current if keep_current and self.online else None
        timestamp = self.last_updated
        self.history.clear()
        self.current = self.min = self.max = self.average = None
        self.count = 0
        self.online = False
        self.last_updated = None
        if current is not None:
            self.update(current, timestamp)


class TelemetryCollector:
    """GUI-side supervisor. Device access is confined to a disposable child.

    Events are JSON-compatible dictionaries. ``status`` carries ``state``,
    ``message`` and ``online``. Only a completed ``snapshot`` sets online True.
    Changing the interval restarts the worker and its connection safely.
    ``worker_command`` is an optional test/development child command override.
    """

    def __init__(self, interval: float = 2.0, *, watchdog_timeout: float = 20.0,
                 restart_delay: float = 3.0, worker_command: Sequence[str] | None = None):
        self._validate_interval(interval)
        if not 0 < watchdog_timeout <= 20:
            raise ValueError("watchdog_timeout must be positive and at most 20 seconds")
        if restart_delay < 0:
            raise ValueError("restart_delay must not be negative")
        self._interval = float(interval)
        self._watchdog_timeout = float(watchdog_timeout)
        self._restart_delay = float(restart_delay)
        self._worker_command = tuple(worker_command) if worker_command else None
        self._events = queue.Queue(maxsize=256)
        self._lock = threading.RLock()
        self._thread = None
        self._process = None
        self._stop_event = threading.Event()
        self._restart_event = threading.Event()
        self._run_id = 0
        self._last_progress = 0.0
        self._online = False

    @staticmethod
    def _validate_interval(interval):
        if isinstance(interval, bool) or not isinstance(interval, (int, float)) or not 1 <= interval <= 30:
            raise ValueError("interval must be 1–30 seconds")

    @property
    def interval(self):
        with self._lock:
            return self._interval

    @property
    def online(self):
        with self._lock:
            return self._online

    @property
    def running(self):
        with self._lock:
            return self._thread is not None and self._thread.is_alive() and not self._stop_event.is_set()

    def _emit(self, event: dict):
        # Bound memory even if the GUI is paused or its event loop is busy.
        try:
            self._events.put_nowait(event)
        except queue.Full:
            try:
                self._events.get_nowait()
            except queue.Empty:
                pass
            try:
                self._events.put_nowait(event)
            except queue.Full:
                pass

    def _status(self, state: str, message: str):
        with self._lock:
            if state != "connected":
                self._online = False
            online = self._online
        self._emit({"event": "status", "state": state, "message": message, "online": online})

    def start(self):
        with self._lock:
            if self.running:
                return
            self._run_id += 1
            self._stop_event = threading.Event()
            self._restart_event = threading.Event()
            self._online = False
            self._thread = threading.Thread(
                target=self._supervise, args=(self._run_id, self._stop_event, self._restart_event),
                name="psu-supervisor", daemon=True)
            self._thread.start()

    def stop(self):
        with self._lock:
            self._stop_event.set()
            process, thread = self._process, self._thread
            self._process = None  # Reject any reader output queued after stop.
            self._online = False
        self._kill(process)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=3)
        self._status("stopped", "监测已停止")

    def set_interval(self, seconds: float):
        self._validate_interval(seconds)
        with self._lock:
            if float(seconds) == self._interval:
                return
            self._interval = float(seconds)
            if self.running:
                self._restart_event.set()

    def get_events(self) -> list[dict]:
        events = []
        while True:
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                return events

    def _command(self):
        if self._worker_command:
            return list(self._worker_command)
        if getattr(sys, "frozen", False):
            return [sys.executable, "--collector-worker", "--interval", str(self.interval),
                    "--parent-pid", str(os.getpid())]
        return [sys.executable, "-X", "utf8", str(Path(__file__).resolve()),
                "--worker", "--interval", str(self.interval),
                "--parent-pid", str(os.getpid())]

    @staticmethod
    def _kill(process):
        if process is None:
            return
        try:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _read_stdout(self, process, run_id: int, stop_event: threading.Event,
                     restart_event: threading.Event):
        try:
            for line in process.stdout:
                with self._lock:
                    if (stop_event.is_set() or restart_event.is_set() or
                            self._process is not process or self._run_id != run_id):
                        return
                    try:
                        event = json.loads(line)
                        if not isinstance(event, dict):
                            continue
                    except (json.JSONDecodeError, ValueError):
                        continue
                    kind = event.get("event")
                    if kind in ("heartbeat", "snapshot"):
                        self._last_progress = time.monotonic()
                    if kind == "snapshot":
                        try:
                            if not flatten_snapshot(event):
                                raise ValueError("Empty telemetry snapshot")
                        except (ValueError, TypeError, AttributeError):
                            self._status("offline", "收到无效传感器数据，等待下次采样")
                            continue
                        self._online = True
                        event["online"] = True
                        event["received_monotonic"] = time.monotonic()
                        self._emit(event)
                    elif kind == "status":
                        self._status(str(event.get("state", "offline")),
                                     str(event.get("message", "电源连接状态已变化")))
                    elif kind == "error":
                        self._online = False
                        self._emit(event)
                        self._status("offline", "读取失败，正在等待重新连接")
                    elif kind == "heartbeat":
                        # Heartbeats feed the watchdog without flooding the GUI.
                        continue
        except (OSError, ValueError):
            pass

    def _supervise(self, run_id, stop_event, restart_event):
        while not stop_event.is_set():
            with self._lock:
                if self._run_id != run_id:
                    return
                restart_event.clear()
            self._status("connecting", "正在连接 HP OMEN 电源…")
            process = None
            reader = None
            reason = "监测进程意外退出，正在重新连接"
            try:
                options = {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT,
                           "stdin": subprocess.DEVNULL, "text": True,
                           "encoding": "utf-8", "errors": "replace", "bufsize": 1}
                if os.name == "nt":
                    options["creationflags"] = subprocess.CREATE_NO_WINDOW
                process = subprocess.Popen(self._command(), **options)
                with self._lock:
                    if stop_event.is_set() or self._run_id != run_id:
                        self._kill(process)
                        return
                    self._process = process
                    self._last_progress = time.monotonic()
                reader = threading.Thread(
                    target=self._read_stdout, args=(process, run_id, stop_event, restart_event),
                    name="psu-worker-output", daemon=True)
                reader.start()
                while not stop_event.wait(0.1):
                    if restart_event.is_set():
                        reason = "采样间隔已更新，正在重新连接"
                        break
                    if process.poll() is not None:
                        break
                    with self._lock:
                        elapsed = time.monotonic() - self._last_progress
                    if elapsed >= self._watchdog_timeout:
                        reason = "电源读取长时间无响应，已重启监测进程"
                        break
            except (OSError, ValueError) as error:
                reason = "无法启动监测进程：" + str(error)
            finally:
                with self._lock:
                    if self._process is process:
                        self._process = None
                    self._online = False
                self._kill(process)
                if reader is not None:
                    reader.join(timeout=0.5)
                if process is not None and process.stdout is not None and (reader is None or not reader.is_alive()):
                    process.stdout.close()
            if stop_event.is_set() or self._run_id != run_id:
                return
            self._status("restarting", reason)
            if not restart_event.is_set():
                stop_event.wait(self._restart_delay)


def _restore_worker_stdout():
    """Use the inherited pipe even when launched from a frozen windowed GUI."""
    if sys.stdout is not None:
        try:
            sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
        except (AttributeError, ValueError):
            pass
        return
    try:
        sys.stdout = open(1, "w", encoding="utf-8", buffering=1, closefd=False)
    except OSError:
        if os.name != "nt":
            raise
        # These APIs access the child's stdout pipe, never the PSU or a device.
        import ctypes
        from ctypes import wintypes
        import msvcrt
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetStdHandle.argtypes = [wintypes.DWORD]
        kernel.GetStdHandle.restype = wintypes.HANDLE
        handle = kernel.GetStdHandle(0xFFFFFFF5)  # STD_OUTPUT_HANDLE (-11)
        if not handle or handle == ctypes.c_void_p(-1).value:
            raise OSError("Worker has no inherited stdout pipe.")
        fd = msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
        sys.stdout = io.TextIOWrapper(os.fdopen(fd, "wb"), encoding="utf-8", line_buffering=True)


class _ParentProcessLink:
    """Hold a SYNCHRONIZE-only reference to the original Windows parent process.

    Keeping the process handle, rather than repeatedly reopening a PID, also
    prevents PID reuse from making a dead parent's replacement appear alive.
    """

    def __init__(self, parent_pid: int):
        self.pid = parent_pid
        self._handle = None
        self._kernel = None
        self._already_exited = False
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel.WaitForSingleObject.restype = wintypes.DWORD
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.CloseHandle.restype = wintypes.BOOL
            self._handle = kernel.OpenProcess(0x00100000, False, parent_pid)
            self._kernel = kernel
            if not self._handle:
                error = ctypes.get_last_error()
                if error == 87:  # The parent already exited before our startup.
                    self._already_exited = True
                else:
                    raise ctypes.WinError(error)

    def wait(self, timeout_ms: int) -> bool:
        if self._already_exited:
            return True
        if self._kernel is not None:
            result = self._kernel.WaitForSingleObject(self._handle, timeout_ms)
            if result == 0:
                return True
            if result == 258:
                return False
            import ctypes
            raise ctypes.WinError(ctypes.get_last_error())
        # Portable development/test fallback; production Windows uses the
        # stable process object above and never probes a device or registry here.
        if timeout_ms:
            time.sleep(timeout_ms / 1000)
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return False
        return False

    def close(self):
        if self._kernel is not None and self._handle:
            self._kernel.CloseHandle(self._handle)
            self._handle = None


def _cleanup_dead_parent_bridge(parent_pid: int):
    # The bridge rechecks app ownership, exact dead-parent PID and session under
    # its cross-process mutex. It never claims or clears a new GUI session.
    from hwinfo_bridge import cleanup_abandoned_parent
    cleanup_abandoned_parent(parent_pid)


class _ParentDeathGuard:
    """Terminate this child independently of any blocked native HID operation."""

    def __init__(self, parent_pid: int, *, link_factory=None, exit_process=None,
                 cleanup=None, cleanup_timeout: float = 0.5):
        self._link = (link_factory or _ParentProcessLink)(parent_pid)
        self._parent_pid = parent_pid
        self._exit_process = exit_process or os._exit
        self._cleanup = cleanup or _cleanup_dead_parent_bridge
        self._cleanup_timeout = cleanup_timeout
        self._stop = threading.Event()
        self._thread = None

    def _parent_exited(self):
        # Registry cleanup is best effort and bounded. A stalled mutex/registry
        # must never prevent the orphaned device worker from terminating.
        def cleanup():
            try:
                self._cleanup(self._parent_pid)
            except Exception:
                pass
        thread = threading.Thread(target=cleanup, name="psu-abandoned-bridge", daemon=True)
        try:
            thread.start()
            thread.join(timeout=self._cleanup_timeout)
        finally:
            self._exit_process(0)

    def start(self) -> bool:
        if self._link.wait(0):
            try:
                self._parent_exited()
            finally:
                self._link.close()
            return False
        self._thread = threading.Thread(target=self._watch, name="psu-parent-guard", daemon=True)
        self._thread.start()
        return True

    def _watch(self):
        try:
            while not self._stop.is_set():
                try:
                    exited = self._link.wait(500)
                except Exception:
                    # Fail closed if the lifetime guarantee itself fails. There
                    # is no confirmed parent death, so do not touch its registry.
                    self._exit_process(2)
                    return
                if exited and not self._stop.is_set():
                    self._parent_exited()
                    return
        finally:
            self._link.close()

    def close(self):
        self._stop.set()
        if self._thread is None:
            self._link.close()
        elif self._thread is not threading.current_thread():
            self._thread.join(timeout=1)


def worker_main(argv: Sequence[str] | None = None) -> int:
    """Child-only native collector; GUI entry supports --collector-worker."""
    _restore_worker_stdout()
    parser = argparse.ArgumentParser(description="HP OMEN PSU collector worker")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--collector-worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--parent-pid", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        TelemetryCollector._validate_interval(args.interval)
    except ValueError as error:
        parser.error(str(error))
    if args.parent_pid is not None and (
            not 0 < args.parent_pid <= 0xFFFFFFFF or args.parent_pid == os.getpid()):
        parser.error("parent-pid must identify another positive process ID")

    guard = None
    try:
        if args.parent_pid is not None:
            guard = _ParentDeathGuard(args.parent_pid)
            if not guard.start():
                return 0
        return _worker_run(args)
    except OSError as error:
        print(json.dumps({"event": "error", "message": "无法监视主程序进程：" + str(error)},
                         ensure_ascii=True), flush=True)
        return 2
    finally:
        if guard is not None:
            guard.close()


def _worker_run(args) -> int:

    # Lazy import: parent/GUI models never initialize or access native HID APIs.
    from hp_omen_psu_monitor import TelemetryHid, QUERIES, decode_response
    from hp_omen_psu_input_probe import utc_now

    def emit(event, **values):
        print(json.dumps({"event": event, **values}, ensure_ascii=True), flush=True)

    def heartbeat(phase):
        emit("heartbeat", phase=phase)

    def wait(seconds, phase):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
            heartbeat(phase)

    def read_query(native, handle, name, capabilities):
        transaction = native.query(handle, name, 2.0)
        raw = bytes.fromhex(transaction["response_hex"])
        try:
            decoded = decode_response(name, raw, capabilities)
        except (ValueError, IndexError) as error:
            emit("query_error", name=name, message=str(error), **{
                key: transaction[key] for key in ("request_hex", "response_hex")})
            raise
        heartbeat("query:" + name)
        transaction["decoded"] = decoded
        return decoded, transaction

    while True:
        native = None
        handle = None
        emit("status", state="connecting", message="正在查找 HP OMEN 电源…")
        try:
            native = TelemetryHid()
            paths = [path for path in native.paths() if "vid_03f0&pid_04b0" in path.lower()]
            if not paths:
                raise RuntimeError("未找到 HP OMEN 电源，请检查 USB 连接")
            last_error = None
            for path in paths:
                candidate = None
                try:
                    candidate = native.open_telemetry(path)
                    metadata = native.metadata(candidate)  # Also verifies VID/PID.
                    expected = {"revision": "0200", "usage_page": "0xFF00", "usage": "0x0001",
                                "input_report_bytes": 64, "output_report_bytes": 64,
                                "feature_report_bytes": 0}
                    if any(metadata[key] != value for key, value in expected.items()):
                        raise RuntimeError("设备协议与已验证的 HP OMEN 0200 版本不符")
                    handle = candidate
                    break
                except (OSError, RuntimeError, KeyError) as error:
                    last_error = error
                    if candidate is not None:
                        native.kernel.CloseHandle(candidate)
            if handle is None:
                raise RuntimeError(str(last_error or "没有匹配的电源接口"))
            emit("status", state="connected", message="电源已连接，正在读取传感器", metadata=metadata)
            capability, capability_transaction = read_query(native, handle, "capability", ())
            capabilities = capability["rail_ids"]
            first = True
            sample = 0
            while True:
                started = time.monotonic()
                values = {}
                transactions = [capability_transaction] if first else []
                for name in QUERIES:
                    if name == "capability":
                        continue
                    decoded, transaction = read_query(native, handle, name, capabilities)
                    values[name] = decoded
                    transactions.append(transaction)
                sample += 1
                emit("snapshot", sample=sample, timestamp_utc=utc_now(),
                     capabilities=capabilities, measurements=values, transactions=transactions)
                first = False
                wait(max(0.0, args.interval - (time.monotonic() - started)), "idle")
        except KeyboardInterrupt:
            return 0
        except Exception as error:
            emit("error", message=str(error), windows_error=getattr(error, "winerror", None))
            emit("status", state="offline", message="电源暂时离线，3 秒后自动重连")
        finally:
            if native is not None and handle is not None:
                native.kernel.CloseHandle(handle)
        try:
            wait(3.0, "reconnect")
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    raise SystemExit(worker_main())
