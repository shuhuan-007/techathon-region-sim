"""经济体检实验台 —— 用真引擎跑多季账，不写配置、不写存档。

跑法（在 region-sim/ 下）：
    python tools/balance_lab.py

它回答三个问题：
  1. 按当前规则（修好磨合计时之后）从 1200 / 零地块开局，三种策略各跑到
     20 季，现金是正还是负？哪一季最紧？
  2. 钱花在哪（工资 / 经营费 / 生产材料 / 建设物资 / 建造现金 / 口粮），
     钱从哪来（每个商品 × 本季 P_t）？
  3. 若只改数字（P0 全线 ×1.1 / 工资 ×0.8 / 补贴 120→200），
     20 季现金会变成多少？M1 锚点（23.52 / 55.222222 / 1031.68 /
     1065.611111 / 合计 176）会不会被破坏？

⚠️ 本脚本**只读**：
    · 用 `copy.deepcopy(cfg)` 在内存里试参数，绝不写回 config.json；
    · 不落存档；
    · 预算不够时按 u 1→0.75→0.5→0.25→0、再丢建造的顺序收缩，不硬闯。
"""
from __future__ import annotations

import copy
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent          # region-sim/
sys.path.insert(0, str(ROOT))

from engine import pipeline, validate                  # noqa: E402
from engine.loader import Config, load_config          # noqa: E402
from engine.models import (                            # noqa: E402
    BuildOrder,
    GameState,
    Plan,
    PlayerState,
    Plot,
    PlotPlan,
    PlotStatus,
    PlotType,
    WorldPhase,
    neighbors,
)

PLAYER_ID = "p1"
START_CASH = 1200.0
TOTAL_QUARTERS = 20
TABLE_QUARTERS = 10

#: 现金收缩阶梯（与任务书一致：先降 u，再丢建造）
U_LADDER = (1.0, 0.75, 0.5, 0.25, 0.0)


# ══════════════════════════════════════════════════════════════════
# 一、策略定义
# ══════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class Strategy:
    """一种经营策略。"""

    key: str
    label: str
    build_threshold: float
    plot_type: PlotType
    product: Optional[str]
    #: 第一块地无条件建（工业线要求"先建 1 块 G"；农业线 1200 > 阈值本来就会建）
    force_first: bool = False


STRATEGIES: tuple[Strategy, ...] = (
    Strategy("all_run", "全开 + 现金>500 建 1 块农业 A", 500.0,
             PlotType.AGRICULTURE, "A"),
    Strategy("farm_expand", "农业优先 + 现金>300 就建", 300.0,
             PlotType.AGRICULTURE, "A"),
    Strategy("industry", "先建 1 块工业 G，再逐步扩", 500.0,
             PlotType.INDUSTRY, "G", force_first=True),
)


# ══════════════════════════════════════════════════════════════════
# 二、造局 / 计划 / 预算收缩
# ══════════════════════════════════════════════════════════════════


def new_state(cfg: Config) -> GameState:
    """开局：1200 现金、零地块、20 季、常规环境（无政策随机噪声）。"""
    world = cfg.new_world()                 # 默认 regular（M1 同款）
    world.total_quarters = TOTAL_QUARTERS
    player = cfg.new_player(PLAYER_ID)      # 读 config.world.opening_cash = 1200
    return GameState(world=world, players={PLAYER_ID: player})


def projected_baseline_food(state: GameState, cfg: Config) -> float:
    """本季粮食结算会看到的 `F_基准`。

    与 `population.compute_population()` 同口径，但必须把「第 1 步会完工」
    的地块也算进来（`construction_left <= 1`）：它们在**生产之前**就转
    RUNNING，本季要吃饭；只按当前 RUNNING 算会漏掉它们。
    """
    player = state.players[PLAYER_ID]
    labor_food = 0.0
    levels: list[int] = []

    for plot in player.plots.values():
        running = plot.status is PlotStatus.RUNNING
        completing = (
            plot.status is PlotStatus.CONSTRUCTING
            and plot.construction_left <= 1
        )
        if not (running or completing):
            continue

        level = plot.level if running else plot.target_level
        recipe = cfg.recipe(plot.plot_type, level, plot.product)
        levels.append(level)
        if plot.plot_type is not PlotType.SERVICE:
            labor_food += recipe.d

    population = labor_food * cfg.population.r
    avg_level = (sum(levels) / len(levels)) if levels else 1.0
    return cfg.population.baseline_food(population, avg_level)


