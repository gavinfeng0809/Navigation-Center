#!/usr/bin/env node
/**
 * notice-hub 前端逻辑测试（零依赖，直接 node tools/test_frontend.js）
 *
 * public/index.html 是单文件内联脚本，没法直接 require。这里的做法是：
 *   1. 抽出 <script> 内容
 *   2. 按大括号配对切出需要测的那些函数
 *   3. 配一套最小 DOM 桩，把渲染与交互路径真跑一遍
 *
 * 覆盖：卡片/深链/Winbox 打开方式、客户 chip、分组折叠与懒渲染、
 *       拖拽排序（分组 + 组内项）、搜索与筛选、账号管理面板、密码生成器。
 * 退出码非 0 表示有失败项。
 */

'use strict';

const fs = require('fs');
const path = require('path');

/* 前端已拆成两个页面 + 共用库：
     public/common.js   共用库（window.NH）
     public/index.html  导航中心（仅门户）
     public/notices.html 重要通知中心（仅通知）
   测试要跑的是「门户 + 共用」这一套；通知中心的断言按源码文本检查。 */
const PUB = path.join(__dirname, '..', 'public');
const read = function (f) { return fs.readFileSync(path.join(PUB, f), 'utf8'); };
const PORTAL_HTML = read('index.html');
const NOTICES_HTML = read('notices.html');
const COMMON_JS = read('common.js');
const COMMON_CSS = read('common.css');
const NH_PS1 = read('tools/nh-open.ps1');
const NH_CMD = read('tools/nh-setup.cmd');
const NH_SETUP_PS1 = read('tools/nh-setup.ps1');
/* 源码级断言用的合并视图 */
const html = PORTAL_HTML + '\n' + NOTICES_HTML + '\n' + COMMON_JS + '\n' + COMMON_CSS;
const htmlPortal = PORTAL_HTML;
const htmlNotices = NOTICES_HTML;

function pageScript(src) {
  /* 只取「没有 src 属性」的 <script>：注意不能用「内容里没有 src=」来判断，
     页面脚本里 linkify 会生成 '<img src=' 这样的字符串。 */
  const re = /<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g;
  let m, out = '';
  while ((m = re.exec(src))) out = m[1];
  return out;
}

/* 把 common.js 与页面脚本的 IIFE 外壳剥掉拼成一个作用域，
   这样原有的「按名字切函数」夹具可以照用。 */
function innerOf(src, isCommon) {
  let b = src.slice(src.indexOf('{') + 1, src.lastIndexOf('}'));
  if (isCommon) {
    const cut = b.indexOf('/* ---------------- 对外导出');
    if (cut >= 0) b = b.slice(0, cut);
  } else {
    b = b.replace(/var NH = window\.NH;[^\n]*\n/, '');
    b = b.replace(/^[ \t]*var [^\n]*= NH\.[^\n]*;[ \t]*$/m, '');
  }
  return b;
}
const script = innerOf(COMMON_JS, true) + '\n' + innerOf(pageScript(PORTAL_HTML), false);
if (script.indexOf('function navCardHtml') < 0) {
  console.error('拼接后的源码里找不到门户函数，拆分后的结构可能变了');
  process.exit(1);
}

/* ---------- 从源码里切函数 ---------- */
function grab(name) {
  const re = new RegExp('\\nfunction ' + name.replace(/[$]/g, '\\$') + '\\s*\\(');
  const hit = re.exec(script);
  if (!hit) return null;
  const start = script.indexOf('{', hit.index);
  let d = 0;
  for (let j = start; j < script.length; j++) {
    if (script[j] === '{') d++;
    else if (script[j] === '}') {
      d--;
      if (d === 0) return script.slice(hit.index, j + 1);
    }
  }
  return null;
}
function grabVar(re) {
  const hit = re.exec(script);
  return hit ? hit[0] : null;
}

const WANT = [
  'esc', 'svg', 'ic', 'internalRoute', 'navInitial', 'hostOf', 'navOpenTarget',
  'navIsApp', 'navSearchText', 'navRank', 'lsGet', 'lsSet',
  'navSelGroup', 'setNavSelGroup', 'navSubSel', 'setNavSubSel', 'navCardHtml', 'groupBodyHtml',
  'flatRowHtml', 'renderFlatList', 'navPaintChips',
  'navDragRow', 'navClearDropMarks', 'navResetDrag', 'navDragStart', 'navDragOver',
  'navDrop', 'navDragEnd', 'navManageChange', 'rndInt', 'genPassword', 'openUsers',
  'navManageList', 'navItemForm', 'findNavItem', 'navSideItem', 'renderNavSide', 'renderPane',
  'isAdmin', 'renderMe', 'renderNavUser', 'fillPwdBanner', 'navUserMenuItem',
  'openNavUserMenu', 'closeNavUserMenu',
  'openNavCtxMenu', 'closeNavCtx', 'bindNavCtxMenus'
];
const missingFn = WANT.filter(function (n) { return !grab(n); });
if (missingFn.length) {
  console.error('从 index.html 里找不到这些函数（可能被改名了）：' + missingFn.join(', '));
  process.exit(1);
}
const PAINT_CHROME = (function () {
  const m = /NH\.hooks\.paintChrome = function \(\) \{([\s\S]*?)\n\};/.exec(pageScript(PORTAL_HTML));
  return m ? ('NH.hooks.paintChrome = function () {' + m[1] + '\n};') : '';
})();
const SRC = 'var NH = { hooks: {} };\n' + WANT.map(grab).join('\n\n') + '\n' + PAINT_CHROME + '\n' +
  grabVar(/\nvar P = \{[\s\S]*?\n\};/) + '\n' +
  grabVar(/\nvar LS_NAVOPEN = [^\n]*/) + '\n' +
  grabVar(/\nvar NAVDRAG = \{[^\n]*/) + '\n' +
  grabVar(/\nvar navCtxEl = [^\n]*/) + '\n' +
  grabVar(/\nvar US = \{[^\n]*/);

/* ---------- 最小 DOM 桩 ---------- */
const _ls = {};
const localStorage = {
  getItem: function (k) { return _ls[k] === undefined ? null : _ls[k]; },
  setItem: function (k, v) { _ls[k] = String(v); },
  removeItem: function (k) { delete _ls[k]; }
};
const location = { href: 'http://10.127.207.71:8091/', origin: 'http://10.127.207.71:8091' };
const S = { navAll: null, navGroupsById: {}, navQ: '', favAll: false, me: null, navmGroup: 0 };
const API = [], TOASTS = [], COPIED = [];
let USER_ROWS = [];

function api(url, opt) {
  API.push({ url: url, body: opt && opt.body });
  if (url === '/api/users') {
    return Promise.resolve({ ok: true, items: USER_ROWS,
      roles: [{ value: 'admin', label: '管理员' }, { value: 'viewer', label: '查看' }],
      me: USER_ROWS[0] });
  }
  return Promise.resolve({ ok: true });
}
function navRefreshAnd() { return Promise.resolve(); }
function toast(msg) { TOASTS.push(msg); }
function mountModal(h) {
  document.getElementById('modalRoot').innerHTML = '<div class="mask" id="maskBg">' + h + '</div>';
}
function closeModal() {}
function renderMe() {}
function copyText(t) { COPIED.push(t); }

function El(id) {
  const self = this;
  this.id = id; this._html = ''; this.value = ''; this.disabled = false;
  this.textContent = ''; this.style = {}; this.L = {}; this._cls = {};
  this.classList = {
    add: function (c) { self._cls[c] = 1; },
    remove: function (c) { delete self._cls[c]; },
    toggle: function (c) { if (self._cls[c]) { delete self._cls[c]; return false; } self._cls[c] = 1; return true; },
    contains: function (c) { return !!self._cls[c]; }
  };
}
Object.defineProperty(El.prototype, 'innerHTML', {
  get: function () { return this._html; },
  set: function (v) {
    this._html = v;
    /* 模拟真实 DOM：innerHTML 中带 id 的元素会被创建出来 */
    const re = /id="([^"]+)"/g; let x;
    while ((x = re.exec(v))) if (!_els[x[1]]) _els[x[1]] = new El(x[1]);
  }
});
El.prototype.addEventListener = function (ev, fn) { (this.L[ev] = this.L[ev] || []).push(fn); };
El.prototype.querySelector = function () { return null; };
El.prototype.querySelectorAll = function () { return []; };
El.prototype.closest = function () { return null; };
El.prototype.getAttribute = function () { return null; };
El.prototype.setAttribute = function (k, v) { (this._attrs = this._attrs || {})[k] = v; };
/* 右键菜单要往 body 挂节点、还要量尺寸避开视口边缘 —— 这两个原来没有 */
El.prototype.appendChild = function (c) {
  (this._kids = this._kids || []).push(c);
  c.parentNode = this;
  return c;
};
El.prototype.removeChild = function (c) {
  this._kids = (this._kids || []).filter(function (k) { return k !== c; });
  c.parentNode = null;
  return c;
};
El.prototype.remove = function () { if (this.parentNode) this.parentNode.removeChild(this); };
El.prototype.contains = function (c) { return (this._kids || []).indexOf(c) >= 0; };
El.prototype.getBoundingClientRect = function () { return { width: 180, height: 150, left: 0, top: 0 }; };
El.prototype.focus = function () {};
El.prototype.select = function () {};
El.prototype.scrollIntoView = function () {};

