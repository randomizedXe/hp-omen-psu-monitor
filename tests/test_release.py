"""Release allowlist and artifact checks; no real hardware or registry access."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from tools.build_release import ReleaseError, archive_timestamp, build_release, source_files


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        temporary_root = Path(__file__).resolve().parents[1] / "build" / "test-temp"
        temporary_root.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix="omen_release_test_", dir=temporary_root)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.names = ["README.md", "LICENSE", "CHANGELOG.md", "main.py", ".github/workflows/test.yml"]
        for name in self.names:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"Public fixture: {name}\n", encoding="utf-8")
        self.manifest = self.root / "release-files.json"
        self.write_manifest(self.names)
        self.exe = self.root / "build" / "test.exe"
        self.exe.parent.mkdir()
        self.exe.write_bytes(b"MZ synthetic executable, never executed")
        self.notices = self.exe.parent / "THIRD_PARTY_NOTICES.txt"
        self.notices.write_text("Synthetic dependency license fixture\n", encoding="utf-8")
        self.output = self.root / "release"

    def write_manifest(self, names):
        self.manifest.write_text(json.dumps({"files": names}), encoding="utf-8")

    def build(self, **overrides):
        arguments = dict(root=self.root, exe=self.exe, output_dir=self.output,
                         notices=self.notices, version="1.1.0")
        arguments.update(overrides)
        return build_release(**arguments)

    def test_archives_contain_only_public_allowlisted_files_and_matching_checksums(self):
        private = self.root / "reports" / "telemetry_first.json"
        private.parent.mkdir()
        private.write_text('"private fixture"', encoding="utf-8")
        assets = self.build()
        binary = self.output / "OmenPsuMonitor-1.1.0-win64.zip"
        source = self.output / "OmenPsuMonitor-1.1.0-source.zip"
        with zipfile.ZipFile(binary) as archive:
            self.assertEqual(set(archive.namelist()), {"OmenPsuMonitor.exe", "README.txt", "LICENSE",
                                                     "THIRD_PARTY_NOTICES.txt", "CHANGELOG.md"})
            self.assertEqual(archive.read("OmenPsuMonitor.exe"), self.exe.read_bytes())
            portable_help = archive.read("README.txt").decode("utf-8")
            self.assertIn("%LOCALAPPDATA%\\OmenPsuMonitor", portable_help)
            self.assertIn("OmenPsuMonitor.exe --portable", portable_help)
            self.assertEqual(archive.namelist(), sorted(archive.namelist()))
            self.assertTrue(all(item.date_time == (1980, 1, 1, 0, 0, 0) for item in archive.infolist()))
        with zipfile.ZipFile(source) as archive:
            self.assertEqual(set(archive.namelist()), set(self.names))
            for name in self.names:
                self.assertEqual(archive.read(name), (self.root / name).read_bytes())
        sums = (self.output / "SHA256SUMS.txt").read_text(encoding="ascii").splitlines()
        self.assertEqual((self.output / "LICENSE.txt").read_bytes(), (self.root / "LICENSE").read_bytes())
        self.assertEqual((self.output / "THIRD_PARTY_NOTICES.txt").read_bytes(), self.notices.read_bytes())
        self.assertEqual(len(sums), 5)
        for line in sums:
            digest, name = line.split("  ")
            self.assertEqual(digest, hashlib.sha256((self.output / name).read_bytes()).hexdigest())
        self.assertEqual(len(assets), 6)

    def test_archive_content_is_repeatable_and_accepts_exe_already_in_output(self):
        first = {path.name: path.read_bytes() for path in self.build()}
        second = {path.name: path.read_bytes() for path in self.build(exe=self.output / "OmenPsuMonitor.exe")}
        self.assertEqual(first, second)

    def test_source_date_epoch_sets_each_zip_entry(self):
        self.build(source_date_epoch="946684800")
        for path in self.output.glob("*.zip"):
            with zipfile.ZipFile(path) as archive:
                self.assertTrue(all(item.date_time == (2000, 1, 1, 0, 0, 0)
                                    for item in archive.infolist()))
        for value in ("not a date", "0", "999999999999999999999999"):
            with self.subTest(value=value), self.assertRaises(ReleaseError):
                archive_timestamp(value)

    def test_manifest_rejects_private_generated_vendor_and_unsafe_paths(self):
        invalid = ["../secret.txt", "/secret.txt", "C:/secret.txt", "C:secret.txt",
                   "folder\\secret.txt", "folder//x.txt", "./README.md", "reports/raw.json",
                   "data/settings.json", "build/module.py", "dist/foo.txt", ".build-deps/LICENSE",
                   "folder/__pycache__/x.pyc", ".git/config", "HP.vendor.dll", "vendor_request_il.txt",
                   "debug.log", "private.exe", "file:stream", "missing.py"]
        for name in invalid:
            self.write_manifest([*self.names, name])
            with self.subTest(name=name), self.assertRaises(ReleaseError):
                source_files(self.root, self.manifest)

    def test_manifest_rejects_duplicates_nonfiles_and_invalid_schema(self):
        for entries in ([*self.names, "README.md"], [*self.names, "readme.md"],
                        [*self.names, ".github"], [*self.names, 123], [], "README.md"):
            self.write_manifest(entries)
            with self.subTest(entries=entries), self.assertRaises(ReleaseError):
                source_files(self.root, self.manifest)
        for document in ([], {"files": self.names, "extra": True}):
            self.manifest.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaises(ReleaseError):
                source_files(self.root, self.manifest)

    def test_links_cannot_escape_or_enter_archives(self):
        link = self.root / "link.md"
        try:
            link.symlink_to(self.root / "README.md")
        except (OSError, NotImplementedError):
            self.skipTest("Symbolic-link creation is not available for this account.")
        self.write_manifest([*self.names, "link.md"])
        with self.assertRaises(ReleaseError):
            source_files(self.root, self.manifest)

    def test_incomplete_or_unsafe_release_fails_before_writing_artifacts(self):
        for output in (self.root, self.root.parent / "outside-release", self.root / ".git" / "release"):
            with self.subTest(output=output), self.assertRaises(ReleaseError):
                self.build(output_dir=output)
        self.write_manifest(["main.py"])
        with self.assertRaises(ReleaseError):
            self.build()
        self.assertFalse(self.output.exists())

    def test_empty_notices_and_unsafe_version_are_rejected(self):
        self.notices.write_bytes(b"")
        with self.assertRaises(ReleaseError):
            self.build()
        with self.assertRaises(ReleaseError):
            self.build(version="../../private")
        self.assertFalse(self.output.exists())

    def test_destination_cannot_overwrite_a_manifest_file(self):
        target = self.root / "published" / "OmenPsuMonitor.exe"
        target.parent.mkdir()
        target.write_text("Source input", encoding="utf-8")
        # Executable suffixes are already forbidden from the source allowlist.
        self.write_manifest([*self.names, "published/OmenPsuMonitor.exe"])
        with self.assertRaises(ReleaseError):
            self.build(output_dir=target.parent)
        self.assertEqual(target.read_text(encoding="utf-8"), "Source input")

    def test_sidecar_license_cannot_overwrite_a_manifest_input(self):
        target = self.root / "published" / "LICENSE.txt"
        target.parent.mkdir()
        target.write_text("Source input", encoding="utf-8")
        self.write_manifest([*self.names, "published/LICENSE.txt"])
        with self.assertRaises(ReleaseError):
            self.build(output_dir=target.parent)
        self.assertEqual(target.read_text(encoding="utf-8"), "Source input")

    def test_sidecar_notices_cannot_overwrite_input_notices(self):
        target = self.root / "published" / "THIRD_PARTY_NOTICES.txt"
        target.parent.mkdir()
        target.write_text("Original notices", encoding="utf-8")
        with self.assertRaises(ReleaseError):
            self.build(output_dir=target.parent, notices=target)
        self.assertEqual(target.read_text(encoding="utf-8"), "Original notices")


if __name__ == "__main__":
    unittest.main()
