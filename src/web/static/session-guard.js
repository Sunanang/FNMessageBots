/* 应用密码会话：真实交互续期，后台读取不续期，所有页面统一处理失效。 */
(() => {
  "use strict";
  const originalFetch = window.fetch.bind(window);
  const home = new URL("./", window.location.href);
  const apiBase = new URL("api/", home);
  const TOKEN_KEY = "fnmb_session_token";
  const SESSION_HEADER = "X-Fnmb-Session";
  let active = false;
  let passwordEnabled = true;
  let timer = null;
  let lastActivitySent = 0;
  let activityPending = false;
  let revision = 0;

  function normalizePassword(value) {
    const smart = {
      "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
      "\u201c": '"', "\u201d": '"', "\u201e": '"',
      "\u2013": "-", "\u2014": "-", "\u2212": "-", "\u2026": "...",
    };
    return String(value ?? "")
      .normalize("NFKC")
      .replace(/[\u00ad\u034f\u180e\u200b-\u200d\u2060\ufeff]/g, "")
      .replace(/[\u2018\u2019\u201a\u201b\u201c\u201d\u201e\u2013\u2014\u2212\u2026]/g, ch => smart[ch])
      .trim();
  }

  function chooseSubmittedPassword(typed, current) {
    const entered = String(typed ?? "");
    const live = String(current ?? "");
    // 输入框已被清空时不再沿用旧草稿，避免退出后误提交上一次的密码。
    if (!entered || !live) return live;
    // 失焦时输入法只补上末尾；系统若把整段换成另一个密码，则保留用户实际输入。
    if (live.startsWith(entered)) return live;
    return entered;
  }

  function readToken() {
    try { return window.sessionStorage.getItem(TOKEN_KEY) || ""; }
    catch (_) { return ""; }
  }

  function remember(token) {
    try {
      if (token) window.sessionStorage.setItem(TOKEN_KEY, String(token));
      else window.sessionStorage.removeItem(TOKEN_KEY);
    } catch (_) { /* 隐私模式可能禁用 sessionStorage */ }
  }

  function forget() {
    remember("");
  }

  function withSession(options) {
    const opts = options ? Object.assign({}, options) : {};
    const headers = new Headers(opts.headers || {});
    const token = readToken();
    if (token && !headers.has(SESSION_HEADER)) headers.set(SESSION_HEADER, token);
    opts.headers = headers;
    if (!opts.credentials) opts.credentials = "include";
    return opts;
  }

  function apiFetch(input, options) {
    if (input instanceof Request) {
      const headers = new Headers(input.headers);
      const token = readToken();
      if (token && !headers.has(SESSION_HEADER)) headers.set(SESSION_HEADER, token);
      return originalFetch(new Request(input, {headers, credentials: input.credentials || "include"}), options);
    }
    return originalFetch(input, withSession(options));
  }

  function setAuthFormEnabled(formId, enabled) {
    const form = document.getElementById(formId);
    if (!form || !form.querySelectorAll) return;
    form.querySelectorAll("input, button").forEach(field => { field.disabled = !enabled; });
  }

  function expired() {
    revision += 1;
    active = false;
    clearTimeout(timer);
    timer = null;
    forget();
    const main = document.getElementById("app-main");
    if (!main) {
      // 二级页同样自动返回密码页，保留飞牛网关前缀。
      document.body.style.visibility = "hidden";
      window.location.replace(home.href);
      return;
    }
    main.style.display = "none";
    document.getElementById("auth-gate").style.display = "flex";
    document.getElementById("auth-set-password").style.display = "none";
    document.getElementById("auth-login").style.display = "block";
    setAuthFormEnabled("form-set-password", false);
    setAuthFormEnabled("form-login", true);
    document.getElementById("login-password").value = "";
    const message = document.getElementById("auth-login-msg");
    message.textContent = "会话已过期，请重新输入密码。未保存的配置不会自动提交。";
    message.className = "auth-msg error";
    window.dispatchEvent(new Event("fnmb-session-expired"));
  }

  function start(data) {
    revision += 1;
    clearTimeout(timer);
    timer = null;
    if (data && data.session_token) remember(data.session_token);
    active = !!data.authenticated;
    passwordEnabled = data.password_enabled !== false;
    if (!active) {
      if (data && data.authenticated === false) forget();
      return;
    }
    if (!passwordEnabled) return;
    // 旧版服务缺少剩余时间时也保留默认超时；NaN/非有限值不能立刻锁定页面。
    let remaining = Number(data.remaining_seconds);
    if (!Number.isFinite(remaining)) remaining = Number(data.idle_seconds);
    if (!Number.isFinite(remaining)) remaining = 900;
    if (remaining <= 0) { expired(); return; }
    timer = setTimeout(expired, remaining * 1000);
  }

  async function check() {
    if (!active && document.getElementById("app-main")) return;
    const requestRevision = revision;
    try {
      const response = await apiFetch(new URL("auth/status", apiBase), {credentials: "include", cache: "no-store"});
      const data = await response.json();
      if (!response.ok || !data.ok) return;
      // 旧请求不能覆盖刚登录、刚续期或已经锁定后的状态。
      if (requestRevision !== revision || activityPending) return;
      if (!data.authenticated) { expired(); return; }
      start(data);
    } catch (_) { /* 断网不续期，已有定时器仍会锁定页面。 */ }
  }

  async function activity(event) {
    if (!active || !passwordEnabled || !event.isTrusted || document.visibilityState !== "visible" || activityPending) return;
    const now = Date.now();
    if (now - lastActivitySent < 30000) return;
    lastActivitySent = now;
    activityPending = true;
    const requestRevision = revision;
    try {
      const response = await apiFetch(new URL("auth/activity", apiBase), {
        method: "POST", credentials: "include", headers: {"Content-Type": "application/json"}, body: "{}",
      });
      if (response.status === 401) {
        if (requestRevision === revision) expired();
        return;
      }
      const data = await response.json();
      if (active && requestRevision === revision && response.ok && data.ok) start(data);
    } catch (_) { /* 请求失败不会延长登录时间。 */ }
    finally { activityPending = false; }
  }

  window.fetch = async function(input, options) {
    const requestRevision = revision;
    const url = new URL(input instanceof Request ? input.url : String(input), window.location.href);
    const ourApi = url.origin === apiBase.origin && url.pathname.startsWith(apiBase.pathname);
    const response = await (ourApi ? apiFetch(input, options) : originalFetch(input, options));
    const protectedApi = ourApi
      && !["auth/login", "auth/set-password", "auth/status", "auth/logout"].includes(url.pathname.slice(apiBase.pathname.length));
    if (protectedApi && response.status === 401) {
      if (requestRevision === revision) expired();
      else check();
    }
    else if (protectedApi && response.ok && (options?.method || (input instanceof Request ? input.method : "GET")).toUpperCase() === "POST") check();
    return response;
  };
  window.fnmbSecret = {chooseSubmittedPassword, normalizePassword};
  window.fnmbSession = {start, check, expired, remember, forget};
  for (const name of ["pointerdown", "pointermove", "keydown", "input", "scroll", "touchstart"]) {
    document.addEventListener(name, activity, {capture: true, passive: true});
  }
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") check();
  });
  window.addEventListener("focus", check);
  window.addEventListener("pageshow", event => {
    if (event.persisted && document.getElementById("app-main")) {
      document.getElementById("app-main").style.display = "none";
      window.location.reload();
    } else if (event.persisted) check();
  });
  document.addEventListener("DOMContentLoaded", () => {
    if (!document.getElementById("app-main")) check();
    setInterval(check, 15000);
  });
})();
