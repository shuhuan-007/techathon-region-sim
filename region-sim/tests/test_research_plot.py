"""研究地块（最小存储改动）—— industry L2 product='研究' 的运行期行为。

跑法（在 region-sim/ 下）：

    python -X utf8 tests/test_research_plot.py

依据（2026-10-18 用户允许的最小改动）：
    研究地块**内部**仍编码为 ``PlotType.INDUSTRY + level=2 + product='研究'``，
    **不改存档格式**；但**所有运行期行为**必须等同研究地块：
      · 建模         → `研究`（`engine/assets.py`）
      · 配方         → `research.maintain`（c=60, d=2, e=0，不产商品）
      · 工资         → `high_tech`（28 / 单位劳动 / 季）
      · 研究加成     → 玩家内 +1%/块、封顶 +3%（研究地块**自身不加**）
      · 升级路线     → 可作为 `ind_2_3_high`（二级研究 → 三级 I）的前置

本文件按「零依赖 stdlib runner」惯例写（无 pytest）。
"""

from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import efficiency as E_mod
from engine import pipeline
from engine import report as R
from engine import validate
from engine.construction import find_work_for_build
from engine.loader import Config, load_config
from engine.models import (
    BuildOrder,
    GameState,
    Plan,
    PlayerState,
    Plot,
    PlotPlan,
    PlotStatus,
    PlotType,
    WorldPhase,
)
from engine.population import compute_population

TOL = 1e-9


# ══════════════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════════════

def _cfg() -> Config:
    cfg = load_config()
    # 让所有商品都能求价（研究地块不产商品，但玩家/邻地可能有产出要走市场）
    for curve in cfg.market.values():
        if curve.Es is None:
            curve.Es = 0.5
        if curve.Ed is None:
            curve.Ed = 0.5
    return cfg


def _research_plot(pid: str = "1,1") -> Plot:
    """核心夹具：内部编码为 industry L2 product='研究' 的研究地块。"""
    return Plot(
        plot_id=pid,
        plot_type=PlotType.INDUSTRY,
        level=2,
        status=PlotStatus.RUNNING,
        running_quarters=99,      # 磨合已满
        product="研究",
    )


def _state(cfg: Config, *plots: Plot, cash: float = 5000.0) -> GameState:
    world = cfg.new_world()
    world.phase = WorldPhase.RUNNING
    player = PlayerState(
        player_id="p1",
        cash=cash,
        plots={p.plot_id: p for p in plots},
    )
    player.last_happiness = 0.8     # → E_幸福 = 1.00，与 M1 同条件
    return GameState(world=world, players={"p1": player})


# ══════════════════════════════════════════════════════════════════
# ① Plot.is_research：双形态识别
# ══════════════════════════════════════════════════════════════════

def test_is_research_industry_l2_research_product():
    assert _research_plot().is_research is True


def test_is_research_research_plot_type():
    p = Plot(plot_id="0,0", plot_type=PlotType.RESEARCH, level=1,
             status=PlotStatus.RUNNING)
    assert p.is_research is True


def test_is_research_negative_cases():
    # 工业 L2 机械路线：不是研究
    assert Plot(plot_id="0,0", plot_type=PlotType.INDUSTRY, level=2,
                status=PlotStatus.RUNNING, product="H-A").is_research is False
    # 工业 L1 G：不是研究
    assert Plot(plot_id="0,0", plot_type=PlotType.INDUSTRY, level=1,
                status=PlotStatus.RUNNING, product="G").is_research is False
    # 工业 L3 I：不是研究
    assert Plot(plot_id="0,0", plot_type=PlotType.INDUSTRY, level=3,
                status=PlotStatus.RUNNING, product="I").is_research is False
    # 服务：不是研究
    assert Plot(plot_id="0,0", plot_type=PlotType.SERVICE, level=1,
                status=PlotStatus.RUNNING).is_research is False


# ══════════════════════════════════════════════════════════════════
# ② validate：industry L2 product='研究' 能过校验（**不抛 ConfigError**）
# ══════════════════════════════════════════════════════════════════

