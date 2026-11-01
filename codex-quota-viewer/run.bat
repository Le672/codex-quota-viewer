@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (
  where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  echo [错误] 没有找到 Python，请先安装 Python 3.10+ 并勾选 Add to PATH。
  pause
  exit /b 1
)

%PY% -c "import PySide6.QtWidgets" >nul 2>nul
if errorlevel 1 (
  %PY% -m pip install -r requirements.txt || (pause & exit /b 1)
)
%PY% run.py %*
if errorlevel 1 (
  echo.
  echo 程序退出，错误码 %errorlevel%
  echo 日志位置: %APPDATA%\CodexQuotaViewer\viewer.log
  pause
)
