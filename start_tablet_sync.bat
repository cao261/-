@echo off
setlocal
chcp 65001 >nul
title MicroBench - 平板工作台同步器

REM ============================================================
REM  把「平板工作台」导出到云盘目录, 并把平板导回来的变更包
REM  合并进 vault。
REM
REM  用法:
REM    双击本文件            -> 用自动探测到的云盘目录
REM    拖一个文件夹到本文件上 -> 用那个文件夹
REM
REM  两种部署方式:
REM    A. 平板直连云盘里的 vault 目录 (推荐)
REM       先跑:  python microbench\move_vault_to_cloud.py --apply
REM       之后平板直接读写云盘目录, PC 不用开着, 一切自动。
REM
REM    B. 平板用变更包回传
REM       平板设置里点「导出变更包」, 把 json 放进本脚本用的目录,
REM       本脚本会自动合并回 vault。需要本窗口一直开着。
REM ============================================================

cd /d "%~dp0"

set "OUT_DIR="
if not "%~1"=="" set "OUT_DIR=%~1"

if "%OUT_DIR%"=="" (
    for /f "usebackq delims=" %%d in (`python -c "import sys;sys.path.insert(0,'.');from microbench.export_offline import detect_cloud_dir;d=detect_cloud_dir();print(str(d)+'/MicroBench平板' if d else '')"`) do set "OUT_DIR=%%d"
)

if "%OUT_DIR%"=="" (
    echo [X] 没有探测到云盘目录。
    echo     把一个文件夹拖到本文件上即可指定输出位置。
    pause
    exit /b 1
)

if "%OUT_DIR:~-1%"=="\" set "OUT_DIR=%OUT_DIR:~0,-1%"
if not exist "%OUT_DIR%" mkdir "%OUT_DIR%"

echo ============================================================
echo      MicroBench - 平板工作台同步器
echo ============================================================
echo.
echo   输出目录: %OUT_DIR%
echo.

REM --- 导出平板工作台 --------------------------------------------------
echo [1/3] 导出平板工作台 ...
python microbench\export_offline.py --tablet "%OUT_DIR%\MicroBench_平板工作台.html"
if %errorlevel% neq 0 (
    echo [X] 导出失败
    pause
    exit /b 1
)

echo.
echo [2/3] 检查 vault 是否已经在云盘里 ...
python -c "import sys;sys.path.insert(0,'.');from microbench.export_offline import detect_cloud_dir;from pathlib import Path;import os;d=detect_cloud_dir();t=(d/'MicroBench工作台') if d else None;print('IN_CLOUD' if t and t.is_dir() else 'NOT_IN_CLOUD')" > "%~dp0logs\vault_cloud_state.txt" 2>nul
set /p VCSTATE=<"%~dp0logs\vault_cloud_state.txt"
if /i "%VCSTATE%"=="IN_CLOUD" (
    echo   [OK] vault 已经在云盘里了 —— 平板可以直接读写那个目录,
    echo         不需要本窗口开着。
    echo.
    echo   平板上: 坚果云 App 等目录同步下来 -> Chrome 打开
    echo           %OUT_DIR%\MicroBench_平板工作台.html
    echo           设置 -> 连接文件夹 -> 选「我的坚果云\MicroBench工作台」
    echo.
) else (
    echo   [!] vault 还在本机。
    echo       想让平板直连云盘 (推荐, PC 不用开着), 跑一次:
    echo         python microbench\move_vault_to_cloud.py --apply
    echo.
    echo       不搬也行 —— 平板可以用「导出变更包」回传,
    echo       本窗口会负责把它合并回 vault。
    echo.
)

echo [3/3] 监视变更包 (每 20 秒检查一次, 合并回 vault)...
echo       Ctrl+C 退出。
echo.
python microbench\merge_changes.py "%OUT_DIR%" --watch 20

echo.
echo 已退出。
pause
