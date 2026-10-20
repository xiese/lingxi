#!/usr/bin/env python3
"""知识库结构卫生检查（只报告，绝不修改）。"""
import sys, argparse, pathlib, collections, json

ROOT = pathlib.Path(__file__).resolve().parent.parent
NOTES = ROOT / '笔记'
STANDARD = {'00-索引', '10-项目', '20-知识', '30-写作', '40-日记', '50-档案', '_模板', '_assets', '60-梦境', '70-经验'}

# 嵌套允许名单：这些名字出现在子目录里是正常的
#   _assets —— 项目自己的资源目录（如 10-项目/phone-pet/_assets）
# 另：50-档案/ 下整棵子树是历史归档镜像，里面出现 20-知识/ 等目录名属正常，
#     一律不报（否则每次归档都会产生噪音）。
NESTED_ALLOW = {'_assets'}


def nested_top_dirs():
    """嵌套目录名撞上顶层标准目录名 —— 2026-09-26 新增。

    背景：2026-09-19、2026-09-26 两次出现 20-知识/10-项目/ 这类错位
    （项目笔记被写进知识区的 10-项目 子目录）。旧版本只校验**顶层**目录，
    嵌套错位完全看不见，所以清完还会复发。
    """
    out = []
    for d in NOTES.rglob('*'):
        if not d.is_dir():
            continue
        try:
            rel = d.relative_to(NOTES)
        except ValueError:
            continue
        if len(rel.parts) < 2:
            continue
        if d.name not in STANDARD:
            continue
        if d.name in NESTED_ALLOW:
            continue
        if rel.parts[0] == '50-档案':
            continue
        out.append(str(rel))
    return sorted(out)


def scan():
    r = {}
    r['total_md'] = sum(1 for p in NOTES.rglob('*.md') if p.is_file())
    r['tmp_files'] = sorted(str(p.relative_to(ROOT)) for p in NOTES.rglob('*')
                            if p.is_file() and ('.bak' in p.name or p.name.endswith('~') or p.name.endswith('.tmp')))
    r['empty_dirs'] = sorted(str(p.relative_to(ROOT)) for p in NOTES.rglob('*')
                             if p.is_dir() and not any(p.iterdir())
                             and not (p.parent == NOTES and p.name in STANDARD))
    bl = collections.defaultdict(list)
    for d in [p for p in NOTES.rglob('*') if p.is_dir()]:
        bl[str(d.parent) + '/' + d.name.lower()].append(str(d.relative_to(ROOT)))
    r['case_dup_dirs'] = {k: v for k, v in bl.items() if len(v) > 1}
    r['nonstandard_top'] = sorted(d.name for d in NOTES.iterdir()
                                  if d.is_dir() and d.name not in STANDARD)
    r['nested_top_dir'] = nested_top_dirs()
    r['root_md'] = sorted(p.name for p in ROOT.glob('*.md'))
    bydir = collections.defaultdict(list)
    for p in NOTES.rglob('*.md'):
        if p.is_file(): bydir[str(p.parent)].append(p.name)
    r['same_dir_dup'] = {k: v for k, v in
                         ((k, sorted(n for n, c in collections.Counter(v).items() if c > 1))
                          for k, v in bydir.items()) if v}
    return r

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--quiet-ok', action='store_true')
    a = ap.parse_args()
    r = scan()
    cats = [('临时/备份文件', r['tmp_files']), ('空目录', r['empty_dirs']),
            ('大小写重复目录', list(r['case_dup_dirs'])), ('非标准顶层目录', r['nonstandard_top']),
            ('嵌套撞名目录', r['nested_top_dir']),
            ('仓库根散落 md', [x for x in r['root_md'] if x.upper() != 'README.MD']),
            ('同目录同名', list(r['same_dir_dup']))]
    bad = [(n, v) for n, v in cats if v]
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=1)); return 1 if bad else 0
    if not bad and a.quiet_ok:
        return 0
    print("知识库结构卫生检查  笔记总数", r['total_md'])
    if not bad:
        print("  干净 ✅")
        return 0
    for n, v in bad:
        print("  [问题] %s: %d" % (n, len(v)))
        for x in (v[:10] if isinstance(v, list) else list(v)[:10]):
            print("       ", x)
    return 1

if __name__ == '__main__':
    raise SystemExit(main())
