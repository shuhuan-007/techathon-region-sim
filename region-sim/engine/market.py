"""市场结算 —— 全国供需曲线、求价、买入计价、触底分配。

依据《规则基线》§4.1（全国商品市场）与 2026-10-16 的交割口径裁定。

═══════════════════════════════════════════════════════════════════
★ 交割口径（2026-10-16 用户裁定，§4.1）—— 本模块的核心
═══════════════════════════════════════════════════════════════════

    买入：按【上一季度结算出来的价格】记账
    卖出：按【本季求出的 P_t】
    扣款：物资款 + 工资 + 经营费，在**结算时一次性**扣

关键连锁 —— **买入量要加进本季的需求侧**：

    B_t = 生产原料 + 建设物资 + 居民口粮      ← 三段全部计入
    P_t = P0 + (B_t − X_t) / (s + d)

也就是说，**买货这个动作本身会把本季价格顶上去**。
代价在季末体现：你按上季价拿货，但你的买入让本季价变高。

为什么这个闭环自洽：买入锁旧价、卖出随新价，两者价差就是交割成本，
天然惩罚无脑囤货。

⚠️ **期初（玩家阶段）不扣钱、不锁价、不动账户**——玩家只是写意愿。
   "期初买"是口语说法，落点是**价格用的是上季那个价**。

═══════════════════════════════════════════════════════════════════
曲线与下限
═══════════════════════════════════════════════════════════════════

    S_外(P) = max(0, Q0 + s(P − P0))        s = Es × Q0 / P0
    D_外(P) = max(0, Q0 − d(P − P0))        d = |Ed| × Q0 / P0

程序**用原始弹性算斜率**，不手工维护四舍五入后的数。

`P_min = 0.1 × P0`。到下限仍供过于求时，**按可实现需求量分配成交，
不保证全量收购**；未售商品清理，不产生收入。

⚠️ 程序**不能**只把负价格截成正数然后仍让全部产量成交——那等于凭空造需求。

═══════════════════════════════════════════════════════════════════
弹性缺失时怎么办
═══════════════════════════════════════════════════════════════════
`Es`/`Ed` 目前**未填**。本模块**明确报错，不编造默认值**（§11.6 数据纪律）。
理由：弹性直接决定成交价，捏一个值会让后面所有对账失去意义。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from engine.models import MarketCurve, WorldState


# ══════════════════════════════════════════════════════════════════
# 一、单个商品的求价
# ══════════════════════════════════════════════════════════════════


@dataclass
class PriceResult:
    """一个商品本季的求价结果（供对账展开用）。"""

    goods: str
    bought: float           # B_t：本季买入量（三段合计）
    sold: float             # X_t：本季卖出量
    P0: float
    s: float                # 供给斜率
    d: float                # 需求斜率
    price: float            # 本季成交价 P_t
    at_floor: bool          # 是否触底
    sellable: float         # 触底时**实际能卖掉**的量（不触底 = sold）

    def to_dict(self) -> dict:
        return {
            "goods": self.goods, "bought": self.bought, "sold": self.sold,
            "P0": self.P0, "s": self.s, "d": self.d,
            "price": self.price, "at_floor": self.at_floor,
            "sellable": self.sellable,
        }


def solve_price(
    curve: MarketCurve,
    bought: float,
    sold: float,
) -> PriceResult:
    """求一个商品的本季成交价（§4.1）。**纯函数。**

        P_t = P0 + (B_t − X_t) / (s + d)
        P_t = max(P_t, P_min)        P_min = 0.1 × P0

    ⚠️ `bought`（B_t）**必须包含三段**：生产原料 + 建设物资 + 居民口粮。
       漏掉任何一段都会让本季价格偏低，进而让卖出收入偏高——
       这是最容易发生、又最难察觉的一类错。

    ⚠️ 弹性未填时 `curve.slopes()` 会抛错。这是设计如此，不要捕获后糊过去。
    """
    s, d = curve.slopes()          # 缺弹性 → 这里明确报错
    denom = s + d
    if denom == 0:
        raise ValueError(
            f"商品 {curve.goods!r} 的 s + d = 0，曲线退化为水平线，无法求价。"
            f"（Es={curve.Es}, Ed={curve.Ed}, Q0={curve.Q0}, P0={curve.P0}）"
        )

    raw = curve.P0 + (bought - sold) / denom
    p_min = curve.P_min
    price = max(raw, p_min)
    at_floor = raw < p_min

    # 触底时按**可实现需求量**分配成交，不保证全量收购（§4.1）。
    sellable = demand_at(curve, price) if at_floor else sold

    return PriceResult(
        goods=curve.goods, bought=bought, sold=sold,
        P0=curve.P0, s=s, d=d,
        price=price, at_floor=at_floor, sellable=sellable,
    )


def external_supply(curve: MarketCurve, price: float) -> float:
    """`S_外(P) = max(0, Q0 + s(P − P0))`（外部供给）。"""
    s, _ = curve.slopes()
    return max(0.0, curve.Q0 + s * (price - curve.P0))


def demand_at(curve: MarketCurve, price: float) -> float:
    """`D_外(P) = max(0, Q0 − d(P − P0))`（外部需求）。

    ⚠️ 触底时用它决定"实际能卖掉多少"。**不是**把价格截成正数就算完。
    """
    _, d = curve.slopes()
    return max(0.0, curve.Q0 - d * (price - curve.P0))


# ══════════════════════════════════════════════════════════════════
# 二、全市场求价
# ══════════════════════════════════════════════════════════════════


def settle_prices(
    world: WorldState,
    bought: dict[str, float],
    sold: dict[str, float],
) -> dict[str, PriceResult]:
    """对**所有有买卖的商品**求本季价（§4.1）。

    没有任何买卖的商品不求价（沿用上期价），避免无谓地要求填弹性。
    """
    results: dict[str, PriceResult] = {}
    for g in sorted(set(bought) | set(sold)):
        b = bought.get(g, 0.0)
        x = sold.get(g, 0.0)
        if b == 0.0 and x == 0.0:
            continue
        curve = world.market.get(g)
        if curve is None:
            raise KeyError(f"商品 {g!r} 不在市场配置里，无法求价")
        results[g] = solve_price(curve, b, x)
    return results


# ══════════════════════════════════════════════════════════════════
# 三、买入计价（★ 用上季价，不是本季价）
# ══════════════════════════════════════════════════════════════════


@dataclass
class BuyBill:
    """买入账单：按**上季结算价**算出的物资款（§4.1）。"""

    quantities: dict[str, float] = field(default_factory=dict)
    unit_prices: dict[str, float] = field(default_factory=dict)
    lines: dict[str, float] = field(default_factory=dict)   # 商品 → 小计
    total: float = 0.0

    def to_dict(self) -> dict:
        return {
            "quantities": dict(self.quantities),
            "unit_prices": dict(self.unit_prices),
            "lines": dict(self.lines),
            "total": self.total,
        }


def buy_bill(world: WorldState, bought: dict[str, float]) -> BuyBill:
    """按【上季结算价】算买入账单（§4.1 交割口径）。

    ★ 这是"买入按上季价"的落地点。
      首季没有上季价时，`WorldState.last_price()` 退回参考价 `P0`（§8.1）。

    ⚠️ 这里**只算钱，不改状态**。真正扣款在结算阶段的"一次性扣款"
       （物资款 + 工资 + 经营费），见 `pipeline.py`。
    """
    bill = BuyBill()
    for g in sorted(bought):
        qty = bought[g]
        if qty == 0.0:
            continue
        unit = world.last_price(g)        # ← 上季价（首季回退 P0）
        bill.quantities[g] = qty
        bill.unit_prices[g] = unit
        bill.lines[g] = qty * unit
        bill.total += qty * unit
    return bill


def sell_revenue(prices: dict[str, PriceResult], sold: dict[str, float]) -> float:
    """按**本季价**算卖出收入（§4.1）。

    ⚠️ 触底商品只算 `sellable` 那部分收入，未售部分**清理、不产生收入**。
    """
    total = 0.0
    for g, qty in sold.items():
        if qty == 0.0:
            continue
        pr = prices.get(g)
        if pr is None:
            raise KeyError(f"商品 {g!r} 有卖出量但没有求价结果")
        # 触底时只卖得掉 sellable；否则全卖。
        effective = min(qty, pr.sellable) if pr.at_floor else qty
        total += effective * pr.price
    return total


def unsold_after_floor(prices: dict[str, PriceResult], sold: dict[str, float]) -> dict[str, float]:
    """触底卖不掉的量（会被清理，不产生收入）。

    用途：财报里要如实显示"这批货没卖出去"，而不是让它悄悄消失。
    """
    left: dict[str, float] = {}
    for g, qty in sold.items():
        pr = prices.get(g)
        if pr is None or not pr.at_floor:
            continue
        gap = qty - min(qty, pr.sellable)
        if gap > 0:
            left[g] = gap
    return left


# ══════════════════════════════════════════════════════════════════
# 四、共享资源分配策略（红线 4：可替换，不写死）
# ══════════════════════════════════════════════════════════════════

Allocator = Callable[[dict[str, float], float], dict[str, float]]


def allocate_proportional(demand: dict[str, float], supply: float) -> dict[str, float]:
    """按比例分配（§1.2 的默认策略）。

    红线 4 要求：**共享资源的分配规则做成可替换策略函数**，
    不要在市场模块里写死"先到先得"。

    ⚠️ 第一期只有一个玩家，这个分配器暂时用不上；
       留着是为了以后加联机时**引擎核心一行不改**。
    """
    total = sum(demand.values())
    if total <= 0:
        return {}
    return {k: supply * v / total for k, v in demand.items()}
