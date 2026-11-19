"""开发调试用：把 QQ 窗口的完整控件树写到文件（名字截断到 N 个字）。

用法：python -m tools.qq_uia_dump <输出文件> [名字最长字数]
"""
import sys
from pathlib import Path

from inquiry.qq import qq_windows, warm_up
from inquiry.runtime import require_d_path
import uiautomation as auto

out = require_d_path(Path(sys.argv[1]))
maxlen = int(sys.argv[2]) if len(sys.argv) > 2 else 20
lines = []
for w in qq_windows():
    warm_up(w)
    lines.append(f"===== {w.Name!r} {w.ClassName} {w.BoundingRectangle}")
    for ctrl, depth in auto.WalkControl(w, maxDepth=80):
        name = (ctrl.Name or "").replace("\n", "⏎")
        name = name[:maxlen] + ("…" if len(name) > maxlen else "")
        r = ctrl.BoundingRectangle
        lines.append(f"{depth:02d}{'  ' * depth}{ctrl.ControlTypeName} cls={ctrl.ClassName!r} name={name!r} aid={ctrl.AutomationId!r} rect=({r.left},{r.top},{r.right},{r.bottom})")
out.write_text("\n".join(lines), encoding="utf-8")
print(f"写入 {len(lines)} 行 -> {out}")
