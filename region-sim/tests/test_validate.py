"""计划校验对账 —— 无效 Plan 必须在**提交时**被挡住。

跑法（在 region-sim/ 下）：

    python tests/test_validate.py

依据《规则基线》§11.3：采购超限 / 材料不足 / 资金不足时**返回明确错误**，
**不自动减少**采购或开工量、**不产生部分状态变更**，
错误信息至少含「**错误类型 / 涉及商品或地块 / 申请值 / 可用值 / 差额**」。

⚠️ 本文件测的是**校验**，不是**结算**。
   资金那一条是**粗校验**（按参考价 P0 估），精确判据仍在第 5 步 ——
   见 `engine/validate.py` 模块头"三条设计口径"其三。
"""

from __future__ import annotations

import io
import sys
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import validate
from engine.loader import load_config
from engine.models import (
    BuildOrder,
    ContractAction,
    Plan,
    PlayerState,
    PlotPlan,
    PlotStatus,
    PlotType,
)
from engine.validate import PlanIssue, PlanRejected, buildable_products
from tests.test_m1 import mature

TOL = 1e-6


# ══════════════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════════════


def make_player(player_id: str = "p1", cash: float = 1000.0, **plots) -> PlayerState:
    """造一个玩家。`plots` 传 `plot_id=Plot`。

    ⚠️ **不设 `last_happiness`**：校验不看幸福度，那是结算的事。
    """
    return PlayerState(player_id=player_id, cash=cash, plots=dict(plots))


def plan_for(player_id: str = "p1", **kw) -> Plan:
    """一份默认**空**的计划（什么都不做 = 合法）。"""
    return Plan(player_id=player_id, **kw)


def codes(issues: list[PlanIssue]) -> set[str]:
    """取出问题码集合，便于断言"报了且只报了这些"。"""
    return {i.code for i in issues}


# ══════════════════════════════════════════════════════════════════
# 一、合法计划必须通过（别把好计划也拦了）
# ══════════════════════════════════════════════════════════════════


def test_empty_plan_is_valid():
    """什么都不做**是合法的**（玩家有权躺平一季）。"""
    cfg = load_config()
    issues = validate.validate_plan(plan_for(), make_player(), cfg)
    assert issues == [], f"空计划应当合法，实得：{[str(i) for i in issues]}"


def test_resource_plot_needs_no_product():
    """资源地块**没有多路线**，不该被要求填产出。"""
    cfg = load_config()
    player = make_player(**{"0,0": mature("0,0", PlotType.RESOURCE, 1)})
    plan = plan_for(plot_plans={"0,0": PlotPlan(plot_id="0,0", u=1.0)})
    issues = validate.validate_plan(plan, player, cfg)
    assert issues == [], f"资源地块开工应当合法，实得：{[str(i) for i in issues]}"


def test_agriculture_with_product_is_valid():
    """农业**填了产出**才合法（建后不可更换，所以建造时就定死了）。"""
    cfg = load_config()
    player = make_player(**{"0,0": mature("0,0", PlotType.AGRICULTURE, 1, product="A")})
    plan = plan_for(plot_plans={"0,0": PlotPlan(plot_id="0,0", u=1.0)})
    issues = validate.validate_plan(plan, player, cfg)
    assert issues == [], f"农业（产出 A）开工应当合法，实得：{[str(i) for i in issues]}"


def test_zero_u_skips_recipe_check():
    """`u=0`（不开工）**不该**因为查不到配方而报错。

    不开工的地块不消耗任何东西，配方根本用不上。
    这条防止"玩家关停一块有问题的地"反被拦住。
    """
    cfg = load_config()
    # 农业**没填产出** —— 若真的开工，配方查不出来
    player = make_player(**{"0,0": mature("0,0", PlotType.AGRICULTURE, 1, product=None)})
    plan = plan_for(plot_plans={"0,0": PlotPlan(plot_id="0,0", u=0.0)})
    issues = validate.validate_plan(plan, player, cfg)
    assert issues == [], f"u=0 不该查配方，实得：{[str(i) for i in issues]}"