def baseline_food_order(f_baseline: float, cfg: Config) -> dict[str, float]:
    """基准档口粮：A = 0.4F、B = 0.2F（§3.3.3）。"""
    if f_baseline <= 0:
        return {}
    return {
        g: ratio * f_baseline
        for g, ratio in cfg.food.baseline_quota.items()
    }


def pick_build_spot(state: GameState, cfg: Config, plot_type: PlotType) -> Optional[str]:
    """挑一块空地：优先已有「能给自己加成」的邻居（贴地建）。

    邻接表来自 `cfg.efficiency.adjacency_table`：
      农业 ← 农业 / 资源；工业 ← 工业 / 资源。
    评分：关联邻居 ×2 + 任意已有地块 ×0.1；同分按行优先。
    """
    player = state.players[PLAYER_ID]
    linked = set(cfg.efficiency.adjacency_table.get(plot_type, set()))

    best_id: Optional[str] = None
    best_score = -1.0
    for row in range(cfg.rows):
        for col in range(cfg.cols):
            plot_id = f"{row},{col}"
            if plot_id in player.plots:
                continue
            score = 0.0
            for nid in neighbors(plot_id, cfg.rows, cfg.cols):
                other = player.plots.get(nid)
                if other is None:
                    continue
                score += 2.0 if other.plot_type in linked else 0.1
            if score > best_score:
                best_score = score
                best_id = plot_id
    return best_id


def make_plan(
    u_ids: list[str],
    food: dict[str, float],
    builds: list[BuildOrder],
    u: float,
    *,
    drop_builds: bool = False,
    drop_food: bool = False,
) -> Plan:
    return Plan(
        player_id=PLAYER_ID,
        plot_plans={pid: PlotPlan(plot_id=pid, u=u) for pid in u_ids},
        food={} if drop_food else dict(food),
        builds=[] if drop_builds else list(builds),
    )


@dataclass
class AttemptMeta:
    u: float = 1.0
    builds_dropped: bool = False
    food_cut: bool = False
    failures: list[str] = field(default_factory=list)


def settle_quarter(
    state: GameState,
    cfg: Config,
    u_ids: list[str],
    food: dict[str, float],
    builds: list[BuildOrder],
) -> tuple[GameState, object, AttemptMeta]:
    """试算 → 通过才真结算；资金不足按 u 阶梯收缩，再丢建造。

    返回 (新 state, QuarterReport, 本季收缩记录)。
    """
    player = state.players[PLAYER_ID]
    meta = AttemptMeta()

    # 先带建造走 u 阶梯；再丢建造走 u 阶梯。顺序与任务书一致。
    for drop_builds in (False, True):
        if drop_builds and not builds:
            continue
        for u in U_LADDER:
            plan = make_plan(u_ids, food, builds, u, drop_builds=drop_builds)
            issues = validate.validate_plan(plan, player, cfg)
            if issues:
                meta.failures.append(
                    f"u={u:g}{'，丢建造' if drop_builds else ''}："
                    f"validate 拒绝（{issues[0].code}）"
                )
                continue
            try:
                pipeline.preview(state, {PLAYER_ID: plan}, cfg)
            except Exception as exc:  # noqa: BLE001 —— 资金不足/其它拒绝都换下一档
                meta.failures.append(
                    f"u={u:g}{'，丢建造' if drop_builds else ''}："
                    f"{type(exc).__name__}: {exc}"
                )
                continue

            state = pipeline.advance(state, {PLAYER_ID: plan}, cfg)
            meta.u = u
            meta.builds_dropped = drop_builds
            return state, state.raw_reports[PLAYER_ID], meta

    # 最后一档：不开工、不买粮、不建造（仍然过不了就是真 bug，不吞）
    plan = make_plan(u_ids, food, builds, 0.0, drop_builds=True, drop_food=True)
    issues = validate.validate_plan(plan, player, cfg)
    if issues:
        raise RuntimeError(
            f"第 {state.world.quarter} 季连空计划都过不了校验：{issues}"
        )
    pipeline.preview(state, {PLAYER_ID: plan}, cfg)
    state = pipeline.advance(state, {PLAYER_ID: plan}, cfg)
    meta.u = 0.0
    meta.builds_dropped = True
    meta.food_cut = True
    return state, state.raw_reports[PLAYER_ID], meta


