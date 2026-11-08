/**
 * 第 16 项：设置页「推哪些情况」多选的行为测试。
 * 真跑 _setField / _setSave，验证渲染出复选框、保存拼成逗号分隔字符串。
 * 跑法：node tests/js/push_events_ui.test.cjs
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.resolve(__dirname, "../..");
const APP = fs.readFileSync(path.join(ROOT, "scripts/web/app.js"), "utf8");
const HTML = fs.readFileSync(path.join(ROOT, "scripts/web/index.html"), "utf8");

let pass = 0, fail = 0;
function ok(c, m) { if (c) { pass++; } else { fail++; console.log("❌ " + m); } }

function extract(startMark, endMark) {
  const i = APP.indexOf(startMark);
  const j = APP.indexOf(endMark, i + startMark.length);
  if (i < 0 || j < 0) throw new Error("截取失败：" + startMark);
  return APP.slice(i, j);
}

// 截出设置页相关函数（从 _SET_GROUPS 到 ls()）
const block = extract("var _SET_GROUPS", "function ls(){");

// 设置项定义在**后端**（1.0.20 起由 /api/settings/schema 下发）：从 config.py 解析出
// schema 注入沙箱供渲染测试用。schema 字面量须保持 JSON 兼容（只用双引号、无 True/False）。
const CFG_SRC = fs.readFileSync(path.join(ROOT, "scripts/kb_lib/config.py"), "utf8");
function _schemaBounds() {
  const i = CFG_SRC.indexOf("SETTINGS_SCHEMA = [");
  const j = CFG_SRC.indexOf("\n]\n", i);
  return [i, j];
}
function schemaSeg() {
  const [i, j] = _schemaBounds();
  return CFG_SRC.slice(i, j);
}
// config.py 里是 Python 字面量：转成 JSON 再解析（True/False/None + 尾随逗号）。
function pyToJson(t) {
  return t.replace(/\bTrue\b/g, "true").replace(/\bFalse\b/g, "false").replace(/\bNone\b/g, "null")
    .replace(/,\s*\]/g, "]");
}
const SCHEMA = (() => {
  const [i, j] = _schemaBounds();
  return JSON.parse(pyToJson(CFG_SRC.slice(i + "SETTINGS_SCHEMA = ".length, j + 2)));
})();

function makeSandbox(fetched) {
  const els = {};
  const sandbox = {
    console,
    _SET_CACHE: { qq_push_events: "new_device,token_revoked" },
    _SET_PAGE: "",
    _SET_Q: "",
    document: {
      getElementById: (id) => els[id] || (els[id] = { id, innerHTML: "", value: "" }),
      querySelector: (sel) => {
        if (sel === '#sf [id^="s_"]') return null; // 多选没有单值控件
        if (sel === "#sf .set-checks") return sandbox.__box;
        return null;
      },
      querySelectorAll: () => [],
      createElement: () => ({ style: {}, classList: { add() {}, remove() {} } }),
      addEventListener() {},
    },
    window: { scrollTo() {} },
    toast: { show() {} },
    esc: (s) => String(s == null ? "" : s),
    A: "http://api",
    T: "tok",
    fetch: (url, opt) => { fetched.push(JSON.parse(opt.body)); return Promise.resolve({ json: () => Promise.resolve({ ok: 1 }) }); },
    Object, Array, String, JSON, Boolean,
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(block, sandbox, { filename: "app.js#settings" });
  // block 里含 `var _SET_CACHE = null;` / `var _SET_GROUPS = [];`，会覆盖预设值，这里再塞一次
  sandbox._SET_CACHE = { qq_push_events: "new_device,token_revoked" };
  sandbox._SET_GROUPS = SCHEMA;
  return { sandbox, els };
}

// ── ① 设置项里真的有「推哪些情况」，且是多选（定义在后端 schema 里） ──
const SEG = schemaSeg();
ok(/"k": "qq_push_events"/.test(SEG), "设置项里没有 qq_push_events");
ok(/"k": "qq_push_events"[\s\S]{0,200}"t": "checks"/.test(SEG), "qq_push_events 不是多选类型");
ok(/new_device": "新设备登录"/.test(SEG), "缺「新设备登录」事件");
ok(/login_failed": "连续登录失败[^"]*"/.test(SEG), "缺「连续登录失败」事件");
ok(/token_revoked": "令牌吊销[^"]*"/.test(SEG), "缺「令牌吊销」事件");

// ── ② 渲染：勾选状态来自已保存的值 ──
{
  const { sandbox } = makeSandbox([]);
  const field = sandbox._SET_GROUPS.flatMap((g) => g.items).find((f) => f.k === "qq_push_events");
  ok(!!field, "取不到 qq_push_events 字段定义");
  const html = sandbox._setField(field, { qq_push_events: "new_device,token_revoked" });
  ok(/class="set-checks"/.test(html), "没有渲染多选容器");
  ok(/data-key="qq_push_events"/.test(html), "多选容器没带 data-key，保存取不到键");
  const boxes = [...html.matchAll(/<input type="checkbox" class="s_chk" data-v="([a-z_]+)"( checked)?>/g)];
  ok(boxes.length >= 5, "复选框数量不对：" + boxes.length);
  const checked = boxes.filter((b) => b[2]).map((b) => b[1]).sort();
  ok(JSON.stringify(checked) === JSON.stringify(["new_device", "token_revoked"]),
    "已保存的两项没被勾上：" + JSON.stringify(checked));
  ok(/data-v="login_failed">/.test(html) && !/data-v="login_failed" checked/.test(html),
    "未保存的项被误勾");
}

// ── ③ 默认全关时一个都不勾 ──
{
  const { sandbox } = makeSandbox([]);
  const field = sandbox._SET_GROUPS.flatMap((g) => g.items).find((f) => f.k === "qq_push_events");
  const html = sandbox._setField(field, {});
  ok(!/ checked>/.test(html), "默认（空值）时不该有任何勾选");
  ok(sandbox._setSummary(field, {}) === "全部关闭", "默认摘要不是「全部关闭」");
  ok(sandbox._setSummary(field, { qq_push_events: "new_device,todo_due" }) === "2 项已勾选", "摘要没显示勾选数");
}

// ── ④ 保存：勾上的拼成逗号分隔字符串 ──
{
  const fetched = [];
  const { sandbox } = makeSandbox(fetched);
  const box = {
    getAttribute: (k) => (k === "data-key" ? "qq_push_events" : null),
    querySelectorAll: () => [
      { checked: true, getAttribute: () => "new_device" },
      { checked: false, getAttribute: () => "login_failed" },
      { checked: true, getAttribute: () => "todo_due" },
    ],
  };
  sandbox.__box = box;
  sandbox._setSave();
  ok(fetched.length === 1, "保存没有发出请求");
  ok(fetched[0] && fetched[0].qq_push_events === "new_device,todo_due",
    "保存值不对：" + JSON.stringify(fetched[0]));
}

// ── ⑤ 全不勾时保存空串（不是 null/undefined） ──
{
  const fetched = [];
  const { sandbox } = makeSandbox(fetched);
  sandbox.__box = {
    getAttribute: () => "qq_push_events",
    querySelectorAll: () => [{ checked: false, getAttribute: () => "new_device" }],
  };
  sandbox._setSave();
  ok(fetched[0] && fetched[0].qq_push_events === "", "全不勾时应保存空字符串");
}

// ── ⑥ 保存成功后把新值写回缓存，摘要立即变 ──
// 注意：写缓存发生在 fetch().then() 里，必须等一个微任务周期再断言，
// 否则测的是「还没回包」的状态，属于假红/假绿。
async function caseCache() {
  const fetched = [];
  const { sandbox } = makeSandbox(fetched);
  sandbox.__box = {
    getAttribute: () => "qq_push_events",
    querySelectorAll: () => [{ checked: true, getAttribute: () => "login_failed" }],
  };
  sandbox._setSave();
  await new Promise((r) => setTimeout(r, 0));
  ok(sandbox._SET_CACHE && sandbox._SET_CACHE.qq_push_events === "login_failed",
    "保存后缓存没更新：" + JSON.stringify(sandbox._SET_CACHE));
}

// ── ⑦ CSS 有对应样式，否则复选框会挤成一团 ──
ok(/\.set-checks\{/.test(HTML), "缺 .set-checks 样式");
ok(/\.set-check\{/.test(HTML), "缺 .set-check 样式");

// ── ⑧ 单值控件的保存不能被这次改动破坏 ──
{
  const fetched = [];
  const { sandbox } = makeSandbox(fetched);
  const input = { id: "s_qq_push_target", value: "home" };
  const origQS = sandbox.document.querySelector;
  sandbox.document.querySelector = (sel) => (sel === '#sf [id^="s_"]' ? input : origQS(sel));
  sandbox._setSave();
  ok(fetched[0] && fetched[0].qq_push_target === "home", "单值控件保存被破坏：" + JSON.stringify(fetched[0]));
}

(async () => {
  await caseCache();
  console.log(`\n${pass} 通过, ${fail} 失败`);
  process.exit(fail ? 1 : 0);
})();
