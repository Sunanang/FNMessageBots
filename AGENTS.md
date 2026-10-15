# AGENTS.md

## 开发与发布分支（Lando，2026-10-15）

- `dev` 是长期开发分支，所有功能、修复、文档和测试改动默认在 `dev` 完成。开始工作前检查当前分支；若在 `main`，先切回 `dev`。
- `main` 仅用于发布：禁止在 `main` 开发、创建普通提交、直接推送、强制推送或删除分支。只能通过同一仓库的 `dev → main` Pull Request 更新。
- 发版前先完成开发和验证，再使用 **Create a merge commit** 合并 `dev → main`；不使用 squash/rebase，不删除长期 `dev` 分支。合入 `main` 后自动构建并发布 Docker amd64、arm64、综合 `latest` 和 FPK。
- 普通开发请求只提交/推送 `dev`，不会发布；用户要求发版或合并时再合入 `main`。本次建立分支流程的配置通过 `dev → main` PR 初始化到发布分支。
- 合并发布后继续使用 `dev`，先抓取远端并将 `dev` 快进到 `origin/main`，再推送 `dev`，保持后续开发的共同历史。
- 本机使用 `git config --local core.hooksPath .githooks` 启用提交和推送保护。新克隆也需执行此命令；不能将本地 hook 当作 GitHub 远端分支保护的替代。
- FPK 已合并到本仓库 `fpk/`，不要再向旧的 `FNMessageBotForFPK` 仓库提交。

## Cursor Cloud specific instructions

### Overview

FNMessageBots is a Python 3.9+ Flask application that monitors FN NAS (飞牛 NAS) event logs (SQLite databases) and pushes notifications to multiple platforms (WeChat Work, DingTalk, Feishu, Bark, PushPlus, Magic Push, SMTP).

### Running the application

```bash
PYTHONPATH=. LOGGER_DB_PATH=./test_db/logger_data.db3 python3 src/main.py
```

The Web UI starts on port **18080** by default (configurable via `UI_PORT` env var). Without any webhook configured, the app still starts and provides the Web configuration UI.

### Key caveats

- **Regression tests**: Run `python -m pytest tests` with project dependencies and pytest installed. Tests cover monitoring, Web access, packaging and release checks; they use temporary data and mocked notification endpoints.
- **Type checking**: Run `basedpyright src/` from the project root. The tool reports pre-existing warnings/errors in the codebase (not blocking).
- **PYTHONPATH must include the repo root** (not `src/`) so that `from config import Config` and similar imports resolve correctly. Setting `PYTHONPATH=.` at the repo root works.
- **SQLite database**: The app requires a SQLite database at `logger_db_path` (or set via `LOGGER_DB_PATH` env var). For development, create a dummy one with a `log` table (columns: `id`, `serviceId`, `uid`, `uname`, `logtime`, `loglevel`, `eventId`, `parameter`, `category`). Without a valid DB, the app still starts but skips polling.
- **Config file location**: The app looks for `config/config.json` relative to the project root (or `/app/config/config.json` in Docker). The file already exists in the repo.
- **Hot reload**: Saving config via the Web UI triggers hot reload—no restart needed.
- **No linter (ruff/flake8) is configured** in this project. Only `basedpyright` is used for static analysis.
- **Dependencies install to user site** (`--user`) on this VM since system site-packages is not writable. All packages land in `~/.local/lib/python3.12/`.
