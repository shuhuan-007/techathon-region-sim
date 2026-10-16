"""语法自检 —— 保证每个 .py 文件都能被解析（跑得快，先于对账跑）。

跑法（在 region-sim/ 下）：

    python tools/check_syntax.py
"""

from __future__ import annotations

import ast
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    files = sorted(ROOT.rglob("*.py"))
    bad = 0
    for p in files:
        try:
            ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError as e:
            print(f"  SYNTAX ERROR  {p.relative_to(ROOT)}:{e.lineno}  {e.msg}")
            bad += 1
    print(f"{len(files)} 个 .py 文件，{bad} 个语法错误")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
