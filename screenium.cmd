@echo off
rem Screenium launcher for Windows. Installed as the `screenium` command by
rem install.bat via a copy into %USERPROFILE%\.local\bin.
setlocal
set "PROJECT_DIR=%~dp0"

rem %~dp0 ends with a backslash; strip it.
if "%PROJECT_DIR:~-1%"=="\" set "PROJECT_DIR=%PROJECT_DIR:~0,-1%"

uv --directory "%PROJECT_DIR%" run python screenrecorder.py %*
exit /b %ERRORLEVEL%
