// 静态检查：登录成功走 enterAuthenticatedSession，并带上 session_token 请求头兜底。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'src/web/templates/index.html'), 'utf8');
const guard = fs.readFileSync(path.join(root, 'src/web/static/session-guard.js'), 'utf8');
assert.match(html, /function enterAuthenticatedSession\s*\(/);
assert.match(html, /session_token/);
assert.match(html, /fnmbSession\.remember/);
const loginSubmit = html.split('getElementById("form-login").addEventListener("submit"')[1].split('function setStatus')[0];
assert.match(loginSubmit, /enterAuthenticatedSession\(json\)/);
assert.doesNotMatch(loginSubmit, /initAuth\(\)/);
assert.match(guard, /X-Fnmb-Session/);
assert.match(guard, /fnmb_session_token/);
console.log('Login enter UI passed: login success enters config without waiting on status.');
