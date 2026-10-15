"""Generate release version resources and complete bundled runtime notices."""
from __future__ import annotations

from importlib import metadata
from pathlib import Path
import platform
import re
import struct
import sys
import tkinter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".build-deps"))
from app_version import VERSION


def main():
    if sys.platform != "win32" or struct.calcsize("P") != 8:
        raise SystemExit("Release builds require Windows x64.")
    if sys.version_info[:3] != (3, 14, 7):
        raise SystemExit("Release builds require CPython 3.14.7; use requirements-build.txt.")
    if not re.fullmatch(r"\d+\.\d+\.\d+", VERSION):
        raise SystemExit("VERSION must contain three numeric components.")
    for line in (ROOT / "requirements-build.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            package, expected = line.split("==")
            actual = metadata.version(package)
            if actual != expected:
                raise SystemExit(f"Build input mismatch: {package} {actual}, expected {expected}")
    build = ROOT / "build"
    build.mkdir(exist_ok=True)
    version_tuple = tuple(map(int, VERSION.split("."))) + (0,)
    values = {"CompanyName": "OMEN PSU Monitor contributors",
              "FileDescription": "HP OMEN PSU telemetry monitor and HWiNFO sensor bridge",
              "FileVersion": VERSION, "InternalName": "OmenPsuMonitor",
              "LegalCopyright": "Copyright (c) 2026 OMEN PSU Monitor contributors",
              "OriginalFilename": "OmenPsuMonitor.exe", "ProductName": "OMEN PSU Monitor",
              "ProductVersion": VERSION}
    strings = ",\n".join(f"StringStruct({key!r}, {value!r})" for key, value in values.items())
    resource = (f"VSVersionInfo(ffi=FixedFileInfo(filevers={version_tuple!r}, prodvers={version_tuple!r}, "
                "mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)), "
                f"kids=[StringFileInfo([StringTable('040904B0', [{strings}])]), "
                "VarFileInfo([VarStruct('Translation', [1033, 1200])])])\n")
    (build / "version_info.txt").write_text(resource, encoding="utf-8")

    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    pyinstaller_license = ROOT / ".build-deps" / "pyinstaller-6.22.3.dist-info" / "licenses" / "COPYING.txt"
    sections = [("Python License", python_license.read_text(encoding="utf-8")),
                ("PyInstaller License and Bootloader Exception", pyinstaller_license.read_text(encoding="utf-8"))]
    window = tkinter.Tk()
    window.withdraw()
    try:
        tcl_version = window.tk.call("info", "patchlevel")
        tk_version = window.tk.call("package", "provide", "Tk")
        for variable, label in (("tcl_library", "Tcl License"), ("tk_library", "Tk License")):
            library = window.tk.getvar(variable)
            handle = window.tk.call("open", str(library) + "/license.terms", "r")
            try:
                sections.append((label, window.tk.call("read", handle)))
            finally:
                window.tk.call("close", handle)
    finally:
        window.destroy()
    header = (f"OMEN PSU Monitor {VERSION} - Third-Party Runtime Notices\n\n"
              f"Python {platform.python_version()} (https://www.python.org/)\n"
              f"Tcl {tcl_version}, Tk {tk_version} (https://www.tcl-lang.org/)\n"
              "PyInstaller 6.22.3 bootloader (https://pyinstaller.org/)\n\n"
              "Application code: MIT (see LICENSE). Bundled runtimes retain their licenses below.\n"
              "No HP or HWiNFO program DLLs, SDK binaries, or disassembled source are bundled.\n")
    notices = header + "".join(f"\n\n--- {title} ---\n{text}" for title, text in sections)
    (build / "THIRD_PARTY_NOTICES.txt").write_text(notices, encoding="utf-8")
    print(f"Generated Windows version {VERSION} and runtime licenses.")


if __name__ == "__main__":
    main()
