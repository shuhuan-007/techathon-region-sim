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
from engine.models import Contract, GameState, Plan, Plot, PlotStatus
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
    # ★ 2026-10-18：本季工程事件（开工/完工），供结算流水逐条显示。
    #   每条形如 {"event":"started"|"completed", "plot_id", "plot_type",
    #            "from_level", "to_level", "product"}。
    construction_events: list[dict] = field(default_factory=list)
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
    # ★ 2026-10-17 用户拍板：每季固定补贴（默认 120.0，可在 config.economy.quarterly_subsidy 调）。
    #   ⚠️ 这是**转移支付**，不是产值：只进玩家现金账面与 report.subsidy，
    #      **不进 value_added / sales_revenue / 市场求价 / GDP**。
    subsidy: float = 0.0

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
            "construction_events": [dict(e) for e in self.construction_events],
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
            # ★ 2026-10-17 用户拍板：只加字段，不改既有字段名/含义。
            #   新闻/财报的提示词输入契约 `QuarterReport.to_dict()` 的既有键一个不动。
            "subsidy": self.subsidy,
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
    # ★ 2026-10-18 修复（磨合计时）：季初已 RUNNING 的地块 = 本季有生产机会。
    #   ⚠️ 必须在第 1 步**之前**抓快照：第 1 步会把「上季完工、本季到期」的
    #      CONSTRUCTING 地块翻成 RUNNING，它们同样有本季生产机会（另由
    #      `completed_events` 补进来）。
    #   ⚠️ 第 6 步本季新建/刚完工的地块**不在**这个集合里 —— 它们本季
    #      生产窗口已过，下季首次生产时 `running_quarters` 才该是 0。
    running_at_quarter_start: dict[str, set[str]] = {
        player_id: {
            pid
            for pid, plot in state.players[player_id].plots.items()
            if plot.status is PlotStatus.RUNNING
        }
        for player_id in plans
    }

    # ══ 第 1 步：期初更新与公告 ═════════════════════════════
    # ★ 2026-10-18：收集“本季完工”事件，进报告给结算流水（工程情况）。
    completed_events: dict[str, list[dict]] = {}
    for player_id in sorted(plans):
        completed_events[player_id] = _advance_construction(state, player_id, cfg)

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
            # ★ 2026-10-18：走 `recipe_for` helper —— 研究地块（industry L2
            #   '研究'）重定向到 research.maintain（output=None），不影响
            #   机械/资源地块的 commodity 判定。
            recipe = cfg.recipe_for(plot)
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
            # ★ 2026-10-18：配方/工资**都走 helper** —— 研究地块用
            #   research.maintain（不产商品、c=60、d=2）+ high_tech 工资 28；
            #   机械/资源等其它地块走原 recipe()/wage_for()。
            recipe = cfg.recipe_for(plot)
            wage_rate = cfg.wage_for_plot(plot)
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

    # ══ 第 4.5 步前：★ 合同对账（§7.2，玩家对玩家、双方确认才成立） ══════
    #
    # 这一洞**集中一次做完**，不是逐人各做：
    #
    #   · 合同天然跨两个玩家，单边循环里"卖方还没见到买方的声明"就成立不了
    #     （对不上账就报错或者静默放过，都不对）；
    #   · 所以先把 `plan.contracts` 全部收齐，按 (卖家,买家,商品,qty,price,季度)
    #     做对账；**双方都提交了、且 商品/数量/单价/季度完全一致** 的合同才成立，
    #     写进 `state.contracts` 并在此算出两笔扣减：
    #
    #         contracted_out_by_player  卖方要从 X_t 扣掉的量（货没进公开市场）
    #         contracted_in_by_player   买方要从 B_t 扣掉的量（货不是从公开市场买的）
    #
    #   · **单人局天然不可达**：没有第二个玩家来提交对家，对账永远凑不齐一对，
    #     所以两个字典恒为空 → 求价、扣款、销售与无合同时**逐字节相同**。
    #
    # ⚠️ 合同价与交付现金**在这里不结** —— 钱要等第 5 步一次性扣款成功之后才转
    #   （合同现金是市场外的对方对价，不是市场买卖，**不走 P_t、不进公开价**），
    #   落到 `_contract_cash_transfers()`；收货落到买方 `inventory` 在第 6 步之前
    #   （`_deliver_contracted_goods()`）—— 因为收到的货**只能用于**：
    #     ① 本季**尚未执行的工程**（扣实物、不重复收采购费），或
    #     ② 本季销售（第 7 步按 P_t 卖出）
    #   **不能**喂给本季已完成的生产（§7.2 时间约束），也**不自动保留到下季**
    #   （没有仓库 → 用不完的当季丢弃，见第 7.7 步）。
    contracted_out_by_player: dict[str, dict[str, float]] = {}
    contracted_in_by_player: dict[str, dict[str, float]] = {}
    pending_contract_cash: list[tuple[str, str, float]] = []   # (买家, 卖家, 金额)
    pending_contract_goods: list[tuple[str, str, float]] = []  # (买家, 商品, 数量)
    # ★ **先给每个玩家建好空桶**，再进 `_settle_contracts` ——
    #   它在登记交付时是 `contracted_out_by_player[seller_id][goods] = ...` 的写法，
    #   桶不存在就会 `KeyError`（2026-10-16 深夜实测：两人局带合同一跑就抛
    #   `KeyError: 'p1'`）。**顺序不能反**：下面的 setdefault 只保证"跑完之后"有键，
    #   救不了 settle 内部的第一次下标写入。
    for player_id in plans:
        contracted_out_by_player.setdefault(player_id, {})
        contracted_in_by_player.setdefault(player_id, {})
    if any(p.contracts for p in plans.values()):
        _settle_contracts(
            state, plans, outcomes, cfg,
            contracted_out_by_player=contracted_out_by_player,
            contracted_in_by_player=contracted_in_by_player,
            pending_cash=pending_contract_cash,
            pending_goods=pending_contract_goods,
        )

    # ══ 第 4.5 步：买入汇总与求价 ══════════════════════════
    # ★ B_t = 生产原料 + 建设物资 + 居民口粮 + （合同已扣）合同外的市场采购（§4.1）
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

        # ★ 买方：合同已收货那部分**不是从公开市场买的** → 从 B_t 扣掉（§4.1）。
        #   不扣的话合同会凭空抬市价：货其实没在市场上成交，需求侧却被记了一笔。
        for g, q in contracted_in_by_player[player_id].items():
            left = total.get(g, 0.0) - q
            if left > 1e-12:
                total[g] = left
            else:
                total.pop(g, None)

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

        # 合同交付：不进公开市场（§7.2；双方对账一致才成立 → 已由 `_settle_contracts` 填进
        # `contracted_out_by_player` / `contract_in_by_player`，见第 4.5 步前的集中对账）
        contracted = contracted_out_by_player[player_id]
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

    # ══ 第 5.5 步：★ 合同现金转账（§7.2）══════════
    # ★ 放在**扣款成功之后** 与 第 0 步政策提交之后：
    #   · 买家此时已经付清了公开市场那一单，账上还剩多少是确定的；
    #   · 合同货款是**对方对价**，不是市场扣款，但同样要求买家**钱够** —
    #     这里**复用**第 5 步"资金不足即整单作废"的语义（§11.3：不产生部分状态变更）。
    #     换言之：合同转账本身也可能成为失败点 → 抛错时第 0 步挂载已撤，
    #     与"资金不足"同一条回滚路径。
    _settle_contract_cash(state, pending_contract_cash, reports)

    # ══ 第 5.6 步：★ 合同货交付（落到买方 inventory，§7.2）══════════
    # ★ 放在第 6 步工程投入**之前**：合同货**只能用于本季尚未执行的工程**（§7.2
    #   明写的两个出口之一），落到 `inventory` 后由 `_invest_construction` 扣实物
    #   （不再重复收一次市场采购费，§11.2/§5.1 口径）。
    #   ⚠️ §7.2 写的另一个出口"**或进入本季销售**"——我们没有把合同货**回补**到
    #   买方 X_t：4.5 步已经从 B_t 扣过它，如果第 7 步再把它加进公开市场供给
    #   就是同一批货**压价两次**（一次是卖方 X_t 扣掉、一次是买方 X_t 回补），
    #   会凭空扰动 P_t。所以这里留给买家的出路**只有"自用本季工程（扣实物）"**；
    #   未用掉的部分没有仓库可放 → 第 7.7 步统一当季丢弃（§7.2："用不完丢弃"）。
    #   ★ 这是"没有仓库 + 不重复扰动市场"两条基线同时约束下的落点，注释已标记。
    _deliver_contracted_goods(state, pending_contract_goods)

    # ★ 本季期初完工的工程事件（第 1 步就发生了；报告已建好，补挂上去）
    for player_id, events in completed_events.items():
        if events:
            reports[player_id].construction_events.extend(events)

    # ══ 第 5.9 步：★ 磨合推进（2026-10-18 修复）════════════
    #
    # 依据 §3.1 / `loader.EfficiencyConfig.ramp_factor`：
    #   第 1 季 0.5、第 2 季 0.75、第 3 季起 1.0。
    # 所以「本季按 0.5 生产过的这块地」到本季末应记 1 个已运行季度，
    # 下季生产时才会读到 0.75；再下季 1.0。
    #
    # ⚠️ 原先 `_advance_construction()` 的 else 分支写的是
    #    `plot.running_quarters += 0`，于是所有地块永远停在 0 → E_磨合
    #    永久 0.5，产量腰斩。M1 用 `running_quarters=99` 的成熟夹具，
    #    所以锚点测试一直没暴露它。
    #
    # ⚠️ 为什么落点在**第 5 步扣款成功之后、第 6 步工程之前**：
    #    · 生产已经在第 4 步用**本季的** E 算完，此时 +1 不会反过来改本季产量；
    #    · 第 5 步是资金不足的失败点（§11.3「失败即无痕」）——
    #      放在它之前会让"扣款失败"留下 rq+1 的脏状态（实测会挂
    #      `test_pipeline.test_insufficient_cash_raises`）；
    #    · 第 6 步才新建/完工的地块要**保持 0**，下季首次生产才是 0.5。
    #
    # ⚠️ 只给「本季有生产机会」的地块 +1：
    #    · 季初已 RUNNING 的（含 u=0 —— 地在、人就在，磨合按日历推进）；
    #    · 第 1 步刚完工转 RUNNING 的。
    for player_id in plans:
        had_production_window = set(running_at_quarter_start[player_id])
        had_production_window |= {
            e["plot_id"]
            for e in completed_events[player_id]
            if e.get("event") == "completed"
        }
        for pid in sorted(had_production_window):
            plot = state.players[player_id].plots.get(pid)
            if plot is not None and plot.status is PlotStatus.RUNNING:
                plot.running_quarters += 1

    # ══ 第 6 步：产品分配与工程投入 ════════════════════════
    for player_id, plan in plans.items():
        started = _invest_construction(state, player_id, plan, cfg)
        if started:
            reports[player_id].construction_events.extend(started)

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
        # GDP：有效产出价值（固定参考价）− 实际消耗的中间投入（§7.5）
        rep.value_added = F.value_added(rep, cfg)

    # ══ 第 8.5 步：每季固定补贴 ★ 2026-10-17 用户拍板 / 2026-10-18 E1 早期补贴 ══
    #
    #   每个玩家每季 +cfg.subsidy_for_quarter(state.world.quarter) 现金：
    #     quarter ≤ cfg.early_subsidy_quarters → cfg.early_quarterly_subsidy（默认 1200）
    #     quarter >  cfg.early_subsidy_quarters → cfg.quarterly_subsidy（默认 300）
    #
    # ⚠️ 落点：第 8 步税收与 GDP 算完之后、cash_after 落账之前 ——
    #   即"扣款/结算之后、写报告之前"（用户口径），让 subsidy 体现在
    #   state.report 的 cash_after 里，也能被新闻/财报读到。
    #
    # ⚠️ 补贴是**转移支付**，不是产值：
    #     · 不改 value_added（GDP，在上一行已经算完，补贴不参与）
    #     · 不改 sales_revenue（销售收入，第 7 步已写完）
    #     · 不进市场求价、不改价格、不改效率、不改任何已算好的中间量
    #   它的唯一作用是给现金账面注入一笔钱，并在 report.subsidy 字段如实记录。
    #
    # ⚠️ 公式放在 `Config.subsidy_for_quarter(quarter)` 纯函数里：
    #   pipeline 与 test_m1 共用同一条 —— 否则改了代码断言还在拼 quarterly_subsidy。
    for player_id, plan in plans.items():
        player = state.players[player_id]
        rep = reports[player_id]
        subsidy = float(cfg.subsidy_for_quarter(state.world.quarter))
        player.cash += subsidy
        rep.subsidy = subsidy
        # ★ cash_after 是"季末玩家手上实际现金"，必须含补贴。
        rep.cash_after = player.cash

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


