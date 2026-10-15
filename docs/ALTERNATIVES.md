# Similar projects reviewed

Research date: 2026-10-14. The goal was an existing public implementation for
HP GAMING PSU USB `03F0:04B0`, revision `0200`, including a HWiNFO data bridge.

| Project | Verified functionality | Relevance to this device |
|---|---|---|
| [liquidctl](https://github.com/liquidctl/liquidctl#supported-devices) | Digital PSU monitoring, including Corsair HXi/RMi and NZXT E | Current support table does not list this HP OMEN device. Its [Corsair driver](https://github.com/liquidctl/liquidctl/blob/main/liquidctl/driver/corsair_hid_psu.py) and [NZXT driver](https://github.com/liquidctl/liquidctl/blob/main/liquidctl/driver/nzxt_epsu.py) target other USB vendors. |
| [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor/tree/master/LibreHardwareMonitorLib/Hardware/Psu) | General hardware monitoring and digital PSU sensors | The reviewed PSU directory contains Corsair and MSI implementations, without a confirmed driver for `03F0:04B0`. |
| [FanControl.HWInfo](https://github.com/Rem0o/FanControl.HWInfo) | Imports readings already available in HWiNFO into FanControl | It does not implement this USB PSU's telemetry protocol. |
| [HASS-2-HwInfo](https://github.com/travis-fm/HASS-2-HwInfo) | Publishes Home Assistant values into HWiNFO custom sensors | Similar registry bridge, with a different data source. |
| [OmenMon](https://github.com/OmenMon/OmenMon) | OMEN laptop monitoring and control through BIOS WMI/EC | The reviewed documentation does not establish support for this USB PSU. |

No ready-to-use project supporting the exact device was confirmed in these
sources. This is a bounded search result, not a claim that none exists.

Public repository/issue searches covered the USB IDs, `HP OMEN PSU`, `OmenPsu`,
`LiteOn PSU`, and `IceAxe PSU`. Unauthenticated GitHub code-search API requests
require authentication, so a complete site-wide source-code search was not
available. Private and unindexed repositories were outside the search.
