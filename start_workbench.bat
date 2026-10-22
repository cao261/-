@echo off
setlocal
chcp 65001 >nul
title MicroBench - 微电子与半导体科研复现工作台

REM ============================================================
REM  关键修复: 切到脚本所在目录 (修复 microbench 模块找不到)
REM ============================================================
cd /d "%~dp0"

echo ============================================================
echo      MicroBench V1.0 - 西交大微电子与半导体科研复现工作台
echo ============================================================
echo.

echo [1/4] 检查 Python 依赖 (fastapi/uvicorn/requests/pydantic)...
python -c "import fastapi, uvicorn, requests, pydantic" 2>nul
if %errorlevel% neq 0 (
    echo   缺少依赖,正在从清华镜像安装...
    python -m pip install -r "%~dp0requirements.txt" -i https://pypi.tuna.tsinghua.edu.cn/simple --quiet --disable-pip-version-check
    if %errorlevel% neq 0 (
        echo   清华镜像失败,改用官方源...
        python -m pip install -r "%~dp0requirements.txt" --quiet --disable-pip-version-check
    )
)

echo.
echo [2/4] 清理残留的 5000 端口进程...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":5000.*LISTENING"') do (
    echo   杀掉占用 5000 的进程 PID %%p
    taskkill /PID %%p /F >nul 2>&1
)

echo.
echo [3/4] 启动 uvicorn (生产模式,启动更快,不卡 watcher)...
echo   日志: logs\uvicorn.log
if not exist logs mkdir logs
start "MicroBench-Server" /MIN cmd /c "python -m uvicorn microbench.app:app --host 127.0.0.1 --port 5000 > logs\uvicorn.log 2>&1"

REM 顺手把离线阅读版刷新一次,免得平板上拿到的还是旧快照
start "MicroBench-Offline" /MIN cmd /c "python microbench\export_offline.py > logs\offline_export.log 2>&1"

echo.
echo [4/4] 轮询等 127.0.0.1:5000 就绪 (最多 30 秒)...
powershell -NoProfile -Command "$ErrorActionPreference='SilentlyContinue'; for ($i=1; $i -le 30; $i++) { if (Test-NetConnection -ComputerName 127.0.0.1 -Port 5000 -InformationLevel Quiet -WarningAction SilentlyContinue) { exit 0 }; Start-Sleep -Seconds 1 }; exit 1" >nul 2>&1

if %errorlevel% equ 0 (
    echo   ✅ 服务已就绪
    echo   自动打开浏览器...
    start "" "http://127.0.0.1:5000"
    exit /b 0
) else (
    echo   ⚠️ 30 秒内服务未就绪
    echo   查看日志: type logs\uvicorn.log
    echo   手动访问: http://127.0.0.1:5000
    pause
)
