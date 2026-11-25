@echo off
rem The update rewrites this file while it runs, so everything is on one line, which cmd has already read.
powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0ghost-update.ps1" & exit /b
