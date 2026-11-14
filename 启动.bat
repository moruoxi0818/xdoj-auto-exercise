@echo off
chcp 936 >nul
setlocal enabledelayedexpansion
title XDOJ 自动刷题
cd /d "%~dp0"

echo.
echo   ==================================================
echo      XDOJ 自动刷题 - 一键启动
echo   ==================================================
echo.

rem ---------------- 1. 查找 Python ----------------
set "PYCMD="
py -3 -V >nul 2>nul
if not errorlevel 1 set "PYCMD=py -3"
if not defined PYCMD (
    python -V >nul 2>nul
    if not errorlevel 1 set "PYCMD=python"
)
if not defined PYCMD (
    for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
        if not defined PYCMD if exist "%%~fD\python.exe" set "PYCMD=%%~fD\python.exe"
    )
)
if not defined PYCMD (
    for /d %%D in ("C:\Python3*") do (
        if not defined PYCMD if exist "%%~fD\python.exe" set "PYCMD=%%~fD\python.exe"
    )
)

if not defined PYCMD (
    echo   [X] 没有检测到 Python
    echo.
    echo       请先安装 Python 3.9 或更高版本：
    echo         https://www.python.org/downloads/
    echo       安装时请务必勾选 "Add python.exe to PATH"，
    echo       装好后再双击本文件。
    echo.
    start "" "https://www.python.org/downloads/"
    pause
    exit /b 1
)
echo   [OK] 已找到 Python

rem ---------------- 2. 准备运行环境 ----------------
rem 已有环境是否还能用（从别的电脑拷过来的 venv 会失效，这里自动重建）
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sys" >nul 2>nul
    if errorlevel 1 (
        echo   旧的运行环境不可用，正在重建...
        rmdir /s /q ".venv"
    )
)

if exist ".venv\Scripts\python.exe" goto :CHECKDEP
echo.
echo   首次运行，正在创建运行环境（只需一次）...
%PYCMD% -m venv .venv
if not exist ".venv\Scripts\python.exe" (
    echo   [X] 创建运行环境失败，请确认 Python 安装完整。
    pause
    exit /b 1
)

:CHECKDEP
".venv\Scripts\python.exe" -c "import fastapi,uvicorn,requests,fitz" >nul 2>nul
if not errorlevel 1 goto :DEPSOK

echo   正在安装依赖，第一次会慢一点，请耐心等待...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q --timeout 60 --retries 2 -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple >nul 2>nul
".venv\Scripts\python.exe" -c "import fastapi,uvicorn,requests,fitz" >nul 2>nul
if not errorlevel 1 goto :DEPSOK

echo   镜像源不通，正在换官方源重试（可能比较慢）...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check --timeout 60 --retries 2 -r requirements.txt >install.log 2>&1
".venv\Scripts\python.exe" -c "import fastapi,uvicorn,requests,fitz" >nul 2>nul
if not errorlevel 1 goto :DEPSOK2

echo.
echo   [X] 依赖安装失败，请检查网络后重新双击本文件。
echo       详细日志：%CD%\install.log
pause
exit /b 1

:DEPSOK2
if exist install.log del /q install.log >nul 2>nul
:DEPSOK
echo   [OK] 运行环境就绪

rem ---------------- 3. 检查 C++ 编译器 ----------------
g++ --version >nul 2>nul
if errorlevel 1 (
    echo   [!] 没有检测到 g++ 编译器
    echo       程序会先用本地编译器验证生成的代码，缺少时会编译失败。
    echo       建议安装 MinGW-w64 或 w64devkit，并把它的 bin 目录加入 PATH。
    echo.
) else (
    echo   [OK] 已找到 C++ 编译器
)

rem ---------------- 4. 启动服务 ----------------
if exist "port.txt" del /q "port.txt" >nul 2>nul
echo.
echo   正在启动服务...
start "" /b ".venv\Scripts\python.exe" server.py

set /a TRIES=0
:WAITPORT
if exist "port.txt" goto :GOTPORT
set /a TRIES+=1
if !TRIES! GEQ 45 goto :NOPORT
ping -n 2 127.0.0.1 >nul
goto :WAITPORT

:GOTPORT
set "PORT="
set /p PORT=<port.txt
if not defined PORT set "PORT=8000"

echo   [OK] 服务已启动
echo.
echo   --------------------------------------------------
echo     地址：http://127.0.0.1:!PORT!
echo.
echo     浏览器里会弹出配置窗口，填自己的
echo       学号 / 密码 / AI API Key
echo     保存后即可填写考试编号和题目编号开始刷题。
echo.
echo     注意：不要关闭这个黑窗口，关闭就停止运行。
echo   --------------------------------------------------
echo.
start "" "http://127.0.0.1:!PORT!"
pause
exit /b 0

:NOPORT
echo   [X] 服务启动失败，请看上方报错信息。
echo       常见原因：依赖没装好，或 8000-8030 端口全被占用。
pause
exit /b 1
