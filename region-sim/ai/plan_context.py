"""AI 可行动作快照 —— 让模型看见「能往哪建、建得起什么、哪块能开工、要多少口粮」。

依据《AI 秘书 · 有效起草方案（2026-10-18）》§三.2：真实故障里，模型只看到
玩家**自己的**地块，于是把已有的 `4,5` 等写成 `builds`（校验报
`build_not_an_upgrade`）；也看不到建造成本 / 工程物资 / 人口口粮需求。

对外只有两个函数：

    describe_capabilities(state, player_id, cfg, *, max_empty=10) -> dict
        结构化快照。数字全部来自 engine / config，**不改任何状态**。

    format_capabilities(cap) -> str
        紧凑中文文本，块名固定（`[现金/预算]` `[空地]` `[可建]` `[可开工]`
        `[在建]` `[人口/口粮]`），供提示词直接拼接。

⚠️ 本模块不做的事（边界）：

    · 不扣钱、不建地块、不写存档 —— 纯只读，与 `ai/tools.py` 的只读工具同层；
    · 不替引擎判断「这条建造合不合法」（那是 `engine/validate.py` 的事）；
    · 不发明数字：材料价一律走 `state.world.last_price(g)`（**没有历史价时它
      自己退回参考价 P0**，见 `engine/models.py::WorldState.last_price`），
      人口 / 口粮一律走 `engine.population.compute_population()`。

⚠️ 已知对齐点（本快照如实转述，不在这一层修）：

    · 工业二级「研究线」在引擎里用 `product="研究"` 表示，但
      `validate.buildable_products()` 不列研究线 —— 本快照的 `products`
      按该函数**原样**给，不自己补一个「研究」（该快照只列新建一级工程，
      当前配置下新建工业只有 `G`）；
    · 地块的 `product` 原样给。资源 / 服务 / 研究地块通常为 `None`，
      文本里显示「—」。

═══════════════════════════════════════════════════════════════════
关于展示值（只影响 format_capabilities 的文本，不影响 dict）
═══════════════════════════════════════════════════════════════════

dict 里的数字一律是引擎原值（不四舍五入）。但浮点会带尾巴
（如 `0.4 × 1.2 = 0.48000000000000004`），直接塞给模型既难读又容易
被当成两个不同的数。所以**仅展示**用 `round(x, 3)` 抹掉浮点噪声，
并在文本里保留三位以内的小数；这不是改值，账以 dict / 引擎为准。
"""

from __future__ import annotations

from typing import Any

from engine import construction as C
from engine import population as P
from engine.loader import Config
from engine.models import GameState, PlotStatus, PlotType, parse_plot_id
from engine.validate import buildable_products


# ══════════════════════════════════════════════════════════════════
# 一、小工具：稳定排序 / 展示格式
# ══════════════════════════════════════════════════════════════════


def _sort_key(plot_id: str) -> tuple:
    """地块 ID 的排序键：**按行列顺序**（`0,2` 在 `0,10` 之前）。

    字符串排序在 10×10 地图上恰好也一致，但那是巧合；这里用
    `parse_plot_id` 显式按 (行, 列) 排，换地图尺寸也稳。
    坏 ID 不炸整份快照 —— 退到末尾，按字符串排。
    """
    try:
        row, col = parse_plot_id(plot_id)
    except (TypeError, ValueError):
        return (1, 0, 0, str(plot_id))
    return (0, row, col, "")


def _sorted_plot_ids(plot_ids) -> list[str]:
    """地块 ID 列表 → 按行列顺序排好的副本。"""
    return sorted(plot_ids, key=_sort_key)


def _display_num(value: Any) -> str:
    """**仅展示**用的数字格式：整数去掉 `.0`，小数最多三位（见模块顶注）。"""
    if isinstance(value, bool):
        return str(value)
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    if num.is_integer():
        return str(int(num))
    return f"{round(num, 3):g}"


def _material_text(materials: dict[str, float]) -> str:
    """工程物资 → `H-A×1` 这种短写法；没有物资写 `—`。"""
    if not materials:
        return "—"
    return ",".join(f"{goods}×{_display_num(qty)}" for goods, qty in sorted(materials.items()))


