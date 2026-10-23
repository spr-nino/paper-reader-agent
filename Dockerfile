# 文献速读 Agent 后端镜像(Render / Railway / Fly.io 通用)
# 仅后端;前端是独立的 Vite 应用,部署到 Vercel。
FROM python:3.11-slim

# 运行环境
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# 先装依赖(利用 Docker 层缓存)
COPY backend/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝后端代码
COPY backend/ ./

# Render 通过 PORT 环境变量指定端口(默认 10000)
ENV PORT=10000
EXPOSE 10000

# 生产模式:必须设置 READER_API_TOKEN(见 backend/security.py)
ENV READER_ENV=production

# data/ 持久化目录(平台上可挂卷;Render 免费层重启即清空,接受即可)
RUN mkdir -p /app/data

# 启动:平台会注入 $PORT
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT}"]
