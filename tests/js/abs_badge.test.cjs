// 第 5 项前端（二）：探测结果的徽标渲染与降级。
//
// 为什么单独一个文件：markAbsPaths 是纯字符串处理，markPathBadges 要动 DOM。
// 用几十行的假 DOM 比拖 jsdom 进来快得多，也够用 —— 被测函数只用到
// querySelectorAll / getAttribute / setAttribute / classList / style / textContent。
//
// 锁死的不变量：
//   ①  三种 kind 各自画对徽标；local_note 才能点
//   ②  结果里缺某条（超 300 条被截断 / 后端漏回）→ 降级「未探测」，不能留「…」
//   ③  请求失败 → 整篇降级「未探测」
//   ④  invalid（隐藏路径、相对路径）→ 不画徽标，保持纯文字
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const APP = path.resolve(__dirname, '../../scripts/web/app.js');
const src = fs.readFileSync(APP, 'utf8');

function grab(name) {
  const re = new RegExp('function ' + name + '\\s*\\([^)]*\\)\\s*\\{', 'g');
  const m = re.exec(src);
  assert(m, '在 app.js 里找不到函数 ' + name);
  let i = m.index + m[0].length;
  let depth = 1;
  while (i < src.length && depth > 0) {
    const ch = src[i];
    if (ch === '{') depth++;
    else if (ch === '}') depth--;
    i++;
  }
  assert.strictEqual(depth, 0, '函数 ' + name + ' 花括号不平衡');
  return src.slice(m.index, i);
}

// ─── 最小假 DOM ───
function makeEl(attrs) {
  const a = Object.assign({}, attrs || {});
  const cls = new Set();
  const badge = { textContent: '…', parentNode: null };
  const el = {
    _attrs: a,
    _cls: cls,
    style: {},
    getAttribute: k => (k in a ? a[k] : null),
    setAttribute: (k, v) => { a[k] = String(v); },
    removeAttribute: k => { delete a[k]; },
    classList: {
      add: (...xs) => xs.forEach(x => cls.add(x)),
      remove: (...xs) => xs.forEach(x => cls.delete(x)),
      contains: x => cls.has(x),
    },
    querySelector: sel => (sel === '.abs-badge' ? (badge.parentNode ? badge : null) : null),
    removeChild: node => { if (node === badge) badge.parentNode = null; },
    _badge: badge,
  };
  badge.parentNode = el;
  return el;
}

function makeContainer(els) {
  return { querySelectorAll: () => els };
}

const sandbox = new Function(
  grab('absDegrade') + '\n' + grab('absUnmark') + '\n' + grab('markPathBadges') +
  '\nreturn { absDegrade, absUnmark, markPathBadges };'
)();
const { absDegrade, absUnmark, markPathBadges } = sandbox;

let pass = 0;
function ok(name, fn) {
  try {
    fn();
    pass++;
    console.log('  ✓ ' + name);
  } catch (e) {
    console.error('  ✗ ' + name + '\n    ' + e.message);
    process.exitCode = 1;
  }
}

console.log('markPathBadges / 徽标渲染');

ok('三种 kind 各自画对，只有 local_note 可点', () => {
  const a = makeEl({ 'data-abs': '/srv/notes/n.md' });
  const b = makeEl({ 'data-abs': '/srv/notes/f.md' });
  const c = makeEl({ 'data-abs': '/srv/notes/r.md' });
  const n = markPathBadges(makeContainer([a, b, c]), {
    '/srv/notes/n.md': { kind: 'local_note', note: 'a.md', clean: '/srv/notes/n.md', server: '' },
    '/srv/notes/f.md': { kind: 'local_file', note: null, clean: '/srv/notes/f.md', server: '' },
    '/srv/notes/r.md': { kind: 'remote', note: null, clean: '/srv/notes/r.md', server: '远程服务器' },
  });
  assert.strictEqual(n, 3);
  assert(a._cls.has('abs-note') && a._badge.textContent === '本机', 'local_note 画错');
  assert.strictEqual(a.getAttribute('data-note'), 'a.md', '可点却没挂 data-note');
  assert(b._cls.has('abs-file') && b._badge.textContent === '本机文件', 'local_file 画错');
  assert(!b.getAttribute('data-note'), '不可点的却挂了 data-note');
  assert(c._cls.has('abs-remote') && c._badge.textContent === '远程服务器', 'remote 画错');
  assert(!c.getAttribute('data-note'), '不可点的却挂了 data-note');
});

