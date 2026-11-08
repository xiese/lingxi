"""设置页 schema 与键集合的回归测试。

1.0.20 起设置页结构由后端 `GET /api/settings/schema` 下发（单一真相源 =
`config.SETTINGS_SCHEMA`），前端不再硬编码 `_SET_GROUPS`。本文件相应改成
「比对 config.py 里那两张表」，并加一条反向守卫：前端不许再把设置项定义搬回去。

锁死四件事：
    ① 写入白名单必须放行 DEFAULT_SETTINGS 里的全部键（含 rerank_enabled/rerank_model）
    ② bool_keys 必须包含 rerank_enabled（否则字符串 "false" 会被当成真值）
    ③ SETTINGS_SCHEMA 的键集合与 DEFAULT_SETTINGS 完全一致（双向无差集）
    ④ 前端设置段里不许再有硬编码设置项

只读源码做静态校验，不启服务。
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "scripts" / "kb_lib" / "config.py"
ADMIN = ROOT / "scripts" / "kb_lib" / "routes" / "routes_admin.py"
APPJS = ROOT / "scripts" / "web" / "app.js"

# schema 里的控件类型白名单：前端 _setField / _setDetail 只认这几种。
VALID_TYPES = {"text", "password", "number", "select", "checks", "textarea", "devices"}


def _config_src():
    return CONFIG.read_text(encoding="utf-8")


def _schema_seg():
    """截出 SETTINGS_SCHEMA 定义段（[ ... ] 之间）。"""
    src = _config_src()
    i = src.index("SETTINGS_SCHEMA = [")
    j = src.index("\n]\n", i)
    return src[i:j]


def _default_setting_keys():
    src = _config_src()
    i = src.index("DEFAULT_SETTINGS = {")
    j = src.index("\n}\n", i)
    return re.findall(r'^\s{4}"([a-z_]+)":', src[i:j], re.M)


def _schema_keys():
    """schema 里的全部 k，含本地伪键 `_devices`（它不对应后端配置项）。"""
    return re.findall(r'"k": "([a-z0-9_]+)"', _schema_seg())


def _whitelist_keys():
    src = ADMIN.read_text(encoding="utf-8")
    i = src.index("for k in [")
    j = src.index("]", i)
    return re.findall(r'"([a-z_]+)"', src[i:j])


def test_后端默认设置项数不低于基线():
    """项数不写死具体数字 —— 每加一个设置项都要改测试，是测试在管实现，不是反过来。"""
    keys = _default_setting_keys()
    assert len(keys) >= 22, f"默认设置项数比基线少，实际 {len(keys)}：{keys}"
    assert "other_server_prefixes" in keys, "其他服务器前缀设置没进默认值"
    assert "rerank_enabled" in keys and "rerank_model" in keys


def test_写入白名单放行全部默认键():
    missing = [k for k in _default_setting_keys() if k not in _whitelist_keys()]
    assert not missing, f"这些键不在写入白名单里，提交会被静默丢弃：{missing}"


def test_布尔键包含rerank_enabled():
    src = ADMIN.read_text(encoding="utf-8")
    m = re.search(r"bool_keys = \{([^}]*)\}", src)
    assert m, "找不到 bool_keys 定义"
    names = re.findall(r'"([a-z_]+)"', m.group(1))
    assert "rerank_enabled" in names, f"bool_keys 缺 rerank_enabled，实际：{names}"
    assert "qq_push_enabled" in names and "index_auto_rebuild" in names


def test_schema键集合与默认设置完全一致():
    be = set(_default_setting_keys())
    sk = _schema_keys()
    assert len(sk) == len(set(sk)), f"schema 里有重复键：{sk}"
    assert not (be - set(sk)), f"后端有、设置页 schema 没渲染：{sorted(be - set(sk))}"
    # 以 _ 开头的键是页面本地项（如 _devices 登录设备，数据来自 /auth/devices），
    # 不对应任何后端配置项，不参与一致性比对。
    local_only = {k for k in sk if k.startswith("_")}
    assert not (set(sk) - be - local_only), f"schema 有、后端没定义：{sorted(set(sk) - be - local_only)}"


def test_schema每项字段合法():
    """每项必须有 k/l/t，控件类型在白名单内，select/checks 必须带 opts。"""
    sys.path.insert(0, str(ROOT / "scripts"))
    import kb_lib.config as kbconfig

    seen = set()
    for g in kbconfig.SETTINGS_SCHEMA:
        assert g.get("group"), f"分组缺 group：{g}"
        items = g.get("items")
        assert isinstance(items, list) and items, f"分组 {g.get('group')} 没有 items"
        for it in items:
            for f in ("k", "l", "t"):
                assert it.get(f), f"设置项缺字段 {f}：{it}"
            assert it["t"] in VALID_TYPES, f"未知控件类型 {it['t']}（{it['k']}）"
            if it["t"] in ("select", "checks"):
                assert it.get("opts"), f"{it['k']} 是 {it['t']} 但没有 opts"
            assert it["k"] not in seen, f"设置键重复出现：{it['k']}"
            seen.add(it["k"])


def test_schema分组为三个卡片():
    assert re.findall(r'"group": "([^"]+)"', _schema_seg()) == ["大模型", "检索", "系统"]


def test_schema标签全中文人话且无emoji():
    """标签去 emoji / 去英文缩写（第 1 项第 5 条，随 schema 一起搬到后端）。

    英文缩写断言只扫 `"l"` 标签本体，不扫 hint 里的示例 URL。
    """
    seg = _schema_seg()
    for emo in ("🤖", "🧬", "⏱", "🔍", "📋", "🎨", "⏳"):
        assert emo not in seg, f"标签区仍残留 emoji {emo}"
    labels = re.findall(r'"l": "([^"]+)"', seg)
    assert len(labels) >= 22, f"扫到的标签比基线少，实际 {len(labels)}：{labels}"
    for bad in ("QQ", "OpenAI", "BM25", "Webhook", "Secret", "Payload", "Token", "API"):
        hit = [x for x in labels if bad in x]
        assert not hit, f"标签里仍有英文 {bad}：{hit}"
    for old in ("LLM API地址", "Embedding API", "Hybrid", "Token 有效期"):
        assert old not in seg, f"标签区仍残留英文缩写标签：{old}"


def test_密钥占位符不会被原样提交():
    """GET 回显的密钥是 `<REDACTED>` 占位符。

    如果 sv() 把它当真实值提交，后端会把这个字符串写进设置，
    等于一次「保存任意一项」就把所有密钥清成占位符。必须在收集阶段跳过。

    真正的行为验证在 tests/js/settings_save.test.cjs（真跑 sv() 看发出去的 body）。
    这里只做源码级双保险，保证跳过逻辑没被删掉。
    """
    src = APPJS.read_text(encoding="utf-8")
    i = src.index("function sv()")
    j = src.index("function lsk()", i)
    seg = src[i:j]
    assert "<REDACTED>" in seg, "sv() 没有跳过 <REDACTED> 占位符，保存会把真实密钥覆盖掉"


def test_设置页渲染链路完整():
    """三张分组卡 + 账号区必须真渲染，且一级页真的走了 schema 加载。"""
    src = APPJS.read_text(encoding="utf-8")
    i = src.index("function _setList")
    j = src.index("function _setHome", i)
    lst = src[i:j]
    assert "set-card" in lst, "_setList() 没有输出 .set-card 卡片容器"
    assert "账号" in lst, "设计稿要求的退出登录区（账号卡）没有渲染"
    assert "_SET_GROUPS.forEach" in lst, "_setList() 没有遍历 _SET_GROUPS，分组定义等于摆设"

    i = src.index("function _setHome")
    j = src.index("function _setDetail", i)
    home = src[i:j]
    assert "_setList()" in home, "_setHome() 没有调用 _setList()，一级列表等于没渲染"
    assert "loadSettingsSchema" in home, \
        "_setHome() 没有先确保 schema 已加载，一级页会渲染成空列表"

    # ls() 必须把数据真正交给渲染（一级页走 _setHome，二级页走 _setDetail）
    i = src.index("function ls()")
    j = src.index("function sv()", i)
    seg = src[i:j]
    assert "_SET_CACHE" in seg, "ls() 没有缓存后端返回值，二级页拿不到数据"
    assert "_setHome()" in seg and "_setDetail()" in seg, "ls() 没有真正调用渲染函数"


def test_前端不再硬编码设置项():
    """反向守卫：设置项定义只许留在后端 schema。

    留着旧静态块的话，加一个设置项照样要改前端，schema 驱动等于白做。
    """
    src = APPJS.read_text(encoding="utf-8")
    i = src.index("var _SET_GROUPS")
    j = src.index("function _setField", i)
    seg = src[i:j]
    assert "var _SET_GROUPS = [];" in seg, "前端仍在本地硬编码 _SET_GROUPS"
    assert "{k:'" not in seg, "前端设置段里仍有硬编码设置项定义"
    assert "/api/settings/schema" in src, "前端没有去拉 /api/settings/schema"


def test_输入框仍用s_前缀id以便保存逻辑复用():
    src = APPJS.read_text(encoding="utf-8")
    i = src.index("function _setField")
    j = src.index("function ls()", i)
    seg = src[i:j]
    assert "id=\"s_'+esc(f.k)+'\"" in seg, "_setField 必须继续输出 id=s_<键名>，否则 sv() 收不到值"