def test_validate_research_plot_does_not_raise():
    cfg = _cfg()
    st = _state(cfg, _research_plot())
    plan = Plan(
        player_id="p1",
        plot_plans={"1,1": PlotPlan(plot_id="1,1", active=True, u=1.0)},
    )
    player = st.players["p1"]
    # validate_plan 会调 cfg.recipe_for(plot) —— 不能抛 ConfigError
    issues = validate.validate_plan(plan, player, cfg, player_id="p1")
    # 只允许"非阻断"性提示；绝不能有 recipe_unavailable
    codes = [i.code for i in issues]
    assert "recipe_unavailable" not in codes, (
        f"研究地块被判 recipe_unavailable：{[i.message for i in issues]}"
    )
    # 实际上它根本不该出现在 issues 里（配方能查到、d=2 是合法劳动）
    assert not issues, f"研究地块不应被 validate 拦截，实得：{[i.message for i in issues]}"


# ══════════════════════════════════════════════════════════════════
# ③ pipeline.preview：不产商品 / 工资 28 / c=60（研究地块配方）
# ══════════════════════════════════════════════════════════════════

def test_preview_research_plot_uses_research_recipe_and_high_tech_wage():
    cfg = _cfg()
    st = _state(cfg, _research_plot())
    plan = Plan(
        player_id="p1",
        plot_plans={"1,1": PlotPlan(plot_id="1,1", active=True, u=1.0)},
    )
    # preview 不能抛 ConfigError（旧实现里 recipe/wage_for 会 raise）
    rep = pipeline.preview(st, {"p1": plan}, cfg)
    assert rep is not None

    # 找到这块地的 PlotOutcome
    outcome = next((o for o in rep.production if o.plot_id == "1,1"), None)
    assert outcome is not None, "研究地块的 PlotOutcome 应该进报告（u=1 开工中）"

    # ── 不产商品 ──
    assert outcome.output_goods is None, "研究地块不能产商品"
    assert outcome.output_qty == 0.0
    assert outcome.inputs == {}, f"研究配方 research.maintain 应为空 input，实得 {outcome.inputs}"

    # ── 配方是 research.maintain（c=60, d=2） ──
    #   d×u = 2×1 = 2 → labor
    assert abs(outcome.labor - 2.0) < TOL
    #   工资 = labor × wage_rate = 2 × 28 = 56 → 反推 wage_rate = 28
    assert abs(outcome.wage_rate - 28.0) < TOL, (
        f"研究地块工资率应为 28（high_tech），实得 {outcome.wage_rate}"
    )
    assert abs(outcome.wage - 56.0) < TOL
    #   经营费 = 0.7c + 0.3c × u × E；c=60、u=1、E=成果（有 happiness 分量）
    #   本夹具 last_happiness=0.8 → E_幸福=1.0；其它分量 ≥ 0.5 → 不细抠，
    #   只按 c=60 这件事锁"配方对"这一头：
    #   若 c 不是 60，operating_cost 会偏出去。
    eff = outcome.efficiency
    expected_operating = 0.7 * 60.0 + 0.3 * 60.0 * 1.0 * eff
    assert abs(outcome.operating_cost - expected_operating) < 1e-6, (
        f"经营费与 c=60 配方不一致：实得 {outcome.operating_cost}，"
        f"按 c=60 应得 {expected_operating}"
    )
    # ── 配方 confirmation：直接查 cfg ──
    plot = st.players["p1"].plots["1,1"]
    recipe = cfg.recipe_for(plot)
    assert recipe.plot_type is PlotType.RESEARCH
    assert recipe.level == 1
    assert recipe.output is None
    assert recipe.c == 60.0
    assert recipe.d == 2.0
    assert recipe.e == 0.0


# ══════════════════════════════════════════════════════════════════
# ④ research_bonus：+1% / 块，**自身不加**、不含研究地块的邻地吃到 +1%
# ══════════════════════════════════════════════════════════════════

def test_research_bonus_counts_industry_l2_research_plot():
    cfg = _cfg()
    research = _research_plot("1,1")
    neighbor = Plot(plot_id="1,2", plot_type=PlotType.RESOURCE, level=1,
                    status=PlotStatus.RUNNING, running_quarters=99, product="F")
    st = _state(cfg, research, neighbor)

    # 研究地块自身：research_multiplier = 1.0（自身不加成）
    self_bonus = E_mod.research_bonus(st, "p1", research, cfg)
    assert abs(self_bonus - 1.0) < TOL, "研究地块自身不能吃研究加成"

    # 邻地（非研究、非 PlotType.RESEARCH）：+1%
    other_bonus = E_mod.research_bonus(st, "p1", neighbor, cfg)
    assert abs(other_bonus - 1.01) < 1e-9, (
        f"一块研究地块应给其它地块 +1%，实得 {other_bonus}"
    )