# ══════════════════════════════════════════════════════════════════
# 二、提交者一致性（红线 1）
# ══════════════════════════════════════════════════════════════════


def test_player_mismatch():
    """计划里的 `player_id` 与提交者不一致 → 拒绝。"""
    cfg = load_config()
    plan = plan_for(player_id="p2")
    issues = validate.validate_plan(plan, make_player("p1"), cfg, player_id="p1")
    assert codes(issues) == {"player_mismatch"}, f"实得：{codes(issues)}"


def test_player_mismatch_returns_early():
    """提交者不一致时**只报这一条**，不刷一堆"地块不存在"的噪声。

    理由：整份计划可能是别人的，后面的逐地块校验**全都不可信**。
    """
    cfg = load_config()
    player = make_player("p1", **{"0,0": mature("0,0", PlotType.RESOURCE, 1)})
    plan = plan_for(
        player_id="p2",
        plot_plans={"9,9": PlotPlan(plot_id="9,9", u=1.0)},   # 不存在的地块
        purchases={"ZZZ": 5.0},                                # 不存在的商品
    )
    issues = validate.validate_plan(plan, player, cfg, player_id="p1")
    assert codes(issues) == {"player_mismatch"}, (
        f"提交者不一致时应立刻返回，实得：{codes(issues)}"
    )


# ══════════════════════════════════════════════════════════════════
# 三、地块安排
# ══════════════════════════════════════════════════════════════════


def test_unknown_plot():
    """地块 ID 不在自己名下 → 拒绝。"""
    cfg = load_config()
    player = make_player(**{"0,0": mature("0,0", PlotType.RESOURCE, 1)})
    plan = plan_for(plot_plans={"9,9": PlotPlan(plot_id="9,9", u=1.0)})
    issues = validate.validate_plan(plan, player, cfg)
    assert codes(issues) == {"unknown_plot"}, f"实得：{codes(issues)}"
    assert issues[0].target == "9,9"


def test_plot_not_running():
    """在建 / 升级中的地块**不能开工**（§6 第 1 步：到期才切 RUNNING）。"""
    cfg = load_config()
    plot = mature("0,0", PlotType.RESOURCE, 1)
    plot.status = PlotStatus.CONSTRUCTING
    plot.construction_left = 2
    player = make_player(**{"0,0": plot})
    plan = plan_for(plot_plans={"0,0": PlotPlan(plot_id="0,0", u=1.0)})

    issues = validate.validate_plan(plan, player, cfg)
    assert codes(issues) == {"plot_not_running"}, f"实得：{codes(issues)}"
    assert abs(issues[0].available - 2.0) < TOL, "可用值应带出剩余工期"


def test_plot_plans_are_reported_in_sorted_order():
    """问题按地块 ID 排序 —— **确定性**（同输入必须同输出，§0）。"""
    cfg = load_config()
    player = make_player()
    plan = plan_for(plot_plans={
        "3,3": PlotPlan(plot_id="3,3", u=1.0),
        "1,1": PlotPlan(plot_id="1,1", u=1.0),
        "2,2": PlotPlan(plot_id="2,2", u=1.0),
    })
    issues = validate.validate_plan(plan, player, cfg)
    assert [i.target for i in issues] == ["1,1", "2,2", "3,3"], (
        f"应按 ID 排序（确定性），实得 {[i.target for i in issues]}"
    )


def test_agriculture_without_product_is_rejected():
    """★ 农业**没填产出** → 校验期就报，而不是等到生产那一步。

    `Config.recipe()` 对农业 `product=None` 是**明确报错**的
    （4 种产出共用"类型+等级"索引会互相覆盖，不能猜默认值）。
    这条校验就是为了把这个错**提前到提交时**。
    """
    cfg = load_config()
    player = make_player(**{"0,0": mature("0,0", PlotType.AGRICULTURE, 1, product=None)})
    plan = plan_for(plot_plans={"0,0": PlotPlan(plot_id="0,0", u=1.0)})
    issues = validate.validate_plan(plan, player, cfg)
    assert codes(issues) == {"recipe_unavailable"}, f"实得：{codes(issues)}"


