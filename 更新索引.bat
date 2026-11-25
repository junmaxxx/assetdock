@echo off
cd /d "%~dp0"
set "PY="
if exist "%~dp0_工具\py\Scripts\python.exe" set "PY=%~dp0_工具\py\Scripts\python.exe"
if not defined PY if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not defined PY (
  where py >nul 2>nul
  if not errorlevel 1 set "PY=py"
)
if not defined PY (
  where python >nul 2>nul
  if not errorlevel 1 set "PY=python"
)
if not defined PY (
  echo [X] 没有找到 Python 3，请先安装 Python 3.10 或更高版本。
  pause
  exit /b 1
)
echo 正在更新索引（只读扫描，不会改动任何文件）……
"%PY%" "%~dp0库服务.py" scan %*
echo.
echo 完成，可以关闭本窗口。
pause