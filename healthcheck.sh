#!/bin/bash
# 容器健康检查脚本

# 检查Python进程 - 使用pgrep替代pidof（更通用）
if ! pgrep -f "python.*src/main.py" > /dev/null; then
    echo "Python进程不存在"
    exit 1
fi

# 检查本容器的 HTTP 服务；不使用代理，避免代理响应掩盖本地后台故障。
python - <<'PY'
import json
import os
import sys
from urllib.request import ProxyHandler, build_opener

try:
    host = (os.getenv("UI_HOST") or "0.0.0.0").strip() or "0.0.0.0"
    host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
    if ":" in host:
        host = f"[{host}]"
    port = int(os.getenv("UI_PORT", "18080"))
    if not 1 <= port <= 65535:
        raise ValueError("UI_PORT 必须为 1～65535")
    url = f"http://{host}:{port}/healthz"
    with build_opener(ProxyHandler({})).open(url, timeout=3) as response:
        body = json.loads(response.read(1024))
        if response.status != 200 or body != {"ok": True, "service": "fn-message-bots"}:
            raise ValueError("响应不是本项目的健康检查结果")
except Exception as error:
    print(f"健康检查失败：配置 UI 无法正常响应（{error}）")
    sys.exit(1)

print("健康检查通过：Python 进程与配置 UI 均正常")
PY
