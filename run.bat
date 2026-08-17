@echo off
rem  Photo Merge - Windows launcher
rem  Double-click this file. It checks for a new version, asks before updating,
rem  then starts the app and opens your browser.

setlocal
cd /d "%~dp0"
title Photo Merge

where uv >nul 2>&1
if errorlevel 1 goto nouv

uv run --no-project --python 3.12 launch.py
goto done

:nouv
echo.
echo   uv is not installed.
echo.
echo   Open PowerShell and paste this line, then run this file again:
echo.
echo      irm https://astral.sh/uv/install.ps1 ^| iex
echo.
echo   uv installs Python and the packages this app needs, so it is the
echo   only thing you have to set up.
echo.

:done
echo.
echo   The app has stopped. You can close this window.
pause >nul
