"""Query HP OMEN PSU telemetry using verified vendor read requests only.

These requests travel in HID Output Reports but use vendor READ operation 03.
Only capability/measurement opcodes are implemented. No settings/fan controls.
Protocol notes: docs/PROTOCOL.md.
"""

from __future__ import annotations

import argparse
import ctypes as C
from ctypes import wintypes as W
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time
from types import MappingProxyType

from hp_omen_psu_input_probe import (
    NativeHid, OVERLAPPED, SHARE_ALL, INVALID_HANDLE, ERROR_IO_PENDING,
    WAIT_OBJECT_0, WAIT_TIMEOUT, WAIT_FAILED, utc_now, error_info,
)


# Fixed whitelist derived from LiteOnHelper.CheckBasicInfo/CheckStatus.
# Tuple: command byte, optional selector payload, unit scale, display unit.
QUERIES = MappingProxyType({
    "capability": (0x16, b"", None, None),
    "power": (0x35, b"", 1, "W"),
    "efficiency": (0x36, b"", 0.1, "%"),
    "temperature": (0x37, b"\x01", 0.1, "C"),
    "fan": (0x38, b"\x01", 1, "RPM"),
    "voltage": (0x33, b"\x01", 0.01, "V"),
    "current": (0x34, b"\x01", 0.01, "A"),
})
RAILS = {1: "3.3V", 2: "5V", 3: "5V standby", 4: "12V total",
         5: "12V motherboard", 6: "12V CPU", 7: "12V graphics"}


def build_query(name):
    opcode, payload, _, _ = QUERIES[name]
    data = bytes((0x02, 0x07, 0x01, 0x03, opcode))
    if payload:
        data += bytes((0x00, len(payload))) + payload
    return data.ljust(64, b"\x00")


def decode_response(name, raw, capabilities=()):
    opcode, request_payload, scale, unit = QUERIES[name]
    if len(raw) != 64:
        raise ValueError(f"Expected 64-byte response, got {len(raw)}")
    if int.from_bytes(raw[3:5], "little") != opcode:
        raise ValueError(f"Unexpected response command: {raw[3:5].hex(' ')}")
    size = raw[5]
    if not 0 < size <= len(raw) - 6:
        raise ValueError(f"Invalid response payload length: {size}")
    payload = raw[6:6 + size]
    if name == "capability":
        return {"rail_ids": list(payload),
                "rails": [RAILS.get(value, f"unknown_{value:02X}") for value in payload]}
    selector = None
    if request_payload:
        selector, payload = payload[0], payload[1:]
    if not payload or len(payload) % 2:
        raise ValueError(f"Invalid LE16 measurement payload length: {len(payload)}")
    integers = [int.from_bytes(payload[index:index + 2], "little")
                for index in range(0, len(payload), 2)]
    values = [round(value * scale, 6) for value in integers]
    result = {"raw_values": integers, "values": values, "unit": unit}
    if selector is not None:
        result["selector"] = selector
    if name in ("power", "efficiency", "temperature", "fan"):
        result["overall"] = values[0]
    if name in ("voltage", "current") and len(capabilities) == len(values):
        result["by_rail"] = {RAILS.get(rail, f"unknown_{rail:02X}"): value
                             for rail, value in zip(capabilities, values)}
    return result


class IoOperation:
    """Own a buffer, OVERLAPPED and event until native I/O has completed."""
    def __init__(self, native, handle, data_or_length):
        self.native, self.handle = native, handle
        self.buffer = (C.create_string_buffer(data_or_length) if isinstance(data_or_length, int)
                       else C.create_string_buffer(data_or_length, len(data_or_length)))
        self.operation = OVERLAPPED()
        self.event = native.kernel.CreateEventW(None, True, False, None)
        if not self.event:
            raise C.WinError(C.get_last_error())
        self.operation.hEvent = self.event
        self.pending = False

    def start(self, function):
        result = function(self.handle, self.buffer, C.sizeof(self.buffer), None,
                          C.byref(self.operation))
        if not result:
            error = C.get_last_error()
            if error != ERROR_IO_PENDING:
                raise C.WinError(error)
            self.pending = True

    def finish(self, timeout_ms):
        if self.pending:
            wait = self.native.kernel.WaitForSingleObject(self.event, timeout_ms)
            if wait == WAIT_TIMEOUT:
                completed = self.native._cancel_and_drain(self.handle, self.operation, self.buffer)
                self.pending = False
                # Completion can win the race with CancelIoEx. Its buffer is
                # valid after draining and still goes through response checks.
                if completed is not None:
                    return completed
                raise TimeoutError("HID operation timed out; pending operation cancelled.")
            if wait != WAIT_OBJECT_0:
                error = C.get_last_error() if wait == WAIT_FAILED else 31
                raise C.WinError(error)
        count = W.DWORD()
        result = self.native.kernel.GetOverlappedResult(
            self.handle, C.byref(self.operation), C.byref(count), False)
        self.pending = False
        if not result:
            raise C.WinError(C.get_last_error())
        return self.buffer.raw[:count.value]

    def __enter__(self):
        return self

    def __exit__(self, *_):
        try:
            if self.pending:
                self.native._cancel_and_drain(self.handle, self.operation, self.buffer)
                self.pending = False
        finally:
            self.native.kernel.CloseHandle(self.event)


