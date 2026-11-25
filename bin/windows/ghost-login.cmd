@echo off
rem ghost-login -- connect ghost to DreamFort, with your account or an API key (ghost-login --help).
setlocal
set "GHOST_HOME=__GHOST_HOME__"
set "GHOST_ENGINE=__ENG__"
"__PYTHON__" -Es "%GHOST_HOME%\privacy\ghost_login.py" %*
exit /b %errorlevel%
