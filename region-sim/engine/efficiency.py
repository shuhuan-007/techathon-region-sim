"""效率计算 —— E 是四分量**相乘**，不是相加。

依据《规则基线》§3.1 / §3.2 / §3.6。

    E = E_邻接 × E_幸福 × E_磨合 × (1 + 研究加成)

**乘法关系是防效率重复放大的关键**：四分量各自有上限，
相乘后总上限可控（理论 1.40 × 1.10 × 1.00 × 1.03 ≈ 1.59）。

═══════════════════════════════════════════════════════════════════
★ 服务是唯一的例外分支（§3.6，2026-10-18 重做）
═══════════════════════════════════════════════════════════════════

    通用（农业/资源/工业/研究）：
        ① 数八邻格中**落在关联表里**的邻居 → 每个 +5%
        ② 再加上八邻格中**运行中的服务地块** → 按该服务自身等级
           一级 +5% / 二级 +7.5% / 三级 +10%
        ③ ①+② 封顶 +40% → E_邻接 = 1 + bonus

    服务：
        **不接收**任何邻接加成（服务之间也不加成）：
            E_邻接 = 1.0、linked_neighbors = 0、adjacency_bonus = 0
        它只**给出去**：周围任何非服务地块都按它的等级拿加成。

★ 2026-10-18 用户口述重做：旧实现把方向写反了 —— 服务自己数
  "任何邻居"拿加成，而它对普通地块完全不加成。新规则反过来：
  服务是**给别人加成的基础设施**，自己不吃邻接。

⚠️ **两个方向不是同一张表**：
     · 服务 → 普通地块：按服务等级取率（5% / 7.5% / 10%），不看关联表；
     · 关联表（工业↔工业/资源 …）：每邻 +5%，**与上面那笔相加**。

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
    counts: bool                      # 是否计入 E_邻接（关联表邻居 **或** 提供加成的服务）
    reason: str                       # 计入或排除的原因（含"服务提供 +x%"）
    # ★ 2026-10-18：这个邻居**实际贡献的加成率**（0.05 = +5%）。
    #   关联表邻居 = per_neighbor；服务邻居 = 该服务等级对应的率；
    #   空/在建/表外非服务 = 0。**只有它能把"总加成"逐项解释清楚。**
    bonus: float = 0.0

    def to_dict(self) -> dict:
        return {
            "plot_id": self.plot_id,
            "plot_type": self.plot_type.value if self.plot_type else None,
            "level": self.level,
            "counts": self.counts,
            "reason": self.reason,
            "bonus": self.bonus,
        }


@dataclass
class EfficiencyBreakdown:
    """效率四分量拆解（UI 悬停要显示的就是这个）。"""

    plot_id: str
    adjacency: float = 1.0
    happiness: float = 1.0
    ramp_up: float = 1.0
    research_multiplier: float = 1.0

    linked_neighbors: int = 0         # 关联表邻居个数（服务恒为 0 —— 它不吃邻接）
    adjacency_bonus: float = 0.0      # **封顶后**的总加成（表内 + 服务，服务自身恒 0）
    adjacency_capped: bool = False    # 是否被 40% 上限截断
    is_service: bool = False
    service_rate: float = 0.0         # 服务地块**给出去**的等级加成率（非服务恒 0）
    # ★ 2026-10-18：把"服务给的"从总加成里单独拆出来，UI 才能解释
    #   "这 12% 里有 7.5% 是隔壁二级服务给的"。
    service_neighbors: int = 0        # 提供加成的相邻运行中服务地块数
    service_bonus: float = 0.0        # 这些服务贡献的加成（封顶**前**的原始和）
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
            "service_neighbors": self.service_neighbors,
            "service_bonus": self.service_bonus,
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
    """算 E_邻接（§3.2 通用 / §3.6 服务）。**纯函数，不改状态。**

    返回 ``(E_邻接, 贡献明细, 关联邻居数, 封顶后加成, 是否封顶, 服务率)``。

    ★ 2026-10-18 重做后的两条分支：

        非服务：
            bonus = per_neighbor × (关联表邻居数)
                    + Σ(相邻运行中服务地块的 rate(该服务等级))
            bonus = min(bonus, adjacency_cap)
            E_邻接 = 1 + bonus

        服务（服务自己**不吃**邻接）：
            E_邻接 = 1.0、linked_neighbors = 0、adjacency_bonus = 0
            —— 服务之间因此天然互不加成。

    ⚠️ **升级中、在建、未运行的地块不提供加成**（§3.2 排除项）。
       空地（无 Plot）当然也不提供。
    """
    eff = cfg.efficiency
    is_service = plot.plot_type is PlotType.SERVICE

    contributions: list[NeighborContribution] = []
    linked = 0              # 关联表邻居数（服务恒 0）
    service_neighbors = 0   # 提供加成的相邻服务数
    service_bonus = 0.0     # 这些服务贡献的原始加成和

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
            # ★ 服务自己不吃邻接：邻居再多也不加成（服务之间也不互相加成）。
            contributions.append(NeighborContribution(
                nid, nplot.plot_type, nplot.level, False,
                "服务地块不接收邻接加成（服务之间也不加成）"))
            continue

        if nplot.plot_type is PlotType.SERVICE:
            # ★ 服务 → 普通地块：按**服务自身等级**取率。
            rate = eff.service_rate(nplot.level)
            service_neighbors += 1
            service_bonus += rate
            contributions.append(NeighborContribution(
                nid, nplot.plot_type, nplot.level, True,
                f"相邻 {nplot.level} 级服务，提供 +{rate * 100:g}%",
                bonus=rate,
            ))
            continue

        allowed = eff.adjacency_table.get(plot.plot_type, set())
        if nplot.plot_type in allowed:
            linked += 1
            per = eff.adjacency_per_neighbor
            contributions.append(NeighborContribution(
                nid, nplot.plot_type, nplot.level, True,
                f"{nplot.plot_type.value} 在 {plot.plot_type.value} 的关联表里"
                f"（+{per * 100:g}%）",
                bonus=per,
            ))
        else:
            contributions.append(NeighborContribution(
                nid, nplot.plot_type, nplot.level, False,
                f"{nplot.plot_type.value} 不在 {plot.plot_type.value} 的关联表里，"
                f"且不是服务（无加成）",
            ))

    if is_service:
        # 只把"本服务能给出去多少"报给 UI；它自己的 E_邻接 恒为 1.0。
        return 1.0, contributions, 0, 0.0, False, eff.service_rate(plot.level)

    raw_bonus = eff.adjacency_per_neighbor * linked + service_bonus
    capped = raw_bonus > eff.adjacency_cap + 1e-12
    bonus = min(raw_bonus, eff.adjacency_cap)
    return 1.0 + bonus, contributions, linked, bonus, capped, 0.0


def compute_service_income(
    state: GameState,
    player_id: str,
    plot: Plot,
    u: float,
    cfg: Config,
) -> tuple[float, int, float]:
    """服务地块本季的**服务收入**（§3.6，2026-10-18 新增）。

        服务收入 = (八邻格中【运行中的非服务地块】数) × 每邻收入(服务等级)

    * 只数**非服务**邻居 —— 服务之间互不产生收入（与邻接同一口径）。
    * 只数**运行中**的邻居 —— 在建/升级中的地块还没有人气。
    * 服务地块自己**停工**（`u = 0` / 不在 `plot_plans`）→ 收入为 0。
    * 收入不看 `E`、不乘 `u`：这是"开门营业"的季度收入，不是产量。

    返回 ``(收入, 计入的非服务邻居数, 每邻收入)``。
    """
    if plot.plot_type is not PlotType.SERVICE:
        return 0.0, 0, 0.0
    if not plot.is_running or u <= 0.0:
        return 0.0, 0, 0.0

    rate = cfg.service_income_rate(plot.level)
    count = 0
    for _nid, nplot in _neighbor_plots(state, player_id, plot.plot_id):
        if nplot is None or not nplot.is_running:
            continue
        if nplot.plot_type is PlotType.SERVICE:
            continue
        count += 1
    return count * rate, count, rate


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

    # ★ 2026-10-18：从贡献明细里把"服务给的"单独汇总（不改 compute_adjacency
    #   的返回签名 —— ai/ 层与旧调用都按 6 元组解包）。
    svc_n = sum(
        1 for c in contribs
        if c.counts and c.plot_type is PlotType.SERVICE
    )
    svc_bonus = sum(
        c.bonus for c in contribs
        if c.counts and c.plot_type is PlotType.SERVICE
    )

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
        service_neighbors=svc_n,
        service_bonus=svc_bonus,
        contributions=contribs,
    )
