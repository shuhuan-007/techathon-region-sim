"""磨合计时回归 —— 防止 `running_quarters` 再次停在 0（E_磨合 永久 0.5）。

背景（2026-10-18）：
    `pipeline._advance_construction()` 的 else 分支曾写成
    `plot.running_quarters += 0`，于是所有地块的磨合永不推进 →
    第 1 季以后 E_磨合 一直是 0.5，产量腰斩。
    M1 用 `running_quarters=99` 的成熟夹具，所以锚点测试一直没暴露它。

本文件钉死正确语义（依据 `loader.EfficiencyConfig.ramp_factor`）：
    · Q1 新建 1 季农业 → 当季完工、`running_quarters = 0`，
      本季没有生产窗口，**不推进磨合**；
    · Q2 首次生产 → 报告里 `ramp_up = 0.50`，季末 `running_quarters = 1`；
    · Q3 生产 → `ramp_up = 0.75`，季末 `running_quarters = 2`；
    · Q4 起 → `ramp_up = 1.00`。

跑法：
    python tests/test_ramp.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import pipeline, validate  # noqa: E402
from engine.loader import Config, load_config  # noqa: E402
from engine.models import (  # noqa: E402
    BuildOrder,
    GameState,
    Plan,
    Plot,
    PlotPlan,
    PlotStatus,
    PlotType,
    WorldPhase,
)
from engine.pipeline import SettlementError  # noqa: E402

TOL = 1e-9


# ══════════════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════════════


def make_state(cfg: Config) -> GameState:
    """开局：1200 现金、零地块。"""
    world = cfg.new_world()
    world.phase = WorldPhase.RUNNING
    player = cfg.new_player("p1")
    return GameState(world=world, players={"p1": player})


def build_agri_plan() -> Plan:
    """Q1 在 0,0 新建一级农业 A（1 季工程，当季完工）。"""
    return Plan(
        player_id="p1",
        builds=[
            BuildOrder(
                plot_id="0,0",
                plot_type=PlotType.AGRICULTURE,
                target_level=1,
                product="A",
            )
        ],
    )


def run_plan(state: GameState, cfg: Config, plan: Plan):
    """校验 → 真结算 → 返回报告。"""
    issues = validate.validate_plan(plan, state.players["p1"], cfg)
    assert not issues, f"计划应通过校验，实得 {issues}"
    pipeline.advance(state, {"p1": plan}, cfg)
    return state.raw_reports["p1"]


def run_one_plot(state: GameState, cfg: Config) -> tuple:
    """跑一季：只有 0,0 满开工。"""
    plan = Plan(
        player_id="p1",
        plot_plans={"0,0": PlotPlan(plot_id="0,0", u=1.0)},
    )
    rep = run_plan(state, cfg, plan)
    return state, rep


# ══════════════════════════════════════════════════════════════════
# 一、新建当季完工的地块：rq = 0，本季没有生产窗口
# ══════════════════════════════════════════════════════════════════


def test_new_plot_completes_with_zero_running_quarters():
    """Q1 建 1 季农业：当季完工转 RUNNING，但 rq 必须是 0。"""
    cfg = load_config()
    state = make_state(cfg)
    rep = run_plan(state, cfg, build_agri_plan())

    plot = state.players["p1"].plots["0,0"]
    assert plot.status is PlotStatus.RUNNING, "1 季工程应当季完工"
    assert plot.level == 1, "完工后等级应落地为 1"
    assert plot.running_quarters == 0, (
        "本季才完工、生产窗口已过，磨合不得提前 +1（下季首次生产才是第 1 季）"
    )
    assert rep.production == [], "当季完工的地块本季不生产"


# ══════════════════════════════════════════════════════════════════
# 二、磨合按 0.5 → 0.75 → 1.0 推进
# ══════════════════════════════════════════════════════════════════


def test_ramp_advances_after_each_production_quarter():
    """Q2/Q3/Q4：报告 ramp 0.50/0.75/1.00；季末 rq 1/2/3。"""
    cfg = load_config()
    state = make_state(cfg)
    run_plan(state, cfg, build_agri_plan())
    plot = state.players["p1"].plots["0,0"]

    expected = (
        # 季, 本季生产用的 ramp, 本季末 rq
        (2, 0.50, 1),
        (3, 0.75, 2),
        (4, 1.00, 3),
    )
    for quarter, ramp, rq_after in expected:
        state, rep = run_one_plot(state, cfg)

        assert rep.quarter == quarter, f"应为第 {quarter} 季，实得 {rep.quarter}"
        assert len(rep.production) == 1, f"Q{quarter} 应有一块地产出"
        eff = rep.efficiency["0,0"]
        assert abs(eff["ramp_up"] - ramp) < TOL, (
            f"Q{quarter} 的 E_磨合 应为 {ramp}，实得 {eff['ramp_up']} —— "
            f"running_quarters 是否没推进？"
        )
        # E = ramp × E_幸福(0.9)，验证报告里的 E 与分量一致
        assert abs(rep.production[0].efficiency - ramp * 0.9) < TOL, (
            f"Q{quarter} 的 E 应等于 ramp×0.9"
        )
        assert plot.running_quarters == rq_after, (
            f"Q{quarter} 生产之后 rq 应为 {rq_after}，实得 {plot.running_quarters}"
        )

    # 第 4 季起封顶 1.0（再跑一季仍是 1.0，rq 继续累加）
    state, rep = run_one_plot(state, cfg)
    assert abs(rep.efficiency["0,0"]["ramp_up"] - 1.0) < TOL
    assert plot.running_quarters == 4


# ══════════════════════════════════════════════════════════════════
# 三、结算失败不能留下 rq+1（§11.3「失败即无痕」）
# ══════════════════════════════════════════════════════════════════


def test_failed_settlement_does_not_advance_ramp():
    """资金不足抛错后，地块的 running_quarters 必须原样。"""
    from copy import deepcopy

    cfg = load_config()
    state = make_state(cfg)

    # 一块成熟工业 G，但只给 1 块钱：买 C/D/F 都不够。
    state.players["p1"].plots["0,0"] = Plot(
        plot_id="0,0",
        plot_type=PlotType.INDUSTRY,
        level=1,
        status=PlotStatus.RUNNING,
        running_quarters=0,
        product="G",
    )
    state.players["p1"].cash = 1.0
    before = deepcopy(state.to_dict())

    plan = Plan(
        player_id="p1",
        plot_plans={"0,0": PlotPlan(plot_id="0,0", u=1.0)},
    )
    try:
        pipeline.advance(state, {"p1": plan}, cfg)
    except SettlementError as exc:
        assert "资金不足" in str(exc)
    else:
        raise AssertionError("资金不足时必须抛 SettlementError")

    assert state.players["p1"].plots["0,0"].running_quarters == 0, (
        "扣款失败发生在磨合推进之前，rq 不得被改"
    )
    assert state.to_dict() == before, "扣款失败不应留下任何部分状态变更"


# ══════════════════════════════════════════════════════════════════
# 四、命令行
# ══════════════════════════════════════════════════════════════════


def _run_all() -> int:
    import io

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    mod = sys.modules[__name__]
    names = sorted(n for n in dir(mod) if n.startswith("test_"))
    ok = fail = 0
    print("=" * 68)
    print("磨合回归（ramp 0.5 → 0.75 → 1.0）")
    print("=" * 68)
    for name in names:
        try:
            getattr(mod, name)()
            print(f"  PASS  {name}")
            ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL  {name}\n        {type(exc).__name__}: {exc}")
            fail += 1
    print(f"\n{ok} passed, {fail} failed, {len(names)} total")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
