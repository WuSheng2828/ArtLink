@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ====================================
echo   ArtLink 词库编辑器
echo   端口 19876
echo ====================================

:: 检测 Python 环境（优先便携）
set PYTHON=python
if exist "%~dp0Python311\python.exe" (
    echo [环境] 使用便携 Python 环境
    set PYTHON=%~dp0Python311\python.exe
) else (
    python --version >nul 2>&1
    if %errorlevel% neq 0 (
        echo ❌ 未找到任何 Python 环境
        pause
        exit /b 1
    )
    echo [环境] 使用系统 Python 环境
)

echo 正在启动编辑器...
start /b "" %PYTHON% wordbank_editor.py
timeout /t 2 /nobreak >nul
start http://127.0.0.1:19876
echo 浏览器已打开
pause
