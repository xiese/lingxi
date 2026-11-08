/**
 * 第 13 项：设置页「一级列表 / 二级页」的行为测试。
 *
 * 为什么必须跑真代码：DS 审查指出，纯源码子串断言（"set-card" in home）证明不了
 * 真的渲染出来了——删掉写回 #sf 那行也照样绿。这里用 vm 把 app.js 真跑起来，
 * 调 _setHome() / _setDetail() 看实际产出的 HTML。
 *
 * 跑法：node tests/js/settings_home_behavior.test.cjs
 */
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const APP = path.join(__dirname, "..", "..", "scripts", "web", "app.js");
const src = fs.readFileSync(APP, "utf8");

// 从 app.js 里截出设置相关的代码段，塞进沙箱跑（不跑整个 app.js，避免依赖 d3/jquery）
function extract(startMarker, endMarker) {
  const i = src.indexOf(startMarker);
  const j = src.indexOf(endMarker, i);
  assert.ok(i > 0 && j > i, `截取失败：${startMarker}`);
  return src.slice(i, j);
}

// 设置项定义在**后端**（1.0.20 起由 /api/settings/schema 下发）。直接从 config.py 解析
// SETTINGS_SCHEMA 当数据源；它是 Python 字面量，要先把 Python 特有的写法转成 JSON：
// True/False/None 三个字面量，以及 Python 允许、JSON 不允许的尾随逗号。
const CONFIG = path.join(__dirname, "..", "..", "scripts", "kb_lib", "config.py");
const SCHEMA = (function () {
  const py = fs.readFileSync(CONFIG, "utf8");
  const i = py.indexOf("SETTINGS_SCHEMA = [");
  assert.ok(i > 0, "找不到 SETTINGS_SCHEMA");
  const j = py.indexOf("\n]\n", i);
  const text = py.slice(i + "SETTINGS_SCHEMA = ".length, j + 2)
    .replace(/\bTrue\b/g, "true").replace(/\bFalse\b/g, "false").replace(/\bNone\b/g, "null")
    .replace(/,\s*\]/g, "]");
  return JSON.parse(text);
})();

const rowFn = extract("function _setField", "function _setSummary");
const summaryFn = extract("function _setSummary", "// 一级页：分组圆角列表");
const homeBlock = extract("// 一级页：分组圆角列表", "function ls(){");

const SETTINGS = {
  llm_base: "https://api.example.com/v1", llm_key: "<REDACTED>", llm_model: "gpt-4o-mini",
  embedding_base: "", embedding_key: "<REDACTED>", embedding_model: "bge-m3",
  embedding_type: "bm25", rerank_enabled: false, rerank_model: "",
  hybrid_alpha: 0.3, dedup_threshold: 0.9, decay_tau_days: 90, index_auto_rebuild: true,
  token_ttl: 129600, max_log_days: 30, auth_watch_interval_seconds: 60, theme: "dark",
  qq_push_enabled: false, qq_push_webhook_url: "", qq_push_webhook_secret: "<REDACTED>",
  qq_push_target: "", qq_push_payload_mode: "generic", other_server_prefixes: "/srv/notes/a/|远程服务器",
};

