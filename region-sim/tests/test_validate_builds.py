"""建造校验硬化对账 —— 「校验通过」要尽量等价于「结算能跑」。

跑法（在 region-sim/ 下）：

    python tests/test_validate_builds.py

依据 `docs/03-架构/AI秘书-有效起草方案.md` §二 / §三.1：

    症状：一份「10 条工业新建」的计划能过 `validate` / `submit`，
          直到结算第 5 步才报「资金不足」；
          原因之一是 `_check_cash` 只算 `purchases`，完全没算
          建造现金费 / 工程物资 / 口粮缺口。

本文件只测**校验**，不测结算：
    · 坐标（格式 / 越界 / 同图重复）；
    · 产出路线（农业/工业的真实选项，含工业二级的「研究」）；
    · 工程表 dry-run（查不到工程 = 结算必炸）；
    · 资金粗估（采购 + 建造 + 口粮）。

⚠️ 资金仍然只是**粗校验**（按参考价 P0 估），最终判据是结算第 5 步 ——
   见 `engine/validate.py` 模块头「三条设计口径」其三。
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
    GameState,
    Plan,
    PlayerState,
    PlotType,
    WorldPhase,
)
from tests.test_m1 import mature

TOL = 1e-6


# ══════════════════════════════════════════════════════════════════
# 夹具：开局（现金 1200、零地块 —— §5.3 地图全是可建空地）
# ══════════════════════════════════════════════════════════════════


def new_state(cash: float | None = None, **plots):
    """造一个开局单人局。

    `cash=None` → 用 `cfg.new_player()` 的正式开局现金（1200）；
    给了 `cash` → 手工指定（测"钱不够"时用）。

    `plots` 传 `plot_id=Plot`（例如 `tests.test_m1.mature(...)`）。
    """
    cfg = load_config()
    if cash is None:
        player = cfg.new_player("p1")
    else:
        player = PlayerState(player_id="p1", cash=float(cash))
    player.plots = dict(plots)

    world = cfg.new_world()
    world.phase = WorldPhase.RUNNING
    state = GameState(world, players={"p1": player})
    return cfg, state


def run_validate(state, plan: Plan):
    """走**玩家真实入口**（`validate_plan`，与 submit 同一个函数）。"""
    cfg = load_config()
    return validate.validate_plan(plan, state.players["p1"], cfg)


def codes(issues) -> set[str]:
    return {i.code for i in issues}


def plan_of(*builds: BuildOrder, **kw) -> Plan:
    return Plan(player_id="p1", builds=list(builds), **kw)


# ══════════════════════════════════════════════════════════════════
# 一、合法计划不许被误伤
# ══════════════════════════════════════════════════════════════════


def test_two_agriculture_builds_are_valid():
    """★ 开局 2 块农业（空地新建 1 级、产出 A）**必须合法**。

    账：每块折后现金 40 + 工程物资 H-A×1（P0 64）= 104，两块 208 ≤ 1200。
    这条同时钉住"新校验没有把正常开局建法拦掉"。
    """
    _, st = new_state()
    plan = plan_of(
        BuildOrder("0,0", PlotType.AGRICULTURE, 1, "A"),
        BuildOrder("0,1", PlotType.AGRICULTURE, 1, "A"),
    )
    issues = run_validate(st, plan)
    assert issues == [], f"两块农业应当合法，实得：{[str(i) for i in issues]}"


def test_industry_level2_research_route_is_valid():
    """★ 工业 1 级 G 升 2 级、`product="研究"` → **合法**（研究线，6 季）。

    依据 `tests/test_construction.py::test_build_order_route_resolution`：
    `find_work_for_build` 认「研究」→ `ind_1_2_research`。

    ⚠️ `validate.buildable_products(INDUSTRY, 2)` **不列**研究 ——
       那是前端下拉框契约，不是校验真值。校验真值见 `_BUILD_PRODUCTS`，
       两者不同步是本轮**刻意**的（不改前端契约）。
    """
    cfg, st = new_state(**{"0,0": mature("0,0", PlotType.INDUSTRY, 1, product="G")})
    plan = plan_of(BuildOrder("0,0", PlotType.INDUSTRY, 2, "研究"))
    issues = run_validate(st, plan)
    assert issues == [], f"研究线应当合法，实得：{[str(i) for i in issues]}"

    # 反向确认：前端下拉框里确实没有它（两个来源**本来就不同**）
    assert "研究" not in validate.buildable_products(PlotType.INDUSTRY, 2)


def test_industry_level3_I_is_valid():
    """★ 二级研究线升三级、`product="I"` → **合法**。

    二级地块的 `product` 是「研究」（或 None），`find_work_for_build`
    据此走 research 路线 → `ind_2_3_high`（8 季，500 现金 + H-G×5）。
    """
    _, st = new_state(**{"0,0": mature("0,0", PlotType.INDUSTRY, 2, product="研究")})
    plan = plan_of(BuildOrder("0,0", PlotType.INDUSTRY, 3, "I"))
    issues = run_validate(st, plan)
    assert issues == [], f"研究线升三级 I 应当合法，实得：{[str(i) for i in issues]}"


# ══════════════════════════════════════════════════════════════════
# 二、★ 资金粗估：10 条工业新建必须当场报「钱不够」
# ══════════════════════════════════════════════════════════════════


def test_ten_industry_builds_report_insufficient_cash():
    """★★ 本文件最重要的一条：**10 条工业新建**在**提交前**就报钱不够。

    修复前：`_check_cash` 只看 `purchases`（这份计划一条采购都没有）→
            直接 `return []` → 校验通过、submit 成功、结算第 5 步才炸。

    实算（config §5.2 + §5.3）：
        每条 ind_new = 180 现金费 + H-G×2（P0 73）；
        工业名额 3 个 → 前 3 条现金 90，其余 7 条 180；
        现金合计 = 3×90 + 7×180 = 1530；物资 = 10×2×73 = 1460；
        合计 = **2990** > 开局 1200。
    """
    _, st = new_state()
    ids = [f"0,{c}" for c in range(10)]
    plan = plan_of(*[BuildOrder(pid, PlotType.INDUSTRY, 1, "G") for pid in ids])
    cash = st.players["p1"].cash

    issues = run_validate(st, plan)
    assert "insufficient_cash_estimate" in codes(issues), (
        f"★ 10 条工业必须被资金粗估挡住，实得：{[str(i) for i in issues]}"
    )
    issue = next(i for i in issues if i.code == "insufficient_cash_estimate")
    assert issue.requested > cash, f"估算应超过现金 {cash}，实得 {issue.requested}"
    assert abs(issue.available - cash) < TOL, f"可用值应为 {cash}，实得 {issue.available}"
    assert issue.shortfall > 0, "差额应为正"
    # 这份计划除了钱**没有别的问题**（坐标/产物/工程表全对）
    assert codes(issues) == {"insufficient_cash_estimate"}, (
        f"应当只报资金一项，实得：{codes(issues)}"
    )


def test_cash_estimate_counts_build_cash_and_materials():
    """没有采购、只有一条建造 → **也**要估钱（修复前 `if not plan.purchases: return []`）。

    一条 ind_new = 折后 90 + H-G×2 = 146 → 236；手上只给 200 → 报。
    """
    _, st = new_state(cash=200.0)
    plan = plan_of(BuildOrder("0,0", PlotType.INDUSTRY, 1, "G"))
    issues = run_validate(st, plan)
    assert codes(issues) == {"insufficient_cash_estimate"}, f"实得：{codes(issues)}"
    assert abs(issues[0].requested - 236.0) < TOL, (
        f"建造粗估应为 90 + 2×73 = 236，实得 {issues[0].requested}"
    )


def test_cash_estimate_counts_food():
    """口粮是**自动买**的（缺口会在购入环节买入）→ 必须算进现金压力。

    A 的 P0 = 10，申报 A×5 = 50；手上 30 → 报。计划里**没有任何采购**，
    所以这条只可能由口粮估出来。
    """
    _, st = new_state(cash=30.0)
    plan = plan_of(food={"A": 5.0})
    issues = run_validate(st, plan)
    assert codes(issues) == {"insufficient_cash_estimate"}, f"实得：{codes(issues)}"
    assert abs(issues[0].requested - 50.0) < TOL, (
        f"口粮粗估应为 5×10 = 50，实得 {issues[0].requested}"
    )


def test_cheap_plan_passes_cash_estimate():
    """钱够就放行（别把正常计划也拦住）。"""
    _, st = new_state()
    plan = plan_of(food={"A": 10.0}, purchases={"F": 2.0})
    issues = run_validate(st, plan)
    assert issues == [], f"实得：{[str(i) for i in issues]}"


# ══════════════════════════════════════════════════════════════════
# 三、坐标：格式 / 越界 / 同一块地重复下指令
# ══════════════════════════════════════════════════════════════════


def test_duplicate_build_plot_is_reported():
    """★ 同一份计划里同一块地出现两次 → 第一条保留，后面报 `duplicate_build_plot`。

    ⚠️ 重复建造不是"覆盖一下"：结算会按两条指令各扣一次钱、各推一次状态。
    """
    _, st = new_state()
    plan = plan_of(
        BuildOrder("3,4", PlotType.AGRICULTURE, 1, "A"),
        BuildOrder("3,4", PlotType.AGRICULTURE, 1, "A"),
    )
    issues = run_validate(st, plan)
    assert codes(issues) == {"duplicate_build_plot"}, f"实得：{codes(issues)}"
    issue = issues[0]
    assert issue.target == "3,4"
    assert issue.requested == 2.0 and issue.available == 1.0, (
        f"五要素：第 2 条与第 1 条重复，实得 requested={issue.requested} "
        f"available={issue.available}"
    )
    assert "第 1 条" in issue.message, "要告诉玩家重复的是哪一条"


def test_duplicate_detected_across_equivalent_ids():
    """★ `'3,4'` 与 `'03,4'` 是**同一块地** —— 重复判定按解析后的坐标，不按字符串。"""
    _, st = new_state()
    plan = plan_of(
        BuildOrder("3,4", PlotType.AGRICULTURE, 1, "A"),
        BuildOrder("03,4", PlotType.AGRICULTURE, 1, "A"),
    )
    issues = run_validate(st, plan)
    assert codes(issues) == {"duplicate_build_plot"}, f"实得：{codes(issues)}"


def test_build_plot_out_of_bounds():
    """★ 越界坐标（地图 10×10）→ `build_plot_out_of_bounds`。

    修复前：`player.plots.get("10,0")` 是 None → 被当成"空地新建"，一路放行，
    结算时凭空造出一块地图外的地（或被 report/地图层静默丢弃）。
    """
    cfg, st = new_state()
    for bad, requested, available in (
        ("10,0", 10.0, 9.0),      # 行越界
        ("0,10", 10.0, 9.0),      # 列越界
        ("99,99", 99.0, 9.0),     # 行列都越界（先报行）
        ("-1,5", -1.0, 9.0),      # 负数
    ):
        plan = plan_of(BuildOrder(bad, PlotType.AGRICULTURE, 1, "A"))
        issues = run_validate(st, plan)
        assert codes(issues) == {"build_plot_out_of_bounds"}, (
            f"{bad} 应报越界，实得：{codes(issues)}"
        )
        issue = issues[0]
        assert issue.target == bad
        assert abs(issue.requested - requested) < TOL, (
            f"{bad} 的申请值应为越界下标 {requested}，实得 {issue.requested}"
        )
        assert abs(issue.available - available) < TOL, (
            f"{bad} 的可用值应为最大合法下标 {available}，实得 {issue.available}"
        )
    # 边界内的极端格子必须合法（别把右下角也算越界）
    plan = plan_of(BuildOrder(f"{cfg.rows - 1},{cfg.cols - 1}",
                              PlotType.AGRICULTURE, 1, "A"))
    assert run_validate(st, plan) == []


def test_bad_build_plot_id_format():
    """格式不是 "r,c" → `bad_build_plot_id`（不是抛 ValueError 打成 500）。"""
    _, st = new_state()
    for bad in ("", "abc", "3", "3,4,5", "a,b"):
        plan = plan_of(BuildOrder(bad, PlotType.AGRICULTURE, 1, "A"))
        issues = run_validate(st, plan)
        assert codes(issues) == {"bad_build_plot_id"}, (
            f"{bad!r} 应报格式非法，实得：{codes(issues)}"
        )


# ══════════════════════════════════════════════════════════════════
# 四、产出 / 路线必须真实
# ══════════════════════════════════════════════════════════════════


def test_bad_build_product_on_agriculture():
    """农业产物写 `Z` → `bad_build_product`，message 里列出可选值。"""
    _, st = new_state()
    plan = plan_of(BuildOrder("0,0", PlotType.AGRICULTURE, 1, "Z"))
    issues = run_validate(st, plan)
    assert codes(issues) == {"bad_build_product"}, f"实得：{codes(issues)}"
    issue = issues[0]
    assert issue.target == "0,0"
    for opt in ("A", "B", "C", "D"):
        assert opt in issue.message, f"message 应列出可选值，缺 {opt}"


def test_bad_build_product_on_industry():
    """工业 1 级只能产 G：写 `H-A`（二级产物）→ `bad_build_product`。"""
    _, st = new_state()
    plan = plan_of(BuildOrder("0,0", PlotType.INDUSTRY, 1, "H-A"))
    issues = run_validate(st, plan)
    assert codes(issues) == {"bad_build_product"}, f"实得：{codes(issues)}"
    assert "G" in issues[0].message, "message 应列出该等级的真实选项"


def test_bad_build_product_on_level3():
    """工业 3 级的真实选项是 H-*+ 或 I；写 `H-A`（二级）→ `bad_build_product`。"""
    _, st = new_state(**{"0,0": mature("0,0", PlotType.INDUSTRY, 2, product="H-A")})
    plan = plan_of(BuildOrder("0,0", PlotType.INDUSTRY, 3, "H-A"))
    issues = run_validate(st, plan)
    assert codes(issues) == {"bad_build_product"}, f"实得：{codes(issues)}"


def test_product_required_still_reported():
    """留空仍然是 `build_product_required`（本轮没有放松这条）。"""
    _, st = new_state()
    plan = plan_of(BuildOrder("0,0", PlotType.AGRICULTURE, 1, None))
    issues = run_validate(st, plan)
    assert codes(issues) == {"build_product_required"}, f"实得：{codes(issues)}"


# ══════════════════════════════════════════════════════════════════
# 五、工程表 dry-run
# ══════════════════════════════════════════════════════════════════


def test_empty_plot_cannot_build_level_directly():
    """★ 空地直接 target_level=2 仍报 `cannot_build_level_directly`。

    工程表只有 0→1 / 1→2 / 2→3，没有 0→2。
    """
    _, st = new_state()
    for lv in (2, 3):
        plan = plan_of(BuildOrder("0,0", PlotType.AGRICULTURE, lv, "A"))
        issues = run_validate(st, plan)
        assert codes(issues) == {"cannot_build_level_directly"}, (
            f"空地建 {lv} 级应只报这一条，实得：{codes(issues)}"
        )


def test_research_plot_has_no_work_and_is_rejected():
    """★ 研究地块**建不了**：工程表 14 行里没有 research 这一类。

    `construction.works` 只有 农业×3 / 资源×3 / 工业×5 / 服务×3 = 14 行，
    `opening.discount_quota` 也只有 4 个类型的名额。也就是说，当前配置下
    "新建研究地块"这条指令**在结算时必抛 ConfigError**（`_construction_materials`）。
    校验现在把它提前报成 `build_work_unavailable`。

    ⚠️ 这条钉住的是一个**待裁定**的事实（要不要给研究地块补工程行 = 改规则）。
       在补上之前，"能过 validate 就一定能结算"要求这里必须报错。
    """
    _, st = new_state()
    plan = plan_of(BuildOrder("0,0", PlotType.RESEARCH, 1, None))
    issues = run_validate(st, plan)
    assert codes(issues) == {"build_work_unavailable"}, f"实得：{codes(issues)}"
    issue = issues[0]
    assert issue.target == "0,0"
    assert "没有这条工程" in issue.message, (
        f"要把 construction 的原始报错带上，实得：{issue.message}"
    )


def test_service_and_resource_builds_are_still_valid():
    """服务 / 资源有工程行 → 不该被新校验拦（确认 dry-run 没误伤）。"""
    _, st = new_state()
    for ptype in (PlotType.SERVICE, PlotType.RESOURCE):
        plan = plan_of(BuildOrder("0,0", ptype, 1, None))
        issues = run_validate(st, plan)
        assert issues == [], f"{ptype.value} 新建应合法，实得：{[str(i) for i in issues]}"


# ══════════════════════════════════════════════════════════════════
# 六、一次报全 / 只读
# ══════════════════════════════════════════════════════════════════


def test_multiple_build_problems_reported_at_once():
    """★ **一次报全**：越界 + 重复 + 产物错 + 钱不够 一起报，不是报第一个就返回。"""
    _, st = new_state(cash=100.0)
    plan = plan_of(
        BuildOrder("0,0", PlotType.AGRICULTURE, 1, "Z"),          # 产物错
        BuildOrder("0,0", PlotType.AGRICULTURE, 1, "A"),          # 与上一条重复
        BuildOrder("10,0", PlotType.AGRICULTURE, 1, "A"),         # 越界
        BuildOrder("0,1", PlotType.INDUSTRY, 1, "G"),             # 合法但超预算
    )
    issues = run_validate(st, plan)
    assert codes(issues) == {
        "bad_build_product", "duplicate_build_plot",
        "build_plot_out_of_bounds", "insufficient_cash_estimate",
    }, f"实得：{codes(issues)}"


def test_validate_does_not_mutate_plan_or_player():
    """★ 校验**只读**：dry-run 也不能顺手改地块/现金/名额。"""
    _, st = new_state(**{"0,0": mature("0,0", PlotType.INDUSTRY, 1, product="G")})
    plan = plan_of(
        BuildOrder("0,0", PlotType.INDUSTRY, 2, "研究"),
        BuildOrder("5,5", PlotType.AGRICULTURE, 1, "A"),
        BuildOrder("5,5", PlotType.AGRICULTURE, 1, "A"),
    )
    plan_before = deepcopy(plan.to_dict())
    player_before = deepcopy(st.players["p1"].to_dict())

    run_validate(st, plan)

    assert plan.to_dict() == plan_before, "★ validate 必须不改计划"
    assert st.players["p1"].to_dict() == player_before, "★ validate 必须不改玩家状态"


# ══════════════════════════════════════════════════════════════════
# 命令行汇总（与 tests/test_validate.py 同款 runner）
# ══════════════════════════════════════════════════════════════════


def _run_all() -> int:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    mod = sys.modules[__name__]
    names = sorted(n for n in dir(mod) if n.startswith("test_"))
    ok = fail = 0
    print("=" * 68)
    print("建造校验硬化（坐标 / 路线 / 工程表 / 资金粗估）")
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
