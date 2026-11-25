@echo off
cd /d "%~dp0"
set "PY="
set "PYW="
if exist "%~dp0_工具\py\Scripts\python.exe" (
  set "PY=%~dp0_工具\py\Scripts\python.exe"
  set "PYW=%~dp0_工具\py\Scripts\pythonw.exe"
)
if not defined PY if exist "%~dp0.venv\Scripts\python.exe" (
  set "PY=%~dp0.venv\Scripts\python.exe"
  set "PYW=%~dp0.venv\Scripts\pythonw.exe"
)
if not defined PY (
  where py >nul 2>nul
  if not errorlevel 1 set "PY=py"
)
if not defined PY (
  where python >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  echo [X] 没有找到 Python 3，请先安装 Python 3.10 或更高版本，或按 README 建好虚拟环境。
  pause
  exit /b 1
)
if exist "%PYW%" (
  start "" "%PYW%" "%~dp0库服务.py" gui %*
  exit /b 0
)
"%PY%" "%~dp0库服务.py" gui %*
if errorlevel 1 (
  echo.
  echo [X] 启动失败，请看上面的报错信息。
  pause
)