# Project-local build dependencies; no global installation is required.
param([ValidateSet('dist', 'dist-update')][string] $OutputDirectory = 'dist')
$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
Push-Location -LiteralPath $projectRoot
try {
    python .\tools\create_app_icon.py
    if ($LASTEXITCODE -ne 0) { throw 'Icon generation failed.' }
    python .\tools\generate_build_metadata.py
    if ($LASTEXITCODE -ne 0) { throw 'Build inputs or runtime licenses are missing. Install requirements-build.txt into .build-deps.' }
    python -c "import sys; from pathlib import Path; sys.path.insert(0, '.build-deps'); from PyInstaller.__main__ import run; run(['--noconfirm','--onefile','--windowed','--noupx','--name','OmenPsuMonitor','--version-file',str(Path('build/version_info.txt').resolve()),'--icon',str(Path('assets/omen_psu.ico').resolve()),'--add-data',str(Path('assets').resolve())+':assets','--distpath',sys.argv[1],'--workpath','build/work','--specpath','build','omen_psu_app.py'])" $OutputDirectory
    if ($LASTEXITCODE -ne 0) { throw 'Executable build failed.' }
} finally {
    Pop-Location
}
