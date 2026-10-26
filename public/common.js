/* notice-hub 共用前端库 —— 导航中心与通知中心共用。
   由 tools/split_frontend.py + tools/build_pages.py 从原单文件版拆分生成。 */
(function () {
'use strict';

'use strict';

/* ======================= 图标（全内联 SVG，无 emoji） ======================= */
function svg(d, size, extra) {
  return '<svg width="' + size + '" height="' + size + '" viewBox="0 0 24 24" fill="none" ' +
    'stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round" ' +
    'aria-hidden="true"' + (extra || '') + '>' + d + '</svg>';
}

var P = {
  search: '<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.6-3.6"/>',
  close: '<path d="M18 6L6 18M6 6l12 12"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  edit: '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 013 3L7 19l-4 1 1-4z"/>',
  trash: '<path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14"/>',
  down: '<path d="M12 3v12"/><path d="M7 11l5 5 5-5"/><path d="M4 20h16"/>',
  upload: '<path d="M12 21V9"/><path d="M7 13l5-5 5 5"/><path d="M4 4h16"/>',
  chart: '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
  refresh: '<path d="M20 11A8 8 0 006.3 6.3L4 8"/><path d="M4 4v4h4"/><path d="M4 13a8 8 0 0013.7 4.7L20 16"/><path d="M20 20v-4h-4"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.5 1.5M17.5 17.5L19 19M19 5l-1.5 1.5M6.5 17.5L5 19"/>',
  moon: '<path d="M20 14.5A8.5 8.5 0 019.5 4a8.5 8.5 0 1010.5 10.5z"/>',
  menu: '<circle cx="12" cy="5" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="12" cy="19" r="1.6"/>',
  pin: '<path d="M15 3l6 6-3 1-4 4 .5 4L9 13 4 18l-1 3 3-1 5-5-4.5-5.5L10 9l4-4z"/>',
  filter: '<path d="M3 5h18l-7 8v6l-4 2v-8z"/>',
  left: '<path d="M15 5l-7 7 7 7"/>',
  right: '<path d="M9 5l7 7-7 7"/>',
  chat: '<path d="M21 12a8 8 0 01-11.6 7.1L4 21l1.9-5.4A8 8 0 1121 12z"/>',
  mail: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 7l9 6 9-6"/>',
  doc: '<path d="M14 3H7a2 2 0 00-2 2v14a2 2 0 002 2h10a2 2 0 002-2V8z"/><path d="M14 3v5h5"/>',
  grid: '<rect x="3" y="3" width="7.5" height="7.5" rx="1.6"/><rect x="13.5" y="3" width="7.5" height="7.5" rx="1.6"/><rect x="3" y="13.5" width="7.5" height="7.5" rx="1.6"/><rect x="13.5" y="13.5" width="7.5" height="7.5" rx="1.6"/>',
  list: '<path d="M8 6h13M8 12h13M8 18h13M3.5 6h.01M3.5 12h.01M3.5 18h.01"/>',
  user: '<circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 3.6-6 8-6s8 2 8 6"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.5 2"/>',
  copy: '<rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 012-2h8"/>',
  check: '<path d="M4 13l5 5L20 7"/>',
  info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8h.01"/>',
  warn: '<path d="M12 3l9 16H3z"/><path d="M12 9v5M12 17h.01"/>',
  sync: '<path d="M12 3v6l3-3"/><path d="M12 21v-6l-3 3"/><path d="M4 12a8 8 0 0114-5"/><path d="M20 12a8 8 0 01-14 5"/>',
  shield: '<path d="M12 3l8 3v6c0 5-3.4 8.3-8 9-4.6-.7-8-4-8-9V6z"/><path d="M9 12l2 2 4-4"/>',
  logout: '<path d="M15 4h3a2 2 0 012 2v12a2 2 0 01-2 2h-3"/><path d="M10 8l-4 4 4 4"/><path d="M6 12h9"/>',
  eye: '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/>',
  db: '<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6"/><path d="M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
  link: '<path d="M10 14a4.5 4.5 0 006.4 0l2.5-2.5a4.5 4.5 0 00-6.4-6.4L11 6.6"/><path d="M14 10a4.5 4.5 0 00-6.4 0L5.1 12.5a4.5 4.5 0 006.4 6.4L13 17.4"/>',
  bell: '<path d="M18 15v-4a6 6 0 10-12 0v4l-1.5 2.5h15z"/><path d="M10 20a2 2 0 004 0"/>',
  arrowUp: '<path d="M12 19V5"/><path d="M6 11l6-6 6 6"/>',
  arrowDown: '<path d="M12 5v14"/><path d="M6 13l6 6 6-6"/>',
  home: '<path d="M3 10.5L12 3l9 7.5"/><path d="M5.5 9.5V20h13V9.5"/>',
  caret: '<path d="M9 5l7 7-7 7"/>',
  external: '<path d="M14 4h6v6"/><path d="M20 4l-8.5 8.5"/><path d="M18 14v5a1 1 0 01-1 1H5a1 1 0 01-1-1V7a1 1 0 011-1h5"/>'
};

function ic(name, size, extra) { return svg(P[name] || '', size || 16, extra); }

/* ======================= 常量 ======================= */
var SRC_COLOR = { wecom: 'var(--wecom)', outlook: 'var(--outlook)', manual: 'var(--manual)', other: 'var(--other)' };

var SRC_LABEL = { wecom: '企业微信', outlook: 'Outlook', manual: '手工录入', other: '其它来源' };

var SRC_ICON = { wecom: 'chat', outlook: 'mail', manual: 'edit', other: 'doc' };

var LV_COLOR = { urgent: 'var(--urgent)', important: 'var(--important)', normal: 'var(--normal)' };

var LV_LABEL = { urgent: '紧急', important: '重要', normal: '普通' };

var LS_THEME = 'nh_theme', LS_VIEW = 'nh_view', LS_TOKEN = 'nh_token', LS_USER = 'nh_user';

/* 会话令牌本地副本。
   为什么要有它：某些浏览器上下文（内嵌 webview、预览面板、被代理剥离 cookie）不会保存
   Set-Cookie，只靠 cookie 会出现「登录成功但下一个请求立刻 401」的循环。
   cookie 仍然优先使用，这个副本只是兜底通道。

   ⚠️ 再兜一层内存副本：部分沙箱环境连 localStorage 都会抛异常/被隔离，
   这时候至少保证「当前这一次页面加载」内会话是有效的（登录跳转后同一次加载里能直接用）。
   注意：这里存的是**会话令牌**，不是密码 —— 密码交给浏览器自己的密码管理器。 */
var MEM_TOKEN = '';

function getToken() {
  if (MEM_TOKEN) return MEM_TOKEN;
  try { return localStorage.getItem(LS_TOKEN) || ''; } catch (e) { return ''; }
}

function setToken(t) {
  MEM_TOKEN = t || '';
  try {
    if (t) localStorage.setItem(LS_TOKEN, t); else localStorage.removeItem(LS_TOKEN);
  } catch (e) {}
}

/* ======================= 工具 ======================= */
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
  });
}