# ══════════════════════════════════════════════════════════════════
# 三、一季财报的拆解
# ══════════════════════════════════════════════════════════════════


def _segment_cost(rep, key: str) -> float:
    """按 `buy_bill.unit_prices` 把某一段买入量折成钱（= 上季价）。"""
    unit = rep.buy_bill.unit_prices if rep.buy_bill else {}
    return sum(
        qty * unit.get(goods, 0.0)
        for goods, qty in (rep.bought_breakdown.get(key) or {}).items()
    )


def sold_lines(rep) -> list[tuple[str, float, float, float]]:
    """收入拆解：[(商品, 卖出量, 本季价 P_t, 小计)]。"""
    lines: list[tuple[str, float, float, float]] = []
    for goods, qty in sorted(rep.sold.items()):
        if qty <= 0:
            continue
        price = rep.prices[goods]["price"]
        lines.append((goods, qty, price, qty * price))
    return lines


@dataclass
class QuarterRow:
    quarter: int
    cash_before: float
    cash_after: float
    total_deduction: float
    sales_revenue: float
    value_added: float
    happiness: float

    wages: float
    operating: float
    production_materials: float
    construction_materials: float
    other_purchases: float
    food_cost: float
    construction_cash: float

    wage_tax: float
    subsidy: float
    u: float
    builds_dropped: bool
    food_cut: bool
    builds_started: int
    plots: int
    sold_lines: list[tuple[str, float, float, float]] = field(default_factory=list)

    @property
    def net(self) -> float:
        """本季现金净变化（含补贴）。"""
        return self.cash_after - self.cash_before


def to_row(rep, meta: AttemptMeta, plots: int) -> QuarterRow:
    production_materials = _segment_cost(rep, "production_input")
    construction_materials = _segment_cost(rep, "construction")
    other_purchases = _segment_cost(rep, "other_purchases")
    food_cost = _segment_cost(rep, "household_food_shortfall")
    happiness = rep.food.happiness if rep.food else 0.0
    builds_started = sum(
        1 for e in rep.construction_events if e.get("event") == "started"
    )

    # 成本拆解必须能对上一次性扣款，否则表就是错的（不静默）。
    parts = (
        rep.wages
        + rep.operating
        + production_materials
        + construction_materials
        + other_purchases
        + food_cost
        + rep.construction_cash
    )
    if abs(parts - rep.total_deduction) > 1e-6:
        raise AssertionError(
            f"第 {rep.quarter} 季成本拆解对不上：拆解 {parts:.6f} "
            f"vs total_deduction {rep.total_deduction:.6f}"
        )

    return QuarterRow(
        quarter=rep.quarter,
        cash_before=rep.cash_before,
        cash_after=rep.cash_after,
        total_deduction=rep.total_deduction,
        sales_revenue=rep.sales_revenue,
        value_added=rep.value_added,
        happiness=happiness,
        wages=rep.wages,
        operating=rep.operating,
        production_materials=production_materials,
        construction_materials=construction_materials,
        other_purchases=other_purchases,
        food_cost=food_cost,
        construction_cash=rep.construction_cash,
        wage_tax=rep.wage_tax_refund,
        subsidy=rep.subsidy,
        u=meta.u,
        builds_dropped=meta.builds_dropped,
        food_cut=meta.food_cut,
        builds_started=builds_started,
        plots=plots,
        sold_lines=sold_lines(rep),
    )


# ══════════════════════════════════════════════════════════════════
# 四、跑一个策略
# ══════════════════════════════════════════════════════════════════


@dataclass
class StrategyResult:
    strategy: Strategy
    rows: list[QuarterRow]

    @property
    def final_cash(self) -> float:
        return self.rows[-1].cash_after if self.rows else START_CASH

    @property
    def cash_10(self) -> float:
        for row in self.rows:
            if row.quarter == TABLE_QUARTERS:
                return row.cash_after
        return self.rows[-1].cash_after if self.rows else START_CASH

    @property
    def pnl_10(self) -> float:
        return self.cash_10 - START_CASH

    @property
    def operating_10(self) -> float:
        """10 季经营累计（不含补贴）：Σ(销售 + 工资税回流 − 一次性扣款)。"""
        total = 0.0
        for row in self.rows:
            if row.quarter > TABLE_QUARTERS:
                break
            total += row.sales_revenue + row.wage_tax - row.total_deduction
        return total

    @property
    def warnings(self) -> list[str]:
        out: list[str] = []
        for row in self.rows:
            notes: list[str] = []
            if row.u < 1.0:
                notes.append(f"u 降到 {row.u:g}")
            if row.builds_dropped:
                notes.append("建造被移除")
            if row.food_cut:
                notes.append("口粮被砍")
            if notes:
                out.append(f"Q{row.quarter}：" + "、".join(notes))
        return out