def test_research_bonus_cap_3_percent():
    cfg = _cfg()
    plots = [_research_plot(f"0,{i}") for i in range(5)]   # 5 块研究
    plots.append(Plot(plot_id="9,9", plot_type=PlotType.SERVICE, level=1,
                      status=PlotStatus.RUNNING, running_quarters=99))
    st = _state(cfg, *plots)
    plot_target = st.players["p1"].plots["9,9"]
    bonus = E_mod.research_bonus(st, "p1", plot_target, cfg)
    assert abs(bonus - 1.03) < 1e-9, f"研究加成应封顶 +3%，实得 {bonus}"


# ══════════════════════════════════════════════════════════════════
# ⑤ population：研究地块 d=2 计岗位（labor_total 含它）
# ══════════════════════════════════════════════════════════════════

def test_population_counts_research_d_as_jobs():
    cfg = _cfg()
    st = _state(cfg, _research_plot())
    u_map: dict = {"1,1": 1.0}   # 本季开工
    pop = compute_population(st, "p1", u_map, cfg)
    # 研究地块 d=2，岗位 = Σd = 2
    assert abs(pop.labor_total - 2.0) < TOL, (
        f"研究地块 d=2 应贡献岗位，实得 labor_total={pop.labor_total}"
    )
    # 人口 = labor_food × r（研究地块不是 SERVICE，吃粮 → 人口也算）
    assert abs(pop.population - 2.0 * cfg.population.r) < TOL


# ══════════════════════════════════════════════════════════════════
# ⑥ report.build_state：研究地块不产出（output_preview.goods 为 None）
# ══════════════════════════════════════════════════════════════════

def test_build_state_research_plot_output_preview_none():
    cfg = _cfg()
    st = _state(cfg, _research_plot())
    plan = Plan(
        player_id="p1",
        plot_plans={"1,1": PlotPlan(plot_id="1,1", active=True, u=1.0)},
    )
    # build_state 也不该炸（recipe_for/wage_for_plot 重定向）
    out = R.build_state(st, "p1", cfg, plan=plan)
    assert out is not None
    plots_block = [p for p in out["plots"] if p["plot_id"] == "1,1"]
    assert len(plots_block) == 1
    row = plots_block[0]
    assert row["output_preview"] is not None
    assert row["output_preview"]["goods"] is None
    assert abs(row["output_preview"]["wage"] - 56.0) < 1e-6


# ══════════════════════════════════════════════════════════════════
# ⑦ 升级路线：research 地块可作为 ind_2_3_high 的前置（不抛 ConfigError）
# ══════════════════════════════════════════════════════════════════

def test_research_plot_can_upgrade_to_i_via_ind_2_3_high():
    cfg = _cfg()
    st = _state(cfg, _research_plot("3,3"))
    plot = st.players["p1"].plots["3,3"]
    assert plot.is_research is True

    order = BuildOrder(
        plot_id="3,3", plot_type=PlotType.INDUSTRY,
        target_level=3, product="I",
    )
    # find_work_for_build 应从 plot.product='研究' 推断成 research 路线
    work = find_work_for_build(cfg, plot, order)
    assert work is not None
    assert work.label == "二级研究→三级 I"
    assert work.from_level == 2 and work.to_level == 3
    assert work.quarters == 8 and work.cash == 500


# ══════════════════════════════════════════════════════════════════
# ⑧ 整季 preview：不抛 ConfigError（CO 覆盖）
# ══════════════════════════════════════════════════════════════════

def test_preview_does_not_raise_with_research_plot():
    cfg = _cfg()
    st = _state(cfg, _research_plot(), cash=100000.0)
    plan = Plan(
        player_id="p1",
        plot_plans={"1,1": PlotPlan(plot_id="1,1", active=True, u=1.0)},
    )
    rep = pipeline.preview(st, {"p1": plan}, cfg)
    # 不为 None = preview 完成 = 不抛 ConfigError
    assert rep is not None
    assert rep.player_id == "p1"


# ══════════════════════════════════════════════════════════════════
# 标准库 runner（与 tests/test_assets.py 同款）
# ══════════════════════════════════════════════════════════════════

def _run_all() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
            passed += 1
        except Exception as e:  # noqa: BLE001
            print(f"  FAIL  {name}\n          {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