def test_agriculture_with_bad_product_is_rejected():
    """农业填了一个不存在的产出（如 `Z`）→ 拒绝。"""
    cfg = load_config()
    player = make_player(**{"0,0": mature("0,0", PlotType.AGRICULTURE, 1, product="Z")})
    plan = plan_for(plot_plans={"0,0": PlotPlan(plot_id="0,0", u=1.0)})
    issues = validate.validate_plan(plan, player, cfg)
    assert codes(issues) == {"recipe_unavailable"}, f"实得：{codes(issues)}"


def test_industry_bad_route_is_rejected():
    """工业 1 级只有 G 一条路线，填别的 → 拒绝。"""
    cfg = load_config()
    player = make_player(**{"0,0": mature("0,0", PlotType.INDUSTRY, 1, product="H-A")})
    plan = plan_for(plot_plans={"0,0": PlotPlan(plot_id="0,0", u=1.0)})
    issues = validate.validate_plan(plan, player, cfg)
    assert codes(issues) == {"recipe_unavailable"}, f"实得：{codes(issues)}"


def test_industry_default_route_is_valid():
    """工业**留空 product** 是合法的 —— 1 级有明确默认路线 G（不是猜的）。"""
    cfg = load_config()
    player = make_player(**{"0,0": mature("0,0", PlotType.INDUSTRY, 1, product=None)})
    plan = plan_for(plot_plans={"0,0": PlotPlan(plot_id="0,0", u=1.0)})
    issues = validate.validate_plan(plan, player, cfg)
    assert issues == [], f"工业 1 级留空应走默认路线 G，实得：{[str(i) for i in issues]}"


# ══════════════════════════════════════════════════════════════════
# 四、采购
# ══════════════════════════════════════════════════════════════════


def test_unknown_goods_in_purchases():
    """买了不存在的商品 → 拒绝（这是原先会跑到第 5 步才炸的典型）。"""
    cfg = load_config()
    plan = plan_for(purchases={"ZZZ": 5.0})
    issues = validate.validate_plan(plan, make_player(), cfg)
    assert codes(issues) == {"unknown_goods"}, f"实得：{codes(issues)}"
    assert issues[0].target == "ZZZ"


def test_negative_purchase():
    """负采购量无意义 → 拒绝。"""
    cfg = load_config()
    plan = plan_for(purchases={"F": -3.0})
    issues = validate.validate_plan(plan, make_player(), cfg)
    assert codes(issues) == {"negative_purchase"}, f"实得：{codes(issues)}"
    assert abs(issues[0].shortfall - (-3.0)) < TOL, "差额 = 申请 − 可用 = −3 − 0"


def test_c_and_d_are_allowed_in_purchases():
    """★ **回归测试**：C / D 放进 `purchases` **必须合法**。

    ══════════════════════════════════════════════════════════════
    ⚠️ 这条钉住的是一个**真实的 bug**（本轮发现并修掉）
    ══════════════════════════════════════════════════════════════

    曾经有过一条 `food_in_purchases` 检查，理由是"口粮不能混进采购"。
    **它是错的** —— 因为：

        **C 和 D 是双用途商品**：
          · 既是**优秀档口粮**（§3.3.3 的 `good_quota` 含 C/D）
          · **又是工业 G 的生产原料**（配方 `G_L1: input {C:2, D:1, F:3}`）

    所以"这个商品是不是口粮"**无法由商品编码判定** ——
    那条检查会**误拦"买 C×2 / D×1 投 G 厂"这种完全正常的计划**。

    实测证据：拿 M1 乙区的正常计划（`purchases={'C':2,'D':1,'F':3}`）
    走 `session.submit()`，会被那条检查拒掉。**这是它被删掉的直接原因。**

    → 现在 `purchases` 与 `food` 是**两条独立通道**，各算各的，互不干扰。
    """
    cfg = load_config()
    plan = plan_for(purchases={"C": 2.0, "D": 1.0, "F": 3.0})   # ← G 厂配方
    issues = validate.validate_plan(plan, make_player(), cfg)
    assert issues == [], (
        f"★ C/D 是双用途商品（口粮 + G 厂原料），必须允许放进 purchases。\n"
        f"  实得：{[str(i) for i in issues]}"
    )


