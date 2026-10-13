// 用 Playwright 验证实际页面脚本；所有 API 被模拟，不保存配置、不发送推送。
// 可通过 FNMB_PLAYWRIGHT_MODULE / FNMB_BROWSER_EXECUTABLE 指定已有的测试运行时。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(process.env.FNMB_PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const source = fs.existsSync(path.join(root, 'src')) ? path.join(root, 'src') : path.join(root, 'cmd/fnmessagebots/src');
const html = fs.readFileSync(path.join(source, 'web/templates/index.html'), 'utf8')
  .replace("{{ url_for('session_guard_js') }}", 'session-guard.js')
  .replace(/\{%[\s\S]*?%\}/g, '').replace(/\{\{[\s\S]*?\}\}/g, '');
const commands = "sudo setfacl -m u:FnMessageBot:r '/path with spaces/db'\necho '<script>unsafe</script>'";
const fixture = process.env.FNMB_WARNING_FIXTURE
  ? JSON.parse(fs.readFileSync(process.env.FNMB_WARNING_FIXTURE, 'utf8')) : null;
const details = fixture ? fixture.save.warning_details : [{title: '任务计划库', message: '权限不足 <img src=x onerror="window.fnmbXss=1">',
  action: '执行授权后重新保存检查。', commands, faq: 'faq-external-db'}];
const warningCommands = details[0].commands;
const warnings = fixture ? fixture.save.warnings : ['任务计划库：权限不足'];
const data = fixture ? fixture.data : {title: 'FnMessageBot', selected_events: ['LoginSucc'],
  events_by_category: [{name: '系统', events: [{id: 'LoginSucc', title: '登录成功'}]}],
  channels: [{type: 'wechat', url: 'https://example.invalid/webhook', enabled: true}],
  channel_options: [{id: 'wechat', name: '企业微信'}]};

(async () => {
  const browser = await chromium.launch({headless: true,
    ...(process.env.FNMB_BROWSER_EXECUTABLE ? {executablePath: process.env.FNMB_BROWSER_EXECUTABLE} : {})});
  try {
    const page = await browser.newPage({viewport: {width: 1100, height: 850}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    let releaseSave;
    let refreshWithoutCheck = false;
    let savePayload;
    await page.route('**/*', async route => {
      const url = new URL(route.request().url());
      let response = {ok: true};
      if (url.pathname.endsWith('/session-guard.js')) {
        await route.fulfill({contentType: 'application/javascript', body: fs.readFileSync(path.join(source, 'web/static/session-guard.js'), 'utf8')});
        return;
      }
      if (url.pathname.endsWith('/api/auth/status')) response = {ok: true, authenticated: true, remaining_seconds: 900};
      else if (url.pathname.endsWith('/api/config')) {
        refreshWithoutCheck ||= url.searchParams.get('check_access') === '0';
        response = {ok: true, data, warnings,
          warning_details: url.searchParams.get('check_access') === '0' ? [] : details};
      } else if (url.pathname.endsWith('/api/save-config')) {
        savePayload = route.request().postDataJSON();
        await new Promise(resolve => { releaseSave = resolve; });
        response = {ok: true, message: '配置已保存，以下数据源仍需处理。',
          warnings, warning_details: details};
      } else if (url.pathname.endsWith('/api/nas-patrol/run')) {
        response = {ok: true, message: '巡检推送已发送', warnings, warning_details: details};
      } else if (url.pathname.endsWith('/api/push-stats')) response = {ok: true, data: {}};
      else if (url.pathname.endsWith('/')) {
        await route.fulfill({contentType: 'text/html', body: html});
        return;
      }
      await route.fulfill({contentType: 'application/json', body: JSON.stringify(response)});
    });
    // 保留 FPK 网关前缀，验证 FAQ 使用相对链接。
    await page.goto('http://fnmb.test/app/FnMessageBot/');
    await page.locator('#warning-panel.show').waitFor();
    assert.equal(await page.locator('#warning-panel img').count(), 0);
    assert.equal(await page.evaluate(() => window.fnmbXss), undefined);
    assert.equal(await page.locator('#warning-panel a').getAttribute('href'), 'faq#' + details[0].faq);
    await page.locator('#warning-panel summary').click();
    assert.equal(await page.locator('#warning-panel pre').textContent(), warningCommands);
    await page.evaluate(() => {
      document.execCommand = () => {
        window.fnmbCopied = document.querySelector('textarea[style]').value;
        return true;
      };
    });
    await page.getByRole('button', {name: '复制命令', exact: true}).click();
    assert.equal(await page.evaluate(() => window.fnmbCopied), warningCommands);
    await page.locator('#save-btn').click();
    await page.waitForFunction(() => document.querySelector('#save-btn').disabled);
    assert.ok(releaseSave);
    if (fixture) {
      assert.equal(savePayload.nas_patrol_enabled, true);
      assert.equal(savePayload.events.includes('NAS_PATROL_REPORT'), false);
    }
    const refreshed = page.waitForResponse(response => response.url().includes('check_access=0'));
    releaseSave();
    await page.waitForFunction(() => !document.querySelector('#save-btn').disabled);
    await refreshed;
    assert.ok(refreshWithoutCheck);
    assert.equal(await page.locator('#warning-panel .warning-panel-item').count(), 1);
    await page.setViewportSize({width: 390, height: 844});
    await page.locator('#warning-panel summary').click();
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
    if (process.env.FNMB_UI_SCREENSHOT) await page.locator('#warning-panel').screenshot({path: process.env.FNMB_UI_SCREENSHOT});
    await page.evaluate(() => renderPersistentWarnings(['旧版字符串提示']));
    assert.ok((await page.locator('#warning-panel').textContent()).includes('旧版字符串提示'));
    await page.evaluate(() => renderPersistentWarnings([]));
    assert.equal(await page.locator('#warning-panel.show').count(), 0);
    await page.locator('#nas-patrol-run-btn').click();
    await page.locator('#warning-panel.show').waitFor();
    assert.equal(await page.locator('#warning-panel pre').textContent(), warningCommands);
    assert.deepEqual(errors, []);
    console.log('Warning UI passed: save/load, command copy, HTML escaping, gateway links, mobile layout, clear/legacy warnings.');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