ok('invalid 不画徽标，且把占位「…」拆干净', () => {
  // 占位徽标是渲染阶段就插进去的，只 continue 会让它永远停在「…」。
  // 这条用例是浏览器实测发现的真 bug：隐藏路径在屏上一直是「…」。
  const a = makeEl({ 'data-abs': '/srv/notes/.ssh/id_rsa', onclick: 'return openAbsPath(this)' });
  const n = markPathBadges(makeContainer([a]), {
    '/srv/notes/.ssh/id_rsa': { kind: 'invalid', note: null, clean: '', server: '' },
  });
  assert.strictEqual(n, 0, 'invalid 也被画了');
  assert.strictEqual(a._cls.size, 0, 'invalid 被加了 class: ' + [...a._cls]);
  assert.strictEqual(a._badge.parentNode, null, '徽标节点还在，屏上会停在「…」');
  assert.strictEqual(a.getAttribute('onclick'), null, 'invalid 还挂着 onclick');
  assert.strictEqual(a.getAttribute('data-abs'), '/srv/notes/.ssh/id_rsa', '原文被改掉了');
});

console.log('降级：不能停在「…」');

ok('结果里缺这条 → 降级「未探测」（审查第 1 条）', () => {
  const a = makeEl({ 'data-abs': '/srv/notes/x.md' });
  const n = markPathBadges(makeContainer([a]), {});
  assert.strictEqual(n, 0);
  assert.strictEqual(a._badge.textContent, '未探测', '还停在 ' + a._badge.textContent);
  assert(a._cls.has('abs-unknown'), '没加 abs-unknown');
});

ok('超过 300 条被截断时，没回来的那些也降级', () => {
  const got = makeEl({ 'data-abs': '/srv/notes/1.md' });
  const lost = makeEl({ 'data-abs': '/srv/notes/301.md' });
  markPathBadges(makeContainer([got, lost]), {
    '/srv/notes/1.md': { kind: 'remote', note: null, clean: '/srv/notes/1.md', server: '' },
  });
  assert.strictEqual(got._badge.textContent, '其他服务器');
  assert.strictEqual(lost._badge.textContent, '未探测', '第 301 条没降级');
});

ok('降级后不可点（不留 data-note）', () => {
  const a = makeEl({ 'data-abs': '/srv/notes/x.md' });
  a.setAttribute('data-note', 'old.md');
  absDegrade(a);
  assert.strictEqual(a.getAttribute('data-note'), null, '降级后仍可点');
  assert.strictEqual(a.style.cursor, 'default');
});

ok('降级会清掉旧的绿标 class，不留混色', () => {
  const a = makeEl({ 'data-abs': '/srv/notes/x.md' });
  a.classList.add('abs-note');
  absDegrade(a);
  assert(!a._cls.has('abs-note'), 'abs-note 没清掉');
  assert(a._cls.has('abs-unknown'));
});

console.log('分批：超过 300 条要分开发');

ok('probePathsIn 按 300 一批切分，不丢路径', () => {
  const body = grab('probePathsIn');
  assert(/ABS_BATCH/.test(src), '没有分批常量 ABS_BATCH');
  const m = /var ABS_BATCH\s*=\s*(\d+)/.exec(src);
  assert(m, 'ABS_BATCH 定义找不到');
  assert.strictEqual(Number(m[1]), 300, '批量大小与后端上限不一致: ' + m[1]);
  assert(/paths\.slice\(i,\s*i\+ABS_BATCH\)/.test(body), '没有按批切片');
});

console.log('\n' + pass + ' passed');
