# Build Quilvo.exe and a release zip.   Usage:  .\build.ps1
# Creates .venv-build on first run, then produces dist\Quilvo\Quilvo.exe and dist\Quilvo-<version>-win64.zip
# Smoke-test the result with:  dist\Quilvo\Quilvo.exe --selftest   (then check %APPDATA%\Quilvo\flow.log)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path .venv-build)) {
    py -3 -m venv .venv-build
    .\.venv-build\Scripts\python.exe -m pip install --upgrade pip
    .\.venv-build\Scripts\python.exe -m pip install -r requirements-lock.txt   # exact tested versions
}

.\.venv-build\Scripts\pyinstaller.exe flow.spec --noconfirm --clean

$version = (Select-String -Path flow.py -Pattern '^__version__ = "(.+)"').Matches[0].Groups[1].Value
$zip = "dist\Quilvo-$version-win64.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path dist\Quilvo -DestinationPath $zip
Write-Host "Built dist\Quilvo\Quilvo.exe and $zip"
