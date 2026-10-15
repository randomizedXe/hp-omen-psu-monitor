"""Opt-in export of live PSU readings to HWiNFO custom user sensors.

Official interface: https://www.hwinfo.com/forum/threads/custom-user-sensors-in-hwinfo.5817/
Only this application's named device below HKCU is writable. Constructing a bridge
does not access the registry; the GUI must call publish only after opting in and
cleanup on disconnect, disable, or exit. No sensor values are fabricated here.
"""

from __future__ import annotations

from contextlib import contextmanager
import ctypes
import math
import os
import threading
from typing import Iterable, Protocol
import uuid


DEVICE_NAME = "HP OMEN PSU (ImprovingDual)"
CUSTOM_PATH = r"Software\HWiNFO64\Sensors\Custom"
DEVICE_PATH = CUSTOM_PATH + "\\" + DEVICE_NAME
OWNER_VALUE = "_ImprovingDualOwner"
SESSION_VALUE = "_ImprovingDualSession"
PID_VALUE = "_ImprovingDualPID"
OWNER_ID = "ImprovingDual.HP.OMEN.PSU.v1"
_MUTEX_NAME = r"Local\ImprovingDual.OmenPsuHWiNFOBridge"
_LOCAL_LOCK = threading.RLock()
_SENSOR_UNITS = {
    "Power0": "W", "Temp0": "°C", "Fan0": "RPM", "Usage0": "%",
    **{f"Volt{index}": "V" for index in range(7)},
    **{f"Current{index}": "A" for index in range(7)},
}
# HWiNFO's custom-sensor name rendering can depend on its character handling.
# Publish stable ASCII labels independently of the native GUI's Chinese labels.
_RAIL_LABELS = ("3.3V", "5V", "5V Standby", "12V Total", "12V Motherboard",
                "12V CPU", "12V Graphics")
_SENSOR_NAMES = {
    "Power0": "PSU Power", "Temp0": "PSU Temperature",
    "Fan0": "PSU Fan Speed", "Usage0": "PSU Efficiency",
    **{f"Volt{index}": f"{rail} Voltage" for index, rail in enumerate(_RAIL_LABELS)},
    **{f"Current{index}": f"{rail} Current" for index, rail in enumerate(_RAIL_LABELS)},
}


class Reading(Protocol):
    name: str
    group: str
    value: float
    unit: str
    hwinfo_key: str


class BridgeError(RuntimeError):
    """The bridge could not publish or clear its current values."""


class OwnershipError(BridgeError):
    """The named registry group belongs to someone else or is still in use."""


def _process_alive(pid: int) -> bool:
    """Conservatively retain another session if its process cannot be inspected."""
    if pid == os.getpid():
        return True
    if pid <= 0 or os.name != "nt":
        return True
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE only.
    if not handle:
        return ctypes.get_last_error() != 87  # Invalid PID means no process.
    try:
        return kernel.WaitForSingleObject(handle, 0) != 0
    finally:
        kernel.CloseHandle(handle)


@contextmanager
def _windows_transaction_lock(enabled: bool):
    """Serialize ownership claims across standalone bridge processes."""
    with _LOCAL_LOCK:
        if not enabled:
            yield
            return
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
        kernel.ReleaseMutex.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.CreateMutexW(None, False, _MUTEX_NAME)
        if not handle:
            raise BridgeError(f"Cannot open HWiNFO bridge mutex: {ctypes.get_last_error()}")
        acquired = False
        try:
            result = kernel.WaitForSingleObject(handle, 5000)
            if result not in (0, 0x80):  # Acquired or abandoned by a terminated owner.
                raise BridgeError("Another HWiNFO bridge registry transaction did not finish.")
            acquired = True
            yield
        finally:
            if acquired:
                kernel.ReleaseMutex(handle)
            kernel.CloseHandle(handle)


