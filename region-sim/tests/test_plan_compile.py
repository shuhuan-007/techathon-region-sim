"""起草编译器测试 —— 模型常见坏输出 → 必须变成“能跑的计划”。

跑法（在 region-sim/ 下）：

    python tests/test_plan_compile.py

覆盖真实故障（2026-10-18 复现）：
  · 模型把玩家**已有地块**写进 builds → 应改成开工，而不是整份作废；
  · 模型**漏写 plot_plans** → 应默认全部开工，而不是全部空转；
  · 模型建 10 条工业（超预算）→ 应缩量到试算跑得通；
  · 产物写错 / 坐标越界 / 未知地块 → 应规整或丢弃，并留下 notes；
  · 模型没给口粮 → 应补基准档；
  · 空地新建零邻接加成 → 应搬到推荐邻格并写 note（已有加成/升级不搬）；
  · 编译器返回的 plan **必须同时过 validate 与 preview**。
"""

from __future__ import annotations

import copy
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai.plan_compile import compile_draft
from engine import pipeline, validate
from engine.loader import load_config
from engine.models import GameState, PlotType, PlayerState
from tests.test_m1 import mature, make_state

TOL = 1e-6


# ══════════════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════════════


def _opening_state(cfg):
    """开局：0 地块、现金 1200（与 Registry.create 同口径）。"""
    return GameState(world=cfg.new_world(), players={"p1": cfg.new_player("p1")})


def _mid_state(cfg):
    """仿 `saves/1.json` 的第 4 季局面：5 块运行地块、现金 416、H=0。"""
    from engine.models import PlotType

    plots = {
        "4,5": mature("4,5", PlotType.RESOURCE, 1, product="A"),
        "5,3": mature("5,3", PlotType.SERVICE, 1),
        "5,4": mature("5,4", PlotType.AGRICULTURE, 1, product="A"),
        "6,3": mature("6,3", PlotType.RESOURCE, 1),
        "6,4": mature("6,4", PlotType.INDUSTRY, 1, product="G"),
    }
    player = PlayerState(
        player_id="p1", cash=416.0, plots=plots, last_happiness=0.0
    )
    return GameState(world=cfg.new_world(), players={"p1": player})


def _farm_state(cfg):
    """开局 + **一块已有农地** `5,5` —— 测「空地新建该贴哪」（用户反馈 B）。

    只放一块地，推荐空地就能逐格断言：5,5 的八邻格全部 +5%，
    同分按 (行,列) 排 → 4,4 → 4,5 → 4,6 → 5,4 → 5,6 …
    """
    return make_state(
        cfg, "p1", mature("5,5", PlotType.AGRICULTURE, 1, product="A"),
        cash=1200.0,
    )


def _raw(**kw):
    base = {"player_id": "p1", "builds": [], "plot_plans": {},
            "purchases": {}, "food": {}}
    base.update(kw)
    return base


def _assert_compiled_ok(result, state, cfg):
    """编译器说 ok 时，必须同时过 validate 与 preview。"""
    assert result.ok is True, (
        f"编译应成功，实得 ok={result.ok}、issues={result.issues}、"
        f"settle_error={result.settle_error}"
    )
    assert result.plan is not None
    player = state.player("p1")
    issues = validate.validate_plan(result.plan, player, cfg, "p1")
    assert not issues, f"编译产物没过 validate：{[i.to_dict() for i in issues]}"
    rep = pipeline.preview(state, {"p1": result.plan}, cfg)
    assert rep is not None
    assert result.preview is not None, "ok=True 必须带 preview 摘要"


# ══════════════════════════════════════════════════════════════════
# 一、合法输入：原样编译，且 validate + preview 都过
# ══════════════════════════════════════════════════════════════════


def test_opening_valid_build_compiles_and_previews():
    cfg = load_config()
    state = _opening_state(cfg)
    raw = _raw(builds=[{
        "plot_id": "0,0", "plot_type": "agriculture",
        "target_level": 1, "product": "A",
    }])
    result = compile_draft(raw, state, "p1", cfg)
    _assert_compiled_ok(result, state, cfg)
    assert len(result.plan.builds) == 1
    assert result.plan.builds[0].plot_id == "0,0"
    assert result.plan.builds[0].product == "A"
    # 开局无人口 → 不会凭空补口粮
    assert result.plan.food == {}
    # 只读：状态逐字不变
    assert state.to_dict() == _opening_state(cfg).to_dict()


# ══════════════════════════════════════════════════════════════════
# 二、真实故障 A：已有地块被写进 builds → 改成开工
# ══════════════════════════════════════════════════════════════════


