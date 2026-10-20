"""写入乐观锁：内容版本号（etag）。

设计（2026-10-17 批 3）
----------------------
- `etag(content)` = 内容 sha256 的**前 16 位十六进制**。
  用 sha256 而不是 md5：不需要抗碰撞强度上的额外成本，但同一份内容
  在任何机器、任何进程、任何时间都得到同一个值（不像内置 hash() 受
  PYTHONHASHSEED 影响），便于跨进程比对与排障。
- GET 单篇返回 etag；写入时客户端可带 `If-Match` 头或 body 里的
  `base_hash`。两者都给了就都校验（任一不匹配即 409）。
- **不带版本号的写入暂时兼容**：仍按旧行为覆盖，但记一条 warning，
  便于观察还有多少客户端没升级；等全量升级后再收紧成硬拦。
"""

from __future__ import annotations

import hashlib

__all__ = ["content_etag", "check_precondition"]

_ETAG_LEN = 16


def content_etag(content: str) -> str:
    """内容版本号：sha256 前 16 位十六进制。

    对「原文」计算（含 frontmatter），保证读写两侧参照同一份字节。
    """
    if content is None:
        content = ""
    if isinstance(content, str):
        data: bytes = content.encode("utf-8")
    elif isinstance(content, bytes):
        data = content
    else:
        data = str(content).encode("utf-8")
    return hashlib.sha256(data).hexdigest()[:_ETAG_LEN]


def _norm(v):
    """归一化客户端传来的版本号：允许 'W/"abc"' / '"abc"' / 'abc'。"""
    if v is None:
        return None
    s = str(v).strip()
    if s.startswith("W/"):
        s = s[2:].strip()
    s = s.strip('"').strip()
    return s or None


def check_precondition(current_content: str, if_match=None, base_hash=None):
    """校验写入前置条件。

    返回 (ok, expected, provided)：
      - ok=True  可以写入
      - ok=False 版本不匹配，调用方应返回 409
    没有任何版本号时 ok=True（兼容期，调用方自行记 warning）。
    """
    provided = _norm(if_match) or _norm(base_hash)
    if provided is None:
        return True, content_etag(current_content), None
    expected = content_etag(current_content)
    # 只认两种形态：
    #   1) 截断版 etag（16 位）与 expected 完全相同；
    #   2) 客户端回传完整 sha256（64 位），其前 16 位等于 expected。
    # 不用 `provided.startswith(expected)`：那会放行「expected + 任意后缀」，
    # 把前置条件弱化成前缀匹配（GLM 审查 2026-10-17 指出）。
    if provided == expected:
        return True, expected, provided
    if len(provided) == 64 and provided[: len(expected)] == expected:
        return True, expected, provided
    return False, expected, provided
