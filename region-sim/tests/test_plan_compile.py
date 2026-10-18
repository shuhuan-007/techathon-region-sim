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

from ai.plan_compile import compile_draft, population_after_completion
from engine import pipeline, validate
from engine.loader import load_config
from engine.models import GameState, Plot, PlotStatus, PlotType, PlayerState
from engine.population import compute_population
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


def test_missing_food_defaults_to_good_tier_need():
    """★ 2026-10-18 AI-food-default-excellent：模型完全不写 food 时，
    新默认 = `_fill_baseline_food` → `_fill_good_food` 接力，最终把
    A/B/C/D 补到**优秀档** `good_quota × F_优秀`。

    （原版本断言「按基准档补 A+B」——与新默认直接冲突，按新语义改写；
    基准档路径见 `test_baseline_only_via_flag`。）
    """
    cfg = load_config()
    state = _mid_state(cfg)
    # 避开 shrink 干扰，直接看数值（与其他优秀档用例同口径）
    state.player("p1").cash = 10000.0
    raw = _raw(plot_plans={})  # 模型完全没提口粮
    result = compile_draft(raw, state, "p1", cfg)
    _assert_compiled_ok(result, state, cfg)

    from engine.population import compute_population

    pop = compute_population(state, "p1", {}, cfg)
    assert pop.good_food > 0
    # A/B/C/D 四样都必须到优秀档
    for g, quota in cfg.food.good_quota.items():
        want = quota * pop.good_food
        assert abs(result.plan.food.get(g, 0.0) - want) < TOL, (
            f"口粮 {g} 应默认到优秀档 {want}，实得 {result.plan.food.get(g)}"
        )
    assert any("口粮" in n for n in result.notes), result.notes


def test_model_food_below_good_tier_is_topped_up():
    """★ 2026-10-18 AI-food-default-excellent：模型给的低于优秀档的口粮，
    在新默认下会被 `_fill_good_food` **向上补到优秀档**（只补不足，不削已有）。

    （原版本断言「模型值原样保留」——与新默认直接冲突；「给高了不被压低」
    见 `test_include_excellent_food_does_not_cut_existing_food`。）
    """
    cfg = load_config()
    state = _mid_state(cfg)
    # 避开 shrink 干扰
    state.player("p1").cash = 10000.0
    # A=2.0 < 优秀档 2.64；B=1.0 < 优秀档 1.32；C/D 没给
    raw = _raw(food={"A": 2.0, "B": 1.0})
    result = compile_draft(raw, state, "p1", cfg)
    _assert_compiled_ok(result, state, cfg)

    from engine.population import compute_population
    pop = compute_population(state, "p1", {}, cfg)

    # A/B 都被向上补到优秀档；C/D 也从 0 补到优秀档
    for g, quota in cfg.food.good_quota.items():
        want = quota * pop.good_food
        assert abs(result.plan.food.get(g, 0.0) - want) < TOL, (
            f"模型给低的 {g} 应被向上补到优秀档 {want}，"
            f"实得 {result.plan.food.get(g)}"
        )
    # 不会被削：模型给的 A=2.0 至少是下限
    assert result.plan.food["A"] >= 2.0 - TOL


def test_include_excellent_food_fills_good_quota_for_all_four():
    """★ 2026-10-18 用户反馈：AI 不知道什么是「优秀档」，不懂得把闲钱花出去。

    `include_excellent_food=True` 时，compile_draft 必须在基准档补齐之后，
    再把 A/B/C/D **四样**口粮按 `good_quota × F_优秀` 补到优秀档
    （只补不足，不削已有），并留「按优秀档补口粮」note。
    """
    cfg = load_config()
    state = _mid_state(cfg)
    # mid 局现金 416：优秀档口粮 + 5 块地全开工可能超预算 → 缩量阶梯会先砍建造再降
    # u%；但它**不是**先动口粮（`_FOOD_LADDER` 是先降 u%、最后才动口粮）。
    # 所以现金不是本测试的核心 —— 把现金留足，避开 shrink 干扰，直接看数值。
    state.player("p1").cash = 10000.0

    from engine.population import compute_population
    pop = compute_population(state, "p1", {}, cfg)

    result = compile_draft(_raw(), state, "p1", cfg, include_excellent_food=True)
    _assert_compiled_ok(result, state, cfg)

    # 四样口粮都必须达到优秀档需求：good_quota[g] × F_优秀
    for g, quota in cfg.food.good_quota.items():
        want = quota * pop.good_food
        got = result.plan.food.get(g, 0.0)
        assert abs(got - want) < TOL, (
            f"include_excellent_food=True 时口粮 {g} 应补到优秀档 {want}，实得 {got}"
        )
    # 与基准档的区分：C/D 基准档没有、优秀档必须有
    assert result.plan.food.get("C", 0.0) > 0, "优秀档必须有 C"
    assert result.plan.food.get("D", 0.0) > 0, "优秀档必须有 D"
    assert any("按优秀档补口粮" in n for n in result.notes), (
        f"必须留下「按优秀档补口粮」的说明：{result.notes}"
    )