class HWiNFOBridge:
    """Publish a complete reading set; remove owned values when no longer live.

    The optional registry and process checker make offline tests independent of
    Windows, device access, and the actual user registry.
    """

    def __init__(self, registry=None, is_process_alive=None):
        self._registry = registry
        self._use_windows_mutex = registry is None and os.name == "nt"
        self._is_process_alive = is_process_alive or _process_alive
        self._session = uuid.uuid4().hex
        self._pid = os.getpid()

    def _reg(self):
        if self._registry is None:
            try:
                import winreg
            except ImportError as exc:
                raise BridgeError("The HWiNFO bridge requires Windows.") from exc
            self._registry = winreg
        return self._registry

    def _access(self):
        reg = self._reg()
        return reg.KEY_READ | reg.KEY_WRITE | getattr(reg, "KEY_WOW64_64KEY", 0)

    def _open_group(self):
        reg = self._reg()
        return reg.OpenKey(reg.HKEY_CURRENT_USER, DEVICE_PATH, 0, self._access())

    def _delete_group_key(self):
        reg = self._reg()
        if hasattr(reg, "DeleteKeyEx"):
            reg.DeleteKeyEx(reg.HKEY_CURRENT_USER, DEVICE_PATH,
                            getattr(reg, "KEY_WOW64_64KEY", 0), 0)
        else:
            reg.DeleteKey(reg.HKEY_CURRENT_USER, DEVICE_PATH)

    def _owner(self, key):
        reg = self._reg()
        try:
            owner, owner_type = reg.QueryValueEx(key, OWNER_VALUE)
            session, session_type = reg.QueryValueEx(key, SESSION_VALUE)
            pid, pid_type = reg.QueryValueEx(key, PID_VALUE)
        except FileNotFoundError:
            return None
        if (owner_type != reg.REG_SZ or session_type != reg.REG_SZ
                or pid_type != reg.REG_DWORD or not isinstance(session, str)
                or not session or not isinstance(pid, int) or isinstance(pid, bool)):
            return None
        return owner, session, pid

    def _is_owned_sensor(self, key, session):
        reg = self._reg()
        try:
            owner, owner_type = reg.QueryValueEx(key, OWNER_VALUE)
            saved_session, session_type = reg.QueryValueEx(key, SESSION_VALUE)
            return (owner == OWNER_ID and owner_type == reg.REG_SZ
                    and saved_session == session and session_type == reg.REG_SZ)
        except FileNotFoundError:
            return False

    @staticmethod
    def _subkeys(reg, key):
        return [reg.EnumKey(key, index) for index in range(reg.QueryInfoKey(key)[0])]

    def _remove_sensor(self, group, sensor_key, session):
        """Remove only our sensor values; retain unexpected foreign content."""
        reg = self._reg()
        try:
            key = reg.OpenKey(group, sensor_key, 0, self._access())
        except FileNotFoundError:
            return
        with key:
            if not self._is_owned_sensor(key, session):
                return
            # Value first: a later error must not leave a stale measurement visible.
            for name in ("Value", "Name", "Unit", OWNER_VALUE, SESSION_VALUE):
                try:
                    reg.DeleteValue(key, name)
                except FileNotFoundError:
                    pass
            subkeys, values, _ = reg.QueryInfoKey(key)
        if not subkeys and not values:
            reg.DeleteKey(group, sensor_key)

    def _remove_group(self, expected_session, expected_pid=None):
        reg = self._reg()
        try:
            group = self._open_group()
        except FileNotFoundError:
            return False
        with group:
            owner = self._owner(group)
            if (owner is None or owner[:2] != (OWNER_ID, expected_session)
                    or (expected_pid is not None and owner[2] != expected_pid)):
                return False
            for sensor_key in self._subkeys(reg, group):
                if sensor_key in _SENSOR_UNITS:
                    self._remove_sensor(group, sensor_key, expected_session)
            for name in (OWNER_VALUE, SESSION_VALUE, PID_VALUE):
                try:
                    reg.DeleteValue(group, name)
                except FileNotFoundError:
                    pass
            subkeys, values, _ = reg.QueryInfoKey(group)
        if not subkeys and not values:
            self._delete_group_key()
            return True
        return False  # A foreign key/value remains; leave it intact.

    def _claim_group(self):
        reg = self._reg()
        try:
            group = self._open_group()
        except FileNotFoundError:
            group = None
        if group is not None:
            with group:
                owner = self._owner(group)
                if owner is None or owner[0] != OWNER_ID:
                    raise OwnershipError("The HWiNFO PSU group already exists without our ownership marker.")
                if owner[1] == self._session and owner[2] == self._pid:
                    return self._open_group()
                if self._is_process_alive(owner[2]):
                    raise OwnershipError("Another running PSU monitor owns the HWiNFO sensor group.")
            if not self._remove_group(owner[1], expected_pid=owner[2]):
                raise OwnershipError("Old HWiNFO PSU data includes foreign content; it was preserved.")
        group = reg.CreateKeyEx(reg.HKEY_CURRENT_USER, DEVICE_PATH, 0, self._access())
        assigned = []
        try:
            # The transaction mutex protects create-and-mark across bridge processes.
            subkeys, values, _ = reg.QueryInfoKey(group)
            if subkeys or values:
                raise OwnershipError("The HWiNFO PSU group changed before ownership could be claimed.")
            for name, kind, value in ((OWNER_VALUE, reg.REG_SZ, OWNER_ID),
                                      (SESSION_VALUE, reg.REG_SZ, self._session),
                                      (PID_VALUE, reg.REG_DWORD, self._pid)):
                reg.SetValueEx(group, name, 0, kind, value)
                assigned.append(name)
            return group
        except BaseException:
            try:
                # Roll back only metadata written while claiming an empty group.
                # A failed marker write must not leave a group that blocks retry.
                for name in reversed(assigned):
                    reg.DeleteValue(group, name)
                subkeys, values, _ = reg.QueryInfoKey(group)
                if not subkeys and not values:
                    self._delete_group_key()
            except OSError:
                pass  # Preserve the original error; there are no sensor values yet.
            finally:
                group.Close()
            raise

    @staticmethod
    def _prepare(readings: Iterable[Reading]):
        prepared = {}
        for reading in readings:
            sensor_key = reading.hwinfo_key
            if sensor_key == "":
                continue  # Unknown rails remain visible in the app only.
            if sensor_key not in _SENSOR_UNITS:
                raise ValueError(f"Unsupported HWiNFO sensor key: {sensor_key!r}")
            unit = "°C" if reading.unit == "C" else reading.unit
            if unit != _SENSOR_UNITS[sensor_key]:
                raise ValueError(f"Wrong unit for {sensor_key}: {reading.unit!r}")
            if sensor_key in prepared:
                raise ValueError(f"Duplicate HWiNFO sensor key: {sensor_key}")
            if not isinstance(reading.name, str) or not reading.name.strip():
                raise ValueError(f"Missing HWiNFO name for {sensor_key}")
            if sensor_key.startswith(("Volt", "Current")):
                if not isinstance(reading.group, str) or not reading.group.strip():
                    raise ValueError(f"Missing rail label for {sensor_key}")
            if isinstance(reading.value, bool):
                raise ValueError(f"Invalid numeric value for {sensor_key}")
            value = float(reading.value)
            if not math.isfinite(value):
                raise ValueError(f"Nonfinite value for {sensor_key}")
            # Locale-independent decimal float strings required by HWiNFO.
            prepared[sensor_key] = (_SENSOR_NAMES[sensor_key], format(value, ".15g"))
        return prepared

    def publish(self, readings: Iterable[Reading]) -> int:
        """Replace the published snapshot; clear our values if publication fails."""
        with _windows_transaction_lock(self._use_windows_mutex):
            try:
                prepared = self._prepare(readings)
                if not prepared:
                    self._remove_group(self._session, expected_pid=self._pid)
                    return 0
                reg = self._reg()
                with self._claim_group() as group:
                    for sensor_key in self._subkeys(reg, group):
                        if sensor_key in _SENSOR_UNITS and sensor_key not in prepared:
                            self._remove_sensor(group, sensor_key, self._session)
                    for sensor_key, (name, value) in prepared.items():
                        try:
                            existing = reg.OpenKey(group, sensor_key, 0, self._access())
                        except FileNotFoundError:
                            existing = None
                        if existing is not None:
                            with existing:
                                if not self._is_owned_sensor(existing, self._session):
                                    raise OwnershipError(f"Sensor key {sensor_key} belongs to another publisher.")
                        with reg.CreateKeyEx(group, sensor_key, 0, self._access()) as key:
                            reg.SetValueEx(key, OWNER_VALUE, 0, reg.REG_SZ, OWNER_ID)
                            reg.SetValueEx(key, SESSION_VALUE, 0, reg.REG_SZ, self._session)
                            reg.SetValueEx(key, "Name", 0, reg.REG_SZ, name)
                            reg.SetValueEx(key, "Value", 0, reg.REG_SZ, value)
                return len(prepared)
            except (OSError, ValueError, TypeError, AttributeError, BridgeError) as exc:
                try:
                    self._remove_group(self._session, expected_pid=self._pid)
                except (OSError, BridgeError) as cleanup_error:
                    raise BridgeError(f"HWiNFO publication failed: {exc}; cleanup failed: {cleanup_error}") from exc
                if isinstance(exc, BridgeError):
                    raise
                raise BridgeError(f"HWiNFO publication failed: {exc}") from exc

    def cleanup(self) -> bool:
        """Remove our live data; preserve a foreign group or another live session."""
        with _windows_transaction_lock(self._use_windows_mutex):
            try:
                return self._remove_group(self._session, expected_pid=self._pid)
            except OSError as exc:
                raise BridgeError(f"Cannot clear HWiNFO PSU readings: {exc}") from exc

    close = cleanup


