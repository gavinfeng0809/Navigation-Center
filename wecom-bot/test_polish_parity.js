#!/usr/bin/env node
/**
 * 规则版「一键整理」的一致性对拍
 * ================================
 *
 * `wecom-bot/polish_rules.py` 是从 `public/notices.html` 里那段 JS 逐行移植的。
 * 移植最怕的就是「看着像、其实不一样」——所以这里把页面里**真实的 JS 源码**
 * 抽出来在 Node 里跑，再把同一批输入喂给 Python 版，两边输出必须**逐字节相同**。
 *
 * 这样做的价值：以后改了任一边，这个测试立刻报错，不会出现
 * 「页面点一键整理」和「企微自动整理」产出两种格式的情况。
 *
 * 跑法：node wecom-bot/test_polish_parity.js
 */

'use strict';

const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');

const HERE = __dirname;
const HTML = path.join(HERE, '..', 'public', 'notices.html');

// ---------------------------------------------------------------- 抽 JS

function extractPolishSource() {
  const html = fs.readFileSync(HTML, 'utf8');
  const start = html.indexOf('var POLISH_HDR =');
  if (start < 0) throw new Error('在 notices.html 里找不到 var POLISH_HDR —— 页面结构变了？');

  // 从 polishRecord 的函数体开始做花括号配对，确定片段结尾
  const fnStart = html.indexOf('function polishRecord(block) {', start);
  if (fnStart < 0) throw new Error('找不到 polishRecord 函数');

  let depth = 0, i = html.indexOf('{', fnStart), end = -1;
  for (; i < html.length; i++) {
    const ch = html[i];
    if (ch === '{') depth++;
    else if (ch === '}') { depth--; if (depth === 0) { end = i + 1; break; } }
  }
  if (end < 0) throw new Error('polishRecord 花括号没配平');

  return html.slice(start, end);
}

const src = extractPolishSource();
// eslint-disable-next-line no-new-func
const factory = new Function(src + '\nreturn { polishRecord: polishRecord, polishKeywords: polishKeywords };');
const js = factory();

// ---------------------------------------------------------------- 测试输入

const CASES = [
  { name: '企微真通知（原始报文）', text:
`【重要通知】客户公网IP回收注意事项

各位同事：

接供应商通知，以下客户公网IP需要回收，请相关同事在 2026-10-20 前完成确认。

1. 客户：星海科技（XHKE）
2. 涉及IP：203.0.113.10 / 203.0.113.11
3. 请务必在回收前确认业务已迁移

如有异常请联系 NOC 值班同事反馈。` },

  { name: '邮件式头部字段 + 落款', text:
`重要通知：钻石客户央视国际YSGJ 新增SLA级监控通知

发布人：Gavin Feng
发布时间：2026-08-18

Dear Team,

IPVPN 链路已开通全程 SLA 监控，告警将发送至 vipcustomeralert@Telstra-pbs.cn。
请值班同事在收到告警后按流程排查，超过 3 分钟未恢复需通知客户。

Best Regards
Gavin` },

  { name: '短文本、无要点', text: '通知：测试归档' },

  { name: '带英文代号与割接信息', text:
`【紧急通知】华南区线路割接

割接窗口：2026-10-22 01:00 - 05:00
涉及线路：WALT-A0231

请务必在窗口前 30 分钟完成告警屏蔽，结束后恢复。
超过窗口未恢复请立即升级。` },

  { name: '只有一行', text: '重要通知 ：测试' },

  { name: '含 markdown 与链接', text:
`重要通知：CSE 排班系统维护

- 维护时间：本周六 22:00
- 影响范围：全部排班查询功能

详 https://doc.weixin.qq.com/sheet/abc
请联系 James Han 反馈。` },
];

// ---------------------------------------------------------------- 对拍

let fail = 0;
const pyScript = path.join(HERE, 'polish_rules.py');

for (const c of CASES) {
  let pyOut = '';
  try {
    pyOut = execFileSync(
      path.join(HERE, 'venv', 'bin', 'python'),
      ['-c',
       'import sys, polish_rules; sys.stdout.write(polish_rules.polish_record(sys.stdin.read()))'],
      { input: c.text, cwd: HERE, encoding: 'utf8', maxBuffer: 1 << 20 },
    );
  } catch (e) {
    console.log(`  ✗ ${c.name}：Python 版执行失败 ${e.message}`);
    fail++;
    continue;
  }
  const jsOut = js.polishRecord(c.text);

  if (jsOut === pyOut) {
    console.log(`  ✓ ${c.name}（${pyOut.length} 字节，两边一致）`);
  } else {
    fail++;
    console.log(`  ✗ ${c.name}：输出不一致`);
    const a = jsOut.split('\n'), b = pyOut.split('\n');
    for (let i = 0; i < Math.max(a.length, b.length); i++) {
      if (a[i] !== b[i]) {
        console.log(`      行 ${i + 1}:`);
        console.log(`        JS : ${JSON.stringify(a[i])}`);
        console.log(`        PY : ${JSON.stringify(b[i])}`);
      }
    }
  }

  // 关键字也要单独比（标签字段直接用它的结果）
  const jsKw = js.polishKeywords(c.text, '').join('|');
  let pyKw = '';
  try {
    pyKw = execFileSync(
      path.join(HERE, 'venv', 'bin', 'python'),
      ['-c',
       'import sys, polish_rules, json; sys.stdout.write(json.dumps(polish_rules.polish_keywords(sys.stdin.read(), ""), ensure_ascii=False))'],
      { input: c.text, cwd: HERE, encoding: 'utf8', maxBuffer: 1 << 20 },
    );
  } catch (e) { /* 上面已报错 */ }
  if (jsKw !== JSON.parse(pyKw || '[]').join('|')) {
    fail++;
    console.log(`  ✗ ${c.name}：关键字不一致`);
    console.log(`      JS : ${jsKw}`);
    console.log(`      PY : ${pyKw}`);
  }
}

console.log('');
if (fail) {
  console.log(`✗ 有 ${fail} 项不一致 —— Python 移植版和页面 JS 已经对不上了`);
  process.exit(1);
}
console.log(`✓ 全部一致（${CASES.length} 组输入 × 正文 + 关键字）`);