def planned_builds(state: GameState, cfg: Config, strategy: Strategy) -> list[BuildOrder]:
    player = state.players[PLAYER_ID]
    cash = player.cash
    should_build = (not player.plots) or (cash > strategy.build_threshold)
    if not should_build:
        return []
    spot = pick_build_spot(state, cfg, strategy.plot_type)
    if spot is None:
        return []                              # 地图满了
    return [
        BuildOrder(
            plot_id=spot,
            plot_type=strategy.plot_type,
            target_level=1,
            product=strategy.product,
        )
    ]


def run_strategy(cfg: Config, strategy: Strategy, quarters: int = TOTAL_QUARTERS) -> StrategyResult:
    state = new_state(cfg)
    rows: list[QuarterRow] = []

    for _ in range(quarters):
        player = state.players[PLAYER_ID]
        u_ids = [
            pid
            for pid, plot in player.plots.items()
            if plot.status is PlotStatus.RUNNING
        ]
        f_baseline = projected_baseline_food(state, cfg)
        food = baseline_food_order(f_baseline, cfg)
        builds = planned_builds(state, cfg, strategy)

        # ★ 建造前预算预检：满开工 + 建造都过不了 preview，就本季先不建。
        #   理由：建造是**可选投资**，不该为了保它把生产压到 25%（那会得到
        #   "越建越穷"的假账）。把建造延期到下季，生产照常满开。
        #   ⚠️ 这与"资金不足 → 先降 u、再丢建造"的兜底**不冲突**：
        #      兜底仍按任务书顺序执行，只是本策略不会主动提出一个明摆着
        #      付不起的建造计划。
        if builds:
            probe = make_plan(u_ids, food, builds, 1.0)
            probe_ok = not validate.validate_plan(probe, player, cfg)
            if probe_ok:
                try:
                    pipeline.preview(state, {PLAYER_ID: probe}, cfg)
                except Exception:  # noqa: BLE001 —— 付不起就不建
                    probe_ok = False
            if not probe_ok:
                builds = []

        state, rep, meta = settle_quarter(state, cfg, u_ids, food, builds)
        rows.append(to_row(rep, meta, len(state.players[PLAYER_ID].plots)))

    return StrategyResult(strategy=strategy, rows=rows)


# ══════════════════════════════════════════════════════════════════
# 五、磨合诊断（证明 ramp 0.5 → 0.75 → 1.0 真的在推进）
# ══════════════════════════════════════════════════════════════════


@dataclass
class RampRow:
    quarter: int
    efficiency: Optional[float]
    ramp: Optional[float]
    running_quarters: int


def ramp_trace(cfg: Config) -> list[RampRow]:
    """一块农业 A：Q1 建（当季完工但本季不生产），Q2–Q5 满开工。"""
    state = new_state(cfg)
    rows: list[RampRow] = []

    for quarter in range(1, 6):
        player = state.players[PLAYER_ID]
        u_ids = [
            pid
            for pid, plot in player.plots.items()
            if plot.status is PlotStatus.RUNNING
        ]
        f_baseline = projected_baseline_food(state, cfg)
        food = baseline_food_order(f_baseline, cfg)
        builds: list[BuildOrder] = []
        if quarter == 1:
            builds = [
                BuildOrder(
                    plot_id="0,0",
                    plot_type=PlotType.AGRICULTURE,
                    target_level=1,
                    product="A",
                )
            ]

        state, rep, _meta = settle_quarter(state, cfg, u_ids, food, builds)
        plot = state.players[PLAYER_ID].plots["0,0"]
        eff = (rep.efficiency.get("0,0") or {}) if rep.efficiency else {}
        rows.append(
            RampRow(
                quarter=quarter,
                efficiency=eff.get("efficiency"),
                ramp=eff.get("ramp_up"),
                running_quarters=plot.running_quarters,
            )
        )
    return rows


# ══════════════════════════════════════════════════════════════════
# 六、M1 锚点（只读重算）
# ══════════════════════════════════════════════════════════════════


