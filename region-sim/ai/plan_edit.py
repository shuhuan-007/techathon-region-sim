"""草稿编辑 —— 把模型给的**轻量编辑**合并到玩家当前草稿上（纯函数）。

依据 `docs/03-架构/AI交互与建设撤销.md` §三（用户反馈第 3 条：
「建设计划无法取消，需要让 AI 助手取消」）。

═══════════════════════════════════════════════════════════════════
为什么要有这一层（而不是让模型重填整份 Plan）
═══════════════════════════════════════════════════════════════════

玩家说「取消 0,0 那条」时，模型**不需要**、也**不应该**把整份计划重写一遍：

    · 重写 = 模型会顺手改动玩家手填的其它安排（用户明确说「自己填不要
      改动玩家已经手动填的，至少要先问」）；
    · 只删一条却丢了对开工率/口粮的微调 = 事实上的覆盖。

所以模型只回**编辑指令**，由本模块确定性地合并到 `current_plan`：

    · `remove_builds: ["0,0"]`         → 从 builds 里删这些 plot_id
    · `builds: [...]`                  → 追加/替换（同 plot_id 以新为准）
    · `plot_plans: {...}`              → **只更新列出的地块**，未列出的保持原值
    · `purchases` / `food`             → 整体替换这两块
    · `why`                            → 一句话说明（只进摘要，不进规则）

⚠️ **纯函数、只读**：不 import `server` / `storage`，不写磁盘、不改传入的
   `current_plan`（内部先 `copy.deepcopy`）。`compile_draft` 拿到合并结果后
   仍走「规整 → validate → preview → 缩量」，本模块**不判定合法性**。
"""

from __future__ import annotations

import copy
from typing import Any, Optional

#: 一份 Plan 里**算动作**的键（`player_id` / `contracts` 不算"编辑动作"）。
#: `contracts` 只在判断"草稿是否为空"时算内容（单人局它总是空）。
PLAN_CONTENT_KEYS: tuple[str, ...] = ("builds", "plot_plans", "purchases", "food")

#: 判断"草稿空不空"时要看的全部键。
_EMPTINESS_KEYS: tuple[str, ...] = PLAN_CONTENT_KEYS + ("contracts",)

#: 一份草稿里允许出现的"动作"键（plan_action_of 判 edited 用）。
_ACTION_KEYS: tuple[str, ...] = ("builds", "plot_plans", "purchases", "food")


# ══════════════════════════════════════════════════════════════════
# 小工具：形状容错（模型与前端给的东西都不一定规整）
# ══════════════════════════════════════════════════════════════════


def _nonempty(value: Any) -> bool:
    """这个字段"有没有内容"（空 dict / 空 list / 空白串都算没有）。"""
    if isinstance(value, dict):
        return bool(value)
    if isinstance(value, (list, tuple, set)):
        return bool(value)
    if isinstance(value, str):
        return bool(value.strip())
    return False


def _build_plot_id(item: Any) -> Optional[str]:
    """从一条 build（或一个裸 plot_id 字符串）里取 `plot_id`；取不到返回 None。"""
    if isinstance(item, str):
        pid = item.strip()
        return pid or None
    if isinstance(item, dict):
        pid = item.get("plot_id", item.get("id", item.get("plot")))
        if isinstance(pid, str) and pid.strip():
            return pid.strip()
    return None


def _iter_builds(raw: Any) -> list[Any]:
    """`builds` 允许 list，也容忍以 plot_id 为键的 dict（与编译器同口径）。

    ⚠️ 只做**形状归一**，不校验合法性 —— 那仍是 `compile_draft` 的活。
    """
    if isinstance(raw, list):
        return list(raw)
    if isinstance(raw, dict):
        out: list[Any] = []
        for pid, item in raw.items():
            if isinstance(item, dict):
                item = dict(item)
                item.setdefault("plot_id", pid)
                out.append(item)
            elif isinstance(item, str):
                out.append({"plot_id": pid})
        return out
    return []


def _remove_ids(raw: Any) -> list[str]:
    """`remove_builds` 归一成 plot_id 列表。

    容忍：`["0,0"]` / `[{"plot_id": "0,0"}]` / `"0,0"` / `{"0,0": true}`。
    去重、保序；认不出的项丢掉（**不猜**）。
    """
    if isinstance(raw, str):
        raw = [raw]
    elif isinstance(raw, dict):
        raw = list(raw.keys())
    if not isinstance(raw, (list, tuple, set)):
        return []
    out: list[str] = []
    for item in raw:
        pid = _build_plot_id(item)
        if pid and pid not in out:
            out.append(pid)
    return out


# ══════════════════════════════════════════════════════════════════
# 公开：草稿是否为空
# ══════════════════════════════════════════════════════════════════


def plan_has_content(plan: Any) -> bool:
    """这份草稿里有没有**任何安排**（空表单 / 只有 player_id → False）。

    这是「编辑模式 vs 旧行为（整份起草）」的分叉点：
    `current_plan` 空 → 走旧路径（模型给什么就是什么，默认全开工/补口粮）；
    非空 → 走编辑（以它为 base，**不默认全开工/不补口粮**）。
    """
    if not isinstance(plan, dict) or not plan:
        return False
    return any(_nonempty(plan.get(key)) for key in _EMPTINESS_KEYS)