def test_existing_plots_written_as_builds_become_run():
    """真跑复现的那份模型输出：3 条 builds 全指向已有地块，plot_plans 空。"""
    cfg = load_config()
    state = _mid_state(cfg)
    raw = _raw(builds=[
        {"plot_id": "4,5", "plot_type": "resource", "target_level": 1, "product": None},
        {"plot_id": "5,4", "plot_type": "agriculture", "target_level": 1, "product": "A"},
        {"plot_id": "6,3", "plot_type": "service", "target_level": 1, "product": None},
    ])
    result = compile_draft(raw, state, "p1", cfg)
    _assert_compiled_ok(result, state, cfg)

    assert result.plan.builds == [], "已有地块绝不能再变成建造指令"
    running = {pid for pid, p in state.player("p1").plots.items() if p.is_running}
    assert set(result.plan.plot_plans) == running, (
        "漏写 plot_plans 时应默认全部开工（而不是空转）"
    )
    assert any("已有" in n for n in result.notes), (
        f"必须留下“已有地块已改为开工”的校正说明：{result.notes}"
    )


def test_existing_plot_build_without_type_infers_run():
    """模型只写 plot_id、不写 plot_type 时，用该地块自己的类型补，别丢意图。"""
    cfg = load_config()
    state = _mid_state(cfg)
    raw = _raw(builds=[{"plot_id": "4,5", "target_level": 1}])
    result = compile_draft(raw, state, "p1", cfg)
    _assert_compiled_ok(result, state, cfg)
    assert result.plan.builds == []
    assert "4,5" in result.plan.plot_plans
    assert any("已有" in n for n in result.notes), result.notes


def test_empty_intent_is_explicit_empty_plan():
    """模型明确啥也不做（空对象）→ 空计划 + 一句说明，不假装有动作。"""
    cfg = load_config()
    state = _opening_state(cfg)
    result = compile_draft(_raw(), state, "p1", cfg)
    _assert_compiled_ok(result, state, cfg)
    assert result.plan.builds == []
    assert result.plan.plot_plans == {}
    assert any("空计划" in n for n in result.notes), result.notes


def test_all_invalid_intent_is_rejected_not_empty_plan():
    """模型给的动作全无效（越界建造）→ 明确失败，不交一份空计划。"""
    cfg = load_config()
    state = _opening_state(cfg)
    raw = _raw(builds=[{
        "plot_id": "99,99", "plot_type": "agriculture",
        "target_level": 1, "product": "A",
    }])
    result = compile_draft(raw, state, "p1", cfg)
    assert result.ok is False
    assert result.plan is None
    assert any(i["code"] == "no_valid_intent" for i in result.issues), result.issues
    assert result.notes, "要说明哪些动作被丢了"


# ══════════════════════════════════════════════════════════════════
# 三、真实故障 B：10 条工业（超预算）→ 缩量到跑得通
# ══════════════════════════════════════════════════════════════════


def test_over_budget_builds_are_shrunk_until_preview_passes():
    cfg = load_config()
    state = _opening_state(cfg)
    raw = _raw(builds=[
        {"plot_id": f"0,{i}", "plot_type": "industry",
         "target_level": 1, "product": "G"}
        for i in range(10)
    ])
    result = compile_draft(raw, state, "p1", cfg)
    _assert_compiled_ok(result, state, cfg)

    assert len(result.plan.builds) < 10, (
        f"10 条工业明显超预算，应缩量；实得 {len(result.plan.builds)} 条"
    )
    assert result.preview["cash_after"] >= -TOL, "缩量后现金不能为负"
    assert any("现金不足" in n or "移除" in n for n in result.notes), (
        f"缩量必须留下说明：{result.notes}"
    )


def test_tight_cash_prefers_production_over_new_build():
    """已有生产 + 新建超预算：**先撤建造，不要把现有地块全停掉**。

    真跑复现：save 1 现金 416，5 块运行地块 + 模型新建 1 块农业；
    旧缩量顺序先降 u，最后 u=0 才跑通 —— 计划合法但 5 块地全停。
    现在应先移除新建造，保留 5 块地原开工率。
    """
    cfg = load_config()
    state = _mid_state(cfg)
    state.player("p1").cash = 250.0       # E1 降本后 416 已能同时建造+开工；这里压到真·超预算但可开工
    raw = _raw(builds=[{
        "plot_id": "0,0", "plot_type": "agriculture",
        "target_level": 1, "product": "A",
    }])
    result = compile_draft(raw, state, "p1", cfg)
    _assert_compiled_ok(result, state, cfg)

    assert result.plan.builds == [], (
        f"现金不够时应先撤新建造，实得 {result.plan.builds}"
    )
    running = {pid for pid, p in state.player("p1").plots.items() if p.is_running}
    assert set(result.plan.plot_plans) == running
    assert all(pp.u > 0 for pp in result.plan.plot_plans.values()), (
        "现有地块不该被降到 0 来给新建造让路"
    )
    assert any("保住现有生产" in n for n in result.notes), result.notes