# ══════════════════════════════════════════════════════════════════
# 二、快照的四个子块（纯函数，只读 state / player / cfg）
# ══════════════════════════════════════════════════════════════════


def _plot_rows(player) -> list[dict]:
    """该玩家自己的地块，按 plot_id 行列顺序。

    `can_run` / `can_upgrade` 是**引擎口径**，不是"看着像"：

        can_run     = status == running
        can_upgrade = running 且 level < 3（在建 / 空地都不能升级指令）
        next_level  = 能升时 level + 1，否则 None
    """
    rows: list[dict] = []
    for plot_id in _sorted_plot_ids(player.plots):
        plot = player.plots[plot_id]
        running = plot.status is PlotStatus.RUNNING
        can_upgrade = running and plot.level < 3
        rows.append({
            "plot_id": plot.plot_id,
            "plot_type": plot.plot_type.value,
            "level": int(plot.level),
            "status": plot.status.value,
            "product": plot.product,
            "can_run": bool(running),
            "can_upgrade": bool(can_upgrade),
            "next_level": int(plot.level) + 1 if can_upgrade else None,
        })
    return rows


def _quota_table(player, cfg: Config) -> dict[str, dict]:
    """开局减免名额：`{类型: {used, total, remaining}}`。

    名额**按类型独立、跨季累计**（§5.3），所以 `used` 读存档里的
    `player.opening_quota_used`，`remaining` 直接复用
    `construction.opening_quota_remaining()`。

    ⚠️ 只列出 config 里配了 `opening.discount_quota` 的类型 —— 没配的类型
       就是"没有名额"，不凭空造一个 0/0 的条目。
    """
    table: dict[str, dict] = {}
    for name in sorted(cfg.opening_quota or {}):
        ptype = PlotType(name)
        table[name] = {
            "used": int((getattr(player, "opening_quota_used", None) or {}).get(name, 0)),
            # total 复用 `_quota_of()`（`opening_quota_remaining()` 内部用的是同一份），
            # 避免在这里再抄一遍 `max(0, int(...))` 造成两处口径漂移。
            "total": int(C._quota_of(cfg, ptype)),
            "remaining": int(C.opening_quota_remaining(player, cfg, ptype)),
        }
    return table


def _materials_value(state: GameState, materials: dict[str, float]) -> float:
    """工程物资按**上季价**折成现金（没有历史价 → `last_price` 自己退回 P0）。

    原样乘加，不取整、不替换商品；缺商品会让 `last_price` 报错（数据问题
    就该显形，不能悄悄按 0 元算）。
    """
    total = 0.0
    for goods, qty in materials.items():
        total += float(qty) * float(state.world.last_price(goods))
    return total


def _build_options(state: GameState, player, cfg: Config, quota: dict[str, dict]) -> list[dict]:
    """只列「新建」工程（`from_level == 0`），按 plot_type 排序。

    折后现金的判定与 `construction.is_opening_discount()` 同口径：

        · 只对**新建一级**（`w.is_new and w.to_level == 1`）；
        · 该类型名额还有剩（`remaining > 0`）；
        · 折扣率 < 1（没配折扣率 = 原价，不猜）。

    ⚠️ 同一季建第 4 块会恢复原价 —— 那是**按 plan.builds 顺序**逐条消耗名额
       算出来的；本快照只说"现在还剩几个"，不预演本季会建几块。
    """
    works = C.load_works(cfg)
    rate = C._discount_rate(cfg)
    rows: list[dict] = []

    for work in sorted(
        works.values(),
        key=lambda w: (w.plot_type.value, w.to_level, w.work_id),
    ):
        if not work.is_new:
            continue

        remaining = int(quota.get(work.plot_type.value, {}).get("remaining", 0))
        discounted = (
            work.is_new
            and work.to_level == 1
            and remaining > 0
            and rate < 1.0
        )
        cash_after = float(work.cash * rate) if discounted else float(work.cash)

        materials = {goods: float(qty) for goods, qty in work.materials.items()}
        materials_cost = _materials_value(state, materials)

        rows.append({
            "plot_type": work.plot_type.value,
            "label": work.label,
            "target_level": int(work.to_level),
            "cash": float(work.cash),
            "cash_after_discount": cash_after,
            "materials": materials,
            "materials_cost": materials_cost,
            "total_cost": cash_after + materials_cost,
            # 可选产出 / 路线：**原样**取自 validate.buildable_products()。
            # 资源 / 服务 / 研究没有多路线 → []（文本里显示 —）。
            "products": list(buildable_products(work.plot_type, work.to_level)),
            "quota_remaining": remaining,
        })
    return rows


