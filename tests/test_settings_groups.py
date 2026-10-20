"""设置页分组重做的回归测试（2026-10-18 第 1 项）。

锁死三件事，防止「前端加出来了但后端静默丢弃」这类假绿：
    ① 后端写入白名单必须放行 DEFAULT_SETTINGS 里的全部键（含 rerank_enabled/rerank_model）
    ② bool_keys 必须包含 rerank_enabled（否则字符串 "false" 会被当成真值）
    ③ 前端 _SET_GROUPS 的键集合必须与 DEFAULT_SETTINGS 完全一致（双向无差集）

只读源码做静态校验，不启服务。
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "scripts" / "kb_lib" / "config.py"
ADMIN = ROOT / "scripts" / "kb_lib" / "routes" / "routes_admin.py"
APPJS = ROOT / "scripts" / "web" / "app.js"


def _default_setting_keys():
    src = CONFIG.read_text(encoding="utf-8")
    i = src.index("DEFAULT_SETTINGS")
    seg = src[i:i + 4000]
    return re.findall(r'^\s{4}"([a-z_]+)":', seg, re.M)


def _whitelist_keys():
    src = ADMIN.read_text(encoding="utf-8")
    i = src.index("for k in [")
    j = src.index("]", i)
    return re.findall(r'"([a-z_]+)"', src[i:j])


def _frontend_keys():
    src = APPJS.read_text(encoding="utf-8")
    i = src.index("var _SET_GROUPS")
    j = src.index("function _setField", i)
    return re.findall(r"\{k:'([a-z_]+)'", src[i:j])


def test_后端默认设置项数与前端一致():
    """项数不写死具体数字 —— 每加一个设置项都要改测试，是测试在管实现，不是反过来。

    真正要守的是「后端定义了、前端必须渲染」和「白名单必须放行」，
    那两条分别由 test_前端分组键与后端默认键完全一致 / test_写入白名单放行全部默认键 把关。
    """
    keys = _default_setting_keys()
    assert len(keys) >= 22, f"默认设置项数比基线少，实际 {len(keys)}：{keys}"
    assert "other_server_prefixes" in keys, "第 5 项的其他服务器前缀设置没进默认值"
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


def test_前端分组键与后端默认键完全一致():
    be = set(_default_setting_keys())
    fe = _frontend_keys()
    assert len(fe) == len(set(fe)), f"前端有重复键：{fe}"
    assert not (be - set(fe)), f"后端有、前端没渲染：{sorted(be - set(fe))}"
    # 以 _ 开头的键是前端本地项（如 _devices 登录设备，数据来自 /auth/devices），
    # 不对应任何后端配置项，不参与一致性比对。
    local_only = {k for k in fe if k.startswith("_")}
    assert not (set(fe) - be - local_only), f"前端有、后端没定义：{sorted(set(fe) - be - local_only)}"


def test_前端分组为三个卡片():
    src = APPJS.read_text(encoding="utf-8")
    i = src.index("var _SET_GROUPS")
    j = src.index("function _setField", i)
    seg = src[i:j]
    groups = re.findall(r"\{t:'([^']+)'", seg)
    assert groups == ["大模型", "检索", "系统"], f"分组应为三张卡，实际：{groups}"


def test_标签全部改中文人话且无emoji():
    """第 1 项第 5 条：标签去 emoji / 去英文缩写。

    扫描范围必须覆盖 `_SET_GROUPS`（所有标签都在这里）到 `ls()` 整段。
    上一版只扫 `_setField` 到 `ls()` 之间的通用渲染函数体——那段一个标签都没有，
    断言恒真，等于没测（glm 审查点出的假绿）。
    英文缩写断言只扫 `l:'…'` 标签本体，不扫 hint 里的示例 URL。
    """
    src = APPJS.read_text(encoding="utf-8")
    i = src.index("var _SET_GROUPS")
    j = src.index("function ls()", i)
    seg = src[i:j]
    for emo in ("🤖", "🧬", "⏱", "🔍", "📋", "🎨", "⏳"):
        assert emo not in seg, f"标签区仍残留 emoji {emo}"
    labels = re.findall(r"l:'([^']+)'", seg)
    assert len(labels) >= 22, f"扫到的标签比基线少，实际 {len(labels)}：{labels}"
    # 展示用标签里不许出现英文品牌/缩写（glm 与 DS 都点过 QQ/OpenAI 残留在文案里）
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


def test_分组卡片在ls里真实渲染():
    """三张分组卡 + 账号区必须真渲染出来，不是只定义在 _SET_GROUPS 里。

    2026-10-18 第 13 项改成「一级列表 / 二级页」后，卡片渲染从 ls() 移到了 _setHome()，
    断言跟着结构走；但要求「必须真渲染」这一点不能放松：
    ls() 必须真的把 _SET_GROUPS 交给渲染函数，渲染函数里必须有卡片、账号区。
    """
    src = APPJS.read_text(encoding="utf-8")
    # 一级页由 _setHome() 拼搜索框 + _setList() 拼列表组成；
    # 卡片和账号区在 _setList() 里（搜索时只换这一段，避免重建输入框）。
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

    # ls() 必须把数据真正交给渲染（一级页走 _setHome，二级页走 _setDetail）
    i = src.index("function ls()")
    j = src.index("function sv()", i)
    seg = src[i:j]
    assert "_SET_CACHE" in seg, "ls() 没有缓存后端返回值，二级页拿不到数据"
    assert "_setHome()" in seg and "_setDetail()" in seg, "ls() 没有真正调用渲染函数"


def test_输入框仍用s_前缀id以便保存逻辑复用():
    src = APPJS.read_text(encoding="utf-8")
    i = src.index("function _setField")
    j = src.index("function ls()", i)
    seg = src[i:j]
    assert "id=\"s_'+esc(f.k)+'\"" in seg, "_setField 必须继续输出 id=s_<键名>，否则 sv() 收不到值"