def test_protected_build_is_not_removed_by_shrink():
    """编辑模式：玩家草稿里已有的建造不能为了省现金被编译器悄悄删掉。"""
    cfg = load_config()
    state = _mid_state(cfg)
    raw = _raw(builds=[{
        "plot_id": "0,0", "plot_type": "agriculture",
        "target_level": 1, "product": "A",
    }])
    result = compile_draft(
        raw, state, "p1", cfg, protected_build_ids={"4,4"},
    )
    _assert_compiled_ok(result, state, cfg)

    assert [b.plot_id for b in result.plan.builds] == ["4,4"], (
        f"受保护的建造不能被缩量删掉，实得 {result.plan.builds}"
    )
    assert all(pp.u > 0 for pp in result.plan.plot_plans.values()), (
        "保建造也不该把现有生产降到 0"
    )


# ══════════════════════════════════════════════════════════════════
# 四、字段规整：产物错 / 坐标越界 / 未知 plot_plan
# ══════════════════════════════════════════════════════════════════


def test_invalid_product_and_out_of_bounds_are_repaired_or_dropped():
    cfg = load_config()
    state = _opening_state(cfg)
    raw = _raw(builds=[
        {"plot_id": "0,0", "plot_type": "agriculture",
         "target_level": 1, "product": "Z"},
        {"plot_id": "99,99", "plot_type": "industry",
         "target_level": 2, "product": "G"},
    ])
    result = compile_draft(raw, state, "p1", cfg)
    _assert_compiled_ok(result, state, cfg)

    assert [b.plot_id for b in result.plan.builds] == ["0,0"], (
        "越界坐标必须丢弃，合法农地必须保留"
    )
    assert result.plan.builds[0].product == "A", "非法产物 Z 应改为默认 A"
    assert any("越界" in n for n in result.notes), result.notes
    assert any("产物" in n or "Z" in n for n in result.notes), result.notes


def test_unknown_plot_plan_is_dropped_but_valid_build_kept():
    """未知 plot_plan 不连累整份草案：丢掉它，合法建造继续。"""
    cfg = load_config()
    state = _opening_state(cfg)
    raw = _raw(
        builds=[{"plot_id": "0,0", "plot_type": "agriculture",
                 "target_level": 1, "product": "A"}],
        plot_plans={"99,99": {"plot_id": "99,99", "active": True, "u": 1.0}},
    )
    result = compile_draft(raw, state, "p1", cfg)
    _assert_compiled_ok(result, state, cfg)

    assert "99,99" not in result.plan.plot_plans
    assert len(result.plan.builds) == 1
    assert any("99,99" in n for n in result.notes), result.notes


# ══════════════════════════════════════════════════════════════════
# 五、默认补口粮（人口只跟已建成地块走）
# ══════════════════════════════════════════════════════════════════


def test_missing_food_defaults_to_baseline_need():
    cfg = load_config()
    state = _mid_state(cfg)
    raw = _raw(plot_plans={})  # 模型完全没提口粮
    result = compile_draft(raw, state, "p1", cfg)
    _assert_compiled_ok(result, state, cfg)

    from engine.population import compute_population

    pop = compute_population(state, "p1", {}, cfg)
    assert pop.baseline_food > 0
    for g, ratio in cfg.food.baseline_quota.items():
        want = ratio * pop.baseline_food
        assert abs(result.plan.food.get(g, 0.0) - want) < TOL, (
            f"口粮 {g} 应按基准档补 {want}，实得 {result.plan.food.get(g)}"
        )
    assert any("口粮" in n for n in result.notes), result.notes


def test_model_food_is_not_overwritten():
    """模型明确给了口粮 → 不要用默认值覆盖它（只做合法性过滤）。"""
    cfg = load_config()
    state = _mid_state(cfg)
    raw = _raw(food={"A": 2.0, "B": 1.0})
    result = compile_draft(raw, state, "p1", cfg)
    _assert_compiled_ok(result, state, cfg)
    assert abs(result.plan.food["A"] - 2.0) < TOL
    assert abs(result.plan.food["B"] - 1.0) < TOL
    assert "C" not in result.plan.food and "D" not in result.plan.food


