# 方案探索：NAS 重启后的「启动静默期」开关

> 状态：**只做方案，未改代码**（2026-11-23）。等确认后再实现。

## 现象

NAS 重启后，FnMessageBots 先起来，接着其它应用与容器陆续启动，飞牛的事件库被这批启动事件刷满：`APP_STARTED` / `APP_AUTO_STARTED` / `DOCKER_CONTAINER_START` / `FoundDisk` / `DiskWakeup` 等。这些事件发生在应用启动**之后**，属于「新记录」，于是被逐条推出去，一次开机能刷出十几到几十条。

## 现有机制为什么挡不住

| 机制 | 实际作用 | 对本次问题的效果 |
| --- | --- | --- |
| 启动游标对齐（`db_poller_cursor.txt`、SSH journal 对齐） | 启动**之前**的存量日志不补推 | 挡不住，开机后的新事件照样推 |
| `dedup_window`（默认 300 秒） | 同内容事件合并 | 只压重复项，不同应用的启动事件各不相同 |
| `poll_batch_summary_enabled` | 一轮日志合并成一条汇总 | 只按轮次合并，开机后连续多轮，仍会多条 |
| `dnd_enabled`（勿扰时段） | 时段内不推、结束后汇总补发 | 与开机时间无关，白天开机不生效 |
| `minimal_push_enabled` | 精简文案 | 条数不变 |

结论：**能做成开关，而且改动集中在通知出口一处**，不用动轮询器、数据库读取和事件游标。

## 建议方案：启动静默期（startup quiet window）

### 语义

应用启动后 N 秒内产生的通知**不发送**，只记入历史；窗口结束时如果有被折叠的事件，补发**一条**汇总：`开机静默期 3 分钟：折叠 27 条事件（应用启动 18 / 容器启动 6 / 磁盘 3）`。

### 配置项（`config.json`，全部带默认值，默认关闭 → 升级后行为与现在完全一致）

```json
{
  "startup_quiet_enabled": false,
  "startup_quiet_seconds": 180,
  "startup_quiet_keep_urgent": true,
  "startup_quiet_only_after_boot": true
}
```

- `startup_quiet_enabled`：总开关。
- `startup_quiet_seconds`：窗口长度，限制 30–1800 秒（默认 180，够覆盖一批应用与容器启动）。
- `startup_quiet_keep_urgent`：白名单，静默期内仍实时推送的安全类事件 —— `LoginSucc` / `LoginFail` / `SSH_LOGIN_SUCCESS` / `SSH_AUTH_FAILED` / `UPS_ONBATT` / `UPS_ONBATT_LOWBATT` / `DISK_IO_ERR` / `STORAGE_DEGRADED` / `APP_CRASH`。开机时最该知道的恰恰是这几种，不能一起折叠。
- `startup_quiet_only_after_boot`：只在「真开机」时静默。读 `/proc/uptime` 判定：uptime 小于窗口长度视为开机后首启；关掉则手动重启、手动启动应用也静默。

### 实现落点（改动面）

1. **判定与状态**（新文件 `src/utils/startup_quiet.py`，约 80 行）
   - `should_suppress(event_type)`：开关是否开、是否在窗口内、是否命中白名单。
   - 状态落盘 `data/cursor/startup_quiet.json`，记 `boot_id`、`window_end`、各事件类型的折叠计数、`summary_sent`；进程崩溃或反复重启不会重复补发汇总。
2. **通知出口**（`src/notifier/unified_notifier.py` 发送前的一处 gate）
   - 命中抑制 → 计数后 `return`，不写 outbox、不重试；推送历史照常记，标 `suppressed=startup_quiet`，配置页历史里能看出「被静默期折叠」。
   - 窗口结束（下次检查或定时器触发）→ 发一条汇总并置 `summary_sent`。
3. **配置**（`src/config.py` 读取 + `src/web/ui_app.py` 保存校验）
   - 新键用 `as_bool` 与整数区间校验，缺失走默认值，旧 `config.json` 不需要迁移。
4. **界面**（`src/web/templates/index.html` 的「通知节流」分组）
   - 一行开关「开机静默期」+ 秒数输入 + 「安全事件照常推送」子开关，和现有「轮询汇总 / 勿扰时段 / 去重窗口」放在同一组。
5. **文档**：README 故障排除、官网 FAQ 各加一条；`fpk/manifest` 的 changelog 写明。

### 与现有机制的优先级

`启动静默期（白名单放行） → 勿扰时段 → 去重窗口 → 轮询汇总`。静默期的计数与勿扰期的累计分开存，避免窗口结束和勿扰结束各补一条重复汇总。

### 验收用例（实现时按此写测试）

1. 启动后 3 分钟内注入 50 条应用启动事件 → 推送 0 条；窗口结束后收到 1 条汇总，计数等于 50。
2. `startup_quiet_keep_urgent=true` 时，静默期内的 `LoginFail` 与 `UPS_ONBATT` 仍即时送达。
3. `startup_quiet_only_after_boot=true` 且系统 uptime 已经很大（只是手动重启应用）→ 不静默。
4. 窗口跨越勿扰时段 → 只补一条汇总，不出现两条。
5. 关闭开关 → 与 2.9.0 行为完全一致（回归基线）。
6. 进程在窗口内崩溃并重启 → 已发过的汇总不重发。

### 工作量与风险

- 后端约 150–200 行（含状态落盘与汇总文案），前端 1 组控件，测试 6 个用例。
- 只碰「是否发送」这一层，不动轮询、不改数据库读取、不改游标；回滚只需关开关。
- 默认关闭，现网零行为变化。

## 备选方案（不建议单独用）

- **只折叠系统类事件**：把 `APP_*`、`DOCKER_CONTAINER_*` 与磁盘类事件长期降级为「只进汇总」，与开机无关。能减噪，但日常的应用启动信息也会被压掉，信息损失更大，更适合作为静默期白名单的补充选项。
- **改轮询汇总**：开机后 N 分钟强制 `poll_batch_summary_enabled`。改动看着小，但只能按轮次合并，一波 5 轮仍是 5 条，而且动的是轮询器，风险高于在出口处 gate。