def test_food_goods_also_allowed_in_food_channel():
    """同一次计划里，C 既能进 `purchases`（投厂）也能进 `food`（喂人）。"""
    cfg = load_config()
    plan = plan_for(purchases={"C": 2.0}, food={"C": 0.3})
    issues = validate.validate_plan(plan, make_player(), cfg)
    assert issues == [], (
        f"两条通道互不干扰，同一商品可同时出现在两边。实得：{[str(i) for i in issues]}"
    )


def test_non_food_goods_pass_purchases():
    """非口粮商品（F/G/机械）放 `purchases` 当然也对。"""
    cfg = load_config()
    plan = plan_for(purchases={"F": 3.0, "G": 2.0})
    issues = validate.validate_plan(plan, make_player(), cfg)
    assert issues == [], f"实得：{[str(i) for i in issues]}"


# ══════════════════════════════════════════════════════════════════
# 五、口粮申报
# ══════════════════════════════════════════════════════════════════


def test_food_abcd_are_edible():
    """A/B/C/D 都可作口粮（基准档 A/B，优秀档 A/B/C/D，并集四样）。"""
    cfg = load_config()
    plan = plan_for(food={"A": 1.2, "B": 0.6, "C": 0.3, "D": 0.06})
    issues = validate.validate_plan(plan, make_player(), cfg)
    assert issues == [], f"实得：{[str(i) for i in issues]}"


def test_inedible_food_is_rejected():
    """★ 别的商品不能吃。测试用 **F**（资源）与 **G**（工业）双验。"""
    cfg = load_config()
    for goods in ("F", "G", "H-A", "I"):
        plan = plan_for(food={goods: 1.0})
        issues = validate.validate_plan(plan, make_player(), cfg)
        assert codes(issues) == {"inedible_food"}, (
            f"{goods} 不能吃，应报 inedible_food，实得：{codes(issues)}"
        )


def test_negative_food():
    """负口粮申报 → 拒绝。"""
    cfg = load_config()
    plan = plan_for(food={"A": -1.0})
    issues = validate.validate_plan(plan, make_player(), cfg)
    assert codes(issues) == {"negative_food"}, f"实得：{codes(issues)}"


# ══════════════════════════════════════════════════════════════════
# 六、建设 / 升级
# ══════════════════════════════════════════════════════════════════


def test_build_agriculture_requires_product():
    """★ 农业建造**必须选产出**（§5.1，2026-10-16 拍板：建后不可更换）。

    程序**不替玩家猜默认值** —— 选错要拆掉重建，代价太大。
    """
    cfg = load_config()
    plan = plan_for(builds=[
        BuildOrder(plot_id="0,0", plot_type=PlotType.AGRICULTURE,
                   target_level=1, product=None),
    ])
    issues = validate.validate_plan(plan, make_player(), cfg)
    assert codes(issues) == {"build_product_required"}, f"实得：{codes(issues)}"


def test_build_industry_requires_product():
    """★ 工业建造同样**必须选路线**。"""
    cfg = load_config()
    plan = plan_for(builds=[
        BuildOrder(plot_id="0,0", plot_type=PlotType.INDUSTRY,
                   target_level=2, product=None),
    ])
    issues = validate.validate_plan(plan, make_player(), cfg)
    assert codes(issues) == {"build_product_required"}, f"实得：{codes(issues)}"


def test_build_agriculture_with_product_passes():
    """农业**选了产出** → 这条校验通过（工程表为空是另一码事）。"""
    cfg = load_config()
    plan = plan_for(builds=[
        BuildOrder(plot_id="0,0", plot_type=PlotType.AGRICULTURE,
                   target_level=1, product="A"),
    ])
    issues = validate.validate_plan(plan, make_player(), cfg)
    assert issues == [], f"实得：{[str(i) for i in issues]}"


