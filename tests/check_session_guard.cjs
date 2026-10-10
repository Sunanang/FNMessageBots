// 延迟响应回归：使用 Node VM 模拟时间与请求，不访问网络或 NAS。
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const source = fs.existsSync(path.join(root, 'src')) ? path.join(root, 'src') : path.join(root, 'cmd/fnmessagebots/src');
const code = fs.readFileSync(path.join(source, 'web/static/session-guard.js'), 'utf8');
function fixture() {
  const elements = Object.fromEntries(['app-main','auth-gate','auth-set-password','auth-login','login-password','auth-login-msg'].map(id=>[id,{style:{display:id==='app-main'?'flex':'none'},value:''}]));
  const requests = [], timers = new Map(), listeners = {};
  let timerNumber = 0;
  const nativeFetch = (url, options) => new Promise(resolve=>requests.push({url:String(url), options, resolve}));
  const window = {fetch:nativeFetch, location:{href:'http://test/app/FnMessageBot/',replace(){}}, addEventListener(){},dispatchEvent(){}};
  const document = {getElementById:id=>elements[id],visibilityState:'visible',addEventListener:(name,handler)=>{listeners[name]=handler;},body:{style:{}}};
  const context = {window,document,URL,Request,Event,Date,setTimeout:(fn,delay)=>{const id=++timerNumber;timers.set(id,{fn,delay});return id;},clearTimeout:id=>timers.delete(id),setInterval(){}};
  vm.runInNewContext(code,context);
  function respond(index, data, status=200) { requests[index].resolve({status,ok:status>=200&&status<300,json:async()=>data}); }
  return {window,elements,requests,timers,listeners,respond};
}
const fresh = {ok:true,authenticated:true,remaining_seconds:900};
(async()=>{
  {
    const f=fixture();f.window.fnmbSession.start(fresh);
    const pending=f.window.fnmbSession.check();
    f.window.fnmbSession.start(fresh); // 用户已续期，旧状态还未回来。
    f.respond(0,{...fresh,remaining_seconds:1});await pending;
    assert.equal([...f.timers.values()][0].delay,900000);
  }
  {
    const f=fixture();f.window.fnmbSession.start(fresh);
    const pending=f.window.fnmbSession.check();f.window.fnmbSession.expired();
    f.respond(0,fresh);await pending;
    assert.equal(f.elements['app-main'].style.display,'none');assert.equal(f.timers.size,0);
  }
  {
    const f=fixture();f.window.fnmbSession.start(fresh);
    const pending=f.window.fetch('api/save-config',{method:'POST'});
    f.window.fnmbSession.start(fresh); // 用户重新登录，旧请求返回 401。
    f.respond(0,{ok:false},401);await pending;
    assert.equal(f.elements['app-main'].style.display,'flex');
    f.respond(1,fresh);await new Promise(resolve=>setImmediate(resolve));
  }
  {
    const f=fixture();f.window.fnmbSession.start(fresh);
    const check=f.window.fnmbSession.check();
    const activity=f.listeners.pointerdown({isTrusted:true});
    f.respond(0,{...fresh,remaining_seconds:1});await check;
    f.respond(1,fresh);await activity;
    assert.equal([...f.timers.values()][0].delay,900000);
  }
  console.log('Session guard passed: stale status, lock race, stale 401 after login, renewal request race.');
})().catch(error=>{console.error(error);process.exitCode=1;});