def _settle_contracts(
    state: GameState,
    plans: dict[str, Plan],
    outcomes: dict[str, list[P.PlotOutcome]],
    cfg: Config,
    *,
    contracted_out_by_player: dict[str, dict[str, float]],
    contracted_in_by_player: dict[str, dict[str, float]],
    pending_cash: list[tuple[str, str, float]],
    pending_goods: list[tuple[str, str, float]],
) -> None:
    """★ 第 4.5 步前：合同对账 + 账本落到 `state.contracts`（§7.2 玩家对玩家）。

    用户 2026-10-16 裁定（A 方案）：**双方各自提交 `ContractAction`，商品/数量/单价/
    季度完全一致才成立**。本函数做的事就是把双方声明凑成一对、在同一个原子步骤里：

      ① **对账**：以卖家视角把 (seller, buyer, goods, qty, price, quarter) 归一化后
         逐个查买家是否有镜像记录（`side` 相反、四项一致）。**凑不齐一对 → 不成立**，
         不写账本、不扣任何货/钱。
      ② **落账**：成立的合同新建一条 `Contract` 进 `state.contracts`，
         并当场把"**本季能真交的**"记成 `delivered`（产出封顶值），
         货价记成 `settled_cash`；超额部分留在 `Contract.shortfall`（§7.3 记录位）。
      ③ **按实际产出封顶交付**（§7.2 时间约束 + §11.3 不允许部分执行 → **只做记录，
         不改产量**）：
           卖方本季产出 `produced[g]` 是上限；超出部分**不交付**、**不重复扣货**，
           差额留在 `Contract.shortfall`（即 `qty − delivered`）——
           这是 §7.3「未履约义务」的记录位。**赔偿/信用扣分尚未实现**，
           缺口已在注释里写明（见 `Contract` docstring 与 `_deliver_contracted_goods`）。
      ④ **填写两份扣减表**（调用方已先 `setdefault` 空字典）：
           `contracted_out_by_player[seller][goods] += 实际交付量`（从卖方 X_t 扣）
           `contracted_in_by_player[buyer][goods]   += 实际收货量`（从买方 B_t 扣）
         以及两条"待后处理"清单：
           `pending_cash`  — 合同货价（= 单价 × 实际交付量），第 5 步后转账
           `pending_goods` — 实际交付的商品量，第 5.6 步落到买方 inventory

    ⚠️ 本函数**就地修改** `state.contracts`（写账本）与 `contracted_*_by_player`
       （写扣减表）。这两个集合都是本季新建的，不与 `state` 既有数据共享引用。
       ★ 任何一条合同交不出（产出不够）**不会**在这里抛错——§7.3「记录未履约」
       是**记账动作**而不是失败；整季作废的语义只保留给"资金不足"这类 §11.3
       真正的失败点（见第 5 步）。
    """
    # ── ① 把双方的 ContractAction 都化成"卖家视角"的标准元组 ──
    #   key = (seller_id, buyer_id, goods, qty, price, quarter)
    #   side="sell" 表示发这条的就是卖家；side="buy" 表示对方是卖家、自己是买家。
    declared: dict[tuple, list[str]] = {}   # key → [提交方 player_id, ...]
    for player_id, plan in plans.items():
        for act in plan.contracts:
            if act.side == "sell":
                key = (player_id, act.counterparty, act.goods, act.qty, act.price, act.quarter)
            else:
                key = (act.counterparty, player_id, act.goods, act.qty, act.price, act.quarter)
            declared.setdefault(key, []).append(player_id)

    # ── ② 逐个凑对：恰好双方各提交了一次才算成立 ──
    #   （一方重复提交两条相同声明 → 视为一次；双方若同时漏一项 → 不成立。）
    for key in sorted(declared):
        seller_id, buyer_id, goods, qty, price, quarter = key
        parties = set(declared[key])

        # 卖家与买家必须是**两个不同的**玩家，且这两人都声明了同一条
        if seller_id == buyer_id:
            continue   # 自合同：validate 已拦；防御性再跳一次，不报错
        if parties != {seller_id, buyer_id}:
            # 单方提交（对家没跟上，或对手条目对不上账）。
            #
            # ⚠️ **单人局**里这条是"必然路径"（A 方案下合同**机制上不可达**：
            #    永远不会有第二个玩家去提交镜像）。所以**单人局不报错**——
            #    那不是"计划有问题"，是"机制上签不成"；走报表透明、账本一字不留。
            # ⚠️ **多人局**里这条按**整季报错**处理（§11.3「不产生部分状态变更」）。
            #    **待裁定**（2026-10-16）：选"报错"而非"挂起"。理由：容忍挂起
            #    等于让脏声明随存档滚进下一季继续扰动对账；正确修法是**让对家在
            #    他自己的 Plan 里补一条镜像**（validate 已各自过形状关），而不是
            #    让系统容忍一份半成品跑进结算。以后要改（比如引入"挂起等待一整个
            #    季度"），改**这里**不动 validate。
            if len(plans) == 1:
                continue   # 单人局：合同机制上不可达，不落账、不扣钱/货
            missing = sorted({seller_id, buyer_id} - parties) or ["对家镜像声明"]
            raise SettlementError(
                f"合同对账不齐（§7.2 A 方案）：卖家 {seller_id} / 买家 {buyer_id} / "
                f"商品 {goods} / 数量 {qty:g} / 单价 {price:g} / 第 {quarter} 季，\n"
                f"  目前只收到 {sorted(parties)} 一方的提交；缺 {missing} 的镜像声明\n"
                f"  （商品/数量/单价/季度完全一致，`side` 取反）。\n"
                f"  ⚠️ A 方案的合同**双方提交才生效**；任意一方缺位即不成立、不交付、不转账。\n"
                f"  ⚠️ 待裁定（2026-10-16）：多人局单方提交按整季报错；单人局是机制上"
                f"签不成合同（A 方案不可达，非计划错）→ 放行、不落账。"
            )

        # 季度必须落在本季（§7.2：本季生产后的交货，不能支持接收方本季已完的生产；
        #   更早的季度已经过账作废，更晚的季度本季还没到——都当作"不成立"，
        #   不跨季结转。**没有仓库**这条贯穿全线。）
        if quarter != state.world.quarter:
            continue

        # ── ③ 产出封顶交付（§7.2 时间约束 + §11.3 不允许部分执行）──
        produced = P.merge_outputs(outcomes.get(seller_id, []))
        available = produced.get(goods, 0.0)
        delivered = min(qty, available)
        if delivered <= 1e-12:
            # 完全交不出来 → 合同成立但本季零交付；差额整笔进 shortfall
            # （§7.3 只记录）。不要在账上留"已交货 0"的噪声。
            fulfilled = False
            settled_cash = 0.0
        else:
            fulfilled = delivered >= qty - 1e-12
            settled_cash = delivered * price

        # ── ④ 落账 ──
        cid = f"{seller_id}>{buyer_id}>{goods}>q{quarter}"
        state.contracts[cid] = Contract(
            contract_id=cid,
            seller_id=seller_id,
            buyer_id=buyer_id,
            goods=goods,
            qty=qty,
            price=price,
            quarter=quarter,
            delivered=delivered,
            settled_cash=settled_cash,
            fulfilled=fulfilled,
        )

        # ── ⑤ 登记两份扣减 + 两条后处理清单 ── 交付量为 0 时**不登记**，
        #   让"完全交不出"的合同在市场上完全透明（不扰动 X_t/B_t）。
        if delivered > 1e-12:
            contracted_out_by_player[seller_id][goods] = (
                contracted_out_by_player[seller_id].get(goods, 0.0) + delivered
            )
            contracted_in_by_player[buyer_id][goods] = (
                contracted_in_by_player[buyer_id].get(goods, 0.0) + delivered
            )
            pending_cash.append((buyer_id, seller_id, settled_cash))
            pending_goods.append((buyer_id, goods, delivered))