# ══════════════════════════════════════════════════════════════════
# 公开：合并编辑
# ══════════════════════════════════════════════════════════════════


def merge_edits(raw: dict, current_plan: Optional[dict] = None) -> dict:
    """把模型回的编辑指令合并到 `current_plan` 上，返回**新的**完整 Plan 形状 dict。

    规则（`AI交互与建设撤销.md` §三，逐条对齐）：

      · `current_plan` 为空 → **原样返回 `raw`**（旧行为：整份起草）；
      · 以 `current_plan` 的**深拷贝**为 base（绝不改调用方的对象）；
      · `remove_builds`（list[str]）→ 从 base.builds 删这些 plot_id；
      · `builds` → 追加/替换到 base.builds（同 plot_id 以新为准、位置不动）；
      · `plot_plans` → **只更新列出的地块**，未列出的保持 base 原值
        （编辑模式不替玩家改没提到的安排）；
      · `purchases` / `food` → 出现就整体替换（空 dict 也算"明确清空"）；
      · 没出现的字段 → 保持 base 原值。

    ⚠️ 返回里**不带** `why` / `remove_builds`：那是聊天用的指令，不是 Plan 字段。
    """
    if not isinstance(raw, dict):
        return {}

    # ── 旧行为：空草稿 → 模型给什么就是什么 ─────────────────────
    if not plan_has_content(current_plan):
        return raw

    base = copy.deepcopy(current_plan)  # type: ignore[arg-type]
    assert isinstance(base, dict)

    # 形状先归一到"编译器也认"的样子
    base["builds"] = _iter_builds(base.get("builds"))
    for key in ("plot_plans", "purchases", "food"):
        if not isinstance(base.get(key), dict):
            base[key] = {}

    # ── ① 撤销：从 builds 里删掉指定 plot_id ────────────────────
    removed = _remove_ids(raw.get("remove_builds"))
    if removed:
        base["builds"] = [
            b for b in base["builds"] if _build_plot_id(b) not in removed
        ]

    # ── ② 追加/替换：同 plot_id 以新为准，位置保持原样 ──────────
    for item in _iter_builds(raw.get("builds")):
        pid = _build_plot_id(item)
        replaced = False
        if pid is not None:
            for idx, old in enumerate(base["builds"]):
                if _build_plot_id(old) == pid:
                    base["builds"][idx] = item
                    replaced = True
                    break
        if not replaced:
            base["builds"].append(item)

    # ── ③ 开工安排：只更新列出的地块，未列出的保留 ──────────────
    raw_pp = raw.get("plot_plans")
    if isinstance(raw_pp, dict):
        for pid, item in raw_pp.items():
            base["plot_plans"][pid] = copy.deepcopy(item)
    elif isinstance(raw_pp, list):
        for item in raw_pp:
            pid = _build_plot_id(item)
            if pid:
                base["plot_plans"][pid] = copy.deepcopy(item)

    # ── ④ 采购 / 口粮：整体替换（出现即替换，空 dict 也认）───────
    for key in ("purchases", "food"):
        if key in raw and isinstance(raw.get(key), dict):
            base[key] = copy.deepcopy(raw[key])

    return base


# ══════════════════════════════════════════════════════════════════
# 公开：聊天摘要用的两个小判定
# ══════════════════════════════════════════════════════════════════


def extract_why(raw: Any) -> str:
    """取模型写的 `why` / `rationale`（"做了什么 + 为什么"）；没有返回 `""`。

    ⚠️ 只用于**聊天摘要**，不参与任何规则判断（§三末条）。
    """
    if not isinstance(raw, dict):
        return ""
    for key in ("why", "rationale"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def plan_action_of(raw: Any, current_plan: Optional[dict] = None) -> str:
    """这次 AI 对草稿做了什么：`removed` / `edited` / `created` / `none`。

    判定顺序（就这一条链，不猜）：

      ① `current_plan` 非空 + `remove_builds` 命中了其中一条 → `"removed"`；
      ② `current_plan` 非空 + raw 里有 builds/plot_plans/purchases/food → `"edited"`；
      ③ `current_plan` 为空 + raw 有动作（含 remove_builds）→ `"created"`；
      ④ 其余 → `"none"`。

    ⚠️ `remove_builds` 给了但**一条都没命中**、又没有别的动作 → `"none"`
       （不能把"什么都没删"报成"撤销了"）。
    """
    if not isinstance(raw, dict):
        return "none"

    has_full_edit = any(_nonempty(raw.get(key)) for key in _ACTION_KEYS)
    removed = _remove_ids(raw.get("remove_builds"))

    if plan_has_content(current_plan):
        current_ids = {
            pid for pid in (_build_plot_id(b) for b in _iter_builds(
                (current_plan or {}).get("builds")
            ))
            if pid
        }
        if any(pid in current_ids for pid in removed):
            return "removed"
        return "edited" if has_full_edit else "none"

    return "created" if (has_full_edit or removed) else "none"


__all__ = [
    "PLAN_CONTENT_KEYS",
    "plan_has_content",
    "merge_edits",
    "extract_why",
    "plan_action_of",
]
