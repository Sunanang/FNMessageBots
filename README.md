# 飞牛 NAS 日志监控机器人

监控飞牛 NAS 事件日志（数据库 `logger_data.db3` 的 `log` 表），并推送多平台通知。

## 功能特性

- **数据库轮询**：定时轮询 eventlogger 的 SQLite 数据库，仅处理启动后的新记录
- **多平台通知**：企业微信、钉钉、飞书、Bark、PushPlus
- **Web 配置页**：内置配置 UI（默认端口 `18080`），支持 Webhook、事件类型、勿扰、标题前缀、PushPlus 等；保存后热加载，一般无需重启
- **Web 访问控制**：默认启用访问密码，可在系统设置关闭；首次设置密码需填写启动日志中的初始化码（或用 `FNMB_WEB_PASSWORD` 预置），`config.json` 内仅保存盐值与哈希；配置页中的 Webhook key、Token、SMTP 密码会打码显示
- **智能去重**：时间窗口去重（默认 300 秒）
- **磁盘事件合并**：同类型磁盘唤醒/休眠在时间窗口内合并推送
- **HTTP 连接池**：统一 HTTP 管理与重试
- **勿扰模式**：可设置时段内不推送，结束后汇总为一条消息
- **通知失败自愈**：连续失败达到阈值时可触发进程内重启通知链路（可通过环境变量关闭或调参）
- **Docker 部署**：支持 Docker Compose 一键部署，带健康检查
- **配置热加载**：通过 Web 保存 `config.json` 后自动生效；环境变量仍优先生效且不会被文件覆盖

### 支持的事件类型

以下为 `monitor_events` 中允许配置的 `eventId`（与数据库一致；旧配置里已移除的类型会在启动时被过滤）：

- **登录**：`LoginSucc`, `LoginSucc2FA1`, `LoginFail`, `Logout`
- **硬盘**：`FoundDisk`, `DiskWakeup`, `DiskSpindown`, `DISK_IO_ERR`
- **应用**：`APP_CRASH`, `APP_UPDATE_FAILED`, `APP_START_FAILED_LOCAL_APP_RUN_EXCEPTION`, `APP_AUTO_START_FAILED_DOCKER_NOT_AVAILABLE`, `APP_STARTED`, `APP_STOPPED`, `APP_UPDATED`, `APP_INSTALLED`, `APP_AUTO_STARTED`, `APP_UNINSTALLED`
- **系统**：`CPU_USAGE_ALARM`, `CPU_USAGE_RESTORED`, `CPU_TEMPERATURE_ALARM`
- **UPS**：`UPS_ONBATT`, `UPS_ONBATT_LOWBATT`, `UPS_ONLINE`, `UPS_ENABLE`, `UPS_DISABLE`
- **SSH**：`SSH_INVALID_USER`, `SSH_AUTH_FAILED`, `SSH_LOGIN_SUCCESS`, `SSH_DISCONNECTED`（优先从宿主机 `journalctl -u ssh/sshd` 采集；需挂载 `/var/log/journal` 与 `/etc/machine-id`）
- **文件与共享**：`ARCHIVING_SUCCESS`, `DeleteFile`, `MovetoTrashbin`, `SHARE_EVENTID_DEL`, `SHARE_EVENTID_PUT`
- **服务开关**：`WEBDAV_ENABLED` / `WEBDAV_DISABLED`, `SAMBA_ENABLED` / `SAMBA_DISABLED`, `DLNA_ENABLED` / `DLNA_DISABLED`, `FTP_ENABLED` / `FTP_DISABLED`, `NFS_ENABLED` / `NFS_DISABLED`
- **防火墙与安全**：`FW_ENABLE`, `FW_DISABLE`, `SECURITY_PORTCHANGED`
- **虚拟机**：`SHUTDOWN_VM`, `STATUS_RUNNING_VM`, `DESTROY_VM`

## 日志存储

- **触发推送的原始数据**写入 `./data/logs`（可配置 `log_dir`），按事件类型与日期分文件；保留天数由 `log_retention_days` 控制。
- **运行日志**：`./data/logs/monitor_YYYYMMDD.log`；应用运行日志保留天数由 `max_log_age` 控制。
- **游标**：`./data/cursor/db_poller_cursor.txt`，记录已处理到的 `log` 表 id。

