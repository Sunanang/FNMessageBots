# 企业微信应用（自建应用消息）

[← 推送渠道总览](../notification-channels.md) · [← README](../../README.md)

通过企业微信 **自建应用** 向成员、部门或标签推送文本消息。与「企业微信群机器人」不同，应用消息可直达个人，并可在微信插件中接收。

<span style="color: #c62828;"><strong>注意：应用 Secret 属于敏感数据，请勿截图外传或提交到公开仓库。</strong></span>

## 一、准备工作（在企业微信管理后台）

1. 登录 [企业微信管理后台](https://work.weixin.qq.com/wework_admin/frame)，在「我的企业」页底部记下 **企业ID（corpid）**。
2. 进入「应用管理 → 自建 → 创建应用」，创建后记下 **AgentId** 与 **Secret**（Secret 需在企业微信客户端中查看）。
3. 在应用详情的「可见范围」中加入要接收消息的成员 / 部门。
4. 在应用详情底部配置 **企业可信IP**，填写 NAS 访问外网时的出口公网 IP。未配置时接口会返回错误码 `60020`（not allow to access from your ip）。
   - 家庭宽带公网 IP 经常变化，建议使用有固定 IP 的服务器搭建反向代理（转发到 `https://qyapi.weixin.qq.com`），并在本项目中把「API 地址」填为该代理地址。
   - 2022 年 6 月后新建的应用，配置可信 IP 前通常需要先设置「可信域名」或「接收消息服务器 URL」，按后台提示操作即可。

## 二、在本项目中配置

**方式 A：Web 配置页**

1. 添加推送渠道，类型选择 **企业微信应用**。
2. 填写 **企业ID**、**应用 Secret**、**AgentId** 三项即可，默认推送给应用可见范围内的全部成员。
3. 需要指定接收人或使用代理时，展开 **高级选项**：**接收成员**（成员账号，多个用 `|` 分隔）、**接收部门ID**、**接收标签ID**、**API 地址**（使用可信代理时填写）。
4. 保存后在页面底部「发送测试」验证。

**方式 B：`config.json` / 环境变量**

配置项 `wecom_app_params`（环境变量 `WECOM_APP_PARAMS`），值为 JSON 字符串，多个应用用 `|` 分隔：

```json
{"corp_id": "ww1234567890abcdef", "corp_secret": "应用Secret", "agent_id": "1000002", "to_user": "@all"}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `corp_id` | 是 | 企业ID |
| `corp_secret` | 是 | 应用 Secret |
| `agent_id` | 是 | 应用 AgentId（数字） |
| `to_user` | 否 | 接收成员，多个用 `\|` 分隔；与部门、标签都为空时默认 `@all` |
| `to_party` | 否 | 接收部门 ID，多个用 `\|` 分隔 |
| `to_tag` | 否 | 接收标签 ID，多个用 `\|` 分隔 |
| `api_base` | 否 | API 地址，默认 `https://qyapi.weixin.qq.com` |

> 在 JSON 内的 `|` 需要写成 `\u007c`，避免与多渠道分隔符冲突；通过 Web 页面保存时会自动处理。

## 三、常见问题

- **`60020` not allow to access from your ip**：未配置或未命中企业可信 IP，见上文第 4 步。
- **`40001` / `40013` invalid credential / corpid**：企业ID 或 Secret 填写错误（注意 Secret 与应用对应）。
- **`81013` user & party & tag all invalid**：接收对象不在应用可见范围内。
- access_token 会自动缓存与刷新，无需手动处理。
