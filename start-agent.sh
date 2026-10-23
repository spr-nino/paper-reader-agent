#!/bin/bash
set -e

# 文献速读 Agent 一键启动脚本（macOS / Linux）
# 自动检查 Python / Node 环境，安装缺失依赖，同时启动后端服务与前端开发服务器。
# 在启动脚本所在的终端按 Ctrl+C 可结束后端与前端的进程。

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

echo "========================================"
echo "  文献速读 Agent 一键启动"
echo "========================================"
echo ""

command_exists() {
    command -v "$1" >/dev/null 2>&1
}

if ! command_exists python3; then
    echo "[错误] 未检测到 python3 命令。请先安装 Python 3.10+（https://www.python.org/downloads/）"
    exit 1
fi
echo "[OK] Python: $(python3 --version)"

if ! command_exists node; then
    echo "[错误] 未检测到 node 命令。请先安装 Node.js 20+（https://nodejs.org/）"
    exit 1
fi
echo "[OK] Node.js: $(node --version)"

if ! command_exists npm; then
    echo "[错误] 未检测到 npm 命令。请检查 Node.js 安装是否完整。"
    exit 1
fi

echo ""
echo "[1/4] 准备后端环境..."

BACKEND_DIR="$ROOT/backend"
if [ ! -d "$BACKEND_DIR" ]; then
    echo "[错误] 未找到 backend 目录，请确认在项目根目录运行本脚本。"
    exit 1
fi
cd "$BACKEND_DIR"

VENV_DIR="$BACKEND_DIR/.venv"
if [ ! -d "$VENV_DIR" ]; then
    echo "  创建 Python 虚拟环境..."
    python3 -m venv .venv
fi

echo "  安装 Python 依赖（首次运行可能需要几分钟）..."
source "$VENV_DIR/bin/activate"
python -m pip install -q -r requirements.txt

if ! command_exists uvicorn; then
    echo "[错误] uvicorn 未正确安装，请检查 requirements.txt。"
    exit 1
fi

echo ""
echo "[2/4] 准备前端环境..."

WEB_DIR="$ROOT/web"
if [ ! -d "$WEB_DIR" ]; then
    echo "[错误] 未找到 web 目录，请确认在项目根目录运行本脚本。"
    exit 1
fi
cd "$WEB_DIR"

if [ ! -d "$WEB_DIR/node_modules" ]; then
    echo "  安装 Node 依赖（首次运行可能需要几分钟）..."
    npm install
else
    echo "  node_modules 已存在，跳过 npm install。"
fi

echo ""
echo "[3/4] 启动服务..."

FRONTEND_URL="http://localhost:5173"
BACKEND_URL="http://127.0.0.1:8321/docs"

cd "$BACKEND_DIR"
source "$VENV_DIR/bin/activate"
uvicorn app:app --host 127.0.0.1 --port 8321 --reload &
BACKEND_PID=$!

cd "$WEB_DIR"
npm run dev &
FRONTEND_PID=$!

echo "  后端 PID: $BACKEND_PID"
echo "  前端 PID: $FRONTEND_PID"

# 捕获 Ctrl+C，优雅关闭
cleanup() {
    echo ""
    echo "正在关闭服务..."
    kill $BACKEND_PID $FRONTEND_PID 2>/dev/null || true
    wait $BACKEND_PID 2>/dev/null || true
    wait $FRONTEND_PID 2>/dev/null || true
    echo "服务已关闭。"
    exit 0
}
trap cleanup INT TERM

echo ""
echo "[4/4] 等待服务就绪..."

READY=0
for i in {1..30}; do
    sleep 1
    if curl -s -o /dev/null -w "%{http_code}" "$FRONTEND_URL" | grep -q "200"; then
        READY=1
        break
    fi
    echo "  等待中... $i/30"
done

if [ "$READY" -eq 1 ]; then
    echo "  服务已就绪，正在打开浏览器..."
    if command_exists open; then
        open "$FRONTEND_URL"
    elif command_exists xdg-open; then
        xdg-open "$FRONTEND_URL"
    fi
else
    echo "  服务可能尚未完全启动，请稍后手动访问 $FRONTEND_URL"
fi

echo ""
echo "========================================"
echo "  Agent 已启动"
echo "  前端地址：$FRONTEND_URL"
echo "  后端地址：$BACKEND_URL"
echo "========================================"
echo ""
echo "按 Ctrl+C 关闭后端和前端服务..."

wait
