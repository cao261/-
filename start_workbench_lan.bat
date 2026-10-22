@echo off
setlocal enabledelayedexpansion
chcp 65001 >nul
title MicroBench - 平板 / 局域网模式

REM ============================================================
REM  MicroBench LAN 模式
REM  把工作台开到 0.0.0.0,让同一 Wi-Fi 下的平板(iPad/Android)
REM  也能访问。访问需要口令,口令会显示在本窗口,并保存在
REM  logs\lan_token.txt 中(重启后不变,平板只需配对一次)。
REM ============================================================

cd /d "%~dp0"

echo ============================================================
echo      MicroBench - 平板 / 局域网模式
echo ============================================================
echo.

echo [1/6] 检查 Python 依赖 (fastapi/uvicorn/requests/pydantic)...
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
echo [2/6] 清理残留的 5000 端口进程...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":5000.*LISTENING"') do (
    echo   杀掉占用 5000 的进程 PID %%p
    taskkill /PID %%p /F >nul 2>&1
)

echo.
echo [3/6] 准备访问口令与局域网地址...
if not exist logs mkdir logs
python -m microbench.lan 5000 --emit-env "%~dp0logs\lan_env.cmd" >nul
if not exist "%~dp0logs\lan_env.cmd" (
    echo   [X] 无法生成口令,请手动运行: python -m microbench.lan 5000
    pause
    exit /b 1
)
call "%~dp0logs\lan_env.cmd"
set "MB_LAN_TOKEN=%LAN_TOKEN%"

echo.
echo [4/6] 检查 Windows 防火墙是否放行 5000 端口...
netsh advfirewall firewall show rule name="MicroBench 5000" >nul 2>&1
if %errorlevel% neq 0 (
    echo   [!] 没有找到 MicroBench 5000 的放行规则。
    echo       平板连不上时,多半就是被防火墙拦住了。
    echo       需要的话,请以【管理员身份】打开一个命令行,执行:
    echo.
    echo         netsh advfirewall firewall add rule name="MicroBench 5000" ^
    echo           dir=in action=allow protocol=TCP localport=5000 profile=private
    echo.
    echo       ^(或者在第一次启动时的 Windows 弹窗里点「允许访问」^)
    echo.
) else (
    echo   [OK] 防火墙规则已存在
)

echo.
echo [5/6] 启动 uvicorn (监听 0.0.0.0:5000)...
echo   日志: logs\uvicorn.log
start "MicroBench-LAN" /MIN cmd /c "python -m uvicorn microbench.app:app --host 0.0.0.0 --port 5000 > logs\uvicorn.log 2>&1"

REM 顺手把离线阅读版刷新一次,免得平板上拿到的还是旧快照
start "MicroBench-Offline" /MIN cmd /c "python microbench\export_offline.py > logs\offline_export.log 2>&1"

echo.
echo [6/6] 等待服务就绪 (最多 30 秒)...
powershell -NoProfile -Command "$ErrorActionPreference='SilentlyContinue'; for ($i=1; $i -le 30; $i++) { if (Test-NetConnection -ComputerName 127.0.0.1 -Port 5000 -InformationLevel Quiet -WarningAction SilentlyContinue) { exit 0 }; Start-Sleep -Seconds 1 }; exit 1" >nul 2>&1

if %errorlevel% equ 0 (
    echo   [OK] 服务已就绪
    echo.
    echo ============================================================
    echo   在平板上打开下面任意一个地址:
    echo.
    echo   [推荐] 一键配对 (自动填入口令):
    echo      %PAIRING_URL%
    echo.
    echo   [或] 手动输入:
    echo      http://%LAN_IP%:5000/
    echo      口令: %LAN_TOKEN%
    echo.
    echo   提示: 平板需与本机连同一个 Wi-Fi。
    echo         iPad 用 Safari 打开后点分享按钮 -> 添加到主屏幕,
    echo         即可像 App 一样全屏使用。
    echo ============================================================
    echo.
    start "" "http://127.0.0.1:5000"
    exit /b 0
) else (
    echo   [!] 30 秒内服务未就绪
    echo   查看日志: type logs\uvicorn.log
    pause
)
