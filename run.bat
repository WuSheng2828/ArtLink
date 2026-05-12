@echo off
chcp 65001 >nul
cd /d "%~dp0"

:: ========================================
::  ArtLink v1.0.0 启动脚本
::  自动检测便携/系统 Python 环境
:: ========================================

:: 1. 检测便携 Python 是否存在
if exist "%~dp0Python311\python.exe" (
    echo [环境] 使用便携 Python 环境
    set PYTHON=%~dp0Python311\python.exe
) else (
    :: 2. 检测系统 Python 是否可用
    python --version >nul 2>&1
    if %errorlevel% equ 0 (
        echo [环境] 使用系统 Python 环境
        set PYTHON=python
    ) else (
        echo ========================================
        echo   未找到任何 Python 环境！
        echo   请安装 Python 3.12 或下载便携环境包
        echo   将便携环境解压到 Python311 文件夹即可
        echo ========================================
        pause
        exit /b 1
    )
)

echo 正在启动 ArtLink...
start /b "" %PYTHON% main.py
timeout /t 3 /nobreak >nul
start http://127.0.0.1:5000
echo 浏览器已打开
pause