def test_excess_food_is_capped_to_good_need():
    """真跑里模型把“22 人”写成 `food: {"A": 22}` —— 必须压回优秀档上限。

    多买的口粮没有仓库可放，结算第 7.7 步会直接丢弃（白烧钱），
    所以编译器按 `good_quota × F_优秀` 封顶，而不是照单全收。
    """
    cfg = load_config()
    state = _mid_state(cfg)
    raw = _raw(food={"A": 22.0})
    result = compile_draft(raw, state, "p1", cfg)
    _assert_compiled_ok(result, state, cfg)

    from engine.population import compute_population

    pop = compute_population(state, "p1", {}, cfg)
    cap = cfg.food.good_quota["A"] * pop.good_food
    assert result.plan.food["A"] <= cap + TOL, (
        f"口粮 A 应压到优秀档上限 {cap}，实得 {result.plan.food['A']}"
    )
    assert any("口粮" in n and "压回" in n for n in result.notes), (
        f"必须说明口粮被压回：{result.notes}"
    )
    # 压回后应该还能开工（而不是为了 22 单位口粮把生产全停掉）
    assert result.plan.plot_plans, "不能把 5 块地全停掉来买 22 单位 A"


# ══════════════════════════════════════════════════════════════════
# 五-b、建后不可更换：升级时不能顺手换产物/路线（§5.1）
# ══════════════════════════════════════════════════════════════════


def test_agriculture_upgrade_cannot_change_product():
    """农业 1→2 写 product=B：规则上只能继续种 A，编译器必须归位。"""
    from engine.models import PlotType

    cfg = load_config()
    state = make_state(cfg, "p1", mature("0,0", PlotType.AGRICULTURE, 1, product="A"),
                       cash=1000.0)
    raw = _raw(builds=[{
        "plot_id": "0,0", "plot_type": "agriculture",
        "target_level": 2, "product": "B",
    }])
    result = compile_draft(raw, state, "p1", cfg)
    _assert_compiled_ok(result, state, cfg)
    assert result.plan.builds[0].product == "A", (
        f"农业升级不能改种，实得 {result.plan.builds[0].product!r}"
    )
    assert any("不可更换" in n or "只能继续" in n for n in result.notes), result.notes


def test_industry_upgrade_keeps_current_route():
    """工业 2→3：当前机械线 H-A，模型写 H-F+ → 必须压回 H-A+。"""
    from engine.models import PlotType

    cfg = load_config()
    state = make_state(cfg, "p1", mature("0,0", PlotType.INDUSTRY, 2, product="H-A"),
                       cash=2000.0)
    raw = _raw(builds=[{
        "plot_id": "0,0", "plot_type": "industry",
        "target_level": 3, "product": "H-F+",
    }])
    result = compile_draft(raw, state, "p1", cfg)
    _assert_compiled_ok(result, state, cfg)
    assert result.plan.builds[0].product == "H-A+", (
        f"工业 2→3 必须沿当前路线，实得 {result.plan.builds[0].product!r}"
    )
    assert any("不可更换" in n or "只能继续" in n for n in result.notes), result.notes



# ══════════════════════════════════════════════════════════════════
# 五-c、空地自动选址：零加成 → 搬到推荐邻格（用户反馈 B，2026-10-18）
# ══════════════════════════════════════════════════════════════════


def test_zero_bonus_build_is_moved_next_to_existing_plot():
    """模型把农地建在 `0,0`（零加成）→ 编译器搬到推荐邻格 `4,4` 并写 note。"""
    cfg = load_config()
    state = _farm_state(cfg)
    raw = _raw(builds=[{
        "plot_id": "0,0", "plot_type": "agriculture",
        "target_level": 1, "product": "A",
    }])
    result = compile_draft(raw, state, "p1", cfg)
    _assert_compiled_ok(result, state, cfg)

    assert len(result.plan.builds) == 1
    assert result.plan.builds[0].plot_id == "4,4", (
        f"零加成的新建应搬到推荐邻格 4,4，实得 {result.plan.builds[0].plot_id}"
    )
    assert any(
        "为吃邻接加成" in n and "0,0" in n and "4,4" in n and "5,5" in n
        for n in result.notes
    ), f"搬迁必须说明原因与邻格：{result.notes}"
    # 只读：原局面没有被改
    assert set(state.player("p1").plots) == {"5,5"}


def test_build_already_on_bonus_cell_is_not_moved():
    """模型已经选了有加成的格子 → 不搬（不覆盖模型的判断）。"""
    cfg = load_config()
    state = _farm_state(cfg)
    raw = _raw(builds=[{
        "plot_id": "4,4", "plot_type": "agriculture",
        "target_level": 1, "product": "A",
    }])
    result = compile_draft(raw, state, "p1", cfg)
    _assert_compiled_ok(result, state, cfg)

    assert result.plan.builds[0].plot_id == "4,4"
    assert not any("为吃邻接加成" in n for n in result.notes), result.notes


