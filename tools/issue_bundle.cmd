@echo off
rem Entry shim for the issue-bundle tool (record a runtime problem and pack it).
rem ASCII only in this file: cmd.exe reads .cmd files in the OEM codepage -- every
rem Chinese prompt comes from the tool itself (--prompt).
rem Double-click: it asks for a one-line title, collects the bundle, opens REPORT.md,
rem then packs a .zip next to it.  Arguments are passed through (e.g. --list).
chcp 65001 >nul
setlocal
cd /d "%~dp0.."

set "PY="
if defined BESTSELLER_PYTHON set "PY=%BESTSELLER_PYTHON%"
if not defined PY for /f "delims=" %%P in ('where python 2^>nul') do if not defined PY set "PY=%%P"
if not defined PY for /f "delims=" %%P in ('where python3 2^>nul') do if not defined PY set "PY=%%P"
if not defined PY (
  echo Cannot find python on PATH. Install Python, or set BESTSELLER_PYTHON.
  pause
  exit /b 2
)

if "%~1"=="" (
  "%PY%" tools\issue_bundle.py --prompt
) else (
  "%PY%" tools\issue_bundle.py %*
)
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" pause
exit /b %RC%