@dataclass
class M1Case:
    name: str
    price: float
    cash: float
    value_added: float


def m1_anchor(cfg: Config) -> list[M1Case]:
    """复刻 test_m1：各 1000 现金、E=1（rq=99、H=0.8）、工资税 10%。

    ⚠️ 期末现金要**扣掉补贴**再比 —— 锚点是运营净值，不是账面现金。
    """
    cases: list[M1Case] = []
    specs = (
        ("甲", PlotType.RESOURCE, None),
        ("乙", PlotType.INDUSTRY, "G"),
    )
    for name, plot_type, product in specs:
        world = cfg.new_world()
        world.phase = WorldPhase.RUNNING
        player = PlayerState(
            player_id=PLAYER_ID,
            cash=1000.0,
            plots={
                "0,0": Plot(
                    plot_id="0,0",
                    plot_type=plot_type,
                    level=1,
                    status=PlotStatus.RUNNING,
                    running_quarters=99,
                    product=product,
                )
            },
        )
        player.last_happiness = 0.8
        state = GameState(world=world, players={PLAYER_ID: player})
        plan = Plan(
            player_id=PLAYER_ID,
            plot_plans={"0,0": PlotPlan(plot_id="0,0", u=1.0)},
        )
        pipeline.advance(state, {PLAYER_ID: plan}, cfg)
        rep = state.raw_reports[PLAYER_ID]
        goods = rep.production[0].output_goods
        cases.append(
            M1Case(
                name=name,
                price=rep.prices[goods]["price"],
                cash=state.players[PLAYER_ID].cash - rep.subsidy,
                value_added=rep.value_added,
            )
        )
    return cases


# ══════════════════════════════════════════════════════════════════
# 七、候选参数（只在内存里 deepcopy，不写回 config）
# ══════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class Candidate:
    key: str
    label: str


CANDIDATES: tuple[Candidate, ...] = (
    Candidate("p0_x1.1", "P0 全线 ×1.1"),
    Candidate("wage_x0.8", "工资全线 ×0.8"),
    Candidate("subsidy_200", "每季补贴 120→200"),
)


def variant(cfg: Config, key: str) -> Config:
    c = copy.deepcopy(cfg)
    if key == "p0_x1.1":
        for curve in c.market.values():
            curve.P0 *= 1.1
    elif key == "wage_x0.8":
        c.wages = {k: v * 0.8 for k, v in c.wages.items()}
    elif key == "subsidy_200":
        c.quarterly_subsidy = 200.0
    else:
        raise KeyError(f"未知候选 {key!r}")
    return c


def stuck_ramp_cfg(cfg: Config) -> Config:
    """复现修复前的效果：E_磨合 永远 0.5（只用于对照，不是当前配置）。"""
    c = copy.deepcopy(cfg)
    # `ramp_up` 只覆盖第 1/2 季；第 3 季起走 `ramp_up_default`，
    # 所以两处都要压到 0.5，才能真的复现「永远 0.5」。
    c.efficiency.ramp_up = {1: 0.5, 2: 0.5}
    c.efficiency.ramp_up_default = 0.5
    return c


# ══════════════════════════════════════════════════════════════════
# 八、输出
# ══════════════════════════════════════════════════════════════════


def print_header(text: str) -> None:
    print()
    print("=" * 78)
    print(text)
    print("=" * 78)


def print_strategy_table(result: StrategyResult, quarters: int = TABLE_QUARTERS) -> None:
    print(f"\n[{result.strategy.key}] {result.strategy.label}")
    header = (
        f"{'季':>3} {'期初现金':>9} {'工资':>7} {'经营费':>7} {'材料':>7} "
        f"{'建造现金':>8} {'口粮':>6} {'扣款合计':>9} {'销售收入':>9} "
        f"{'期末现金':>9} {'增加值':>8} {'H':>5}"
    )
    print(header)
    print("-" * len(header))
    for row in result.rows:
        if row.quarter > quarters:
            break
        materials = row.production_materials + row.construction_materials + row.other_purchases
        print(
            f"{row.quarter:>3} {row.cash_before:>9.1f} {row.wages:>7.1f} "
            f"{row.operating:>7.1f} {materials:>7.1f} {row.construction_cash:>8.1f} "
            f"{row.food_cost:>6.1f} {row.total_deduction:>9.1f} "
            f"{row.sales_revenue:>9.1f} {row.cash_after:>9.1f} "
            f"{row.value_added:>8.1f} {row.happiness:>5.2f}"
        )


