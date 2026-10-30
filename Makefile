# FNMessageBots 常用命令（make 收口，避免记长命令）
VENV := .venv
PY   := $(VENV)/bin/python
PIP  := $(VENV)/bin/pip

.PHONY: help venv install test run logs clean

help: ## 列出所有命令
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

venv: ## 创建 .venv（已存在则跳过）
	@test -d $(VENV) || python3 -m venv $(VENV)

install: venv ## 安装运行与测试依赖
	$(PIP) install -q -U pip setuptools wheel
	$(PIP) install -q -r requirements.txt pytest

test: ## 跑全部测试（pytest tests）
	$(PY) -m pytest tests -q

run: ## 前台启动 bot（需先 make install）
	PYTHONPATH=src $(PY) src/main.py

logs: ## 查看最新日志尾部
	@f=$$(ls -t data/logs/*.log 2>/dev/null | head -1); [ -n "$$f" ] && { echo "== $$f"; tail -50 "$$f"; } || echo "(暂无日志 data/logs/*.log)"

clean: ## 清理 __pycache__ / .pytest_cache
	@find . -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
	@rm -rf .pytest_cache
