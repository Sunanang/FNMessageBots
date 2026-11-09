# FnMessageBots 官网

单文件静态站点：`index.html` + `assets/`，**零依赖、零构建**（与 `jiadi/website` 同一套做法）。

## 本地预览

```bash
cd website && python3 -m http.server 8081
# http://127.0.0.1:8081
```

## 下载区怎么更新

官网的「下载」不跳 GitHub，走 **本站直连**（EdgeOne 国内节点）：安装包放在
`website/downloads/`，页面从 `downloads/version.json` 读版本号、文件名、大小与 SHA256。

每次发版后跑一次：

```bash
./scripts/sync-website-downloads.sh   # 拉最新 Release 的包 + 重建 version.json
git add website/downloads && git commit -m "chore(website): 同步 vX.Y.Z 安装包"
```

页面里 `data-ver / data-date / data-name` 的默认值是兜底（脚本失效或 JS 被关时仍能下），
有 `version.json` 时会被自动覆盖，所以平时**不用手改 HTML**。
「历史版本」链接始终指向 GitHub Releases。

## 部署

线上地址 `https://www.fnmb.lndo.top`（`fnmb.lndo.top` 用 DNSPod 显性 URL 301 到 www）。

EdgeOne Makers 新建项目指向本目录即可（纯静态，无需构建命令、无需 node）。

## 设计约定

形制：**设备运行手册**。刻意避开另外两个站点（家底 = 近黑极简、Pome = 深色影像）的深色营销页路线：

- 暖白纸底 `#FAF8F3`、细黑规则线、**左侧常驻目录**（桌面）/ 顶部 chip 条（手机）、
  编号章节 `01–07`、数据与标签一律等宽字。
- 全站只有一个强调色：朱橘 `#B8451F`（用于章节编号、当前目录项、汇总卡左边线）；
  绿色 `#2C8A5C` 只表示「已送达」。无渐变、无发光、无阴影堆叠、无 emoji。
- 唯一的动态与彩色集中在 01 章：真在跑的轮询示例 —— 日志流入 → 变暗 → 汇总出现 →
  9 个渠道格依次点亮 → 进度条走向下一轮，约 6 秒循环。`prefers-reduced-motion` 下停在终态。
  汇总卡始终占位（只做透明度过渡），所以循环不会让下方内容跳动。
- 其余部分安静：两栏对照表、带刻度的四步尺、渠道清单 + 同一条消息的四种排版、
  真实字段的配置面板、浅色代码块。
- 两段内联脚本：循环演示 + 目录高亮/刻度填充的 IntersectionObserver。零第三方依赖。
- 页脚必须有 `京ICP备2026075277号-1` 并链接 `https://beian.miit.gov.cn/`（只在正文页脚出现一次）。
- 下载区只放 FPK（普通版 / root 版）与 Docker 镜像说明；不要为了凑「多平台」在页面上
  编造不存在的产物。