def test_default_fills_good_tier_by_default():
    """★ 2026-10-18 AI-food-default-excellent：不传 include_excellent_food
    时默认 True —— 与旧的「显式 True」路径完全一致：A/B/C/D 四样补到
    `good_quota × F_优秀`，并留下「按优秀档补口粮」note。

    原回归测试「默认不加 C/D」与新默认直接冲突，按新语义改写。
    """
    cfg = load_config()
    state = _mid_state(cfg)
    # 与 test_include_excellent_food_fills_good_quota_for_all_four 同理，
    # 把现金留足避开 shrink 干扰，直接看数值。
    state.player("p1").cash = 10000.0

    from engine.population import compute_population
    pop = compute_population(state, "p1", {}, cfg)

    result = compile_draft(_raw(), state, "p1", cfg)
    _assert_compiled_ok(result, state, cfg)

    # 四样口粮都必须达到优秀档需求：good_quota[g] × F_优秀
    for g, quota in cfg.food.good_quota.items():
        want = quota * pop.good_food
        got = result.plan.food.get(g, 0.0)
        assert abs(got - want) < TOL, (
            f"默认（True）时口粮 {g} 应补到优秀档 {want}，实得 {got}"
        )
    # 与基准档的区分：C/D 基准档没有、优秀档必须有
    assert result.plan.food.get("C", 0.0) > 0, "默认就必须有 C"
    assert result.plan.food.get("D", 0.0) > 0, "默认就必须有 D"
    assert any("按优秀档补口粮" in n for n in result.notes), (
        f"默认必须留下「按优秀档补口粮」的说明：{result.notes}"
    )


def test_baseline_only_via_flag():
    """★ 2026-10-18 AI-food-default-excellent：显式传 False → 只补基准档
    A+B，C/D 一律不进 plan.food。"""
    cfg = load_config()
    state = _mid_state(cfg)
    state.player("p1").cash = 10000.0

    result = compile_draft(_raw(), state, "p1", cfg, include_excellent_food=False)
    _assert_compiled_ok(result, state, cfg)
    assert "C" not in result.plan.food, "显式 False 不许加 C"
    assert "D" not in result.plan.food, "显式 False 不许加 D"
    assert not any("按优秀档补口粮" in n for n in result.notes), (
        f"显式 False 不该出现优秀档 note：{result.notes}"
    )
    # A/B 仍按基准档补
    from engine.population import compute_population
    pop = compute_population(state, "p1", {}, cfg)
    for g, quota in cfg.food.baseline_quota.items():
        assert abs(result.plan.food[g] - quota * pop.baseline_food) < TOL


def test_include_excellent_food_does_not_cut_existing_food():
    """开关只补不足、不削已有：模型给了高于优秀档的 A，不能压回（也压不回，
    因为 `_cap_food_to_need` 已经反复压过）。"""
    cfg = load_config()
    state = _mid_state(cfg)
    state.player("p1").cash = 10000.0

    from engine.population import compute_population
    pop = compute_population(state, "p1", {}, cfg)
    # 模型给 A 是优秀档需要量的 2 倍 —— 但 `_cap_food_to_need` 会把它压回上限。
    # 所以这里选择的策略：给一个**低于**上限、但**高于**基准档的 A；
    # 期望开关把它**再补到上限**（只向上，不向下）。
    model_a = 0.6 * pop.good_food * cfg.food.good_quota["A"]
    result = compile_draft(
        _raw(food={"A": model_a}), state, "p1", cfg,
        include_excellent_food=True,
    )
    _assert_compiled_ok(result, state, cfg)
    want_a = cfg.food.good_quota["A"] * pop.good_food
    assert abs(result.plan.food["A"] - want_a) < TOL, (
        f"开关应该把 A 补到优秀档 {want_a}，实得 {result.plan.food['A']}"
    )


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
# 七、H=1.0 兜底检测（2026-10-18 第 5 步新增）
# ══════════════════════════════════════════════════════════════════