function pad(n) { return n < 10 ? '0' + n : '' + n; }

function parseDT(s) {
  if (!s) return null;
  var m = String(s).match(/^(\d{4})-(\d{2})-(\d{2})(?:[ T](\d{2}):(\d{2})(?::(\d{2}))?)?/);
  if (!m) return null;
  return new Date(+m[1], +m[2] - 1, +m[3], +(m[4] || 0), +(m[5] || 0), +(m[6] || 0));
}

function timeAgo(s) {
  var d = parseDT(s); if (!d) return s || '—';
  var diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 0) return '刚刚';
  if (diff < 60) return '刚刚';
  if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前';
  if (diff < 86400) return Math.floor(diff / 3600) + ' 小时前';
  if (diff < 86400 * 30) return Math.floor(diff / 86400) + ' 天前';
  return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
}

function fmtDT(s) {
  var d = parseDT(s); if (!d) return s || '—';
  return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' +
    pad(d.getHours()) + ':' + pad(d.getMinutes());
}

function todayStr(offsetDays) {
  var d = new Date(); if (offsetDays) d.setDate(d.getDate() + offsetDays);
  return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
}

function hl(text, kws) {
  var out = esc(text);
  if (!kws || !kws.length) return out;
  kws.forEach(function (k) {
    if (!k) return;
    var re = new RegExp('(' + k.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')', 'gi');
    out = out.replace(re, '\u0001$1\u0002');
  });
  return out.replace(/\u0001/g, '<mark>').replace(/\u0002/g, '</mark>');
}

