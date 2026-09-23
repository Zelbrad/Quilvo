@echo off
REM Run Quilvo from source in the background (no console). Needs Python 3.12+ and
REM the packages in requirements.txt. The packaged release is just Quilvo.exe.
start "" pyw "%~dp0flow.py"
