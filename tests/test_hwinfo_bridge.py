"""Offline registry contract and ownership tests. No winreg/HID calls."""

from dataclasses import dataclass
import unittest

from hwinfo_bridge import (
    BridgeError, DEVICE_PATH, HWiNFOBridge, OWNER_ID, OWNER_VALUE,
    OwnershipError, PID_VALUE, SESSION_VALUE, cleanup_abandoned_parent,
)


@dataclass
class Reading:
    key: str = "power"
    name: str = "PSU Total Power"
    group: str = "PSU"
    unit: str = "W"
    value: float = 123.5
    hwinfo_key: str = "Power0"


class Key:
    def __init__(self, registry, path):
        self.registry, self.path = registry, path

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.Close()

    def Close(self):
        pass


class Registry:
    HKEY_CURRENT_USER = "HKCU"
    KEY_READ, KEY_WRITE, KEY_WOW64_64KEY = 1, 2, 4
    REG_SZ, REG_DWORD = 1, 4

    def __init__(self):
        self.keys = {}
        self.mutations = []
        self.fail_value_once = None

    def path(self, root, subkey):
        return (root.path if isinstance(root, Key) else root) + "\\" + subkey

    def seed(self, path, values=None):
        path = self.path(self.HKEY_CURRENT_USER, path)
        parts = path.split("\\")
        for length in range(1, len(parts) + 1):
            self.keys.setdefault("\\".join(parts[:length]), {})
        if values:
            self.keys[path].update(values)

    def OpenKey(self, root, subkey, reserved=0, access=0):
        path = self.path(root, subkey)
        if path not in self.keys:
            raise FileNotFoundError(path)
        return Key(self, path)

    def CreateKeyEx(self, root, subkey, reserved=0, access=0):
        path = self.path(root, subkey)
        if path not in self.keys:
            self.seed(path.removeprefix("HKCU\\"))
            self.mutations.append(("create", path))
        return Key(self, path)

    def SetValueEx(self, key, name, reserved, kind, value):
        if name == self.fail_value_once:
            self.fail_value_once = None
            raise PermissionError("simulated write failure")
        self.keys[key.path][name] = (value, kind)
        self.mutations.append(("set", key.path, name, value, kind))

    def QueryValueEx(self, key, name):
        try:
            return self.keys[key.path][name]
        except KeyError:
            raise FileNotFoundError(name)

    def QueryInfoKey(self, key):
        return len(self.children(key.path)), len(self.keys[key.path]), 0

    def children(self, path):
        prefix = path + "\\"
        return sorted(name[len(prefix):] for name in self.keys
                      if name.startswith(prefix) and "\\" not in name[len(prefix):])

    def EnumKey(self, key, index):
        return self.children(key.path)[index]

    def DeleteValue(self, key, name):
        if name not in self.keys[key.path]:
            raise FileNotFoundError(name)
        del self.keys[key.path][name]
        self.mutations.append(("delete_value", key.path, name))

    def DeleteKey(self, root, subkey):
        path = self.path(root, subkey)
        if path not in self.keys:
            raise FileNotFoundError(path)
        if self.children(path):
            raise OSError("key has children")
        del self.keys[path]
        self.mutations.append(("delete_key", path))

    def DeleteKeyEx(self, root, subkey, access=0, reserved=0):
        if access != self.KEY_WOW64_64KEY:
            raise AssertionError("cleanup must use the same 64-bit registry view")
        self.DeleteKey(root, subkey)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.registry = Registry()
        self.bridge = HWiNFOBridge(registry=self.registry, is_process_alive=lambda pid: True)
        self.device = "HKCU\\" + DEVICE_PATH

    def value(self, sensor_key, name):
        return self.registry.keys[self.device + "\\" + sensor_key][name]

    def test_construction_does_not_access_or_mutate_registry(self):
        self.assertEqual(self.registry.keys, {})
        self.assertEqual(self.registry.mutations, [])

    def test_floats_and_stable_sensor_indices(self):
        readings = [Reading(), Reading(name="PSU Temperature", unit="°C", value=34.2, hwinfo_key="Temp0"),
                    Reading(name="电压", group="12V总路", unit="V", value=12.05, hwinfo_key="Volt3"),
                    Reading(name="电流", group="12V CPU", unit="A", value=3.125, hwinfo_key="Current5"),
                    Reading(name="Efficiency", unit="%", value=91.5, hwinfo_key="Usage0")]
        self.assertEqual(self.bridge.publish(readings), 5)
        self.assertEqual(self.value("Power0", "Value"), ("123.5", Registry.REG_SZ))
        self.assertEqual(self.value("Temp0", "Value"), ("34.2", Registry.REG_SZ))
        self.assertEqual(self.value("Volt3", "Name"), ("12V Total Voltage", Registry.REG_SZ))
        self.assertEqual(self.value("Current5", "Value"), ("3.125", Registry.REG_SZ))
        self.assertFalse(any("Unit" in values for values in self.registry.keys.values()))
        self.assertTrue(all(change[1].startswith("HKCU\\") for change in self.registry.mutations))

    def test_all_export_labels_are_ascii_even_with_chinese_gui_labels(self):
        units = {"Power0": "W", "Temp0": "°C", "Fan0": "RPM", "Usage0": "%",
                 **{f"Volt{index}": "V" for index in range(7)},
                 **{f"Current{index}": "A" for index in range(7)}}
        readings = [Reading(name="中文传感器", group="中文供电路", unit=unit,
                            value=12.34, hwinfo_key=key) for key, unit in units.items()]
        self.assertEqual(self.bridge.publish(readings), 18)
        for key in units:
            name, kind = self.value(key, "Name")
            self.assertTrue(name.isascii())
            self.assertNotIn("?", name)
            self.assertEqual(kind, Registry.REG_SZ)
            self.assertEqual(self.value(key, "Value"), ("12.34", Registry.REG_SZ))
        self.assertEqual(self.value("Volt2", "Name")[0], "5V Standby Voltage")
        self.assertEqual(self.value("Current5", "Name")[0], "12V CPU Current")

    def test_synthetic_snapshot_keeps_chinese_gui_and_exports_english_names(self):
        import json
        from pathlib import Path
        from psu_service import flatten_snapshot
        snapshot = json.loads((Path(__file__).resolve().parent /
                               "fixtures/snapshot.json").read_text(encoding="utf-8"))
        readings = flatten_snapshot(snapshot)
        self.assertTrue(any(not reading.name.isascii() for reading in readings))
        self.assertEqual(self.bridge.publish(readings), 12)
        for reading in readings:
            self.assertTrue(self.value(reading.hwinfo_key, "Name")[0].isascii())
            self.assertEqual(float(self.value(reading.hwinfo_key, "Value")[0]), reading.value)

    def test_publish_replaces_missing_sensors_without_changing_indices(self):
        self.bridge.publish([Reading(), Reading(unit="V", hwinfo_key="Volt3")])
        self.bridge.publish([Reading(unit="V", value=11.9, hwinfo_key="Volt3")])
        self.assertNotIn(self.device + "\\Power0", self.registry.keys)
        self.assertEqual(self.value("Volt3", "Value"), ("11.9", Registry.REG_SZ))

    def test_disable_disconnect_and_exit_cleanup_are_idempotent(self):
        self.bridge.publish([Reading()])
        self.assertTrue(self.bridge.cleanup())
        self.assertNotIn(self.device, self.registry.keys)
        self.assertFalse(self.bridge.close())
        self.assertEqual(self.bridge.publish([]), 0)
        self.assertNotIn(self.device, self.registry.keys)

    def test_foreign_group_is_never_changed_or_deleted(self):
        self.registry.seed(DEVICE_PATH, {"Keep": ("foreign", Registry.REG_SZ)})
        before = {key: value.copy() for key, value in self.registry.keys.items()}
        with self.assertRaises(OwnershipError):
            self.bridge.publish([Reading()])
        self.assertFalse(self.bridge.cleanup())
        self.assertEqual(self.registry.keys, before)
        self.assertEqual(self.registry.mutations, [])

    def test_live_other_session_is_not_changed(self):
        other = HWiNFOBridge(registry=self.registry, is_process_alive=lambda pid: True)
        other.publish([Reading(value=80)])
        before = {key: value.copy() for key, value in self.registry.keys.items()}
        with self.assertRaises(OwnershipError):
            self.bridge.publish([Reading()])
        self.assertFalse(self.bridge.cleanup())
        self.assertEqual(self.registry.keys, before)

    def test_dead_marked_session_can_be_reclaimed(self):
        old = HWiNFOBridge(registry=self.registry)
        old.publish([Reading(value=80)])
        self.bridge = HWiNFOBridge(registry=self.registry, is_process_alive=lambda pid: False)
        self.bridge.publish([Reading(value=100)])
        self.assertEqual(self.value("Power0", "Value"), ("100", Registry.REG_SZ))
        self.assertFalse(old.cleanup())
        self.assertTrue(self.bridge.cleanup())

    def test_foreign_sensor_under_owned_group_is_preserved(self):
        self.bridge.publish([Reading()])
        foreign = DEVICE_PATH + r"\Other0"
        self.registry.seed(foreign, {"Value": ("leave", Registry.REG_SZ)})
        self.assertFalse(self.bridge.cleanup())
        self.assertNotIn(self.device + "\\Power0", self.registry.keys)
        self.assertEqual(self.registry.keys["HKCU\\" + foreign]["Value"][0], "leave")
        with self.assertRaises(OwnershipError):
            self.bridge.publish([Reading()])

    def test_foreign_value_inside_our_sensor_is_preserved_and_reading_removed(self):
        self.bridge.publish([Reading()])
        self.registry.keys[self.device + "\\Power0"]["Foreign"] = ("keep", Registry.REG_SZ)
        self.assertFalse(self.bridge.cleanup())
        self.assertEqual(self.registry.keys[self.device + "\\Power0"], {"Foreign": ("keep", Registry.REG_SZ)})

    def test_failed_publish_clears_values_instead_of_leaving_stale_data(self):
        self.bridge.publish([Reading()])
        self.registry.fail_value_once = "Value"
        with self.assertRaises(BridgeError):
            self.bridge.publish([Reading(value=200)])
        self.assertNotIn(self.device, self.registry.keys)

    def test_failed_initial_ownership_marker_can_be_retried(self):
        self.registry.fail_value_once = SESSION_VALUE
        with self.assertRaises(BridgeError):
            self.bridge.publish([Reading()])
        self.assertNotIn(self.device, self.registry.keys)
        self.assertEqual(self.bridge.publish([Reading()]), 1)

    def test_dead_session_with_foreign_content_is_not_reclaimed(self):
        old = HWiNFOBridge(registry=self.registry)
        old.publish([Reading(value=80)])
        self.registry.seed(DEVICE_PATH + r"\Other0", {"Value": ("keep", Registry.REG_SZ)})
        self.bridge = HWiNFOBridge(registry=self.registry, is_process_alive=lambda pid: False)
        with self.assertRaises(OwnershipError):
            self.bridge.publish([Reading()])
        self.assertNotIn(self.device + "\\Power0", self.registry.keys)
        self.assertEqual(self.registry.keys[self.device + "\\Other0"]["Value"][0], "keep")

    def test_invalid_snapshot_clears_previous_values(self):
        for invalid in (Reading(value=float("nan")), Reading(value=float("inf")),
                        Reading(hwinfo_key=r"Other0\escape"), Reading(unit="kW"),
                        Reading(value=True)):
            with self.subTest(invalid=invalid):
                self.bridge.publish([Reading()])
                with self.assertRaises(BridgeError):
                    self.bridge.publish([invalid])
                self.assertNotIn(self.device, self.registry.keys)

    def test_duplicate_keys_rejected_and_unknown_rails_skipped(self):
        self.assertEqual(self.bridge.publish([Reading(hwinfo_key="")]), 0)
        with self.assertRaises(BridgeError):
            self.bridge.publish([Reading(), Reading()])
        self.assertNotIn(self.device, self.registry.keys)

    def test_missing_or_malformed_owner_marker_is_foreign(self):
        for values in ({}, {OWNER_VALUE: (OWNER_ID, Registry.REG_SZ)},
                       {OWNER_VALUE: (OWNER_ID, Registry.REG_SZ),
                        SESSION_VALUE: ("x", Registry.REG_SZ), PID_VALUE: ("bad", Registry.REG_SZ)}):
            with self.subTest(values=values):
                registry = Registry()
                registry.seed(DEVICE_PATH, values)
                bridge = HWiNFOBridge(registry=registry)
                with self.assertRaises(OwnershipError):
                    bridge.publish([Reading()])
                self.assertEqual(registry.mutations, [])

    def test_dead_parent_cleanup_removes_only_its_marked_session(self):
        self.bridge.publish([Reading()])
        parent_pid = self.registry.keys[self.device][PID_VALUE][0]
        self.assertTrue(cleanup_abandoned_parent(
            parent_pid, registry=self.registry, is_process_alive=lambda pid: False))
        self.assertNotIn(self.device, self.registry.keys)

    def test_parent_cleanup_refuses_live_parent_or_replacement_owner(self):
        self.bridge.publish([Reading()])
        parent_pid = self.registry.keys[self.device][PID_VALUE][0]
        before = {key: value.copy() for key, value in self.registry.keys.items()}
        self.assertFalse(cleanup_abandoned_parent(
            parent_pid, registry=self.registry, is_process_alive=lambda pid: True))
        self.assertFalse(cleanup_abandoned_parent(
            parent_pid + 1, registry=self.registry, is_process_alive=lambda pid: False))
        self.assertEqual(self.registry.keys, before)

    def test_parent_cleanup_refuses_foreign_or_missing_group_without_creation(self):
        self.assertFalse(cleanup_abandoned_parent(
            1234, registry=self.registry, is_process_alive=lambda pid: False))
        self.assertEqual(self.registry.keys, {})
        self.registry.seed(DEVICE_PATH, {"Foreign": ("keep", Registry.REG_SZ)})
        before = {key: value.copy() for key, value in self.registry.keys.items()}
        self.assertFalse(cleanup_abandoned_parent(
            1234, registry=self.registry, is_process_alive=lambda pid: False))
        self.assertEqual(self.registry.keys, before)
        self.assertEqual(self.registry.mutations, [])

    def test_parent_cleanup_rechecks_session_before_deletion(self):
        self.bridge.publish([Reading()])
        parent_pid = self.registry.keys[self.device][PID_VALUE][0]
        # Simulate an unrelated replacement while checking the old process.
        # Actual bridge publishers are additionally serialized by the mutex.
        def replaced_parent(pid):
            self.registry.keys[self.device][SESSION_VALUE] = ("new-session", Registry.REG_SZ)
            return False
        self.assertFalse(cleanup_abandoned_parent(
            parent_pid, registry=self.registry, is_process_alive=replaced_parent))
        self.assertEqual(self.value("Power0", "Value"), ("123.5", Registry.REG_SZ))

    def test_parent_cleanup_preserves_foreign_children(self):
        self.bridge.publish([Reading()])
        parent_pid = self.registry.keys[self.device][PID_VALUE][0]
        self.registry.seed(DEVICE_PATH + r"\Other0", {"Value": ("keep", Registry.REG_SZ)})
        self.assertFalse(cleanup_abandoned_parent(
            parent_pid, registry=self.registry, is_process_alive=lambda pid: False))
        self.assertNotIn(self.device + "\\Power0", self.registry.keys)
        self.assertEqual(self.registry.keys[self.device + "\\Other0"]["Value"][0], "keep")

    def test_parent_cleanup_rejects_invalid_pid(self):
        for invalid in (True, 0, -1, "1234", 0x100000000):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                cleanup_abandoned_parent(invalid, registry=self.registry)
        self.assertEqual(self.registry.keys, {})


if __name__ == "__main__":
    unittest.main()
