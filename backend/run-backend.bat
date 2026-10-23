@echo off
chcp 65001 >nul
title 文献速读 Agent - 后端

REM 后端独立启动脚本：无论从哪里被调用，都先切到本脚本所在目录
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [后端] 未找到虚拟环境 .venv\Scripts\python.exe
    echo [后端] 请先在项目根目录运行 start-agent.bat 完成环境准备。
    echo.
    pause
    exit /b 1
)

echo [后端] 正在启动 uvicorn: http://127.0.0.1:8321  (Ctrl+C 或关闭窗口停止)
echo [后端] 如 import 报错，通常为依赖未装全，请重跑 start-agent.bat。
echo.

".venv\Scripts\python.exe" -m uvicorn app:app --host 127.0.0.1 --port 8321 --reload

echo.
echo [后端] 服务已退出。以上若有报错，请据此排查，或查看项目根目录 start-agent.log
pause
