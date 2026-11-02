// 真实浏览器覆盖主题关闭重开、网关入口及被禁用的 localStorage；不访问 NAS。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require(process.env.FNMB_PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const read = name => fs.readFileSync(path.join(root, name), 'utf8');
const shots = process.env.FNMB_THEME_SCREENSHOTS || '/private/tmp/fnmb-theme-shots';

(async () => {
  const browser = await chromium.launch({headless: true,
    ...(process.env.FNMB_BROWSER_EXECUTABLE ? {executablePath: process.env.FNMB_BROWSER_EXECUTABLE} : {})});
  fs.mkdirSync(shots, {recursive: true});
  try {
    for (const prefix of ['', '/app/FnMessageBot']) {
      let saved = '', failSave = false, posts = 0;
      const open = async (blocked = false) => {
        const context = await browser.newContext({viewport: {width: 1100, height: 850}});
        if (blocked) await context.addInitScript(() => Object.defineProperty(window, 'localStorage', {
          get() { throw new DOMException('Storage blocked', 'SecurityError'); },
        }));
        const page = await context.newPage();
        const errors = [];
        page.on('pageerror', error => errors.push(error.message));
        await page.route('**/*', async route => {
          const url = new URL(route.request().url());
          const target = url.pathname.slice(prefix.length);
          if (target === '/theme.js' || target === '/session-guard.js') {
            return route.fulfill({contentType: 'application/javascript', body: read(`src/web/static${target}`)});
          }
          if (target === '/assets/icons/app-icon.png') {
            return route.fulfill({contentType: 'image/png', body: fs.readFileSync(path.join(root, 'assets/icons/app-icon.png'))});
          }
          const pages = {'/': 'index', '/faq': 'faq', '/history': 'history', '/support': 'support'};
          if (pages[target]) {
            const html = read(`src/web/templates/${pages[target]}.html`)
              .replace('{{ saved_theme }}', saved)
              .replace('{{ icon_url }}', 'assets/icons/app-icon.png')
              .replaceAll("{{ url_for('session_guard_js') }}", 'session-guard.js')
              .replace(/\{%[\s\S]*?%\}/g, '').replace(/\{\{[\s\S]*?\}\}/g, '');
            return route.fulfill({contentType: 'text/html', body: html});
          }
          let response = {ok: true}, status = 200;
          if (target.startsWith('/api/auth/')) response = {ok: true, password_enabled: false, authenticated: true};
          else if (target.startsWith('/api/config')) response.data = {
            web_password_enabled: false, selected_events: ['LoginSucc'],
            events_by_category: [{name: '系统', events: [{id: 'LoginSucc', title: '登录成功'}]}],
            channels: [], channel_options: [],
          };
          else if (target === '/api/ui-preferences') {
            posts++;
            if (failSave) { status = 500; response = {ok: false, message: '主题保存失败：disk full'}; }
            else { saved = route.request().postDataJSON().theme; response.theme = saved; }
          } else if (target.startsWith('/api/push-history')) response.data = {records: [], total: 0};
          else response.data = {};
          await route.fulfill({status, contentType: 'application/json', body: JSON.stringify(response)});
        });
        await page.goto(`http://fnmb.test${prefix}/`);
        await page.locator('#theme-toggle-btn').waitFor({state: 'visible'});
        return {context, page, errors};
      };

      let view = await open(true);
      assert.equal(await view.page.locator('html').getAttribute('data-theme'), 'light');
      const saveResponse = view.page.waitForResponse(r => r.url().endsWith('/api/ui-preferences') && r.status() === 200);
      await view.page.locator('#theme-toggle-btn').click();
      await saveResponse;
      await view.page.waitForFunction(() => document.documentElement.getAttribute('data-theme') === 'dark');
      assert.equal(saved, 'dark');
      assert.deepEqual(view.errors, []);
      await view.page.screenshot({path: path.join(shots, prefix ? 'gateway-dark.png' : 'direct-dark.png'), fullPage: true});
      await view.context.close();

      view = await open(true); // 新浏览器上下文，任何旧 localStorage 都不存在。
      assert.equal(await view.page.locator('html').getAttribute('data-theme'), 'dark');
      const before = posts;
      for (const child of ['faq', 'history', 'support']) {
        await view.page.goto(`http://fnmb.test${prefix}/${child}`);
        assert.equal(await view.page.locator('html').getAttribute('data-theme'), 'dark');
      }
      assert.equal(posts, before); // 打开、浏览不触发配置写入。
      assert.deepEqual(view.errors, []);
      await view.context.close();

      view = await open();
      await view.page.locator('#theme-toggle-btn').click();
      await view.page.waitForFunction(() => document.documentElement.getAttribute('data-theme') === 'light');
      await view.page.waitForFunction(() => localStorage.getItem('fnmb_theme') === 'light');
      assert.equal(saved, 'light');
      failSave = true;
      await view.page.locator('#theme-toggle-btn').click();
      await view.page.waitForFunction(() => document.querySelector('#status-bar').textContent.includes('主题保存失败'));
      assert.equal(await view.page.locator('html').getAttribute('data-theme'), 'light');
      assert.equal(saved, 'light');
      assert.deepEqual(view.errors, []);
      await view.context.close();
    }
    console.log(`Theme UI passed: reopen, blocked storage, gateway, child pages and save failure. Screenshots: ${shots}`);
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exitCode = 1;});
