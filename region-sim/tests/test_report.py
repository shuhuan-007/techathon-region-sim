"""`/state` 报告对账 —— 前端拿到的数必须就是引擎算的数。

跑法（在 region-sim/ 下）：

    python tests/test_report.py

依据《界面要显示的变量清单》附录「数据从哪来」（`/state` 的形状）与
《规则基线》§0（程序决定事实，前端零计算）。

★ 本文件最重要的一条是 `test_projection_equals_real_settlement`：
  **界面上显示的本季预览，必须与真结算一分不差。**
  这是"前端只显示、零计算"能成立的前提。
"""

from __future__ import annotations

import io
import sys
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import report
from engine.loader import load_config
from engine.models import (
    GameState,
    Plan,
    PlayerState,
    Plot,
    PlotPlan,
    PlotStatus,
    PlotType,
    WorldPhase,
)
from engine.pipeline import advance

TOL = 1e-6


# ══════════════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════════════


def plot(pid: str, ptype: PlotType = PlotType.RESOURCE, level: int = 1,
         product=None) -> Plot:
    """一块**成熟**地块（磨合已满 → E_磨合 = 1.0）。"""
    return Plot(plot_id=pid, plot_type=ptype, level=level,
                status=PlotStatus.RUNNING, running_quarters=99, product=product)


def state_with(*plots: Plot, cash: float = 1000.0) -> GameState:
    """造一个单人局。`last_happiness = 0.8` → `E_幸福 = 1.00`（M1 题设）。"""
    cfg = load_config()
    player = PlayerState(
        player_id="p1", cash=cash,
        plots={p.plot_id: p for p in plots},
    )
    player.last_happiness = 0.8
    world = cfg.new_world()
    world.phase = WorldPhase.RUNNING
    return GameState(world=world, players={"p1": player})


def full_plan(*plot_ids: str, **kw) -> Plan:
    """把所有给定地块都按 u=1 安排的计划。"""
    return Plan(
        player_id="p1",
        plot_plans={pid: PlotPlan(plot_id=pid, u=1.0) for pid in plot_ids},
        **kw,
    )


# ══════════════════════════════════════════════════════════════════
# 一、★ 显示的数 == 真结算的数
# ══════════════════════════════════════════════════════════════════


def test_projection_equals_real_settlement():
    """★★ 界面上显示的本季预览，必须**一分不差**地等于真结算。

    这是整个"前端零计算"能成立的前提。如果这条挂了，
    玩家看到的"本季盈亏"就是假的 —— 比没有这个数字更糟。

    `build_state` 内部走 `preview()`，而 `preview()` 是"深拷贝后跑真引擎"，
    所以两者**必然**一致（见 `pipeline.preview` 的注释）。
    """
    cfg = load_config()
    st = state_with(plot("0,0"), plot("0,1", PlotType.INDUSTRY, product="G"))
    plan = full_plan("0,0", "0,1", purchases={"C": 2.0, "D": 1.0, "F": 3.0})

    shown = report.build_state(st, "p1", cfg, plan=plan)["projection"]

    # 真跑一遍结算（在副本上）
    shadow = st.deep_copy()
    advance(shadow, {"p1": plan}, cfg)
    actual = shadow.raw_reports["p1"]

    assert abs(shown["cash_after"] - actual.cash_after) < TOL, (
        f"★ 界面预览的期末现金 {shown['cash_after']} "
        f"≠ 真结算 {actual.cash_after}"
    )
    assert abs(shown["total_deduction"] - actual.total_deduction) < TOL
    assert abs(shown["sales_revenue"] - actual.sales_revenue) < TOL
    assert abs(shown["value_added"] - actual.value_added) < TOL


def test_projection_prices_match_settlement():
    """市场上显示的「本季求价 P_t」也必须等于真结算的价。"""
    cfg = load_config()
    st = state_with(plot("0,0"), plot("0,1", PlotType.INDUSTRY, product="G"))
    plan = full_plan("0,0", "0,1", purchases={"C": 2.0, "D": 1.0, "F": 3.0})

    shown = report.build_state(st, "p1", cfg, plan=plan)["market"]

    shadow = st.deep_copy()
    advance(shadow, {"p1": plan}, cfg)
    actual = shadow.raw_reports["p1"].prices

    for goods, pr in actual.items():
        assert abs(shown[goods]["current_price"] - pr["price"]) < TOL, (
            f"{goods} 的显示价 {shown[goods]['current_price']} "
            f"≠ 结算价 {pr['price']}"
        )


