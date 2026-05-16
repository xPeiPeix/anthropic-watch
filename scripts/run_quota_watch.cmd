@echo off
rem Anthropic Watch quota monitor - manual / scheduled entry point.
rem Resolves Python and the watcher script relative to this file so it works
rem regardless of the working directory.
rem Python launcher precedence: %AW_PYTHON% env > AW_PYTHON in
rem aw_config.local.env > python on PATH.
setlocal EnableExtensions
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

set "PY=%AW_PYTHON%"
if not defined PY (
  for /f "usebackq tokens=1,* delims==" %%A in ("%~dp0aw_config.local.env") do (
    if /i "%%A"=="AW_PYTHON" set "PY=%%B"
  )
)
if not defined PY set "PY=python"
if not exist "%PY%" set "PY=python"

"%PY%" "%~dp0anthropic_quota_watch.py" %*
endlocal