def _settle_contract_cash(
    state: GameState,
    pending_cash: list[tuple[str, str, float]],
    reports: dict[str, QuarterReport],
) -> None:
    """★ 第 5.5 步：合同货款逐笔转账（§7.2；**不走 P_t、不进公开价**）。

    放在**扣款成功之后**：买家此时已经付清了公开市场那一单，账上还剩多少是
    确定的。合同货款是**对方对价**（买家付、卖家收），**不是**市场扣款、
    也不是市场求价的一部分——所以不动 `world.price_history`、不进 `P_t`。

    ⚠️ 同样要求买家**钱够**：钱不够时**整笔合同货款都不转**（§11.3 不允许部分
       执行）。抛错会让本季回到"调用前逐字节相同"（第 0 步挂载已撤，
       合同账本还没建，见 `_settle_contracts` 的失败边界）。
    """
    for buyer_id, seller_id, amount in pending_cash:
        buyer = state.players[buyer_id]
        seller = state.players[seller_id]
        if amount > buyer.cash + 1e-9:
            raise SettlementError(
                f"合同货款不足：{buyer_id} 应付 {amount:.2f} 给 {seller_id}，"
                f"手上只有 {buyer.cash:.2f}。\n"
                f"  ⚠️ 规则不允许透支：合同货款与市场扣款同时在本季结清，"
                f"合并后现金必须非负。"
            )
        buyer.cash -= amount
        seller.cash += amount
        # 财务报告上各记一笔（让前端能看到合同流水），不重复改总价
        if buyer_id in reports:
            reports[buyer_id].total_deduction += amount
        if seller_id in reports:
            reports[seller_id].sales_revenue += amount


