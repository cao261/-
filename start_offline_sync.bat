@echo off
setlocal
chcp 65001 >nul
title MicroBench - 离线阅读版同步器

REM ============================================================
REM  持续把 vault 导出成单文件离线阅读版。
REM
REM  为什么需要它:
REM    实时版(LAN)要求电脑开着、平板和电脑在同一 Wi-Fi。
REM    离线版是一个「快照」—— 如果出门前忘了重新导出,
REM    平板上看到的还是旧内容。这个脚本让快照自动保持最新。
REM
REM  用法:
REM    双击本文件                -> 导出到工作台根目录
REM    拖一个文件夹到本文件上    -> 导出到那个文件夹(推荐用云盘目录)
REM
REM  导出到云盘目录 (OneDrive / 坚果云 / iCloud Drive) 时,
REM  平板上直接用对应的云盘 App 打开那个 HTML 即可,
REM  不用带电脑, 内容也永远是最新的。
REM ============================================================

cd /d "%~dp0"

set "OUT_DIR=%~dp0"
if not "%~1"=="" set "OUT_DIR=%~1"

if "%OUT_DIR:~-1%"=="\" set "OUT_DIR=%OUT_DIR:~0,-1%"
if not exist "%OUT_DIR%" (
    echo [X] 目标目录不存在: %OUT_DIR%
    pause
    exit /b 1
)

echo ============================================================
echo      MicroBench - 离线阅读版同步器
echo ============================================================
echo.
echo   输出目录: %OUT_DIR%
echo   文件名  : MicroBench_离线阅读版.html
echo.

REM --- 探测常见云盘目录,方便直接选 ---------------------------------
set "FOUND="
for %%D in (
    "%USERPROFILE%\OneDrive"
    "%USERPROFILE%\OneDrive - Personal"
    "%USERPROFILE%\Nutstore"
    "%USERPROFILE%\坚果云"
    "%USERPROFILE%\iCloudDrive"
    "%USERPROFILE%\BaiduNetdiskDownload"
) do (
    if exist %%D (
        set "FOUND=!FOUND!  %%~D
"
    )
)
if defined FOUND (
    echo   发现以下云盘目录,你可以直接双击本文件并把其中一个拖到本文件上:
    echo!FOUND!
    echo   导出到云盘目录后,平板用云盘 App 打开该 HTML 即可,
    echo   内容会自动保持最新。
    echo.
)

REM --- 先立刻导出一份,让用户马上就有东西可用 -----------------------
echo [1/3] 首次导出 ...
python microbench\export_offline.py "%OUT_DIR%\MicroBench_离线阅读版.html"
if %errorlevel% neq 0 (
    echo [X] 导出失败
    pause
    exit /b 1
)

echo.
echo [2/3] 启动监视 (每 60 秒检查一次, 有改动就重新导出)...
echo       保持本窗口开着即可。Ctrl+C 退出。
echo.
python microbench\export_offline.py "%OUT_DIR%\MicroBench_离线阅读版.html" --watch 60

echo.
echo [3/3] 已退出。
pause
