#!/usr/bin/env python3
"""存量明文令牌 → 哈希键 一次性迁移（C2 / 台账 #23，设计 §2.3 做法 A）。

背景：C2 把 `store["tokens"]` 的键从「明文令牌」换成
`sha256(pepper + 明文)` 的 64 位十六进制。旧代码只认明文键，所以文件一旦
换键，**必须先停服务、再跑本脚本、最后起新代码**（停机 10~20 秒）。
客户端侧零影响：明文令牌本身没变，新代码算哈希后照样命中。

用法：
    python3 scripts/kb_migrate_token_hash.py --kb-root /srv/knowledge-base
    python3 scripts/kb_migrate_token_hash.py --kb-root ... --dry-run

前置闸（任一不满足即退出码 2，不动文件）：
    1. <kb-root>/.kb-env 内容为 prod   → 拒绝
    2. pepper 取不到（环境变量与 .kb_pepper 都没有且不允许生成）→ 拒绝
    3. 令牌文件不存在                   → 退出码 0（无事可做）

幂等：已经是 64 位十六进制的键原样跳过，重跑结果一致。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from kb_lib import store as S  # noqa: E402

HEX = set("0123456789abcdef")


def _is_hash_key(k) -> bool:
    return isinstance(k, str) and len(k) == 64 and set(k) <= HEX


def _read_env(root: Path) -> str:
    f = root / ".kb-env"
    try:
        return f.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def migrate(kb_root, *, apply=False, allow_generate_pepper=True):
    """迁移一个库根目录下的令牌文件。返回结果字典（不打印）。

    返回：{"status": "noop"|"ok"|"dry-run", "migrated": n, "skipped": m,
           "backup": str|None, "file": str}
    前置闸不满足时抛 RuntimeError（CLI 转成退出码 2）。
    """
    root = Path(kb_root).resolve()
    env = _read_env(root)
    if env == "prod":
        raise RuntimeError(f"拒绝在 prod 库上迁移: {root}")

    tf = root / ".api_tokens.json"
    if not tf.exists():
        return {"status": "noop", "migrated": 0, "skipped": 0, "backup": None,
                "file": str(tf)}

    pepper = os.environ.get("KB_TOKEN_PEPPER", "").strip()
    if not pepper:
        pf = root / S.PEPPER_FILE_NAME
        if pf.exists():
            pepper = pf.read_text(encoding="utf-8").strip()
        elif allow_generate_pepper:
            pepper = S.get_pepper(root)
        if not pepper:
            raise RuntimeError("取不到 pepper（KB_TOKEN_PEPPER 与 .kb_pepper 均为空）")

    data = json.loads(tf.read_text(encoding="utf-8"))
    tokens = data.get("tokens") or {}
    todo = [k for k in tokens if not _is_hash_key(k)]
    if not todo:
        return {"status": "noop", "migrated": 0, "skipped": len(tokens),
                "backup": None, "file": str(tf)}

    if not apply:
        return {"status": "dry-run", "migrated": len(todo),
                "skipped": len(tokens) - len(todo), "backup": None,
                "file": str(tf)}

    # 备份：迁移前原样留一份（0600），出事直接换回来。
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = tf.with_name(tf.name + f".bak-{stamp}")
    shutil.copy2(str(tf), str(backup))
    os.chmod(str(backup), 0o600)

    new_tokens = {}
    for k, info in tokens.items():
        info = dict(info or {})
        if _is_hash_key(k):
            new_tokens[k] = info
            continue
        h = S.token_hash(k, root)
        info.setdefault("id", "tk_" + os.urandom(5).hex())
        info["prefix"] = h[:12]
        new_tokens[h] = info

    data["tokens"] = new_tokens
    data.setdefault("pending_revoke", {})
    meta = data.get("meta") or {}
    meta["schema"] = 2
    meta["pepper_id"] = hashlib.sha256(pepper.encode("utf-8")).hexdigest()[:8]
    meta["migrated_at"] = datetime.now().isoformat()
    data["meta"] = meta

    fd, tmp = tempfile.mkstemp(dir=str(root), prefix=".api_tokens_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, str(tf))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    return {"status": "ok", "migrated": len(todo),
            "skipped": len(tokens) - len(todo), "backup": str(backup),
            "file": str(tf)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="存量明文令牌 → 哈希键 迁移")
    ap.add_argument("--kb-root", default=os.environ.get("KB_ROOT", ""),
                    help="知识库根目录（必填或读 KB_ROOT）")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写盘")
    args = ap.parse_args(argv)

    if not args.kb_root:
        print("缺少 --kb-root（或设置 KB_ROOT）", file=sys.stderr)
        return 2
    try:
        res = migrate(args.kb_root, apply=not args.dry_run)
    except RuntimeError as e:
        print(f"拒绝迁移: {e}", file=sys.stderr)
        return 2

    if res["status"] == "noop":
        print(f"无事可做：{res['file']} 不存在或已全部是哈希键")
        return 0
    if res["status"] == "dry-run":
        print(f"[dry-run] 待迁移 {res['migrated']} 条，跳过 {res['skipped']} 条")
        return 0
    print(f"迁移 {res['migrated']} 条，跳过 {res['skipped']} 条")
    print(f"备份：{res['backup']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