def _deliver_contracted_goods(
    state: GameState,
    pending_goods: list[tuple[str, str, float]],
) -> None:
    """★ 第 5.6 步：合同货落到买方 `inventory`（§7.2）。

    出口只有一个：**本季尚未执行的工程**（`_invest_construction` 接着从这里
    扣实物、不再重复收采购费）。**不喂本季已完成的生产**（§7.2 时间约束），
    **不补回公开市场**（4.5 步已从 B_t 扣过，再加回 X_t 就是同一批货压价两次），
    **用不完当季丢弃**（没有仓库 → 第 7.7 步统一清理）。
    """
    for buyer_id, goods, qty in pending_goods:
        state.players[buyer_id].add_inventory(goods, qty)


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


def _advance_construction(state: GameState, player_id: str, cfg: Config) -> list[dict]:
    """第 1 步：推进工期，到期的地块本季可用（§6）。

    返回本季**完工**事件列表（给报告/结算流水用）：
        {"event":"completed","plot_id","plot_type","from_level","to_level","product"}
    """
    player = state.players[player_id]
    events: list[dict] = []
    for pid in sorted(player.plots):
        plot = player.plots[pid]
        if plot.status is not PlotStatus.CONSTRUCTING:
            continue
        from_level = plot.level
        if plot.construction_left > 0:
            plot.construction_left -= 1
        if plot.construction_left <= 0:
            plot.status = PlotStatus.RUNNING
            plot.level = plot.target_level
            plot.running_quarters = 0     # 磨合从本季起算
            events.append({
                "event": "completed",
                "plot_id": pid,
                "plot_type": plot.plot_type.value,
                "from_level": from_level,
                "to_level": plot.level,
                "product": plot.product,
            })
        else:
            plot.running_quarters += 0
    return events