class TelemetryHid(NativeHid):
    def __init__(self):
        super().__init__()
        self._bind(self.kernel, "WriteFile",
                   [W.HANDLE, C.c_void_p, W.DWORD, C.POINTER(W.DWORD),
                    C.POINTER(OVERLAPPED)], W.BOOL)

    def open_telemetry(self, path):
        handle = self.kernel.CreateFileW(path, 0x80000000 | 0x40000000,
                                         SHARE_ALL, None, 3, 0x40000000, None)
        if handle == INVALID_HANDLE:
            raise C.WinError(C.get_last_error())
        return handle

    def query(self, handle, name, timeout):
        packet = build_query(name)  # Arbitrary report bytes/opcodes are not accepted.
        expected = QUERIES[name][0].to_bytes(2, "little")
        deadline = time.monotonic() + timeout
        unsolicited = []
        # Post the input read before output, matching the vendor's async reader.
        with IoOperation(self, handle, 64) as incoming:
            incoming.start(self.kernel.ReadFile)
            with IoOperation(self, handle, packet) as outgoing:
                outgoing.start(self.kernel.WriteFile)
                written = outgoing.finish(max(1, int((deadline - time.monotonic()) * 1000)))
                if len(written) != 64:
                    raise RuntimeError(f"Short query write: {len(written)}/64")
            raw = incoming.finish(max(1, int((deadline - time.monotonic()) * 1000)))
        # Another shared application can create unrelated replies; preserve them.
        while len(raw) != 64 or raw[3:5] != expected:
            unsolicited.append(raw.hex(" "))
            emit("unrelated_response", requested_name=name, response_hex=raw.hex(" "))
            remaining = deadline - time.monotonic()
            if remaining <= 0 or len(unsolicited) >= 32:
                raise TimeoutError(f"No matching response to {name}; unrelated={len(unsolicited)}")
            raw, _ = self.receive(handle, 64, max(1, int(remaining * 1000)))
            if raw is None:
                raise TimeoutError(f"No matching response to {name}")
        return {"name": name, "request_hex": packet.hex(" "), "response_hex": raw.hex(" "),
                "unrelated_responses_hex": unsolicited}


def emit(event, **values):
    print(json.dumps({"event": event, **values}), flush=True)


def worker(args):
    native = TelemetryHid()
    handle = None
    try:
        handle = native.open_telemetry(args._worker_path)
        metadata = native.metadata(handle)
        expected = {"revision": "0200", "usage_page": "0xFF00", "usage": "0x0001",
                    "input_report_bytes": 64, "output_report_bytes": 64, "feature_report_bytes": 0}
        if any(metadata[key] != value for key, value in expected.items()):
            raise RuntimeError("Device metadata differs from the verified 0200 protocol target.")
        emit("opened", metadata=metadata, vendor_operation="03 READ", output_report_id=2)
        capabilities = []
        for sample in range(args.samples):
            started = time.monotonic()
            values, transactions = {}, []
            names = list(QUERIES) if sample == 0 else [name for name in QUERIES if name != "capability"]
            for name in names:
                emit("query_started", sample=sample + 1, name=name,
                     request_hex=build_query(name).hex(" "))
                transaction = native.query(handle, name, args.timeout)
                raw = bytes.fromhex(transaction["response_hex"])
                try:
                    decoded = decode_response(name, raw, capabilities)
                except ValueError as exc:
                    emit("transaction", sample=sample + 1, **transaction, decode_error=str(exc))
                    raise
                transaction["decoded"] = decoded
                transactions.append(transaction)
                emit("transaction", sample=sample + 1, **transaction)
                if name == "capability":
                    capabilities = decoded["rail_ids"]
                else:
                    values[name] = decoded
            emit("snapshot", sample=sample + 1, timestamp_utc=utc_now(),
                 capabilities=capabilities, measurements=values)
            if sample + 1 < args.samples:
                time.sleep(max(0, args.interval - (time.monotonic() - started)))
        emit("finished", samples=args.samples)
        return 0
    except (OSError, RuntimeError, ValueError) as exc:
        emit("error", **error_info(exc))
        return 2
    finally:
        if handle is not None:
            native.kernel.CloseHandle(handle)


