"""读《规则基线》并给出**摘要与检索**（纯标准库，同进程直读文件）。

⚠️ **直读文件，不发起 HTTP**。
   `/api/rules` 是给**外部**调用者的端点；同一进程里"go 起 HTTP 读自己"
   是绕远（任务书原话："同进程内直接读文件更简单"）。

═══════════════════════════════════════════════════════════════════
三个接口（按"越来越具体"排）
═══════════════════════════════════════════════════════════════════

    rule_sections()       → 所有一级/二级标题 [{line, level, title, key}]
    rule_section(kw)      → 精确命中**某一节**的全文（如 '7.2' → 合同一节）
    digest(kw)            → 命中节的**摘要**（前若干行 + 节标题链）

`kw` 可以是：
    · 节号   '7.2' / '11.4' / '3.3.5' / '5'
    · 关键词 '合同' / 'AI' / '清仓'
节号命中**优先**于关键词（精确 > 模糊）。

⚠️ **不缓存到磁盘**；每次调用重新读文件 —— 基线随时可能在改，
   读旧缓存比读慢一点更糟（文件只有 ~1800 行，读一次是毫秒级）。
"""

from __future__ import annotations

import re
from pathlib import Path

# ══════════════════════════════════════════════════════════════════
# 基线文件的**唯一**权威位置（README §八 权威顺序 ②）
# ══════════════════════════════════════════════════════════════════

#: 规则基线的绝对路径。**唯一权威**，别处（`docs/99-历史版本/`、口头转述…）
#: 都不算数。改这里要过审查 —— 它是整个 AI 解释权的源。
RULES_PATH: Path = (
    Path(__file__).resolve().parent.parent.parent
    / "docs" / "01-规范" / "规则基线.md"
)

#: 一节"摘要"最多带几行正文（不含标题）。**任务书要求"不许编数字"**：
#: 摘要里出现的任何数字都是**从基线原文截取**的，不是 AI 生成的。
_DIGEST_BODY_LINES = 12


class RuleNotFoundError(KeyError):
    """按节号 / 关键词找不到对应小节（**明说拿不到**，不猜）。"""


# ══════════════════════════════════════════════════════════════════
# 内部：把 markdown 劈成"节"
# ══════════════════════════════════════════════════════════════════

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
#: 节号（如 `7.2` / `11.4`）在标题开头可能出现的形状
_SEC_NUM = re.compile(r"^(\d+(?:\.\d+)*)\s*[.、]?\s+")


def _read_lines() -> list[str]:
    if not RULES_PATH.is_file():
        raise FileNotFoundError(f"规则基线不在：{RULES_PATH}")
    return RULES_PATH.read_text(encoding="utf-8").splitlines()


def _is_fence(line: str) -> bool:
    s = line.lstrip()
    return s.startswith("```") or s.startswith("~~~")


def _heading_events(lines: list[str]) -> list[tuple[int, int, str]]:
    """返回 `[(行号, 级别, 标题原文), ...]`，**跳过代码块里的 #**。"""
    out: list[tuple[int, int, str]] = []
    in_fence = False
    fence_mark = ""
    for i, ln in enumerate(lines):
        if _is_fence(ln):
            s = ln.lstrip()
            mark = "```" if s.startswith("```") else "~~~"
            if not in_fence:
                in_fence = True
                fence_mark = mark
            elif mark == fence_mark:
                in_fence = False
            continue
        if in_fence:
            continue
        m = _HEADING.match(ln)
        if m:
            out.append((i, len(m.group(1)), m.group(2).strip()))
    return out


def _sec_key(title: str) -> str | None:
    """从标题提取节号（如 `### 7.2 合同` → `'7.2'`）。取不到返回 None。"""
    m = _SEC_NUM.match(title)
    return m.group(1) if m else None


# ══════════════════════════════════════════════════════════════════
# 接口一：rule_sections —— 目录
# ══════════════════════════════════════════════════════════════════

def rule_sections() -> list[dict]:
    """全部标题目录：`[{line, level, title, key}, ...]`（按文件顺序）。

    `key` 是节号（取不到为 None）。给"基线都讲了什么"一个总览，
    AI 汇报"我解释的是哪一节"时用。
    """
    lines = _read_lines()
    return [
        {"line": ln, "level": level, "title": title, "key": _sec_key(title)}
        for ln, level, title in _heading_events(lines)
    ]


# ══════════════════════════════════════════════════════════════════
# 接口二：rule_section —— 某一节的全文
# ══════════════════════════════════════════════════════════════════

def rule_section(keyword: str) -> dict:
    """取命中节的**全文**。

    返回 `{key, title, line, end_line, level, text}`：
      · `text` 是从该节标题到**下一同级或更高级标题**之前的全部原文；
      · 找不到 → 抛 `RuleNotFoundError`（**明说，不猜**）。

    ⚠️ 节号命中优先：`rule_section('7.2')` 取的是 `### 7.2 合同` 那一节，
       即使别处也出现"合同"两个字。
    """
    lines = _read_lines()
    heads = _heading_events(lines)

    # ── ① 节号精确命中 ─────────────────────────────────────────
    for idx, (ln, level, title) in enumerate(heads):
        if _sec_key(title) == keyword:
            end = heads[idx + 1][0] if idx + 1 < len(heads) else len(lines)
            return {
                "key": keyword, "title": title, "line": ln,
                "end_line": end, "level": level,
                "text": "\n".join(lines[ln:end]),
            }

    # ── ② 关键词模糊命中（第一个匹配节）────────────────────────
    for idx, (ln, level, title) in enumerate(heads):
        if keyword in title:
            end = heads[idx + 1][0] if idx + 1 < len(heads) else len(lines)
            return {
                "key": _sec_key(title), "title": title, "line": ln,
                "end_line": end, "level": level,
                "text": "\n".join(lines[ln:end]),
            }

    raise RuleNotFoundError(
        f"基线里找不到节 {keyword!r}（按节号与关键词都没命中）。"
        f"用 rule_sections() 看都有哪几节。"
    )


# ══════════════════════════════════════════════════════════════════
# 接口三：digest —— 某一节的摘要
# ══════════════════════════════════════════════════════════════════

def digest(keyword: str) -> dict:
    """命中节的**摘要**：标题 + 前若干行正文。

    返回 `{key, title, line, level, summary, truncated,
           full_line_count}`。
      · `summary` 是**原文截取**（不是 AI 改写），最多 `_DIGEST_BODY_LINES` 行；
      · 全文更长时 `truncated=True` —— AI 要说"我只看了开头"，
        不许装作看完了整节。
    """
    sec = rule_section(keyword)
    body = sec["text"].splitlines()
    head = body[: _DIGEST_BODY_LINES + 1]          # 标题 + 前 N 行
    truncated = len(body) > len(head)
    return {
        "key": sec["key"],
        "title": sec["title"],
        "line": sec["line"],
        "level": sec["level"],
        "summary": "\n".join(head),
        "truncated": truncated,
        "full_line_count": len(body),
    }
