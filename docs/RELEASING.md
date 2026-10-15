# Building and releasing OMEN PSU Monitor

Run native Windows builds with x64 Python and Tkinter. The verified environment is Python **3.14.7** on Windows 11 x64. Do not reuse an executable built before changing the version or application code.

## Local build

From the repository root in PowerShell:

```powershell
python -m unittest discover -s tests -v
python -m pip install --target .build-deps -r requirements-build.txt
.\build_windows.ps1 -OutputDirectory dist-update
python tools/build_release.py --exe dist-update/OmenPsuMonitor.exe
```

The build script generates the app icon, Windows version resources, a one-file Windows GUI executable, and `build/THIRD_PARTY_NOTICES.txt` from the installed runtime licenses. `requirements-build.txt` pins build dependencies; the application runtime itself uses Python's standard library. Packaging requires this generated notices file; `--notices` can select another reviewed copy if needed.

For version 1.1.0, packaging produces:

```text
release/
  OmenPsuMonitor.exe
  OmenPsuMonitor-1.1.0-win64.zip
  OmenPsuMonitor-1.1.0-source.zip
  LICENSE.txt
  THIRD_PARTY_NOTICES.txt
  SHA256SUMS.txt
```

The Windows archive includes the executable, `README.txt`, `CHANGELOG.md`, the project license, and the full runtime licenses in `THIRD_PARTY_NOTICES.txt`. The standalone EXE is the same binary contained in that archive; its adjacent `LICENSE.txt` and `THIRD_PARTY_NOTICES.txt` supply the licenses separately. The source archive uses the explicit `release-files.json` allowlist and includes the project's [NOTICES.md](../NOTICES.md). Local `reports`, `data`, build caches/dependencies, existing distribution directories, and vendor inspection material are excluded.

Verify the generated checksum file against the standalone executable, both archives, and both license files:

```powershell
Get-FileHash release/OmenPsuMonitor.exe -Algorithm SHA256
Get-FileHash release/OmenPsuMonitor-1.1.0-win64.zip -Algorithm SHA256
Get-FileHash release/OmenPsuMonitor-1.1.0-source.zip -Algorithm SHA256
Get-FileHash release/LICENSE.txt -Algorithm SHA256
Get-FileHash release/THIRD_PARTY_NOTICES.txt -Algorithm SHA256
Get-Content release/SHA256SUMS.txt
```

## Review before publication

1. Keep `app_version.py`, `CHANGELOG.md`, `RELEASE_NOTES.md`, and both READMEs consistent with the intended release.
2. Run the offline tests and native build on a clean Windows environment.
3. Extract the Windows archive to a new folder and start its executable. Check normal data storage and explicit `--portable` storage.
4. With compatible hardware, check collection, disconnect/reconnect, pause/resume, CSV export, and shutdown. If HWiNFO publication is enabled, check group discovery, English labels, and removal of published values after normal shutdown.
5. Inspect archive contents and notices. Public tests must use synthetic fixtures. Diagnostic captures, screenshots containing personal information, vendor binaries, and private device paths must not be included.
6. Upload all six generated files to a GitHub draft release: the standalone EXE, both archives, both license files, and the checksum file. Describe support as experimental and limited to the verified device/revision.

Hardware checks require the actual PSU and are not performed by CI. Do not describe a passing offline test run as independent hardware calibration. The executable is unsigned unless the publisher separately adds a code-signing process.

## GitHub workflows

- [ci.yml](../.github/workflows/ci.yml) runs offline tests, builds the Windows executable, and uploads packaged assets as the `release-preview` Actions artifact.
- [release.yml](../.github/workflows/release.yml) builds release assets and creates or updates a **draft** GitHub release when manually dispatched or triggered by a version tag. It refuses to update an already published release.

Create a GitHub repository and upload the reviewed public source before using the workflows. The release job requires repository permissions to write release contents. Create and push a tag matching `app_version.py`, such as `v1.1.0`; pushing that tag triggers the release workflow.

For a manual run from the Actions page, enter that **existing remote tag** in the `tag` input. The manual workflow also checks out the named tag and requires it to equal `v` plus the checked-out application version. It does not create a missing tag or package arbitrary untagged branch changes. Release notes come from `RELEASE_NOTES.md` at the checked-out tag.

Review the generated draft, attached assets, checksums, and release notes in GitHub. Publish the draft only when those concrete artifacts are ready. Preparing source and local archives does not itself upload or publish a GitHub release.
