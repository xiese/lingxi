// 第 5 项前端：正文绝对路径识别 + 徽标渲染。
//
// 为什么不用 jsdom 跑整页：m2() 依赖 marked、escapeHtml、jss 一整套运行时，
// 整页跑起来慢且脆。这里从 app.js 抠出真实函数源码单独测（不复制粘贴），
// 规则错了立刻红，不会被渲染细节掩盖。
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const APP = path.resolve(__dirname, '../../scripts/web/app.js');
const src = fs.readFileSync(APP, 'utf8');

// 从 app.js 抠函数源码：从 `function name(` 起按花括号配平截取
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

const PRELUDE = `
function escapeHtml(s){
  return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;')
    .replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
`;
const sandbox = new Function(
  PRELUDE + grab('markAbsPaths') + '\nreturn { markAbsPaths, escapeHtml };'
)();
const { markAbsPaths } = sandbox;
const esc = function(s){return sandbox.escapeHtml(s)};

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

console.log('markAbsPaths / 只认绝对路径');

ok('绝对路径被包成徽标占位', () => {
  const h = markAbsPaths('<p>见 /srv/notes/示例/docs/a.md 这里</p>');
  assert(/class="abs-path"/.test(h), h);
  assert(/data-abs="\/srv\/notes\/示例\/docs\/a.md"/.test(h), h);
});

ok('相对路径 ./docs/a.md 不识别', () => {
  const h = markAbsPaths('<p>见 ./docs/a.md 这里</p>');
  assert(!/class="abs-path"/.test(h), h);
});

ok('裸文件名 a.md 不识别', () => {
  const h = markAbsPaths('<p>见 a.md 这里</p>');
  assert(!/class="abs-path"/.test(h), h);
});

ok('http 链接里的 / 不识别', () => {
  const h = markAbsPaths('<p>https://x.com/a/b 和 ftp://y/z</p>');
  assert(!/class="abs-path"/.test(h), h);
});

ok('代码块里的路径不识别', () => {
  const h = markAbsPaths('<pre><code>/srv/notes/示例/a.md</code></pre>');
  assert(!/class="abs-path"/.test(h), h);
});

ok('行内代码里的路径不识别', () => {
  const h = markAbsPaths('<p>写法是 <code>/srv/notes/示例/a.md</code> 这样</p>');
  assert(!/class="abs-path"/.test(h), h);
});

ok('已经有 <a> 的路径不重复包', () => {
  const h = markAbsPaths('<a href="/srv/notes/a.md">/srv/notes/a.md</a>');
  assert(!/class="abs-path"/.test(h), h);
});

ok('中文标点不算路径的一部分', () => {
  const h = markAbsPaths('<p>路径是 /srv/notes/示例/docs/a.md，后面还有字</p>');
  assert(/data-abs="\/srv\/notes\/示例\/docs\/a.md"/.test(h), h);
});

ok('路径里的 & 在属性里被转义，不产生标签注入', () => {
  // marked 输出的正文里 < > 已经是实体；这里只验 & 和引号不会破坏属性
  const h = markAbsPaths('<p>见 /srv/notes/a&b/c.md 这里</p>');
  assert(/data-abs="\/srv\/notes\/a&amp;b\/c\.md"/.test(h), '属性未转义: ' + h);
});

ok('路径里带引号不会截断属性', () => {
  const h = markAbsPaths('<p>/srv/notes/a&quot;b.md</p>');
  const m = h.match(/data-abs="([^"]*)"/);
  assert(m, '没包成徽标: ' + h);
  assert(!/onmouseover/.test(h), '注入风险: ' + h);
});

ok('空串和纯文本不炸', () => {
  assert.strictEqual(markAbsPaths(''), '');
  assert.strictEqual(markAbsPaths('没有路径'), '没有路径');
  assert.strictEqual(markAbsPaths(null), '');
});

ok('同一路径出现两次都各自包（不靠去重影响渲染）', () => {
  const h = markAbsPaths('<p>/srv/notes/a/b.md 和 /srv/notes/a/b.md</p>');
  const n = (h.match(/class="abs-path"/g) || []).length;
  assert.strictEqual(n, 2, h);
});

ok('中文引号不会被吃进路径', () => {
  const out = markAbsPaths('<p>“/srv/notes/e.md”引号里</p>');
  const got = [...out.matchAll(/data-abs="([^"]*)"/g)].map(m => m[1]);
  assert.deepStrictEqual(got, ['/srv/notes/e.md'], '右引号被当成路径一部分：' + JSON.stringify(got));
});

