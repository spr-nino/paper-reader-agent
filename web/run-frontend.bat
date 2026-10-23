@echo off
chcp 65001 >nul
title 文献速读 Agent - 前端

REM 前端独立启动脚本：无论从哪里被调用，都先切到本脚本所在目录
cd /d "%~dp0"

if not exist "node_modules" (
    echo [前端] 未安装依赖 node_modules
    echo [前端] 请先在项目根目录运行 start-agent.bat，或在此目录手动执行 npm install。
    echo.
    pause
    exit /b 1
)

echo [前端] 正在启动 Vite: http://localhost:5173  (Ctrl+C 或关闭窗口停止)
echo [前端] 如启动失败，请查看下方报错，或查看项目根目录 start-agent.log
echo.

call npm run dev

echo.
echo [前端] 服务已退出。以上若有报错，请据此排查。
pause
