"""Offline vendor-framing and decode fixtures; no device access."""

import unittest

from hp_omen_psu_monitor import QUERIES, build_query, decode_response


def response(name, payload):
    # Synthetic fixture using the raw offsets confirmed in the HP parser.
    raw = bytearray(64)
    raw[0] = 1
    raw[3:5] = QUERIES[name][0].to_bytes(2, "little")
    raw[5] = len(payload)
    raw[6:6 + len(payload)] = payload
    return bytes(raw)


class ProtocolTests(unittest.TestCase):
    def test_fixed_read_requests(self):
        for name, (opcode, payload, _, _) in QUERIES.items():
            raw = build_query(name)
            self.assertEqual(len(raw), 64)
            self.assertEqual(raw[:5], bytes((2, 7, 1, 3, opcode)))
            if payload:
                self.assertEqual(raw[5:8], b"\x00\x01\x01")
                self.assertFalse(any(raw[8:]))
            else:
                self.assertFalse(any(raw[5:]))
        self.assertEqual(set(q[0] for q in QUERIES.values()), {0x16, *range(0x33, 0x39)})
        with self.assertRaises(KeyError):
            build_query("fan_control")

    def test_capability_and_rail_order(self):
        caps = decode_response("capability", response("capability", b"\x04\x01"))
        self.assertEqual(caps["rail_ids"], [4, 1])
        payload = b"\x01" + (1205).to_bytes(2, "little") + (330).to_bytes(2, "little")
        decoded = decode_response("voltage", response("voltage", payload), caps["rail_ids"])
        self.assertEqual(decoded["by_rail"], {"12V total": 12.05, "3.3V": 3.3})
        self.assertNotIn("by_rail", decode_response("voltage", response("voltage", payload), [4]))

    def test_measurement_units(self):
        cases = [("power", 240, 240), ("efficiency", 915, 91.5),
                 ("temperature", 425, 42.5), ("fan", 850, 850),
                 ("current", 1234, 12.34)]
        for name, integer, value in cases:
            payload = integer.to_bytes(2, "little")
            if QUERIES[name][1]:
                payload = b"\x01" + payload
            self.assertEqual(decode_response(name, response(name, payload))["values"], [value])

    def test_rejects_wrong_command_length_and_misaligned_data(self):
        with self.assertRaises(ValueError):
            decode_response("power", response("fan", b"\x01\x01\x00"))
        with self.assertRaises(ValueError):
            decode_response("power", bytes(63))
        with self.assertRaises(ValueError):
            decode_response("power", response("power", b"\x01"))
        raw = bytearray(response("power", b"\x01\x00"))
        raw[5] = 59
        with self.assertRaises(ValueError):
            decode_response("power", bytes(raw))


if __name__ == "__main__":
    unittest.main()
