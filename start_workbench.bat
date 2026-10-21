@echo off
chcp 65001 >nul
setlocal EnableExtensions
title MicroBench Launcher

rem ============================================================
rem   MicroBench - 微电子与半导体科研复现工作台
rem   一键启动：启动本地服务 -> 等待就绪 -> 自动打开浏览器
rem ============================================================

rem [关键] 无论从快捷方式 / 资源管理器 / 任意目录启动，
rem        都先把工作目录切到项目根目录，保证 microbench 包能被正确导入。
cd /d "%~dp0"

set "APP_DIR=%~dp0"
if "%APP_DIR:~-1%"=="\" set "APP_DIR=%APP_DIR:~0,-1%"
set "URL=http://127.0.0.1:5000"

rem [关键] 端口探测必须精确：Windows 上 127.0.0.1:50000 等相邻端口很常见
rem        例如 Zotero 插件就占用 50000，用 findstr 子串匹配会误判成"已启动"。
rem        这里直接请求首页并校验页面特征串，确保"是我们自己的服务"。
set "PROBE=python -c "import urllib.request as u,sys;o=u.build_opener(u.ProxyHandler({}));b=o.open('http://127.0.0.1:5000/',timeout=3).read();sys.exit(0 if b'MicroBench' in b else 1)" 2>nul"

rem 用 ping 代替 timeout 做等待，不依赖标准输入句柄，
rem 这样从快捷方式、计划任务、脚本里调用都能正常等待。
set "SLEEP=ping -n 2 127.0.0.1 >nul"

echo.
echo ============================================================
echo      MicroBench - 西交大微电子与半导体科研复现工作台 V1.0
echo ============================================================
echo.

rem ------------------------------------------------------------
echo [1/3] 检查 Python 环境与依赖...
python -c "import fastapi, uvicorn, requests, pydantic, multipart" 2>nul
if errorlevel 1 goto :INSTALL_DEPS
echo     依赖就绪。
goto :AFTER_DEPS

:INSTALL_DEPS
echo     依赖缺失，正在安装 fastapi / uvicorn / requests / python-multipart ...
pip install -r "%APP_DIR%\requirements.txt" -i https://pypi.tuna.tsinghua.edu.cn/simple --quiet --disable-pip-version-check
if errorlevel 1 goto :INSTALL_DEPS_FALLBACK
goto :DEPS_DONE

:INSTALL_DEPS_FALLBACK
echo     清华镜像源不可用，改用官方源 ...
pip install -r "%APP_DIR%\requirements.txt" --quiet --disable-pip-version-check
if errorlevel 1 goto :DEPS_FAILED
goto :DEPS_DONE

:DEPS_FAILED
echo     [X] 依赖安装失败。请手动执行下面这行命令，然后重新双击本快捷方式：
echo         pip install -r "%APP_DIR%\requirements.txt"
pause
exit /b 1

:DEPS_DONE
echo     依赖安装完成。

:AFTER_DEPS
echo.

rem ------------------------------------------------------------
echo [2/3] 检查服务是否已在运行...
%PROBE%
if not errorlevel 1 goto :ALREADY

echo     正在启动本地科研工作台服务 %URL% ...
rem 服务端单独开一个窗口：日志可见，Ctrl+C 或关闭该窗口即可停止。
start "MicroBench Server" /D "%APP_DIR%" cmd /k python -m uvicorn microbench.app:app --host 127.0.0.1 --port 5000 --reload

echo [3/3] 等待服务就绪...
set /a TRIES=0
:WAIT_LOOP
%SLEEP%
set /a TRIES+=1
%PROBE%
if not errorlevel 1 goto :READY
if %TRIES% lss 45 goto :WAIT_LOOP

echo.
echo     [X] 服务在 45 秒内未就绪。
echo     请切换到 "MicroBench Server" 窗口查看具体报错，通常是依赖缺失或端口被占用。
%SLEEP%
exit /b 1

:READY
echo     服务已就绪，正在打开浏览器 ...
start "" "%URL%"
goto :DONE

:ALREADY
echo     检测到工作台已在运行，直接打开浏览器。
start "" "%URL%"

:DONE
echo.
echo   ------------------------------------------------------------
echo     工作台地址 : %URL%
echo     停止服务   : 切换到 "MicroBench Server" 窗口按 Ctrl+C，或关闭该窗口。
echo   ------------------------------------------------------------
echo.
%SLEEP%
exit /b 0
