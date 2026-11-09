# FnMessageBots 官网

单文件静态站点：`index.html` + `assets/`，**零依赖、零构建**（与 `jiadi/website` 同一套做法）。

## 本地预览

```bash
cd website && python3 -m http.server 8081
# http://127.0.0.1:8081
```

## 部署

线上地址 `https://www.fnmb.lndo.top`（`fnmb.lndo.top` 用 DNSPod 显性 URL 301 到 www）。

EdgeOne Makers 新建项目指向本目录即可（纯静态，无需构建命令、无需 node）。

## 设计约定

- 视觉 token 取自应用内置 Web 配置页（`src/web/templates/index.html`）：浅色底 `#eef3ff`、主色 `#2563eb`、墨色 `#1f2933`、卡片圆角 16px、系统字体栈；深色跟随 `prefers-color-scheme`。
- 首屏右侧的「通知流转」、界面区的配置页与消息气泡都是 **HTML 复刻**，不是截图——缩放不糊字，改文案不用重出图。
- 只有一段内联脚本，用于修正 Safari 下锚点落点（`scroll-margin-top` 不被原生片段跳转识别）。
- 页脚必须有 `京ICP备2026075277号-1` 并链接 `https://beian.miit.gov.cn/`。
