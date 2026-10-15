# HP GAMING PSU measurement protocol

These notes describe the limited protocol implemented by this project. The byte layout and conversion factors were independently documented from behavior and static inspection of an installed HP component, then exercised on one matching PSU. No vendor source code, assemblies, or disassembly are distributed with this project. This document is not an official HP specification or a calibration certificate.

## Verified target

| Field | Value |
| --- | --- |
| USB VID / PID | `03F0` / `04B0` |
| Device revision | `0200` |
| HID Usage Page / Usage | `FF00` / `0001` |
| Input / output report length | 64 / 64 bytes, including report ID |
| Feature report length | 0 |

Windows report lengths include the report-ID byte, as described by [Microsoft's HIDP_CAPS documentation](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/hidpi/ns-hidpi-_hidp_caps). The application checks revision, collection usage, and report lengths before issuing measurement queries. Other firmware variants need separate evidence and validation.

## Requests

Byte values below are hexadecimal and offsets are zero based. Every request is exactly 64 bytes, zero padded. Byte 0 is output report ID `02`.

| Offset | Content |
| --- | --- |
| 0 | `02` |
| 1–2 | Fixed prefix `07 01` |
| 3 | Vendor operation `03` (READ) |
| 4 | Allowed command byte from the table below |
| 5 | `00` |
| 6 | Selector-payload length: `00` or `01` |
| 7 | Selector `01` when present; otherwise zero padding |
| Remaining bytes | Zero padding |

Only the following queries can be built:

| Query | Command | Selector payload | Numeric conversion |
| --- | --- | --- | --- |
| Rail capability list | `16` | Empty | Byte-valued rail IDs |
| Voltage | `33` | `01` | Unsigned LE16 × 0.01 V |
| Current | `34` | `01` | Unsigned LE16 × 0.01 A |
| Power | `35` | Empty | Unsigned LE16 × 1 W |
| Efficiency | `36` | Empty | Unsigned LE16 × 0.1 percent |
| Temperature | `37` | `01` | Unsigned LE16 × 0.1 °C |
| Fan speed | `38` | `01` | Unsigned LE16 × 1 RPM |

For example, a voltage request begins `02 07 01 03 33 00 01 01`, followed by 56 zero bytes. A power request begins `02 07 01 03 35`, followed by 59 zero bytes. `--dry-run` displays the actual fixed packets without device access.

The transport opens a shared read/write HID handle because queries require output reports. The vendor operation remains READ. The application does not accept arbitrary commands or implement setting, fan-control, or firmware-update operations.

## Responses

The decoder uses these fields from a 64-byte raw input report:

| Offset | Meaning used by this implementation |
| --- | --- |
| 0 | Input report-ID byte; retained in raw diagnostics |
| 3–4 | Returned command, unsigned little-endian; must match the requested command |
| 5 | Payload byte count, from 1 to 58 |
| 6 onward | Payload of the declared length |

The remaining header bytes have no validated meaning in this implementation. The decoder does not infer a checksum or status field.

Capability payload bytes are rail IDs in the order reported by the device. For voltage, current, temperature, and fan responses, the first payload byte is a selector; subsequent bytes are unsigned little-endian 16-bit integers. Power and efficiency start decoding with the first payload byte. Measurement data must be nonempty and contain a whole number of 16-bit values. Power, temperature, efficiency, and fan speed expose the first decoded number as their overall reading.

Efficiency uses a percentage scale: a synthetic integer `900` becomes `90.0%`, rather than a ratio of `0.9`.

## Rail mapping

| ID | Rail |
| --- | --- |
| 1 | 3.3 V |
| 2 | 5 V |
| 3 | 5 V standby |
| 4 | 12 V total |
| 5 | 12 V motherboard |
| 6 | 12 V CPU |
| 7 | 12 V graphics |

Voltage/current arrays map to the capability list in its returned order. The tested device exposes four rails, IDs 1–4. The other labels are understood by the software but have not been exercised on hardware. A count mismatch is not silently assigned fixed rail labels. Unknown rail IDs can appear in the desktop display but are not published to HWiNFO as known rails.

## Collection lifecycle

The worker posts an asynchronous input read before sending each query with `WriteFile`, then waits for a matching command response. Unrelated responses are skipped within a bounded deadline. Per-query deadlines, a supervised worker process, and cancellation handling bound stalled operations. [Microsoft documents](https://learn.microsoft.com/en-us/windows/win32/api/ioapiset/nf-ioapiset-cancelioex) that cancellation requests do not themselves establish completion; buffers remain owned until the operation is drained.

Capability is fetched on a new connection; subsequent snapshots query the six measurement commands. Each measurement is a separate transaction, so a snapshot does not represent one simultaneous electrical measurement.

## HWiNFO publication

The bridge follows the [official custom user sensor registry format](https://www.hwinfo.com/forum/threads/custom-user-sensors-in-hwinfo.5817/). It publishes to the current user's `Software\HWiNFO64\Sensors\Custom\HP OMEN PSU (ImprovingDual)` group. Overall readings map to `Power0`, `Temp0`, `Fan0`, and `Usage0`; known rail IDs map to `Volt{ID-1}` and `Current{ID-1}`. Floating-point readings use decimal strings. Sensor names are English ASCII text.

Ownership/session markers and a process mutex coordinate bridge instances. Normal cleanup removes owned sensor values when they are no longer live and preserves unrelated registry content. This bridge supplies data to HWiNFO; HWiNFO performs its own display and statistics.
