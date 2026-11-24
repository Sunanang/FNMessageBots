/* FnMessageBot 自定义弹窗：确认 / 提示 / 输入。禁止依赖系统 alert/confirm/prompt。 */
(() => {
  "use strict";

  const STYLE_ID = "fnmb-app-dialog-style";
  const ROOT_ID = "fnmb-app-dialog";
  let resolver = null;
  let previousFocus = null;
  let mode = "confirm"; // confirm | alert | prompt

  function ensureStyle() {
    if (document.getElementById(STYLE_ID)) return;
    const style = document.createElement("style");
    style.id = STYLE_ID;
    style.textContent = `
#${ROOT_ID}{
  position:fixed;inset:0;z-index:120;display:none;align-items:center;justify-content:center;
  padding:24px 16px;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,"Noto Sans","PingFang SC",sans-serif;
}
#${ROOT_ID}.open{display:flex}
#${ROOT_ID} .fnmb-dlg-backdrop{position:absolute;inset:0;background:rgba(15,23,42,.5)}
#${ROOT_ID} .fnmb-dlg-panel{
  position:relative;width:min(400px,100%);background:rgba(255,255,255,.96);
  border:1px solid rgba(148,163,184,.35);border-radius:16px;
  box-shadow:0 18px 40px rgba(15,23,42,.22);backdrop-filter:blur(10px);padding:22px 22px 18px;
}
#${ROOT_ID} .fnmb-dlg-title{font-size:16px;font-weight:650;color:#111827;margin:0 0 8px}
#${ROOT_ID} .fnmb-dlg-msg{font-size:14px;line-height:1.55;color:#4b5563;margin:0 0 18px;white-space:pre-line}
#${ROOT_ID} .fnmb-dlg-input{
  width:100%;box-sizing:border-box;margin:-6px 0 18px;padding:8px 10px;border-radius:8px;
  border:1px solid #d1d5db;font-size:14px;outline:none;background:#fff;color:#111827;
}
#${ROOT_ID} .fnmb-dlg-input:focus{border-color:#2563eb;box-shadow:0 0 0 1px rgba(37,99,235,.25)}
#${ROOT_ID} .fnmb-dlg-actions{display:flex;justify-content:flex-end;gap:10px;flex-wrap:wrap}
#${ROOT_ID} .fnmb-dlg-btn{
  min-width:96px;border-radius:999px;padding:8px 20px;border:none;font-size:14px;font-weight:500;
  cursor:pointer;display:inline-flex;align-items:center;justify-content:center;
  transition:background-color .15s,box-shadow .15s,transform .05s;
}
#${ROOT_ID} .fnmb-dlg-btn-primary{
  background:linear-gradient(135deg,#2563eb,#1d4ed8);color:#fff;box-shadow:0 12px 22px rgba(37,99,235,.28);
}
#${ROOT_ID} .fnmb-dlg-btn-primary:hover{background:linear-gradient(135deg,#1d4ed8,#1e40af);transform:translateY(-1px)}
#${ROOT_ID} .fnmb-dlg-btn-ghost{background:#fff;color:#111827;border:1px solid #d1d5db}
#${ROOT_ID} .fnmb-dlg-btn-ghost:hover{background:#f3f4f6}
#${ROOT_ID} .fnmb-dlg-cancel[hidden],#${ROOT_ID} .fnmb-dlg-input-wrap[hidden]{display:none!important}
html[data-theme="dark"] #${ROOT_ID} .fnmb-dlg-panel{background:rgba(17,24,39,.96);border-color:#374151}
html[data-theme="dark"] #${ROOT_ID} .fnmb-dlg-title{color:#f9fafb}
html[data-theme="dark"] #${ROOT_ID} .fnmb-dlg-msg{color:#9ca3af}
html[data-theme="dark"] #${ROOT_ID} .fnmb-dlg-input{background:#0f172a;border-color:#4b5563;color:#e5e7eb}
html[data-theme="dark"] #${ROOT_ID} .fnmb-dlg-btn-ghost{background:#111827;color:#e5e7eb;border-color:#4b5563}
html[data-theme="dark"] #${ROOT_ID} .fnmb-dlg-btn-ghost:hover{background:#1f2937}
`;
    document.head.appendChild(style);
  }

  function ensureDom() {
    ensureStyle();
    let root = document.getElementById(ROOT_ID);
    if (root) return root;
    root = document.createElement("div");
    root.id = ROOT_ID;
    root.setAttribute("aria-hidden", "true");
    root.innerHTML = `
      <div class="fnmb-dlg-backdrop" data-fnmb-dlg="backdrop"></div>
      <div class="fnmb-dlg-panel" role="alertdialog" aria-modal="true" aria-labelledby="fnmb-dlg-title" aria-describedby="fnmb-dlg-msg">
        <div class="fnmb-dlg-title" id="fnmb-dlg-title">确认</div>
        <p class="fnmb-dlg-msg" id="fnmb-dlg-msg"></p>
        <div class="fnmb-dlg-input-wrap" data-fnmb-dlg="input-wrap" hidden>
          <input class="fnmb-dlg-input" id="fnmb-dlg-input" type="text" autocomplete="off" />
        </div>
        <div class="fnmb-dlg-actions">
          <button type="button" class="fnmb-dlg-btn fnmb-dlg-btn-ghost fnmb-dlg-cancel" data-fnmb-dlg="cancel">取消</button>
          <button type="button" class="fnmb-dlg-btn fnmb-dlg-btn-primary" data-fnmb-dlg="ok">确定</button>
        </div>
      </div>`;
    const mount = () => {
      if (!document.getElementById(ROOT_ID)) document.body.appendChild(root);
    };
    if (document.body) mount();
    else document.addEventListener("DOMContentLoaded", mount, { once: true });
    root.querySelector('[data-fnmb-dlg="ok"]').addEventListener("click", () => dismiss(true));
    root.querySelector('[data-fnmb-dlg="cancel"]').addEventListener("click", () => dismiss(false));
    root.querySelector('[data-fnmb-dlg="backdrop"]').addEventListener("click", () => {
      if (mode === "alert") dismiss(true);
      else dismiss(false);
    });
    document.addEventListener("keydown", (e) => {
      if (!isOpen()) return;
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        if (mode === "alert") dismiss(true);
        else dismiss(false);
      } else if (e.key === "Enter") {
        const tag = (e.target && e.target.tagName) || "";
        if (tag === "TEXTAREA") return;
        e.preventDefault();
        dismiss(true);
      }
    }, true);
    return root;
  }

  function isOpen() {
    const root = document.getElementById(ROOT_ID);
    return !!(root && root.classList.contains("open"));
  }

  function dismiss(ok) {
    const root = document.getElementById(ROOT_ID);
    if (root) {
      root.classList.remove("open");
      root.setAttribute("aria-hidden", "true");
    }
    const resolve = resolver;
    resolver = null;
    const prev = previousFocus;
    previousFocus = null;
    let value = !!ok;
    if (mode === "prompt") {
      const input = document.getElementById("fnmb-dlg-input");
      value = ok && input ? input.value : null;
    } else if (mode === "alert") {
      value = true;
    }
    if (prev && typeof prev.focus === "function") {
      try { prev.focus(); } catch (_) { /* ignore */ }
    }
    if (resolve) resolve(value);
  }

  function openDialog(opts) {
    const options = opts || {};
    return new Promise((resolve) => {
      if (resolver) {
        const pending = resolver;
        resolver = null;
        pending(mode === "prompt" ? null : false);
      }
      const root = ensureDom();
      const titleEl = root.querySelector("#fnmb-dlg-title");
      const msgEl = root.querySelector("#fnmb-dlg-msg");
      const okBtn = root.querySelector('[data-fnmb-dlg="ok"]');
      const cancelBtn = root.querySelector('[data-fnmb-dlg="cancel"]');
      const inputWrap = root.querySelector('[data-fnmb-dlg="input-wrap"]');
      const input = root.querySelector("#fnmb-dlg-input");

      mode = options.mode || "confirm";
      previousFocus = document.activeElement;
      resolver = resolve;
      titleEl.textContent = options.title || (mode === "alert" ? "提示" : "确认");
      msgEl.textContent = options.message || "";
      okBtn.textContent = options.confirmText || "确定";
      cancelBtn.textContent = options.cancelText || "取消";
      cancelBtn.hidden = mode === "alert";
      inputWrap.hidden = mode !== "prompt";
      if (mode === "prompt" && input) {
        input.value = options.defaultValue != null ? String(options.defaultValue) : "";
        input.placeholder = options.placeholder || "";
      }
      root.classList.add("open");
      root.setAttribute("aria-hidden", "false");
      setTimeout(() => {
        try {
          if (mode === "prompt" && input) input.focus();
          else if (mode === "alert") okBtn.focus();
          else cancelBtn.focus();
        } catch (_) { /* ignore */ }
      }, 0);
    });
  }

  function confirm(opts) {
    if (typeof opts === "string") opts = { message: opts };
    return openDialog(Object.assign({}, opts, { mode: "confirm" }));
  }

  function alert(opts) {
    if (typeof opts === "string") opts = { message: opts };
    return openDialog(Object.assign({}, opts, { mode: "alert" }));
  }

  function prompt(opts) {
    if (typeof opts === "string") opts = { message: opts };
    return openDialog(Object.assign({}, opts, { mode: "prompt" }));
  }

  window.fnmbDialog = {
    confirm,
    alert,
    prompt,
    isOpen,
    dismiss,
  };

  // 禁用系统弹窗：alert 走自定义提示；confirm/prompt 同步 API 无法等待用户，一律安全拒绝
  window.alert = function (message) {
    void alert({ message: String(message ?? "") });
  };
  window.confirm = function () {
    console.error("[FnMessageBot] window.confirm 已禁用，请使用 await fnmbDialog.confirm(...)");
    return false;
  };
  window.prompt = function () {
    console.error("[FnMessageBot] window.prompt 已禁用，请使用 await fnmbDialog.prompt(...)");
    return null;
  };
})();