def test_build_service_needs_no_product():
    """★ 服务 / 资源 / 研究**没有多路线**，不该被要求选产出。"""
    cfg = load_config()
    for ptype in (PlotType.SERVICE, PlotType.RESOURCE, PlotType.RESEARCH):
        plan = plan_for(builds=[
            BuildOrder(plot_id="0,0", plot_type=ptype, target_level=1, product=None),
        ])
        issues = validate.validate_plan(plan, make_player(), cfg)
        assert issues == [], (
            f"{ptype.value} 没有多路线，不该要求填产出，实得：{[str(i) for i in issues]}"
        )


def test_build_on_existing_plot_must_be_an_upgrade():
    """已有 1 级地块，指令目标也是 1 级 → 不是升级。"""
    cfg = load_config()
    player = make_player(**{"0,0": mature("0,0", PlotType.AGRICULTURE, 1, product="A")})
    plan = plan_for(builds=[
        BuildOrder(plot_id="0,0", plot_type=PlotType.AGRICULTURE,
                   target_level=1, product="A"),
    ])
    issues = validate.validate_plan(plan, player, cfg)
    assert codes(issues) == {"build_not_an_upgrade"}, f"实得：{codes(issues)}"


def test_build_type_mismatch():
    """升级**不能改变地块类型**（换类型＝拆掉重建，是两步）。"""
    cfg = load_config()
    player = make_player(**{"0,0": mature("0,0", PlotType.AGRICULTURE, 1, product="A")})
    plan = plan_for(builds=[
        BuildOrder(plot_id="0,0", plot_type=PlotType.INDUSTRY,
                   target_level=2, product="H-A"),
    ])
    issues = validate.validate_plan(plan, player, cfg)
    assert codes(issues) == {"build_type_mismatch"}, f"实得：{codes(issues)}"


def test_plot_already_constructing():
    """已经在建的地块不能再下一条建设指令。"""
    cfg = load_config()
    plot = mature("0,0", PlotType.AGRICULTURE, 1, product="A")
    plot.status = PlotStatus.CONSTRUCTING
    plot.construction_left = 2
    player = make_player(**{"0,0": plot})
    plan = plan_for(builds=[
        BuildOrder(plot_id="0,0", plot_type=PlotType.AGRICULTURE,
                   target_level=2, product="A"),
    ])
    issues = validate.validate_plan(plan, player, cfg)
    assert codes(issues) == {"plot_already_constructing"}, f"实得：{codes(issues)}"


def test_bad_target_level():
    """目标等级只能是 1/2/3 —— 4 级不存在。"""
    cfg = load_config()
    plan = plan_for(builds=[
        BuildOrder(plot_id="0,0", plot_type=PlotType.SERVICE,
                   target_level=4, product=None),
    ])
    issues = validate.validate_plan(plan, make_player(), cfg)
    assert codes(issues) == {"bad_target_level"}, f"实得：{codes(issues)}"


# ══════════════════════════════════════════════════════════════════
# 七、合同通道（第一期明确报错，不静默忽略）
# ══════════════════════════════════════════════════════════════════


