# Contributing

Use Windows x64 and CPython 3.14.7 for release builds. Runtime source dependencies
are from the Python standard library, including Tkinter.

Run `python -m unittest discover -s tests -v` before submitting a change. Tests use
synthetic data and fake collectors/registry objects; they do not need a PSU.
Build and packaging instructions are in [docs/RELEASING.md](docs/RELEASING.md).

Keep changes to the fixed telemetry request whitelist reviewable. A new USB ID,
firmware revision, command, or conversion needs documented protocol evidence,
offline fixtures, and an explicit hardware validation report. Do not silently
enable device configuration, fan control, or firmware operations.

When reporting a problem, include the application version, Windows version,
USB VID/PID/revision, reproduction steps, and relevant error messages. Remove
usernames, full device instance paths and other private data from attachments.

Public source files are listed in `release-files.json`. Local logs, real device
captures, vendor binaries and disassembly output do not belong in that list.
