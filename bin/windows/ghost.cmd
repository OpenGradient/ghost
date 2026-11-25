@echo off
rem ghost -- the command. privacy\launch.py does the work; `update` and `uninstall` don't go through it
rem because they replace or remove the very Python it runs on (and, without `call`, don't come back here).
setlocal
set "GHOST_HOME=__GHOST_HOME__"
set "GHOST_ENGINE=__ENG__"
if /I "%~1"=="update" goto update
if /I "%~1"=="uninstall" goto uninstall
"__PYTHON__" -Es "%GHOST_HOME%\privacy\launch.py" %*
exit /b %errorlevel%
:update
"%~dp0ghost-update.cmd"
:uninstall
"%~dp0ghost-uninstall.cmd" %*