def test_projection_includes_finance_derived_fields():
    """本季收支预览要带上 `finance` 的派生量（工资税/净工资/GDP 的 P0 拆解）。

    这几个数**只**在 `finance.py` 里有一份实现，report 直接取，不重算。
    """
    cfg = load_config()
    st = state_with(plot("0,0"))
    proj = report.build_state(st, "p1", cfg, plan=full_plan("0,0"))["projection"]

    for key in ("wage_tax", "wage_net", "output_value_at_p0", "material_value_at_p0"):
        assert key in proj, f"projection 缺 {key}：{sorted(proj)}"

    # 甲区：工资 36 → 税 3.6、净 32.4；产出价值按 P0=24 算 = 96
    assert abs(proj["wage_tax"] - 3.6) < TOL
    assert abs(proj["wage_net"] - 32.4) < TOL
    assert abs(proj["output_value_at_p0"] - 96.0) < TOL
    assert abs(proj["wage_net"] - (proj["wages"] - proj["wage_tax"])) < TOL


def test_projection_prefers_report_values_over_finance_recompute():
    """★ 重复的键以**报告原值**为准，不用 finance 重算的那份。

    否则同一件事有两个来源，将来对不上时无法判断该信谁。
    """
    cfg = load_config()
    st = state_with(plot("0,0"))
    proj = report.build_state(st, "p1", cfg, plan=full_plan("0,0"))["projection"]

    shadow = st.deep_copy()
    advance(shadow, {"p1": full_plan("0,0")}, cfg)
    actual = shadow.raw_reports["p1"]

    for key, want in (("value_added", actual.value_added),
                      ("total_deduction", actual.total_deduction),
                      ("sales_revenue", actual.sales_revenue),
                      ("wages", actual.wages),
                      ("operating", actual.operating)):
        assert abs(proj[key] - want) < TOL, f"{key} 应取报告原值 {want}，实得 {proj[key]}"


# ══════════════════════════════════════════════════════════════════
# 二、只读
# ══════════════════════════════════════════════════════════════════


def test_build_state_does_not_mutate():
    """★ `build_state` **绝不改状态**（试算走 preview，内部深拷贝）。"""
    cfg = load_config()
    st = state_with(plot("0,0"), cash=500.0)
    plan = full_plan("0,0", food={"A": 1.2, "B": 0.6})

    before = deepcopy(st.to_dict())
    report.build_state(st, "p1", cfg, plan=plan)
    assert st.to_dict() == before, "★ build_state 必须不改状态"


def test_build_state_does_not_mutate_without_plan():
    """没有计划时同样不改状态。"""
    cfg = load_config()
    st = state_with(plot("0,0"))
    before = deepcopy(st.to_dict())
    report.build_state(st, "p1", cfg)
    assert st.to_dict() == before


# ══════════════════════════════════════════════════════════════════
# 三、没有计划时**不编数字**
# ══════════════════════════════════════════════════════════════════


def test_no_plan_gives_none_not_zero():
    """★ 没提交计划 → 本季预览一律 `None`，**不是 0**。

    ⚠️ 给 0 会被读成"本季确实不产/不赚钱"，那是在**编造事实**。
    """
    cfg = load_config()
    st = state_with(plot("0,0"))
    out = report.build_state(st, "p1", cfg, plan=None)

    assert out["population"] is None, "没计划时人口应为 None，不是 0"
    assert out["efficiency"] is None, "没计划时平均效率应为 None，不是 0"
    assert out["projection"] is None, "没计划时本季收支应为 None，不是 0"
    assert out["plots"][0]["u"] is None, "没计划时开工比例应为 None，不是 0"
    assert out["plots"][0]["output_preview"] is None


def test_no_plan_still_shows_map_and_market():
    """★ 没计划也要能看地图、市场、上季报告 —— 否则玩家进不了游戏。"""
    cfg = load_config()
    st = state_with(plot("0,0"))
    out = report.build_state(st, "p1", cfg, plan=None)

    assert out["cash"] == 1000.0
    assert out["quarter"] == 1
    assert out["total_quarters"] == 20
    assert len(out["plots"]) == 1
    assert set(out["market"]) == set(cfg.all_goods())
    # 效率**不开工也算** —— 玩家要知道"这块地效率多少、该不该开"
    assert out["plots"][0]["efficiency"] is not None
    assert out["plots"][0]["efficiency"]["efficiency"] > 0