def print_income_breakdown(result: StrategyResult, quarter: int = 10) -> None:
    row = next((r for r in result.rows if r.quarter == quarter), None)
    if row is None:
        return
    print(f"\n  Q{quarter} 收入拆解（{result.strategy.key}）：", end="")
    if not row.sold_lines:
        print("（本季无卖出）")
        return
    print()
    for goods, qty, price, subtotal in row.sold_lines:
        print(f"    {goods:<4} {qty:>7.2f} × {price:>7.2f} = {subtotal:>8.2f}")


def print_cash_curve(results: list[StrategyResult]) -> None:
    print("\n10 季现金曲线（期末现金，含每季补贴）：")
    header = f"{'季':>3} " + " ".join(f"{r.strategy.key:>12}" for r in results)
    print(header)
    print("-" * len(header))
    for q in range(1, TABLE_QUARTERS + 1):
        cells = []
        for result in results:
            row = next((x for x in result.rows if x.quarter == q), None)
            cells.append(f"{row.cash_after:>12.1f}" if row else f"{'-':>12}")
        print(f"{q:>3} " + " ".join(cells))
    print(f"{'20':>3} " + " ".join(f"{r.final_cash:>12.1f}" for r in results))


def print_summary(results: list[StrategyResult]) -> None:
    print("\n策略汇总：")
    print(f"{'策略':<10} {'10季末':>10} {'10季累计':>10} {'10季经营累计':>13} "
          f"{'20季末':>10} {'末季地块':>8} {'收缩季':>7}")
    for result in results:
        print(
            f"{result.strategy.key:<10} {result.cash_10:>10.1f} "
            f"{result.pnl_10:>+10.1f} {result.operating_10:>+13.1f} "
            f"{result.final_cash:>10.1f} {result.rows[-1].plots:>8} "
            f"{len(result.warnings):>7}"
        )
    print("\n收缩/资金不足季：")
    any_warning = False
    for result in results:
        if result.warnings:
            any_warning = True
            print(f"  {result.strategy.key}: " + "；".join(result.warnings))
    if not any_warning:
        print("  无（三种策略在 20 季内均未触发 u 降档、丢建造或砍口粮）")


def print_ramp(rows: list[RampRow]) -> None:
    print(f"{'季':>3} {'E':>7} {'E_磨合':>7} {'已运行季度':>10}")
    print("-" * 32)
    for row in rows:
        eff = f"{row.efficiency:.3f}" if row.efficiency is not None else "—"
        ramp = f"{row.ramp:.2f}" if row.ramp is not None else "—"
        print(f"{row.quarter:>3} {eff:>7} {ramp:>7} {row.running_quarters:>10}")


def print_anchors(base: list[M1Case], candidate: list[M1Case]) -> None:
    for b, c in zip(base, candidate):
        delta = c.cash - b.cash
        print(
            f"    {c.name}：P_t {b.price:.6f} → {c.price:.6f}；"
            f"现金 {b.cash:.6f} → {c.cash:.6f}（Δ {delta:+.6f}）；"
            f"增加值 {b.value_added:.2f} → {c.value_added:.2f}"
        )
    base_va = sum(c.value_added for c in base)
    cand_va = sum(c.value_added for c in candidate)
    print(f"    合计增加值：{base_va:.2f} → {cand_va:.2f}")


