@echo off
rem The uninstall removes this file while it runs, so everything is on one line, which cmd has already read.
powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0ghost-uninstall.ps1" %* & exit /b
