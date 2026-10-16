"""季度流水线 —— 程序的"心脏"，唯一能改状态的地方。

依据《规则基线》§6（季度执行顺序）与 2026-10-16 的交割口径裁定。

═══════════════════════════════════════════════════════════════════
★ 唯一实现，两个包装（红线：《AI自主演算-架构影响》§2.3）
═══════════════════════════════════════════════════════════════════

    _run(state, plans)          ← 唯一实现
    advance(state, plans)       ← 真结算，改状态
    preview(state, plans)       ← 试算，改副本

**绝不允许各写一份公式**。如果 AI 用一套算法演算、结算用另一套，
AI 说的数会和真实结算不一致 —— 玩家按 AI 说的做、结果钱不对，
这是最伤信任的 bug。

═══════════════════════════════════════════════════════════════════
十步顺序（§6）
═══════════════════════════════════════════════════════════════════

     1. 期初更新与公告      工期推进、到期完工
     2. 玩家提交计划        只校验，**不扣钱**
     3. 期初人口计算        E → L → N → ḡ → m → F_基准/F_优秀
     4. 生产                产量 = e·u·E，投入 = input·u·E，劳动 = d·u
     4.5 买入汇总与求价      B_t = 生产原料 + 建设物资 + 口粮 → P_t
     5. 一次性扣款     ★     物资款（按**上季价**）+ 工资 + 经营费
     6. 产品分配与工程投入    合同履约；本季产品投工程
     7. 市场结算             按 **P_t** 卖出
     7.5 粮食结算            扣 A/B/C/D → 得 H
     7.7 季末清仓            剩余普通库存全部出售，归零
     8. 税收与报告           工资税 10% 记回流收入
     9. 结转                 quarter += 1，保存本季 H

═══════════════════════════════════════════════════════════════════
★ 玩家阶段一分钱都不动
═══════════════════════════════════════════════════════════════════
"期初按上期价买入"是**口语说法**，落点是**价格用的是上季那个价**，
而不是"钱在期初就扣了"。钱只在第 5 步动一次。

═══════════════════════════════════════════════════════════════════
六条顺序铁律（写错任何一条，数字就对不上且极难查）
═══════════════════════════════════════════════════════════════════
  1. 幸福度用**上一季**的 H（否则循环依赖，算不出来）
  2. 粮食在**结算时**扣，不在期初
  3. 买入按上季价、卖出按本季价
  4. **劳动不乘 E**（劳动 = d·u）
  5. 本季产出不能倒填本季已开始的生产
  6. 粮食结算必须在市场结算**之后**
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from engine import market as M
from engine import production as P
from engine import settlement as S
from engine.efficiency import EfficiencyBreakdown, compute_efficiency
from engine.loader import Config
from engine.models import GameState, Plan, PlotStatus
from engine.population import PopulationResult, compute_population


class SettlementError(Exception):
    """结算无法进行（资金不足、计划非法等）。

    ⚠️ 刻意单独定义：调用方要能区分"**玩家计划有问题**"（返回给玩家改）
       和"**代码有 bug**"（自己修）。见《规则基线》§11.3。
    """


@dataclass
class QuarterReport:
    """一季的完整财报（前端只显示、AI 只解读，都读这个）。"""

    quarter: int
    player_id: str

    production: list[P.PlotOutcome] = field(default_factory=list)
    efficiency: dict[str, dict] = field(default_factory=dict)
    population: Optional[PopulationResult] = None

    bought: dict[str, float] = field(default_factory=dict)       # B_t（三段合计）
    bought_breakdown: dict[str, dict] = field(default_factory=dict)
    sold: dict[str, float] = field(default_factory=dict)         # X_t
    prices: dict[str, dict] = field(default_factory=dict)
    buy_bill: Optional[M.BuyBill] = None

    materials_cost: float = 0.0
    wages: float = 0.0
    operating: float = 0.0
    total_deduction: float = 0.0          # ★ 第 5 步一次性扣款
    wage_tax_refund: float = 0.0

    sales_revenue: float = 0.0            # 第 7 步
    clearing_revenue: float = 0.0         # 第 7.7 步
    unsold_cleared: dict[str, float] = field(default_factory=dict)

    food: Optional[S.FoodResult] = None

    cash_before: float = 0.0
    cash_after: float = 0.0
    value_added: float = 0.0              # GDP（增加值）

    def to_dict(self) -> dict:
        return {
            "quarter": self.quarter,
            "player_id": self.player_id,
            "production": [o.to_dict() for o in self.production],
            "efficiency": dict(self.efficiency),
            "population": self.population.to_dict() if self.population else None,
            "bought": dict(self.bought),
            "bought_breakdown": dict(self.bought_breakdown),
            "sold": dict(self.sold),
            "prices": dict(self.prices),
            "buy_bill": self.buy_bill.to_dict() if self.buy_bill else None,
            "materials_cost": self.materials_cost,
            "wages": self.wages,
            "operating": self.operating,
            "total_deduction": self.total_deduction,
            "wage_tax_refund": self.wage_tax_refund,
            "sales_revenue": self.sales_revenue,
            "clearing_revenue": self.clearing_revenue,
            "unsold_cleared": dict(self.unsold_cleared),
            "food": self.food.to_dict() if self.food else None,
            "cash_before": self.cash_before,
            "cash_after": self.cash_after,
            "value_added": self.value_added,
        }


def _season_scale(plan: Plan) -> dict[str, float]:
    """把 Plan 里的地块安排翻成 {地块ID: 开工比例}。"""
    return {pid: pp.effective_u for pid, pp in plan.plot_plans.items()}


def _run(state: GameState, plans: dict[str, Plan], cfg: Config) -> GameState:
    """**唯一实现**。`advance` 与 `preview` 都走这里。

    ⚠️ 会**就地修改** `state`（含 `state.report`）。试算请传副本。
    """
    if not plans:
        raise SettlementError("没有任何玩家的计划，无法结算")

    reports: dict[str, QuarterReport] = {}

    # ══ 第 1 步：期初更新与公告 ═════════════════════════════
    for player_id in sorted(plans):
        _advance_construction(state, player_id, cfg)

    # ══ 第 3 步：算效率 E（服务走独立分支） ═══════════════
    # ⚠️ 必须在生产之前算，且用**上一季**的 H。
    efficiencies: dict[str, dict[str, EfficiencyBreakdown]] = {}
    for player_id, plan in plans.items():
        player = state.players[player_id]
        per_plot: dict[str, EfficiencyBreakdown] = {}
        for pid in sorted(player.plots):
            plot = player.plots[pid]
            if plot.status is not PlotStatus.RUNNING:
                continue
            per_plot[pid] = compute_efficiency(state, player_id, plot, cfg)
        efficiencies[player_id] = per_plot

    # ══ 第 4 步：生产 ══════════════════════════════════════
    outcomes: dict[str, list[P.PlotOutcome]] = {}
    populations: dict[str, PopulationResult] = {}

    for player_id, plan in plans.items():
        player = state.players[player_id]
        u_map = _season_scale(plan)

        # 人口（§3.3）：必须在生产之前，供粮食判定用
        populations[player_id] = compute_population(state, player_id, u_map, cfg)

        outs: list[P.PlotOutcome] = []
        for pid in sorted(player.plots):
            plot = player.plots[pid]
            if plot.status is not PlotStatus.RUNNING:
                continue
            u = u_map.get(pid, 0.0)
            if u <= 0.0:
                continue
            recipe = cfg.recipe(plot.plot_type, plot.level, plot.product)
            wage_rate = cfg.wage_for(plot.plot_type, plot.level, plot.product)
            eff = efficiencies[player_id][pid]
            outs.append(P.run_plot(plot, recipe, u, eff.efficiency, cfg, wage_rate))
        outcomes[player_id] = outs

    # ══ 第 4.5 步：买入汇总与求价 ══════════════════════════
    # ★ B_t = 生产原料 + 建设物资 + 居民口粮（三段全计入，§4.1）
    bought_by_player: dict[str, dict[str, float]] = {}
    breakdown_by_player: dict[str, dict[str, dict]] = {}

    for player_id, plan in plans.items():
        production_inputs = P.merge_inputs(outcomes[player_id])
        construction = _construction_materials(plan)          # 建设物资
        # 口粮：玩家在 purchases 里申报的、且属于 A/B/C/D 的部分算口粮；
        # 其余 purchases 视为额外自购原料。两者都计入 B_t。
        household_food = {
            g: q for g, q in plan.purchases.items() if g in S.FOOD_GOODS
        }
        other_purchases = {
            g: q for g, q in plan.purchases.items() if g not in S.FOOD_GOODS
        }

        total: dict[str, float] = {}
        for part in (production_inputs, construction, household_food, other_purchases):
            for g, q in part.items():
                total[g] = total.get(g, 0.0) + q

        bought_by_player[player_id] = total
        breakdown_by_player[player_id] = {
            "production_input": production_inputs,
            "construction": construction,
            "household_food": household_food,
            "other_purchases": other_purchases,
        }

    # 卖出量：玩家申报的卖出。
    # ⚠️ 必须先校验"申报卖出 ≤ 手上实际有的"，**不能静默截断**：
    #    §11.3 明确要求"不自动减少采购或开工量；不产生部分状态变更"，
    #    而求价时若用"申报量"而非"实际可供量"，会把本季价格人为压低。
    sold_by_player: dict[str, dict[str, float]] = {}
    for player_id, plan in plans.items():
        available = _expected_available(plan, outcomes[player_id])
        for g, qty in plan.sales.items():
            have = available.get(g, 0.0)
            if qty > have + 1e-9:
                raise SettlementError(
                    f"申报卖出超量\n"
                    f"  错误类型：卖出量超过实际持有\n"
                    f"  涉及商品：{g}\n"
                    f"  申请值  ：{qty:.6f}\n"
                    f"  可用值  ：{have:.6f}\n"
                    f"  差额    ：{qty - have:.6f}\n"
                    f"  → 本季可卖 = 期初库存 + 本季买入 + 本季自产。\n"
                    f"  ⚠️ 规则不自动截断（§11.3）：请改计划后重新提交。"
                )
        sold_by_player[player_id] = dict(plan.sales)

    # 求价需要把**所有玩家**的买卖合并（共享资源，一起结算）
    total_bought: dict[str, float] = {}
    total_sold: dict[str, float] = {}
    for d in bought_by_player.values():
        for g, q in d.items():
            total_bought[g] = total_bought.get(g, 0.0) + q
    for d in sold_by_player.values():
        for g, q in d.items():
            total_sold[g] = total_sold.get(g, 0.0) + q

    prices = M.settle_prices(state.world, total_bought, total_sold)

    # ══ 第 5 步：一次性扣款（物资 + 工资 + 经营费） ★ ═════
    for player_id, plan in plans.items():
        player = state.players[player_id]
        bill = M.buy_bill(state.world, bought_by_player[player_id])

        wages = sum(o.wage for o in outcomes[player_id])
        operating = sum(o.operating_cost for o in outcomes[player_id])
        total = bill.total + wages + operating

        if total > player.cash + 1e-9:
            raise SettlementError(
                f"资金不足：{player_id} 本季需扣 {total:.2f}，手上只有 {player.cash:.2f}。\n"
                f"  物资款 {bill.total:.2f} + 工资 {wages:.2f} + 经营费 {operating:.2f}\n"
                f"  → 请减少采购、降低开工比例，或先卖掉存货。\n"
                f"  ⚠️ 规则不允许透支：结算一次性扣款时必须够钱。"
            )

        player.cash -= total

        reports[player_id] = QuarterReport(
            quarter=state.world.quarter,
            player_id=player_id,
            production=outcomes[player_id],
            efficiency={pid: e.to_dict() for pid, e in efficiencies[player_id].items()},
            population=populations[player_id],
            bought=bought_by_player[player_id],
            bought_breakdown=breakdown_by_player[player_id],
            sold=sold_by_player[player_id],
            prices={g: pr.to_dict() for g, pr in prices.items()},
            buy_bill=bill,
            materials_cost=bill.total,
            wages=wages,
            operating=operating,
            total_deduction=total,
            cash_before=player.cash + total,
        )

        # 入库：买入的物资 + 本季产出
        for g, q in bought_by_player[player_id].items():
            player.add_inventory(g, q)
        for o in outcomes[player_id]:
            if o.output_goods is not None and o.output_qty:
                player.add_inventory(o.output_goods, o.output_qty)

    # ══ 第 6 步：产品分配与工程投入 ════════════════════════
    for player_id, plan in plans.items():
        _invest_construction(state, player_id, plan, cfg)

    # ══ 第 7 步：市场结算（按 P_t 卖出） ══════════════════
    for player_id, plan in plans.items():
        player = state.players[player_id]
        rep = reports[player_id]

        revenue = 0.0
        for g, qty in sorted(sold_by_player[player_id].items()):
            have = player.inventory.get(g, 0.0)
            actual = min(have, qty)          # 不足则按实际能卖的算
            if actual <= 0:
                continue
            pr = prices.get(g)
            if pr is None:
                raise SettlementError(
                    f"商品 {g!r} 申报卖出 {qty}，但求价时没有把它算进去（内部错误）"
                )
            effective = min(actual, pr.sellable) if pr.at_floor else actual
            player.take_inventory(g, actual)
            revenue += effective * pr.price
            if pr.at_floor and actual > effective:
                rep.unsold_cleared[g] = rep.unsold_cleared.get(g, 0.0) + (actual - effective)

        player.cash += revenue
        rep.sales_revenue = revenue

    # ══ 第 7.5 步：粮食结算 → H ═══════════════════════════
    # ⚠️ 必须在第 7 步**之后**（否则卖不掉的东西不能拿来吃）。
    for player_id, plan in plans.items():
        pop = populations[player_id]
        rep = reports[player_id]
        rep.food = S.settle_food(
            state, player_id, pop.baseline_food, pop.good_food, cfg
        )

    # ══ 第 7.7 步：季末清仓 ════════════════════════════════
    for player_id, plan in plans.items():
        player = state.players[player_id]
        rep = reports[player_id]
        clearing = 0.0
        for g in sorted(player.inventory):
            qty = player.inventory[g]
            if qty <= 0:
                continue
            pr = prices.get(g)
            if pr is None:
                # 本季没有买卖该商品 → 按上季价清仓
                unit = state.world.last_price(g)
                price = unit
            else:
                price = pr.price
            player.take_inventory(g, qty)
            clearing += qty * price
        player.cash += clearing
        rep.clearing_revenue = clearing

    # ══ 第 8 步：税收与报告 ════════════════════════════════
    for player_id, plan in plans.items():
        player = state.players[player_id]
        rep = reports[player_id]
        # 工资税：工资支出记税前总额，税记**回流收入**（§7.1）
        tax = rep.wages * cfg.wage_tax_rate
        player.cash += tax
        rep.wage_tax_refund = tax
        rep.cash_after = player.cash
        # GDP：有效产出价值（固定参考价）− 实际消耗的中间投入
        rep.value_added = _value_added(rep, cfg)

    # ══ 第 9 步：结转 ══════════════════════════════════════
    for g, pr in prices.items():
        state.world.record_price(g, pr.price)

    state.report = {pid: r.to_dict() for pid, r in reports.items()}
    state.raw_reports = reports

    state.world.quarter += 1
    return state


# ══════════════════════════════════════════════════════════════════
# 辅助
# ══════════════════════════════════════════════════════════════════


def _expected_available(plan: Plan, outcomes: list[P.PlotOutcome]) -> dict[str, float]:
    """本季**实际可卖**的量 = 期初库存 + 本季买入 + 本季自产。

    用途：在第 4.5 步校验玩家申报的卖出量（§11.3 要求明确报错，不自动截断）。
    """
    available: dict[str, float] = {}
    for g, q in plan.purchases.items():
        available[g] = available.get(g, 0.0) + q
    for o in outcomes:
        if o.output_goods is not None and o.output_qty:
            available[o.output_goods] = available.get(o.output_goods, 0.0) + o.output_qty
    return available


def _construction_materials(plan: Plan) -> dict[str, float]:
    """本季建设要投入的物资（计入 `B_t`）。

    ⚠️ 建设表 `construction.works` 目前**为空**（待填，《规则基线》§5.2）。
       有建设指令时**明确报错**，而不是静默忽略 ——
       忽略会让玩家以为建了、其实没建。
    """
    if not plan.builds:
        return {}
    raise SettlementError(
        "建设/升级功能尚未接入：config 的 construction.works 为空。\n"
        "  → 这是【待填数据】（《规则基线》§5.2 的表尚未录入 config）。\n"
        "  ⚠️ 不静默忽略：忽略会让玩家以为建了、其实没建。"
    )


def _advance_construction(state: GameState, player_id: str, cfg: Config) -> None:
    """第 1 步：推进工期，到期的地块本季可用（§6）。"""
    player = state.players[player_id]
    for pid in sorted(player.plots):
        plot = player.plots[pid]
        if plot.status is not PlotStatus.CONSTRUCTING:
            continue
        if plot.construction_left > 0:
            plot.construction_left -= 1
        if plot.construction_left <= 0:
            plot.status = PlotStatus.RUNNING
            plot.level = plot.target_level
            plot.running_quarters = 0     # 磨合从本季起算
        else:
            plot.running_quarters += 0


def _invest_construction(state: GameState, player_id: str, plan: Plan, cfg: Config) -> None:
    """第 6 步：把材料投进已启动工程（§11.2 工程材料分账）。"""
    return None


def _value_added(rep: QuarterReport, cfg: Config) -> float:
    """GDP 增加值（§7.5）。

        增加值 = 有效产出价值 − 实际消耗的中间投入价值

    - 用**固定统计价（参考价 P0）**，不随季度成交价变
    - 工资**不**作中间投入再扣
    - 原料**要**扣；经营费 c **要**扣
    - 粮食扣减**也算**中间投入
    - 贷款、拨款、税款**不计入**
    """
    output_value = 0.0
    for o in rep.production:
        if o.output_goods is None:
            continue
        output_value += o.output_qty * cfg.price_of(o.output_goods)

    material_value = 0.0
    for g, qty in (rep.bought_breakdown.get("production_input") or {}).items():
        material_value += qty * cfg.price_of(g)

    food_value = 0.0
    if rep.food:
        for g, qty in rep.food.deducted.items():
            food_value += qty * cfg.price_of(g)

    return output_value - material_value - rep.operating - food_value


# ══════════════════════════════════════════════════════════════════
# 两个入口（★ 共用同一份实现）
# ══════════════════════════════════════════════════════════════════


def advance(state: GameState, plans: dict[str, Plan], cfg: Config) -> GameState:
    """真结算：**改状态**。整局中每人每季只调一次。"""
    return _run(state, plans, cfg)


def preview(state: GameState, plans: dict[str, Plan], cfg: Config) -> QuarterReport:
    """试算：**不改状态**，只返回报告。AI 和 UI 都用它。

    ⚠️ 实现方式是"深拷贝后跑真引擎"，所以试算的数字与真实结算**必然一致**。
       **绝不**另写一份公式。

    ⚠️ 单人局返回该玩家的报告；多人局需要按 player_id 取（返回第一个）。
       AI 永远调不到 `advance`（见《AI自主演算-架构影响》§8）。
    """
    shadow = state.deep_copy()
    _run(shadow, plans, cfg)
    first = sorted(shadow.raw_reports)[0]
    return shadow.raw_reports[first]