const _els = {};
function el(id) { if (!_els[id]) _els[id] = new El(id); return _els[id]; }
function getEl(id) { return _els[id] || null; }

const document = {
  getElementById: getEl,
  querySelector: function (sel) {
    const mm = /\[(data-idrag|data-gdrag)="(\d+)"\]/.exec(sel);
    if (!mm) return null;
    return DRAG_ROWS.filter(function (r) { return r.attrs[mm[1]] === mm[2]; })[0] || null;
  },
  querySelectorAll: function () { return []; },
  createElement: function () { return new El('t'); },
  body: new El('body'),
  activeElement: new El('ae')
};
const window = { crypto: crypto.webcrypto, isSecureContext: true };
const navigator = {};
function dt() {
  return { effectAllowed: '', dropEffect: '', setData: function () {}, getData: function () { return ''; } };
}
function ev(target, extra) {
  const o = { target: target, dataTransfer: dt(), preventDefault: function () {} };
  if (extra) for (const k in extra) o[k] = extra[k];
  return o;
}

/* 拖拽用的行元素桩：带 previousElementSibling 链 */
let DRAG_ROWS = [];
function Row(attr, id) {
  const self = this;
  this.attrs = {}; this.attrs[attr] = String(id);
  this.prev = null; this._top = 0; this._h = 40; this.cls = {};
  this.classList = {
    add: function (c) { self.cls[c] = 1; },
    remove: function (c) { delete self.cls[c]; },
    toggle: function () {}, contains: function (c) { return !!self.cls[c]; }
  };
  this.getAttribute = function (k) { return self.attrs[k] === undefined ? null : self.attrs[k]; };
  this.getBoundingClientRect = function () { return { top: self._top, height: self._h }; };
  this.closest = function (sel) {
    if (sel.indexOf('data-idrag') >= 0 || sel.indexOf('data-gdrag') >= 0) return self;
    return null;   /* tr 不是 button/input/select/a */
  };
}
Object.defineProperty(Row.prototype, 'previousElementSibling', { get: function () { return this.prev; } });
function chain(attr, ids) {
  const rows = ids.map(function (id) { return new Row(attr, id); });
  rows.forEach(function (r, i) { r.prev = i > 0 ? rows[i - 1] : null; });
  return rows;
}

/* ---------- 在桩环境里求值被测代码 ---------- */
const sandbox = { localStorage, location, S, api, navRefreshAnd, toast, mountModal,
  closeModal, renderMe, copyText, document, window, navigator, crypto: crypto.webcrypto,
  console, Promise, setTimeout, Math, JSON, String, Number, Array, Object, RegExp, Date, Uint32Array };
const runner = new Function(
  'localStorage,location,S,api,navRefreshAnd,toast,mountModal,closeModal,renderMe,copyText,' +
  'document,window,navigator,crypto,console',
  SRC + '\n;return {navCardHtml:navCardHtml,groupBodyHtml:groupBodyHtml,flatRowHtml:flatRowHtml,' +
  'navOpenTarget:navOpenTarget,' +
  'navIsApp:navIsApp,navSearchText:navSearchText,navRank:navRank,' +
  'navPaintChips:navPaintChips,renderNavSide:renderNavSide,renderPane:renderPane,' +
  'navDragStart:navDragStart,navDragOver:navDragOver,navDrop:navDrop,' +
  'navResetDrag:navResetDrag,genPassword:genPassword,renderFlatList:renderFlatList,' +
  'navSelGroup:navSelGroup,setNavSelGroup:setNavSelGroup,navSubSel:navSubSel,' +
  'setNavSubSel:setNavSubSel,openUsers:openUsers,navManageChange:navManageChange,' +
  'getNAVDRAG:function(){return NAVDRAG;},setNAVDRAG:function(v){NAVDRAG=v;},' +
  'navManageList:navManageList,navItemForm:navItemForm,findNavItem:findNavItem,' +
  'openNavCtxMenu:openNavCtxMenu,closeNavCtx:closeNavCtx,bindNavCtxMenus:bindNavCtxMenus,' +
  'hasNavCtx:function(){return !!navCtxEl;},' +
  'isAdmin:isAdmin,renderMe:renderMe,renderNavUser:renderNavUser,' +
  'fillPwdBanner:fillPwdBanner,openNavUserMenu:openNavUserMenu,closeNavUserMenu:closeNavUserMenu,' +
  'setUSER:function(v){USER_ROWS=v;},getUS:function(){return US;}};'
);
let A;
try {
  A = runner(localStorage, location, S, api, navRefreshAnd, toast, mountModal, closeModal,
    renderMe, copyText, document, window, navigator, crypto.webcrypto, console);
} catch (e) {
  console.error('求值 public/index.html 内联脚本时出错：', e.message);
  process.exit(1);
}

/* ---------- 断言 ---------- */
let pass = 0, fail = 0;
function ok(name, cond, extra) {
  if (cond) { pass++; console.log('  \x1b[32mPASS\x1b[0m  ' + name); }
  else { fail++; console.log('  \x1b[31mFAIL\x1b[0m  ' + name + '  ' + (extra === undefined ? '' : extra)); }
}
function head(t) { console.log('\n\x1b[1m' + t + '\x1b[0m'); }
function tick() { return new Promise(function (r) { setTimeout(r, 0); }); }

const ITEMS = [
  { id: 1, name: 'ATKN-001', subgroup: 'ATKN', url: 'https://198.19.223.29', favorite: 0, visit_count: 0, launch: 'web' },
  { id: 2, name: 'ATKN-002', subgroup: 'ATKN', url: 'https://198.19.223.30', favorite: 1, visit_count: 9, launch: 'web' },
  { id: 3, name: 'THGN-005', subgroup: 'THGN', url: 'https://198.19.224.10', favorite: 0, visit_count: 0, launch: 'web' },
  { id: 4, name: 'SZLR-001', subgroup: 'SZLR',
    url: 'https://198.19.223.215/firewall/shaper?tabType=policy', favorite: 0, visit_count: 3, launch: 'web' },
  { id: 5, name: 'MIKROTIK-CCR', subgroup: 'MIKR', url: '', launch: 'winbox', launch_host: '192.168.88.1' },
  { id: 6, name: 'ZHKJ-001', subgroup: 'ZHKJ',
    url: 'https://198.19.223.47:8443/network/sd-wan?tabType=health-check', favorite: 0, visit_count: 0, launch: 'web' }
];