# ══════════════════════════════════════════════════════════════════
# 四、can_settle（✅ 2026-10-16 用户裁定：不安排 = 不开工，合法）
# ══════════════════════════════════════════════════════════════════


def test_can_settle_requires_plan():
    """没提交计划 → 不能推进。"""
    cfg = load_config()
    st = state_with(plot("0,0"))
    out = report.build_state(st, "p1", cfg, plan=None)
    assert out["can_settle"] is False
    assert out["settle_blockers"] == ["尚未提交计划"]


def test_unarranged_plots_are_legal():
    """★★ **不安排 = 不开工，是合法的**（2026-10-16 用户裁定）。

    《前端设计规范》§2.3 曾建议"每块地都已安排"才能推进，但那一节
    自己标着「（建议）」、引的《回合循环与状态机》已删除，
    **《规则基线》里没有这条规则**。

    → 有 3 块地、只安排 1 块，**必须仍然可以推进**。
    """
    cfg = load_config()
    st = state_with(plot("0,0"), plot("0,1"), plot("0,2"))
    plan = full_plan("0,0")          # 只安排一块

    out = report.build_state(st, "p1", cfg, plan=plan)
    assert out["can_settle"] is True, (
        f"★ 不安排的地块＝不开工，不该挡住推进。实得 blockers：{out['settle_blockers']}"
    )
    assert out["settle_blockers"] == []

    # 未安排的地块 u = 0（不是 None —— 计划已提交，就是"本季不开工"）
    u_by_plot = {p["plot_id"]: p["u"] for p in out["plots"]}
    assert abs(u_by_plot["0,1"] - 0.0) < TOL
    assert abs(u_by_plot["0,2"] - 0.0) < TOL


def test_invalid_plan_blocks_settle():
    """计划不合法（§11.3）→ 不能推进，并给出原因。"""
    cfg = load_config()
    st = state_with(plot("0,0"))
    plan = Plan(player_id="p1", purchases={"ZZZ": 1.0})
    out = report.build_state(st, "p1", cfg, plan=plan)
    assert out["can_settle"] is False
    assert any("ZZZ" in b for b in out["settle_blockers"]), out["settle_blockers"]


def test_insufficient_cash_blocks_settle_without_crashing():
    """★ 钱不够时**不能崩**，要把它变成一条 blocker。

    构造方式：让 `validate` 的**粗校验**（按参考价 P0 估**采购**）放行，
    但真结算（采购 + 工资 + 经营费）不够 ——
    这正是"粗校验"与"最终判据"分工的体现。
    """
    cfg = load_config()
    # 现金 100；F 的 P0 = 24 → 买 4 个粗估 96 ≤ 100，validate 放行
    # 但结算还要加工资 36 + 经营费 30 → 总 162 > 100
    st = state_with(plot("0,0"), cash=100.0)
    plan = full_plan("0,0", purchases={"F": 4.0})

    out = report.build_state(st, "p1", cfg, plan=plan)
    assert out["can_settle"] is False, "钱不够却允许推进"
    assert out["settle_blockers"], "钱不够却没给出原因"
    assert any("资金不足" in b for b in out["settle_blockers"]), out["settle_blockers"]
    # 预览块仍在，但带上 error（前端要能显示"为什么不能推进"）
    assert out["projection"]["error"] is not None


def test_can_settle_true_for_plain_plan():
    """一份普通可行的计划 → 可以推进。"""
    cfg = load_config()
    st = state_with(plot("0,0"))
    out = report.build_state(st, "p1", cfg, plan=full_plan("0,0"))
    assert out["can_settle"] is True, out["settle_blockers"]


# ══════════════════════════════════════════════════════════════════
# 五、地块列表
# ══════════════════════════════════════════════════════════════════


def test_plots_sorted_by_id():
    """★ 地块按 ID 排序 —— **确定性**（§0 同输入同输出）。"""
    cfg = load_config()
    st = state_with(plot("3,3"), plot("1,1"), plot("2,2"))
    out = report.build_state(st, "p1", cfg)
    assert [p["plot_id"] for p in out["plots"]] == ["1,1", "2,2", "3,3"]


