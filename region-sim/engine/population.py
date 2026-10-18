"""人口与粮食需求链 —— L → N → ḡ → m → F_基准 / F_优秀。

依据《规则基线》§3.3。

═══════════════════════════════════════════════════════════════════
★ 全线不取整（2026-10-16 定案，取消 round）
═══════════════════════════════════════════════════════════════════

    L = Σ(所有已建成地块的 d)   岗位数 —— **不随开工比例 u 变**
    N = L × 2                   人口      r = 2
    ḡ = 所有已建成地块的平均等级
    m = 1 + 0.15 × (ḡ − 1)
    F_基准 = (N / 5) × m
    F_优秀 = 1.5 × F_基准

**★ 岗位 vs 工资 vs 产出（2026-10-16 用户拍板，三条一起看）**：

    u 只决定「本季产不产出」——它**不再影响岗位与人口**。
    岗位跟「已建成的地块」走：地建好了，人就在那儿，就要吃饭。
    工资跟「本季实际开工」走（`production.py` 仍是 d × u）：在岗但歇工，不领薪。

    所以一块地建成后本季不申报开工（u=0）时：
      · 产出 = 0、经营费 = 0、工资 = 0        （production.py，不变）
      · 但岗位照占、人口照有、口粮照样要吃   （本模块，★本次改的就是这一条）

    ⚠️ 这条改掉了旧口径 `L = Σ(d × u)`。旧口径下 u=0 等于"没雇人、不用吃饭"，
       玩家只要不申报开工就能规避粮食支出。**基线 §3.3.1 / §3.4 / §8.3 已同步。**

**为什么 L 仍可能带小数**：`d` 本身就是小数——三级农业 d=1.5，建成即 L=1.5。
（旧口径还有一个来源是 `u` 的 0–1 比例，**该来源已随本次改动消失**。）

这不是"人口可以是半个人"，而是 `d` 的口径是**劳动投入当量**而非人头数。
**引擎一律存小数**（否则账对不上），**显示层负责取整**。

⚠️ 想改的是**配方里的 d**（全改整数），不是加一个 round 去掩盖它。

═══════════════════════════════════════════════════════════════════
★ 服务地块不吃粮（2026-10-16 定案）
═══════════════════════════════════════════════════════════════════
服务 0→0，纯资金流，**既不提供也不消耗食物**。
所以用于算人口与粮食需求的劳动量要**把服务地块剔掉**。

    劳动量两个口径：
      labor_total  全部已建成地块 —— 岗位数（显示用）
      labor_food   非服务地块     —— 推出人口 N → 决定粮食需求

⚠️ 但 `ḡ`（平均等级）**仍含服务与研究**（§3.3.2 已拍板：它们也有岗位）。
   两个口径不要混。

⚠️ `labor_worked`（Σ d×u）**只用来对账/显示**，它喂的是工资那条链，
   而工资仍在 `production.py` 里按地块各算各的。本模块不用它推任何东西。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from engine.loader import Config
from engine.models import GameState, PlotStatus, PlotType


@dataclass
class PopulationResult:
    """人口与粮食需求的完整中间量（对账时逐项看）。"""

    labor_total: float          # 全部已建成地块的 Σd —— 岗位数（显示）
    labor_food: float           # 非服务地块的 Σd —— 推出人口用
    population: float           # N = labor_food × r（不取整）
    avg_level: float            # ḡ（含服务/研究）
    level_multiplier: float     # m
    baseline_food: float        # F_基准
    good_food: float            # F_优秀
    working_plots: int = 0      # 参与统计的已建成地块数（ḡ 的分母）
    labor_worked: float = 0.0   # 本季实际开工的 Σ(d×u) —— 喂工资那条链，对账用

    def to_dict(self) -> dict:
        return {
            "labor_total": self.labor_total,
            "labor_food": self.labor_food,
            "labor_worked": self.labor_worked,
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
             ⚠️ **u=0 只影响"本季产不产出"，不再把这块地踢出岗位统计** ——
                人还在、还要吃饭（2026-10-16 用户拍板）。它只进 `labor_worked`。

    ⚠️ 只统计**正常运行**（`PlotStatus.RUNNING`）的地块：
       在建 / 升级中 / 空地都不产生岗位。
    ⚠️ 劳动**不乘 E** —— 效率高只是同样的人干更多批次，不是雇更多人。
    """
    player = state.players[player_id]
    pop_cfg = cfg.population

    labor_total = 0.0
    labor_food = 0.0
    labor_worked = 0.0
    levels: list[int] = []

    for pid in sorted(player.plots):
        plot = player.plots[pid]
        if plot.status is not PlotStatus.RUNNING:
            continue

        # ★ 2026-10-18：走 `recipe_for` helper —— 研究地块（industry L2 '研究'）
        #   的 d=2 被当作岗位置人口/口粮链用，**不抛 ConfigError**。
        #   研究地块**不产商品**，但其 d 与其它地块一样贡献 L → N → F。
        recipe = cfg.recipe_for(plot)

        # ★ 岗位 = d（**不乘 u**）：地建成了，岗位就在那儿，人就要吃饭
        labor_total += recipe.d
        levels.append(plot.level)

        # ★ 服务不吃粮 → 不进"推出人口"的那个口径
        if plot.plot_type is not PlotType.SERVICE:
            labor_food += recipe.d

        # 本季实际开工量：只喂工资那条链（工资在 production.py 按地块各算各的）
        labor_worked += recipe.d * max(plan_u.get(pid, 0.0), 0.0)

    population = pop_cfg.population(labor_food)          # N = L × r，不取整

    # ★ ḡ 的分母是**已建成**地块数，与 u 无关（旧口径按 u>0 筛，会让全停一季
    #   的粮食需求偷偷跌回一级胃口）
    avg_level = (sum(levels) / len(levels)) if levels else 1.0
    baseline = pop_cfg.baseline_food(population, avg_level)
    good = pop_cfg.good_food(baseline)

    return PopulationResult(
        labor_total=labor_total,
        labor_food=labor_food,
        labor_worked=labor_worked,
        population=population,
        avg_level=avg_level,
        level_multiplier=pop_cfg.level_multiplier(avg_level),
        baseline_food=baseline,
        good_food=good,
        working_plots=len(levels),
    )
