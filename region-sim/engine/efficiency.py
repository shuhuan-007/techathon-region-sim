"""效率计算 —— E 是四分量**相乘**，不是相加。

依据《规则基线》§3.1 / §3.2 / §3.6。

    E = E_邻接 × E_幸福 × E_磨合 × (1 + 研究加成)

**乘法关系是防效率重复放大的关键**：四分量各自有上限，
相乘后总上限可控（理论 1.40 × 1.10 × 1.00 × 1.03 ≈ 1.59）。

═══════════════════════════════════════════════════════════════════
★ 服务是唯一的例外分支（§3.6，2026-10-16 改定）
═══════════════════════════════════════════════════════════════════

    通用（农业/资源/工业/研究）：
        数八邻格中**落在关联表里**的邻居 → 每个 +5%，封顶 +40%

    服务：
        **任何**邻居都算（不看关联表）→ 加成率**按服务自身等级**取
            一级 +5% / 二级 +7.5% / 三级 +10%

旧写法是"任何邻居一律 +10%"，导致一级与三级服务收益完全相同、
升级毫无意义。改按等级取率后，升级服务第一次有了直接收益。

⚠️ "别人给服务"的加成**仍走关联表**（服务认 服务/农业/工业），
   每个 +5%、封顶 +40%。两个方向用的不是同一张表。

═══════════════════════════════════════════════════════════════════
为什么计算器要返回拆解
═══════════════════════════════════════════════════════════════════
《规则基线》§11.5 要求邻接计算器**可解释**：玩家得知道效率为什么低，
才知道该摆位置、还是该喂饱人。所以这里返回 `EfficiencyBreakdown`，
而不是一个光秃秃的 float。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from engine.loader import Config
from engine.models import GameState, Plot, PlotStatus, PlotType


@dataclass
class NeighborContribution:
    """一个邻居对 E_邻接 的贡献（§11.5 可解释性字段）。"""

    plot_id: str
    plot_type: Optional[PlotType]     # None = 空地/地图外
    level: Optional[int]
    counts: bool                      # 是否算作"关联邻居"
    reason: str                       # 计入或排除的原因

    def to_dict(self) -> dict:
        return {
            "plot_id": self.plot_id,
            "plot_type": self.plot_type.value if self.plot_type else None,
            "level": self.level,
            "counts": self.counts,
            "reason": self.reason,
        }


@dataclass
class EfficiencyBreakdown:
    """效率四分量拆解（UI 悬停要显示的就是这个）。"""

    plot_id: str
    adjacency: float = 1.0
    happiness: float = 1.0
    ramp_up: float = 1.0
    research_multiplier: float = 1.0

    linked_neighbors: int = 0         # 关联邻居个数（服务=全部邻居数）
    adjacency_bonus: float = 0.0      # 封顶**前**的总加成
    adjacency_capped: bool = False    # 是否被上限截断
    is_service: bool = False
    service_rate: float = 0.0         # 服务专用的等级加成率
    contributions: list[NeighborContribution] = field(default_factory=list)

    @property
    def efficiency(self) -> float:
        """E = 四分量相乘。"""
        return self.adjacency * self.happiness * self.ramp_up * self.research_multiplier

    def to_dict(self) -> dict:
        return {
            "plot_id": self.plot_id,
            "adjacency": self.adjacency,
            "happiness": self.happiness,
            "ramp_up": self.ramp_up,
            "research_multiplier": self.research_multiplier,
            "efficiency": self.efficiency,
            "linked_neighbors": self.linked_neighbors,
            "adjacency_bonus": self.adjacency_bonus,
            "adjacency_capped": self.adjacency_capped,
            "is_service": self.is_service,
            "service_rate": self.service_rate,
            "contributions": [c.to_dict() for c in self.contributions],
        }


def _neighbor_plots(
    state: GameState,
    player_id: str,
    plot_id: str,
) -> list[tuple[str, Optional[Plot]]]:
    """列出八邻格及其地块（地图外/空地返回 None）。

    ⚠️ 邻接只认**八邻格**（含对角），已由用户拍板（§11.7-3）。
    """
    world = state.world
    out: list[tuple[str, Optional[Plot]]] = []
    for nid in world.neighbor_ids(plot_id):
        out.append((nid, state.players[player_id].plots.get(nid)))
    return out


def compute_adjacency(
    state: GameState,
    player_id: str,
    plot: Plot,
    cfg: Config,
) -> tuple[float, list[NeighborContribution], int, float, bool, float]:
    """算 E_邻接（§3.2 通用 / §3.6 服务）。

    返回 ``(E_邻接, 贡献明细, 关联邻居数, 封顶前加成, 是否封顶, 服务率)``。

    ⚠️ **升级中、在建、未运行的地块不提供加成**（§3.2 排除项）。
       空地（无 Plot）当然也不提供。
    """
    eff = cfg.efficiency
    is_service = plot.plot_type is PlotType.SERVICE

    contributions: list[NeighborContribution] = []
    counted = 0

    for nid, nplot in _neighbor_plots(state, player_id, plot.plot_id):
        if nplot is None:
            contributions.append(NeighborContribution(
                nid, None, None, False, "空地，无加成"))
            continue
        if not nplot.is_running:
            # 在建 / 升级中：不提供邻接加成
            contributions.append(NeighborContribution(
                nid, nplot.plot_type, nplot.level, False,
                "在建/升级中，不提供加成"))
            continue

        if is_service:
            # ★ 服务：任何邻居都算，率按自身等级
            counted += 1
            contributions.append(NeighborContribution(
                nid, nplot.plot_type, nplot.level, True,
                "服务：任何邻居都算（不看关联表）"))
        else:
            allowed = eff.adjacency_table.get(plot.plot_type, set())
            if nplot.plot_type in allowed:
                counted += 1
                contributions.append(NeighborContribution(
                    nid, nplot.plot_type, nplot.level, True,
                    f"{nplot.plot_type.value} 在 {plot.plot_type.value} 的关联表里"))
            else:
                contributions.append(NeighborContribution(
                    nid, nplot.plot_type, nplot.level, False,
                    f"{nplot.plot_type.value} 不在 {plot.plot_type.value} 的关联表里"))

    if is_service:
        rate = eff.service_rate(plot.level)
        bonus = rate * counted
        capped = False
        if eff.service_cap is not None:
            capped = bonus > eff.service_cap
            bonus = min(bonus, eff.service_cap)
        return 1.0 + bonus, contributions, counted, bonus, capped, rate

    rate = eff.adjacency_per_neighbor
    raw_bonus = rate * counted
    capped = raw_bonus > eff.adjacency_cap
    bonus = min(raw_bonus, eff.adjacency_cap)
    return 1.0 + bonus, contributions, counted, bonus, capped, 0.0


def research_bonus(state: GameState, player_id: str, plot: Plot, cfg: Config) -> float:
    """研究加成：每块**正常运行**的研究地块 +1%，玩家内封顶 +3%（§3.1）。

    ★ 2026-10-18 最小存储改动：**研究地块有两种编码形态，都被识别**——
      ① `PlotType.RESEARCH`（**独立**研究地块，工程表外）
      ② `PlotType.INDUSTRY + level=2 + product='研究'`（**嵌入**工业的
         研究线二级，是来自 `ind_1_2_research` 的产物）

    ⚠️ **不对研究地块自身加成**（§3.1 明确）——`plot.is_research` 时返回 1.0。
    ⚠️ **不能写成"每季永久累加"** —— 是"当前有 N 块研究在跑 → +min(N%, 3%)"。
       这条最容易写成累加器，一旦写错，几十季后效率会飞上天。
    """
    if plot.is_research:
        return 1.0
    player = state.players[player_id]
    running_research = sum(
        1 for p in player.running_plots
        if p.is_research
    )
    bonus = min(cfg.efficiency.research_bonus_per_plot * running_research,
                cfg.efficiency.research_bonus_cap)
    return 1.0 + bonus


def compute_efficiency(
    state: GameState,
    player_id: str,
    plot: Plot,
    cfg: Config,
) -> EfficiencyBreakdown:
    """算一块地的 E 及四分量拆解（§3.1）。**纯函数，不改状态。**

    ⚠️ `E_幸福` 用的是**上一季**的 H（`player.last_happiness`），
       不是本季的。否则与"粮食 → H → 效率"形成循环依赖，程序算不出来。
       开局 `H₀ = 0.6` → `E_幸福 = 0.90`（§3.3.5）。
    """
    player = state.players[player_id]

    adjacency, contribs, counted, bonus, capped, svc_rate = compute_adjacency(
        state, player_id, plot, cfg)

    happiness = cfg.efficiency.happiness_factor(player.last_happiness)
    ramp = cfg.efficiency.ramp_factor(plot.running_quarters)
    research = research_bonus(state, player_id, plot, cfg)

    return EfficiencyBreakdown(
        plot_id=plot.plot_id,
        adjacency=adjacency,
        happiness=happiness,
        ramp_up=ramp,
        research_multiplier=research,
        linked_neighbors=counted,
        adjacency_bonus=bonus,
        adjacency_capped=capped,
        is_service=plot.plot_type is PlotType.SERVICE,
        service_rate=svc_rate,
        contributions=contribs,
    )