def test_h_shortfall_appends_note_when_cash_forces_low_tier():
    """意图是优秀档（include_excellent_food=True）但现金不够买齐四样时：
    preview 显示 H<1.0，编译器必须 append 一条 note 暴露缺口，
    且**不改 Plan 本身**（兜底的语义就是"只报告，不修复"）。
    """
    from engine.population import compute_population

    cfg = load_config()
    state = _mid_state(cfg)
    # 把现金压到只够 A/B，不够四样优秀档
    state.player("p1").cash = 30.0
    raw = _raw()  # 空草稿：什么都没有 → 编译器仍会默认补口粮/开工
    result = compile_draft(raw, state, "p1", cfg, include_excellent_food=True)
    _assert_compiled_ok(result, state, cfg)

    pop = compute_population(state, "p1", {}, cfg)
    quotas = cfg.food.good_quota or {}
    # 算出"按优秀档应该达到"的量 vs 当前 plan 里的量
    has_short = any(
        float(q) * pop.good_food > float(result.plan.food.get(g, 0.0) or 0.0) + 1e-6
        for g, q in quotas.items()
        if float(q) * pop.good_food > 0
    )
    if has_short:
        assert any(
            "按优秀档仍缺" in n for n in result.notes
        ), f"现金不够买齐四样时必须 append 兜底 note：{result.notes}"
        # 且 preview 的 H 必须 < 1.0（否则这条 note 不该出现）
        assert result.preview["food"]["happiness"] < 1.0 - 1e-9, result.preview["food"]


def test_h_full_skips_note_when_cash_is_plenty():
    """现金足够买齐四样时：preview H 应为 1.0，兜底 note **不应**出现。"""
    cfg = load_config()
    state = _mid_state(cfg)
    state.player("p1").cash = 5000.0     # 非常充裕
    state.player("p1").last_happiness = 0.8
    raw = _raw()
    result = compile_draft(raw, state, "p1", cfg, include_excellent_food=True)
    _assert_compiled_ok(result, state, cfg)
    # H=1.0 → 兜底 note 不该出现
    assert not any("按优秀档仍缺" in n for n in result.notes), (
        f"现金足够且 H=1.0 时不该有兜底 note：{result.notes}"
    )


# ══════════════════════════════════════════════════════════════════
# 八、完工后人口（★ 2026-10-18 R2）
#   结算第 1 步**先完工**（`_advance_construction`）→ 第 4 步才算人口，
#   所以编译侧的两档 F 必须按「完工后」算；否则本季完工地块的胃口没算进去，
#   口粮买少、H 掉档（实测 8550：应填 A=69.83，只填 66.41 → H=0.6）。
# ══════════════════════════════════════════════════════════════════


def _completing_state(cfg, cash: float = 2000.0):
    """1 块运行农业 `5,5` + 1 块**本季完工**的农业 `5,6`（left==1、1→2）。"""
    plots = {
        "5,5": mature("5,5", PlotType.AGRICULTURE, 1, product="A"),
        "5,6": Plot(
            plot_id="5,6", plot_type=PlotType.AGRICULTURE, level=1,
            status=PlotStatus.CONSTRUCTING, running_quarters=0,
            construction_left=1, target_level=2, product="A",
        ),
    }
    player = PlayerState(player_id="p1", cash=cash, plots=plots, last_happiness=0.8)
    return GameState(world=cfg.new_world(), players={"p1": player})


def test_population_after_completion_matches_engine_step1():
    """投影人口必须与**引擎第 1 步真跑**之后的人口逐位相同（不另写公式）。

    顺带锁两条：`construction_left == 2` 的地块本季**不**完工、不许计入；
    `population_after_completion()` 纯只读。
    """
    cfg = load_config()
    state = _completing_state(cfg)
    # left==2 → 第 1 步只减到 1，本季仍在建（不该进人口）
    state.player("p1").plots["5,7"] = Plot(
        plot_id="5,7", plot_type=PlotType.RESOURCE, level=1,
        status=PlotStatus.CONSTRUCTING, running_quarters=3,
        construction_left=2, target_level=2,
    )
    before = copy.deepcopy(state.to_dict())
    projected = population_after_completion(state, "p1", cfg)
    assert state.to_dict() == before, "★ population_after_completion 不许改状态"

    engine_state = GameState.from_dict(copy.deepcopy(before))
    events = pipeline._advance_construction(engine_state, "p1", cfg)   # 引擎第 1 步
    assert [e["plot_id"] for e in events] == ["5,6"], (
        f"夹具里应该只有 5,6 本季完工：{events}"
    )
    after = compute_population(engine_state, "p1", {}, cfg)
    assert projected.to_dict() == after.to_dict(), (
        f"★ 投影人口 ≠ 引擎第 1 步之后的人口："
        f"{projected.to_dict()} != {after.to_dict()}"
    )

    pre = compute_population(state, "p1", {}, cfg)
    assert projected.labor_food > pre.labor_food + 1e-9, "完工的地块必须带进岗位"
    assert projected.working_plots == 2, projected  # 含 5,6、不含 left==2 的 5,7