### 管理工具

```bash
# 需在项目根目录，或设置 PYTHONPATH
python tools/log_manager.py stats
python tools/log_manager.py recent --hours 24
python tools/log_manager.py type LoginSucc --limit 10
python tools/log_manager.py export ./logs.json --event-type APP_CRASH
python tools/log_manager.py cleanup 30
```

## 使用方法

### 1. [配置通知渠道](docs/notification-channels.md)

至少配置一个推送渠道；环境变量与 Web 配置页二选一即可（亦可混用）。各平台图文说明见 **[配置通知渠道](docs/notification-channels.md)**（内含企业微信、钉钉、飞书、Bark、魔法推送、SMTP 邮件、PushPlus、企业微信应用、通用 Webhook、MeoW 的独立文档入口）。

未配置任何 Webhook / PushPlus 时进程仍可启动，仅提供 Web 配置页；配置完成后自动开始监控与推送。

### 2. 主要配置项（`config/config.json`）

| 项 | 说明 |
| --- | --- |
| `wechat_webhook_url` / `dingtalk_webhook_url` / `feishu_webhook_url` / `bark_url` | 各平台 Webhook 或 Bark URL |
| `pushplus_params` | PushPlus JSON（可多个，`\|` 分隔） |
| `wecom_app_params` | 企业微信自建应用 JSON（可多个，`\|` 分隔），见 [企业微信应用](docs/channels/wecom-app.md) |
| `webhook_params` | 通用 Webhook JSON（可多个，`\|` 分隔），见 [通用 Webhook](docs/channels/webhook.md) |
| `meow_params` | MeoW JSON（可多个，`\|` 分隔），见 [MeoW](docs/channels/meow.md) |
| `title_prefix` | 推送标题前缀，留空则使用默认「飞牛NAS」 |
| `monitor_events` | 要监控的事件 ID 列表 |
| `log_level` | 日志级别 |
| `log_dir` / `cursor_dir` | 日志与游标目录 |
| `logger_db_path` | SQLite 数据库路径 |
| `logger_poll_interval` | 轮询间隔（秒） |
| `http_pool_size` / `http_retry_count` / `http_timeout` | HTTP 客户端参数 |
| `dedup_window` | 去重时间窗口（秒） |
| `log_retention_days` | 原始推送日志保留天数 |
| `max_log_age` | 应用运行日志 `monitor_*.log` 保留天数 |
| `dnd_enabled` / `dnd_start_time` / `dnd_end_time` | 勿扰开关与时段（HH:MM，可跨日，与巡检 Cron 使用同一运行时区；Docker 默认北京时间，FPK 使用 NAS 系统时区） |
| `ssh_ignore_loopback` | 忽略来源为 127.0.0.1 / ::1 的 SSH 登录成功与断开（默认关闭；失败告警不受影响；SSH 经 frp 等内网穿透接入时勿开启）。环境变量 `SSH_IGNORE_LOOPBACK` |
首次在 Web 中设置密码后，会在同文件写入 `web_password_salt`、`web_password_hash`（请勿手工泄露）。在配置页「系统设置」中取消勾选「启用访问密码」并保存，即可免输密码（对应 `web_password_enabled: false`）；开关默认开启，兼容旧配置中的显式关闭值。关闭后刷新页面、重新打开、应用重启或空闲超过 15 分钟均无需登录；原密码哈希保留，再次勾选并保存后恢复密码登录，旧会话作废。仅建议在可信网络关闭密码验证。`FNMB_DISABLE_AUTH` 仍不生效。

启用密码时，FPK 和 Docker 首次访问或连续 15 分钟无操作后需输入应用密码，未过期时重新打开或刷新配置页可直接进入；飞牛管理员身份不能代替应用密码。连续 15 分钟无操作后自动返回登录页，后台统计刷新不会续期，未保存的配置不会自动提交。

**首次设置密码**：为防止局域网内他人抢先设密，首次设置需填写初始化码。初始化码在启动时打印到日志（`docker logs 容器名`），同时写入配置目录的 `.setup_code`（权限 600），设置密码后自动失效。也可用环境变量 `FNMB_WEB_PASSWORD` 预置初始密码（至少 8 位）。