/* ---------- 用例 ---------- */
(async function run() {
  head('[1] 深链必须保留（不被裸地址覆盖）');
  ok('SZLR-001 深链保留', A.navCardHtml(ITEMS[3]).indexOf('/firewall/shaper?tabType=policy') >= 0);
  ok('ZHKJ-001 深链保留', A.navCardHtml(ITEMS[5]).indexOf('network/sd-wan?tabType=health-check') >= 0);

  head('[2] Winbox 4.x 打开方式');
  /* Windows 上 Winbox 默认不注册协议，winbox://host 点了没反应，
     所以改用本机安装的 nh:// 处理程序。 */
  ok('生成 nh:// 协议链接（不是没用的 winbox://）',
     A.navOpenTarget(ITEMS[4]) === 'nh://winbox?host=192.168.88.1', A.navOpenTarget(ITEMS[4]));
  ok('web 项走原 URL', A.navOpenTarget(ITEMS[0]) === 'https://198.19.223.29');
  ok('navIsApp 识别 winbox', A.navIsApp(ITEMS[4]) === true && A.navIsApp(ITEMS[0]) === false);
  /* Winbox 项空 url 时不能被当成站内路由；href 用 # 交给 JS 接管 */
  ok('本地应用项不误判为站内路由（空 url 的坑）',
    A.navCardHtml(ITEMS[4]).indexOf('href="#"') >= 0
    && A.navCardHtml(ITEMS[4]).indexOf('data-winbox="1"') >= 0,
    A.navCardHtml(ITEMS[4]).slice(0, 140));
  ok('Winbox 卡片带 data-winbox 标记（供兜底提示识别）',
    A.navCardHtml(ITEMS[4]).indexOf('data-winbox="1"') >= 0);
  ok('普通 web 卡片不带 data-winbox', A.navCardHtml(ITEMS[0]).indexOf('data-winbox') < 0);
  ok('显示 WINBOX 角标', A.navCardHtml(ITEMS[4]).indexOf('WINBOX') >= 0);

  head('[3] 星标（已改为文字星号，不再每卡片一个 SVG）');
  var c1 = A.navCardHtml(ITEMS[0]), c2 = A.navCardHtml(ITEMS[1]);
  ok('常用项带 is-fav', c2.indexOf('is-fav') >= 0 && c1.indexOf('is-fav') < 0);
  ok('有 data-star 按钮', c1.indexOf('data-star="1"') >= 0);
  ok('星标在卡片内且在文本之后', c2.indexOf('data-star') > c2.indexOf('navbody'));
  ok('星号是文字不是 svg', c1.indexOf('<svg') < 0, c1.slice(0, 80));

  head('[4] 客户 chip 渲染与计数');
  var body = A.groupBodyHtml({ id: 11, name: '客户站点防火墙', items: ITEMS });
  ok('渲染 navsubs 容器', body.indexOf('class="navsubs"') >= 0);
  ok('渲染 chip 行', body.indexOf('data-chips="11"') >= 0);
  ok('ATKN chip 计数为 2', /data-subpick="ATKN"[^>]*>ATKN<span class="sc">2<\/span>/.test(body));
  ok('「全部」chip 计数为 6', /data-subpick=""[^>]*>全部<span class="sc">6<\/span>/.test(body));
  ok('设备多的客户排前面', body.indexOf('data-subpick="ATKN"') < body.indexOf('data-subpick="THGN"'));
  ok('单个 .navgrid', (body.match(/class="navgrid"/g) || []).length === 1);
  ok('每卡片带 data-sub', (body.match(/data-sub="/g) || []).length === 6);
  ok('6 个客户时不渲染筛选框', body.indexOf('data-subfilter') < 0);

  head('[5] 84 个客户时渲染筛选框');
  var many = [];
  for (var i = 0; i < 84; i++) {
    many.push({ id: 100 + i, name: 'C' + i + '-001', subgroup: 'C' + i, url: 'https://10.0.0.' + i, launch: 'web' });
  }
  var big = A.groupBodyHtml({ id: 11, name: 'x', items: many });
  ok('>24 个客户时渲染筛选框', big.indexOf('data-subfilter="11"') >= 0);
  ok('显示客户总数 84', big.indexOf('84 个客户') >= 0);

  head('[6] 左侧标题选择与客户选择（localStorage）');
  ok('默认未选中任何标题', A.navSelGroup() === '', A.navSelGroup());
  A.setNavSelGroup(11);
  ok('选中分组后记住', A.navSelGroup() === '11', A.navSelGroup());
  A.setNavSelGroup('fav');
  ok('可选中「★ 常用」', A.navSelGroup() === 'fav', A.navSelGroup());
  A.setNavSelGroup('');
  ok('默认未选客户', A.navSubSel(11) === '');
  A.setNavSubSel(11, 'ATKN');
  ok('选择后记住', A.navSubSel(11) === 'ATKN');
  ok('选中后 chip 高亮',
    A.groupBodyHtml({ id: 11, name: 'x', items: ITEMS }).indexOf('class="subchip on" data-subpick="ATKN"') >= 0);
  A.setNavSubSel(11, '');

  head('[7] 排序：常用 → 频次 → 名称');
  var sorted = ITEMS.slice().sort(A.navRank).map(function (x) { return x.name; });
  ok('常用项排第一', sorted[0] === 'ATKN-002', sorted.join(','));
  ok('频次高的次之', sorted[1] === 'SZLR-001', sorted.join(','));

  head('[8] 搜索文本：可按 IP / 客户代号 / Winbox 地址搜');
  ok('按 IP', A.navSearchText(ITEMS[0]).indexOf('198.19.223.29') >= 0);
  ok('按客户代号', A.navSearchText(ITEMS[0]).indexOf('atkn') >= 0);
  ok('按 winbox 设备地址', A.navSearchText(ITEMS[4]).indexOf('192.168.88.1') >= 0);
  var memo = { name: 'KARL-001', description: '', tags: [], url: 'https://x', subgroup: 'K', launch_host: '' };
  memo._s = A.navSearchText(memo);
  var first = memo._s;
  memo.name = 'CHANGED';
  ok('_s 命中缓存后不再重算', A.navSearchText(memo) === first);

  head('[9] 扁平搜索结果');
  S.navAll = ITEMS.map(function (x) { x._s = A.navSearchText(x); return x; });
  var flat = { innerHTML: '' };
  var list = A.renderFlatList('atkn', flat);
  ok('命中 2 条', list.length === 2, String(list.length));
  ok('含计数表头', flat.innerHTML.indexOf('找到 <b>2</b>') >= 0);
  ok('显示客户代号', flat.innerHTML.indexOf('class="fg">ATKN') >= 0);
  var flat2 = { innerHTML: '' };
  ok('无命中给提示', A.renderFlatList('zzzz', flat2).length === 0 &&
    flat2.innerHTML.indexOf('没有匹配') >= 0);

  head('[10] 右侧只渲染当前选中的那一组（性能核心）');
  /* 以前默认全部折叠也要把 435 张卡、约 6000 个元素全塞进 DOM。
     改成左标题右内容后，DOM 里最多只有一组的卡片。 */
  ITEMS[1].favorite = 0;      /* 先清掉常用，这样左栏应恰好是 2 个分组标题 */
  var G5 = { id: 5, name: 'A组', items: ITEMS };
  var G6 = { id: 6, name: 'B组', items: [{ id: 99, name: 'only-in-b', url: 'https://b', launch: 'web' }] };
  S.nav = { groups: [G5, G6] };
  S.navGroupsById = { 5: G5, 6: G6 };
  S.navAll = ITEMS.map(function (x) { x._s = A.navSearchText(x); return x; });
  el('navGroups'); el('navSide');
  A.setNavSelGroup(5);
  A.renderNavSide();
  A.renderPane();
  var pane5 = getEl('navGroups').innerHTML;
  ok('右栏渲染了选中的 A 组标题', pane5.indexOf('A组') >= 0);
  ok('A 组卡片全部渲染', (pane5.match(/class="navcard/g) || []).length === ITEMS.length,
    String((pane5.match(/class="navcard/g) || []).length));
  ok('B 组的内容没有被渲染（不在 DOM 里）', pane5.indexOf('only-in-b') < 0);
  ok('右栏含客户 chip', pane5.indexOf('data-subpick') >= 0);
  var side5 = getEl('navSide').innerHTML;
  ok('左栏列出两个分组标题', (side5.match(/data-navsel=/g) || []).length === 2,
    String((side5.match(/data-navsel=/g) || []).length));
  ok('左栏渲染分组名与项数', side5.indexOf('A组') >= 0 && side5.indexOf('B组') >= 0);
  ok('选中项带 on 类', side5.indexOf('data-navsel="5"') >= 0 && /psitem on"[^>]*data-navsel="5"/.test(side5.replace(/\n/g, ' ')) ||
    side5.indexOf('psitem on') >= 0, side5.slice(0, 120));
  A.setNavSelGroup(6);
  A.renderNavSide();
  A.renderPane();
  var pane6 = getEl('navGroups').innerHTML;
  ok('切到 B 组后只渲染 B 组',
    pane6.indexOf('only-in-b') >= 0 && (pane6.match(/class="navcard/g) || []).length === 1);
  ok('切组后 A 组卡片已从 DOM 移除', pane6.indexOf('ATKN-001') < 0);

  head('[10b] 「★ 常用」是一个可选中的虚拟分组');
  ITEMS[1].favorite = 1;
  S.navAll = ITEMS;
  A.setNavSelGroup('fav');
  A.renderNavSide();
  A.renderPane();
  var paneF = getEl('navGroups').innerHTML;
  ok('左栏出现「★ 常用」条目', getEl('navSide').innerHTML.indexOf('★ 常用') >= 0);
  ok('右栏标题为「常用」', paneF.indexOf('<h1>常用</h1>') >= 0);
  ok('只显示被标记为常用的项（1 个）',
    (paneF.match(/class="navcard/g) || []).length === 1,
    String((paneF.match(/class="navcard/g) || []).length));
  ok('显示的正是那一个常用项', paneF.indexOf('ATKN-002') >= 0);
  ITEMS[1].favorite = 0;

  head('[11] 拖拽：组内项排序');
  DRAG_ROWS = chain('data-idrag', [10, 11, 12, 13]);
  API.length = 0;
  A.navDragStart(ev(DRAG_ROWS[2]));
  ok('dragstart 记录 item 类型', A.getNAVDRAG().kind === 'item' && A.getNAVDRAG().id === '12',
    A.getNAVDRAG().kind + '/' + A.getNAVDRAG().id);
  A.navDragOver(ev(DRAG_ROWS[0], { clientY: 35 }));
  ok('拖到下半 → 判定为“后面”', A.getNAVDRAG().after === true && A.getNAVDRAG().over === '10');
  A.navDrop(ev({}));
  ok('提交 reorder_after=10', API[0] && API[0].body.reorder_after === '10', JSON.stringify(API[0]));
  ok('打到 items 接口', API[0].url === '/api/nav/items/12', API[0].url);

  API.length = 0;
  A.navDragStart(ev(DRAG_ROWS[3]));
  A.navDragOver(ev(DRAG_ROWS[0], { clientY: 5 }));
  ok('拖到上半 → 判定为“前面”', A.getNAVDRAG().after === false);
  A.navDrop(ev({}));
  ok('第一行之前 → 置顶（reorder_after=null）',
    API[0] && API[0].body.reorder_after === null, JSON.stringify(API[0]));

  API.length = 0;
  A.navDragStart(ev(DRAG_ROWS[0]));
  A.navDragOver(ev(DRAG_ROWS[2], { clientY: 5 }));
  A.navDrop(ev({}));
  ok('插到中间行之前 → 用它的上一行做锚点',
    API[0] && API[0].body.reorder_after === '11', JSON.stringify(API[0]));

  API.length = 0;
  A.navDragStart(ev(DRAG_ROWS[1]));
  A.navDragOver(ev(DRAG_ROWS[1], { clientY: 35 }));
  A.navDrop(ev({}));
  ok('拖到自己身上 → 不发请求', API.length === 0, JSON.stringify(API));

  head('[12] 拖拽：分组排序');
  var GROUPS = chain('data-gdrag', [1, 2, 3]);
  DRAG_ROWS = DRAG_ROWS.concat(GROUPS);
  API.length = 0;
  A.navDragStart(ev(GROUPS[2]));
  A.navDragOver(ev(GROUPS[0], { clientY: 35 }));
  A.navDrop(ev({}));
  ok('打到 groups 接口', API[0].url === '/api/nav/groups/3', API[0].url);
  ok('reorder_after=1', API[0].body.reorder_after === '1', JSON.stringify(API[0]));

  head('[13] 从按钮上起手不触发拖拽');
  A.navResetDrag();
  A.navDragStart(ev({ closest: function (sel) { return sel.indexOf('button') >= 0 ? {} : null; } }));
  ok('NAVDRAG 未被置位', !A.getNAVDRAG().id, JSON.stringify(A.getNAVDRAG()));

  head('[14] 密码生成器');
  var pws = [];
  for (var k = 0; k < 2000; k++) pws.push(A.genPassword(14));
  ok('长度都是 14', pws.every(function (p) { return p.length === 14; }));
  ok('四类字符齐全', pws.every(function (p) {
    return /[a-z]/.test(p) && /[A-Z]/.test(p) && /[0-9]/.test(p) && /[!@#$%^&*\-_=+]/.test(p);
  }));
  ok('无易混字符 0O1lI', pws.every(function (p) { return !/[0O1lI]/.test(p); }));
  var uniq = {}; pws.forEach(function (p) { uniq[p] = 1; });
  ok('2000 条不重复', Object.keys(uniq).length === 2000, '唯一=' + Object.keys(uniq).length);
  var fu = pws.filter(function (p) { return /[A-Z]/.test(p[0]); }).length;
  ok('首字符已打乱（非固定类别）', fu > 2000 * 0.2 && fu < 2000 * 0.8, 'upper=' + fu);

  head('[15] 账号管理面板');
  USER_ROWS = [
    { id: 1, username: 'admin', role: 'admin', role_label: '管理员', display_name: '系统管理员',
      enabled: 1, weak_pwd: 1, created_by: 'system', created_at: 'x', last_login: '2026-09-29 11:46:32' },
    { id: 2, username: 'zhangwei', role: 'viewer', role_label: '查看', display_name: '张伟',
      enabled: 1, weak_pwd: 0, created_by: 'admin', created_at: 'x', last_login: '' },
    { id: 3, username: 'lisi', role: 'viewer', role_label: '查看', display_name: '',
      enabled: 0, weak_pwd: 0, created_by: 'admin', created_at: 'x', last_login: '2026-09-29 12:05:00' }
  ];
  S.me = USER_ROWS[0];
  el('modalRoot'); el('maskBg');
  A.setUSER(USER_ROWS);
  A.openUsers();
  await tick(); await tick();
  var shell = getEl('usersBody') ? getEl('usersBody').innerHTML : '';
  ok('渲染统计条/搜索框/表格', shell.indexOf('id="uStats"') >= 0 && shell.indexOf('id="uQ"') >= 0 &&
    shell.indexOf('id="uRows"') >= 0);
  ok('密码旁有生成/复制按钮', shell.indexOf('data-gen="nuPass"') >= 0 && shell.indexOf('data-cp="nuPass"') >= 0);
  var stats = getEl('uStats') ? getEl('uStats').innerHTML : '';
  ok('统计：3 个账号 / 1 管理员 / 1 停用 / 1 弱密码',
    stats.indexOf('<b>3</b> 个账号') >= 0 && stats.indexOf('<b>1</b> 个管理员') >= 0 &&
    stats.indexOf('个已停用') >= 0 && stats.indexOf('个弱密码待改') >= 0);
  var rows = getEl('uRows') ? getEl('uRows').innerHTML : '';
  ok('3 行；停用行带 uoff', (rows.match(/<tr /g) || []).length === 3 && rows.indexOf('class="uoff"') >= 0);
  ok('弱密码徽章', rows.indexOf('ubadge weak') >= 0);
  ok('姓名可就地编辑', rows.indexOf('data-field="display_name"') >= 0);
  ok('显示创建人', rows.indexOf('由 admin 创建') >= 0);

  function fireMask(evName, node) {
    (el('maskBg').L[evName] || []).forEach(function (f) { f({ target: node }); });
  }
  function FakeNode(attrs) {
    this.a = attrs || {}; this.value = ''; this.disabled = false;
    this.closest = function () { return this; };
    this.getAttribute = function (k) { return this.a[k] === undefined ? null : this.a[k]; };
  }
  el('nuPass').value = '';
  fireMask('click', new FakeNode({ 'data-gen': 'nuPass' }));
  ok('生成按钮填入 14 位强密码', el('nuPass').value.length === 14, 'len=' + el('nuPass').value.length);
  COPIED.length = 0;
  fireMask('click', new FakeNode({ 'data-cp': 'nuPass' }));
  ok('复制按钮复制的是该密码', COPIED[0] === el('nuPass').value);

  fireMask('click', new FakeNode({ 'data-reset': '2', 'data-name': 'zhangwei' }));
  var rb = getEl('uResetBox') ? getEl('uResetBox').innerHTML : '';
  ok('重置密码面板已渲染（不再用 prompt）', rb.indexOf('重置「zhangwei」的密码') >= 0);
  ok('面板内预生成密码 + 生成/复制按钮',
    rb.indexOf('data-gen="urPass"') >= 0 && rb.indexOf('data-cp="urPass"') >= 0);

  el('nuName').value = 'wangwu'; el('nuPass').value = 'Str0ng!Pass#2026';
  el('nuRole').value = 'viewer'; el('nuDisplay').value = '王五';
  API.length = 0;
  fireMask('click', new FakeNode({ 'data-act': 'adduser' }));
  await tick(); await tick();
  var post = API.filter(function (c) { return c.body && c.body.username; })[0];
  ok('发出创建请求且字段正确',
    !!post && post.body.username === 'wangwu' && post.body.role === 'viewer', post ? JSON.stringify(post.body) : '');
  var cb = getEl('uCreatedBox') ? getEl('uCreatedBox').innerHTML : '';
  ok('渲染账号信息卡片（含账号/密码/地址）',
    cb.indexOf('账号已创建') >= 0 && cb.indexOf('wangwu') >= 0 &&
    cb.indexOf('Str0ng!Pass#2026') >= 0 && cb.indexOf('http://10.127.207.71:8091') >= 0);

  head('[16] 导航管理面板：分组下拉必须列出全部分组（含空分组）');
  /* 造一个「空分组」和一个「有项分组」，模拟用户刚建完分组还没加导航项的情形 */
  S.nav = {
    groups: [
      { id: 1, name: '重要通知中心', items: [{ id: 11, name: 'a', url: 'https://a', group_id: 1, launch: 'web' }] },
      { id: 2, name: '刚建的空分组', items: [] },
      { id: 3, name: '客户Forti站点登录方式', items: [
        { id: 21, name: 'ATKN-001', url: 'https://x', group_id: 3, subgroup: 'ATKN', launch: 'web' },
        { id: 22, name: 'ATKN-002', url: 'https://y', group_id: 3, subgroup: 'ATKN', launch: 'web' }] }
    ],
    visibilities: [{ value: 'guest', label: '免登录可见' }, { value: 'member', label: '需登录可见' },
                   { value: 'admin', label: '仅管理员可见' }]
  };
  S.navmGroup = 0;
  var mg = A.navManageList();
  ok('下拉列出全部 3 个分组（含空分组）',
    (mg.match(/<option value="/g) || []).length === 3,
    String((mg.match(/<option value="/g) || []).length));
  ok('空分组被标注为「空」', mg.indexOf('刚建的空分组（空）') >= 0);
  ok('有项分组显示项数', mg.indexOf('客户Forti站点登录方式（2 项）') >= 0);
  ok('分组列表里空分组也有拖拽手柄',
    (mg.match(/data-gdrag=/g) || []).length === 3,
    String((mg.match(/data-gdrag=/g) || []).length));

  /* 选中空分组时不应崩，且给出提示 */
  S.navmGroup = 2;
  var mg2 = A.navManageList();
  ok('选中空分组可正常渲染', mg2.indexOf('该组还没有导航项') >= 0);
  /* 「新增导航项」按钮已从列表底部移到工具栏右上角（275 项时底部够不着），
     引导文案跟着改；这里断言新文案，同时确认按钮在工具栏里而不是列表末尾。 */
  ok('空分组时给出引导文案', mg2.indexOf('点右上角的「新增导航项」添加') >= 0);
  ok('「新增导航项」在工具栏（不在长列表末尾）',
     mg2.indexOf('class="navm-toolbar"') >= 0 &&
     mg2.indexOf('navm-toolbar') < mg2.indexOf('data-act="additem"'));
  ok('导航项列表限高可滚动（长分组不撑爆弹窗）',
     mg2.indexOf('class="navm-scroll"') >= 0 || mg2.indexOf('navm-empty') >= 0);
  ok('空分组不被自动切走', S.navmGroup === 2, String(S.navmGroup));

  head('[17] 编辑表单：div 嵌套正确（曾经把「打开方式」那组嵌进上一组）');
  var it = S.nav.groups[2].items[0];
  var form = A.navItemForm(it);
  /* 统计每个 class="grid3" 出现时的嵌套深度：三个应当在同一层 */
  var depths = [], depth = 0, re = /<div\b|class="grid3"|\/div>/g, mm2;
  var tokens = form.split(/(<div[^>]*>|\/div>)/);
  depth = 0;
  tokens.forEach(function (t) {
    if (/^<div/.test(t)) { if (t.indexOf('class="grid3"') >= 0) depths.push(depth); depth++; }
    else if (t === '/div>') { depth--; }
  });
  ok('三个 grid3 都出现在同一嵌套层', depths.length === 3 &&
    depths[0] === depths[1] && depths[1] === depths[2], 'depths=' + JSON.stringify(depths));
  ok('最终 div 配平（深度归零）', depth === 0, 'depth=' + depth);
  ok('表单含全部关键字段',
    ['nfName', 'nfUrl', 'nfGroup', 'nfSub', 'nfVis', 'nfColor', 'nfTags', 'nfIcon',
     'nfLaunch', 'nfLaunchHost', 'nfLaunchArg', 'nfProbe', 'nfFav'].every(function (id) {
       return form.indexOf('id="' + id + '"') >= 0;
    }));

  head('[18] 编辑表单：分组下拉选中项正确');
  var form2 = A.navItemForm(S.nav.groups[2].items[0]);
  ok('选中项是该导航项真实所属分组 group_id=3',
    form2.indexOf('<option value="3" selected>') >= 0, form2.match(/<select id="nfGroup">[\s\S]{0,200}/)[0]);
  ok('新增表单可按 presetGroupId 预选分组',
    A.navItemForm(null, 3).indexOf('<option value="3" selected>') >= 0);
  ok('不带 preset 时回落到第一个分组',
    A.navItemForm(null).indexOf('<option value="1" selected>') >= 0);

  head('[19] 长地址不能撑破管理表格（右列被挤出去过）');
  /* 登录地址最长 400 字符且无空格、无法折行；table-layout 默认 auto 会把表格
     撑到近 2800px，右列「可见性/操作」被挤出 .modal.wide 的 940px 可视区。 */
  var LONG = 'https://myid.telstra.com/identity/as/authorization.oauth2?client_id=b2b-telstraconnect&' +
    'redirect_uri=https%3A%2F%2Fconnectapp.telstra.com%2F&response_type=id_token%20token&' +
    'scope=openid%20profile%20email&state=9cb2ff0a5eb4f988a60bda7fbsC2zXyLongEnoughToOverflow';
  S.nav = { visibilities: [{ value: 'member', label: '需登录可见' }], groups: [{
    id: 15, name: '系统与平台', items: [
      { id: 1, name: 'Telstar Connect (myid.telstra.com)', url: LONG,
        visible_to: 'member', vis_label: '需登录可见', favorite: 0, launch: 'web' },
      { id: 2, name: 'BPM系统', url: 'https://bpm.pbscn.net/',
        visible_to: 'member', vis_label: '需登录可见', favorite: 0, launch: 'web' },
      { id: 3, name: '没填地址的项', url: '',
        visible_to: 'member', vis_label: '需登录可见', favorite: 0, launch: 'web' }
    ] }] };
  S.navmGroup = 15;
  var mt = A.navManageList();
  var rows19 = mt.match(/<tr draggable="true" data-idrag="\d+">[\s\S]*?<\/tr>/g) || [];
  ok('渲染出 3 行', rows19.length === 3, String(rows19.length));
  ok('每行都是 5 个单元格（手柄/状态点/名称/可见性/操作）',
    rows19.every(function (r) { return (r.match(/<td/g) || []).length === 5; }),
    JSON.stringify(rows19.map(function (r) { return (r.match(/<td/g) || []).length; })));
  ok('每行都保留「可见性」列',
    rows19.every(function (r) { return r.indexOf('需登录可见') >= 0; }));
  ok('每行都保留编辑与删除按钮',
    rows19.every(function (r) { return r.indexOf('data-iedit') >= 0 && r.indexOf('data-idel') >= 0; }));
  ok('长地址使用可截断的 .navm-url 类（而不是裸 div）',
    mt.indexOf('class="navm-url" title="' + LONG.replace(/&/g, '&amp;') + '"') >= 0);
  ok('长地址完整值保留在 title 悬浮提示里', mt.indexOf(LONG.slice(0, 60)) >= 0);
  ok('名称也带 title（过长时截断但不丢信息）',
    mt.indexOf('<b title="Telstar Connect (myid.telstra.com)">') >= 0);
  ok('空地址显示占位而不是空白', mt.indexOf('（未填地址）') >= 0);

  /* 样式层守卫：固定布局 + 单行省略号必须还在 */
  var rawCss = html;
  ok('CSS 里 #navmItems 使用 table-layout:fixed',
    /#navmItems\s*\{[^}]*table-layout\s*:\s*fixed/.test(rawCss));
  ok('CSS 里 .navm-url 有单行省略号',
    /\.navm-url\s*\{[^}]*text-overflow\s*:\s*ellipsis/.test(rawCss));

  head('[20] 操作列必须放得下 4 个按钮（删除按钮被裁掉过）');
  /* 固定布局下列宽由声明决定：↑33 + 空格4 + ↓33 + 空格4 + 编辑45 + 空格4 + 删除45
     + 单元格 padding 18 = 186px 才能放下；再叠加给 td 加 overflow:hidden，
     放不下的按钮会被直接裁掉（曾经列宽只有 120px，「删除」就消失了）。 */
  var mt20 = A.navManageList();
  ok('操作列使用 .navm-acts（而不是写死 120px）', mt20.indexOf('class="navm-acts"') >= 0);
  var actsCss = /\.navm-acts\{([^}]*)\}/.exec(html);
  var wpx = actsCss ? parseInt((/width\s*:\s*(\d+)px/.exec(actsCss[1]) || [])[1], 10) : 0;
  ok('操作列宽度 >= 186px（4 个按钮所需下限）', wpx >= 186, 'width=' + wpx + 'px');
  ok('操作列不换行', !!actsCss && /white-space\s*:\s*nowrap/.test(actsCss[1]));
  ok('不再给 #navmItems 的所有 td 加 overflow:hidden（会把按钮裁掉）',
    !/#navmItems\s+td\s*\{[^}]*overflow\s*:\s*hidden/.test(html));

  /* 列宽预算：handle+dot+vis+acts 之后，名称/地址列仍需留有合理宽度 */
  var fixed = 0;
  var m20 = /<td style="width:(\d+)px"/g, g20;
  while ((g20 = m20.exec(mt20))) fixed += parseInt(g20[1], 10);
  var avail = 940 - 40;                    /* .modal.wide 940 - .mbody padding 2*20 */
  ok('总列宽不超可视区，且名称/地址列至少 300px',
    fixed + wpx <= avail && (avail - fixed - wpx) >= 300,
    'fixed=' + fixed + ' acts=' + wpx + ' name=' + (avail - fixed - wpx));

  head('[21] 门户顶栏的账号功能（从通知中心迁过来的）');
  el('navUserChip'); el('navUserMenu'); el('navPwdBanner'); el('pwdBanner');
  el('navAccount'); el('navManage'); el('navRefresh'); el('btnImport'); el('userChip');

  /* 未登录：chip 显示「登录」，菜单只给登录入口
     （真实代码里 applyRoute 会调 renderMe，它会同时处理 chip 与管理员入口显隐） */
  S.me = null;
  A.renderMe();
  ok('未登录时 chip 显示「登录」', getEl('navUserChip').innerHTML.indexOf('登录') >= 0);
  A.openNavUserMenu();
  var mOut = getEl('navUserMenu').innerHTML;
  ok('未登录菜单只有「登录」', mOut.indexOf('data-navact="login"') >= 0 &&
     mOut.indexOf('data-navact="logout"') < 0 && mOut.indexOf('data-navact="pwd"') < 0);
  ok('未登录时门户的管理员入口全部隐藏',
     ['navAccount','navManage','navRefresh'].every(function(id){
       return getEl(id).hidden === true;
     }), JSON.stringify(['navAccount','navManage','navRefresh'].map(function(id){return getEl(id).hidden;})));
  /* 「导入」按钮属于通知中心，由它自己的 paintChrome 控制，门户不该管 */
  ok('通知中心的导入按钮由它自己的 paintChrome 控制（源码级）',
     /paintChrome = function \(\) \{[\s\S]{0,220}btnImport/.test(pageScript(htmlNotices)));
  A.closeNavUserMenu();

  /* 查看账号：有修改密码和退出登录，但没有账号管理/管理导航 */
  S.me = { id: 5, username: 'james', role: 'viewer', role_label: '查看',
           display_name: 'james.han', weak_pwd: 1, enabled: 1 };
  A.renderMe();
  ok('查看账号 chip 显示姓名', getEl('navUserChip').innerHTML.indexOf('james.han') >= 0);
  ok('查看账号 chip 显示角色', getEl('navUserChip').innerHTML.indexOf('查看') >= 0);
  ok('查看账号时管理员入口仍隐藏',
     getEl('navAccount').hidden === true && getEl('navManage').hidden === true);
  ok('查看账号时「重新探测」隐藏', getEl('navRefresh').hidden === true);
  A.openNavUserMenu();
  var mV = getEl('navUserMenu').innerHTML;
  ok('菜单含「修改我的密码」', mV.indexOf('data-navact="pwd"') >= 0 && mV.indexOf('修改我的密码') >= 0);
  ok('菜单含「退出登录」', mV.indexOf('data-navact="logout"') >= 0 && mV.indexOf('退出登录') >= 0);
  ok('菜单不含账号管理/管理导航（非管理员）',
     mV.indexOf('data-navact="account"') < 0 && mV.indexOf('data-navact="manage"') < 0);
  ok('菜单顶部显示账号与角色', mV.indexOf('james') >= 0 && mV.indexOf('查看') >= 0);
  ok('弱密码提醒条已显示', getEl('navPwdBanner').hidden === false);
  ok('弱密码提醒条含「立即修改密码」',
     getEl('navPwdBanner').innerHTML.indexOf('立即修改密码') >= 0);
  ok('两条弱密码提醒内容一致（门户 + 通知中心）',
     getEl('navPwdBanner').innerHTML.replace(/\s/g,'') ===
     getEl('pwdBanner').innerHTML.replace(/\s/g,''));
  A.closeNavUserMenu();

  /* 管理员：菜单多出账号管理与管理导航 */
  S.me = { id: 1, username: 'admin', role: 'admin', role_label: '管理员',
           display_name: '系统管理员', weak_pwd: 0, enabled: 1 };
  A.renderMe();
  ok('管理员时账号管理入口显示', getEl('navAccount').hidden === false);
  ok('管理员时管理导航入口显示', getEl('navManage').hidden === false);
  ok('管理员时重新探测显示', getEl('navRefresh').hidden === false);
  A.openNavUserMenu();
  var mA = getEl('navUserMenu').innerHTML;
  /* 账号管理 / 管理导航 已经是顶栏上的独立按钮，菜单里不再重复 */
  ok('菜单不重复放账号管理与管理导航（顶栏已有独立按钮）',
     mA.indexOf('data-navact="account"') < 0 && mA.indexOf('data-navact="manage"') < 0,
     mA.slice(0, 160));
  ok('管理员菜单有修改密码与退出登录',
     mA.indexOf('data-navact="pwd"') >= 0 && mA.indexOf('data-navact="logout"') >= 0);
  ok('账号管理仍是顶栏独立按钮（一键可达）',
     html.indexOf('id="navAccount"') >= 0 && html.indexOf('<span class="hmob">账号管理</span>') >= 0);
  ok('强密码时不显示弱密码提醒', (function(){
    A.renderMe();
    return getEl('navPwdBanner').hidden === true && getEl('navPwdBanner').innerHTML === '';
  })());
  A.closeNavUserMenu();
  ok('菜单可关闭', getEl('navUserMenu').classList.contains('on') === false);

  head('[22] 账号功能只保留在导航中心（通知中心已迁走）');
  /* 顶栏按钮看 markup；「更多」菜单项在 openMenu() 函数体里，要单独切出来看 */
  var appSeg = htmlNotices.slice(htmlNotices.indexOf('<div id="app" hidden>'),
                                 htmlNotices.indexOf('<div id="modalRoot">'));
  var noticesJs = pageScript(htmlNotices);
  var menuSrc = (function () {
    var i = noticesJs.indexOf('function openMenu()');
    return i < 0 ? '' : noticesJs.slice(i, i + 1600);
  })();
  ok('切到了 openMenu 源码', menuSrc.length > 300, 'len=' + menuSrc.length);
  ok('通知中心顶栏不再有身份 chip', appSeg.indexOf('id="userChip"') < 0);
  ok('通知中心「更多」不再有账号管理', menuSrc.indexOf("mi('users'") < 0);
  ok('通知中心「更多」不再有 修改密码/退出登录',
     menuSrc.indexOf("mi('pwd'") < 0 && menuSrc.indexOf("mi('logout'") < 0);
  ok('通知中心「更多」改为「返回导航中心」', menuSrc.indexOf("mi('backportal'") >= 0);
  ok('通知中心仍保留弱密码提醒（提醒不等于账号管理）',
     appSeg.indexOf('id="pwdBanner"') >= 0);
  ok('门户仍有完整账号功能',
     htmlPortal.indexOf('id="navUserChip"') >= 0 &&
     htmlPortal.indexOf('id="navPwdBanner"') >= 0);

  head('[23] 「登录」一键直达，不再二次弹菜单');
  ok('未登录时点 chip 直接跳登录页（源码级）',
     /if \(!S\.me\) return go\('notices'\);/.test(htmlPortal));
  ok('未登录时不会走 openNavUserMenu',
     /navUserChip'\)\.addEventListener[\s\S]{0,260}?if \(!S\.me\) return go/.test(htmlPortal));

  head('[24] Winbox 本地联动（nh:// 协议 + 安装说明）');
  ok('页面含 nh:// 协议生成逻辑',
     htmlPortal.indexOf("'nh://winbox?host='") >= 0);
  ok('含「点了没反应」兜底定时器', htmlPortal.indexOf('function armWinboxFallback') >= 0);
  ok('兜底只在没成功过时提醒（记住成功状态）',
     htmlPortal.indexOf('nh_winbox_ok') >= 0);
  ok('含安装说明弹窗', htmlPortal.indexOf('function openWinboxHelp') >= 0);
  ok('含零安装路径：复制地址 + 引导粘贴',
     htmlPortal.indexOf('function openWinboxLaunch') >= 0
     && htmlPortal.indexOf('Connect To') >= 0
     && htmlPortal.indexOf('data-act="copyhost"') >= 0);
  ok('点 Winbox 卡片走复制流程而不是死协议导航',
     htmlPortal.indexOf('openWinboxLaunch(wbHost') >= 0
     && /data-winbox[\s\S]{0,400}preventDefault/.test(htmlPortal));
  ok('弹窗里保留 nh:// 直启与安装入口（给装了的人）',
     htmlPortal.indexOf("'nh://winbox?host=' + encodeURIComponent(host)") >= 0
     && htmlPortal.indexOf("data-act=\"setup\"") >= 0);
  ok('卡片提示语说明是「点一下复制地址」',
     htmlPortal.indexOf('点一下复制设备地址') >= 0);
  ok('说明里有测试按钮（装完可自测）',
     htmlPortal.indexOf('测试打开 Winbox') >= 0 && htmlPortal.indexOf("'nh://winbox?host='") >= 0);
  ok('说明里提供三个文件下载',
     htmlPortal.indexOf('/tools/nh-setup.cmd') >= 0
     && htmlPortal.indexOf('/tools/nh-setup.ps1') >= 0
     && htmlPortal.indexOf('/tools/nh-open.ps1') >= 0);
  ok('说明里讲了 winbox 路径可配、存在本机',
     htmlPortal.indexOf('config.json') >= 0 && htmlPortal.indexOf('每台电脑的 winbox.exe 路径都不一样') >= 0);
  ok('说明里有排查/改路径/批量的命令',
     htmlPortal.indexOf('-Show') >= 0 && htmlPortal.indexOf('-Uninstall') >= 0);
  ok('账号菜单里有 Winbox 设置入口',
     htmlPortal.indexOf("navUserMenuItem('winbox'") >= 0);
  ok('点卡片会触发兜底（并带上设备地址）',
     /data-winbox/.test(htmlPortal) && /armWinboxFallback\(/.test(htmlPortal)
     && /data-host=/.test(htmlPortal));
  /* 本地处理程序必须严格校验参数：nh:// 能被任意网页触发 */
  /* 浏览器会把 nh://winbox?host=X 规范化成 nh://winbox/?host=X，
     只认前者的话从网页点必然失败（安装器自测却能过）。这里必须容忍可选斜杠。 */
  ok('处理程序容忍浏览器规范化后的链接（winbox 后可选斜杠）',
     /nh:\/\/winbox\/\?\\\?/.test(NH_PS1),
     'len=' + NH_PS1.length);
  /* 查询串必须按 host= 取值，不能把整串当地址 —— 否则 nh://winbox?a=b 也会去启动 winbox */
  ok('查询串只从 host= 取值（其余参数不参与）',
     NH_PS1.indexOf('host=([^&]+)') >= 0 || NH_PS1.indexOf('host=') >= 0);
  ok('无法识别时提示支持的形式（便于自助排查）',
     NH_PS1.indexOf('无法识别的链接') >= 0 && NH_PS1.indexOf('nh://winbox/?host=') >= 0);
  ok('处理程序对地址做了白名单校验（防命令注入）',
     /\^\[A-Za-z0-9\]/.test(NH_PS1) && NH_PS1.indexOf('-notmatch') >= 0);
  ok('处理程序用 Start-Process 启动、不拼 shell',
     NH_PS1.indexOf('Start-Process') >= 0 && NH_PS1.indexOf('Invoke-Expression') < 0);
  /* 注册表操作从 .cmd 移到了 .ps1（.cmd 现在是薄包装） */
  ok('安装器只写当前用户 HKCU（无需管理员）',
     (NH_CMD + NH_SETUP_PS1).indexOf('HKCU') >= 0
     && (NH_CMD + NH_SETUP_PS1).indexOf('HKLM') < 0,
     'HKCU=' + ((NH_CMD + NH_SETUP_PS1).indexOf('HKCU') >= 0) +
     ' HKLM=' + ((NH_CMD + NH_SETUP_PS1).indexOf('HKLM') >= 0));
  ok('安装器支持卸载', (NH_CMD + NH_SETUP_PS1).toLowerCase().indexOf('uninstall') >= 0);
  /* 自测若只测字面量 nh://winbox?host=，就会绕过浏览器的规范化，
     出现「自测通过、网页点了没反应」——本轮就栽在这里，必须用真实形式自测。 */
  ok('安装器自测覆盖浏览器真实发出的形式（带空路径）',
     NH_SETUP_PS1.indexOf("'nh://winbox/?host=10.94.6.9'") >= 0
     && NH_SETUP_PS1.indexOf("'nh://winbox?host=10.94.6.9'") >= 0);
  /* 每台 PC 的 winbox 路径不同，必须能指定 —— 这是本轮的核心诉求 */
  ok('安装器提供文件选择框指定 winbox.exe',
     NH_SETUP_PS1.indexOf('OpenFileDialog') >= 0 && NH_SETUP_PS1.indexOf('winbox.exe') >= 0);
  ok('安装器支持命令行直接指定路径（批量部署）',
     NH_SETUP_PS1.indexOf('[string]$WinboxPath') >= 0 || NH_SETUP_PS1.indexOf('$WinboxPath') >= 0);
  ok('安装器把路径写到本机 config.json',
     NH_SETUP_PS1.indexOf('config.json') >= 0 && NH_SETUP_PS1.indexOf('ConvertTo-Json') >= 0);
  ok('处理程序优先读本机配置的路径',
     NH_PS1.indexOf('config.json') >= 0 && NH_PS1.indexOf('cfg.winbox') >= 0);
  ok('配置里的路径失效时会提示重新指定',
     NH_PS1.indexOf('cfgStale') >= 0 && NH_PS1.indexOf('重新运行') >= 0);
  ok('能查看当前配置（-Show）',
     NH_SETUP_PS1.indexOf('$Show') >= 0 && NH_SETUP_PS1.indexOf('协议注册') >= 0);
  /* 处理程序是拷贝到本机的、不会自动更新，曾因此出现「服务端修好了、同事机器还是旧的」。
     安装器必须先从门户拉最新一份，并校验内容，拉不到才回退。 */
  ok('安装器先从门户拉取最新处理程序',
     NH_SETUP_PS1.indexOf('Invoke-WebRequest') >= 0
     && NH_SETUP_PS1.indexOf('UseBasicParsing') >= 0
     && NH_SETUP_PS1.indexOf('/tools/nh-open.ps1') >= 0);
  ok('安装器校验下载内容（拒绝旧版/错误页）',
     NH_SETUP_PS1.indexOf("'nh://winbox/?'") >= 0
     && NH_SETUP_PS1.indexOf("'Start-Process'") >= 0
     && NH_SETUP_PS1.indexOf("'Invoke-Expression'") >= 0);
  ok('安装器有三级回退（门户→同目录→已装），不会因为断网装不上',
     NH_SETUP_PS1.indexOf('(1) 先试门户') >= 0
     && NH_SETUP_PS1.indexOf('(2) 门户不可用') >= 0
     && NH_SETUP_PS1.indexOf('(3) 再退一步') >= 0);
  ok('安装器支持 -Offline 与 -BaseUrl',
     NH_SETUP_PS1.indexOf('[switch]$Offline') >= 0 && NH_SETUP_PS1.indexOf('[string]$BaseUrl') >= 0);
  ok('处理程序带版本号（便于判断本机装的是不是最新）',
     NH_PS1.indexOf('$NhHandlerVersion') >= 0);
  ok('-Show 会显示处理程序版本、并对旧版告警',
     NH_SETUP_PS1.indexOf('处理程序 :') >= 0 && NH_SETUP_PS1.indexOf('没有版本号') >= 0
     && NH_SETUP_PS1.indexOf('不认浏览器发来的链接') >= 0);
  ok('不会自己拷自己（避免 Copy-Item 报 source and destination are the same）',
     NH_SETUP_PS1.indexOf('$Src -ne $Dest') >= 0);
  /* 反引号续行一旦尾随空格就会断行，这类错在 Windows 上很难查 —— 干脆不用 */
  ok('脚本不用反引号续行',
     NH_SETUP_PS1.split('\n').every(function (l) { return l.replace(/\r$/, '').slice(-1) !== '`'; }));
  /* PowerShell 5.1 兼容性：不能用内联 if 表达式 / 错误的 -NoNewline */
  ok('不依赖 PowerShell 7 的内联 if 表达式',
     !/Say \(if |\$\(if /.test(NH_SETUP_PS1));
  ok('没有把 -NoNewline 误传给自定义 Say 函数',
     !/Say '[^']*' -NoNewline/.test(NH_SETUP_PS1));

  head('[25] 本地应用链接不能开新标签（否则浏览器会拦掉协议调用）');
  /* 这是「点了没反应」的第二种表现：加了 target="_blank" 后，
     Chrome/Edge 认为新标签里的跳转不是用户手势，直接拦掉 nh:// 调用，
     只留一个空白页显示 nh://winbox?host=... */
  var wbCard = A.navCardHtml(ITEMS[4]);
  ok('Winbox 卡片不带 target="_blank"',
     wbCard.indexOf('target="_blank"') < 0, wbCard.slice(0, 150));
  /* href 用 #：否则 Ctrl+点击/中键会开一个空白 nh:// 标签页（用户截图里就是那样） */
  ok('Winbox 卡片 href 不是死协议（避免空白标签页）',
     wbCard.indexOf('href="#"') >= 0 && wbCard.indexOf('href="nh://') < 0, wbCard.slice(0, 150));
  var wbFlat = A.flatRowHtml(ITEMS[4]);
  ok('命令面板结果里的 Winbox 项也不带 target',
     wbFlat.indexOf('target="_blank"') < 0, wbFlat.slice(0, 150));
  ok('命令面板结果里的 Winbox 项 href 也不是死协议',
     wbFlat.indexOf('href="nh://') < 0, wbFlat.slice(0, 150));
  /* 反向：真正的站外 http 链接应该照常新开标签 */
  var webCard = A.navCardHtml(ITEMS[0]);
  ok('站外 web 卡片仍然新开标签（不受影响）',
     webCard.indexOf('target="_blank"') >= 0, webCard.slice(0, 150));
  var longWeb = A.navCardHtml({ id: 9, name: 'x', url: 'https://ext.example.com/a',
                                launch: 'web', subgroup: '' });
  ok('外部域名照常新开标签', longWeb.indexOf('target="_blank"') >= 0);

  head('[26] 导航项右键菜单（直接编辑/删除单个导航项）');

  /* 用自造的干净状态：前面的用例往 S.nav 里塞过 id=1..N 的条目，
     直接拿 ITEMS[0] 会撞 id，findNavItem 找到的是别人的条目。 */
  var ctxItem = { id: 90001, group_id: 900, name: 'CTXTEST-001', url: 'https://198.19.223.29',
                  subgroup: 'CTXT', favorite: 0, visit_count: 0, launch: 'web' };
  var ctxWinbox = { id: 90002, group_id: 900, name: 'CTXWB-002', url: '', subgroup: 'CTX',
                    favorite: 0, visit_count: 0, launch: 'winbox', launch_host: '192.168.88.9' };
  S.nav = { groups: [{ id: 900, name: 'CTX', items: [ctxItem, ctxWinbox] }] };
  ok('测试状态自洽（findNavItem 能取到专用条目）',
     A.findNavItem(90001) && A.findNavItem(90001).name === 'CTXTEST-001');

  ok('未打开菜单时没有菜单节点', A.hasNavCtx() === false);

  S.me = { role: 'viewer', username: 'v' };
  A.openNavCtxMenu(120, 120, ctxItem.id);
  var menuHtml = (document.body._kids || []).length
    ? document.body._kids[document.body._kids.length - 1].innerHTML : '';
  ok('右键后生成了菜单节点', A.hasNavCtx() === true, 'hasCtx=' + A.hasNavCtx());
  ok('菜单里带「复制地址」', menuHtml.indexOf('复制地址') >= 0, menuHtml.slice(0, 120));
  ok('菜单顶部显示该项名称（知道改的是哪一条）',
     menuHtml.indexOf('ctxname') >= 0 && menuHtml.indexOf(ctxItem.name) >= 0,
     'name=' + JSON.stringify(ctxItem.name) + ' html=' + menuHtml.slice(0, 160));
  ok('普通用户看不到「编辑此项」', menuHtml.indexOf('编辑此项') < 0);
  ok('普通用户看不到「删除此项」', menuHtml.indexOf('删除此项') < 0);

  A.closeNavCtx();
  ok('closeNavCtx 把菜单节点移除了', A.hasNavCtx() === false);

  S.me = { role: 'admin', username: 'a' };
  A.openNavCtxMenu(120, 120, ctxItem.id);
  var adminHtml = document.body._kids[document.body._kids.length - 1].innerHTML;
  ok('管理员能看到「编辑此项」', adminHtml.indexOf('编辑此项') >= 0, adminHtml.slice(0, 160));
  ok('管理员能看到「删除此项」', adminHtml.indexOf('删除此项') >= 0);
  ok('删除项带 danger 样式（防误点）',
     /data-ctx="del"[^>]*class="danger"|class="danger"[^>]*data-ctx="del"/.test(adminHtml),
     adminHtml.slice(0, 200));
  A.closeNavCtx();

  /* 三个卡片容器都要挂 contextmenu 监听，否则「最近访问」「搜索平铺」里右键没反应 */
  ['navGroups', 'navFlat', 'navRecent'].forEach(function (cid) { el(cid); });
  A.bindNavCtxMenus();
  ['navGroups', 'navFlat', 'navRecent'].forEach(function (cid) {
    ok('容器 ' + cid + ' 已绑定 contextmenu',
       !!(getEl(cid).L && getEl(cid).L.contextmenu && getEl(cid).L.contextmenu.length));
  });

  head('[27] 左侧栏蓝底铺满左侧（固定定位，不受内容高度影响）');

  /* 原来是 sticky + align-self:flex-start + max-height，高度只等于内容高度：
     分组少时下方留白，页面一长左列断成白色。改成固定定位后蓝底恒满。 */
  function cssBlock(sel) {
    var i = COMMON_CSS.indexOf(sel + '{');
    if (i < 0) return '';
    var j = COMMON_CSS.indexOf('}', i);
    return COMMON_CSS.slice(i, j + 1);
  }
  var pside = cssBlock('.pside');
  ok('.pside 用固定定位', /position:fixed/.test(pside), pside.slice(0, 90));
  ok('.pside 上沿贴顶栏、下沿到底部', /top:60px/.test(pside) && /bottom:0/.test(pside),
     pside.slice(0, 120));
  ok('.pside 不再靠 sticky/max-height 决定高度',
     !/position:sticky/.test(pside) && !/max-height/.test(pside), pside.slice(0, 120));
  ok('.pside 保留深蓝底色', /background:var\(--side-bg\)/.test(pside));
  ok('.pmain 让出侧栏宽度（margin-left 250px）',
     /margin-left:250px/.test(cssBlock('.pmain')), cssBlock('.pmain'));
  /* 窄屏必须还原，否则固定侧栏会盖住内容 */
  var mob = COMMON_CSS.slice(COMMON_CSS.indexOf('@media (max-width:880px)'));
  ok('窄屏下侧栏回到文档流', /position:static/.test(mob.slice(0, 600)));
  ok('窄屏下内容区不再留左边距', /margin-left:0/.test(mob.slice(0, 700)));

  head('[28] 管理导航：长分组用滚动窗口，新增按钮常驻');
  ok('导航项列表有限高滚动容器', /\.navm-scroll\{[^}]*max-height/.test(COMMON_CSS));
  ok('分组列表也限高（16 组不至于把弹窗拉长）',
     /\.navm-groups\{[^}]*max-height/.test(COMMON_CSS));
  ok('表头吸顶（滚到深处仍知道列是什么）',
     /#navmItems thead th\{[^}]*position:sticky/.test(COMMON_CSS));

  head('[29] 通知中心：列表右键菜单（直接编辑单条通知）');

  ok('定义了右键菜单函数', /function openNoticeCtxMenu\(/.test(htmlNotices) &&
     /function closeNoticeCtx\(/.test(htmlNotices));
  ok('菜单动作齐全（查看/复制/编辑/置顶/删除）',
     ['view', 'copy', 'edit', 'pin', 'del'].every(function (a) {
       return htmlNotices.indexOf('data-nctx="' + a + '"') >= 0;
     }));
  ok('管理操作被 isAdmin() 包住（普通用户看不到）',
     /if \(isAdmin\(\)\) \{[\s\S]{0,400}data-nctx="edit"/.test(htmlNotices));
  ok('删除项带 danger 样式', /data-nctx="del" class="danger"/.test(htmlNotices));
  ok('列表容器绑定了 contextmenu（具名函数，可被测试调用）',
     /function bindNoticeCtxMenu\(/.test(htmlNotices) &&
     /bindNoticeCtxMenu\(\);/.test(htmlNotices));
  ok('编辑走 openEditor（复用既有表单，不另造一套）',
     /api\('\/api\/notices\/' \+ id\)\.then\(function \(res\) \{\s*openEditor\(res\.item\)/.test(htmlNotices));
  ok('菜单样式复用 common.css 的 .ctxmenu（两页共用）',
     /\.ctxmenu\{/.test(COMMON_CSS) && /\.ctxmenu \.ctxname\{/.test(COMMON_CSS));

  console.log('\n' + '='.repeat(58));
  if (fail) {
    console.log('\x1b[31m失败 ' + fail + ' / ' + (pass + fail) + ' 项\x1b[0m');
    process.exit(1);
  }
  console.log('\x1b[32m全部通过：' + pass + ' 项\x1b[0m');
})();
