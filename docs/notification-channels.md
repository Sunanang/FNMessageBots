# 配置通知渠道

[← 返回 README](../README.md)

至少配置一个推送渠道后，监控事件才会向外部推送。配置方式二选一（或混用：环境变量优先生效，未被环境变量覆盖的项可读 `config.json` / Web 页面）。


## 推送渠道一览

| 渠道 | 说明 | 详细文档 |
| --- | --- | --- |
| 企业微信（可以在微信接收消息） | 群机器人 Webhook | [企业微信](channels/wechat.md) |
| 钉钉 | 群机器人 Webhook | [钉钉](channels/dingtalk.md) |
| 飞书 | 群机器人 Webhook | [飞书](channels/feishu.md) |
| Bark | iOS 推送（HTTP API） | [Bark](channels/bark.md) |
| 魔法推送 | 推送聚合 | [魔法推送](channels/magicpush.md) |
| SMTP 邮件 | 邮箱通知 | [SMTP 邮件](channels/email.md) |
| PushPlus | 微信模板消息等 | [官方消息接口文档](https://www.pushplus.plus/doc/guide/api.html) |

## 按渠道打开文档

- [企业微信](channels/wechat.md)
- [钉钉](channels/dingtalk.md)
- [飞书](channels/feishu.md)
- [Bark](channels/bark.md)
- [魔法推送](channels/magicpush.md)
- [SMTP 邮件](channels/email.md)
- [PushPlus 官方消息接口文档](https://www.pushplus.plus/doc/guide/api.html)

## 环境变量速查

| 环境变量 | 对应配置项 |
| --- | --- |
| `WECHAT_WEBHOOK_URL` 或兼容名 `WEBHOOK_URL` | 企业微信 |
| `DINGTALK_WEBHOOK_URL` | 钉钉 |
| `FEISHU_WEBHOOK_URL` | 飞书 |
| `BARK_URL` | Bark |
| `SMTP_PARAMS` | SMTP 邮件（`smtp_params`） |

**PushPlus**：在 Web 或 `config.json` 中配置 `pushplus_params`，值为包含 `token` 的 JSON 字符串；多个渠道可用 `|` 分隔，例如 `{"token":"xxx"}|{"token":"yyy"}`。接口与参数说明见 [PushPlus 消息接口文档](https://www.pushplus.plus/doc/guide/api.html)

**SMTP 邮件**：在 Web 或 `config.json` 中配置 `smtp_params`，或通过环境变量 `SMTP_PARAMS` 覆盖；值为 SMTP 参数的 JSON 字符串，多套可用 `|` 分隔。字段与常见邮箱示例见 [SMTP 邮件](channels/email.md)。

## 捐赠

创作不易，为了项目的稳定和可持续发展，欢迎大家捐赠支持
<table>
  <tr>
    <td><img src="images/wechat_pay.jpg" width="300" alt="图1说明" /></td>
    <td><img src="images/ali_pay.jpg" width="300" alt="图2说明" /></td>
  </tr>
</table>
