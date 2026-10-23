@echo off
chcp 65001 >nul
title 文献速读 Agent 启动器
setlocal enabledelayedexpansion

REM ============ 路径定义 ============
set "ROOT=%~dp0"
set "LOG=%ROOT%start-agent.log"
set "BACKEND_DIR=%ROOT%backend"
set "WEB_DIR=%ROOT%web"
set "VENV_DIR=%BACKEND_DIR%\.venv"
set "PYEXE=%VENV_DIR%\Scripts\python.exe"
set "FRONTEND_URL=http://localhost:5173"
set "BACKEND_URL=http://127.0.0.1:8321/docs"

echo [启动器] 脚本已开始运行 %DATE% %TIME%
>"%LOG%" echo ==== 启动日志 %DATE% %TIME% ====
echo [启动器] 项目根目录: %ROOT%

REM ============ 环境检查 ============
call :findpython
if "%PYCMD%"=="" call :fail "未检测到 Python。请先安装 Python 3.10+，安装时务必勾选 'Add Python to PATH'。"
echo [OK] Python 命令: %PYCMD%

if not exist "%BACKEND_DIR%" call :fail "未找到 backend 目录: %BACKEND_DIR%"
if not exist "%WEB_DIR%" call :fail "未找到 web 目录: %WEB_DIR%"

where node >nul 2>&1 || call :fail "未检测到 Node.js。请先安装 Node.js 20+ (https://nodejs.org/)。"
where npm  >nul 2>&1 || call :fail "未检测到 npm。请检查 Node.js 安装是否完整。"
echo [OK] Node.js 与 npm 已就绪

REM ============ 后端环境 ============
echo.
echo [1/3] 准备后端环境...
cd /d "%BACKEND_DIR%"
if not exist "%PYEXE%" (
    echo   创建 Python 虚拟环境...
    %PYCMD% -m venv ".venv" >>"%LOG%" 2>&1
    if errorlevel 1 call :fail "创建虚拟环境失败，详见 %LOG%"
)
echo   安装 Python 依赖（首次可能需要几分钟）...
"%PYEXE%" -m pip install -q -r requirements.txt >>"%LOG%" 2>&1
if errorlevel 1 call :fail "安装 Python 依赖失败，详见 %LOG%"
"%PYEXE%" -c "import uvicorn" >nul 2>&1 || call :fail "uvicorn 未正确安装，详见 %LOG%"

REM ============ 前端环境 ============
echo.
echo [2/3] 准备前端环境...
cd /d "%WEB_DIR%"
if not exist "%WEB_DIR%\node_modules" (
    echo   安装 Node 依赖（首次可能需要几分钟）...
    call npm install >>"%LOG%" 2>&1
    if errorlevel 1 call :fail "安装 Node 依赖失败，详见 %LOG%"
) else (
    echo   node_modules 已存在，跳过 npm install。
)

REM ============ 启动服务 ============
echo.
echo [3/3] 启动服务（将弹出后端/前端两个窗口）...
start "文献速读 Agent - 后端" cmd /k "%BACKEND_DIR%\run-backend.bat"
timeout /t 1 /nobreak >nul
start "文献速读 Agent - 前端" cmd /k "%WEB_DIR%\run-frontend.bat"

echo   正在等待前端就绪...
set READY=0
where curl >nul 2>&1 || goto :waitdone
for /l %%i in (1,1,30) do (
    timeout /t 1 /nobreak >nul
    curl -s -o nul -w "%%{http_code}" "%FRONTEND_URL%" > "%TEMP%\agent_status.txt" 2>nul
    set /p STATUS=<"%TEMP%\agent_status.txt"
    if "!STATUS!"=="200" (set READY=1 & goto :waitdone)
    echo   等待中... %%i/30
)
:waitdone
del "%TEMP%\agent_status.txt" 2>nul

echo.
echo ========================================
if "%READY%"=="1" (
    echo   服务已就绪，正在打开浏览器...
    start "" "%FRONTEND_URL%"
) else (
    echo   前端尚未就绪，请稍后手动访问 %FRONTEND_URL%
    echo   并查看弹出的后端/前端窗口中的报错信息，或查看 %LOG%
)
echo   前端地址：%FRONTEND_URL%
echo   后端地址：%BACKEND_URL%
echo ========================================
echo.
echo 说明：后端与前端在各自弹出的黑色窗口中运行。
echo       关闭那两个窗口即可停止服务；本窗口按任意键仅退出启动器。
pause >nul
exit /b 0

REM ============ 子程序 ============
:findpython
set "PYCMD="
where python  >nul 2>&1 && (set "PYCMD=python"  & goto :eof)
where py      >nul 2>&1 && (set "PYCMD=py -3"   & goto :eof)
where python3 >nul 2>&1 && (set "PYCMD=python3" & goto :eof)
goto :eof

:fail
echo.
echo [错误] %~1
echo.
pause
exit /b 1