function makeSandbox() {
  const els = {};
  const sandbox = {
    console,
    esc: (s) => String(s === undefined || s === null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;"),
    document: {
      getElementById: (id) => {
        if (!els[id]) els[id] = { id, innerHTML: "", value: "", focus() {}, setSelectionRange() {} };
        return els[id];
      },
      querySelector: () => null,
      querySelectorAll: () => [],
    },
    window: { scrollTo() {} },
    toast: { show() {} },
  };
  sandbox.globalThis = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(
    "var _SET_GROUPS=" + JSON.stringify(SCHEMA) + ";\n" + rowFn + "\n" + summaryFn + "\n" + homeBlock +
    "\nthis._setHome=_setHome;this._setDetail=_setDetail;this._setOpen=_setOpen;" +
    "this._setSummary=_setSummary;this._SET=function(d){_SET_CACHE=d};" +
    "this._setQ=function(q){_SET_Q=q};",
    sandbox
  );
  return { sandbox, els };
}

let n = 0;
function ok(cond, msg) {
  n++;
  assert.ok(cond, `断言 ${n} 失败：${msg}`);
}

// ① 真跑 _setHome()：必须把内容写进 #sf
{
  const { sandbox, els } = makeSandbox();
  sandbox._SET(SETTINGS);
  sandbox._setHome();
  const html = els["sf"].innerHTML;
  ok(html.length > 500, "#sf 没被写入内容（渲染没生效）");
  ok(html.includes('class="set-search"'), "一级页没有搜索框");
  ok(html.includes('class="set-arrow"'), "一级页没有箭头");
  ok(html.includes('class="set-cur"'), "一级页没有右侧当前值");
  ok(html.includes('id="setList"'), "搜索时替换的列表容器 #setList 不存在");
  // 一级页绝不能出现输入控件
  ok(!html.includes("<input id=\"s_"), "一级页直接渲染了输入框（应只显示摘要）");
  ok(!html.includes("<select id=\"s_"), "一级页直接渲染了下拉框");
  ok(!html.includes("<textarea id=\"s_"), "一级页直接渲染了多行框");
  // 23 项全部出现在一级列表里
  const listed = [...html.matchAll(/onclick="_setOpen\('([a-z0-9_]+)'\)"/g)].map((m) => m[1]);
  ok(listed.length >= 23, `一级列表不应少于 23 项，实际 ${listed.length}`);
  // 放宽数量下限后必须补偿：原有 23 项一个都不能少（防止「删一项加两项」混过去）
  const OLD_KEYS = ["llm_base", "llm_key", "llm_model", "embedding_base", "embedding_key", "embedding_model", "embedding_type", "rerank_enabled", "rerank_model", "hybrid_alpha", "dedup_threshold", "decay_tau_days", "index_auto_rebuild", "token_ttl", "max_log_days", "auth_watch_interval_seconds", "theme", "qq_push_enabled", "qq_push_webhook_url", "qq_push_webhook_secret", "qq_push_target", "qq_push_payload_mode", "other_server_prefixes"];
  OLD_KEYS.forEach((k) => ok(listed.indexOf(k) >= 0, `原有设置项丢了：${k}`));
  // 当前值真的显示出来了
  ok(html.includes("https://api.example.com/v1"), "对话服务地址的当前值没显示");
  ok(html.includes("••••••••"), "密码类没有显示掩码");
  ok(!html.includes("<REDACTED>"), "密码类把 <REDACTED> 占位符直接显示出来了");
  ok(html.includes("90 天填 129600") || html.includes("129600"), "token_ttl 当前值没显示");
  // 图标（2026-10-19 重做：图标带分组色系类名，如 class="set-icon i-brain"）
  ok(/class="set-icon(\s+[a-z-]+)?"/.test(html), "一级列表项没有图标（12:48 口径要求图标）");
}

// ② 真跑 _setDetail()：必须出现输入控件，且 id 仍是 s_<key>
{
  const { sandbox, els } = makeSandbox();
  sandbox._SET(SETTINGS);
  sandbox._setOpen("llm_model");
  const html = els["sf"].innerHTML;
  ok(html.includes('id="s_llm_model"'), "二级页没有渲染出 s_llm_model 输入框");
  ok(html.includes('value="gpt-4o-mini"'), "二级页没有带出当前值");
  ok(html.includes("保存"), "二级页没有保存按钮");
  ok(html.includes("_setBack()"), "二级页没有返回入口");
  // 只能有当前这一个输入框，不能把整页铺开
  const inputs = (html.match(/id="s_[a-z0-9_]+"/g) || []);
  ok(inputs.length === 1, `二级页只该有 1 个输入控件，实际 ${inputs.length}`);
}

// ③ 下拉类设置项：二级页要出 select，且选中当前值
{
  const { sandbox, els } = makeSandbox();
  sandbox._SET(SETTINGS);
  sandbox._setOpen("theme");
  const html = els["sf"].innerHTML;
  ok(html.includes("<select id=\"s_theme\""), "下拉类没渲染 select");
  ok(/value="dark" selected/.test(html), "下拉类没有选中当前值 dark");
}

// ④ 搜索：真过滤
{
  const { sandbox, els } = makeSandbox();
  sandbox._SET(SETTINGS);
  sandbox._setQ("密钥");
  sandbox._setHome();
  const html = els["sf"].innerHTML;
  const listed = [...html.matchAll(/onclick="_setOpen\('([a-z0-9_]+)'\)"/g)].map((m) => m[1]);
  ok(listed.length > 0 && listed.length < 23, `搜索应过滤出部分项，实际 ${listed.length}`);
  ok(!listed.includes("hybrid_alpha"), "搜索「密钥」不该出现 hybrid_alpha");
  ok(listed.includes("llm_key"), "搜索「密钥」应能找到对话服务密钥");
}

// ⑤ 搜索无结果要有提示，不能静默空白
{
  const { sandbox, els } = makeSandbox();
  sandbox._SET(SETTINGS);
  sandbox._setQ("zzz不存在的设置zzz");
  sandbox._setHome();
  ok(els["sf"].innerHTML.includes("没有匹配的设置项"), "搜不到时没有提示");
}

// ⑥ 二级页拿到不存在的 key 要退回一级页（不能白屏）
{
  const { sandbox, els } = makeSandbox();
  sandbox._SET(SETTINGS);
  sandbox._setOpen("不存在_的_键");
  const html = els["sf"].innerHTML;
  ok(html.includes('class="set-search"'), "未知键没有退回一级页");
}

console.log(`✅ 第 13 项设置页行为：${n} 项断言全部通过`);
