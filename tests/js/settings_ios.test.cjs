// 第 13 项：设置页改成分组式（类 iOS 设置）
// 验收点（17:34 C 节 + 12:48 维护者定）：
//   1. 一级页是分组圆角列表：名称 + 右侧当前值 + 箭头
//   2. 点进二级页改具体项（不是所有项摊在一页）
//   3. 顶部可搜索设置项
//   4. 所有原有设置项一个都不能少
// 运行：node tests/js/settings_ios.test.cjs
"use strict";
const fs = require("fs");
const path = require("path");
const assert = require("assert");

const ROOT = path.resolve(__dirname, "../..");
const APP = path.join(ROOT, "scripts/web/app.js");
const HTML = path.join(ROOT, "scripts/web/index.html");

const src = fs.readFileSync(APP, "utf8");
const html = fs.readFileSync(HTML, "utf8");

let n = 0;
function ok(cond, msg) {
  n++;
  assert.ok(cond, `断言 ${n} 失败：${msg}`);
}

// 取 _SET_GROUPS 定义块
function groupsBlock() {
  const i = src.indexOf("var _SET_GROUPS");
  assert.ok(i > 0, "找不到 _SET_GROUPS");
  const j = src.indexOf("];", i);
  return src.slice(i, j);
}
const gb = groupsBlock();
const keys = [...gb.matchAll(/\{k:'([a-z0-9_]+)'/g)].map((m) => m[1]);

// ① 原有设置项一个都不能少（23 项，与待办里「所有原有设置项都能在新分组里找到」对应）
const REQUIRED = [
  "llm_base", "llm_key", "llm_model",
  "embedding_base", "embedding_key", "embedding_model", "embedding_type",
  "rerank_enabled", "rerank_model",
  "hybrid_alpha", "dedup_threshold", "decay_tau_days", "index_auto_rebuild",
  "token_ttl", "max_log_days", "auth_watch_interval_seconds", "theme",
  "qq_push_enabled", "qq_push_webhook_url", "qq_push_webhook_secret",
  "qq_push_target", "qq_push_payload_mode", "other_server_prefixes",
];
// 原有项一个不能少；后续项允许增加（第 16 项新增了 qq_push_events）
ok(keys.length >= REQUIRED.length, `设置项数量不应少于 ${REQUIRED.length}，实际 ${keys.length}`);
REQUIRED.forEach((k) => ok(keys.indexOf(k) >= 0, `原有设置项丢了：${k}`));
for (const k of REQUIRED) ok(keys.includes(k), `设置项 ${k} 缺失`);

// ② 存在一级页渲染：右侧当前值 + 箭头
ok(/function _setHome\(\)/.test(src), "缺少一级页函数 _setHome");
ok(/class="set-cur"/.test(src), "一级列表缺少「右侧当前值」");
ok(/class="set-arrow"/.test(src), "一级列表缺少「箭头」");
ok(/set-row-link/.test(src), "一级列表项不可点击（缺 set-row-link）");

// ③ 存在二级页：点进去才显示输入框
ok(/function _setDetail\(\)/.test(src), "缺少二级页函数 _setDetail");
ok(/function _setOpen\(/.test(src), "缺少「进入二级页」_setOpen");
ok(/function _setBack\(/.test(src), "缺少「返回一级页」_setBack");
// 一级页绝对不能渲染输入控件：只有二级页才调 _setRow
const homeBody = src.slice(src.indexOf("function _setHome"), src.indexOf("function _setDetail"));
ok(!/_setRow\(/.test(homeBody), "一级页不该直接铺输入框（应只显示摘要值）");
const detailBody = src.slice(src.indexOf("function _setDetail"), src.indexOf("function _setOpen"));
// 2026-10-19：二级页改用 _setField（只渲染控件，标题和说明由大标题/小字给出）。
// 原来调 _setRow 会把「标签 + 说明」再输出一遍，标题重复出现两次。
ok(/_setField\(/.test(detailBody), "二级页必须渲染输入控件 _setField");
ok(!/_setRow\(/.test(detailBody), "二级页不该再调 _setRow（会重复输出标签和说明）");
ok(/function _setField\(/.test(src), "缺少二级页控件渲染函数 _setField");

// ④ 顶部搜索
ok(/placeholder="搜索设置项"/.test(src), "缺少搜索框");
ok(/function _setSearch\(/.test(src), "缺少搜索处理函数");
ok(/_setQ/.test(src) && /toLowerCase\(\)\.indexOf\(q\)/.test(src.replace(/\s/g, "").replace(/\.toLowerCase\(\)\.indexOf\(q\)/, ".toLowerCase().indexOf(q)")) || /indexOf\(q\)/.test(src),
  "搜索没有真正参与过滤");

// ⑤ 保存仍走原接口，且保存键与渲染的 id 一致
ok(/#sf \[id\^="s_"\]/.test(src), "二级页保存没有复用 s_<key> 取值口径");
ok(/admin\/settings/.test(src), "保存没有走 /admin/settings");
// s_ 前缀必须来自 _setField（二级页唯一的控件渲染函数），三种控件
// （select / textarea / input）都要带，少一种就会让那一类设置项保存时取不到值。
// 2026-10-19：二级页从 _setRow 换成 _setField（不再重复输出标签和说明），断言跟着改。
{
  const i = src.indexOf("function _setField");
  const j = src.indexOf("function _setDetail", i);
  const rowFn = src.slice(i, j);
  ok(rowFn.length > 100, "找不到 _setField 函数体");
  const idCount = (rowFn.match(/id="s_'\+esc\(f\.k\)\+'"/g) || []).length;
  ok(idCount === 3, `_setField 里 select/textarea/input 三处都要带 id="s_<key>"，实际 ${idCount} 处`);
}

// ⑥ CSS 只新增类，未改动既有分组卡片规则
ok(/\.set-cur\{/.test(html), "缺少 .set-cur 样式");
ok(/\.set-arrow\{/.test(html), "缺少 .set-arrow 样式");
ok(/\.set-back\{/.test(html), "缺少 .set-back 样式");
ok(/\.set-save\{/.test(html), "缺少 .set-save 样式");
ok(/\.set-card\{background:var\(--card-bg\)/.test(html), ".set-card 原规则被破坏");
// 2026-10-19 重做 iOS 范式：.set-row 加了 position:relative（用于缩进分隔线），
// 断言改为「规则里仍必须是 flex 横向布局」，不再要求 display:flex 紧贴左花括号。
{
  const m = html.match(/\.set-row\{[^}]*\}/);
  ok(!!m && /display:flex/.test(m[0]), ".set-row 原规则被破坏（缺少 display:flex）");
  ok(!!m && /min-height:44px/.test(m[0]), ".set-row 未按 iOS 44px 行高重做");
}
ok(/\.set-group-head\{/.test(html), "缺少 iOS 分组标题（卡片外小灰字）");
ok(/\.set-icon\{[^}]*width:29px/.test(html), "图标未做成彩色圆角方块");
ok(/\.set-title\{font-size:34px/.test(html), "缺少 iOS 大标题「设置」");

console.log(`✅ 第 13 项设置页：${n} 项断言全部通过（${keys.length} 个设置项）`);
