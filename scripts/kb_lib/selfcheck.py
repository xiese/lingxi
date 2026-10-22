#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""selfcheck.py — kb-server 启动自检：探上游 /v1/models，模型名不在就告警。

背景（T-04 / B1-b）：生产 `llm_model` 曾被写成上游不存在的名字
（`deepseek-v4.1-flash`），服务照常启动、`/health` 照常 200，只有真正发起
问答时才 404/503，问题被发现时已经过去很久。这里在启动阶段主动探一次上游
模型清单，名字不在就打印醒目告警。

行为约定（刻意保守）：
- **只告警，不阻止启动**。上游不可达是常态（网络/额度/网关未起），
  自检失败不能把服务拖死；不可达与「名字不存在」必须分开报。
- 只读配置，不写任何文件、不改配置、不重试写库。
- 超时 5 秒，启动路径上不允许长时间阻塞。
"""
from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

log = logging.getLogger("kb.selfcheck")

TIMEOUT_S = 5
TAG = "[启动自检]"


def _models_url(base: str) -> str:
    b = str(base or "").rstrip("/")
    if b.endswith("/v1"):
        return b + "/models"
    return b + "/v1/models"


def fetch_model_ids(base: str, key: str = "") -> tuple[list[str] | None, str]:
    """返回 (模型 id 列表, 失败原因)。成功时原因为空串。"""
    if not base:
        return None, "llm_base 为空"
    url = _models_url(base)
    req = urllib.request.Request(url)
    if key:
        req.add_header("Authorization", "Bearer " + key)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            payload = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return None, f"HTTP {e.code}"
    except Exception as e:  # 网络/超时/解析
        return None, repr(e)[:160]
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return None, "返回体没有 data 列表"
    ids = [str(m.get("id")) for m in data if isinstance(m, dict) and m.get("id")]
    return ids, ""


def check_llm_model(settings: dict | None = None) -> dict:
    """自检主入口。返回结构化结果，便于测试与日志核对。

    返回字段：
      ok          上游探通且模型名存在
      reachable   上游是否可达（False 时 status 为 "upstream_unreachable"）
      model       配置里的 llm_model
      status      ok / model_missing / upstream_unreachable / no_model_configured
      detail      人话原因
      models      上游返回的模型数（探通时）
    """
    if settings is None:
        from kb_lib.config import load_settings

        settings = load_settings()
    model = str(settings.get("llm_model") or "").strip()
    base = str(settings.get("llm_base") or "").strip()
    key = str(settings.get("llm_key") or "").strip()

    if not model:
        res = {
            "ok": False,
            "reachable": True,
            "model": "",
            "status": "no_model_configured",
            "detail": "没有配置 llm_model，问答功能不可用",
            "models": 0,
        }
        log.warning("%s %s", TAG, res["detail"])
        return res

    ids, err = fetch_model_ids(base, key)
    if ids is None:
        res = {
            "ok": False,
            "reachable": False,
            "model": model,
            "status": "upstream_unreachable",
            "detail": f"上游 {base or '(未配置)'} 探不通：{err}",
            "models": 0,
        }
        log.warning("%s %s（不阻止启动）", TAG, res["detail"])
        return res

    if model in ids:
        res = {
            "ok": True,
            "reachable": True,
            "model": model,
            "status": "ok",
            "detail": f"llm_model={model} 在上游模型清单里（共 {len(ids)} 个）",
            "models": len(ids),
        }
        log.info("%s %s", TAG, res["detail"])
        return res

    hint = ""
    near = [m for m in ids if model.replace(".", "") in m.replace(".", "")]
    if near:
        hint = "；相近的有：" + "、".join(near[:5])
    res = {
        "ok": False,
        "reachable": True,
        "model": model,
        "status": "model_missing",
        "detail": f"llm_model={model} 不在上游模型清单里（共 {len(ids)} 个）{hint}",
        "models": len(ids),
    }
    log.error("%s ⚠️ %s", TAG, res["detail"])
    print(f"{TAG} ⚠️ {res['detail']}", flush=True)
    return res


def run_on_startup() -> dict:
    """给 kb-server 启动处调用；任何异常都吞掉，绝不影响启动。"""
    try:
        return check_llm_model()
    except Exception as e:  # 自检自身出错也不能拖死服务
        log.warning("%s 自检自身异常，已忽略：%r", TAG, e)
        return {"ok": False, "reachable": False, "model": "", "status": "selfcheck_error",
                "detail": repr(e)[:160], "models": 0}
