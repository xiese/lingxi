#!/usr/bin/env python3
"""
批量上传本地笔记到远程知识库服务器
用法: python upload_to_kb.py [--base-url URL] [--token MASTER_TOKEN] [--notes-dir PATH]
"""

import os, sys, json, time, argparse
import urllib.request
import urllib.error
from pathlib import Path

DEFAULT_API = os.environ.get("KB_API", "http://127.0.0.1:8899")
DEFAULT_DIR = Path(__file__).resolve().parent.parent / "笔记"  # 真实笔记根目录（config.py NOTES_DIR）

class KBUploader:
    def __init__(self, base_url, token, notes_dir):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.notes_dir = notes_dir

    def api(self, endpoint, method="GET", data=None):
        url = f"{self.base_url}{endpoint}"
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        body = json.dumps(data, ensure_ascii=False).encode() if data else None
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            resp = urllib.request.urlopen(req, timeout=30)
            return json.loads(resp.read()), resp.status
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read())
            except:
                err = {"error": str(e)}
            return err, e.code

    def smart_upload(self, filepath):
        content = filepath.read_text(encoding="utf-8")
        title = filepath.stem
        for line in content.split("\n"):
            s = line.strip()
            if s.startswith("# ") and not s.startswith("# 🗂"):
                title = s[2:].strip()
                break
        result, code = self.api("/api/smart-upload", method="POST",
                                data={"title": title, "text": content})
        return result, code

    def run(self, dry_run=False):
        files = sorted(self.notes_dir.rglob("*.md"))
        print(f"[*] Found {len(files)} note files\n")

        if dry_run:
            for f in files:
                print(f"  - {f.relative_to(self.notes_dir)}")
            return

        # 验证连接
        r, c = self.api("/health")
        print(f"[>] {self.base_url} (v{r.get('version','?')})")
        print(f"[i] Indexed: {r.get('indexed',0)} notes\n")

        success = skipped = failed = 0
        total = len(files)
        for i, fp in enumerate(files, 1):
            rel = str(fp.relative_to(self.notes_dir))
            size = len(fp.read_text(encoding="utf-8"))
            sys.stdout.write(f"[{i}/{total}] {rel} ({size}B) ... ")
            sys.stdout.flush()
            try:
                result, code = self.smart_upload(fp)
                action = result.get("action", "?")
                if action in ("created", "updated"):
                    success += 1
                    print(f"OK ({action})")
                elif code == 401:
                    print("FAIL: bad token")
                    sys.exit(1)
                else:
                    print(f"WARN {code}: {result.get('error','?')}")
                    skipped += 1
            except Exception as e:
                print(f"FAIL: {e}")
                failed += 1
            time.sleep(0.3)

        print(f"\n{'='*50}")
        print(f"OK: {success}  |  SKIP: {skipped}  |  FAIL: {failed}")
        print(f"Total: {success}/{total}")
        print(f"View: {self.base_url}/web/")

def main():
    p = argparse.ArgumentParser(description="批量上传笔记到知识库")
    p.add_argument("--base-url", default=DEFAULT_API)
    p.add_argument("--token", help="Master Token")
    p.add_argument("--notes-dir", default=str(DEFAULT_DIR))
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    notes_dir = Path(args.notes_dir)
    if not notes_dir.exists():
        print(f"[ERROR] Dir not found: {notes_dir}"); sys.exit(1)

    token = args.token or os.environ.get("KB_MASTER_TOKEN")
    if not token and not args.dry_run:
        print("[!] Master Token required")
        print("    On server: cat $KB_ROOT/.api_tokens.json")
        print("    Use master.token field value\n")
        token = input("Token: ").strip()

    if not token and not args.dry_run:
        print("[ERROR] Token needed"); sys.exit(1)

    KBUploader(args.base_url, token, notes_dir).run(dry_run=args.dry_run)

if __name__ == "__main__":
    main()