def test_plot_efficiency_breakdown_is_complete():
    """每块地都要带**四分量拆解**（界面清单 §四 第 19 条悬停提示）。"""
    cfg = load_config()
    st = state_with(plot("0,0"))
    eff = report.build_state(st, "p1", cfg)["plots"][0]["efficiency"]
    for key in ("adjacency", "happiness", "ramp_up", "research_multiplier", "efficiency"):
        assert key in eff, f"效率拆解缺 {key}：{sorted(eff)}"
    # 四分量相乘 == 总效率（口径自洽）
    product = (eff["adjacency"] * eff["happiness"]
               * eff["ramp_up"] * eff["research_multiplier"])
    assert abs(product - eff["efficiency"]) < TOL, (
        f"四分量乘积 {product} ≠ 总效率 {eff['efficiency']}"
    )


def test_service_plot_has_no_output():
    """★ 服务地块**没有产出**（0→0）—— 界面不能给它画产出。"""
    cfg = load_config()
    st = state_with(plot("0,0", PlotType.SERVICE))
    out = report.build_state(st, "p1", cfg, plan=full_plan("0,0"))
    prev = out["plots"][0]["output_preview"]
    assert prev["goods"] is None, f"服务不该有产出商品，实得 {prev['goods']}"
    assert abs(prev["qty"] - 0.0) < TOL


# ══════════════════════════════════════════════════════════════════
# 六、市场块
# ══════════════════════════════════════════════════════════════════


def test_market_has_all_goods_with_required_fields():
    """市场块要覆盖**全部 15 个商品**，且字段齐全（界面清单 §五）。"""
    cfg = load_config()
    st = state_with(plot("0,0"))
    market = report.build_state(st, "p1", cfg)["market"]

    assert len(market) == len(cfg.all_goods()) == 15
    for goods, block in market.items():
        for key in ("name", "P0", "last_price", "P_min", "current_price", "at_floor"):
            assert key in block, f"{goods} 缺 {key}"
        assert abs(block["P_min"] - 0.1 * block["P0"]) < TOL, "价格下限 = 0.1 × P0"


def test_first_quarter_last_price_falls_back_to_p0():
    """开局第一季没有"上期价" → 退回参考价 P0（§8.1 初始条件）。"""
    cfg = load_config()
    st = state_with(plot("0,0"))
    market = report.build_state(st, "p1", cfg)["market"]
    for goods, block in market.items():
        assert abs(block["last_price"] - block["P0"]) < TOL, (
            f"{goods} 第一季的上期价应为 P0"
        )


def test_current_price_is_none_without_plan():
    """没计划 → 本季求价给 `None`（**不编造**）。"""
    cfg = load_config()
    st = state_with(plot("0,0"))
    market = report.build_state(st, "p1", cfg)["market"]
    assert all(b["current_price"] is None for b in market.values())
    assert all(b["at_floor"] is None for b in market.values())


# ══════════════════════════════════════════════════════════════════
# 七、人口块
# ══════════════════════════════════════════════════════════════════


def test_population_keeps_decimals():
    """★ `N = L×2` **不取整** —— 引擎存小数，显示层负责取整。

    ⚠️ 若这里被 `round` 了，账就对不上（见界面清单 §三 与基线 §3.3.1）。
    """
    cfg = load_config()
    # 三级农业 d=1.5，u=0.5 → L = 0.75 → N = 1.5（带小数）
    st = state_with(plot("0,0", PlotType.AGRICULTURE, 3, product="A"))
    plan = Plan(player_id="p1", plot_plans={"0,0": PlotPlan("0,0", u=0.5)})
    pop = report.build_state(st, "p1", cfg, plan=plan)["population"]

    assert abs(pop["labor_total"] - 0.75) < TOL, f"L 应为 0.75，实得 {pop['labor_total']}"
    assert abs(pop["population"] - 1.5) < TOL, (
        f"★ N 应为 1.5（不取整），实得 {pop['population']}"
    )


def test_food_need_two_tiers():
    """两档需求 `F_优秀 = 1.5 × F_基准`（§3.3.3）。"""
    cfg = load_config()
    st = state_with(plot("0,0"))
    pop = report.build_state(st, "p1", cfg, plan=full_plan("0,0"))["population"]
    assert abs(pop["good_food"] - 1.5 * pop["baseline_food"]) < TOL


def test_average_efficiency_ignores_idle_plots():
    """★ 平均效率只统计**开工**地块 —— 把停工地块算进去会误导玩家。"""
    cfg = load_config()
    st = state_with(plot("0,0"), plot("0,1"))
    plan = full_plan("0,0")           # 只开 0,0
    out = report.build_state(st, "p1", cfg, plan=plan)
    only_one = out["plots"][0]["efficiency"]["efficiency"]
    assert abs(out["efficiency"] - only_one) < TOL, (
        "只开一块地时，平均效率应等于那一块的效率"
    )