def test_food_defaults_use_post_completion_population():
    """默认补口粮按**完工后** `F_优秀`（不是当前 RUNNING 的 F），且 H=1.0。"""
    cfg = load_config()
    state = _completing_state(cfg)
    pre = compute_population(state, "p1", {}, cfg)
    post = population_after_completion(state, "p1", cfg)
    assert post.good_food > pre.good_food + 1e-9, "夹具必须真的本季完工"

    result = compile_draft(_raw(), state, "p1", cfg)
    _assert_compiled_ok(result, state, cfg)
    for g, quota in cfg.food.good_quota.items():
        want = quota * post.good_food
        got = float(result.plan.food.get(g, 0.0) or 0.0)
        assert abs(got - want) < TOL, (
            f"★ 口粮 {g} 应按完工后优秀档 {want:.4f} 补，实得 {got:.4f}"
            f"（按完工前只会是 {quota * pre.good_food:.4f}）"
        )
    assert result.preview["food"]["good_met"] is True, result.preview["food"]
    assert result.preview["food"]["happiness"] >= 1.0 - 1e-9, result.preview["food"]


def test_baseline_food_fill_uses_post_completion_population():
    """`include_excellent_food=False`（只要基准档）时，A/B 也按完工后 `F_基准`。"""
    cfg = load_config()
    state = _completing_state(cfg)
    post = population_after_completion(state, "p1", cfg)

    result = compile_draft(_raw(), state, "p1", cfg, include_excellent_food=False)
    _assert_compiled_ok(result, state, cfg)
    assert set(result.plan.food) == set(cfg.food.baseline_quota), result.plan.food
    for g, quota in cfg.food.baseline_quota.items():
        want = quota * post.baseline_food
        assert abs(result.plan.food[g] - want) < TOL, (
            f"★ 基准档 {g} 应按完工后 {want:.4f} 补，实得 {result.plan.food[g]}"
        )


def test_build_this_season_is_not_counted_in_food():
    """当季**新建**的地块第 6 步才创建 → 第 4 步人口看不到，不许预扣口粮。"""
    cfg = load_config()
    state = make_state(
        cfg, "p1", mature("5,5", PlotType.AGRICULTURE, 1, product="A"), cash=2000.0,
    )
    raw = _raw(builds=[{
        "plot_id": "0,0", "plot_type": "agriculture",
        "target_level": 1, "product": "A",
    }])
    result = compile_draft(raw, state, "p1", cfg)
    _assert_compiled_ok(result, state, cfg)
    assert len(result.plan.builds) == 1, "这条建造应该保留（只验口粮口径）"

    pop = compute_population(state, "p1", {}, cfg)   # 新建的地块不在里面
    for g, quota in cfg.food.good_quota.items():
        want = quota * pop.good_food
        got = float(result.plan.food.get(g, 0.0) or 0.0)
        assert abs(got - want) < TOL, (
            f"★ 本季新建不该算进胃口：{g} 应按 {want:.4f}，实得 {got:.4f}"
        )


def test_h_gap_note_uses_post_completion_need():
    """现金只够空计划时：H 兜底 note 的缺口必须按**完工后** F_优秀 算。

    旧口径按完工前 F 算，缺口会偏小（8550 那种 4 块完工的档甚至算成 0 →
    连 note 都不加）；这里直接用「0.4/0.2/0.1/0.02 × 完工后 F_优秀」对账。
    """
    cfg = load_config()
    state = _completing_state(cfg, cash=30.0)     # 只够「什么都不做」
    post = population_after_completion(state, "p1", cfg)

    result = compile_draft(_raw(), state, "p1", cfg)
    _assert_compiled_ok(result, state, cfg)
    h = result.preview["food"]["happiness"]
    assert h < 1.0 - 1e-9, f"这条夹具的 H 必须 <1.0：{result.preview['food']}"
    note = next((n for n in result.notes if "按优秀档仍缺" in n), "")
    assert note, f"★ H<1.0 时必须 append 缺口 note：{result.notes}"
    for g, quota in cfg.food.good_quota.items():
        want = quota * post.good_food
        have = float(result.plan.food.get(g, 0.0) or 0.0)
        if have + 1e-6 < want:
            assert f"{g} 缺 {want - have:.2f}" in note, (
                f"★ 缺口 {g} 应按完工后 {want:.2f} 算（缺口 {want - have:.2f}）：{note}"
            )


# ══════════════════════════════════════════════════════════════════
# 九、只读 / player_id 钉死
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
