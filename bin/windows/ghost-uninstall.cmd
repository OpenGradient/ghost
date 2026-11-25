@echo off
rem The uninstall removes this file, so leave it first: (goto) ends the batch, and the rest of the line still runs.
(goto) 2>nul & powershell -ExecutionPolicy Bypass -NoProfile -File "%~dp0ghost-uninstall.ps1" %*