def test_second_build_takes_next_recommended_cell():
    """同计划两条 build 都零加成 → 第一条占 4,4，第二条顺位拿 4,5（不撞车）。"""
    cfg = load_config()
    state = _farm_state(cfg)
    raw = _raw(builds=[
        {"plot_id": "0,0", "plot_type": "agriculture",
         "target_level": 1, "product": "A"},
        {"plot_id": "0,1", "plot_type": "agriculture",
         "target_level": 1, "product": "A"},
    ])
    result = compile_draft(raw, state, "p1", cfg)
    _assert_compiled_ok(result, state, cfg)

    ids = [b.plot_id for b in result.plan.builds]
    assert ids == ["4,4", "4,5"], f"两条建造必须落在不同推荐格，实得 {ids}"
    assert sum("为吃邻接加成" in n for n in result.notes) == 2, result.notes


def test_upgrade_is_never_relocated():
    """升级不搬：即使原格零加成，也只升它自己（`plot_plans` 不碰）。"""
    cfg = load_config()
    state = make_state(
        cfg, "p1", mature("0,0", PlotType.AGRICULTURE, 1, product="A"),
        cash=2000.0,
    )
    raw = _raw(builds=[{
        "plot_id": "0,0", "plot_type": "agriculture",
        "target_level": 2, "product": "A",
    }])
    result = compile_draft(raw, state, "p1", cfg)
    _assert_compiled_ok(result, state, cfg)

    assert result.plan.builds[0].plot_id == "0,0", "升级指令不许被搬走"
    assert not any("为吃邻接加成" in n for n in result.notes), result.notes


def test_auto_place_can_be_disabled():
    """`auto_place=False` → 完全照模型给的坐标（零加成也认）。"""
    cfg = load_config()
    state = _farm_state(cfg)
    raw = _raw(builds=[{
        "plot_id": "0,0", "plot_type": "agriculture",
        "target_level": 1, "product": "A",
    }])
    result = compile_draft(raw, state, "p1", cfg, auto_place=False)
    _assert_compiled_ok(result, state, cfg)

    assert result.plan.builds[0].plot_id == "0,0"
    assert not any("为吃邻接加成" in n for n in result.notes), result.notes


# ══════════════════════════════════════════════════════════════════
# 六、失败路径：preview 非资金错误 → 明确失败，不返回坏计划
# ══════════════════════════════════════════════════════════════════


def test_non_cash_preview_error_fails_without_plan():
    cfg = load_config()
    state = _opening_state(cfg)
    raw = _raw(builds=[{"plot_id": "0,0", "plot_type": "agriculture",
                        "target_level": 1, "product": "A"}])

    import ai.plan_compile as pc
    orig = pc.pipeline.preview

    def boom(*a, **k):
        raise RuntimeError("boom")

    pc.pipeline.preview = boom
    try:
        result = compile_draft(raw, state, "p1", cfg)
    finally:
        pc.pipeline.preview = orig

    assert result.ok is False
    assert result.plan is None, "试算炸了绝不许把计划交给前端"
    assert result.issues and result.issues[0]["code"] == "preview_failed"


# ══════════════════════════════════════════════════════════════════
# 七、只读 / player_id 钉死
# ══════════════════════════════════════════════════════════════════


def test_compile_is_readonly_and_pins_player_id():
    cfg = load_config()
    state = _mid_state(cfg)
    before = copy.deepcopy(state.to_dict())
    raw = _raw(player_id="hacker", builds=[])
    result = compile_draft(raw, state, "p1", cfg)
    assert result.ok is True
    assert result.plan.player_id == "p1", "模型乱写 player_id 必须被钉死"
    assert state.to_dict() == before, "★ 编译器不许改状态"


# ══════════════════════════════════════════════════════════════════
# runner（与 tests/test_ai_host.py 同写法）
# ══════════════════════════════════════════════════════════════════


def _run_all() -> int:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    mod = sys.modules[__name__]
    names = sorted(n for n in dir(mod) if n.startswith("test_"))
    ok = fail = 0
    print("=" * 68)
    print("AI 起草编译器")
    print("=" * 68)
    for n in names:
        try:
            getattr(mod, n)()
            print(f"  PASS  {n}")
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"  FAIL  {n}\n        {type(e).__name__}: {e}")
            fail += 1
    print(f"\n{ok} passed, {fail} failed, {len(names)} total")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
