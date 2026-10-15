"""Create public release archives from explicit, checked file lists.

This tool never walks the workspace. Archive entries use stable ordering and
timestamps; the input executable itself is not guaranteed to be reproducible.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import sys
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
DENIED_DIRECTORIES = {
    "reports", "data", "build", "dist", "dist-update", "release", ".build-deps",
    ".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".codex", ".agents", ".aws",
}
DENIED_SUFFIXES = {".dll", ".pdb", ".exe", ".zip", ".7z", ".pyc", ".pyo", ".log"}


class ReleaseError(ValueError):
    """A release input or destination is unsafe or incomplete."""


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _reject_links(path: Path, root: Path) -> None:
    current = path
    while current != root:
        if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
            raise ReleaseError(f"Links are not release inputs: {path}")
        current = current.parent


def _output_directory(path: Path, root: Path) -> Path:
    # Inspect the lexical path before resolve() can hide an in-tree symlink.
    lexical = Path(os.path.abspath(path))
    if not _inside(lexical, root) or lexical == root:
        raise ReleaseError("Release output must be a dedicated directory inside the project.")
    _reject_links(lexical, root)
    resolved = lexical.resolve()
    if not _inside(resolved, root) or resolved == root:
        raise ReleaseError("Release output must be a dedicated directory inside the project.")
    return resolved


def source_files(root: Path, manifest: Path) -> dict[str, Path]:
    """Validate each literal manifest entry and return its public archive path."""
    root = root.resolve(strict=True)
    try:
        document = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ReleaseError(f"Cannot read release manifest: {error}") from error
    if not isinstance(document, dict) or set(document) != {"files"}:
        raise ReleaseError('Manifest must be an object containing only "files".')
    entries = document["files"]
    if not isinstance(entries, list) or not entries:
        raise ReleaseError("Manifest files must be a nonempty list.")
    files: dict[str, Path] = {}
    normalized_names = set()
    for entry in entries:
        if not isinstance(entry, str) or not entry or "\\" in entry:
            raise ReleaseError("Manifest entries must be nonempty relative paths using '/'.")
        posix, windows = PurePosixPath(entry), PureWindowsPath(entry)
        parts = entry.split("/")
        if (posix.is_absolute() or windows.drive or windows.is_absolute()
                or any(part in ("", ".", "..") for part in parts)
                or any(":" in part or "\x00" in part for part in parts)):
            raise ReleaseError(f"Unsafe manifest path: {entry}")
        if any(part.casefold() in DENIED_DIRECTORIES for part in parts):
            raise ReleaseError(f"Private/generated directory in manifest: {entry}")
        if posix.suffix.casefold() in DENIED_SUFFIXES or posix.name.casefold().endswith("_il.txt"):
            raise ReleaseError(f"Binary, private log or raw IL in source manifest: {entry}")
        folded = entry.casefold()
        if folded in normalized_names:
            raise ReleaseError(f"Duplicate manifest path: {entry}")
        normalized_names.add(folded)
        path = root.joinpath(*parts)
        _reject_links(path, root)
        try:
            resolved = path.resolve(strict=True)
        except OSError as error:
            raise ReleaseError(f"Missing manifest file: {entry}") from error
        if not _inside(resolved, root) or not resolved.is_file():
            raise ReleaseError(f"Manifest entry is not a regular file inside the project: {entry}")
        files[entry] = resolved
    return files


def archive_timestamp(epoch: str | None = None) -> tuple[int, int, int, int, int, int]:
    """Use SOURCE_DATE_EPOCH when provided, or ZIP's fixed 1980 baseline."""
    if epoch is None:
        return (1980, 1, 1, 0, 0, 0)
    try:
        timestamp = datetime.fromtimestamp(int(epoch), timezone.utc)
    except (ValueError, OverflowError, OSError) as error:
        raise ReleaseError("SOURCE_DATE_EPOCH must be a valid integer Unix timestamp.") from error
    if not 1980 <= timestamp.year <= 2107:
        raise ReleaseError("SOURCE_DATE_EPOCH must fall between 1980 and 2107 for ZIP archives.")
    return tuple(timestamp.timetuple()[:6])


