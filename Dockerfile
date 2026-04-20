FROM python:3.11-slim

# 设置环境变量
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# 设置PYTHONPATH以包含app目录
ENV PYTHONPATH=/app

# 设置工作目录
WORKDIR /app

# 仅保留健康检查所需的 pgrep（procps）
RUN set -ex && \
    apt-get -o Acquire::Retries=5 update && \
    apt-get install -y --no-install-recommends procps \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件
COPY requirements.txt .

# 安装Python依赖
RUN pip install --no-cache-dir -r requirements.txt

# 复制源代码
COPY src/ ./src/
COPY assets/ ./assets/

# 复制健康检查脚本（已在复制时设置执行权限）
COPY --chmod=755 healthcheck.sh /app/healthcheck.sh

# 应用写入的目录（与 config 中 log_dir、cursor_dir 一致）
RUN mkdir -p /app/data/logs /app/data/cursor

# 暴露 Web UI 端口（用于配置页面）
EXPOSE 18080

# 容器入口点
ENTRYPOINT ["python", "-u", "src/main.py"]

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=40s \
    CMD /app/healthcheck.sh