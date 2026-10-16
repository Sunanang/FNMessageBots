// 浏览器验证密码开关、保存与超时；仅模拟接口，不连接 NAS 或推送渠道。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.FNMB_PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'src/web/templates/index.html'), 'utf8')
  .replace("{{ url_for('session_guard_js') }}", 'session-guard.js')
  .replace(/\{%[\s\S]*?%\}/g, '').replace(/\{\{[\s\S]*?\}\}/g, '');

(async () => {
  const browser = await chromium.launch({headless: true,
    ...(process.env.FNMB_BROWSER_EXECUTABLE ? {executablePath: process.env.FNMB_BROWSER_EXECUTABLE} : {})});
  try {
    for (const prefix of ['', '/app/FnMessageBot']) {
      const page = await browser.newPage({viewport: {width: 1100, height: 850}});
      const errors = [];
      page.on('pageerror', error => errors.push(error.message));
      let enabled = true, signedIn = true, savedMode, activityRequests = 0;
      await page.clock.install();
      await page.route('**/*', async route => {
        const url = new URL(route.request().url());
        let response = {ok: true};
        if (url.pathname.endsWith('/session-guard.js')) {
          await route.fulfill({contentType: 'application/javascript',
            body: fs.readFileSync(path.join(root, 'src/web/static/session-guard.js'), 'utf8')});
          return;
        }
        if (url.pathname.endsWith('/api/auth/status') || url.pathname.endsWith('/api/auth/activity')) {
          if (url.pathname.endsWith('/activity')) activityRequests += 1;
          response = {ok: true, password_enabled: enabled, authenticated: !enabled || signedIn,
            need_login: enabled && !signedIn, remaining_seconds: enabled ? 900 : null};
        } else if (url.pathname.endsWith('/api/config')) {
          response = {ok: true, data: {version: '2.7.0', web_password_enabled: enabled,
            selected_events: ['LoginSucc'], events_by_category: [{name: '系统',
              events: [{id: 'LoginSucc', title: '登录成功'}]}],
            channels: [{type: 'wechat', url: 'https://example.invalid/webhook', enabled: true}],
            channel_options: [{id: 'wechat', name: '企业微信'}]}};
        } else if (url.pathname.endsWith('/api/save-config')) {
          savedMode = route.request().postDataJSON().web_password_enabled;
          if (enabled !== savedMode) signedIn = false;
          enabled = savedMode;
        } else if (url.pathname.endsWith('/api/push-stats')) response.data = {};
        else if (url.pathname.endsWith('/')) {
          await route.fulfill({contentType: 'text/html', body: html});
          return;
        }
        await route.fulfill({contentType: 'application/json', body: JSON.stringify(response)});
      });
      await page.goto(`http://fnmb.test${prefix}/`);
      const toggle = page.getByLabel('启用访问密码', {exact: true});
      await toggle.waitFor({state: 'visible'});
      assert.equal(await toggle.isChecked(), true);
      assert.equal(await page.locator('#app-version').textContent(), '2.7.0');
      await toggle.uncheck();
      await page.locator('#save-btn').click();
      await page.waitForFunction(() => !document.querySelector('#save-btn').disabled);
      assert.equal(savedMode, false);
      await page.reload();
      await toggle.waitFor({state: 'visible'});
      assert.equal(await toggle.isChecked(), false);
      const beforeIdle = activityRequests;
      await page.clock.runFor(16 * 60 * 1000);
      assert.equal(await page.locator('#app-main').isVisible(), true);
      assert.equal(await page.locator('#auth-gate').isVisible(), false);
      assert.equal(activityRequests, beforeIdle);
      await toggle.check();
      await page.locator('#save-btn').click();
      await page.locator('#auth-login').waitFor({state: 'visible'});
      assert.equal(savedMode, true);
      assert.equal(await page.locator('#app-main').isVisible(), false);
      assert.deepEqual(errors, []);
      await page.close();
    }
    console.log('Password mode UI passed: Docker + gateway, save, reload, 16 minute idle, restore login.');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