def test_contract_shape_issues_reported():
    """★ 合同动作在**提交时**就做形状校验（§7.2 A 方案，2026-10-16 换成真机制）。

    ⚠️ 旧版这里是 `contracts_not_implemented`（"功能未实现"占位）。
       本轮合同机制已实现，所以改为核对**真正的形状规则**：
       数量必须为正、单价不能为负、商品必须在市场表里、对手方不能是自己。

    ⚠️ "单方提交"**不在这里报**：那是结算期的事（单人局=机制上签不成→放行；
       多人局=整季报错）。validate 只读 plan、看不到 GameState，故不判那一条。
    """
    cfg = load_config()
    player = make_player()

    # ① 数量 ≤ 0
    issues = validate.validate_plan(
        plan_for(contracts=[ContractAction(action="deliver", side="sell",
                                           counterparty="p2", goods="F", qty=0.0)]),
        player, cfg,
    )
    assert "contract_qty_not_positive" in codes(issues), f"实得：{codes(issues)}"

    # ② 单价为负
    issues = validate.validate_plan(
        plan_for(contracts=[ContractAction(action="deliver", side="sell",
                                           counterparty="p2", goods="F", qty=1.0,
                                           price=-5.0)]),
        player, cfg,
    )
    assert "contract_price_negative" in codes(issues), f"实得：{codes(issues)}"

    # ③ 商品不在市场表
    issues = validate.validate_plan(
        plan_for(contracts=[ContractAction(action="deliver", side="sell",
                                           counterparty="p2", goods="不存在",
                                           qty=1.0, price=10.0)]),
        player, cfg,
    )
    assert "unknown_goods" in codes(issues), f"实得：{codes(issues)}"

    # ④ 对手方是自己
    issues = validate.validate_plan(
        plan_for(contracts=[ContractAction(action="deliver", side="sell",
                                           counterparty=player.player_id,
                                           goods="F", qty=1.0, price=10.0)]),
        player, cfg,
    )
    assert "contract_self_deal" in codes(issues), f"实得：{codes(issues)}"

    # ⑤ 形状全对 → 一条都不报（**签不签得成是结算期的事**，不归 validate 管）
    issues = validate.validate_plan(
        plan_for(contracts=[ContractAction(action="deliver", side="sell",
                                           counterparty="p2", goods="F",
                                           qty=1.0, price=10.0)]),
        player, cfg,
    )
    assert codes(issues) == set(), (
        f"形状合法的合同不该在提交期报错（单方提交是结算期的事）。实得：{codes(issues)}"
    )


# ══════════════════════════════════════════════════════════════════
# 八、资金粗校验
# ══════════════════════════════════════════════════════════════════


def test_insufficient_cash_estimate():
    """钱明显不够（按参考价估）→ 提前报，别等到第 5 步。"""
    cfg = load_config()
    # F 的 P0 = 24 → 100 × 24 = 2400，远超手上的 10
    plan = plan_for(purchases={"F": 100.0})
    issues = validate.validate_plan(plan, make_player(cash=10.0), cfg)
    assert codes(issues) == {"insufficient_cash_estimate"}, f"实得：{codes(issues)}"
    issue = issues[0]
    assert abs(issue.requested - 2400.0) < TOL, f"申请值应为 2400，实得 {issue.requested}"
    assert abs(issue.available - 10.0) < TOL, f"可用值应为 10，实得 {issue.available}"
    assert abs(issue.shortfall - 2390.0) < TOL, f"差额应为 2390，实得 {issue.shortfall}"


def test_enough_cash_passes():
    """钱够就放行。"""
    cfg = load_config()
    plan = plan_for(purchases={"F": 10.0})     # 10 × 24 = 240
    issues = validate.validate_plan(plan, make_player(cash=1000.0), cfg)
    assert issues == [], f"实得：{[str(i) for i in issues]}"


def test_no_purchases_skips_cash_check():
    """**没有采购时不估资金** —— 工资/经营费要等第 3、4 步才知道，估不了。

    ⚠️ 这条是刻意的：宁可不报，也不报一个错的钱数。
       真正的判据在第 5 步。
    """
    cfg = load_config()
    plan = plan_for()                            # 空计划，但手上只有 1 块钱
    issues = validate.validate_plan(plan, make_player(cash=1.0), cfg)
    assert issues == [], f"无采购时不该做资金估算，实得：{[str(i) for i in issues]}"


# ══════════════════════════════════════════════════════════════════
# 九、一次报全 / 只读 / 五要素
# ══════════════════════════════════════════════════════════════════


def test_reports_all_issues_at_once():
    """★ **一次报全**，不是报第一个就返回。

    玩家改一次计划不容易，"修一个报一个"体验很差。
    """
    cfg = load_config()
    player = make_player(cash=10.0, **{"0,0": mature("0,0", PlotType.RESOURCE, 1)})
    plan = plan_for(
        plot_plans={"9,9": PlotPlan(plot_id="9,9", u=1.0)},   # 地块不存在
        purchases={"ZZZ": 1.0, "F": 100.0},                    # 商品不存在 + 钱不够
        food={"G": 1.0},                                       # 不能吃
    )
    issues = validate.validate_plan(plan, player, cfg)
    assert codes(issues) == {
        "unknown_plot", "unknown_goods", "insufficient_cash_estimate", "inedible_food",
    }, f"应一次报全 4 条，实得：{codes(issues)}"


