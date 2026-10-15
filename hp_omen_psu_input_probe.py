"""Passively receive Windows HID input from HP GAMING PSU 03F0:04B0.

No device output, feature requests, settings changes, or firmware operations.
Uses only the Python standard library. Run with Python 3.10+ on Windows.
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


VID, PID = 0x03F0, 0x04B0
INVALID_HANDLE = C.c_void_p(-1).value
SHARE_ALL = 0x1 | 0x2 | 0x4
ERROR_NO_MORE_ITEMS = 259
ERROR_INSUFFICIENT_BUFFER = 122
ERROR_IO_PENDING = 997
ERROR_OPERATION_ABORTED = 995
ERROR_NOT_FOUND = 1168
WAIT_OBJECT_0, WAIT_TIMEOUT, WAIT_FAILED = 0, 258, 0xFFFFFFFF


class GUID(C.Structure):
    _fields_ = [("Data1", W.DWORD), ("Data2", W.WORD),
                ("Data3", W.WORD), ("Data4", W.BYTE * 8)]


class SP_DEVICE_INTERFACE_DATA(C.Structure):
    _fields_ = [("cbSize", W.DWORD), ("InterfaceClassGuid", GUID),
                ("Flags", W.DWORD), ("Reserved", C.c_size_t)]


class HIDD_ATTRIBUTES(C.Structure):
    _fields_ = [("Size", W.ULONG), ("VendorID", W.USHORT),
                ("ProductID", W.USHORT), ("VersionNumber", W.USHORT)]


class HIDP_CAPS(C.Structure):
    _fields_ = [("Usage", W.USHORT), ("UsagePage", W.USHORT),
                ("InputReportByteLength", W.USHORT),
                ("OutputReportByteLength", W.USHORT),
                ("FeatureReportByteLength", W.USHORT),
                ("Reserved", W.USHORT * 17)] + [
        (name, W.USHORT) for name in (
            "NumberLinkCollectionNodes", "NumberInputButtonCaps",
            "NumberInputValueCaps", "NumberInputDataIndices",
            "NumberOutputButtonCaps", "NumberOutputValueCaps",
            "NumberOutputDataIndices", "NumberFeatureButtonCaps",
            "NumberFeatureValueCaps", "NumberFeatureDataIndices")]


class OVERLAPPED(C.Structure):
    _fields_ = [("Internal", C.c_size_t), ("InternalHigh", C.c_size_t),
                ("Offset", W.DWORD), ("OffsetHigh", W.DWORD),
                ("hEvent", W.HANDLE)]


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def error_info(exc):
    return {"message": str(exc), "windows_error": getattr(exc, "winerror", None)}


class NativeHid:
    def __init__(self):
        if sys.platform != "win32":
            raise RuntimeError("This probe requires Windows.")
        expected = {GUID: 16, HIDD_ATTRIBUTES: 12, HIDP_CAPS: 64,
                    SP_DEVICE_INTERFACE_DATA: 32 if C.sizeof(C.c_void_p) == 8 else 28,
                    OVERLAPPED: 32 if C.sizeof(C.c_void_p) == 8 else 20}
        for struct, size in expected.items():
            if C.sizeof(struct) != size:
                raise RuntimeError(f"Unexpected {struct.__name__} layout: {C.sizeof(struct)}")
        self.kernel = C.WinDLL("kernel32", use_last_error=True)
        self.hid = C.WinDLL("hid", use_last_error=True)
        self.setup = C.WinDLL("setupapi", use_last_error=True)
        self._bind(self.hid, "HidD_GetHidGuid", [C.POINTER(GUID)], None)
        self._bind(self.setup, "SetupDiGetClassDevsW",
                   [C.POINTER(GUID), W.LPCWSTR, W.HWND, W.DWORD], W.HANDLE)
        self._bind(self.setup, "SetupDiEnumDeviceInterfaces",
                   [W.HANDLE, C.c_void_p, C.POINTER(GUID), W.DWORD,
                    C.POINTER(SP_DEVICE_INTERFACE_DATA)], W.BOOL)
        self._bind(self.setup, "SetupDiGetDeviceInterfaceDetailW",
                   [W.HANDLE, C.POINTER(SP_DEVICE_INTERFACE_DATA), C.c_void_p,
                    W.DWORD, C.POINTER(W.DWORD), C.c_void_p], W.BOOL)
        self._bind(self.setup, "SetupDiDestroyDeviceInfoList", [W.HANDLE], W.BOOL)
        self._bind(self.kernel, "CreateFileW",
                   [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD,
                    W.DWORD, W.HANDLE], W.HANDLE)
        self._bind(self.kernel, "CloseHandle", [W.HANDLE], W.BOOL)
        self._bind(self.hid, "HidD_GetAttributes",
                   [W.HANDLE, C.POINTER(HIDD_ATTRIBUTES)], C.c_ubyte)
        self._bind(self.hid, "HidD_GetPreparsedData",
                   [W.HANDLE, C.POINTER(C.c_void_p)], C.c_ubyte)
        self._bind(self.hid, "HidD_FreePreparsedData", [C.c_void_p], C.c_ubyte)
        self._bind(self.hid, "HidP_GetCaps",
                   [C.c_void_p, C.POINTER(HIDP_CAPS)], W.LONG)
        self._bind(self.kernel, "CreateEventW",
                   [C.c_void_p, W.BOOL, W.BOOL, W.LPCWSTR], W.HANDLE)
        self._bind(self.kernel, "ReadFile",
                   [W.HANDLE, C.c_void_p, W.DWORD, C.POINTER(W.DWORD),
                    C.POINTER(OVERLAPPED)], W.BOOL)
        self._bind(self.kernel, "WaitForSingleObject", [W.HANDLE, W.DWORD], W.DWORD)
        self._bind(self.kernel, "CancelIoEx",
                   [W.HANDLE, C.POINTER(OVERLAPPED)], W.BOOL)
        self._bind(self.kernel, "GetOverlappedResult",
                   [W.HANDLE, C.POINTER(OVERLAPPED), C.POINTER(W.DWORD), W.BOOL], W.BOOL)

    @staticmethod
    def _bind(library, name, args, result):
        function = getattr(library, name)
        function.argtypes, function.restype = args, result

    def paths(self):
        guid = GUID()
        self.hid.HidD_GetHidGuid(C.byref(guid))
        devices = self.setup.SetupDiGetClassDevsW(C.byref(guid), None, None, 0x12)
        if devices == INVALID_HANDLE:
            raise C.WinError(C.get_last_error())
        paths = []
        try:
            index = 0
            while True:
                interface = SP_DEVICE_INTERFACE_DATA()
                interface.cbSize = C.sizeof(interface)
                if not self.setup.SetupDiEnumDeviceInterfaces(
                        devices, None, C.byref(guid), index, C.byref(interface)):
                    error = C.get_last_error()
                    if error == ERROR_NO_MORE_ITEMS:
                        break
                    raise C.WinError(error)
                needed = W.DWORD()
                result = self.setup.SetupDiGetDeviceInterfaceDetailW(
                    devices, C.byref(interface), None, 0, C.byref(needed), None)
                error = C.get_last_error()
                if not result and error != ERROR_INSUFFICIENT_BUFFER:
                    raise C.WinError(error)
                if not 8 <= needed.value <= 1024 * 1024:
                    raise RuntimeError(f"Invalid device detail size: {needed.value}")
                detail = C.create_string_buffer(needed.value)
                C.cast(detail, C.POINTER(W.DWORD))[0] = 8 if C.sizeof(C.c_void_p) == 8 else 6
                if not self.setup.SetupDiGetDeviceInterfaceDetailW(
                        devices, C.byref(interface), detail, needed,
                        C.byref(needed), None):
                    raise C.WinError(C.get_last_error())
                # WCHAR DevicePath begins immediately after DWORD cbSize.
                paths.append(C.wstring_at(C.addressof(detail) + C.sizeof(W.DWORD)))
                index += 1
        finally:
            self.setup.SetupDiDestroyDeviceInfoList(devices)
        return paths

    def open(self, path, *, read=False):
        handle = self.kernel.CreateFileW(path, 0x80000000 if read else 0,
                                         SHARE_ALL, None, 3,
                                         0x40000000 if read else 0, None)
        if handle == INVALID_HANDLE:
            raise C.WinError(C.get_last_error())
        return handle

    def metadata(self, handle):
        attrs = HIDD_ATTRIBUTES()
        attrs.Size = C.sizeof(attrs)
        if not self.hid.HidD_GetAttributes(handle, C.byref(attrs)):
            raise C.WinError(C.get_last_error())
        if (attrs.VendorID, attrs.ProductID) != (VID, PID):
            raise RuntimeError("Device identity did not match HP PSU 03F0:04B0.")
        preparsed = C.c_void_p()
        if not self.hid.HidD_GetPreparsedData(handle, C.byref(preparsed)):
            raise C.WinError(C.get_last_error())
        try:
            caps = HIDP_CAPS()
            status = self.hid.HidP_GetCaps(preparsed, C.byref(caps)) & 0xFFFFFFFF
            if status != 0x00110000:
                raise RuntimeError(f"HidP_GetCaps failed with NTSTATUS 0x{status:08X}")
            return {
                "vid": f"{attrs.VendorID:04X}", "pid": f"{attrs.ProductID:04X}",
                "revision": f"{attrs.VersionNumber:04X}",
                "usage_page": f"0x{caps.UsagePage:04X}", "usage": f"0x{caps.Usage:04X}",
                "input_report_bytes": caps.InputReportByteLength,
                "output_report_bytes": caps.OutputReportByteLength,
                "feature_report_bytes": caps.FeatureReportByteLength,
                "input_button_caps": caps.NumberInputButtonCaps,
                "input_value_caps": caps.NumberInputValueCaps,
            }
        finally:
            self.hid.HidD_FreePreparsedData(preparsed)

    def receive(self, handle, length, timeout_ms):
        """One overlapped read; keep buffer/event alive until completion drains."""
        buffer = C.create_string_buffer(length)
        operation = OVERLAPPED()
        event = self.kernel.CreateEventW(None, True, False, None)
        if not event:
            raise C.WinError(C.get_last_error())
        operation.hEvent = event
        pending = False
        try:
            completed = self.kernel.ReadFile(handle, buffer, length, None, C.byref(operation))
            if not completed:
                error = C.get_last_error()
                if error != ERROR_IO_PENDING:
                    raise C.WinError(error)
                pending = True
                wait = self.kernel.WaitForSingleObject(event, timeout_ms)
                if wait == WAIT_TIMEOUT:
                    data = self._cancel_and_drain(handle, operation, buffer)
                    pending = False
                    return data, True
                if wait != WAIT_OBJECT_0:
                    error = C.get_last_error() if wait == WAIT_FAILED else 31
                    self._cancel_and_drain(handle, operation, buffer)
                    pending = False
                    raise C.WinError(error)
            count = W.DWORD()
            result = self.kernel.GetOverlappedResult(handle, C.byref(operation), C.byref(count), False)
            pending = False
            if not result:
                raise C.WinError(C.get_last_error())
            return buffer.raw[:count.value], False
        finally:
            # Cancel/drain also on an exception or Ctrl+C while a read is pending.
            try:
                if pending:
                    self._cancel_and_drain(handle, operation, buffer)
            finally:
                self.kernel.CloseHandle(event)

    def _cancel_and_drain(self, handle, operation, buffer):
        cancelled = self.kernel.CancelIoEx(handle, C.byref(operation))
        cancel_error = C.get_last_error() if not cancelled else 0
        count = W.DWORD()
        # Cancellation is asynchronous; ERROR_NOT_FOUND can race with completion.
        if self.kernel.GetOverlappedResult(handle, C.byref(operation), C.byref(count), True):
            return buffer.raw[:count.value]
        error = C.get_last_error()
        if error == ERROR_OPERATION_ABORTED:
            return None
        if cancel_error not in (0, ERROR_NOT_FOUND):
            raise C.WinError(cancel_error)
        raise C.WinError(error)


def emit_worker(event, **values):
    print(json.dumps({"event": event, **values}), flush=True)


def worker(args):
    native = NativeHid()
    handle = None
    try:
        handle = native.open(args._worker_path, read=True)
        metadata = native.metadata(handle)
        length = metadata["input_report_bytes"]
        if not 1 <= length <= 65535:
            raise RuntimeError(f"Invalid input report length: {length}")
        emit_worker("opened", metadata=metadata, access="GENERIC_READ", share="READ|WRITE|DELETE")
        started = time.monotonic()
        deadline = started + args.duration
        count, cancelled = 0, 0
        outcome = "timeout"
        while count < args.max_reports:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            data, cancellation = native.receive(handle, length, max(1, int(remaining * 1000)))
            cancelled += int(cancellation)
            if data is None:
                break
            if not data:
                outcome = "zero_length_completion"
                break
            count += 1
            emit_worker("report", elapsed_s=round(time.monotonic() - started, 6),
                        length=len(data), hex=data.hex(" "))
            if cancellation:
                break
        if count >= args.max_reports:
            outcome = "max_reports"
        elif count and outcome == "timeout":
            outcome = "observation_complete"
        emit_worker("finished", outcome=outcome, reports=count, cancelled_reads=cancelled,
                    elapsed_s=round(time.monotonic() - started, 6))
        return 0
    except (OSError, RuntimeError) as exc:
        emit_worker("error", **error_info(exc))
        return 2
    finally:
        if handle is not None:
            native.kernel.CloseHandle(handle)


def capture(path, args):
    # A child process provides an outer deadline even if a driver delays cancel.
    command = [sys.executable, "-X", "utf8", str(Path(__file__).resolve()), "--_worker-path", path,
               "--duration", str(args.duration), "--max-reports", str(args.max_reports)]
    deadline_exceeded = False
    try:
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8",
                                timeout=args.duration + 8, check=False)
        output, stderr, code = result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired as exc:
        deadline_exceeded = True
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        stderr, code = "Read worker exceeded outer deadline and was terminated.", None
    events, unparsed = [], []
    for line in output.splitlines():
        if line.strip():
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                unparsed.append(line)
    return {"events": events, "returncode": code,
            "outer_deadline_exceeded": deadline_exceeded, "stderr": stderr.strip(),
            "unparsed_output": unparsed}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=10, help="Seconds per interface (0.1-60).")
    parser.add_argument("--max-reports", type=int, default=32, help="Maximum reports per interface (1-4096).")
    parser.add_argument("--metadata-only", action="store_true", help="Do not open an input read handle.")
    parser.add_argument("--output", type=Path, help="JSON result path.")
    parser.add_argument("--_worker-path", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 0.1 <= args.duration <= 60 or not 1 <= args.max_reports <= 4096:
        parser.error("duration must be 0.1-60 and max-reports must be 1-4096")
    if args._worker_path:
        return worker(args)
    report = {"started_utc": utc_now(), "target": "VID_03F0&PID_04B0",
              "python": sys.version, "python_executable": sys.executable,
              "pointer_bits": C.sizeof(C.c_void_p) * 8,
              "mode": "metadata_only" if args.metadata_only else "passive_input",
              "duration_seconds": args.duration, "max_reports": args.max_reports,
              "safety": {"device_writes": False, "feature_requests": False,
                         "get_input_report_requests": False, "settings_changes": False},
              "interfaces": []}
    exit_code = 0
    print("HP GAMING PSU passive HID input probe (03F0:04B0)")
    print("No output reports, feature requests, settings or firmware operations.")
    try:
        native = NativeHid()
        paths = native.paths()
        report["total_hid_interfaces"] = len(paths)
        matches = [path for path in paths if "vid_03f0&pid_04b0" in path.lower()]
        print(f"Windows HID interfaces: {len(paths)}; matching PSU interfaces: {len(matches)}")
        if not matches:
            report["outcome"] = "device_not_found"
            exit_code = 2
        for path in matches:
            item = {"path": path}
            report["interfaces"].append(item)
            print(f"\n{path}")
            handle = None
            try:
                handle = native.open(path)
                item["metadata"] = native.metadata(handle)
            except (OSError, RuntimeError) as exc:
                item["metadata_error"] = error_info(exc)
                print(f"Metadata error: {exc}")
                exit_code = 2
                continue
            finally:
                if handle is not None:
                    native.kernel.CloseHandle(handle)
            meta = item["metadata"]
            print(f"REV={meta['revision']} UsagePage={meta['usage_page']} Usage={meta['usage']}")
            print(f"Input={meta['input_report_bytes']} Output={meta['output_report_bytes']} "
                  f"Feature={meta['feature_report_bytes']} bytes (including report ID)")
            if args.metadata_only:
                continue
            print(f"Listening for up to {args.duration:g}s / {args.max_reports} reports...", flush=True)
            item["capture"] = capture(path, args)
            for event in item["capture"]["events"]:
                if event["event"] == "report":
                    print(f"[{event['elapsed_s']:.3f}s] Received {event['length']} bytes: {event['hex']}")
                elif event["event"] == "finished":
                    print(f"Outcome: {event['outcome']}; reports={event['reports']}; "
                          f"elapsed={event['elapsed_s']:.3f}s")
                elif event["event"] == "error":
                    print(f"Input access/read error: {event['message']}")
            if item["capture"]["returncode"] != 0:
                exit_code = 2
                if item["capture"]["stderr"]:
                    print(item["capture"]["stderr"])
    except (OSError, RuntimeError, ValueError) as exc:
        report["error"] = error_info(exc)
        print(f"Error: {exc}")
        exit_code = 2
    report["finished_utc"] = utc_now()
    output = args.output or Path(__file__).resolve().parent / "reports" / (
        "psu_input_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nJSON result: {output.resolve()}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
