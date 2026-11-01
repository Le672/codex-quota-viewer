@echo off
chcp 65001 >nul
setlocal EnableDelayedExpansion
cd /d "%~dp0"

echo ============================================================
echo   Codex 额度查看器 - 一键打包
echo ============================================================
echo.

rem ---------- 1. 找 Python ----------
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (
  where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  echo [错误] 没有找到 Python。
  echo        请先安装 Python 3.10+ ^(https://www.python.org/downloads/^)，
  echo        安装时务必勾选 "Add python.exe to PATH"。
  echo.
  pause
  exit /b 1
)
echo [1/6] 使用 Python: %PY%
%PY% -c "import sys;print('     版本', sys.version.split()[0])" || (echo [错误] Python 无法运行 & pause & exit /b 1)

rem ---------- 2. 安装 Qt 运行库 ----------
echo [2/6] 检查 Qt 运行库...
%PY% -c "from PySide6.QtWidgets import QApplication; import psutil" >nul 2>nul
if errorlevel 1 (
  %PY% -m pip install -r requirements.txt
  if errorlevel 1 (
    echo [错误] Qt 运行库安装失败。
    pause
    exit /b 1
  )
)
%PY% -c "from PySide6.QtWidgets import QApplication; import psutil" || (pause & exit /b 1)
echo       Qt 可用

rem ---------- 3. 离线自检 ----------
echo [3/6] 运行离线自检...
%PY% run.py --selftest
if errorlevel 1 (
  echo.
  echo [错误] 自检存在失败项，停止打包，请先修复上面的 FAIL 项。
  pause
  exit /b 1
)

rem ---------- 4. 准备图标 ----------
echo [4/6] 准备图标...
if not exist "icon.ico" (
  %PY% tools\make_icon.py "icon.ico" || echo [警告] 图标生成失败，将使用默认图标
) else (
  echo       icon.ico 已存在，跳过生成
)

rem ---------- 5. 准备 PyInstaller ----------
echo [5/6] 检查 PyInstaller...
%PY% -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
  echo       未安装，正在通过 pip 安装 PyInstaller...
  %PY% -m pip install --upgrade pip
  %PY% -m pip install "pyinstaller>=6.0"
  %PY% -m PyInstaller --version >nul 2>nul
  if errorlevel 1 (
    echo [错误] PyInstaller 安装失败。
    echo        若处于内网环境，请手动执行：
    echo            %PY% -m pip install pyinstaller -i https://pypi.tuna.tsinghua.edu.cn/simple
    pause
    exit /b 1
  )
)
for /f "delims=" %%v in ('%PY% -m PyInstaller --version 2^>nul') do echo       PyInstaller %%v

rem ---------- 6. 打包 ----------
echo [6/6] 开始打包（首次约需 1-3 分钟）...
echo.
%PY% -m PyInstaller --noconfirm --clean CodexQuotaViewer.spec
if errorlevel 1 (
  echo.
  echo [错误] 打包失败，请把上面的错误信息发给我。
  pause
  exit /b 1
)

set "EXE=dist\Codex额度查看器.exe"
if not exist "%EXE%" (
  echo [错误] 没有找到产物 %EXE%
  pause
  exit /b 1
)
if not exist "dist\Codex额度联动.exe" (
  echo [错误] 没有找到联动程序。
  pause
  exit /b 1
)
echo.
echo ============================================================
echo   打包完成: %EXE%
echo ============================================================

rem ---------- 验收：自检 + JSON 数据 ----------
echo.
echo 正在验收打包产物...
"%EXE%" --selftest >nul 2>nul
echo      自检退出码: %errorlevel%

echo.
echo 验收输出（来自 %%APPDATA%%\CodexQuotaViewer\viewer.log 末尾）:
powershell -NoProfile -Command "if (Test-Path \"$env:APPDATA\CodexQuotaViewer\viewer.log\") { Get-Content \"$env:APPDATA\CodexQuotaViewer\viewer.log\" -Tail 30 }" 2>nul

echo.
echo 双击 %EXE% 即可运行；右键托盘图标可刷新、设置、退出。
echo.
pause
