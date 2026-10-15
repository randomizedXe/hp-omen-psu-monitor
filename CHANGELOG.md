# Changelog

## 1.1.0 — 2026-10-14

- Prepare the first public distribution with English and Chinese documentation, an MIT project license, and bundled-runtime notices.
- Add a native Windows build/release workflow, source packaging, and SHA-256 checksums.
- Add GitHub CI and a release workflow that creates draft releases for review.
- Store normal-mode settings and logs in the current user's local application-data folder; retain adjacent data through explicit `--portable` mode and migrate known legacy settings on first normal startup.
- Prevent duplicate desktop instances and stop the collection worker if its parent app exits, with best-effort cleanup of abandoned HWiNFO values.
- Keep hardware access limited to the verified `03F0:04B0`, revision `0200` measurement protocol.
- Use synthetic fixtures in public offline tests and exclude local captures, personal settings, and vendor inspection artifacts from distribution.

## 1.0.1 — 2026-10-14

- Use English ASCII labels for HWiNFO custom sensors to avoid question-mark text in affected configurations.
- Preserve stable sensor IDs and the Chinese desktop interface.

## 1.0.0 — 2026-10-14

- Add a desktop display for PSU power, temperature, efficiency, fan speed, and capability-mapped rail voltage/current.
- Add current/minimum/maximum/average statistics, history charts, CSV recording/export, reconnection, and pause/resume.
- Add opt-in publication to HWiNFO64's registry custom sensor interface.
- Add fixed-query command-line collection and offline protocol, service, GUI, and bridge tests.
