# OMEN PSU Monitor

[简体中文](README.zh-CN.md)

A Windows desktop monitor for the **HP GAMING PSU USB HID device `03F0:04B0`**, with an optional bridge to HWiNFO64 custom sensors.

Version **1.1.0**. This is an independent community project, unaffiliated with HP or HWiNFO. It monitors the PSU; it does not replace a general hardware monitor.

## Compatibility

| Item | Supported target |
| --- | --- |
| Operating system | Windows 10/11 x64; verified on Windows 11 x64 |
| Device | HP GAMING PSU, USB VID `03F0`, PID `04B0` |
| Firmware/device revision | `0200` only |
| HID collection | Usage page `FF00`, usage `0001`; 64-byte input/output reports, no Feature reports |
| Interface language | Chinese desktop UI; English sensor names in HWiNFO |

Hardware testing covers one device and firmware revision. Support is experimental: readings use the device's reported values and vendor-derived conversion factors, without independent calibration. A matching brand name alone does not establish compatibility.

## Features

- Twelve sensors on the tested device: PSU power, temperature, efficiency, fan speed, and voltage/current for 3.3 V, 5 V, 5 V standby, and 12 V total.
- Current, minimum, maximum, average, and a history chart for each sensor.
- 1/2/3/5/10-second sampling, pause/resume, reset statistics, and an always-on-top option.
- CSV recording and session export, automatic reconnection, and stale-reading removal.
- Optional live publication into HWiNFO64's Sensors window.

Queries send HID output reports with vendor **READ operation `03`**. Only capability and measurement commands are implemented; there are no fan-setting, configuration, or firmware-update commands. See [protocol notes](docs/PROTOCOL.md).

## Run the Windows app

1. Download `OmenPsuMonitor-1.1.0-win64.zip` from this repository's Releases page and extract it.
2. Run `OmenPsuMonitor.exe`. Python and HWiNFO are not required for the standalone display.
3. Wait for the connected status, then select a sensor to view its chart.

Settings and application logs are saved under `%LOCALAPPDATA%\OmenPsuMonitor`. To keep data beside the executable instead, start `OmenPsuMonitor.exe --portable` from a writable folder; portable mode uses its `data` subfolder. On the first normal startup, known settings from the older adjacent `data/settings.json` are migrated if available.

`F5` resets statistics; `Ctrl+S` exports session history. Charts retain up to 600 points per sensor and session export retains up to 50,000 snapshots. Use CSV recording for longer sessions.

## Show readings in HWiNFO64

1. Start this app and wait for live PSU readings.
2. Enable **接入 HWiNFO64** at the bottom of the window.
3. Restart HWiNFO if the group is not discovered, then open Sensors.
4. Find **HP OMEN PSU (ImprovingDual)**. Keep this app running to supply readings.

The bridge uses HWiNFO's [documented custom user sensor interface](https://www.hwinfo.com/forum/threads/custom-user-sensors-in-hwinfo.5817/), through the current user's registry:

```text
HKCU\Software\HWiNFO64\Sensors\Custom\HP OMEN PSU (ImprovingDual)
```

This is an external sensor provider, rather than a DLL loaded into HWiNFO. HWiNFO is installed separately and is not included. Sensor labels use English ASCII text to avoid the question-mark rendering seen with some HWiNFO configurations. Published values are withdrawn when collection pauses, data becomes unavailable, the bridge is disabled, or the app exits normally.

## Run from source

Use a Windows x64 Python installation with Tkinter. Python **3.14.7** is the verified build environment; application runtime code uses the standard library.

From the repository folder:

```powershell
python omen_psu_app.py
python -m unittest discover -s tests -v
```

Command-line collection is also available:

```powershell
# Print the fixed query packets without opening a device.
python hp_omen_psu_monitor.py --dry-run
# Read ten snapshots at a minimum interval of two seconds.
python hp_omen_psu_monitor.py --samples 10 --interval 2 --output telemetry.json
```

The JSON output contains raw reports and device interface paths. Review and redact diagnostic files before sharing them publicly. Offline tests use synthetic data and do not require the PSU or HWiNFO.

To build the executable and release archives, follow [RELEASING.md](docs/RELEASING.md). Changes are listed in [CHANGELOG.md](CHANGELOG.md). See [related projects](docs/ALTERNATIVES.md) for the GitHub compatibility search.

## Troubleshooting

- **Device not found:** check that Windows exposes USB device `VID_03F0&PID_04B0`. Other OMEN hardware is not supported by this protocol.
- **Protocol target differs:** revision and report-layout checks intentionally reject unverified variants. Include those identifiers in an issue rather than bypassing the checks.
- **Timeouts/disconnections:** reconnect the device and retry. If another PSU polling application is open, close it temporarily to check for interference.
- **HWiNFO group missing or old names:** keep publication enabled and fully restart HWiNFO. Only one running monitor instance can own the bridge group.
- **Logs:** inspect `app.log` in the data location above. Include the app version, Windows version, device revision, and a redacted error excerpt when reporting a problem.

## License

Project source is released under the [MIT License](LICENSE). See [NOTICES.md](NOTICES.md) for interoperability and bundled-runtime information. The Windows archive includes the full runtime licenses in `THIRD_PARTY_NOTICES.txt`. HP/OMEN and HWiNFO names identify compatible products; their respective owners retain their trademarks.
