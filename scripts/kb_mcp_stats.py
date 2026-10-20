#!/usr/bin/env python3
"""统计知识库 MCP 使用日志。"""
from __future__ import annotations
import argparse, json, os
from pathlib import Path
from datetime import datetime, timedelta
from collections import Counter
import logging

log = logging.getLogger("kb_mcp_stats")

_KB_ROOT = Path(os.environ.get("KB_ROOT") or Path(__file__).resolve().parent.parent)
DEFAULT_LOGS = [
    Path(os.environ.get("KB_MCP_USAGE_LOG", _KB_ROOT / ".kb_mcp_usage.jsonl")),
]

def parse_time(s: str):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace('Z','+00:00').replace('+08:00',''))
    except Exception:
        log.debug("fromisoformat 解析失败，尝试 strptime: %r", s, exc_info=True)
        try:
            return datetime.strptime(s[:19], '%Y-%m-%dT%H:%M:%S')
        except Exception:
            log.debug("时间字符串解析失败，该行不计入统计: %r", s, exc_info=True)
            return None

def iter_rows(paths):
    seen=set()
    for p in paths:
        p=Path(p)
        if not p.exists():
            continue
        for line in p.read_text(encoding='utf-8', errors='replace').splitlines():
            line=line.strip()
            if not line:
                continue
            try:
                row=json.loads(line)
            except Exception:
                log.debug("跳过无法解析的日志行: %.120r", line, exc_info=True)
                continue
            key=(row.get('time'), row.get('tool'), row.get('query'), row.get('path'), row.get('title'))
            if key in seen:
                continue
            seen.add(key)
            row['_log']=str(p)
            yield row

def build_report(hours=12, paths=DEFAULT_LOGS, silent=False):
    now=datetime.now()
    since=now-timedelta(hours=hours)
    rows=[]
    for r in iter_rows(paths):
        t=parse_time(str(r.get('time','')))
        if not t or t < since:
            continue
        r['_dt']=t
        rows.append(r)
    if not rows and silent:
        return '[SILENT]'
    ok=sum(1 for r in rows if r.get('ok') is True)
    fail=sum(1 for r in rows if r.get('ok') is False or r.get('error') or r.get('reason'))
    tool_counter=Counter(r.get('tool','unknown') for r in rows)
    client_counter=Counter(r.get('client','unknown') for r in rows)
    readonly_counter=Counter(str(r.get('readonly','?')) for r in rows)
    queries=Counter()
    writes=[]
    failures=[]
    for r in rows:
        q=r.get('query') or r.get('question')
        if q:
            queries[str(q)[:80]] += 1
        if r.get('tool') in ('kb_create','kb_create_project_log','kb_upload','kb_index_rebuild'):
            writes.append(r)
        if r.get('ok') is False or r.get('error') or r.get('reason'):
            failures.append(r)
    lines=[]
    lines.append(f'# 知识库 MCP 使用统计（近 {hours} 小时）')
    lines.append('')
    lines.append(f'- 时间范围：{since.strftime("%Y-%m-%d %H:%M")} ~ {now.strftime("%Y-%m-%d %H:%M")}')
    lines.append(f'- 总调用：{len(rows)}')
    lines.append(f'- 成功/失败：{ok} / {fail}')
    lines.append('- 日志来源：' + ', '.join(str(p) for p in paths if Path(p).exists()))
    lines.append('')
    lines.append('## 工具调用 Top')
    if tool_counter:
        for k,v in tool_counter.most_common(12):
            lines.append(f'- `{k}`：{v}')
    else:
        lines.append('- 无调用记录')
    lines.append('')
    lines.append('## 客户端 / 权限')
    for k,v in client_counter.most_common():
        lines.append(f'- client `{k}`：{v}')
    if readonly_counter:
        lines.append('- readonly 分布：' + ', '.join(f'{k}={v}' for k,v in readonly_counter.items()))
    lines.append('')
    lines.append('## 高频查询')
    if queries:
        for k,v in queries.most_common(10):
            lines.append(f'- {v}x：{k}')
    else:
        lines.append('- 无查询类调用')
    lines.append('')
    lines.append('## 写入/重建动作')
    if writes:
        for r in writes[-10:]:
            desc=r.get('title') or r.get('path') or r.get('file_path') or r.get('query') or ''
            lines.append(f'- {r.get("time","")[:19]} `{r.get("tool")}` {desc}')
    else:
        lines.append('- 本周期没有写入/上传/重建动作')
    lines.append('')
    lines.append('## 异常 / 拒绝')
    if failures:
        for r in failures[-10:]:
            lines.append(f'- {r.get("time","")[:19]} `{r.get("tool")}` reason={r.get("reason","")} error={r.get("error","")}')
    else:
        lines.append('- 未发现异常')
    lines.append('')
    lines.append('> 提示：如果连续多个周期只有搜索没有写入，说明 AI 可能仍在“只查不沉淀”，需要加强任务完成后的 kb_create_project_log 规则。')
    return '\n'.join(lines)

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--hours', type=int, default=12)
    ap.add_argument('--silent-empty', action='store_true')
    args=ap.parse_args()
    print(build_report(args.hours, DEFAULT_LOGS, args.silent_empty))

if __name__ == '__main__':
    main()
