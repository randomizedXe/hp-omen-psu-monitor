# Third-party and interoperability notices

The application code is licensed under MIT; see [LICENSE](LICENSE).

The Windows executable bundles CPython and Tcl/Tk and uses the PyInstaller
bootloader. The release build generates `THIRD_PARTY_NOTICES.txt` from the actual
runtime license files and the pinned PyInstaller license/bootloader exception.
That file is mandatory in the Windows release archive.

The build tools are pinned in `requirements-build.txt`. Their packages and caches
are not included in the source or Windows release archives.

HP, OMEN and HWiNFO names identify compatible hardware and software. This is an
independent project. It does not bundle HP application DLLs, HWiNFO SDK binaries,
or vendor disassembled code. The HWiNFO integration uses its publicly documented
[custom sensor registry interface](https://www.hwinfo.com/forum/threads/custom-user-sensors-in-hwinfo.5817/).
