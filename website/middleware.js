/*
 * 裸域 → www，301 永久跳转。
 *
 * 为什么放这里而不放 DNS：DNSPod 免费版的「显性URL 转发」每域名只有一条，
 * 已被 pome 占用，第二个站就没名额了。EdgeOne Makers 的边缘中间件对纯静态站
 * 同样生效（V8 运行时），判断 Host 即可，不占 DNS 配额、也不依赖控制台规则。
 *
 * 生效前提：fnmb.lndo.top 也要绑定到本项目（控制台「自定义域名」里加），
 * DNS 加 CNAME fnmb.lndo.top → 控制台给出的目标。只绑 www 的话这段永远不会被触发。
 */
const CANONICAL_HOST = 'www.fnmb.lndo.top';
const NAKED_HOST = 'fnmb.lndo.top';

export function middleware(context) {
  const url = new URL(context.request.url);

  if (url.hostname === NAKED_HOST) {
    return context.redirect(`https://${CANONICAL_HOST}${url.pathname}${url.search}`, 301);
  }

  return context.next();
}