**反向代理**：本机访问也需要初始化码。若使用反代，设置 `FNMB_TRUSTED_PROXIES` 为应用实际看到的代理 IP / CIDR（逗号分隔），并配置代理正确传递 `X-Forwarded-For`；仅信任自己控制的代理，避免 `0.0.0.0/0`。网关登录限流按 NAS 用户区分。

### 3. 常用环境变量

除上述 Webhook 外，还可通过环境变量覆盖（**已设置的环境变量不会被 `config.json` 覆盖**）：

- `LOGGER_DB_PATH`、`LOGGER_POLL_INTERVAL`
- `MONITOR_EVENTS`（逗号分隔）
- `LOG_LEVEL`、`HTTP_POOL_SIZE`、`HTTP_RETRY_COUNT`、`HTTP_TIMEOUT`、`DEDUP_WINDOW`
- `LOG_RETENTION_DAYS`、`MAX_LOG_AGE`
- `UI_PORT`：Web 端口（默认 `18080`）；`UI_HOST`：监听地址（默认 `0.0.0.0`，仅本机访问可设 `127.0.0.1`）
- `FNMB_WEB_PASSWORD`：未设密码时作为初始 Web 密码（至少 8 位）
- `TZ`：Docker 镜像默认 `Asia/Shanghai`（北京时间），勿扰判断、汇总消息时间与巡检 Cron 均使用此时区；可按 NAS 所在时区覆盖，如 `Europe/Berlin`。FPK 默认沿用 NAS 系统时区。
- `LOGTIME_DISPLAY_OFFSET_SECONDS`：修正日志时间显示（默认 `28800`，即 +8 小时；若显示不准可调整）
- `NOTIFY_RESTART_ENABLED`、`NOTIFY_RESTART_CONSECUTIVE`、`NOTIFY_RESTART_WINDOW`、`NOTIFY_RESTART_COOLDOWN`：通知链路失败重启策略（默认关闭；推送限流/额度用尽时重启无法恢复，反而会重复推送）
- `APP_HOME`：自定义应用根目录（影响 `config.json` 解析路径，一般 Docker 内为 `/app`）

### 4. 数据库路径

- 默认：`/usr/trim/var/eventlogger_service/logger_data.db3`
- 覆盖：`LOGGER_DB_PATH=/path/to/logger_data.db3` 或在 `config.json` 中设置 `logger_db_path`
- Docker 需将该文件或所在目录以只读等方式挂载进容器（见下方 Compose 示例或仓库内 `docker-compose.yml`）。

### 5. 启动

#### 使用 Docker Compose（推荐，官方镜像）

在任意目录新建 `docker-compose.yml`，内容如下（或在克隆本仓库后对照修改）：

```yaml
services:
  fn-message-bot:
    image: sunanang/fn-message-bots:latest
    container_name: fn-message-bot
    restart: unless-stopped
    network_mode: host                   # 巡检使用 NAS 的网卡/IP，端口由 UI_PORT 控制
    volumes:
      - ./data/logs:/app/data/logs:rw       # 数据目录，可修改
      - ./data/cursor:/app/data/cursor:rw   # 数据目录，可修改
      - /usr/trim/var/eventlogger_service:/usr/trim/var/eventlogger_service:ro  # 飞牛系统 eventlogger 目录，路径勿改
      - /etc/os-release:/host/etc/os-release:ro # 宿主操作系统版本
      - /var/lib/dpkg/status:/host/dpkg/status:ro # 宿主飞牛 trim 版本
      # - /var/run/docker.sock:/var/run/docker.sock  # 可选：勾选 Docker 容器事件时启用，等同授予 Docker API 访问能力
      - ./config:/app/config:rw             # 配置目录，可修改
    environment:
      - TZ=Asia/Shanghai
      - UI_PORT=${UI_PORT:-18080}          # 安装时可在同目录 .env 中填写 UI_PORT=19080
```

