/* 外观偏好：服务端持久化优先，浏览器缓存仅作兼容与快速回显。 */
(() => {
  "use strict";
  const root = document.documentElement;
  const key = "fnmb_theme";
  const valid = value => value === "dark" || value === "light";
  let localMode;
  try { localMode = localStorage.getItem(key); } catch (_) { /* 部分 NAS WebView 禁止本地存储 */ }
  const saved = root.getAttribute("data-saved-theme");
  let mode = valid(saved) ? saved : valid(localMode) ? localMode : "light";
  let committed = mode;
  let revision = 0;
  let pending = Promise.resolve();

  function cache(value) {
    try { localStorage.setItem(key, value); } catch (_) { /* 服务端仍保存偏好 */ }
  }

  function apply(value) {
    mode = value;
    root.setAttribute("data-theme", mode);
    const button = document.getElementById("theme-toggle-btn");
    if (button) {
      button.textContent = mode === "dark" ? "☀" : "🌙";
      button.title = mode === "dark" ? "切换到浅色模式" : "切换到深色模式";
      button.setAttribute("aria-label", button.title);
    }
  }

  function toggle() {
    const next = mode === "dark" ? "light" : "dark";
    const ownRevision = ++revision;
    apply(next);
    pending = pending.then(async () => {
      const response = await fetch("api/ui-preferences", {
        method: "POST", credentials: "include", keepalive: true,
        headers: {"Content-Type": "application/json"}, body: JSON.stringify({theme: next}),
      });
      const body = await response.json();
      if (!response.ok || !body.ok) throw new Error(body.message || "主题保存失败");
      committed = next;
      cache(next);
    }).catch(error => {
      if (revision === ownRevision) apply(committed);
      window.dispatchEvent(new CustomEvent("fnmb-theme-error", {detail: error.message}));
    });
  }

  window.fnmbTheme = {
    bindToggle() {
      const button = document.getElementById("theme-toggle-btn");
      apply(mode);
      if (button && !button.dataset.themeBound) {
        button.dataset.themeBound = "true";
        button.addEventListener("click", toggle);
      }
    },
  };
  apply(mode);
})();
