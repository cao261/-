@echo off
chcp 65001 >nul
setlocal EnableExtensions
title MicroBench Desktop

rem ============================================================
rem   MicroBench 桌面应用启动器 —— 独立应用窗口，不走浏览器
rem   用 pythonw 启动：不弹黑色控制台窗口，日志写到 logs\desktop.log
rem ============================================================

cd /d "%~dp0"

rem [关键] 优先用 pythonw.exe（无控制台）；找不到再退回 python.exe
set "PYW="
for %%I in (pythonw.exe) do if not defined PYW if exist "%%~$PATH:I" set "PYW=%%~$PATH:I"
if not defined PYW set "PYW=python.exe"

rem [关键] start "" 让启动器独立运行，本窗口立刻关闭，
rem        用户只看到应用窗口，看不到 cmd 黑窗
start "MicroBench App" /D "%~dp0" "%PYW%" "%~dp0desktop.py" %*
exit /b 0
