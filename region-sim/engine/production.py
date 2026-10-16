"""生产计算 —— 一个地块一季到底产出多少、投入多少、花多少钱。

依据《规则基线》§3.4（三量关系）与 §3.5（生产总成本）。

═══════════════════════════════════════════════════════════════════
★ 三量关系（§3.4）—— 本项目最容易写错的一条
═══════════════════════════════════════════════════════════════════

    产量       = e × u × E            ← 随 u 与 E 放大
    商品投入   = input × u × E        ← 原料跟着产出一起放大
    劳动需求   = d × u                ← ★ 不乘 E
    经营费     = 0                    , u = 0
               = 0.7c + 0.3c × u × E  , u > 0

**"劳动不乘 E"必须写进注释**：效率提高的含义是"同样的人一季处理更多批次"，
而不是"雇更多人"。写成 `d × u × E` 会让工资随效率一起涨，
M1 对账时甲区会从 36 变成别的数，且极难查。

**经营费的 0.7 / 0.3 拆分**同理：固定部分（维护、场地）照付，
变动部分（能源、耗材）才随开工率与效率走。两个数来自 config，代码不含字面量。

═══════════════════════════════════════════════════════════════════
本模块是纯函数
═══════════════════════════════════════════════════════════════════
输入 state / plot / recipe / u / E，输出一个 `PlotOutcome`，
**不改任何状态**。谁来扣钱、什么时候扣，是 `market.py` 与 `pipeline.py` 的事。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from engine.loader import Config
from engine.models import Plot, PlotType, Recipe


@dataclass
class PlotOutcome:
    """一块地一个季度的生产结果（**纯数据，不改状态**）。

    这是"对账"的基本单位：把每块地的这些数摊开，
    就能一眼看出账是在哪一步对不上的。
    """

    plot_id: str
    plot_type: PlotType
    level: int
    product: Optional[str]

    # ── 三量 ────────────────────────────────────────────
    u: float                        # 开工比例（生效值，active=False 时为 0）
    efficiency: float               # E（四分量乘积）
    output_goods: Optional[str]     # 产出商品；服务/研究为 None
    output_qty: float               # 产量 = e × u × E
    inputs: dict[str, float]        # 商品投入 = input × u × E

    # ── 劳动与费用 ──────────────────────────────────────
    labor: float                    # 劳动需求 = d × u（不乘 E）
    wage_rate: float                # 工资单价（/单位劳动/季）
    wage: float                     # 工资总额 = labor × wage_rate（税前）
    operating_cost: float           # 经营费 = 0.7c + 0.3c·u·E（u=0 时为 0）

    # ── 汇总 ────────────────────────────────────────────
    @property
    def cash_out_before_materials(self) -> float:
        """工资 + 经营费（不含物资款）。

        物资款要按**上季价**算，属于市场模块的职责，
        所以这里只汇总"确定的那部分"。
        """
        return self.wage + self.operating_cost

    def material_cost(self, prices: dict[str, float]) -> float:
        """按给定价格表算这块地的原料费。

        ⚠️ 传进来的通常是**上季结算价**（§4.1 交割口径），不是参考价。
        """
        return sum(qty * prices[g] for g, qty in self.inputs.items())

    def to_dict(self) -> dict:
        return {
            "plot_id": self.plot_id,
            "plot_type": self.plot_type.value,
            "level": self.level,
            "product": self.product,
            "u": self.u,
            "efficiency": self.efficiency,
            "output_goods": self.output_goods,
            "output_qty": self.output_qty,
            "inputs": dict(self.inputs),
            "labor": self.labor,
            "wage_rate": self.wage_rate,
            "wage": self.wage,
            "operating_cost": self.operating_cost,
        }


def operating_cost(c: float, u: float, efficiency: float, cfg: Config) -> float:
    """经营费（§3.4）。

        u = 0 → 0
        u > 0 → fixed_share×c + variable_share×c×u×E

    ⚠️ 停工（u=0）时经营费为 **0**，不是 0.7c。
       这是"关停地块能止血"这条玩法的基础；写成固定照付会让玩家没法止损。
    """
    if u <= 0.0:
        return 0.0
    fixed = cfg.production_operating_fixed_share * c
    variable = cfg.production_operating_variable_share * c * u * efficiency
    return fixed + variable


def run_plot(
    plot: Plot,
    recipe: Recipe,
    u: float,
    efficiency: float,
    cfg: Config,
    wage_rate: float,
) -> PlotOutcome:
    """算一块地一个季度的生产结果（§3.4 / §3.5）。**纯函数，不改状态。**

    参数
    ----
    plot        : 地块（只读，用于取类型/等级/产物）
    recipe      : 该地块本季用的配方
    u           : 开工比例（0–1）
    efficiency  : E（四分量乘积，由 efficiency.py 算出）
    wage_rate   : 该活动的工资单价

    ⚠️ **本函数不判断"是否该开工"**。`u=0` 直接传 0 即可，
       产出、投入、劳动全为 0，经营费也为 0。
    """
    if u < 0.0:
        raise ValueError(f"开工比例不能为负：地块 {plot.plot_id} 收到 u={u}")

    scale = u * efficiency

    # 产量与商品投入**同步放大**（§3.4）：
    # 效率高 = 同样的人处理更多批次 = 原料和产出一起变。
    inputs = {g: qty * scale for g, qty in recipe.input.items() if qty * scale != 0.0}
    output_qty = recipe.e * scale

    # ★ 劳动**不乘 E** —— 只随开工比例变。
    labor = recipe.d * u

    return PlotOutcome(
        plot_id=plot.plot_id,
        plot_type=plot.plot_type,
        level=plot.level,
        product=plot.product or recipe.output,
        u=u,
        efficiency=efficiency,
        output_goods=recipe.output,
        output_qty=output_qty,
        inputs=inputs,
        labor=labor,
        wage_rate=wage_rate,
        wage=labor * wage_rate,
        operating_cost=operating_cost(recipe.c, u, efficiency, cfg),
    )


def total_material_cost(
    outcomes: list[PlotOutcome],
    prices: dict[str, float],
) -> float:
    """把一批地块的原料费加总（按给定价格表）。"""
    total = 0.0
    for o in outcomes:
        total += o.material_cost(prices)
    return total


def merge_inputs(outcomes: list[PlotOutcome]) -> dict[str, float]:
    """把一批地块的商品投入**按商品合并**。

    用途：合并后的结果就是本季要向市场买入的**生产原料**部分，
    它要计入 `B_t`（§4.1：买入量顶高本季价）。
    """
    merged: dict[str, float] = {}
    for o in outcomes:
        for g, qty in o.inputs.items():
            merged[g] = merged.get(g, 0.0) + qty
    return merged


def merge_outputs(outcomes: list[PlotOutcome]) -> dict[str, float]:
    """把一批地块的产出**按商品合并**（服务/研究无产出，自动跳过）。"""
    merged: dict[str, float] = {}
    for o in outcomes:
        if o.output_goods is None or o.output_qty == 0.0:
            continue
        merged[o.output_goods] = merged.get(o.output_goods, 0.0) + o.output_qty
    return merged
