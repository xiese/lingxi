"""临时验证：把「无索引时返回未知」改回原来的 0/True，看测试是否报红。

用法：python3 /tmp/mut18bm25.py <rg 文件路径>
"""
import sys

PATH = sys.argv[1]
src = open(PATH, encoding="utf-8").read()

OLD = "        stale, cached = None, False\n"
NEW = "        stale, cached = 0, True\n"
if OLD not in src:
    sys.exit("锚点未找到，请先核对文件内容")
src = src.replace(OLD, NEW, 1)
open(PATH, "w", encoding="utf-8").write(src)
print("已改回 0/True")
