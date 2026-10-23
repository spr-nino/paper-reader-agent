<#
.SYNOPSIS
    文献速读 Agent 一键启动脚本（PowerShell）
.DESCRIPTION
    自动检查 Python / Node 环境，安装缺失依赖，同时启动后端服务与前端开发服务器，
    并尝试打开浏览器。关闭本窗口或按任意键可结束后端与前端的进程。
#>

param(
    [switch]$SkipBrowser
)

$ErrorActionPreference = "Stop"

# 项目根目录
$ROOT = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ROOT

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  文献速读 Agent 一键启动" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

function Test-CommandExists {
    param([string]$Command)
    $null = Get-Command $Command -ErrorAction SilentlyContinue
    return $?
}

function Stop-WithPrompt {
    param([string]$Message)
    Write-Host "ERROR: $Message" -ForegroundColor Red
    Write-Host ""
    Write-Host "按任意键退出..." -ForegroundColor Yellow
    $null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")
    exit 1
}

if (-not (Test-CommandExists "python")) {
    Stop-WithPrompt "未检测到 python 命令。请先安装 Python 3.10+（https://www.python.org/downloads/）"
}
$pyVersion = (& python --version 2>&1).ToString()
Write-Host "✔ Python: $pyVersion" -ForegroundColor Green

if (-not (Test-CommandExists "node")) {
    Stop-WithPrompt "未检测到 node 命令。请先安装 Node.js 20+（https://nodejs.org/）"
}
$nodeVersion = (& node --version 2>&1).ToString()
Write-Host "✔ Node.js: $nodeVersion" -ForegroundColor Green

if (-not (Test-CommandExists "npm")) {
    Stop-WithPrompt "未检测到 npm 命令。请检查 Node.js 安装是否完整。"
}

Write-Host ""
Write-Host "【1/4】准备后端环境..." -ForegroundColor Cyan

$BACKEND_DIR = Join-Path $ROOT "backend"
if (-not (Test-Path $BACKEND_DIR)) {
    Stop-WithPrompt "未找到 backend 目录，请确认在项目根目录运行本脚本。"
}
Set-Location $BACKEND_DIR

$VENV_DIR = Join-Path $BACKEND_DIR ".venv"
if (-not (Test-Path $VENV_DIR)) {
    Write-Host "  创建 Python 虚拟环境..." -ForegroundColor Yellow
    & python -m venv .venv
    if ($LASTEXITCODE -ne 0) { Stop-WithPrompt "创建虚拟环境失败。" }
}

$ACTIVATE = Join-Path $VENV_DIR "Scripts\Activate.ps1"
if (-not (Test-Path $ACTIVATE)) {
    Stop-WithPrompt "未找到虚拟环境激活脚本：$ACTIVATE"
}
& $ACTIVATE
if ($LASTEXITCODE -ne 0) { Stop-WithPrompt "激活虚拟环境失败。" }

Write-Host "  安装 Python 依赖（首次运行可能需要几分钟）..." -ForegroundColor Yellow
& python -m pip install -q -r requirements.txt
if ($LASTEXITCODE -ne 0) {
    Stop-WithPrompt "安装 Python 依赖失败。请检查网络连接或 requirements.txt。"
}

if (-not (Test-CommandExists "uvicorn")) {
    Stop-WithPrompt "uvicorn 未正确安装，请检查 requirements.txt。"
}

Write-Host ""
Write-Host "【2/4】准备前端环境..." -ForegroundColor Cyan

$WEB_DIR = Join-Path $ROOT "web"
if (-not (Test-Path $WEB_DIR)) {
    Stop-WithPrompt "未找到 web 目录，请确认在项目根目录运行本脚本。"
}
Set-Location $WEB_DIR

if (-not (Test-Path (Join-Path $WEB_DIR "node_modules"))) {
    Write-Host "  安装 Node 依赖（首次运行可能需要几分钟）..." -ForegroundColor Yellow
    & npm install
    if ($LASTEXITCODE -ne 0) { Stop-WithPrompt "安装 Node 依赖失败。请检查网络连接或 package.json。" }
}
else {
    Write-Host "  node_modules 已存在，跳过 npm install。" -ForegroundColor Green
}

Write-Host ""
Write-Host "【3/4】启动服务..." -ForegroundColor Cyan

$backendCmd = @"
Set-Location "$BACKEND_DIR"; & "$ACTIVATE"; uvicorn app:app --host 127.0.0.1 --port 8321 --reload
"@
$backendProcess = Start-Process powershell -ArgumentList "-NoExit", "-Command", $backendCmd -PassThru

$frontendCmd = @"
Set-Location "$WEB_DIR"; npm run dev
"@
$frontendProcess = Start-Process powershell -ArgumentList "-NoExit", "-Command", $frontendCmd -PassThru

Write-Host "  后端 PID: $($backendProcess.Id)" -ForegroundColor Gray
Write-Host "  前端 PID: $($frontendProcess.Id)" -ForegroundColor Gray

Write-Host ""
Write-Host "【4/4】等待服务就绪..." -ForegroundColor Cyan

$frontendUrl = "http://localhost:5173"
$backendUrl = "http://127.0.0.1:8321/docs"

$ready = $false
$maxWait = 30
for ($i = 0; $i -lt $maxWait; $i++) {
    Start-Sleep -Seconds 1
    try {
        $resp = Invoke-WebRequest -Uri $frontendUrl -UseBasicParsing -TimeoutSec 2 -ErrorAction SilentlyContinue
        if ($resp.StatusCode -eq 200) { $ready = $true; break }
    } catch { }
    Write-Host "  等待中... $($i + 1)/$maxWait" -ForegroundColor Gray
}

if ($ready -and -not $SkipBrowser) {
    Write-Host "  服务已就绪，正在打开浏览器..." -ForegroundColor Green
    Start-Process $frontendUrl
}
elseif (-not $ready) {
    Write-Host "  服务可能尚未完全启动，请稍后手动访问 $frontendUrl" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "  Agent 已启动" -ForegroundColor Green
Write-Host "  前端地址：$frontendUrl" -ForegroundColor Green
Write-host "  后端地址：$backendUrl" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""
Write-Host "按任意键关闭后端和前端服务..." -ForegroundColor Yellow

$null = $Host.UI.RawUI.ReadKey("NoEcho,IncludeKeyDown")

Write-Host ""
Write-Host "正在关闭服务..." -ForegroundColor Cyan

function Stop-SafeProcess {
    param([System.Diagnostics.Process]$Proc)
    if ($Proc -and -not $Proc.HasExited) {
        try {
            $Proc.Kill()
            $Proc.WaitForExit(5000) | Out-Null
        }
        catch {
            Write-Host "  关闭进程 $($Proc.Id) 时出错：$_" -ForegroundColor Red
        }
    }
}

Stop-SafeProcess -Proc $backendProcess
Stop-SafeProcess -Proc $frontendProcess

Write-Host "服务已关闭。" -ForegroundColor Green
Start-Sleep -Seconds 1
