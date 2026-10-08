# 通用 Webhook（自定义 HTTP 推送）

[← 推送渠道总览](../notification-channels.md) · [← README](../../README.md)

把事件通知以自定义的 HTTP 请求发到任意地址，可对接 Gotify、ntfy、Server 酱、Home Assistant、n8n、自建服务等未内置的平台。返回 HTTP 2xx 即视为发送成功。

## 一、在本项目中配置

**方式 A：Web 配置页**

1. 添加推送渠道，类型选择 **通用Webhook**，填写 **Webhook 地址** 即可。默认以 POST + JSON 发送 `{"title": "标题", "content": "正文"}`。
2. 对方需要鉴权或特定字段时，展开 **高级选项** 填写 **请求头**（每行一个，如 `Authorization: Bearer xxx`）或 **请求体模板**。
3. 保存后在页面底部「发送测试」验证。

GET / PUT、表单、纯文本等方式无需在页面选择，如确有需要可在 `config.json` 中通过 `method` / `content_type` 字段指定，Web 页面保存时会保留。

**方式 B：`config.json` / 环境变量**

配置项 `webhook_params`（环境变量 `WEBHOOK_PARAMS`），值为 JSON 字符串，多个用 `|` 分隔：

```json
{"url": "https://example.com/hook", "method": "POST", "content_type": "json", "headers": "Authorization: Bearer xxx", "body": "{\"title\": \"{title}\", \"content\": \"{content}\"}"}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `url` | 是 | 请求地址（http/https），可包含占位符 |
| `method` | 否 | `POST`（默认）/ `GET` / `PUT`；GET 不发送请求体 |
| `content_type` | 否 | `json`（默认）/ `form` / `text` |
| `headers` | 否 | 请求头，每行 `名称: 值`，也可写成 JSON 对象 |
| `body` | 否 | 请求体模板；JSON 留空时默认 `{"title": "{title}", "content": "{content}"}` |

> 在 JSON 内的 `|` 需要写成 `\u007c`，避免与多渠道分隔符冲突；通过 Web 页面保存时会自动处理。

## 二、占位符

| 占位符 | 内容 |
| --- | --- |
| `{title}` | 消息标题 |
| `{content}` | 消息正文 |
| `{text}` | 标题 + 正文的完整纯文本 |
| `{time}` | 发送时间（`YYYY-MM-DD HH:MM:SS`） |

占位符按内容类型自动转义：JSON 中转义引号与换行（模板必须是合法 JSON），表单与 URL 中做 URL 编码，纯文本原样替换。

## 三、示例

- **ntfy**：地址 `https://ntfy.sh/你的主题`，方法 POST，内容类型「纯文本」，请求体 `{text}`。
- **Gotify**：地址 `https://gotify.example.com/message?token=xxx`，JSON 请求体 `{"title": "{title}", "message": "{content}"}`。
- **GET 推送**：地址 `https://example.com/push?msg={text}`，方法 GET。

运行日志中不会打印请求地址的查询参数；配置页回显时，地址里的 token / key 等疑似密钥参数以及 Authorization、Token、Cookie 等认证类请求头会打码，保存时未修改的打码值会自动还原。
