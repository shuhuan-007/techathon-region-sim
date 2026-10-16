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
九步顺序（§6）
═══════════════════════════════════════════════════════════════════

     1. 期初更新与公告      工期推进、到期完工
     2. 玩家提交计划        只校验，**不扣钱**
     3. 期初人口计算        E → L → N → ḡ → m → F_基准/F_优秀
     4. 生产                产量 = e·u·E，投入 = input·u·E，劳动 = d·u
     4.5 买入汇总与求价      B_t = 生产原料 + 建设物资 + 口粮
                            X_t = 本季产出 − 合同交付
     5. 一次性扣款     ★     物资款（按**上季价**）+ 工资 + 经营费
     6. 产品分配与工程投入    合同履约；本季产品投工程
     7. 市场结算             **产出按 P_t 全部卖出**（不用申报）
     7.5 粮食结算            扣 A/B/C/D → 得 H
     8. 税收与报告           工资税 10% 记回流收入
     9. 结转                 quarter += 1，保存本季 H

═══════════════════════════════════════════════════════════════════
★ 没有仓库 → 产出就是供给（2026-10-16 用户拍板）
═══════════════════════════════════════════════════════════════════

用户原话："玩家不需要申报要卖，因为没有仓库，生产出来的就是'供'本身。"

由此定死三件事：

  ① **`X_t` = 本季全部产出**（减去合同交付），玩家不用申报卖多少
  ② **没有"季末清仓"这个动作** —— 没有库存可清，旧 §11.1 作废
  ③ **居民口粮只能来自买入**（`purchases` 里的 A/B/C/D）——
     产出已经全部是市场供给，不能拿来吃

唯一能让货不进公开市场的通道是**合同**（玩家间协议、低于市场价），
接口已留：见 `_contract_deliveries()`。

═══════════════════════════════════════════════════════════════════
★ 玩家阶段一分钱都不动
═══════════════════════════════════════════════════════════════════
"期初按上季价买入"是**口语说法**，落点是**价格用的是上季那个价**，
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

