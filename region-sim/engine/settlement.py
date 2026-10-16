"""粮食结算与幸福度 —— 结算时扣 A/B/C/D，算出本季 H。

依据《规则基线》§3.3.3 / §3.3.4 / §3.3.6。

═══════════════════════════════════════════════════════════════════
★ 时序：结算时才扣，不在期初
═══════════════════════════════════════════════════════════════════
期初是玩家**支配资金**的阶段——他可以自己决定这一季要不要花钱买粮、
买多少、要不要冲优秀档。系统**不预先代扣**，也不强制满足需求。
**买不买、买多少，是玩家的经营决策。**

**食物不够会直接压低下季效率**——这是本机制的核心惩罚回路。
**没有"只扣到基准线就停"这回事**（部分达到时扣光手上所有能吃的）。

═══════════════════════════════════════════════════════════════════
★ 两档要的东西不一样（§3.3.3）
═══════════════════════════════════════════════════════════════════

    基准档：只要 **A、B**     配额 = 0.4 / 0.2（× F_基准）
    优秀档：要 **A、B、C、D** 配额 = 0.4 / 0.2 / 0.1 / 0.02（× F_优秀）

基准档的判定式：

    R = 0.6 × F_基准                       A、B 合计口粮需求
    S = min(A, 0.4×F_基准) + min(B, 0.2×F_基准)

    S ≥ R → 基准达标

⚠️ "A、B 必须都有"这条**不需要单独的 if**：因为
   `S ≤ 0.4F + 0.2F = 0.6F = R`，取等号要求 A ≥ 0.4F **且** B ≥ 0.2F。
   所以只要有一边是 0，S 必然 < R，判定式自动不成立。
   多写一个 if 反而容易和判定式打架。

═══════════════════════════════════════════════════════════════════
⚠️ 一处**规则未完全指定**、本模块自行定的口径（需用户确认）
═══════════════════════════════════════════════════════════════════
H 的插值公式本身是定死的（§3.3.4），但它依赖一个量：
**"实际达成的等效篮位数 F"**。而"四样商品怎么折算成 F"
在文档里只给了两个锚点（基准、优秀）和几行示例，**中间带没有明确定义**。

本模块采用的口径（能同时对上文档里全部锚点）：

    achieved_base = S / 0.6                        ← 基准档视角的达成度
    achieved_good = min(A/0.4, B/0.2, C/0.1, D/0.02)  ← 优秀档视角（四样取瓶颈）
    achieved_F    = max(achieved_base, achieved_good)

验证（F_基准 = 2.0、F_优秀 = 3.0）：

    A0.8/B0.4            → achieved 2.0 → H = 0.60  ✅ 文档锚点
    A1.2/B0.6/C0.3/D0.06 → achieved 3.0 → H = 1.00  ✅ 文档锚点
    A1.2/B0.6/C0/D0      → achieved 2.0 → H = 0.60  ✅ "C/D 只在优秀档"
    A1.0/B0.5/C0.25/D0.05→ achieved 2.5 → H = 0.80  ← 中间带，本口径的定义
    A0/B1.2              → achieved 0.667 → H = 0.20（部分达成）
    完全不投             → achieved 0     → H = 0     ✅ 文档锚点

⚠️ 中间带那一行是本项目**自己的解释**，不是文档原文。
   如果用户对中间带有别的想法，改 `_achieved_food_level()` 一个函数即可。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from engine.loader import Config
from engine.models import GameState, PlayerState


@dataclass
class FoodResult:
    """一季粮食结算的完整结果（对账时逐项看）。"""

    baseline_need: float           # F_基准
    good_need: float               # F_优秀
    available: dict[str, float]    # 结算时手上可用于吃的 A/B/C/D
    baseline_met: bool
    good_met: bool
    achieved_level: float          # 实际达成的等效篮位数 F
    deducted: dict[str, float]     # 实际扣减量
    happiness: float               # 本季 H

    def to_dict(self) -> dict:
        return {
            "baseline_need": self.baseline_need,
            "good_need": self.good_need,
            "available": dict(self.available),
            "baseline_met": self.baseline_met,
            "good_met": self.good_met,
            "achieved_level": self.achieved_level,
            "deducted": dict(self.deducted),
            "happiness": self.happiness,
        }


# 参与粮食结算的商品（§3.3.3）
FOOD_GOODS: tuple[str, ...] = ("A", "B", "C", "D")


def _achieved_food_level(
    available: dict[str, float],
    baseline_need: float,
    good_need: float,
    cfg: Config,
) -> float:
    """把"手上四样各多少"折算成**实际达成的等效篮位数 F**。

    见模块头部的口径说明。这是全项目**唯一**需要解释的地方，
    改口径只改这一个函数。
    """
    quotas_b = cfg.food.baseline_quota      # {A:0.4, B:0.2}
    quotas_g = cfg.food.good_quota          # {A:0.4, B:0.2, C:0.1, D:0.02}

    # ── 基准档视角 ──────────────────────────────────────
    # S = min(A, 0.4×F_基准) + min(B, 0.2×F_基准)，满值 0.6×F_基准
    covered_b = 0.0
    total_ratio_b = 0.0
    for g, ratio in quotas_b.items():
        cap = ratio * baseline_need
        covered_b += min(available.get(g, 0.0), cap)
        total_ratio_b += ratio
    achieved_base = covered_b / total_ratio_b if total_ratio_b > 0 else 0.0

    # ── 优秀档视角：四样取瓶颈 ──────────────────────────
    achieved_good = float("inf")
    for g, ratio in quotas_g.items():
        if ratio <= 0:
            continue
        achieved_good = min(achieved_good, available.get(g, 0.0) / ratio)
    if achieved_good == float("inf"):
        achieved_good = 0.0

    # 优秀档的瓶颈值再按 F_优秀 封顶（多投不产生额外幸福度）
    achieved_good = min(achieved_good, good_need)

    return max(achieved_base, achieved_good)


def happiness_from_level(
    achieved: float,
    baseline_need: float,
    good_need: float,
    cfg: Config,
) -> float:
    """由实际达成量算 H（§3.3.4）。**两段直线 + 一段封顶**。

    ★ 三段形状（用户 2026-10-16 确认：中间就是线性）::

        F < F_基准           H = 0.6 × (F / F_基准)                  斜率 0.6/F_基准
        F_基准 ≤ F < F_优秀   H = 0.6 + 0.4 × (F−F_基准)/(F_优秀−F_基准) 斜率 0.4/(F_优秀−F_基准)
        F ≥ F_优秀           H = 1.0                                 斜率 0

    ★ **中间段的斜率可以化简**（因为 `F_优秀 = 1.5 × F_基准`）::

        F_优秀 − F_基准 = 0.5 × F_基准
        斜率 = 0.4 / (0.5 × F_基准) = **0.8 / F_基准**

    也就是说：**中段斜率是下段斜率的 4/3**（0.8 / 0.6 ≈ 1.333）。

    以 `F_基准 = 2.0` 为例：下段斜率 0.30，中段斜率 **0.40**，中段从
    `(2.0, 0.60)` 直线到 `(3.0, 1.00)`。

    **两个锚点**：吃饱 → 0.60；吃好 → 1.00。**中间直线连接，不拐弯。**
    """
    if baseline_need <= 0:
        return 1.0
    if achieved < baseline_need:
        return 0.6 * (achieved / baseline_need)
    if achieved >= good_need:
        return 1.0
    span = good_need - baseline_need
    if span <= 0:
        return 0.6
    return 0.6 + 0.4 * (achieved - baseline_need) / span


def happiness_slopes(baseline_need: float, good_need: float) -> dict[str, float]:
    """H 曲线两段的斜率（供 UI / 对账显示用）。

    ★ 化简后的形式（`F_优秀 = 1.5 × F_基准` 时）::

        下段（饿着）    斜率 = 0.6 / F_基准
        中段（吃饱→吃好）斜率 = 0.8 / F_基准   ← 是下段的 4/3

    例：`F_基准 = 2.0` → 下段 0.30、中段 0.40。
    """
    if baseline_need <= 0:
        return {"lower": 0.0, "middle": 0.0}
    lower = 0.6 / baseline_need
    span = good_need - baseline_need
    middle = (0.4 / span) if span > 0 else 0.0
    return {"lower": lower, "middle": middle}


def plan_food_deduction(
    available: dict[str, float],
    baseline_need: float,
    good_need: float,
    cfg: Config,
) -> "FoodPlan":
    """**纯函数**：只算"会扣哪些、扣多少"，**不动库存**（§3.3.6）。

    ★ 为什么要单独抽出来（2026-10-16 修的一个漏洞）：

    季末清仓（§6 第 7.7 步）卖出去的货，**也必须算进本季的总供给 `X_t`**，
    否则玩家只要"什么都不申报"，就能把一大堆货按**上季价**清掉，
    等于绕开了"供给冲击压价"这条规则。

    而 `X_t = 申报卖出 + 清仓量`，清仓量又 = `可供出售 − 申报卖出 − 居民吃掉`。
    所以**求价之前必须先知道"居民会吃掉多少"**。

    ⚠️ 这里**没有循环依赖**：居民吃多少**只看数量、不看价格**
       （§4.3.7 明确作废了预算约束 `D_实 = min(D, B/k)`）。
       所以可以在求价之前先按数量把扣减算出来。
    """
    achieved = _achieved_food_level(available, baseline_need, good_need, cfg)
    goodness = happiness_from_level(achieved, baseline_need, good_need, cfg)

    achieved_base = achieved_ge_baseline(available, baseline_need, cfg)
    achieved_good = achieved_ge_good(available, good_need, cfg)

    deducted: dict[str, float] = {}

    if achieved_good:
        # 扣满四样配额（按 F_优秀 的配额）
        for g, ratio in cfg.food.good_quota.items():
            want = ratio * good_need
            actual = min(available.get(g, 0.0), want)
            if actual > 0:
                deducted[g] = actual
    elif achieved_base:
        # 只扣 A、B（按 F_基准 的配额），C/D 不动
        for g, ratio in cfg.food.baseline_quota.items():
            want = ratio * baseline_need
            actual = min(available.get(g, 0.0), want)
            if actual > 0:
                deducted[g] = actual
    else:
        # 部分达到：扣光手上所有能吃的
        for g in FOOD_GOODS:
            have = available.get(g, 0.0)
            if have > 0:
                deducted[g] = have

    return FoodPlan(
        baseline_met=achieved_base,
        good_met=achieved_good,
        achieved_level=achieved,
        happiness=goodness,
        deducted=deducted,
    )


@dataclass
class FoodPlan:
    """粮食扣减的**计划**（纯数据，尚未执行）。

    `settle_food` 会把它应用到库存上；`pipeline` 在求价前用它推算清仓量。
    """

    baseline_met: bool
    good_met: bool
    achieved_level: float
    happiness: float
    deducted: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "baseline_met": self.baseline_met,
            "good_met": self.good_met,
            "achieved_level": self.achieved_level,
            "happiness": self.happiness,
            "deducted": dict(self.deducted),
        }


def settle_food(
    state: GameState,
    player_id: str,
    baseline_need: float,
    good_need: float,
    cfg: Config,
) -> FoodResult:
    """粮食结算：判定两档 → 扣减 → 得 H（§3.3.6）。

    ⚠️ **会修改 `player.inventory`**（扣减实物）。这是"结算"而非"试算"。
       试算请先 `state.deep_copy()`（见 `pipeline.preview`）。

    实现上只是 `plan_food_deduction()` + 真正执行扣减，
    **保证"预先推算的清仓量"与"实际扣减"用的是同一套口径**。
    """
    player: PlayerState = state.players[player_id]

    available = {g: player.inventory.get(g, 0.0) for g in FOOD_GOODS}
    plan = plan_food_deduction(available, baseline_need, good_need, cfg)

    deducted: dict[str, float] = {}
    for g, want in plan.deducted.items():
        actual = player.take_inventory(g, want)
        if actual:
            deducted[g] = actual

    player.last_happiness = plan.happiness

    return FoodResult(
        baseline_need=baseline_need,
        good_need=good_need,
        available=available,
        baseline_met=plan.baseline_met,
        good_met=plan.good_met,
        achieved_level=plan.achieved_level,
        deducted=deducted,
        happiness=plan.happiness,
    )


def achieved_ge_baseline(
    available: dict[str, float],
    baseline_need: float,
    cfg: Config,
) -> bool:
    """基准档是否达标（§3.3.3）。

        R = 0.6 × F_基准
        S = min(A, 0.4×F_基准) + min(B, 0.2×F_基准)
        S ≥ R → 达标

    "A、B 必须都有"由式子本身保证（见模块头部说明）。
    """
    quotas = cfg.food.baseline_quota
    total_ratio = sum(quotas.values())
    if total_ratio <= 0:
        return True
    need = total_ratio * baseline_need           # R
    covered = sum(
        min(available.get(g, 0.0), ratio * baseline_need)
        for g, ratio in quotas.items()
    )                                            # S
    # 浮点容差：供需刚好打平时应算达标
    return covered >= need - 1e-9


def achieved_ge_good(
    available: dict[str, float],
    good_need: float,
    cfg: Config,
) -> bool:
    """优秀档是否达标（§3.3.3）：**四样都要满足各自配额**，缺一样即不达标。"""
    for g, ratio in cfg.food.good_quota.items():
        if available.get(g, 0.0) < ratio * good_need - 1e-9:
            return False
    return True
