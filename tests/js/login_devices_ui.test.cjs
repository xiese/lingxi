/**
 * 第 15 项：设置页「登录设备」二级页 + 一键吊销。
 *
 * 为什么要有这个测试：待办原文要求设备清单「可以一键吊销」，
 * 后端 /auth/devices 早就返回了 tokens，但前端一直没有入口——
 * 等于功能只做了一半。这里真跑渲染函数，确认按钮和请求都接上了。
 *
 * 跑法：node tests/js/login_devices_ui.test.cjs
 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const ROOT = path.resolve(__dirname, "../..");
const APP = path.join(ROOT, "scripts/web/app.js");
const src = fs.readFileSync(APP, "utf8");

let pass = 0;
let fail = 0;
function ok(cond, msg) {
  if (cond) { pass++; } else { fail++; console.log("  ✗ " + msg); }
}

// 按函数名截取「最内层」函数体（花括号配平）
function extract(name) {
  const start = src.indexOf("function " + name + "(");
  if (start < 0) return "";
  const open = src.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}") { depth--; if (depth === 0) return src.slice(start, i + 1); }
  }
  return "";
}

// ── ① 设置项里真的有「登录设备」入口，且是 devices 类型 ──
{
  ok(/k:'_devices'/.test(src), "设置项里没有 _devices 入口");
  ok(/k:'_devices'[^}]*t:'devices'/.test(src), "_devices 不是 devices 类型");
}

// ── ② 一级页摘要显示「查看」，不是空白 ──
{
  const f = extract("_setSummary");
  ok(/f\.t==='devices'/.test(f), "_setSummary 没有处理 devices 类型");
  ok(/'查看'/.test(f), "devices 摘要没有显示「查看」");
}

// ── ③ 二级页真的渲染列表容器并触发加载 ──
{
  const f = extract("_setDetail");
  ok(/f\.t==='devices'/.test(f), "_setDetail 没有 devices 分支");
  ok(/id="devList"/.test(f), "二级页没有 devList 容器");
  ok(/_setLoadDevices\(\)/.test(f), "二级页没有触发 _setLoadDevices()");
}

// ── ④ 加载函数真的请求 /auth/devices ──
{
  const f = extract("_setLoadDevices");
  ok(f.length > 0, "没有 _setLoadDevices 函数");
  ok(/ap\('\/auth\/devices'\)/.test(f), "_setLoadDevices 没有请求 /auth/devices");
  ok(/it\.tokens/.test(f), "没有遍历设备下的令牌");
  ok(/it\.first_seen/.test(f) && /it\.last_seen/.test(f), "没有显示首次/最近使用时间");
  ok(/it\.uses/.test(f), "没有显示使用次数");
  ok(/_setRevoke\(/.test(f), "设备行里没有吊销按钮");
}

// ── ⑤ 吊销真的发 DELETE 到 /auth/revoke/ ──
{
  const f = extract("_setRevoke");
  ok(f.length > 0, "没有 _setRevoke 函数");
  ok(/ap\('\/auth\/revoke\/'\+encodeURIComponent\(hint\)/.test(f), "吊销没有请求 /auth/revoke/<hint>");
  ok(/'DELETE'/.test(f), "吊销没有用 DELETE 方法");
  ok(/confirm\(/.test(f), "吊销前没有二次确认");
  ok(/_setLoadDevices\(\)/.test(f), "吊销后没有刷新列表");
}

// ── ⑥ 行为：真跑 _setLoadDevices，看产出的 HTML ──
{
  const els = { devList: { innerHTML: "" } };
  const calls = [];
  const sandbox = {
    document: { getElementById: (id) => els[id] || null },
    ap: (url, method) => {
      calls.push([url, method]);
      if (url === "/auth/devices") {
        return Promise.resolve({
          count: 1,
          devices: [{
            device: "手机A", first_seen: "2026-10-01 10:00:00",
            last_seen: "2026-10-18 20:00:00", uses: 7, fails: 1,
            last_ip: "203.0.113.10",
            tokens: [{ prefix: "abc123def456...", name: "手机A", expires_at: "2027-01-01 00:00" }],
          }],
        });
      }
      return Promise.resolve({});
    },
    esc: (s) => String(s == null ? "" : s).replace(/[<>&"]/g, (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;" }[c])),
    toast: { show: () => {} },
    confirm: () => true,
    console,
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(extract("_setLoadDevices") + "\n" + extract("_setRevoke"), sandbox, { filename: "app.js" });

  sandbox._setLoadDevices();
  // 等 promise 链跑完
  setTimeout(() => {
    const html = els.devList.innerHTML;
    ok(calls.some((c) => c[0] === "/auth/devices"), "行为测试：没有请求设备列表");
    ok(/手机A/.test(html), "行为测试：设备名没有渲染出来");
    ok(/203\.0\.113\.10/.test(html), "行为测试：最近 IP 没有渲染");
    ok(/使用 7 次/.test(html), "行为测试：使用次数没有渲染");
    ok(/失败 1 次/.test(html), "行为测试：失败次数没有渲染");
    ok(/吊销/.test(html), "行为测试：没有吊销按钮");
    ok(/_setRevoke\('abc123def456\.\.\.'/.test(html), "行为测试：吊销按钮没有带上令牌前缀");
    ok(!/undefined/.test(html), "行为测试：渲染出了 undefined");

    console.log(`\n第15项前端：${pass} 通过，${fail} 失败`);
    if (fail) process.exit(1);
  }, 30);
}