def _atomic_bytes(path: Path, content: bytes) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".release-", suffix=".tmp", dir=path.parent,
                                         delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _archive(path: Path, contents: dict[str, Path | bytes], timestamp: tuple) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix=".release-", suffix=".zip.tmp", dir=path.parent,
                                         delete=False) as stream:
            temporary = Path(stream.name)
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED,
                             compresslevel=9) as archive:
            for name in sorted(contents):
                info = zipfile.ZipInfo(name, date_time=timestamp)
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                value = contents[name]
                archive.writestr(info, value.read_bytes() if isinstance(value, Path) else value,
                                 compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def portable_readme(version: str) -> bytes:
    text = f"""OMEN PSU Monitor {version}

Extract this ZIP, then run OmenPsuMonitor.exe. Windows 10/11 x64.
Supports the verified HP GAMING PSU USB HID device:
VID 03F0, PID 04B0, revision 0200. Other devices/revisions are unsupported.

Displays PSU power, temperature, efficiency, fan speed and rail measurements,
with current/min/max/average values, history charts and CSV logging/export.
F5 resets statistics; Ctrl+S exports history. The interface is in Chinese.
Only measurement queries are implemented; no fan/settings/firmware controls.

For HWiNFO64, enable the HWiNFO checkbox after connecting, then restart
HWiNFO if necessary. Find "HP OMEN PSU (ImprovingDual)" in Sensors.
Keep this application running to refresh the custom readings.
Pause, disconnect and exit remove the readings owned by this application.

Settings and logs are saved by default in %LOCALAPPDATA%\\OmenPsuMonitor.
To store them beside the executable instead, start:
  OmenPsuMonitor.exe --portable
Portable mode uses the adjacent data folder, which must be writable.

This is an independent project, unaffiliated with HP or HWiNFO.
License and runtime notices are included. See CHANGELOG.md for this release.
"""
    return text.encode("utf-8")


def build_release(*, root: Path, exe: Path, output_dir: Path, notices: Path,
                  version: str, manifest: Path | None = None,
                  source_date_epoch: str | None = None) -> list[Path]:
    """Build archives and a standalone EXE with notices and checksums."""
    root = root.resolve(strict=True)
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?", version):
        raise ReleaseError("Version must be a safe semantic version such as 1.1.0.")
    output_dir = _output_directory(output_dir, root)
    if any(part.casefold() in {".git", ".codex", ".agents", ".aws"}
           for part in output_dir.relative_to(root).parts):
        raise ReleaseError("Release output cannot use project metadata directories.")
    exe = exe.resolve(strict=True)
    notices = notices.resolve(strict=True)
    if not exe.is_file() or not notices.is_file():
        raise ReleaseError("Executable and third-party notices must be regular files.")
    if not notices.stat().st_size:
        raise ReleaseError("Third-party notices are empty.")
    files = source_files(root, manifest or root / "release-files.json")
    required = {"LICENSE", "CHANGELOG.md", "README.md"}
    if not required.issubset(files):
        raise ReleaseError("Source manifest must include LICENSE, CHANGELOG.md and README.md.")
    timestamp = archive_timestamp(source_date_epoch)
    target_exe = output_dir / "OmenPsuMonitor.exe"
    target_license = output_dir / "LICENSE.txt"
    target_notices = output_dir / "THIRD_PARTY_NOTICES.txt"
    binary_zip = output_dir / f"OmenPsuMonitor-{version}-win64.zip"
    source_zip = output_dir / f"OmenPsuMonitor-{version}-source.zip"
    checksum_path = output_dir / "SHA256SUMS.txt"
    destinations = [target_exe, target_license, target_notices, binary_zip, source_zip, checksum_path]
    for destination in destinations:
        if destination.is_symlink() or getattr(destination, "is_junction", lambda: False)():
            raise ReleaseError(f"Release destination is a link: {destination}")
        if destination in files.values() or destination == notices:
            raise ReleaseError(f"Release output would overwrite an input file: {destination}")
        if destination.exists() and not destination.is_file():
            raise ReleaseError(f"Release destination is not a file: {destination}")
    output_dir.mkdir(parents=True, exist_ok=True)
    if exe != target_exe:
        # Atomic replacement avoids leaving a truncated executable on failure.
        _atomic_bytes(target_exe, exe.read_bytes())
    _atomic_bytes(target_license, files["LICENSE"].read_bytes())
    _atomic_bytes(target_notices, notices.read_bytes())
    portable = {"OmenPsuMonitor.exe": target_exe,
                "README.txt": portable_readme(version),
                "LICENSE": files["LICENSE"],
                "THIRD_PARTY_NOTICES.txt": notices,
                "CHANGELOG.md": files["CHANGELOG.md"]}
    _archive(binary_zip, portable, timestamp)
    _archive(source_zip, files, timestamp)
    assets = sorted([target_exe, target_license, target_notices, binary_zip, source_zip],
                    key=lambda path: path.name)
    checksums = "".join(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n" for path in assets)
    _atomic_bytes(checksum_path, checksums.encode("ascii"))
    return [*assets, checksum_path]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--exe", type=Path, required=True, help="Existing Windows executable to package.")
    parser.add_argument("--output-dir", type=Path, default=Path("release"))
    parser.add_argument("--notices", type=Path, default=Path("build/THIRD_PARTY_NOTICES.txt"))
    args = parser.parse_args(argv)
    sys.path.insert(0, str(ROOT))
    from app_version import VERSION

    def project_path(path):
        return path if path.is_absolute() else ROOT / path

    try:
        paths = build_release(root=ROOT, exe=project_path(args.exe),
                              output_dir=project_path(args.output_dir),
                              notices=project_path(args.notices), version=VERSION,
                              source_date_epoch=os.environ.get("SOURCE_DATE_EPOCH"))
    except (ReleaseError, OSError) as error:
        parser.error(str(error))
    for path in paths:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