def _invest_construction(state: GameState, player_id: str, plan: Plan, cfg: Config) -> list[dict]:
    """第 6 步：把材料投进工程、**启动**工期（§11.2 工程材料分账）。

    ✅ 2026-10-16：不再是空桩。

    做两件事：
      ① 记账：把投入的物资记进 `plot.construction_materials`
         （材料一旦投入就**离开普通库存**，不会被当成存货）
      ② 启动：把地块切成 `CONSTRUCTING`，写入工期与目标等级

    ★ 2026-10-18：返回本季**工程事件**（给结算流水显示“工程情况”）：
        {"event":"started", "plot_id","plot_type","from_level","to_level","product"}
      若这条工程当季就完工（1 季工程），再追加一条 "completed"。

    ⚠️ **新建**（地上还没这块地）时在这里**创建** `Plot`，
       所以"玩家的地"是在下完建设指令那一季才出现的。
       地图上开局就是可建的空地（§5.3，不引入"零级地块"）。

    ⚠️ 钱的扣除在第 5 步已经做完（`买物资 + 工程现金费`），这里只管实物与状态。
    """
    if not plan.builds:
        return []

    events: list[dict] = []
    player = state.players[player_id]
    for order in plan.builds:
        existing = player.plots.get(order.plot_id)
        work, materials = C.build_steps(cfg, existing, order)
        from_level = 0 if existing is None else existing.level

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
        events.append({
            "event": "started",
            "plot_id": order.plot_id,
            "plot_type": order.plot_type.value,
            "from_level": from_level,
            "to_level": work.to_level,
            "product": plot.product,
        })

        # ══════════════════════════════════════════════════════════
        # ★ 2026-10-17 用户拍板：本季投钱投料也算一季工期
        # ══════════════════════════════════════════════════════════
        #
        # 以前：第 1 步 `_advance_construction` 本季已跑过（那时地块还不存在），
        #   工期要等到下季才第一次递减 →「1 季工程」要等两季才完工、第 3 季才能生产。
        #
        # 现在：第 6 步一旦 start_work 立即扣 1 季工期，归零则当场切 RUNNING：
        #
        #     1 季工程  →  当季 CONSTRUCTING→RUNNING（本季完工见报，下季可开工生产）
        #     2 季工程  →  当季 left=1（下季第 1 步递减→0、完工、当季即可生产）
        #
        # ⚠️ 本函数是第 6 步、生产在第 4 步：当季完工的地块本季不会有产出
        #   （生产窗口已过），这是正确口径，与任务书红线一致。
        #
        # ⚠️ 它覆盖掉旁边那段 2026-10-17 的"已回滚"备注 ——
        #   那段是基线口径下的保护，现在用户推翻基线把它改为新口径，
        #   就**保留 start_work 之后立即扣 1 季**这条路径（本段注释写清缘由）。
        if plot.construction_left > 0:
            plot.construction_left -= 1
        if plot.construction_left <= 0:
            plot.status = PlotStatus.RUNNING
            plot.level = plot.target_level
            plot.running_quarters = 0     # 磨合从完工本季起算
            events.append({
                "event": "completed",
                "plot_id": order.plot_id,
                "plot_type": order.plot_type.value,
                "from_level": from_level,
                "to_level": plot.level,
                "product": plot.product,
            })
    return events


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
