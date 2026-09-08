# -*- coding: utf-8 -*-
"""一次性修复脚本：把 api/app.py 里被写坏的 ndjson 换行拼接改为 chr(10)。"""
from pathlib import Path

p = Path("src/yunpai_orchestrator/api/app.py")
lines = p.read_text(encoding="utf-8").split("\n")
fixed = 0
out = []
for i, ln in enumerate(lines):
    if 'return json.dumps({"type": event_type, **payload}, ensure_ascii=False) + "' in ln:
        out.append('            return json.dumps({"type": event_type, **payload}, ensure_ascii=False) + chr(10)')
        fixed += 1
        # 吞掉被断行的孤立引号行
        if i + 1 < len(lines) and lines[i + 1].strip() in ('"', "'", ""):
            continue
        continue
    out.append(ln)
assert fixed == 1, f"expect 1 fix point, got {fixed}"
p.write_text("\n".join(out), encoding="utf-8")
print("fixed; compile check:")
import py_compile
py_compile.compile(str(p), doraise=True)
print("OK")
