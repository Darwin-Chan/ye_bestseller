@echo off
rem Entry shim for the onboarding wizard.
rem PowerShell / cmd has no bash on PATH, so this finds Git Bash's bash.exe and runs
rem tools\setup_wizard.sh with the same arguments. Double-click it, or from PowerShell:
rem     .\tools\setup_wizard.cmd [--role-change]
rem ASCII only in this file: cmd.exe reads .cmd files in the OEM codepage.
chcp 65001 >nul
setlocal
set "CAND1=%ProgramFiles%\Git\bin\bash.exe"
set "CAND2=%ProgramFiles(x86)%\Git\bin\bash.exe"
set "CAND3=%LocalAppData%\Programs\Git\bin\bash.exe"
set "BASH="
for %%B in ("%CAND1%" "%CAND2%" "%CAND3%") do if not defined BASH if exist "%%~B" set "BASH=%%~B"
if not defined BASH for /f "delims=" %%P in ('where bash 2^>nul') do if not defined BASH set "BASH=%%P"
if not defined BASH (
  echo Cannot find Git Bash ^(bash.exe^).
  echo Install Git for Windows, or open a Git Bash window and run:  bash tools/setup_wizard.sh
  pause
  exit /b 2
)
cd /d "%~dp0.."
"%BASH%" tools/setup_wizard.sh %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" pause
exit /b %RC%
