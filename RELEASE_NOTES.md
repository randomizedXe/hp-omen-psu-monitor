# OMEN PSU Monitor 1.1.0

First release prepared for public distribution.

- Windows x64 desktop monitoring for the verified HP GAMING PSU `03F0:04B0`, firmware `0200`.
- Power, temperature, fan speed, efficiency and rail voltage/current readings.
- Current/minimum/maximum/average, history charts, CSV logging and export.
- Optional HWiNFO custom sensor integration with English sensor labels.
- Supervised collection, automatic reconnect, stale-value removal and parent-exit cleanup.
- Default user-writable configuration directory; optional `--portable` mode.
- Source archive, Windows archive, runtime licenses and SHA-256 checksums.

Compatibility is experimental and limited to the device/revision described in
the README. The program only sends the fixed telemetry read requests. Values
are vendor-reported measurements.

Download the `win64.zip` for normal use, or `source.zip` for the project files.
After enabling the bridge and receiving live data, reopen HWiNFO Sensors to
discover **HP OMEN PSU (ImprovingDual)**.