def test_average_efficiency_none_when_all_idle():
    """一块都不开工 → 平均效率 `None`（**不是 0**）。"""
    cfg = load_config()
    st = state_with(plot("0,0"))
    plan = Plan(player_id="p1", plot_plans={"0,0": PlotPlan("0,0", u=0.0)})
    out = report.build_state(st, "p1", cfg, plan=plan)
    assert out["efficiency"] is None, "全停工时应为 None，不是 0"


# ══════════════════════════════════════════════════════════════════
# 八、地图
# ══════════════════════════════════════════════════════════════════


def test_build_map_covers_whole_10x10():
    """地图给**整张 10×10 = 100 格**（含别人的格子，画邻居要用）。"""
    cfg = load_config()
    st = state_with(plot("0,0"))
    m = report.build_map(st, "p1")
    assert m["rows"] == 10 and m["cols"] == 10
    assert len(m["cells"]) == 100

    mine = [c for c in m["cells"] if c["mine"]]
    assert len(mine) == 1 and mine[0]["plot_id"] == "0,0"


def test_build_map_marks_other_players():
    """别人的格子标 `owner`，但 `mine=False`（多人时看邻居要用）。"""
    cfg = load_config()
    p1 = PlayerState(player_id="p1", cash=1000.0, plots={"0,0": plot("0,0")})
    p2 = PlayerState(player_id="p2", cash=1000.0, plots={"5,5": plot("5,5")})
    world = cfg.new_world()
    world.phase = WorldPhase.RUNNING
    st = GameState(world=world, players={"p1": p1, "p2": p2})

    m = report.build_map(st, "p1")
    by_id = {c["plot_id"]: c for c in m["cells"]}
    assert by_id["0,0"]["owner"] == "p1" and by_id["0,0"]["mine"] is True
    assert by_id["5,5"]["owner"] == "p2" and by_id["5,5"]["mine"] is False


# ══════════════════════════════════════════════════════════════════
# 九、接进会话
# ══════════════════════════════════════════════════════════════════


def test_session_state_for_uses_report():
    """★ `GameSession.state_for()` 现在走 `report.build_state()`。

    此前它手搓了一个六字段的小字典，既盖不住界面清单要的 40 项，
    也把数字形状写在了服务层里（违反"服务层只转发"）。
    """
    from server.session import GameSession

    cfg = load_config()
    st = state_with(plot("0,0"))
    s = GameSession(state=st, cfg=cfg)

    out = s.state_for("p1")
    for key in ("quarter", "total_quarters", "cash", "happiness", "population",
                "efficiency", "plots", "market", "report", "projection",
                "can_settle", "settle_blockers", "phase"):
        assert key in out, f"state_for 缺字段 {key}"
    assert out["phase"] == "collecting"


def test_session_state_for_reflects_submitted_plan():
    """提交计划后，`state_for` 要**立刻**反映本季预览（不用等结算）。"""
    from server.session import GameSession

    cfg = load_config()
    st = state_with(plot("0,0"))
    s = GameSession(state=st, cfg=cfg)

    before = s.state_for("p1")
    assert before["can_settle"] is False

    s.submit("p1", full_plan("0,0"))
    after = s.state_for("p1")

    assert after["can_settle"] is True, after["settle_blockers"]
    assert after["population"] is not None
    assert after["projection"] is not None


def test_session_state_for_reads_plan_not_stale():
    """`state_for` 每次现算 —— 不会返回上一次的缓存。"""
    from server.session import GameSession

    cfg = load_config()
    st = state_with(plot("0,0"), plot("0,1"))
    s = GameSession(state=st, cfg=cfg)

    a = s.state_for("p1")
    assert a["plots"][0]["u"] is None

    s.submit("p1", full_plan("0,0"))
    b = s.state_for("p1")
    assert abs(b["plots"][0]["u"] - 1.0) < TOL
    assert abs(b["plots"][1]["u"] - 0.0) < TOL      # 未安排 = 不开工


# ══════════════════════════════════════════════════════════════════
# 命令行汇总
# ══════════════════════════════════════════════════════════════════


def _run_all() -> int:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    mod = sys.modules[__name__]
    names = sorted(n for n in dir(mod) if n.startswith("test_"))
    ok = fail = 0
    print("=" * 68)
    print("状态报告对账（/state）")
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