def test_validate_does_not_mutate_plan():
    """★ 校验**只读**，绝不改计划（否则"校验"就成了"修改"）。"""
    cfg = load_config()
    player = make_player(cash=10.0, **{"0,0": mature("0,0", PlotType.RESOURCE, 1)})
    plan = plan_for(
        plot_plans={"0,0": PlotPlan(plot_id="0,0", u=0.5)},
        purchases={"ZZZ": 1.0},
        food={"A": 1.0},
    )
    before = deepcopy(plan.to_dict())
    validate.validate_plan(plan, player, cfg)
    assert plan.to_dict() == before, "★ validate 必须不改计划"


def test_validate_does_not_mutate_player():
    """★ 校验**不改玩家状态**（现金/地块一律不动）。"""
    cfg = load_config()
    player = make_player(cash=10.0, **{"0,0": mature("0,0", PlotType.RESOURCE, 1)})
    plan = plan_for(purchases={"F": 100.0})
    before = deepcopy(player.to_dict())
    validate.validate_plan(plan, player, cfg)
    assert player.to_dict() == before, "★ validate 必须不改玩家状态"


def test_issue_carries_five_elements():
    """★ §11.3 的五要素：**类型 / 商品或地块 / 申请值 / 可用值 / 差额**。

    前端与 AI 都要靠这几个**结构化字段**填提示框，
    不该去解析 `message` 字符串。
    """
    issue = PlanIssue(
        code="demo", target="F", message="示例",
        requested=100.0, available=10.0,
    )
    d = issue.to_dict()
    assert d["code"] == "demo"                    # 错误类型
    assert d["target"] == "F"                     # 涉及商品或地块
    assert abs(d["requested"] - 100.0) < TOL      # 申请值
    assert abs(d["available"] - 10.0) < TOL       # 可用值
    assert abs(d["shortfall"] - 90.0) < TOL       # 差额


def test_shortfall_is_none_when_not_applicable():
    """拿不到申请/可用值时，差额是 `None` —— **不编造 0**。

    ⚠️ 编造 0 会让人读成"差额为零"，也就是"没问题"，那是错的。
    """
    issue = PlanIssue(code="demo", target="x", message="示例")
    assert issue.shortfall is None
    assert issue.to_dict()["shortfall"] is None

    # 只给一半也应是 None（不拿 0 顶替）
    half = PlanIssue(code="demo", target="x", message="示例", requested=5.0)
    assert half.shortfall is None


def test_issue_str_shows_five_elements():
    """`str(issue)` 在有人看终端时也要能读懂（带申请/可用/差额）。"""
    issue = PlanIssue(
        code="insufficient_cash_estimate", target="p1", message="钱不够",
        requested=2400.0, available=10.0,
    )
    text = str(issue)
    assert "insufficient_cash_estimate" in text
    assert "2400" in text and "10" in text and "2390" in text


# ══════════════════════════════════════════════════════════════════
# 十、两个包装入口
# ══════════════════════════════════════════════════════════════════


def test_require_valid_raises_plan_rejected():
    """`require_valid` 有问题就抛 `PlanRejected`，并把 issues 带上。"""
    cfg = load_config()
    plan = plan_for(purchases={"ZZZ": 1.0})
    try:
        validate.require_valid(plan, make_player(), cfg)
    except PlanRejected as e:
        assert len(e.issues) == 1
        assert e.issues[0].code == "unknown_goods"
    else:
        raise AssertionError("★ 无效计划必须抛 PlanRejected")


def test_require_valid_silent_when_ok():
    """合法计划不抛错。"""
    cfg = load_config()
    validate.require_valid(plan_for(), make_player(), cfg)


def test_is_valid_helper():
    """`is_valid` 是给 UI 判 `can_settle` 用的布尔入口。"""
    cfg = load_config()
    player = make_player()
    assert validate.is_valid(plan_for(), player, cfg) is True
    assert validate.is_valid(plan_for(purchases={"ZZZ": 1.0}), player, cfg) is False