from engine import construction as C
from engine import finance as F
from engine import market as M
from engine import policies as POL
from engine import production as P
from engine import settlement as S
from engine.efficiency import EfficiencyBreakdown, compute_efficiency
from engine.loader import Config
from engine.models import GameState, Plan, Plot, PlotStatus
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
    sold: dict[str, float] = field(default_factory=dict)   # X_t：本季供给（= 产出 − 合同交付）
    contracted_out: dict[str, float] = field(default_factory=dict)  # 合同交付量（不进公开市场）
    prices: dict[str, dict] = field(default_factory=dict)
    buy_bill: Optional[M.BuyBill] = None

    materials_cost: float = 0.0
    wages: float = 0.0
    operating: float = 0.0
    # ★ 工程现金费（§5.1）。**不含物资** —— 物资在 `materials_cost` 里。
    #   两者相加才是工程表那一行的"按参考价合计"（§5.2）。
    construction_cash: float = 0.0
    construction: dict[str, float] = field(default_factory=dict)   # 本季投入的物资
    total_deduction: float = 0.0          # ★ 第 5 步一次性扣款
    wage_tax_refund: float = 0.0

    sales_revenue: float = 0.0            # 第 7 步
    clearing_revenue: float = 0.0         # 已废弃（没有仓库 → 没有季末清仓），恒为 0
    unsold_cleared: dict[str, float] = field(default_factory=dict)  # 触底卖不掉、被清理的量
    discarded: dict[str, float] = field(default_factory=dict)       # 买了但没吃掉的口粮

    food: Optional[S.FoodResult] = None

    # ★ 本季政策（§11.9.3）。`None` = 未启用/未抽。
    #   前端"弹窗告知政策"读这里，不用自己去翻 `world.policies`。
    policy: Optional[dict] = None

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
            "contracted_out": dict(self.contracted_out),
            "prices": dict(self.prices),
            "buy_bill": self.buy_bill.to_dict() if self.buy_bill else None,
            "materials_cost": self.materials_cost,
            "wages": self.wages,
            "operating": self.operating,
            "construction_cash": self.construction_cash,
            "construction": dict(self.construction),
            "total_deduction": self.total_deduction,
            "wage_tax_refund": self.wage_tax_refund,
            "sales_revenue": self.sales_revenue,
            "clearing_revenue": self.clearing_revenue,
            "unsold_cleared": dict(self.unsold_cleared),
            "discarded": dict(self.discarded),
            "food": self.food.to_dict() if self.food else None,
            "policy": dict(self.policy) if self.policy else None,
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

    ★ **失败即无痕**（§11.3「不产生部分状态变更」）：第 0 步**只抽不落盘**，
      抽到的政策临时挂在内存里；`policy_state` 到第 5 步扣款成功之后才写。
      任何一步抛错（资金不足、合同未实现……）→ 临时挂载被撤掉，
      `state.to_dict()` 与调用前**逐字节相同**。
    """
    if not plans:
        raise SettlementError("没有任何玩家的计划，无法结算")

    reports: dict[str, QuarterReport] = {}

    # ══ 第 0 步：★ 政策抽取（§11.9.3「每季开始」） ══════════
    #
    # 基线原文：**每季开始** —— 上季政策到期 → 随机抽取本季政策 →
    # 保存抽取结果 → 弹窗告知 → 玩家看 AI 建议后制定计划。
    #
    # ⚠️ 所以抽签在**第 1 步之前**（"期初更新与公告"里的"公告"）。
    # ⚠️ **第一季也抽**；**每季恰好一项**；**允许连续抽到相同政策**。
    # ⚠️ **幂等**：本季已经抽过就直接返回，**不重抽** ——
    #    "重新打开界面或读取同一季度存档不重新抽取"由它保证。
    #    于是 `preview()`（深拷贝后跑同一份 `_run`）只会重放**同一条**政策，
    #    试算与真结算的数字必然一致。
    #
    # ══════════════════════════════════════════════════════════════
    # ★ 2026-10-16 修复：抽签与落盘**分开**（§11.3「不产生部分状态变更」）
    # ══════════════════════════════════════════════════════════════
    #
    # 原先这里直接调 `POL.ensure_season_policy()`，**当场落盘**；而资金检查在
    # **第 5 步**。于是"资金不足"抛错时世界已经被改了：
    #
    #     world.policies      [] → [本季政策]
    #     world.policy_state  {} → {seed, mode, seq:[本季]}
    #
    # 违反基线 §11.3「扣款失败不应留下部分状态变更」。
    #
    # ⇒ 现在**第 0 步只抽、只放内存**；真正的提交（写 `policy_state`
    #   与 `world.policies`）挪到**第 5 步扣款成功之后**（见下）。
    #
    # ⚠️ 第 0 步仍然要先把政策放进 `world.policies`：本季的效率 / 工资 /
    #    经营费乘数都按它算（`_apply_efficiency_multiplier`、
    #    `POL.wage_multiplier` ...），而它们在**第 3、4 步**就要用到。
    #    这笔"临时挂上去"的记录在**任何失败路径上都撤掉** → 见 `_rollback()`。
    pending_policy, drawn_now = POL.draw_season_policy(state.world)
    if drawn_now and pending_policy is not None:
        # 临时挂载：**只挂 `world.policies`**，`policy_state` 一个字都不写。
        # ⚠️ 用 append 而非 insert：位置不影响 `active_policies()` 的判定，
        #    而 append 让"撤掉"就是一次 O(1) 的 pop（下面的 `_rollback`）。
        state.world.policies.append(pending_policy)

    try:
        return _run_body(state, plans, cfg, reports, pending_policy, drawn_now)
    except BaseException:
        _rollback(state, pending_policy, drawn_now)
        raise


def _run_body(
    state: GameState,
    plans: dict[str, Plan],
    cfg: Config,
    reports: dict[str, QuarterReport],
    pending_policy,
    drawn_now: bool,
) -> GameState:
    """`_run` 的**第 1 步到第 9 步**（第 0 步的政策抽签已由 `_run` 做完）。

    ⚠️ 从 `_run` 里拆出来**只为一件事**：让"第 1–9 步整段可能抛错"能被
       `_run` 的 try 整个罩住 —— 任何一步失败都要撤销第 0 步的临时挂载
       （§11.3「不产生部分状态变更」）。**公式与顺序逐字未动。**

    ⚠️ 就地修改 `state`；`reports` 是调用方给的**同一个** dict（被填满后
       `_run` 之外的调用方也能看到）。
    """
    # ══ 第 1 步：期初更新与公告 ═════════════════════════════
    for player_id in sorted(plans):
        _advance_construction(state, player_id, cfg)

    # ══ 第 3 步：算效率 E（服务走独立分支） ═══════════════
    # ⚠️ 必须在生产之前算，且用**上一季**的 H。
    #
    # ★ 政策效率乘数（§11.9.3「计算位置」）：
    #     E_base      = 原有效率公式的计算结果      ← compute_efficiency
    #     E_effective = E_base × 政策效率乘数       ← 下面这一行
    #   ⚠️ 乘数**只作用于商品生产地块**（研究 R / 本地服务 J 不受影响）；
    #   ⚠️ 乘数在**原有效率结果之后**应用，**不回写**邻接/幸福/磨合分量，
    #      更不碰邻接的 40% 上限（§11.9.3 明写）。
    #   ⚠️ 存进 `efficiencies` 的 **E_effective** ——
    #      `EfficiencyBreakdown.efficiency` 是四分量乘积（E_base），
    #      所以这里换成一个显式携带 E_effective 的副本，
    #      让报告里报的 E 与生产真正用的 E **是同一个数**。
    efficiencies: dict[str, dict[str, EfficiencyBreakdown]] = {}
    for player_id, plan in plans.items():
        player = state.players[player_id]
        per_plot: dict[str, EfficiencyBreakdown] = {}
        for pid in sorted(player.plots):
            plot = player.plots[pid]
            if plot.status is not PlotStatus.RUNNING:
                continue
            eff = compute_efficiency(state, player_id, plot, cfg)
            recipe = cfg.recipe(plot.plot_type, plot.level, plot.product)
            commodity = POL.is_commodity_plot(recipe.output, cfg)
            per_plot[pid] = _apply_efficiency_multiplier(
                eff, POL.efficiency_multiplier(state.world, commodity)
            )
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
            commodity = POL.is_commodity_plot(recipe.output, cfg)
            # ★ 政策工资 / 经营费乘数（§11.9.3「计算位置」）：
            #     wage_payment   = 原工资计算结果 × 政策工资乘数
            #     operating_cost = (原经营费公式用 E_effective 算出的结果)
            #                      × 政策经营费乘数
            #   ⚠️ `E_effective` 已经在上面的 `eff.efficiency` 里；
            #      `production.operating_cost()` 收的就是它，所以经营费
            #      的"用 E_effective 算"**自动成立**，这里只需再乘减免。
            #   ⚠️ **停工（u=0）的地块在上面就被 `continue` 掉了**，
            #      所以政策不可能给"本该不发工资"的地块凭空加出工资
            #      （§11.9.3 末条红线）。
            wage_mult = POL.wage_multiplier(state.world, commodity)
            op_mult = POL.operating_multiplier(state.world, commodity)
            outs.append(P.run_plot(
                plot, recipe, u, eff.efficiency, cfg, wage_rate,
                wage_multiplier=wage_mult, operating_multiplier=op_mult,
            ))
        outcomes[player_id] = outs

    # ══ 第 4.5 步：买入汇总与求价 ══════════════════════════
    # ★ B_t = 生产原料 + 建设物资 + 居民口粮（三段全计入，§4.1）
    bought_by_player: dict[str, dict[str, float]] = {}
    breakdown_by_player: dict[str, dict[str, dict]] = {}
    # 口粮的两个去向：自产顶上的（不进市场）+ 缺口买入的（进 B_t）
    food_covered_by_player: dict[str, dict[str, float]] = {}
    food_shortfall_by_player: dict[str, dict[str, float]] = {}

    for player_id, plan in plans.items():
        production_inputs = P.merge_inputs(outcomes[player_id])
        # ★ 建设物资：按工程表投入（计入 B_t，§5.1 工程现金口径）
        construction = _construction_materials(plan, state.players[player_id], cfg)

        # ══════════════════════════════════════════════════════════
        # ★ 居民口粮：自产先顶，缺口才买（2026-10-16 用户口径）
        # ══════════════════════════════════════════════════════════
        #
        # 用户原话："首先玩家会决定这期的粮食供给。然后缺了的话会在购入
        #           环节购入（此时仍然是上期价格，然后这期市场的需发生
        #           变化了呀），不然扣除"
        #
        #   ① 玩家申报 `plan.food`（要给居民吃多少）
        #   ② 自产先顶   = min(本季产出, 申报量)
        #   ③ 缺口       = 申报量 − 自产顶上      ← 这笔去市场买，计入 B_t
        #   ④ 自产顶上那部分**不进公开市场**      ← 从 X_t 扣掉（见下）
        produced = P.merge_outputs(outcomes[player_id])
        food_want = {g: q for g, q in plan.food.items() if q > 0}
        food_from_own: dict[str, float] = {}
        food_shortfall: dict[str, float] = {}
        for g, want in food_want.items():
            own = min(produced.get(g, 0.0), want)
            if own > 0:
                food_from_own[g] = own
            gap = want - own
            if gap > 1e-12:
                food_shortfall[g] = gap

        food_covered_by_player[player_id] = food_from_own
        food_shortfall_by_player[player_id] = food_shortfall

        total: dict[str, float] = {}
        for part in (production_inputs, construction, plan.purchases, food_shortfall):
            for g, q in part.items():
                total[g] = total.get(g, 0.0) + q

        bought_by_player[player_id] = total
        breakdown_by_player[player_id] = {
            "production_input": production_inputs,
            "construction": construction,
            "household_food_shortfall": food_shortfall,   # ← 口粮缺口（买入）
            "household_food_from_own": food_from_own,     # ← 自产顶上（不买入）
            "other_purchases": dict(plan.purchases),
        }

    # ══ 本季供给 X_t：**产出本身就是供** ═══════════════════
    #
    # ★ 用户拍板（2026-10-16）："玩家不需要申报要卖，因为没有仓库，
    #   生产出来的就是'供'本身。"
    #
    # 所以：
    #   X_t = 本季全部产出 − 合同交付量
    #
    # ⚠️ 没有"申报卖出"，也**没有"季末清仓"** —— 没有仓库就没有库存可清。
    #    上一轮为清仓求价做的那些推导（预估清仓量、并进 X_t）随之消失，
    #    因为清仓这个概念本身不存在了。
    #
    # ⚠️ 居民口粮**不来自产出**（产出已全部是供给），
    #    而来自玩家在 `purchases` 里申报买入的口粮，见第 7.5 步。
    sold_by_player: dict[str, dict[str, float]] = {}
    contracted_by_player: dict[str, dict[str, float]] = {}

    for player_id, plan in plans.items():
        produced = P.merge_outputs(outcomes[player_id])

        # 合同交付：不进公开市场（口子已留，第一期返回空）
        contracted = _contract_deliveries(plan)
        supply = dict(produced)
        # ★ 扣掉两样东西：合同交付 + 自产顶上居民口粮的那部分
        #   （口粮那部分已经"给居民了"，不再进公开市场）
        for source in (contracted, food_covered_by_player[player_id]):
            for g, q in source.items():
                left = supply.get(g, 0.0) - q
                if left > 1e-12:
                    supply[g] = left
                else:
                    supply.pop(g, None)

        sold_by_player[player_id] = supply
        contracted_by_player[player_id] = contracted

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

    # ══ 第 5 步：一次性扣款（物资 + 工程现金费 + 工资 + 经营费） ★ ═════
    #
    # ⚠️ **这一步是失败点**（资金不足 → `SettlementError`）。它抛错时由
    #    `_run` 的 try 兜住 —— 第 0 步临时挂上的政策会被撤销，
    #    世界状态与调用前**逐字节相同**（§11.3「不产生部分状态变更」）。
    _deduct_all_players(
        state, plans, cfg,
        outcomes=outcomes,
        bought_by_player=bought_by_player,
        breakdown_by_player=breakdown_by_player,
        sold_by_player=sold_by_player,
        contracted_by_player=contracted_by_player,
        efficiencies=efficiencies,
        populations=populations,
        prices=prices,
        reports=reports,
    )

    # ★ **扣款全部成功之后**才提交政策状态（§11.9.3「保存抽取结果」）：
    #   写 `world.policy_state`（seed/mode/seq）+ 确认政策留在 `world.policies`。
    #   ⚠️ 第 0 步只"临时挂"了 `world.policies`，**`policy_state` 到这一刻才写**。
    if drawn_now and pending_policy is not None:
        POL.commit_season_policy(state.world, pending_policy)

    # ══ 第 6 步：产品分配与工程投入 ════════════════════════
    for player_id, plan in plans.items():
        _invest_construction(state, player_id, plan, cfg)

    # ══ 第 7 步：市场结算（产出按 P_t 全部卖出） ══════════
    #
    # ★ 没有"申报卖出"：本季产出就是供给本身（用户 2026-10-16 拍板）。
    #   所以这一步只是**结算收入**，不再从库存里挑货去卖。
    for player_id, plan in plans.items():
        player = state.players[player_id]
        rep = reports[player_id]

        revenue = 0.0
        for g, qty in sorted(sold_by_player[player_id].items()):
            pr = prices.get(g)
            if pr is None:
                raise SettlementError(
                    f"商品 {g!r} 有产出 {qty}，但求价时没有把它算进去（内部错误）"
                )
            # ✅ 触底也全量成交（§4.1，2026-10-16 深夜用户裁定：
            #    "价格碰地板判定为全按地板价卖出就行了。就当是全国统一收购。"）
            #    价格碰地板只压低单价，**不减少销量**，所以这里不再截断。
            revenue += qty * pr.price

        player.cash += revenue
        rep.sales_revenue = revenue

    # ══ 第 7.5 步：粮食结算 → H ═══════════════════════════
    #
    # ★ 居民口粮来自玩家**本季申报买入**的 A/B/C/D（用户 2026-10-16：
    #   "都是需求，玩家处理，结算的时候买"）。
    #
    # ⚠️ 产出不能拿来吃 —— 产出已经全部是市场供给（没有仓库）。
    #    所以玩家要想让居民吃上饭，必须在 `purchases` 里报口粮。
    #
    # ⚠️ 必须在第 7 步**之后**（§6 铁律 6）：先卖货拿到钱，再谈吃饭。
    for player_id, plan in plans.items():
        pop = populations[player_id]
        rep = reports[player_id]
        rep.food = S.settle_food(
            state, player_id, pop.baseline_food, pop.good_food, cfg
        )

    # ══ 第 7.7 步：季末清仓 —— ★ 已取消（没有仓库） ════════
    #
    # 用户 2026-10-16："没有仓库，生产出来的就是'供'本身。"
    # 既然没有库存，就没有"清仓"这个动作。旧 §11.1 的季末清仓作废。
    #
    # ⚠️ 留下的唯一残留可能在"买了口粮但居民没吃完"的情况：
    #    那种情况下多余的口粮会被丢弃（不产生收入、也不结转）。
    #    第一期按此处理；若以后要让它变现，在合同通道里做更合理。
    for player_id, plan in plans.items():
        player = state.players[player_id]
        rep = reports[player_id]
        leftover = {g: q for g, q in player.inventory.items() if q > 1e-12}
        if leftover:
            rep.discarded = leftover
            for g in list(player.inventory):
                player.take_inventory(g, player.inventory[g])

    # ══ 第 8 步：税收与报告 ════════════════════════════════
    for player_id, plan in plans.items():
        player = state.players[player_id]
        rep = reports[player_id]
        # 工资税：工资支出记税前总额，税记**回流收入**（§7.1）
        #
        # ⚠️ 账面口径是**两笔**（扣全额 → 再回流），**不是**直接扣 90%。
        #    算式已抽到 `engine/finance.py`，口径说明见那个模块的头部。
        tax = F.wage_tax(rep.wages, cfg)
        player.cash += tax
        rep.wage_tax_refund = tax
        rep.cash_after = player.cash
        # GDP：有效产出价值（固定参考价）− 实际消耗的中间投入（§7.5）
        rep.value_added = F.value_added(rep, cfg)

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


def _policy_report(world) -> Optional[dict]:
    """本季政策的报告条目（前端弹窗读它，§11.9.3）。

    ⚠️ 加上**展开后的乘数**，前端就不用自己去查政策表 —— 与"前端只显示、
       零计算"的约定一致。
    """
    pid = POL.active_policy_id(world)
    if pid is None:
        return None
    d = POL.def_for(pid)
    row = d.to_dict()
    row["multipliers_applied"] = {
        "efficiency": POL.efficiency_multiplier(world, True),
        "wage_commodity": POL.wage_multiplier(world, True),
        "operating": POL.operating_multiplier(world, True),
    }
    return row


def _rollback(state: GameState, policy, drawn_now: bool) -> None:
    """★ 失败路径：撤掉第 0 步**临时挂上**的那条政策，让世界回到调用前。

    与第 5 步成功后的 `POL.commit_season_policy()` 互为反操作。
    两者一起保证基线 §11.3「**不产生部分状态变更**」——
    任何一步抛错（资金不足、合同未实现……）之后，
    `st.to_dict()` 与调用前**逐字节相同**。

    ⚠️ 只撤**本季新抽的**那条（`drawn_now=True`）：
       已经是存档里既有政策的季（读档 / 重开界面）压根没动过它，不能撤。

    ⚠️ `world.policy_state` 本来就没写（第 0 步走的是只读的
       `POL.draw_season_policy()`），所以这里**不需要**回滚它 ——
       这正是把 `rng_state()`（会就地补缺省值）换成 `_read_seed()` 的原因。

    ⚠️ **本函数只负责第 0 步的挂载**。第 1 步之后各步（工期推进、扣现金、
       进口粮……）的写入**不在这里回滚** —— 它们是**扣款前**的正常推进还是
       **扣款后**的遗留，取决于失败点，语义不同。
       ⚠️ **已知边界**：第 6/7 步之后若再抛错，`world.quarter` 之前的写入
       （比如第 1 步的工期推进）不会自动撤销。要根治得整局快照回滚，
       那是比本缺陷大得多的改动 —— 这里按任务书范围只堵第 0 步这一处。
    """
    if not drawn_now or policy is None:
        return
    try:
        state.world.policies.remove(policy)
    except ValueError:
        # 已经被撤过 / 从没挂上（防御性：绝不因清理动作本身再抛一个错）
        pass


def _deduct_all_players(
    state: GameState,
    plans: dict[str, Plan],
    cfg: Config,
    *,
    outcomes: dict[str, list[P.PlotOutcome]],
    bought_by_player: dict[str, dict[str, float]],
    breakdown_by_player: dict[str, dict[str, dict]],
    sold_by_player: dict[str, dict[str, float]],
    contracted_by_player: dict[str, dict[str, float]],
    efficiencies: dict[str, dict[str, EfficiencyBreakdown]],
    populations: dict[str, PopulationResult],
    prices: dict,
    reports: dict[str, QuarterReport],
) -> None:
    """★ 第 5 步：一次性扣款（物资 + 工程现金费 + 工资 + 经营费）。

    从 `_run` 里抽出来**只为一件事**：让"这一步可能抛错"在 `_run` 里
    看得见（`_run` 用 try 包住它，抛错时撤销临时挂载的政策）。
    公式、顺序、判钱与扣钱的口径**与抽取前逐字相同**。

    ⚠️ **就地修改** `state`（扣现金、登记减免名额、记报告、进口粮）。
    """
    for player_id, plan in plans.items():
        player = state.players[player_id]
        bill = M.buy_bill(state.world, bought_by_player[player_id])

        wages = sum(o.wage for o in outcomes[player_id])
        operating = sum(o.operating_cost for o in outcomes[player_id])
        # ★ 工程现金费：**不含物资**（物资在上面的 bill 里），§5.1 工程现金口径
        # ★ 2026-10-16 用户裁定（§5.3）：每类 3 个开局减免名额 × 0.50，
        #   只打「现金费」那一项，物资按原样投入。
        #   ⚠️ **判钱与扣钱必须是同一个数** —— 两者都用 `build_cash`，
        #      绝不允许一个用原价、一个用折后价。
        build_cash, quota_hits = _construction_cash(plan, player, cfg)
        total = bill.total + wages + operating + build_cash

        if total > player.cash + 1e-9:
            raise SettlementError(
                f"资金不足：{player_id} 本季需扣 {total:.2f}，手上只有 {player.cash:.2f}。\n"
                f"  物资款 {bill.total:.2f} + 工资 {wages:.2f} + "
                f"经营费 {operating:.2f} + 工程现金费 {build_cash:.2f}\n"
                f"  → 请减少采购、降低开工比例，或先卖掉存货。\n"
                f"  ⚠️ 规则不允许透支：结算一次性扣款时必须够钱。"
            )

        player.cash -= total

        # ★ 名额只在**扣款成功之后**才登记（§5.3 一次性、用掉就永久占用）。
        #   放在这里而不是 `_invest_construction()` 里，是因为第 6 步已经在
        #   扣款之后 —— 万一以后加了新的失败点，"钱没扣、名额却没了"不会发生。
        for ptype_value in quota_hits:
            player.opening_quota_used[ptype_value] = (
                int(player.opening_quota_used.get(ptype_value, 0)) + 1
            )

        reports[player_id] = QuarterReport(
            quarter=state.world.quarter,
            player_id=player_id,
            production=outcomes[player_id],
            efficiency={pid: e.to_dict() for pid, e in efficiencies[player_id].items()},
            population=populations[player_id],
            bought=bought_by_player[player_id],
            bought_breakdown=breakdown_by_player[player_id],
            sold=sold_by_player[player_id],
            contracted_out=contracted_by_player[player_id],
            prices={g: pr.to_dict() for g, pr in prices.items()},
            buy_bill=bill,
            materials_cost=bill.total,
            wages=wages,
            operating=operating,
            construction_cash=build_cash,
            construction=dict(breakdown_by_player[player_id].get("construction") or {}),
            total_deduction=total,
            cash_before=player.cash + total,
            policy=_policy_report(state.world),
        )

        # ══════════════════════════════════════════════════════════
        # ★ 产出**默认全部是供给**，不进库存（没有仓库）
        # ══════════════════════════════════════════════════════════
        # 买进来的东西分三类，**只有口粮进库存**：
        #   · 生产原料   → 第 4 步生产时**当场消耗**，不是库存
        #   · 建设物资   → 进工程项目，不是库存
        #   · 居民口粮   → 落到库存，等第 7.5 步被吃掉
        #
        # 口粮 = 自产顶上的 + 缺口买入的（两者合起来正好是玩家申报的 `food`）
        for g, q in (breakdown_by_player[player_id]["household_food_from_own"] or {}).items():
            player.add_inventory(g, q)
        for g, q in (breakdown_by_player[player_id]["household_food_shortfall"] or {}).items():
            player.add_inventory(g, q)


def _apply_efficiency_multiplier(
    eff: EfficiencyBreakdown, multiplier: float
) -> EfficiencyBreakdown:
    """把政策效率乘数应用在**原有效率结果之后**（§11.9.3「计算位置」）。

        E_base      = eff.efficiency（四分量乘积）
        E_effective = E_base × 政策效率乘数

    ⚠️ **不回写分量**：`adjacency / happiness / ramp_up / research_multiplier`
       原样保留 —— 基线明写"效率乘数在原有效率结果之后应用，**不回写邻接
       参数**"，且"政策**不改变**邻接分项的 40% 上限"。
       所以这里只换掉那个**派生属性** `efficiency`，
       而且必须新建对象（`frozen=False` 的 dataclass，但共享引用会污染调用方）。

    ⚠️ `multiplier == 1.0` 时**原样返回**，连对象都不换 ——
       常规环境下 M1 拿到的 `EfficiencyBreakdown` 与改动前**逐位相同**。
    """
    if multiplier == 1.0:
        return eff
    scaled = EfficiencyBreakdown(
        plot_id=eff.plot_id,
        adjacency=eff.adjacency,                 # ← 分量**不动**（不回写）
        happiness=eff.happiness,
        ramp_up=eff.ramp_up,
        research_multiplier=eff.research_multiplier,
        linked_neighbors=eff.linked_neighbors,
        adjacency_bonus=eff.adjacency_bonus,
        adjacency_capped=eff.adjacency_capped,
        is_service=eff.is_service,
        service_rate=eff.service_rate,
        contributions=list(eff.contributions),
    )
    # `efficiency` 是只读 property → 用子类覆盖，保持"报告里的 E =
    # 生产真正用的 E"这一条不变式（否则 UI 显示的 E 会与实际产量脱节）。
    scaled.__class__ = _ScaledEfficiency
    object.__setattr__(scaled, "_effective", eff.efficiency * multiplier)
    return scaled


class _ScaledEfficiency(EfficiencyBreakdown):
    """带政策乘数的 `EfficiencyBreakdown`（只覆盖 `efficiency` 这个派生量）。"""

    _effective: float = 1.0

    @property
    def efficiency(self) -> float:
        return self._effective


def _contract_deliveries(plan: Plan) -> dict[str, float]:
    """★ **合同通道**：本季通过合同直接交付给别的玩家、**不进公开市场**的量。

    用户 2026-10-16："要保留一个额外的通道，之后玩家间可以通过协议低于市场价
    提供，这个需要到时候能实现。"

    为什么需要这个口子
    ------------------
    没有仓库之后，**产出 100% 自动进公开市场**（`X_t` = 产出）。
    于是"我想把货便宜卖给隔壁老王"这件事**没有地方表达**。
    合同就是这个唯一的例外通道。

    对求价的影响（两条都要扣，否则合同会凭空扰动市场）
    --------------------------------------------------
        卖方：`X_t = 本季产出 − 合同交付量`     ← 货没进公开市场，不该压价
        买方：`B_t = 采购量 − 合同收货量`       ← 货不是从公开市场买的，不该抬价

    ⚠️ **第一期返回空 dict**（`builds` 为空时同理，不报错）。
       但主流程**已经按"扣掉合同量"的形状写好**：
       以后接合同只需要在这里解析 `plan.contracts`，**不动 `_run` 一行**。

    实现时要做的事（留给以后）
    --------------------------
      · `ContractAction` 已有 action / counterparty / goods / qty / price 字段
      · 需要一个"合同账本"（谁欠谁多少货），挂在 GameState 上
      · 交货要校验：卖方本季确实产得出这么多（否则违约，走 §7.3 流程）
      · 合同价由双方协商，**低于市场价**；结算时按合同价转账，不走 `P_t`
    """
    if not plan.contracts:
        return {}
    # 第一期：合同交付尚未实现。**明确报错**而不是静默忽略 ——
    # 静默忽略会让玩家以为货交出去了，其实还在自己账上。
    raise SettlementError(
        "合同交付功能尚未实现（第一期留口子）。\n"
        "  → 规则见《规则基线》§7.2；接口形状见 Plan.contracts。\n"
        "  ⚠️ 不静默忽略：忽略会让玩家以为货已交付，实际没有。"
    )


def _construction_cash(plan: Plan, player, cfg: Config) -> tuple[float, list[str]]:
    """本季要付的**工程现金费**（§5.1 工程现金口径）+ 吃掉的减免名额。

        工程现金支出 = 其他工程费用 + 实际外购工程物资成本

    ⚠️ **工程表里的现金费不含物资**（§5.2 表头写明），
       物资在 `_construction_materials()` 那边按商品单独买、单独算钱。
       两者加起来才是这一行工程的"按参考价合计"。

    ★ 2026-10-16 用户裁定（基线 §5.3）：**每类 3 个减免名额、折扣 0.50**，
      只作用于「新建一级」（`work.is_new` 且 `to_level == 1`），
      **只打现金费那一项**，物资按原样投入；名额按类型独立、跨季累计、
      用掉就永久占用（存在 `PlayerState.opening_quota_used`）。

    返回
    ----
    (实付现金总额, 本季**新吃掉**的名额类型列表——每次命中一个算一个，
     顺序 = `plan.builds` 的顺序，`["agriculture", "agriculture", ...]`）

    ⚠️ 判定只认 `plan.builds` 的顺序：同一季农业建 4 块 → 前 3 块折后价、
       第 4 块全价（**不是**按地块 ID 排序，玩家的指令顺序就是消耗顺序）。

    ⚠️ 本函数**不改任何状态**（`opening_quota_used` 由第 5 步扣款成功后登记），
       所以"钱不够 → 抛错"时不会留下"名额被偷走"的脏状态（§11.3）。
    """
    if not plan.builds:
        return 0.0, []

    rows = C.build_cash_plan(plan, player, cfg)
    total = sum(r["cash"] for r in rows)
    hits = [r["plot_type"].value for r in rows if r["discounted"]]
    return total, hits


def _construction_materials(plan: Plan, player, cfg: Config) -> dict[str, float]:
    """本季建设要投入的物资（计入 `B_t`，§5.1 工程现金口径）。

    ✅ 2026-10-16：`construction.works` 已按基线 §5.2 录入 14 行，本节不再是空桩。

    ⚠️ 物资**按工程表原样投入**，不随 `u` / `E` 缩放 ——
       工程是一次性投入，与"生产原料随开工率放大"不是一回事。

    ⚠️ 查不到工程 / 工业路线没指定 → 由 `construction` 抛 `ConfigError`，
       **不静默跳过**（跳过会让玩家以为建了、其实没建）。
    """
    if not plan.builds:
        return {}

    total: dict[str, float] = {}
    for order in plan.builds:
        plot = player.plots.get(order.plot_id)
        _work, materials = C.build_steps(cfg, plot, order)
        for g, qty in materials.items():
            total[g] = total.get(g, 0.0) + qty
    return total


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
    """第 6 步：把材料投进工程、**启动**工期（§11.2 工程材料分账）。

    ✅ 2026-10-16：不再是空桩。

    做两件事：
      ① 记账：把投入的物资记进 `plot.construction_materials`
         （材料一旦投入就**离开普通库存**，不会被当成存货）
      ② 启动：把地块切成 `CONSTRUCTING`，写入工期与目标等级

    ⚠️ **新建**（地上还没这块地）时在这里**创建** `Plot`，
       所以"玩家的地"是在下完建设指令那一季才出现的。
       地图上开局就是可建的空地（§5.3，不引入"零级地块"）。

    ⚠️ 钱的扣除在第 5 步已经做完（`买物资 + 工程现金费`），这里只管实物与状态。
    """
    if not plan.builds:
        return

    player = state.players[player_id]
    for order in plan.builds:
        existing = player.plots.get(order.plot_id)
        work, materials = C.build_steps(cfg, existing, order)

        if existing is None:
            # ── 新建：造出这块地 ──────────────────────────────
            plot = Plot(
                plot_id=order.plot_id,
                plot_type=order.plot_type,
                level=0,                      # 还没建成，完工时由 target_level 落地
                status=PlotStatus.CONSTRUCTING,
            )
            player.plots[order.plot_id] = plot
        else:
            plot = existing

        # ① 材料分账：投入的物资离开普通库存
        for g, qty in materials.items():
            plot.construction_materials[g] = plot.construction_materials.get(g, 0.0) + qty

        # ② 启动工期 + 定下产出/路线（建后不可更换，§5.1）
        C.start_work(plot, work, order.product)


def _value_added(rep: QuarterReport, cfg: Config) -> float:
    """GDP 增加值（§7.5）。

    ⚠️ **已迁移到 `engine/finance.py::value_added()`**（2026-10-16 第 4 步）。
       这个薄壳保留只是为了不打断任何外部调用；
       **新代码请直接调 `finance.value_added()`**。
       口径说明（什么算中间投入、工资为什么不扣）见 `finance.py` 模块头。
    """
    import engine.finance as F

    return F.value_added(rep, cfg)


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