/* 列表里的摘要去掉 markdown 记号：图片标记换成 [图片]、链接留文字、去掉 ** 加粗符 */
function cleanSummary(s) {
  return String(s || '')
    .replace(/!\[[^\]]*\]\([^)]*\)/g, '[图片]')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/\*\*/g, '');
}

function isImageUrl(u) {
  return /\.(png|jpe?g|gif|webp|bmp)(\?|#|$)/i.test(u || '');
}

/* 正文渲染：先转义，再依次认 markdown 图片 / 链接 / **重点**，最后把裸网址变成可点链接。
   已生成好的标签先抽出来占位，避免被「裸网址」那一趟二次包裹。 */
function linkify(text) {
  var s = esc(text);
  s = s.replace(/!\[([^\]]*)\]\((\/uploads\/[^\s"'<>)]+|https?:\/\/[^\s"'<>)]+)\)/g,
    function (m, alt, url) {
      var nm = alt || url.split('/').pop();
      return '<a class="thumb inline" href="' + url + '" target="_blank" rel="noopener">' +
        '<img src="' + url + '" alt="' + nm + '" loading="lazy"></a>';
    });
  s = s.replace(/\[([^\]!][^\]]*)\]\((\/uploads\/[^\s"'<>)]+|https?:\/\/[^\s"'<>)]+)\)/g,
    function (m, t, url) {
      return '<a href="' + url + '" target="_blank" rel="noopener">' + t + '</a>';
    });
  s = s.replace(/\*\*([^*\n]{1,160})\*\*/g, '<strong>$1</strong>');
  var kept = [];
  s = s.replace(/<a\b[^>]*>[\s\S]*?<\/a>|<img\b[^>]*>/g, function (m) {
    kept.push(m); return '\u0001' + (kept.length - 1) + '\u0001';
  });
  s = s.replace(/(https?:\/\/[^\s<）)】"'<>]+)/g,
    '<a href="$1" target="_blank" rel="noopener noreferrer">$1</a>');
  return s.replace(/\u0001(\d+)\u0001/g, function (m, i) { return kept[+i]; });
}

/* 附件渲染：兼容老数据（纯文件名字符串）和新数据（{name,url}）。
   图片给缩略图（点开看大图），文档给下载链接。 */
function renderAttachList(list) {
  if (!list || !list.length) return '';
  return list.map(function (a) {
    if (typeof a === 'string') return '<span class="tag">' + esc(a) + '</span>';
    var url = a.url || '', name = a.name || url || '附件';
    if (!url) return '<span class="tag">' + esc(name) + '</span>';
    if (isImageUrl(url)) {
      return '<a class="thumb" href="' + url + '" target="_blank" rel="noopener">' +
        '<img src="' + url + '" alt="' + esc(name) + '" loading="lazy">' +
        '<span>' + esc(name) + '</span></a>';
    }
    return '<a class="filechip" href="' + url + '" target="_blank" rel="noopener" download="' +
      esc(name) + '">' + ic('doc', 14) + esc(name) + '</a>';
  }).join('');
}

function toast(msg, kind) {
  var el = document.createElement('div');
  el.className = 't ' + (kind || '');
  el.textContent = msg;
  document.getElementById('toastRoot').appendChild(el);
  setTimeout(function () { el.style.opacity = '0'; el.style.transition = 'opacity .25s'; }, 2600);
  setTimeout(function () { el.remove(); }, 3000);
}

function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(function () { toast('已复制到剪贴板', 'ok'); },
      function () { fallbackCopy(text); });
  } else fallbackCopy(text);
}

function fallbackCopy(text) {
  var ta = document.createElement('textarea');
  ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
  document.body.appendChild(ta); ta.select();
  try { document.execCommand('copy'); toast('已复制到剪贴板', 'ok'); }
  catch (e) { toast('复制失败，请手动选择', 'err'); }
  ta.remove();
}

/* ======================= 状态 ======================= */
var S = {
  page: 1, size: 20, pages: 1, total: 0, items: [],
  q: '', source: [], level: [], category: [], from: '', to: '', pinned: false,
  sort: 'published-desc', view: 'card',
  categories: [], stats: null, syncInfo: null, me: null,
  nav: null, navQ: '', loginMsg: '', noticeLoaded: false, navFlow: 'list',
  pendingManage: false, pendingAccount: false, navAll: null,
  navGroupsById: null, navmGroup: 0
};

function isAdmin() { return !!(S.me && S.me.role === 'admin'); }

/* 按当前角色刷新两个顶栏的身份 chip、弱密码提醒、以及管理员专属入口的显隐。
   门户（导航中心）和通知中心各有自己的顶栏，账号功能两边都要有。 */
function renderMe() {
  /* 两个页面各有自己的顶栏，所以这里只做共用部分（弱密码提醒），
     页面专属的顶栏绘制交给 NH.hooks.paintChrome。 */
  fillPwdBanner(S.me);
  if (NH.hooks && NH.hooks.paintChrome) {
    try { NH.hooks.paintChrome(); } catch (e) { console.error(e); }
  }
}

/* 弱密码提醒条：两个视图各一条，内容一致 */
function fillPwdBanner(me) {
  ['pwdBanner', 'navPwdBanner'].forEach(function (id) {
    var b = document.getElementById(id);
    if (!b) return;
    if (me && me.weak_pwd) {
      b.hidden = false;
      b.innerHTML = ic('warn', 16) +
        '<div>账号 <b>' + esc(me.username) + '</b> 正在使用弱密码，仅限内网使用也有风险，' +
        '建议立刻改掉。</div><span class="spacer"></span>' +
        '<button class="btn" data-pwd="1">立即修改密码</button>' +
        '<button class="btn ghost icon" data-pwdx="1" title="知道了">' + ic('close', 15) + '</button>';
    } else {
      b.hidden = true;
      b.innerHTML = '';
    }
  });
}

document.addEventListener('click', function (e) {
  if (!e.target.closest || (!e.target.closest('#navUserMenu') && !e.target.closest('#navUserChip'))) {
    closeNavUserMenu();
  }
});

document.addEventListener('keydown', function (e) {
  if (e.key === 'Escape') closeNavUserMenu();
});

function pwdBannerClick(e) {
  if (e.target.closest('[data-pwd]')) openChangePwd();
  else if (e.target.closest('[data-pwdx]')) e.currentTarget.hidden = true;
}

['pwdBanner', 'navPwdBanner'].forEach(function (id) {
  var el = document.getElementById(id);
  if (el) el.addEventListener('click', pwdBannerClick);
});

/* ======================= API ======================= */
function api(path, opts) {
  opts = opts || {};
  var hdrs = {};
  var tk = getToken();
  if (tk) hdrs['X-Session-Token'] = tk;
  var init = { method: opts.method || 'GET', credentials: 'same-origin', headers: hdrs };
  if (opts.body !== undefined && !(opts.body instanceof FormData)) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(opts.body);
  } else if (opts.body instanceof FormData) {
    init.body = opts.body;
  }
  return fetch(path, init).then(function (r) {
    if (r.status === 401) setToken('');   // 令牌已失效，清掉避免反复重试
    var ct = r.headers.get('content-type') || '';
    if (ct.indexOf('application/json') < 0) {
      if (!r.ok) {
        var e2 = new Error('请求失败 (' + r.status + ')');
        e2.status = r.status;
        throw e2;
      }
      return r.blob().then(function (b) {
        return { _blob: b, _name: r.headers.get('content-disposition') || '' };
      });
    }
    return r.json().then(function (j) {
      if (!r.ok || j.ok === false) {
        var err = new Error(j.error || ('请求失败 (' + r.status + ')'));
        err.status = r.status;
        throw err;
      }
      return j;
    });
  });
}

/* ======================= 主题 ======================= */
function applyTheme(t) {
  document.documentElement.setAttribute('data-theme', t);
  localStorage.setItem(LS_THEME, t);
  var label = t === 'dark' ? '切换到浅色主题' : '切换到深色主题';
  ['btnTheme', 'navTheme'].forEach(function (id) {
    var el = document.getElementById(id);
    if (el) { el.innerHTML = ic(t === 'dark' ? 'sun' : 'moon', 17); el.title = label; }
  });
}

function initTheme() {
  /* 默认「蓝白」（左侧深蓝标题栏 + 右侧白底）。用户手动切过就沿用他的选择。 */
  var t = localStorage.getItem(LS_THEME) || 'light';
  applyTheme(t);
}

/* ======================= 登录 ======================= */
function showLogin(msg) {
  var ls = document.getElementById('loginScreen');
  if (ls) ls.hidden = false;
  ['app', 'portalView'].forEach(function (id) {
    var elx = document.getElementById(id);
    if (elx) elx.hidden = true;
  });
  document.getElementById('loginErr').textContent = msg || '';
  var p = document.getElementById('loginPwd');
  if (p) p.value = '';
  setTimeout(function () {
    var u = document.getElementById('loginUser');
    (u.value ? p : u).focus();
  }, 40);
}

/* 原生表单提交后回到首页，从 URL fragment 里取回令牌 / 错误标记。
   登录成功或失败都跳到「通知中心」路由 —— 因为只有进入通知中心才需要登录。 */
function readLoginHash() {
  var h = location.hash || '';
  if (h.indexOf('#token=') === 0) {
    setToken(decodeURIComponent(h.slice(7)));
    history.replaceState(null, '', location.pathname + '#/notices');
    return {};
  }
  if (h === '#login-error') {
    history.replaceState(null, '', location.pathname + '#/notices');
    return { error: '账号或密码不正确' };
  }
  return {};
}

/* 用 fetch 提交登录。
   兜底通道：沙箱化 iframe（缺 allow-forms）会静默拦掉原生表单提交，
   此时页面上什么都不会发生 —— 由下面的定时器兜住。 */
function submitLoginByFetch(uname, pwd) {
  var errEl = document.getElementById('loginErr');
  errEl.textContent = '登录中…';
  api('/api/login', { method: 'POST', body: { username: uname, password: pwd } })
    .then(function (res) {
      S.me = res.user;
      if (res.token) setToken(res.token);
      document.getElementById('loginPwd').value = '';
      errEl.textContent = '';
      S.loginMsg = '';
      var wantManage = S.pendingManage, wantAccount = S.pendingAccount;
      S.pendingManage = false;
      S.pendingAccount = false;
      go((wantManage || wantAccount) ? 'portal' : 'notices');
      if (wantManage) {
        setTimeout(function () {
          if (isAdmin()) openNavManage();
          else toast('当前账号是查看权限，不能管理导航', 'err');
        }, 80);
      }
      if (wantAccount) {
        setTimeout(function () {
          if (isAdmin()) openUsers();
          else toast('当前账号是查看权限，不能管理账号', 'err');
        }, 80);
      }
    })
    .catch(function (err) {
      errEl.textContent = err.message || '登录失败';
      S.loginMsg = errEl.textContent;
    });
}

var loginPageLeaving = false;

var loginFallbackTimer = null;

window.addEventListener('pagehide', function () { loginPageLeaving = true; });

function loginRememberUser() {
  var uname = document.getElementById('loginUser').value.trim();
  try {
    var box = document.getElementById('loginRemember');
    if (box && box.checked && uname) localStorage.setItem(LS_USER, uname);
    else localStorage.removeItem(LS_USER);
  } catch (e) {}
  return uname;
}

/* 启动「原生提交失败」的兜底。
   ⚠️ 关键：沙箱化 iframe（缺 allow-forms）会在**触发 submit 事件之前**就把提交拦掉，
   所以光监听 submit 是没用的 —— 必须挂在登录按钮的 click 上（click 在沙箱里照常触发）。
   正常环境：click → 原生提交 → 页面跳转 → pagehide 把定时器作废，密码管理器正常弹窗。
   沙箱环境：click → 原生提交被拦、页面纹丝不动 → 1.5 秒后定时器接管，改用 fetch 登录。 */
function armLoginFallback() {
  var uname = document.getElementById('loginUser').value.trim();
  var pwd = document.getElementById('loginPwd').value;
  if (!uname) return;
  if (loginFallbackTimer) clearTimeout(loginFallbackTimer);
  loginFallbackTimer = setTimeout(function () {
    loginFallbackTimer = null;
    if (loginPageLeaving) return;                                // 原生提交已触发跳转
    var appEl = document.getElementById('app');
    if (appEl && !appEl.hidden) return;                           // 已经登录进去了
    if (document.getElementById('loginScreen').hidden) return;    // 已经不在登录页
    submitLoginByFetch(uname, pwd);
  }, 1500);
}

/* ======================= 数据加载 ======================= */
function queryParams() {
  var p = { page: S.page, size: S.size, q: S.q, sort: S.sort.split('-')[1] };
  if (S.sort.indexOf('received') === 0) p.orderby = 'received';
  if (S.source.length) p.source = S.source.join(',');
  if (S.level.length) p.level = S.level.join(',');
  if (S.category.length) p.category = S.category.join(',');
  if (S.from) p.from = S.from;
  if (S.to) p.to = S.to;
  if (S.pinned) p.pinned = '1';
  return p;
}

/* ======================= 详情弹窗 ======================= */
function closeModal() { document.getElementById('modalRoot').innerHTML = ''; }

function mountModal(html) {
  document.getElementById('modalRoot').innerHTML = '<div class="mask" id="maskBg">' + html + '</div>';
  var bg = document.getElementById('maskBg');
  bg.addEventListener('mousedown', function (e) { if (e.target === bg) closeModal(); });
}

/* ======================= 修改我的密码 ======================= */
function openChangePwd() {
  var h = '<div class="modal" style="max-width:440px"><div class="mhead"><h3>修改我的密码</h3>' +
    '<button class="btn icon ghost" data-act="close">' + ic('close', 17) + '</button></div>' +
    '<div class="mbody">';
  h += '<div class="field"><label>当前账号</label><input value="' +
    esc(S.me ? S.me.username : '') + '" disabled></div>';
  h += '<div class="field"><label>原密码</label>' +
    '<input type="password" id="cpOld" autocomplete="current-password"></div>';
  h += '<div class="field"><label>新密码</label>' +
    '<input type="password" id="cpNew" autocomplete="new-password" placeholder="至少 6 位"></div>';
  h += '<div class="field"><label>确认新密码</label>' +
    '<input type="password" id="cpNew2" autocomplete="new-password"></div>';
  h += '<div class="hint">改完密码后，其他设备上的登录会立即失效，需要重新登录。</div>';
  h += '</div><div class="mfoot"><span class="spacer"></span>' +
    '<button class="btn" data-act="close">取消</button>' +
    '<button class="btn primary" data-act="save">' + ic('check', 15) + '保存</button></div></div>';
  mountModal(h);
  document.querySelector('.mask .modal').addEventListener('click', function (e) {
    var b = e.target.closest('[data-act]');
    if (!b) return;
    var act = b.getAttribute('data-act');
    if (act === 'close') return closeModal();
    if (act !== 'save') return;
    var oldp = document.getElementById('cpOld').value;
    var np = document.getElementById('cpNew').value;
    var np2 = document.getElementById('cpNew2').value;
    if (!oldp) return toast('请输入原密码', 'err');
    if (np.length < 6) return toast('新密码至少 6 位', 'err');
    if (np !== np2) return toast('两次输入的新密码不一致', 'err');
    api('/api/me/password', {
      method: 'POST', body: { old_password: oldp, new_password: np }
    }).then(function (res) {
      S.me = res.user;
      if (res.token) setToken(res.token);
      closeModal();
      renderMe();
      toast('密码已修改，其他设备需重新登录', 'ok');
    }).catch(function (err) { toast(err.message, 'err'); });
  });
  setTimeout(function () {
    var el = document.getElementById('cpOld');
    if (el) el.focus();
  }, 50);
}

/* 两个页面之间的跳转：整页导航（不再有 hash 路由） */
var ROUTES = { portal: '/', notices: '/notices' };
function go(route) { location.href = ROUTES[route] || '/'; }

window.addEventListener('hashchange', function () {
  closeModal();
  applyRoute();
});

document.addEventListener('keydown', function (e) {
  // Ctrl+K / Cmd+K：快速打开面板（输入框里也生效，符合通用习惯）
  if ((e.ctrlKey || e.metaKey) && (e.key === 'k' || e.key === 'K')) {
    e.preventDefault();
    if (cmdkOn) cmdClose(); else cmdOpen();
    return;
  }
  if (e.key === 'Escape') {
    if (cmdkOn) { cmdClose(); return; }        // 面板优先，避免同时关掉底层弹窗
    if (document.getElementById('modalRoot').innerHTML) closeModal();
    else if (drawerOpen) toggleDrawer(false);
  }
  if (e.key === '/' && !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName)) {
    e.preventDefault();
    // 门户页：/ 直接开快速打开面板；通知中心：仍聚焦筛选框
    var pv = document.getElementById('portalView');
    if (pv && !pv.hidden) { cmdOpen(); return; }
    ['navQ', 'q', 'loginUser'].some(function (id) {
      var el = document.getElementById(id);
      if (el && el.offsetParent !== null) { el.focus(); return true; }
      return false;
    });
  }
});

window.addEventListener('popstate', function () { closeModal(); });




/* ---------------- 对外导出 ---------------- */
var NH = window.NH = { hooks: {} };
NH.LS_THEME = LS_THEME;
NH.LS_TOKEN = LS_TOKEN;
NH.LS_USER = LS_USER;
NH.LS_VIEW = LS_VIEW;
NH.LV_COLOR = LV_COLOR;
NH.LV_LABEL = LV_LABEL;
NH.P = P;
NH.S = S;
NH.SRC_COLOR = SRC_COLOR;
NH.SRC_ICON = SRC_ICON;
NH.SRC_LABEL = SRC_LABEL;
NH.api = api;
NH.applyTheme = applyTheme;
NH.armLoginFallback = armLoginFallback;
NH.cleanSummary = cleanSummary;
NH.closeModal = closeModal;
NH.copyText = copyText;
NH.esc = esc;
NH.fallbackCopy = fallbackCopy;
NH.fillPwdBanner = fillPwdBanner;
NH.fmtDT = fmtDT;
NH.getToken = getToken;
NH.go = go;
NH.hl = hl;
NH.ic = ic;
NH.initTheme = initTheme;
NH.isAdmin = isAdmin;
NH.isImageUrl = isImageUrl;
NH.linkify = linkify;
NH.loginFallbackTimer = loginFallbackTimer;
NH.loginPageLeaving = loginPageLeaving;
NH.loginRememberUser = loginRememberUser;
NH.mountModal = mountModal;
NH.openChangePwd = openChangePwd;
NH.pad = pad;
NH.parseDT = parseDT;
NH.pwdBannerClick = pwdBannerClick;
NH.queryParams = queryParams;
NH.readLoginHash = readLoginHash;
NH.renderAttachList = renderAttachList;
NH.renderMe = renderMe;
NH.setToken = setToken;
NH.showLogin = showLogin;
NH.submitLoginByFetch = submitLoginByFetch;
NH.svg = svg;
NH.timeAgo = timeAgo;
NH.toast = toast;
NH.todayStr = todayStr;
NH.LS_VIEW = LS_VIEW; NH.LS_USER = LS_USER; NH.LS_TOKEN = LS_TOKEN; NH.LS_THEME = LS_THEME;
})();