# ══════════════════════════════════════════════════════════════════
# 十一、给 UI 的建造选项（前端零计算）
# ══════════════════════════════════════════════════════════════════


def test_buildable_products():
    """建造面板的下拉选项由**引擎**给出（前端零计算）。"""
    assert buildable_products(PlotType.AGRICULTURE, 1) == ["A", "B", "C", "D"]
    assert buildable_products(PlotType.INDUSTRY, 1) == ["G"]
    assert buildable_products(PlotType.INDUSTRY, 2) == ["H-A", "H-F", "H-G", "H-J"]
    assert buildable_products(PlotType.INDUSTRY, 3) == ["H-A+", "H-F+", "H-G+", "H-J+", "I"]
    # 没有多路线的类型 → 空列表（前端据此**不画**下拉框）
    assert buildable_products(PlotType.RESOURCE, 1) == []
    assert buildable_products(PlotType.SERVICE, 2) == []
    assert buildable_products(PlotType.RESEARCH, 3) == []


# ══════════════════════════════════════════════════════════════════
# 十二、★ 接进会话：submit 不再放行无效计划
# ══════════════════════════════════════════════════════════════════


def test_session_submit_rejects_invalid_plan():
    """★ `GameSession.submit()` 现在**真的校验计划**了。

    在此之前它只查"是不是重复提交"，于是无效计划一路走到结算第 5 步才炸。
    这是 `engine/validate.py` 存在的**首要理由**。
    """
    from server.session import Conflict, GameSession

    cfg = load_config()
    player = make_player(**{"0,0": mature("0,0", PlotType.RESOURCE, 1)})
    state = _state_with(cfg, player)
    session = GameSession(state=state, cfg=cfg)

    bad = plan_for(purchases={"ZZZ": 1.0})
    try:
        session.submit("p1", bad)
    except PlanRejected as e:
        assert e.issues[0].code == "unknown_goods"
    else:
        raise AssertionError("★ 无效计划不该被 session 收下")

    assert session.plans == {}, "无效计划不该进 plans（不产生部分状态变更）"

    # 换成合法计划 → 收下
    session.submit("p1", plan_for())
    assert "p1" in session.plans


def test_session_conflict_and_rejection_are_distinct():
    """★ 两类错误**刻意分开**：`Conflict`（会话状态）vs `PlanRejected`（计划本身）。

    分开的理由：前者要玩家"等/换时机"，后者要玩家"改计划"，
    服务层与前端要做的事完全不同。合成一个会丢掉这个区分。
    """
    from server.session import Conflict, GameSession

    cfg = load_config()
    player = make_player(**{"0,0": mature("0,0", PlotType.RESOURCE, 1)})
    state = _state_with(cfg, player)
    session = GameSession(state=state, cfg=cfg)

    # ① 计划本身不合法 → PlanRejected（**不是** Conflict）
    try:
        session.submit("p1", plan_for(purchases={"ZZZ": 1.0}))
        raise AssertionError("应当抛 PlanRejected")
    except PlanRejected:
        pass

    # ② 合法提交后重复提交 → Conflict（**不是** PlanRejected）
    session.submit("p1", plan_for())
    try:
        session.submit("p1", plan_for())
        raise AssertionError("应当抛 Conflict")
    except PlanRejected:
        raise AssertionError("重复提交是 Conflict，不是 PlanRejected") from None
    except Conflict:
        pass


def _state_with(cfg, player):
    """把玩家塞进一个开局世界，造出 `GameState`。"""
    from engine.models import GameState, WorldPhase

    world = cfg.new_world()
    world.phase = WorldPhase.RUNNING
    return GameState(world=world, players={player.player_id: player})


# ══════════════════════════════════════════════════════════════════
# 命令行汇总
# ══════════════════════════════════════════════════════════════════


def _run_all() -> int:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    mod = sys.modules[__name__]
    names = sorted(n for n in dir(mod) if n.startswith("test_"))
    ok = fail = 0
    print("=" * 68)
    print("计划校验对账")
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
