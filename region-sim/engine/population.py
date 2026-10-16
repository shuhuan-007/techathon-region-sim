"""人口与粮食需求链 —— L → N → ḡ → m → F_基准 / F_优秀。

依据《规则基线》§3.3。

═══════════════════════════════════════════════════════════════════
★ 全线不取整（2026-10-16 定案，取消 round）
═══════════════════════════════════════════════════════════════════

    L = Σ(d × u)          岗位数 = 劳动当量（**天然带小数**）
    N = L × 2             人口      r = 2
    ḡ = 开工地块平均等级
    m = 1 + 0.15 × (ḡ − 1)
    F_基准 = (N / 5) × m
    F_优秀 = 1.5 × F_基准

**为什么 L 是小数**：`L` 不是"几块地"，而是"这季实际雇了多少劳动量"。
两个来源都带小数——

    · `u` 是 0–1 的比例：一块 d=2 的地半开工 → L = 2 × 0.5 = 1
    · `d` 本身就是小数：三级农业 d=1.5，满负荷 → L = 1.5

配方里三级地块的 `d` 大量是 1.5 / 2.5，所以**即使全部满负荷，L 也带小数**。

这不是"人口可以是半个人"，而是 `d` 的口径是**劳动投入当量**而非人头数。
**引擎一律存小数**（否则账对不上），**显示层负责取整**。

⚠️ 想改的是**配方里的 d**（全改整数）+ **u 的粒度**，
   不是加一个 round 去掩盖它。

═══════════════════════════════════════════════════════════════════
★ 服务地块不吃粮（2026-10-16 定案）
═══════════════════════════════════════════════════════════════════
服务 0→0，纯资金流，**既不提供也不消耗食物**。
所以用于算人口与粮食需求的劳动量要**把服务地块剔掉**。

    劳动量两个口径：
      labor_total  全部开工地块 —— 岗位数（显示用）
      labor_food   非服务地块   —— 推出人口 N → 决定粮食需求

⚠️ 但 `ḡ`（平均等级）**仍含服务与研究**（§3.3.2 已拍板：它们也有岗位）。
   两个口径不要混。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from engine.loader import Config
from engine.models import GameState, PlotStatus, PlotType


@dataclass
class PopulationResult:
    """人口与粮食需求的完整中间量（对账时逐项看）。"""

    labor_total: float          # 全部开工地块的 Σ(d×u) —— 岗位数（显示）
    labor_food: float           # 非服务地块的 Σ(d×u) —— 推出人口用
    population: float           # N = labor_food × r（不取整）
    avg_level: float            # ḡ（含服务/研究）
    level_multiplier: float     # m
    baseline_food: float        # F_基准
    good_food: float            # F_优秀
    working_plots: int = 0      # 参与统计的开工地块数（ḡ 的分母）

    def to_dict(self) -> dict:
        return {
            "labor_total": self.labor_total,
            "labor_food": self.labor_food,
            "population": self.population,
            "avg_level": self.avg_level,
            "level_multiplier": self.level_multiplier,
            "baseline_food": self.baseline_food,
            "good_food": self.good_food,
            "working_plots": self.working_plots,
        }


def compute_population(
    state: GameState,
    player_id: str,
    plan_u: dict[str, float],
    cfg: Config,
) -> PopulationResult:
    """算岗位 → 人口 → 粮食需求（§3.3）。**纯函数，不改状态。**

    参数
    ----
    plan_u : {地块ID: 开工比例} —— 本季玩家为每块地申报的 u。
             没出现在字典里的地块视为**不开工**（u=0）。

    ⚠️ 只统计**正常运行**（`PlotStatus.RUNNING`）的地块：
       在建 / 升级中 / 空地都不产生岗位。
    ⚠️ 劳动**不乘 E** —— 效率高只是同样的人干更多批次，不是雇更多人。
    """
    player = state.players[player_id]
    pop_cfg = cfg.population

    labor_total = 0.0
    labor_food = 0.0
    levels: list[int] = []

    for pid in sorted(player.plots):
        plot = player.plots[pid]
        if plot.status is not PlotStatus.RUNNING:
            continue
        u = plan_u.get(pid, 0.0)
        if u <= 0.0:
            continue

        recipe = cfg.recipe(plot.plot_type, plot.level, plot.product)

        # ★ 劳动 = d × u（不乘 E）
        labor = recipe.d * u
        labor_total += labor
        levels.append(plot.level)

        # ★ 服务不吃粮 → 不进"推出人口"的那个口径
        if plot.plot_type is not PlotType.SERVICE:
            labor_food += labor

    population = pop_cfg.population(labor_food)          # N = L × r，不取整

    avg_level = (sum(levels) / len(levels)) if levels else 1.0
    baseline = pop_cfg.baseline_food(population, avg_level)
    good = pop_cfg.good_food(baseline)

    return PopulationResult(
        labor_total=labor_total,
        labor_food=labor_food,
        population=population,
        avg_level=avg_level,
        level_multiplier=pop_cfg.level_multiplier(avg_level),
        baseline_food=baseline,
        good_food=good,
        working_plots=len(levels),
    )