def cleanup_abandoned_parent(parent_pid: int, *, registry=None,
                             is_process_alive=None) -> bool:
    """Clear this app's abandoned readings after its GUI parent has terminated.

    A collector's parent-death guard may call this best-effort before exiting.
    This function never creates a group, claims a session, or publishes values.
    It preserves live processes, foreign groups and a replacement GUI's session.
    """
    if (isinstance(parent_pid, bool) or not isinstance(parent_pid, int)
            or not 0 < parent_pid <= 0xFFFFFFFF):
        raise ValueError("parent_pid must be a positive Windows process ID")
    bridge = HWiNFOBridge(registry=registry, is_process_alive=is_process_alive)
    with _windows_transaction_lock(bridge._use_windows_mutex):
        try:
            try:
                group = bridge._open_group()
            except FileNotFoundError:
                return False
            with group:
                owner = bridge._owner(group)
                if (owner is None or owner[0] != OWNER_ID or owner[2] != parent_pid
                        or bridge._is_process_alive(parent_pid)):
                    return False
            # Re-read both saved session and PID before deletion. The mutex
            # prevents another bridge from claiming the group between checks.
            return bridge._remove_group(owner[1], expected_pid=parent_pid)
        except OSError as exc:
            raise BridgeError(f"Cannot clear abandoned HWiNFO PSU readings: {exc}") from exc

