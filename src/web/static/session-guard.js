/* 应用密码会话：真实交互续期，后台读取不续期，所有页面统一处理失效。 */
(() => {
  "use strict";
  const originalFetch = window.fetch.bind(window);
  const home = new URL("./", window.location.href);
  const apiBase = new URL("api/", home);
  let active = false;
  let timer = null;
  let lastActivitySent = 0;
  let activityPending = false;
  let revision = 0;

  function expired() {
    revision += 1;
    active = false;
    clearTimeout(timer);
    timer = null;
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
    active = !!data.authenticated;
    if (!active) return;
    // 旧版服务缺少剩余时间时也保留默认超时；当前后端始终返回剩余秒数。
    const remaining = Number(data.remaining_seconds ?? data.idle_seconds ?? 900);
    if (remaining <= 0) { expired(); return; }
    timer = setTimeout(expired, remaining * 1000);
  }

  async function check() {
    if (!active && document.getElementById("app-main")) return;
    const requestRevision = revision;
    try {
      const response = await originalFetch(new URL("auth/status", apiBase), {credentials: "include", cache: "no-store"});
      const data = await response.json();
      if (!response.ok || !data.ok) return;
      // 旧请求不能覆盖刚登录、刚续期或已经锁定后的状态。
      if (requestRevision !== revision || activityPending) return;
      if (!data.authenticated) { expired(); return; }
      start(data);
    } catch (_) { /* 断网不续期，已有定时器仍会锁定页面。 */ }
  }

  async function activity(event) {
    if (!active || !event.isTrusted || document.visibilityState !== "visible" || activityPending) return;
    const now = Date.now();
    if (now - lastActivitySent < 30000) return;
    lastActivitySent = now;
    activityPending = true;
    const requestRevision = revision;
    try {
      const response = await originalFetch(new URL("auth/activity", apiBase), {
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
    const response = await originalFetch(input, options);
    const url = new URL(input instanceof Request ? input.url : String(input), window.location.href);
    const protectedApi = url.origin === apiBase.origin && url.pathname.startsWith(apiBase.pathname)
      && !["auth/login", "auth/set-password", "auth/status", "auth/logout"].includes(url.pathname.slice(apiBase.pathname.length));
    if (protectedApi && response.status === 401) {
      if (requestRevision === revision) expired();
      else check();
    }
    else if (protectedApi && response.ok && (options?.method || (input instanceof Request ? input.method : "GET")).toUpperCase() === "POST") check();
    return response;
  };
  window.fnmbSession = {start, check, expired};
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