def _population_rows(state: GameState, player_id: str, cfg: Config) -> tuple[dict, dict]:
    """人口五项 + 两档口粮需求（纯来自 `compute_population` 与 config 配比）。"""
    pop = P.compute_population(state, player_id, {}, cfg)
    population = {
        "labor_total": float(pop.labor_total),
        "labor_food": float(pop.labor_food),
        "population": float(pop.population),
        "baseline_food": float(pop.baseline_food),
        "good_food": float(pop.good_food),
    }
    food_need = {
        "baseline": {
            goods: float(ratio) * population["baseline_food"]
            for goods, ratio in sorted((cfg.food.baseline_quota or {}).items())
        },
        "good": {
            goods: float(ratio) * population["good_food"]
            for goods, ratio in sorted((cfg.food.good_quota or {}).items())
        },
    }
    return population, food_need


# ══════════════════════════════════════════════════════════════════
# 三、公开接口 1：结构化快照
# ══════════════════════════════════════════════════════════════════


def describe_capabilities(
    state: GameState,
    player_id: str,
    cfg: Config,
    *,
    max_empty: int = 10,
) -> dict:
    """把"这个玩家本季能做什么"压成一份 dict（**只读**，不改 state）。

    返回键（形状是主实现依赖的，别删键）：

        player_id / quarter / cash
        empty_total / empty_cells      —— 全图空地（任何人未占的格子）
        plots                          —— 自己的地块（含 can_run / can_upgrade）
        build_options                  —— 新建工程（含材料价与折后现金）
        quota                          —— 开局减免名额
        population / food_need         —— 岗位、人口、两档口粮需求

    `max_empty` 只影响 `empty_cells` 的截断；`empty_total` 永远是全量。
    """
    player = state.player(player_id)

    # ── 空地：全图格子 − 所有玩家已占的格子（不区分地块状态）──────────
    occupied = {
        plot_id
        for other in state.players.values()
        for plot_id in other.plots
    }
    empty_all = [
        plot_id
        for plot_id in _sorted_plot_ids(state.world.all_plot_ids())
        if plot_id not in occupied
    ]
    limit = max(0, int(max_empty))

    quota = _quota_table(player, cfg)
    population, food_need = _population_rows(state, player_id, cfg)

    return {
        "player_id": player_id,
        "quarter": int(state.world.quarter),
        "cash": float(player.cash),
        "empty_total": len(empty_all),
        "empty_cells": empty_all[:limit],
        "plots": _plot_rows(player),
        "build_options": _build_options(state, player, cfg, quota),
        "quota": quota,
        "population": population,
        "food_need": food_need,
    }


# ══════════════════════════════════════════════════════════════════
# 四、公开接口 2：紧凑中文文本（提示词直接拼）
# ══════════════════════════════════════════════════════════════════
#
# ⚠️ 块名（[现金/预算] [空地] [可建] [可开工] [在建] [人口/口粮]）是**契约**：
#    主实现（server/ai_host.py 的起草要求）会按这些名字指代，别改字。
# ⚠️ 文本里**不许**出现「起草」/「draft」：默认路径的回归测试要保证
#    不传 draft 时 user message 里没有这两个词（本快照默认也会拼进去）。