def run_worker(path, args):
    command = [sys.executable, "-X", "utf8", str(Path(__file__).resolve()),
               "--_worker-path", path, "--samples", str(args.samples),
               "--interval", str(args.interval), "--timeout", str(args.timeout)]
    # Each query has a deadline. Supervisor also bounds cancellation/driver faults.
    budget = (args.samples * 6 + 1) * args.timeout + (args.samples - 1) * args.interval + 8
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                timeout=budget, check=False)
        output, stderr, code = result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or b""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        stderr, code = "Telemetry worker exceeded outer deadline and was terminated.", None
    events, unparsed = [], []
    for line in output.splitlines():
        if line.strip():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                unparsed.append(line)
    return {"events": events, "unparsed_output": unparsed,
            "returncode": code, "stderr": stderr.strip()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=1, help="Number of snapshots (1-30).")
    parser.add_argument("--interval", type=float, default=2, help="Minimum interval between snapshots (1-30s).")
    parser.add_argument("--timeout", type=float, default=2, help="Per-query deadline (0.1-5s).")
    parser.add_argument("--dry-run", action="store_true", help="Show fixed requests without accessing hardware.")
    parser.add_argument("--output", type=Path, help="JSON result path.")
    parser.add_argument("--_worker-path", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 1 <= args.samples <= 30 or not 1 <= args.interval <= 30 or not 0.1 <= args.timeout <= 5:
        parser.error("samples must be 1-30, interval 1-30s, timeout 0.1-5s")
    if args.dry_run:
        for name in QUERIES:
            print(f"{name}: {build_query(name).hex(' ')}")
        return 0
    if args._worker_path:
        return worker(args)
    report = {"started_utc": utc_now(), "target": "VID_03F0&PID_04B0",
              "mode": "verified_telemetry_queries", "samples_requested": args.samples,
              "safety": {"hid_output_reports": True, "vendor_read_operation": "03",
                         "allowed_queries": list(QUERIES), "settings_operations": False,
                         "fan_controls": False, "feature_requests": False},
              "interfaces": []}
    exit_code = 0
    print("HP OMEN PSU telemetry: fixed capability + measurement read queries")
    print("Uses HID output for vendor READ 03; no settings or fan-control commands.")
    try:
        native = NativeHid()
        matches = [path for path in native.paths() if "vid_03f0&pid_04b0" in path.lower()]
        if not matches:
            raise RuntimeError("Target HP PSU HID interface not found.")
        for path in matches:
            print(f"Querying {path}", flush=True)
            capture = run_worker(path, args)
            report["interfaces"].append({"path": path, "capture": capture})
            for event in capture["events"]:
                if event["event"] == "snapshot":
                    values = event["measurements"]
                    print(f"Sample {event['sample']}: " + ", ".join(
                        f"{name}={values[name]['overall']:g} {values[name]['unit']}"
                        for name in ("power", "temperature", "fan", "efficiency")))
                    for name in ("voltage", "current"):
                        print(f"  {name}: {json.dumps(values[name], ensure_ascii=False)}")
                elif event["event"] == "error":
                    print(f"Query error: {event['message']}")
            if capture["returncode"] != 0:
                exit_code = 2
                if capture["stderr"]:
                    print(capture["stderr"])
    except (OSError, RuntimeError) as exc:
        report["error"] = error_info(exc)
        print(f"Error: {exc}")
        exit_code = 2
    report["finished_utc"] = utc_now()
    output = args.output or Path(__file__).resolve().parent / "reports" / (
        "psu_telemetry_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"JSON result: {output.resolve()}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