安装前可在 Compose 同目录的 `.env` 文件中填写 `UI_PORT=19080`（示例端口，可自行选择）；不填则使用 `18080`。浏览器访问 `http://<本机IP>:所选端口` 打开 Web 配置页。`./data/logs`、`./data/cursor`、`./config` 为相对 compose 文件所在目录的路径，可按需调整左侧宿主机路径。

Docker 不会自动继承 NAS 时区。直接从镜像创建容器时，镜像也默认使用北京时间；若容器配置中已显式设置 `TZ=UTC` 或其他时区，仍以该配置为准。出现「07:00 的勿扰汇总在北京时间 15:00 才送达」时，请将容器环境变量设为 `TZ=Asia/Shanghai` 并重建容器（仅重启不会应用修改后的容器环境）。启动日志会显示实际时区和 UTC 偏移。`LOGTIME_DISPLAY_OFFSET_SECONDS` 用于日志库字段的时间偏差，不用于调整勿扰或 Cron 时区。

巡检默认使用 host 网络，避免把容器的 `172.x` 地址当作 NAS 内网 IP；host 模式不配置 `ports`，修改监听端口请设置 `UI_PORT`。若保留自定义桥接网络及端口映射，请设置 `NAS_LAN_IP=<NAS 的内网 IPv4>`。原有容器需按更新后的 Compose 重建，才能使用新增挂载及网络配置。

系统版本优先读取宿主的 `/etc/os-release`；飞牛版本优先开放 API / `TRIM_SYS_VERSION`，再读取宿主版本文件或 dpkg 包信息。容器未挂载宿主版本文件且无其他可靠来源时显示 `--`。硬盘温度通过 sysfs 的设备链接匹配传感器，无法确认传感器归属或无读取权限时显示 `--`，不再借用其他硬盘温度；普通用户 FPK 版无需为了读取可访问的 hwmon 温度而提升整个进程权限。

**从源码构建**：若使用仓库根目录自带的 `docker-compose.yml`（`build: .`、服务名可能为 `fn-message-bots`），同样在项目根执行 `docker compose up -d` 即可。

#### 本地运行（无 Docker）

**依赖**：Python 3.9+（见 `pyproject.toml`）。先安装依赖：`pip install -e .` 或 `pip install -r requirements.txt`（与镜像一致）。

```bash
PYTHONPATH=. LOGGER_DB_PATH=./logger_data.db3 WECHAT_WEBHOOK_URL=xxx python3 src/main.py
```

## 项目结构

```
├── src/
│   ├── monitor/          # 数据库轮询、事件处理、模型
│   ├── notifier/         # 多平台通知、连接池
│   ├── utils/            # 日志、存储、健康检查、推送统计
│   ├── web/              # Web 配置 UI（Flask）
│   ├── config.py
│   └── main.py
├── config/config.json    # 配置文件（可挂载，Web 可写）
├── data/logs             # 运行日志与推送存储
├── data/cursor           # 轮询游标等
├── tools/log_manager.py
├── tests/                # 采集、权限诊断与配置接口回归（模拟网络、临时数据库）
│   └── fpk/              # FPK 安装/升级/权限脚本生命周期测试
├── fpk/                  # 飞牛应用中心 FPK 打包工程（manifest、cmd、wizard、app/ui）
├── docs/                 # 文档（notification-channels 与各推送渠道子页）
├── scripts/              # 辅助脚本（build-fpk.sh 打包 FPK、推送历史种子数据等）
├── dist/                 # FPK 构建产物（不入库）
├── .github/workflows/    # 自动测试、Docker 双架构与 FPK 发布；保留手动 manifest 合并
│   ├── publish.yml
│   ├── docker-manifest.yml
│   └── merge-manifest.yml
├── Dockerfile
├── docker-compose.yml
├── deploy.sh             # 本地 compose 部署
├── healthcheck.sh        # 容器健康检查
├── publish-amd64.sh      # AMD64 镜像构建与推送
├── publish-arm64.sh      # ARM64 镜像构建与推送
├── LICENSE               # MIT 许可证全文
└── pyproject.toml
```

### FPK 打包

```bash
./scripts/build-fpk.sh            # 需 fnpack（PATH 或 ~/.local/bin），或 FNPACK=/path/to/fnpack
# 产物: dist/FnMessageBot-<manifest version>.fpk 及 .fpk.sha256
python3 scripts/verify-fpk.py dist/FnMessageBot-2.7.0.fpk  # 示例：核对包内文件与当前源码
```

