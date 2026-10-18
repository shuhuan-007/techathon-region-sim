"""★ 2026-10-18 地块名字分配（《规则基线》外的 UI 便利层）。

用户给定五种地块类型各 12 个名字（见 `config.json` 顶层 `plot_names`）：
agriculture / resource / industry / service / research（研究＝industry L2 product='研究'）。

分配契约：
  ① **全局不重复**：一局里同名只出现一次（按玩家内已用地块查重）。
  ② **按列表顺序取下一个空闲**：从 `plot_names[type]` 头开始扫，第一个
     未被本玩家占用的名字胜出。
  ③ **用尽退化**：12 名全占 → 返回 `「类型中文-L{target_level}」`
     （重复时按 `plot_id` 再缀尾，例如 `「农业-L1#3,4」`，保底不重复）。
  ④ **只显示**：本模块不影响任何结算数、不进效率/政策/人口公式。

完工时点在 pipeline 里调一次：`_advance_construction`（跨季工程）
与 `_invest_construction`（1 季工程当场完工）各自打一次。

老存档没有 `name` 字段 → `None`，UI 兜底仍显示 `plot_id`。
"""

from __future__ import annotations

from typing import Optional

from engine.loader import Config
from engine.models import GameState, Plot, PlotType


# 与 config.plot_names 的键一一对应；同时是退化方案的"中文标签"
_TYPE_KEY = {
    PlotType.AGRICULTURE: "agriculture",
    PlotType.RESOURCE: "resource",
    PlotType.INDUSTRY: "industry",
    PlotType.SERVICE: "service",
    PlotType.RESEARCH: "research",
}

_TYPE_LABEL = {
    PlotType.AGRICULTURE: "农业",
    PlotType.RESOURCE: "资源",
    PlotType.INDUSTRY: "工业",
    PlotType.SERVICE: "服务",
    PlotType.RESEARCH: "研究",
}


def _name_pool_key(plot: Plot) -> str:
    """按地块形态选名字库键。

    ⚠️ 研究地块按用户 2026-10-18 定案是 `INDUSTRY L2 product='研究'`，
    不独立 PlotType；但**取名字要走 research 列**，不能被当成工业。
    """
    if plot.is_research:
        return "research"
    return _TYPE_KEY.get(plot.plot_type, "industry")


def _type_label(plot: Plot) -> str:
    if plot.is_research:
        return "研究"
    return _TYPE_LABEL.get(plot.plot_type, "工业")


def assign_plot_name(state: GameState, player_id: str, plot: Plot, cfg: Config) -> str:
    """给 `plot` 定一个全局不重复的显示名。

    已占用的集合 = 本玩家所有 `plot.name` 非空的现有地块（含自身若已有）。
    如果 `plot.name` 已设置过则原样返回（**幂等**：不调换不换名）。
    """
    if plot.name:
        return plot.name

    player = state.player(player_id)
    used = {p.name for p in player.plots.values() if p.name and p.plot_id != plot.plot_id}

    pool_key = _name_pool_key(plot)
    pool = (cfg.plot_names or {}).get(pool_key) or []
    for candidate in pool:
        if candidate not in used:
            plot.name = candidate
            return candidate

    # 用尽退化：类型中文-Lxx，再从 plot_id 缀尾保底
    label = _type_label(plot)
    level = plot.target_level if plot.target_level else plot.level
    base = f"{label}-L{level}"
    if base not in used:
        plot.name = base
        return base
    fallback = f"{label}-L{level}#{plot.plot_id}"
    plot.name = fallback
    return fallback


__all__ = ["assign_plot_name"]