ok('破折号省略号不会被吃进路径', () => {
  const out = markAbsPaths('<p>/srv/notes/g.md——后面</p>');
  const got = [...out.matchAll(/data-abs="([^"]*)"/g)].map(m => m[1]);
  assert.deepStrictEqual(got, ['/srv/notes/g.md'], JSON.stringify(got));
});

ok('URL 不会被误认成路径', () => {
  for (const t of ['https://example.com/a/b', 'ftp://x/y/z', 'http://192.0.2.1:8899/notes/x']) {
    const out = markAbsPaths('<p>' + t + '</p>');
    assert(!/data-abs=/.test(out), '误认：' + t);
  }
});

ok('分数和日期不会被误认成路径', () => {
  for (const t of ['12/25 和 3/4 分', '比例 1/2 与 3/8', '2/3 的人']) {
    const out = markAbsPaths('<p>' + t + '</p>');
    assert(!/data-abs=/.test(out), '误认：' + t);
  }
});

ok('中文斜杠词组不被当成路径（前端/后端、读/写）', () => {
  const cases = ['前端/后端分离', '读/写模式', '输入/输出', '开/关状态', '白天/黑夜'];
  cases.forEach(t => {
    const got = [...markAbsPaths('<p>' + esc(t) + '</p>').matchAll(/data-abs="([^"]*)"/g)].map(m => m[1]);
    assert.deepStrictEqual(got, [], '误判: ' + t + ' -> ' + JSON.stringify(got));
  });
});

ok('全角标点后面的路径仍要识别（：，「」）', () => {
  [['路径：/srv/notes/a.md', '/srv/notes/a.md'], ['「/srv/notes/d.md」', '/srv/notes/d.md']].forEach(([t, want]) => {
    const got = [...markAbsPaths('<p>' + esc(t) + '</p>').matchAll(/data-abs="([^"]*)"/g)].map(m => m[1]);
    assert.deepStrictEqual(got, [want], t);
  });
});

ok('路径含 & 不被二次转义', () => {
  const out = markAbsPaths('<p>' + esc('/srv/notes/a&b.md') + '</p>');
  assert(!out.includes('&amp;amp;'), '出现双重转义: ' + out);
  const attr = out.match(/data-abs="([^"]*)"/)[1];
  assert.strictEqual(attr.replace(/&amp;/g, '&'), '/srv/notes/a&b.md');
});

ok('不用 lookbehind（旧 WebView 兼容）', () => {
  const src = fs.readFileSync(__dirname + '/../../scripts/web/app.js', 'utf8');
  assert(!src.includes('?<!'), 'app.js 里还有 lookbehind，旧 WebView 会白屏');
});

ok('带空格的路径不截断误标（/srv/notes/a b.md）', () => {
  // 真存在 `/srv/notes/a b.md` 这种文件，正则遇到空格会断成 `/srv/notes/a`。
  // 截断后 stat 的是另一个路径，徽标与原文无关 → 宁可不标。
  const cases = [
    '/srv/notes/a b.md',
    '见 /srv/notes/a b.md 这里',
    '文件 /srv/notes/my docs/x.py 说明',
    '/tmp/x y.json',
  ];
  cases.forEach(t => {
    const got = [...markAbsPaths('<p>' + esc(t) + '</p>').matchAll(/data-abs="([^"]*)"/g)].map(m => m[1]);
    assert.deepStrictEqual(got, [], '截断误标: ' + t + ' -> ' + JSON.stringify(got));
  });
});

ok('空格后接新路径仍要正常识别', () => {
  const got = [...markAbsPaths('<p>/srv/notes/a.md /srv/notes/b.md</p>').matchAll(/data-abs="([^"]*)"/g)].map(m => m[1]);
  assert.deepStrictEqual(got, ['/srv/notes/a.md', '/srv/notes/b.md']);
});

ok('版本号不被当成文件名片段', () => {
  const got = [...markAbsPaths('<p>/srv/notes/a.md 2.0 版本</p>').matchAll(/data-abs="([^"]*)"/g)].map(m => m[1]);
  assert.deepStrictEqual(got, ['/srv/notes/a.md'], '被误伤: ' + JSON.stringify(got));
});

console.log('\n' + pass + ' passed, ' + (process.exitCode ? '有失败' : '全绿'));
