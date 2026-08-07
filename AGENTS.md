# AGENTS.md

## Cursor Cloud specific instructions

### Overview

FNMessageBots is a Python 3.9+ Flask application that monitors FN NAS (飞牛 NAS) event logs (SQLite databases) and pushes notifications to multiple platforms (WeChat Work, DingTalk, Feishu, Bark, PushPlus, Magic Push, SMTP).

### Running the application

```bash
PYTHONPATH=. LOGGER_DB_PATH=./test_db/logger_data.db3 python3 src/main.py
```

The Web UI starts on port **18080** by default (configurable via `UI_PORT` env var). Without any webhook configured, the app still starts and provides the Web configuration UI.

### Key caveats

- **No formal test suite exists** in this codebase. There are no `tests/` directory or test files. Validation is done via type checking with `basedpyright` and manual testing of the running application.
- **Type checking**: Run `basedpyright src/` from the project root. The tool reports pre-existing warnings/errors in the codebase (not blocking).
- **PYTHONPATH must include the repo root** (not `src/`) so that `from config import Config` and similar imports resolve correctly. Setting `PYTHONPATH=.` at the repo root works.
- **SQLite database**: The app requires a SQLite database at `logger_db_path` (or set via `LOGGER_DB_PATH` env var). For development, create a dummy one with a `log` table (columns: `id`, `serviceId`, `uid`, `uname`, `logtime`, `loglevel`, `eventId`, `parameter`, `category`). Without a valid DB, the app still starts but skips polling.
- **Config file location**: The app looks for `config/config.json` relative to the project root (or `/app/config/config.json` in Docker). The file already exists in the repo.
- **Hot reload**: Saving config via the Web UI triggers hot reload—no restart needed.
- **No linter (ruff/flake8) is configured** in this project. Only `basedpyright` is used for static analysis.
- **Dependencies install to user site** (`--user`) on this VM since system site-packages is not writable. All packages land in `~/.local/lib/python3.12/`.