def _budget_line(cap: dict) -> str:
    return (
        f"[现金/预算] cash={_display_num(cap['cash'])} quarter={cap['quarter']}；"
        f"先保证 建造现金+材料+工资+经营费+口粮 ≤ cash"
        f"（建造看下表「折后」与「材料价」；材料/口粮按上季价，"
        f"首季无历史价用 P0，结算时一次扣；生产原料另计）"
    )


def _empty_line(cap: dict) -> str:
    total = int(cap["empty_total"])
    cells = list(cap["empty_cells"])
    if total <= 0:
        return "[空地] 没有空地（地图已满，builds 无处可建）"
    if not cells:
        return f"[空地] 共{total}格（本次未列示例，builds 只能用地空格子）"
    if len(cells) < total:
        return (
            f"[空地] 共{total}格（示例前{len(cells)}）：" + " ".join(cells)
            + f" …（其余 {total - len(cells)} 格也是空地）"
        )
    return f"[空地] 共{total}格（全部列出）：" + " ".join(cells)


def _build_lines(cap: dict) -> list[str]:
    options = cap.get("build_options") or []
    if not options:
        return ["[可建] 没有可建工程"]

    lines = ["[可建] 类型|名|现金|材料|材料价|折后|可产"]
    for opt in options:
        lines.append(
            f"{opt['plot_type']}|{opt['label']}|{_display_num(opt['cash'])}|"
            f"{_material_text(opt['materials'])}|{_display_num(opt['materials_cost'])}|"
            f"{_display_num(opt['cash_after_discount'])}|"
            f"{'/'.join(opt['products']) or '—'}"
        )

    quota = cap.get("quota") or {}
    if quota:
        parts = " ".join(
            f"{name} 剩{info['remaining']}/{info['total']}"
            for name, info in sorted(quota.items())
        )
        lines.append(
            f"（合计价 = 折后 + 材料价；升级工程不在本表。开局减免名额：{parts}）"
        )
    return lines


def _running_line(cap: dict) -> str:
    running = [plot for plot in cap.get("plots") or [] if plot.get("can_run")]
    if not running:
        return "[可开工] 没有可开工地块（先建造，完工后才能开工）"
    parts = [
        f"{plot['plot_id']}({plot['plot_type']} L{plot['level']} "
        f"产{plot['product'] or '—'})"
        for plot in running
    ]
    return "[可开工] " + " ".join(parts) + "（u 取 0–1）"


def _constructing_line(cap: dict) -> str:
    constructing = [
        plot for plot in cap.get("plots") or []
        if plot.get("status") == PlotStatus.CONSTRUCTING.value
    ]
    if not constructing:
        return "[在建] 无"
    parts = [
        f"{plot['plot_id']}({plot['plot_type']} L{plot['level']} 在建)"
        for plot in constructing
    ]
    return "[在建] " + " ".join(parts) + "（在建本季不能开工）"


def _population_line(cap: dict) -> str:
    population = cap.get("population") or {}
    need = cap.get("food_need") or {}
    baseline = need.get("baseline") or {}
    good = need.get("good") or {}

    def pairs(items: dict) -> str:
        return " ".join(f"{goods}={_display_num(qty)}" for goods, qty in items.items()) or "—"

    return (
        f"[人口/口粮] 岗位={_display_num(population.get('labor_total', 0))} "
        f"人口={_display_num(population.get('population', 0))} "
        f"基准档 {pairs(baseline)} 优秀档 {pairs(good)}"
        f"（岗位含服务/研究；口粮只按非服务地块推）"
    )


def format_capabilities(cap: dict) -> str:
    """把 `describe_capabilities()` 的 dict 压成**紧凑中文文本**（给提示词）。

    固定块序（主实现按块名引用）：[现金/预算] → [空地] → [可建] →
    [可开工] → [在建] → [人口/口粮] → 最后一句硬提示。
    """
    lines = [
        _budget_line(cap),
        _empty_line(cap),
        *_build_lines(cap),
        _running_line(cap),
        _constructing_line(cap),
        _population_line(cap),
        "要开工必须写进 plot_plans，漏写=不开工；builds 只能用于 [空地] 里的格子。",
    ]
    return "\n".join(lines)
