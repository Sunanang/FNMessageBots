"""
Web UI 页面模板常量。
"""

HISTORY_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
  {% if favicon_url %}
  <link rel="icon" href="{{ favicon_url }}" />
  {% endif %}
<title>推送记录 - FnMessageBot</title>
  <style>
    * { box-sizing: border-box; }
    :root { color-scheme: light; }
    html[data-theme="dark"] { color-scheme: dark; }
    body {
      margin: 0;
      padding: 32px 24px 40px;
      min-height: 100vh;
      min-height: 100dvh;
      display: flex;
      flex-direction: column;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, "Noto Sans", "PingFang SC", sans-serif;
      background: #eef3ff;
      color: #1f2933;
    }
    .history-main {
      flex: 1 1 auto;
      width: 100%;
      min-height: 0;
    }
    h1 {
      font-size: 24px;
      font-weight: 700;
      letter-spacing: 0.06em;
      color: #111827;
      margin: 0 0 8px;
    }
    .page-hint {
      font-size: 14px;
      color: #6b7280;
      line-height: 1.5;
      margin: 0 0 16px;
    }
    .top-bar { margin-bottom: 20px; display: flex; justify-content: flex-start; }
    .toolbar { margin-bottom: 14px; display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
    .toolbar .field-label { margin-bottom: 0; }
    .field-label {
      font-size: 13px;
      color: #4b5563;
      margin-bottom: 4px;
    }
    a.btn {
      text-decoration: none;
    }
    .btn {
      min-width: 96px;
      border-radius: 999px;
      padding: 8px 20px;
      border: none;
      font-size: 14px;
      font-weight: 500;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      transition: background-color 0.15s, box-shadow 0.15s, transform 0.05s;
      font-family: inherit;
    }
    .btn-primary {
      background: linear-gradient(135deg, #2563eb, #1d4ed8);
      color: #fff;
      box-shadow: 0 12px 22px rgba(37, 99, 235, 0.28);
    }
    .btn-primary:hover {
      background: linear-gradient(135deg, #1d4ed8, #1e40af);
      box-shadow: 0 14px 26px rgba(37, 99, 235, 0.3);
      transform: translateY(-1px);
    }
    .btn-ghost {
      background: #fff;
      color: #111827;
      border: 1px solid #d1d5db;
    }
    .btn-ghost:hover {
      background: #f3f4f6;
    }
    .table-wrap { width: 100%; overflow-x: auto; border-radius: 8px; }
    table {
      width: 100%;
      table-layout: fixed;
      border-collapse: collapse;
      font-size: 13px;
      background: #fff;
      border-radius: 8px;
      overflow: hidden;
      box-shadow: 0 1px 3px rgba(0, 0, 0, 0.08);
      min-width: 760px;
    }
    th, td { padding: 10px 12px; text-align: left; vertical-align: middle; }
    thead th {
      color: #6b7280;
      font-weight: 500;
      border-bottom: 1px solid #e5e7eb;
      background: #fff;
    }
    tbody td {
      border-bottom: 1px solid #f3f4f6;
      color: #374151;
    }
    tbody tr:last-child td { border-bottom: none; }
    tbody td.result-ok { color: #059669; font-weight: 700; }
    tbody td.result-fail { color: #dc2626; font-weight: 700; }
    .summary {
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      vertical-align: middle;
    }
    .channel-results {
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      vertical-align: middle;
      font-size: 12px;
    }
    .empty { padding: 24px; text-align: center; color: #9ca3af; font-size: 13px; }
    .link { color: #2563eb; cursor: pointer; }
    .link:hover { text-decoration: underline; }
    .more-wrap { margin-top: 14px; text-align: center; }
    .detail-wrap { margin-top: 20px; padding: 16px; background: #fffbeb; border-radius: 8px; border: 1px solid #fde68a; }
    .detail-wrap h2 { font-size: 14px; margin: 0 0 8px; }
    pre { margin: 0; white-space: pre-wrap; word-break: break-all; font-size: 12px; background: #1f2937; color: #e5e7eb; padding: 12px; border-radius: 6px; }
    .license-footer {
      flex-shrink: 0;
      margin: 0;
      padding-top: 12px;
      text-align: center;
      font-size: 11px;
      line-height: 1.5;
      color: #9ca3af;
    }
    html[data-theme="dark"] body { background: #111827; color: #e5e7eb; }
    html[data-theme="dark"] h1 { color: #f9fafb; }
    html[data-theme="dark"] .page-hint { color: #9ca3af; }
    html[data-theme="dark"] .field-label { color: #9ca3af; }
    html[data-theme="dark"] table { background: #0f172a; box-shadow: 0 1px 3px rgba(0, 0, 0, 0.4); }
    html[data-theme="dark"] thead th {
      background: #0f172a;
      color: #9ca3af;
      border-bottom-color: #374151;
    }
    html[data-theme="dark"] tbody td {
      color: #d1d5db;
      border-bottom-color: #1f2937;
    }
    html[data-theme="dark"] tbody td.result-ok { color: #34d399; font-weight: 700; }
    html[data-theme="dark"] tbody td.result-fail { color: #f87171; font-weight: 700; }
    html[data-theme="dark"] .empty { color: #9ca3af; }
    html[data-theme="dark"] .detail-wrap { background: #111827; border-color: #374151; }
    html[data-theme="dark"] .license-footer { color: #6b7280; }
    html[data-theme="dark"] .btn-ghost {
      background: #111827;
      color: #e5e7eb;
      border-color: #4b5563;
    }
    html[data-theme="dark"] .btn-ghost:hover {
      background: #1f2937;
    }
    @media (max-width: 768px) {
      body { padding: 16px 16px 24px; }
      h1 { font-size: 20px; }
      .btn { min-width: 0; }
      .detail-wrap { padding: 12px; }
    }
  </style>
</head>
<body>
  <main class="history-main">
      <div class="top-bar">
        <a class="btn btn-ghost" href="/">返回配置页</a>
      </div>
      <h1>推送记录</h1>
      <p class="page-hint">最多存储一万条数据，超过限制会自动删除。</p>
      <div class="toolbar">
        <span class="field-label">筛选：</span>
        <button type="button" class="btn btn-primary filter-btn" data-filter="">全部</button>
        <button type="button" class="btn btn-ghost filter-btn" data-filter="true">成功</button>
        <button type="button" class="btn btn-ghost filter-btn" data-filter="false">失败</button>
      </div>
      <div class="table-wrap">
        <table>
          <colgroup>
            <col style="width:160px" />
            <col style="width:120px" />
            <col style="width:64px" />
            <!-- 摘要列尽量宽，「渠道返回结果」列保持更窄，避免占满视口 -->
            <col style="width:calc(100% - 604px)" />
            <col style="width:260px" />
          </colgroup>
          <thead><tr><th>时间</th><th>事件类型</th><th>结果</th><th>摘要</th><th>渠道返回结果</th></tr></thead>
          <tbody id="tbody"></tbody>
        </table>
      </div>
      <div id="empty" class="empty" style="display:none;">暂无推送记录</div>
      <div class="more-wrap"><button type="button" id="btn-more" class="btn btn-ghost" style="display:none;">加载更多</button></div>
  <script>
  var THEME_STORAGE_KEY = "fnmb_theme";
  function applyHistoryTheme() {
    var mode = localStorage.getItem(THEME_STORAGE_KEY);
    var resolved = (mode === "dark") ? "dark" : "light";
    document.documentElement.setAttribute("data-theme", resolved);
  }
  applyHistoryTheme();
  var fetchOpts = { credentials: "include" };
  var offset = 0;
  var pageSize = 30;
  var currentFilter = "";
  function loadList(reset) {
    if (reset) { offset = 0; document.getElementById("tbody").innerHTML = ""; }
    var params = "limit=" + pageSize + "&offset=" + offset;
    if (currentFilter !== "") params += "&success=" + currentFilter;
    fetch("/api/push-history?" + params, fetchOpts).then(function(r){
      if (r.status === 401) { window.location.href = "/"; return; }
      return r.json();
    }).then(function(json){
      if (!json || !json.ok || !Array.isArray(json.data)) return;
      var rows = json.data;
      var tbody = document.getElementById("tbody");
      var emptyEl = document.getElementById("empty");
      if (rows.length === 0 && offset === 0) { emptyEl.style.display = "block"; } else { emptyEl.style.display = "none"; }
      for (var i = 0; i < rows.length; i++) {
        var r = rows[i];
        var tr = document.createElement("tr");
        var td1 = document.createElement("td"); td1.textContent = r.created_at || ""; tr.appendChild(td1);
        var td2 = document.createElement("td"); td2.textContent = r.event_type_label || r.event_type || ""; tr.appendChild(td2);
        var td3 = document.createElement("td"); td3.textContent = r.success ? "成功" : "失败"; td3.className = r.success ? "result-ok" : "result-fail"; tr.appendChild(td3);
        var td4 = document.createElement("td"); td4.className = "summary"; td4.textContent = r.summary || "-"; td4.title = r.summary || ""; tr.appendChild(td4);
        var td5 = document.createElement("td"); td5.className = "channel-results";
        var channelText = "-";
        var channelTitle = "";
        if (r.detail) {
          try {
            var detail = typeof r.detail === "string" ? JSON.parse(r.detail) : r.detail;
            if (detail && Array.isArray(detail.channel_results) && detail.channel_results.length > 0) {
              var parts = [];
              var fullParts = [];
              detail.channel_results.forEach(function(c){
                var status = c.success ? "成功" : "失败";
                var extra = "";
                if (!c.success) {
                  if (c.response != null && typeof c.response === "object") {
                    extra = JSON.stringify(c.response);
                  } else if (typeof c.response === "string") {
                    extra = c.response;
                  }
                  if (!extra && c.error) extra = c.error;
                  if (!extra) extra = "无返回详情";
                }
                var extraShort = extra ? (extra.length > 28 ? extra.slice(0, 28) + "…" : extra) : "";
                var short = c.channel + ": " + status + (extraShort ? " (" + extraShort + ")" : "");
                parts.push(short);
                var fullExtra = extra ? " — " + extra : "";
                fullParts.push(c.channel + ": " + status + fullExtra);
              });
              channelText = parts.join("; ");
              channelTitle = fullParts.join(String.fromCharCode(10));
            }
          } catch (e) {
            channelText = "详情解析失败";
          }
        }
        td5.textContent = channelText;
        td5.title = channelTitle || channelText;
        tr.appendChild(td5);
        tbody.appendChild(tr);
      }
      offset += rows.length;
      document.getElementById("btn-more").style.display = rows.length >= pageSize ? "inline-block" : "none";
    }).catch(function(){});
  }
  document.querySelectorAll(".filter-btn").forEach(function(btn){
    btn.onclick = function(){
      document.querySelectorAll(".filter-btn").forEach(function(b){
        b.classList.remove("btn-primary");
        b.classList.add("btn-ghost");
      });
      this.classList.remove("btn-ghost");
      this.classList.add("btn-primary");
      currentFilter = this.getAttribute("data-filter") || "";
      loadList(true);
    };
  });
  document.getElementById("btn-more").onclick = function(){ loadList(false); };
  loadList(true);
  </script>
  </main>
  <p class="license-footer">© 2024 Sunanang · FnMessageBot · MIT License terms apply.</p>
</body>
</html>"""


SUPPORT_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
  {% if favicon_url %}
  <link rel="icon" href="{{ favicon_url }}" />
  {% endif %}
  <title>支持作者 - FnMessageBot</title>
  <script>
    (function() {
      var mode = localStorage.getItem("fnmb_theme");
      var resolved = (mode === "dark") ? "dark" : "light";
      document.documentElement.setAttribute("data-theme", resolved);
    })();
  </script>
  <style>
    :root {
      color-scheme: light;
      --support-bg: #eef3ff;
      --support-bg-accent: #e0e7ff;
      --support-card: #ffffff;
      --support-panel: #f9fafb;
      --support-panel-border: #e5e7eb;
      --support-top-btn-bg: rgba(255, 255, 255, 0.96);
      --support-text: #1f2933;
      --support-muted: #6b7280;
      --support-faint: #9ca3af;
      --support-accent: #ea580c;
      --support-accent-soft: #fb923c;
      --support-border: rgba(148, 163, 184, 0.35);
      --support-shadow: 0 14px 36px rgba(15, 23, 42, 0.08);
      --support-shadow-card: 0 8px 28px rgba(15, 23, 42, 0.06);
      --ali-blue: #1677ff;
      --wx-green: #07c160;
    }
    html[data-theme="dark"] {
      color-scheme: dark;
      --support-bg: #111827;
      --support-bg-accent: #1f2937;
      --support-card: rgba(17, 24, 39, 0.92);
      --support-panel: rgba(17, 24, 39, 0.92);
      --support-panel-border: rgba(75, 85, 99, 0.55);
      --support-top-btn-bg: rgba(17, 24, 39, 0.92);
      --support-text: #f3f4f6;
      --support-muted: #9ca3af;
      --support-faint: #6b7280;
      --support-accent: #fb923c;
      --support-accent-soft: #fdba74;
      --support-border: rgba(75, 85, 99, 0.55);
      --support-shadow: 0 18px 40px rgba(0, 0, 0, 0.35);
      --support-shadow-card: 0 12px 32px rgba(0, 0, 0, 0.25);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, "Noto Sans", "PingFang SC", sans-serif;
      background: var(--support-bg);
      color: var(--support-text);
    }
    .page {
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 32px 16px;
    }
    .card {
      width: 100%;
      max-width: 920px;
      background: var(--support-card);
      border-radius: 16px;
      box-shadow: 0 18px 40px rgba(15,23,42,0.18);
      border: 1px solid var(--support-border);
      backdrop-filter: blur(10px);
    }
    html[data-theme="dark"] .card {
      background: rgba(17,24,39,0.9);
      border-color: rgba(75,85,99,0.55);
      box-shadow: 0 18px 40px rgba(0, 0, 0, 0.45);
    }
    .support-back-bar {
      display: flex;
      justify-content: flex-start;
      align-items: center;
      padding: 24px 24px 0;
      flex-wrap: wrap;
      gap: 10px;
    }
    .support-wrap {
      max-width: 760px;
      margin: 0 auto;
      padding: 20px 24px 36px;
    }
    a.btn {
      text-decoration: none;
    }
    .btn {
      min-width: 96px;
      border-radius: 999px;
      padding: 8px 20px;
      border: none;
      font-size: 14px;
      font-weight: 500;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      transition: background-color 0.15s, box-shadow 0.15s, transform 0.05s;
      font-family: inherit;
    }
    .btn-ghost {
      background: #fff;
      color: #111827;
      border: 1px solid #d1d5db;
    }
    .btn-ghost:hover {
      background: #f3f4f6;
    }
    html[data-theme="dark"] .btn-ghost {
      background: #111827;
      color: #e5e7eb;
      border-color: #4b5563;
    }
    html[data-theme="dark"] .btn-ghost:hover {
      background: #1f2937;
    }
    .hero {
      text-align: center;
      margin-bottom: 28px;
    }
    .hero h1 {
      font-size: clamp(1.65rem, 4.5vw, 2rem);
      font-weight: 700;
      margin: 0 0 12px;
      letter-spacing: 0.02em;
      color: var(--support-accent);
    }
    .hero p {
      margin: 0 auto;
      max-width: 34em;
      font-size: 14px;
      line-height: 1.65;
      color: var(--support-muted);
    }
    .sheet {
      background: var(--support-panel);
      border-radius: 16px;
      border: 1px solid var(--support-panel-border);
      box-shadow: var(--support-shadow);
      padding: 22px 22px 20px;
      margin-bottom: 28px;
    }
    .sheet h2 {
      font-size: 16px;
      font-weight: 650;
      margin: 0 0 14px;
      display: flex;
      align-items: center;
      gap: 8px;
      color: var(--support-text);
    }
    .sheet h2 .emoji {
      font-size: 18px;
      line-height: 1;
    }
    .sheet p {
      margin: 0;
      font-size: 14px;
      line-height: 1.7;
      color: var(--support-muted);
    }
    .pay-section-title {
      text-align: center;
      font-size: 15px;
      font-weight: 650;
      margin: 0 0 18px;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      color: var(--support-text);
    }
    .pay-section-title .emoji { font-size: 17px; }
    .pay-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 18px;
    }
    .pay-grid.single {
      grid-template-columns: minmax(0, 280px);
      justify-content: center;
    }
    .pay-card {
      background: var(--support-panel);
      border-radius: 14px;
      border: 1px solid var(--support-panel-border);
      box-shadow: var(--support-shadow-card);
      padding: 18px 16px 16px;
      text-align: center;
    }
    .pay-card-head {
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      margin-bottom: 14px;
      font-size: 15px;
      font-weight: 600;
    }
    .pay-card--ali .pay-card-head { color: var(--ali-blue); }
    .pay-card--wx .pay-card-head { color: var(--wx-green); }
    .pay-badge {
      width: 28px;
      height: 28px;
      border-radius: 8px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      font-size: 14px;
      font-weight: 700;
      color: #fff;
    }
    .pay-badge--ali { background: var(--ali-blue); }
    .pay-badge--wx { background: var(--wx-green); }
    .qr-frame {
      display: flex;
      align-items: center;
      justify-content: center;
      margin: 0 auto 12px;
      max-width: 240px;
    }
    .pay-card--ali .qr-frame {
      padding: 10px;
      background: #0f172a;
      border-radius: 12px;
    }
    html[data-theme="dark"] .pay-card--ali .qr-frame {
      background: #030712;
    }
    .pay-card--wx .qr-frame {
      padding: 10px;
      background: #ffffff;
      border-radius: 12px;
      border: 2px solid rgba(251, 191, 36, 0.65);
      box-shadow: inset 0 0 0 1px rgba(251, 191, 36, 0.2);
    }
    html[data-theme="dark"] .pay-card--wx .qr-frame {
      background: #0f172a;
    }
    .qr-frame img {
      display: block;
      width: 100%;
      height: auto;
      border-radius: 8px;
    }
    .pay-card--ali .qr-frame img {
      border-radius: 6px;
    }
    .pay-caption {
      font-size: 12px;
      line-height: 1.5;
      color: var(--support-faint);
      margin: 0;
    }
    .no-qr {
      text-align: center;
      font-size: 13px;
      line-height: 1.65;
      color: var(--support-muted);
      padding: 8px 4px 0;
    }
    .no-qr code {
      font-size: 12px;
      padding: 2px 6px;
      border-radius: 6px;
      background: #f3f4f6;
    }
    html[data-theme="dark"] .no-qr code {
      background: var(--support-bg-accent);
    }
    .page-footer {
      text-align: center;
      margin-top: 32px;
      padding-top: 20px;
      border-top: 1px solid var(--support-border);
    }
    .page-footer .thanks {
      font-size: 13px;
      color: var(--support-muted);
      margin: 0 0 10px;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
    }
    .page-footer .license-footer {
      margin: 0;
      padding: 0;
      font-size: 12px;
      color: var(--support-faint);
      border: none;
    }
    @media (max-width: 640px) {
      .page { padding: 12px; }
      .card { border-radius: 14px; }
      .support-back-bar { padding: 16px 16px 0; }
      .support-wrap { padding: 16px 16px 24px; }
      .pay-grid { grid-template-columns: 1fr; }
      .pay-grid.single { grid-template-columns: 1fr; }
      .btn { min-width: 0; }
    }
  </style>
</head>
<body>
  <div class="page">
    <div class="card">
      <div class="support-back-bar">
        <a class="btn btn-ghost" href="/">返回配置页</a>
      </div>
      <div class="support-wrap">
    <header class="hero">
      <h1>支持作者</h1>
      <p>您的每一份支持都是我继续创作的动力，感谢您的鼓励与陪伴！</p>
    </header>

    <section class="sheet">
      <h2><span class="emoji" aria-hidden="true">❤️</span>关于作者</h2>
      <p>
        我热爱用技术把复杂的事情变简单。FnMessageBot（日志推送） 面向飞牛 NAS 用户，把日志里的关键事件推到微信、钉钉、飞书、Bark 与 PushPlus 等平台，希望能在你身边默默当好一只「消息守门员」。
        如果这个小工具为你省过时间、少踩过坑，欢迎通过下方二维码随缘打赏——金额随意，心意最重，也欢迎到 GitHub 点个 Star 传播给更多需要的人。
      </p>
      <h2 style="margin-top: 16px;"><span class="emoji" aria-hidden="true">📮</span>联系方式</h2>
      <p style="margin-bottom: 0;">
        1. GitHub 联系：
        <a href="https://github.com/Sunanang/FNMessageBots/issues/3" target="_blank" rel="noopener noreferrer">
          查看 Issue #3 二维码加群
        </a><br />
        2. 邮箱联系：<a href="mailto:1334630986@qq.com">1334630986@qq.com</a><br />
        3. 用户群：查看论坛 FnMessageBots 帖子的置顶评论
        <a href="https://club.fnnas.com/forum.php?mod=viewthread&tid=57252" target="_blank" rel="noopener noreferrer">
          点击前往
        </a>
      </p>
    </section>

    <section>
      <h2 class="pay-section-title"><span class="emoji" aria-hidden="true">🧡</span>选择支付方式</h2>
      {% if wechat_src or ali_src %}
      <div class="pay-grid{% if (wechat_src and not ali_src) or (ali_src and not wechat_src) %} single{% endif %}">
        {% if ali_src %}
        <div class="pay-card pay-card--ali">
          <div class="pay-card-head">
            <span class="pay-badge pay-badge--ali" aria-hidden="true">支</span>
            支付宝
          </div>
          <div class="qr-frame">
            <img src="{{ ali_src }}" alt="支付宝收款码" loading="lazy" decoding="async" />
          </div>
          <p class="pay-caption">使用支付宝扫描二维码进行捐赠</p>
        </div>
        {% endif %}
        {% if wechat_src %}
        <div class="pay-card pay-card--wx">
          <div class="pay-card-head">
            <span class="pay-badge pay-badge--wx" aria-hidden="true">微</span>
            微信支付
          </div>
          <div class="qr-frame">
            <img src="{{ wechat_src }}" alt="微信收款码" loading="lazy" decoding="async" />
          </div>
          <p class="pay-caption">使用微信扫描二维码进行捐赠</p>
        </div>
        {% endif %}
      </div>
      {% else %}
      <p class="no-qr">未找到本地收款码图片。镜像或源码部署需在 <code>assets/icons</code> 下放置 <code>wechat_pay.jpg</code> 与 <code>ali_pay.jpg</code>；也可前往 GitHub 仓库 README 查看。</p>
      {% endif %}
    </section>

        <footer class="page-footer">
          <p class="thanks"><span aria-hidden="true">💛</span>再次感谢您的支持与鼓励！</p>
          <p class="license-footer">© 2024 Sunanang · FnMessageBot · MIT License terms apply.</p>
        </footer>
      </div>
    </div>
  </div>
</body>
</html>"""


FAQ_PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
  {% if favicon_url %}
  <link rel="icon" href="{{ favicon_url }}" />
  {% endif %}
  <title>常见问题 - FnMessageBot</title>
  <style>
    * { box-sizing: border-box; }
    :root {
      color-scheme: light;
      --faq-bg: #eef3ff;
      --faq-bg-accent: #e0e7ff;
      --faq-card: #ffffff;
      --faq-panel: #f9fafb;
      --faq-panel-border: #e5e7eb;
      --faq-text: #1f2933;
      --faq-muted: #6b7280;
      --faq-faint: #9ca3af;
      --faq-accent: #2563eb;
      --faq-border: rgba(148, 163, 184, 0.35);
      --faq-shadow: 0 14px 36px rgba(15, 23, 42, 0.08);
      --faq-shadow-card: 0 8px 28px rgba(15, 23, 42, 0.06);
    }
    html[data-theme="dark"] {
      color-scheme: dark;
      --faq-bg: #111827;
      --faq-bg-accent: #1f2937;
      --faq-card: rgba(17, 24, 39, 0.92);
      --faq-panel: rgba(17, 24, 39, 0.92);
      --faq-panel-border: rgba(75, 85, 99, 0.55);
      --faq-text: #f3f4f6;
      --faq-muted: #9ca3af;
      --faq-faint: #6b7280;
      --faq-accent: #60a5fa;
      --faq-border: rgba(75, 85, 99, 0.55);
      --faq-shadow: 0 18px 40px rgba(0, 0, 0, 0.35);
      --faq-shadow-card: 0 12px 32px rgba(0, 0, 0, 0.25);
    }
    body {
      margin: 0;
      background: var(--faq-bg);
      color: var(--faq-text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, "Noto Sans", "PingFang SC", sans-serif;
      line-height: 1.65;
    }
    a { color: var(--faq-accent); text-decoration: none; }
    a:hover { text-decoration: underline; }
    .page {
      min-height: 100vh;
      display: flex;
      justify-content: center;
      padding: 24px 16px 32px;
      background: linear-gradient(180deg, var(--faq-bg-accent) 0%, var(--faq-bg) 180px);
    }
    .card {
      width: 100%;
      max-width: 960px;
      background: var(--faq-card);
      border-radius: 18px;
      border: 1px solid var(--faq-border);
      box-shadow: var(--faq-shadow);
      overflow: hidden;
    }
    .faq-back-bar {
      padding: 18px 24px 0;
      display: flex;
      justify-content: flex-start;
    }
    .faq-wrap {
      padding: 18px 24px 28px;
    }
    .hero h1 {
      margin: 0;
      font-size: 28px;
      letter-spacing: 0.04em;
    }
    .hero p {
      margin: 8px 0 0;
      color: var(--faq-muted);
      font-size: 14px;
    }
    .faq-list {
      margin-top: 18px;
      display: grid;
      gap: 12px;
    }
    .faq-item {
      background: var(--faq-panel);
      border: 1px solid var(--faq-panel-border);
      border-radius: 12px;
      padding: 14px 14px 12px;
      box-shadow: var(--faq-shadow-card);
    }
    .faq-item h2 {
      margin: 0 0 8px;
      font-size: 16px;
      line-height: 1.4;
    }
    .faq-item p {
      margin: 0;
      color: var(--faq-text);
      font-size: 14px;
      white-space: pre-line;
    }
    code {
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
      font-size: 13px;
      background: rgba(148, 163, 184, 0.18);
      padding: 1px 4px;
      border-radius: 4px;
    }
    pre.faq-pre {
      margin: 8px 0 0;
      padding: 10px 12px;
      overflow-x: auto;
      font-size: 12px;
      line-height: 1.5;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
      background: rgba(148, 163, 184, 0.18);
      border-radius: 8px;
      border: 1px solid var(--faq-panel-border);
      white-space: pre-wrap;
      word-break: break-all;
    }
    .btn {
      min-width: 96px;
      border-radius: 999px;
      padding: 8px 20px;
      border: none;
      font-size: 14px;
      font-weight: 500;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      text-decoration: none;
      transition: background-color .15s, transform .05s;
      font-family: inherit;
    }
    .btn-ghost {
      background: #fff;
      color: #111827;
      border: 1px solid #d1d5db;
    }
    .btn-ghost:hover { background: #f3f4f6; }
    html[data-theme="dark"] .btn-ghost {
      background: #111827;
      color: #e5e7eb;
      border-color: #4b5563;
    }
    html[data-theme="dark"] .btn-ghost:hover { background: #1f2937; }
    .page-footer {
      margin-top: 20px;
      padding-top: 14px;
      border-top: 1px solid var(--faq-border);
      text-align: center;
      color: var(--faq-faint);
      font-size: 12px;
    }
    @media (max-width: 640px) {
      .faq-back-bar { padding: 16px 16px 0; }
      .faq-wrap { padding: 16px 16px 24px; }
      .hero h1 { font-size: 24px; }
      .btn { min-width: 0; }
    }
  </style>
</head>
<body>
  <div class="page">
    <div class="card">
      <div class="faq-back-bar">
        <a class="btn btn-ghost" href="/">返回配置页</a>
      </div>
      <div class="faq-wrap">
        <header class="hero">
          <h1>常见问题</h1>
        </header>

        <section class="faq-list">
          <article class="faq-item">
            <h2>1. 渠道说明</h2>
            <p>查看：<a href="https://github.com/Sunanang/FNMessageBots/blob/main/docs/notification-channels.md" target="_blank" rel="noopener noreferrer">notification-channels.md</a></p>
          </article>

          <article class="faq-item">
            <h2>2. 忘记密码</h2>
            <p>编辑配置文件 <code>config/config.json</code>，删除 <code>web_password_salt</code> 和 <code>web_password_hash</code>。</p>
          </article>

          <article class="faq-item">
            <h2>3. 无法安装</h2>
            <p>目前存在部分用户在应用市场无法安装，提示 python 异常或 pip 异常。这个问题受系统环境影响，暂时无法完全规避。
可尝试：
一：直接使用 Docker 版本，通常可稳定安装运行。
二：切换系统下载源，并开启科学网络，多尝试几次，大概率可下载 python 成功。</p>
          </article>

          <article class="faq-item">
            <h2>4. Docker 名称和应用市场名称</h2>
            <p>飞牛应用市场名称：<code>日志推送</code>
Docker 镜像名称：<code>fn-message-bots</code>
说明：Docker 镜像通常会先发布（修复与功能更新更快），稳定后再提审飞牛应用市场，所以应用市场版本一般会比 Docker 晚约一周。</p>
          </article>

          <article class="faq-item">
            <h2>5. 排查问题优先看日志</h2>
            <p>排查问题建议先确认飞牛日志是否与推送内容一致；若不一致，再定位为项目侧问题。
如果你希望新增推送格式，请尽量附上数据库记录格式和日志截图，便于快速支持。</p>
          </article>

          <article class="faq-item">
            <h2>6. 关于备份库 / 影视 / 照片事件说明</h2>
            <p>备份库 / 影视 / 照片事件默认不勾选，需要时请手动开启。
它们对应不同数据库，只有勾选对应推送事件时，项目才会轮询对应数据库；未勾选则不会轮询。</p>
          </article>

          <article class="faq-item">
            <h2>7. 联系作者</h2>
            <p>前往 <a href="/support">支持作者</a> 页面查看联系方式与支持信息。</p>
          </article>

          <article class="faq-item">
            <h2>8. 端口说明</h2>
            <p>Docker 版默认端口：<code>18080</code>（可自行修改）
应用市场版默认端口：<code>18230</code>（不可修改）</p>
          </article>

          <article class="faq-item">
            <h2>9. 测试推送没问题，但是无法推送日志消息</h2>
            <p>这个问题大概率是数据库存在异常。Docker 版可先查看运行日志，是否提示：<code>数据库查询失败：database disk image is malformed</code>。
如果出现这个提示，基本可以判定是数据库挂载失败；再检查日志显示是否正常，大概率是日志数据库的问题。
修复命令（仅供参考，建议通过 SSH 执行）：
<code>sudo -i</code>
<code>systemctl stop eventlogger_service</code>
<code>rm /usr/trim/var/eventlogger_service/logger_data.db3</code>
<code>chown -R trim:trim /usr/trim/var/eventlogger_service/</code>
<code>systemctl start eventlogger_service</code>
<code>systemctl status eventlogger_service</code></p>
          </article>

          <article class="faq-item">
            <h2>10. 挂载影视库 / 相册提示权限问题</h2>
            <p><strong>Docker 版：</strong>请在编排文件（如 <code>docker-compose.yml</code>）中自行添加卷挂载，使容器能访问宿主上的影视 / 相册数据路径；权限与 UID/GID、挂载只读等均在您本机配置中处理。
<strong>应用市场版：</strong>若读取影视 / 相册相关数据库时报权限不足，可在 SSH（root）下按需执行下列命令。<strong>请自行评估放宽目录权限、数据库文件权限以及 ACL 带来的安全风险</strong>（例如更多用户可进入目录或读取数据库文件）；系统需支持 <code>setfacl</code>（ACL），否则请先安装/启用相关功能。

<pre class="faq-pre">sudo chmod 755 /usr/local/apps/@appdata/trim.media/database /usr/local/apps/@appdata/trim.photos/db
sudo chmod 644 /usr/local/apps/@appdata/trim.media/database/*.db* /usr/local/apps/@appdata/trim.photos/db/*.db* 2>/dev/null

sudo setfacl -m u:FnMessageBot:rx /usr/local/apps/@appdata/trim.media/database
sudo setfacl -m u:FnMessageBot:r /usr/local/apps/@appdata/trim.media/database/trimmedia.db
sudo setfacl -m u:FnMessageBot:r /usr/local/apps/@appdata/trim.media/database/trimactivity.db</pre>
若相册侧仍有权限错误，可对 <code>trim.photos/db</code> 下实际使用的 <code>.db</code> 文件按同样方式为 <code>FnMessageBot</code> 增加只读 ACL（示例：<code>sudo setfacl -m u:FnMessageBot:r &lt;文件路径&gt;</code>）。</p>
          </article>
        </section>

        <footer class="page-footer">
          © 2024 Sunanang · FnMessageBot · MIT License terms apply.
        </footer>
      </div>
    </div>
  </div>
  <script>
    (function () {
      var key = "fnmb_theme";
      var mode = localStorage.getItem(key);
      var resolved = (mode === "dark") ? "dark" : "light";
      document.documentElement.setAttribute("data-theme", resolved);
    })();
  </script>
</body>
</html>"""

