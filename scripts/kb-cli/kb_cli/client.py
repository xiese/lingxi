#!/usr/bin/env python3
"""
知识库 CLI 工具 (kb-cli) v2.0 — Python 版，无需编译
用法: python kb-cli.py <命令> [参数]
      或设置别名: alias kb-cli='python3 /path/to/kb-cli.py'
"""
import sys, os, json, argparse, urllib.request, urllib.parse, urllib.error, io
import logging

log = logging.getLogger("kb_cli.client")

# Windows 下强制 stdin/stdout/stderr 使用 UTF-8，避免 GBK 编码错误
CLIVERSION = "2.1.0"
DEFAULT_API = os.environ.get("KB_API", "http://127.0.0.1:8899")
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".kb-cli.json")

# ── 配置 ──

def load_config():
    cfg = {"api_url": "", "token": ""}
    # 1. 读取配置文件
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        # 配置文件不存在属首次运行的正常情况，记 debug 不刷 stderr
        log.debug("未读到 CLI 配置文件，使用默认配置: %s", CONFIG_PATH, exc_info=True)
    # 2. 环境变量覆盖
    if os.environ.get("KB_API"):
        cfg["api_url"] = os.environ["KB_API"]
    if os.environ.get("KB_TOKEN"):
        cfg["token"] = os.environ["KB_TOKEN"]
    # 3. 默认值
    if not cfg["api_url"]:
        cfg["api_url"] = DEFAULT_API
    return cfg

def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except Exception:
        log.warning("设置 CLI 配置文件权限 0600 失败: %s", CONFIG_PATH, exc_info=True)

# ── API 客户端 ──

class Client:
    def __init__(self, cfg):
        self.base_url = cfg["api_url"].rstrip("/")
        self.token = cfg.get("token", "")
        self.timeout = 60

    def _request(self, method, path, body=None):
        url = self.base_url + path
        data = None
        headers = {}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout)
            raw = resp.read()
            return raw, resp.status, None
        except urllib.error.HTTPError as e:
            raw = e.read()
            return raw, e.code, None
        except Exception as e:
            log.warning("HTTP 请求异常（连接失败/超时/非 HTTP 错误）: %s", url, exc_info=True)
            return None, 0, str(e)

    def get(self, path):
        return self._request("GET", path)

    def post(self, path, body=None):
        return self._request("POST", path, body)

    def post_multipart(self, path, fields=None, files=None):
        """multipart/form-data POST。
        fields: dict[str, str]
        files: list[(field_name, filename, content_bytes, content_type)]
        """
        import mimetypes
        import uuid

        boundary = "----KbCliBoundary" + uuid.uuid4().hex
        parts = []
        fields = fields or {}
        files = files or []

        for name, value in fields.items():
            if value is None or value == "":
                continue
            parts.append(
                (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                    f"{value}\r\n"
                ).encode("utf-8")
            )

        for field_name, filename, content, content_type in files:
            if not content_type:
                content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
            parts.append(
                (
                    f"--{boundary}\r\n"
                    f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
                    f"Content-Type: {content_type}\r\n\r\n"
                ).encode("utf-8")
            )
            parts.append(content)
            parts.append(b"\r\n")

        parts.append(f"--{boundary}--\r\n".encode("utf-8"))
        body = b"".join(parts)

        url = self.base_url + path
        headers = {
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            resp = urllib.request.urlopen(req, timeout=self.timeout)
            raw = resp.read()
            return raw, resp.status, None
        except urllib.error.HTTPError as e:
            raw = e.read()
            return raw, e.code, None
        except Exception as e:
            log.warning("HTTP 请求异常（连接失败/超时/非 HTTP 错误）: %s", url, exc_info=True)
            return None, 0, str(e)

    def delete(self, path):
        return self._request("DELETE", path)

def check_response(raw, code, allowed=(200,)):
    if code in allowed:
        return None
    try:
        err = json.loads(raw.decode("utf-8"))
        msg = err.get("error", "")
        if msg:
            return f"HTTP {code}: {msg}"
    except Exception:
        log.warning("错误响应体不是合法 JSON，按纯文本展示: HTTP %s", code, exc_info=True)
    text = raw.decode("utf-8", errors="replace").strip() if raw else ""
    return f"HTTP {code}: {text}" if text else f"HTTP {code}"

def parse_json(raw):
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        log.warning("响应不是合法 JSON，返回空对象", exc_info=True)
        return {}

def encode_note_path(path):
    """URL 编码笔记路径，保持 / 不变"""
    return urllib.parse.quote(path, safe="/")

# ── 工具函数 ──

def yn(b):
    return "✅ 是" if b else "❌ 否"

def truncate(s, n=100):
    s = s.replace("\n", " ")
    return s if len(s) <= n else s[:n] + "..."

def mask_token(token):
    if not token:
        return "❌ 未设置"
    if len(token) > 12:
        return token[:8] + "..." + token[-4:]
    return token

def die(msg, code=1):
    print(msg, file=sys.stderr)
    sys.exit(code)

# ── 命令处理 ──