def print_candidate_tables(cfg: Config, results: dict[str, list[StrategyResult]]) -> None:
    base_anchor = m1_anchor(cfg)
    print("\nM1 锚点影响（口径同 test_m1：各 1000 现金、E=1、现金已扣补贴）：")
    print(f"{'组':<14} {'甲P_t':>9} {'甲现金':>10} {'甲VA':>7} "
          f"{'乙P_t':>10} {'乙现金':>11} {'乙VA':>7} "
          f"{'合计VA':>7} {'判定':<22}")
    for key, label in (("baseline", "基准（现 config）"),) + tuple(
        (c.key, c.label) for c in CANDIDATES
    ):
        variant_cfg = cfg if key == "baseline" else variant(cfg, key)
        cases = m1_anchor(variant_cfg)
        jia, yi = cases
        va_total = jia.value_added + yi.value_added
        if key == "baseline":
            verdict = "基准"
        elif key == "p0_x1.1":
            verdict = "❌ 破坏：需重算全部锚点"
        elif key == "wage_x0.8":
            verdict = "⚠️ 现金锚变：需同步改"
        else:
            verdict = "✅ 锚点不变"
        print(
            f"{label:<14} {jia.price:>9.3f} {jia.cash:>10.2f} {jia.value_added:>7.1f} "
            f"{yi.price:>10.3f} {yi.cash:>11.2f} {yi.value_added:>7.1f} "
            f"{va_total:>7.1f} {verdict:<22}"
        )

    print("\n候选参数对现金的影响（只读 deepcopy，不写回 config）：")
    print(f"{'组':<14} {'策略':<10} {'10季末':>10} {'10季累计':>10} "
          f"{'10季经营累计':>13} {'20季末':>10}")
    for key, label in (("baseline", "基准"),) + tuple((c.key, c.label) for c in CANDIDATES):
        variant_cfg = cfg if key == "baseline" else variant(cfg, key)
        for strategy in STRATEGIES:
            result = run_strategy(variant_cfg, strategy)
            print(
                f"{label:<14} {strategy.key:<10} {result.cash_10:>10.1f} "
                f"{result.pnl_10:>+10.1f} {result.operating_10:>+13.1f} "
                f"{result.final_cash:>10.1f}"
            )
    print("\n逐组 M1 锚点变化：")
    for candidate in CANDIDATES:
        print(f"  [{candidate.label}]")
        print_anchors(base_anchor, m1_anchor(variant(cfg, candidate.key)))


def print_stuck_ramp_compare(cfg: Config) -> None:
    bug_cfg = stuck_ramp_cfg(cfg)
    result = run_strategy(bug_cfg, STRATEGIES[0])
    print(f"\n修前对照（E_磨合 永远 0.5，只用于说明问题）：")
    print(f"  20 季末现金：{result.final_cash:.1f}（当前修复后：见上表）")


# ══════════════════════════════════════════════════════════════════
# 九、主流程
# ══════════════════════════════════════════════════════════════════


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cfg = load_config()

    print_header("经济体检实验台（只读）")
    print(
        f"开局 {cfg.opening_cash:.0f} 现金 / 零地块 / {TOTAL_QUARTERS} 季 / "
        f"每季补贴 {cfg.quarterly_subsidy:.0f} / 政策模式 regular（无随机政策噪声）"
    )

    # 1) 磨合诊断
    print_header("一、磨合诊断：一块农业 A，Q1 建，Q2 起满开工")
    print_ramp(ramp_trace(cfg))

    # 2) 三种策略
    results = [run_strategy(cfg, strategy) for strategy in STRATEGIES]
    print_header(f"二、策略 10 季明细（每季成本拆解 + 期末现金）")
    for result in results:
        print_strategy_table(result)
    print_cash_curve(results)
    print_summary(results)

    # 3) 收入拆解
    print_header("三、第 10 季收入拆解（卖出量 × 本季 P_t）")
    for result in results:
        print_income_breakdown(result)

    # 4) 修前对照
    print_header("四、修前/修后对照（20 季期末现金）")
    print_stuck_ramp_compare(cfg)
    for result in results:
        print(f"  当前（磨合正常）{result.strategy.key}: {result.final_cash:.1f}")

    # 5) 候选参数
    print_header("五、候选参数实验（只读 deepcopy；P0/工资/补贴）")
    print_candidate_tables(cfg, {})

    # 6) 结论
    print_header("六、自动判读（数字的结论，不是拍板）")
    base = results[0]
    print(
        f"· 修好后 all_run 20 季末 {base.final_cash:.1f}，"
        f"最低点 {min(r.cash_after for r in base.rows):.1f}（Q"
        f"{min(base.rows, key=lambda r: r.cash_after).quarter}）"
    )
    print(
        "· 若把 E_磨合 固定在 0.5（复现修复前），同一策略会跌到 "
        f"{run_strategy(stuck_ramp_cfg(cfg), STRATEGIES[0]).final_cash:.1f}。"
    )
    print("· P0 ×1.1 会同时改 P_t / 现金 / 增加值三个 M1 锚点 → 不能用，除非整套重算。")
    print("· 工资 ×0.8 价格与增加值锚不变，但甲/乙期末现金锚会变 → 需同步改测试与文档。")
    print("· 补贴 200 不影响任何 M1 锚点，但修好后现金已转正 → 当前不需要。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