构建在独立临时目录进行，使用顶层 `src/`、`requirements.txt` 和 `assets/`，保留 `fpk/` 中的安装向导、权限与网关配置，不修改或删除工作区中的临时源码。

### 自动发布（GitHub Actions）

`dev` 负责开发，`main` 只接受同仓库 `dev → main` PR 的合并并负责发布。开发完成、准备发版时使用 **Create a merge commit** 合并 PR，不 squash/rebase，不删除 `dev`；合并后 [Publish Docker and FPK](https://github.com/Sunanang/FNMessageBots/actions/workflows/publish.yml) 自动测试、构建并发布。发布前会通过 GitHub API 核对当前提交确实来自已合并的 `dev → main` PR，直接推送的普通提交不能发布。

推送 `dev` 和提交到 `main` 的 PR 只运行开发检查，不发布镜像或 FPK。手动发布工作流也仅允许选择 `main`，并执行同样的来源校验。

新克隆后执行以下命令，后续保持在 `dev` 工作：

```bash
git switch dev
git config --local core.hooksPath .githooks
git push origin dev
# 准备发版时：在 GitHub 创建 dev → main PR，检查通过后以 merge commit 合并。
# 发布合并后同步开发分支：
git fetch origin
git switch dev
git merge --ff-only origin/main
git push origin dev
```

本地 hook 拒绝在 `main` 创建开发提交及直接推送 `main`。GitHub 远端还需启用针对 `refs/heads/main` 的规则集，要求 PR、`Main must come from dev` 和 `Development tests` 检查通过，禁止删除和强制推送，不设置绕过名单。可使用仓库中的 `.github/main-ruleset.json` 配置；已有 `main` 规则集 ID 为 `18157912`，拥有仓库 Administration 写权限并登录 `gh` 后可应用：

```bash
gh api --method PUT repos/Sunanang/FNMessageBots/rulesets/18157912 \
  --input .github/main-ruleset.json
```

规则配置文件不会自行启用远端保护；需在 GitHub 设置中启用，或执行上述管理命令。

测试通过后，在原生 amd64 / arm64 runner 分别构建镜像并启动容器验证 HTTP 健康检查；FPK 使用与 Pome Panel 相同的固定版本官方 fnpack 1.2.3，并校验工具的 SHA-256。两个架构及 FPK 均成功后才更新公开的最新构建：

| 产物 | 发布位置 |
| --- | --- |
| amd64 Docker 镜像 | `sunanang/fn-message-bots:latest-amd64` |
| arm64 Docker 镜像 | `sunanang/fn-message-bots:latest-arm64` |
| 自动选择架构的 Docker 镜像 | `sunanang/fn-message-bots:latest`（同时包含 amd64、arm64） |
| FPK 安装包 | [FnMessageBot-latest.fpk](https://github.com/Sunanang/FNMessageBots/releases/download/latest/FnMessageBot-latest.fpk) |
| FPK 校验文件 | [FnMessageBot-latest.fpk.sha256](https://github.com/Sunanang/FNMessageBots/releases/download/latest/FnMessageBot-latest.fpk.sha256) |

FPK 保存在固定 `latest` 标签的[最新构建预发布](https://github.com/Sunanang/FNMessageBots/releases/tag/latest)，同时提供带版本号的安装包和校验文件。它随通过检查的 `main` 提交更新，不要求每次改版本号；包内版本仍以 `fpk/manifest` 为准，正式升级时需同步维护 `pyproject.toml` 与 manifest 版本。每次构建的 FPK 也会保存为 Actions Artifact。

仓库的 **Settings → Secrets and variables → Actions** 需配置 `DOCKERHUB_USERNAME` 和 `DOCKERHUB_TOKEN`，与原有手动合并工作流使用的名称一致。Token 使用可推送 `sunanang/fn-message-bots` 的 Docker Hub Personal Access Token；FPK 发布使用工作流自带的 `GITHUB_TOKEN`，无需 NAS 凭据。发布前会核对镜像的提交与平台，再用 digest 合并，防止不同提交混入 `latest`；旧提交重跑时若 `main` 已前进，会保留构建产物并跳过覆盖最新构建。

运行测试：`python -m pytest tests`

## 故障排除

配置加载或保存后会按已勾选的功能检查数据源。权限不足时，保存按钮下方显示授权方式、可复制命令及 FAQ；文件缺失、表结构不兼容或服务未运行时给出对应原因。普通用户 FPK 的命令针对 `FnMessageBot`，Docker 提示针对挂载及容器 UID/GID。应用不自动执行授权，也不因一个数据源不可用阻止其他配置生效。

回归测试：安装项目运行依赖后，在项目根目录运行 `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v`。测试覆盖 IP、版本、多硬盘温度、权限诊断与配置保存接口，只读写临时配置和数据库，不请求公网接口、不发送通知。页面交互测试可在已有 Playwright 运行时执行 `node tests/check_warning_ui.cjs`，也可用 `FNMB_PLAYWRIGHT_MODULE` 和 `FNMB_BROWSER_EXECUTABLE` 指定已有运行时。

- **收不到通知**：检查 Webhook / PushPlus、网络与 `docker compose logs`。
- **时间不对**：调整 `LOGTIME_DISPLAY_OFFSET_SECONDS`（默认已为 +8 小时）。
- **重复通知**：调大 `dedup_window`（秒）。
- **Web 配置页打不开**：直接使用 `http://NAS内网IP:所选端口` 验证（默认 `18080`，本服务不直接提供 HTTPS），再检查桌面快捷入口、反向代理和防火墙。host 模式通过 `UI_PORT` 设置端口；桥接模式需核对端口映射。监听失败或 Web 服务线程退出后，程序每 5 秒尝试恢复入口，并将监听地址、失败和恢复信息写入 `monitor_*.log`。Docker 健康检查同时验证 Python 进程及 `/healthz` HTTP 响应；不验证 NAS 外部的代理或端口映射。
- **忘记 Web 密码**：在可信环境下编辑 `config.json`，删除 `web_password_salt` 与 `web_password_hash` 后重启，再用日志或 `config/.setup_code` 中的初始化码重新设置密码。

## 捐赠

创作不易，为了项目的稳定和可持续发展，欢迎大家捐赠支持
<table>
  <tr>
    <td><img src="docs/images/wechat_pay.jpg" width="300" alt="图1说明" /></td>
    <td><img src="docs/images/ali_pay.jpg" width="300" alt="图2说明" /></td>
  </tr>
</table>

## Star History

<a href="https://www.star-history.com/?repos=Sunanang%2FFNMessageBots&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=Sunanang/FNMessageBots&type=date&theme=dark&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=Sunanang/FNMessageBots&type=date&legend=top-left" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=Sunanang/FNMessageBots&type=date&legend=top-left" />
 </picture>
</a>

## 免责声明

- **非官方项目**：本仓库为独立开源作品，与飞牛 NAS、企业微信、钉钉、飞书、Bark、PushPlus 等主体**无隶属、赞助或背书关系**；各产品名称与商标归权利人所有。
- **按现状提供**：除开源许可证另有约定外，作者不对本软件的**正确性、完整性、安全性或持续可用性**作任何明示或默示担保；详见 [`LICENSE`](LICENSE) 中的免责声明条款。
- **使用风险自负**：部署、配置 Webhook / 第三方推送、开放 Web 配置端口、挂载系统路径等行为均由使用者自行评估与承担；请妥善保管 `config.json`、访问密码与各类 Token，**勿将密钥提交到公开仓库**。
- **数据与业务影响**：监控与推送依赖 NAS 日志库、网络与各通知渠道服务；**不保证**通知必达、实时或无遗漏，亦不对因依赖本软件而产生的业务中断、数据误解或决策损失承担责任。
- **合规**：使用本软件及对接的第三方服务时，请遵守相应平台规则、隐私政策及所在地法律法规。

## 许可证

本项目采用 [MIT License](https://opensource.org/licenses/MIT)。与 README 全文一致的纯文本见仓库根目录 [`LICENSE`](LICENSE)。

```
MIT License

Copyright (c) 2024 Sunanang

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
