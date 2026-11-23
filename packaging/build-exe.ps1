# Builds "MOSS Image Studio.exe" (one self-contained file) into the dist folder with PyInstaller.
# Run from anywhere:  powershell -ExecutionPolicy Bypass -File .\packaging\build-exe.ps1
# Needs Python 3.10+ on PATH. Uses its own build environment (packaging\.venv-build).

$ErrorActionPreference = 'Stop'
$repo  = Split-Path -Parent $PSScriptRoot
$tools = Join-Path $repo 'moss-image-pc\tools'
$venv  = Join-Path $PSScriptRoot '.venv-build'
$py    = Join-Path $venv 'Scripts\python.exe'

if (-not (Test-Path $py)) {
    Write-Host 'Creating the build environment...'
    python -m venv $venv
}
& $py -m pip install --disable-pip-version-check -q -r (Join-Path $tools 'requirements.txt') 'pyinstaller==6.22.3'
if ($LASTEXITCODE) { throw 'Installing the build tools failed.' }

$icon = Join-Path $tools 'moss.ico'
& $py -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name 'MOSS Image Studio' `
    --icon $icon `
    --add-data "$icon;." `
    --version-file (Join-Path $PSScriptRoot 'version_info.txt') `
    --paths $tools `
    --distpath (Join-Path $repo 'dist') `
    --workpath (Join-Path $PSScriptRoot 'build') `
    --specpath (Join-Path $PSScriptRoot 'build') `
    (Join-Path $tools 'moss_gui.py')
if ($LASTEXITCODE) { throw 'PyInstaller failed.' }

$exe = Join-Path $repo 'dist\MOSS Image Studio.exe'
Write-Host ''
Write-Host ("Built {0} ({1:N1} MB)" -f $exe, ((Get-Item $exe).Length / 1MB)) -ForegroundColor Green
