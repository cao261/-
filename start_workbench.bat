@echo off
chcp 65001 >nul
title MicroBench - 微电子与半导体科研复现工作台

echo ============================================================
echo      MicroBench - 西交大微电子与半导体科研复现工作台 V1.0
echo ============================================================
echo [1/3] 检查 Python 环境与依赖...
python -c "import fastapi, uvicorn, requests, pydantic" 2>nul
if %errorlevel% neq 0 (
    echo [*] 首次运行，正在极速安装轻量依赖库 (fastapi, uvicorn, requests)...
    pip install -r "%~dp0requirements.txt" -i https://pypi.tuna.tsinghua.edu.cn/simple --quiet --disable-pip-version-check
    if %errorlevel% neq 0 (
        echo [!] 清华镜像源受限，尝试官方源安装...
        pip install -r "%~dp0requirements.txt" --quiet --disable-pip-version-check
    )
)

echo [2/3] 正在启动本地科研工作台服务 (http://127.0.0.1:5000)...
echo [3/3] 自动拉起默认浏览器...
timeout /t 1 /nobreak >nul
start http://127.0.0.1:5000

python -m uvicorn microbench.app:app --host 127.0.0.1 --port 5000 --reload
pause
