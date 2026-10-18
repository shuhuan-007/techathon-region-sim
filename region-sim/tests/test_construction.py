"""建设 / 升级对账 —— 工程表 14 行、工业双路线、工期。

跑法（在 region-sim/ 下）：

    python tests/test_construction.py

依据《规则基线》§5.1（物资对应 / 产出路线建后不可更换）与 §5.2（一次性工程数值）。

★ 本文件最硬的一条是 `test_every_work_matches_baseline_total`：
  逐行核对 `现金费 + 物资@参考价 == 基线 §5.2 的『按参考价合计』列`。
  14 行**全部**要对上 —— 录错一个数字立刻报警。
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import construction as C
from engine import pipeline
from engine.loader import ConfigError, load_config
from engine.models import (
    BuildOrder,
    GameState,
    Plan,
    PlayerState,
    Plot,
    PlotStatus,
    PlotType,
    WorldPhase,
)

TOL = 1e-9

# ══════════════════════════════════════════════════════════════════
# ★ 基线 §5.2 的 14 行全表（现金费 / 物资 / 按参考价合计 / 工期）
# ══════════════════════════════════════════════════════════════════
#
# ⚠️ 这张表是**从《规则基线》§5.2 逐行抄下来的**，不是从 config 生成的。
#    两边对不上就说明有一边错了 —— 这正是本测试的目的。
BASELINE_5_2 = [
    # work_id,            现金费, 物资,              合计, 工期
    ("agri_new",           80,  {"H-A": 1},  144, 1),
    ("agri_1_2",           80,  {"H-A": 1},  144, 2),
    ("agri_2_3",          100,  {"H-A+": 1}, 377, 3),
    ("res_new",           100,  {"H-F": 1},  168, 1),
    ("res_1_2",           120,  {"H-F": 2},  256, 2),
    ("res_2_3",           160,  {"H-F+": 1}, 443, 3),
    ("ind_new",           180,  {"H-G": 2},  326, 2),
    ("ind_1_2_machine",   240,  {"H-G": 3},  459, 2),
    ("ind_1_2_research",  300,  {"H-G": 3},  519, 6),
    ("ind_2_3_high",      500,  {"H-G": 5},  865, 8),
    ("ind_2_3_machine",   360,  {"H-G": 3},  579, 4),
    ("srv_new",            80,  {"H-J": 1},  144, 1),
    ("srv_1_2",           100,  {"H-J": 1},  164, 2),
    ("srv_2_3",           140,  {"H-J+": 1}, 417, 3),
]


# ══════════════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════════════


def fresh_state(cash: float = 1200.0, **plots) -> GameState:
    """开局：给定现金、给定地块（默认**零地块** —— 地图上全是可建空地）。"""
    cfg = load_config()
    player = PlayerState(player_id="p1", cash=cash, plots=dict(plots))
    player.last_happiness = 0.8
    world = cfg.new_world()
    world.phase = WorldPhase.RUNNING
    return GameState(world=world, players={"p1": player})


def run(state: GameState, plan: Plan):
    """跑一季，返回报告。"""
    cfg = load_config()
    pipeline.advance(state, {"p1": plan}, cfg)
    return state.raw_reports["p1"]


def mature(pid: str, ptype: PlotType, level: int = 1, product=None) -> Plot:
    return Plot(plot_id=pid, plot_type=ptype, level=level,
                status=PlotStatus.RUNNING, running_quarters=99, product=product)


# ══════════════════════════════════════════════════════════════════
# 一、★ 工程表逐行核对
# ══════════════════════════════════════════════════════════════════


def test_all_fourteen_works_are_loaded():
    """config 里就是 14 行，一行不多一行不少（§5.2 全表）。"""
    cfg = load_config()
    works = C.load_works(cfg)
    assert len(works) == 14, f"应有 14 条工程，实得 {len(works)}：{sorted(works)}"
    assert set(works) == {w[0] for w in BASELINE_5_2}, "工程 ID 应与基线一致"


def test_every_work_matches_baseline_total():
    """★★ **逐行**核对：`现金费 + 物资@参考价 == 基线 §5.2 的合计列`。

    ⚠️ 这是本文件最硬的一条 —— 录错任何一个数字都会在这里显形。
       14 行**全部**要过。
    """
    cfg = load_config()
    works = C.load_works(cfg)
    problems = []

    for work_id, cash, materials, total, quarters in BASELINE_5_2:
        w = works[work_id]
        if abs(w.cash - cash) > TOL:
            problems.append(f"{work_id}: 现金费 {w.cash} ≠ 基线 {cash}")
        if w.materials != {g: float(q) for g, q in materials.items()}:
            problems.append(f"{work_id}: 物资 {w.materials} ≠ 基线 {materials}")
        if w.quarters != quarters:
            problems.append(f"{work_id}: 工期 {w.quarters} ≠ 基线 {quarters}")

        # 合计 = 现金费 + 物资按参考价
        mat_value = sum(cfg.price_of(g) * q for g, q in w.materials.items())
        got = w.cash + mat_value
        if abs(got - total) > 1e-6:
            problems.append(
                f"{work_id}: 现金 {w.cash} + 物资@{mat_value:.0f} = {got:.0f} "
                f"≠ 基线合计 {total}"
            )

    assert not problems, "★ 工程表与基线 §5.2 对不上：\n  " + "\n  ".join(problems)


def test_all_materials_are_real_goods():
    """工程用到的物资都必须是市场里存在的商品（写错编码要早报）。"""
    cfg = load_config()
    known = set(cfg.all_goods())
    for work_id, w in C.load_works(cfg).items():
        for g in w.materials:
            assert g in known, f"{work_id} 用了不存在的商品 {g!r}"


def test_startup_loop_exception_uses_plain_hg():
    """★ **启动循环例外**（§5.1）：三级高技术线用**普通 H-G**，不是 H-G+。

    否则形成"I 需要 H-G+ 建厂，而 H-G+ 又需要 I 才能生产"的死循环。
    """
    cfg = load_config()
    works = C.load_works(cfg)

    assert works["ind_2_3_high"].materials == {"H-G": 5.0}, (
        f"★ 二级研究→三级 I 必须用普通 H-G（启动循环例外），"
        f"实得 {works['ind_2_3_high'].materials}"
    )
    assert "H-G+" not in str(works["ind_2_3_high"].materials), "不该出现 H-G+"
    # 整张表都不该出现 H-G+（那是"自动化机械"，第一期没有工程用它建厂）
    for work_id, w in works.items():
        assert "H-G+" not in w.materials, (
            f"{work_id} 用了 H-G+，但 §5.1 的例外说明建线用普通 H-G"
        )


def test_construction_events_report_started_and_completed():
    """★ 2026-10-18：报告要带工程事件，供结算流水显示“工程情况”。

    1 季工程（农业新建）当季既 started 又 completed；
    前端据此在结算时弹出两条，而不是只能看到一笔“工程现金费”。
    """
    st = fresh_state()
    rep = run(st, Plan("p1", builds=[
        BuildOrder("0,0", PlotType.AGRICULTURE, 1, "A"),
    ]))
    events = rep.construction_events
    assert any(
        e["event"] == "started" and e["plot_id"] == "0,0"
        and e["from_level"] == 0 and e["to_level"] == 1
        for e in events
    ), f"缺开工事件：{events}"
    assert any(
        e["event"] == "completed" and e["plot_id"] == "0,0"
        and e["to_level"] == 1
        for e in events
    ), f"1 季工程当季应完工，缺 completed 事件：{events}"
    # 报告序列化后也要在（前端读的是 to_dict）
    assert rep.to_dict()["construction_events"], "to_dict 丢了工程事件"


# ══════════════════════════════════════════════════════════════════
# 二、★ 工业双路线
# ══════════════════════════════════════════════════════════════════


def test_industry_level_up_has_two_routes():
    """★ 工业 1→2 与 2→3 **各有两条互斥路线**，工期差很远。

    | 路线 | 1→2 | 2→3 |
    |---|---:|---:|
    | 机械 | 2 季 | 4 季 |
    | 研究 | **6 季** | **8 季** |
    """
    cfg = load_config()
    assert C.find_work(cfg, PlotType.INDUSTRY, 1, 2, "machine").quarters == 2
    assert C.find_work(cfg, PlotType.INDUSTRY, 1, 2, "research").quarters == 6
    assert C.find_work(cfg, PlotType.INDUSTRY, 2, 3, "machine").quarters == 4
    assert C.find_work(cfg, PlotType.INDUSTRY, 2, 3, "research").quarters == 8


def test_ambiguous_route_is_not_guessed():
    """★ 两条路线可选时**不替玩家猜**，直接报错。

    ⚠️ 猜错的代价很大（2 季 vs 6 季），所以必须显式指定。
    """
    cfg = load_config()
    try:
        C.find_work(cfg, PlotType.INDUSTRY, 1, 2)      # 没给 route
    except ConfigError as e:
        assert "互斥路线" in str(e)
    else:
        raise AssertionError("★ 工业 1→2 有两条线，不给 route 必须报错")


def test_single_route_types_need_no_route():
    """农业/资源/服务只有一条线 → 不需要 route。"""
    cfg = load_config()
    for ptype in (PlotType.AGRICULTURE, PlotType.RESOURCE, PlotType.SERVICE):
        w = C.find_work(cfg, ptype, 1, 2)
        assert w.route is None, f"{ptype.value} 1→2 不该有 route"


def test_unknown_work_is_reported():
    """查不到的工程 → 明确报错，不返回 None。"""
    cfg = load_config()
    try:
        C.find_work(cfg, PlotType.RESOURCE, 2, 1)      # 没有降级
    except ConfigError as e:
        assert "没有这条工程" in str(e)
    else:
        raise AssertionError("不存在的工程必须报错")


def test_build_order_route_resolution():
    """`find_work_for_build` 按 `BuildOrder` 正确解析路线。"""
    cfg = load_config()

    # 新建一级工业 → 只有 G 一条线
    w = C.find_work_for_build(cfg, None,
                              BuildOrder("0,0", PlotType.INDUSTRY, 1, product="G"))
    assert w.work_id == "ind_new"

    # 一级 G 升二级，选机械 H-A → machine（2 季）
    g1 = mature("0,0", PlotType.INDUSTRY, 1, product="G")
    w = C.find_work_for_build(cfg, g1,
                              BuildOrder("0,0", PlotType.INDUSTRY, 2, product="H-A"))
    assert w.work_id == "ind_1_2_machine" and w.quarters == 2

    # 一级 G 升二级，选研究线 → research（6 季）
    w = C.find_work_for_build(cfg, g1,
                              BuildOrder("0,0", PlotType.INDUSTRY, 2, product="研究"))
    assert w.work_id == "ind_1_2_research" and w.quarters == 6

    # 二级机械升三级 → machine（4 季）
    mech = mature("0,0", PlotType.INDUSTRY, 2, product="H-A")
    w = C.find_work_for_build(cfg, mech,
                              BuildOrder("0,0", PlotType.INDUSTRY, 3, product="H-A+"))
    assert w.work_id == "ind_2_3_machine" and w.quarters == 4

    # 二级研究线升三级 → research（8 季）
    res = mature("0,0", PlotType.INDUSTRY, 2, product=None)
    w = C.find_work_for_build(cfg, res,
                              BuildOrder("0,0", PlotType.INDUSTRY, 3, product="I"))
    assert w.work_id == "ind_2_3_high" and w.quarters == 8


# ══════════════════════════════════════════════════════════════════
# 三、★ 新建与升级
# ══════════════════════════════════════════════════════════════════


def test_new_build_creates_the_plot():
    """★ **新建**：地上本来没有这块地，下完指令才出现（§5.3 开局全是空地）。"""
    st = fresh_state()
    assert st.players["p1"].plots == {}, "开局不该有任何地块"

    run(st, Plan("p1", builds=[BuildOrder("0,0", PlotType.AGRICULTURE, 1, "A")]))

    assert "0,0" in st.players["p1"].plots, "新建后应出现地块"
    p = st.players["p1"].plots["0,0"]
    # 2026-10-17 用户拍板：本季投钱投料也算一季工期 → 1 季工程当季完工
    assert p.status is PlotStatus.RUNNING, (
        f"一级农业工期 1 季，投钱投料的当季度完工，实得 {p.status}"
    )
    assert p.construction_left == 0, "当季完工后剩余工期应为 0"
    assert p.target_level == 1
    assert p.product == "A", "★ 产出建造时选定"


def test_build_charges_cash_and_materials():
    """扣款 = 工程现金费 + 物资款（两者分开记，§5.1）。"""
    st = fresh_state(cash=1200.0)
    r = run(st, Plan("p1", builds=[BuildOrder("0,0", PlotType.AGRICULTURE, 1, "A")]))

    # 2026-10-16 用户裁定：开局减免 每类 3 名额 × 0.50（现金费折半）生效
    assert abs(r.construction_cash - 40.0) < TOL, f"折后现金费应为 40，实得 {r.construction_cash}"
    assert r.construction == {"H-A": 1.0}, f"物资应为 H-A×1，实得 {r.construction}"
    # 物资款 = H-A 参考价 64（物资不打折）
    assert abs(r.materials_cost - 64.0) < TOL, f"物资款应为 64，实得 {r.materials_cost}"
    # 2026-10-16 用户裁定：开局减免 每类 3 名额 × 0.50（现金费折半）生效
    # 总扣款 = 40 + 64 = 104（现金打折不改变物资款）
    assert abs(r.total_deduction - 104.0) < TOL, (
        f"总扣款应为 104（= 折后现金 40 + 物资 64），实得 {r.total_deduction}"
    )
    # 2026-10-16 用户裁定：开局减免 每类 3 名额 × 0.50（现金费折半）生效
    # ★ 2026-10-18 S1：补贴季初发放、不进结算 → 期末 = 运营账本身
    assert abs(r.subsidy) < TOL, f"S1 起结算报告 subsidy 应为 0，实得 {r.subsidy}"
    assert abs(st.players["p1"].cash - 1096.0) < TOL, (
        f"期末现金应为 1200 − 104 = 1096.0（S1 起补贴不进账），"
        f"实得 {st.players['p1'].cash}"
    )


def test_construction_completes_after_quarters():
    """★ 工期一到就完工，等级落地、磨合从完工起算。

    2026-10-17 用户拍板：本季投钱投料也算一季工期
    → 1 季工程**建成当季即完工**（无需再空跑一季）。
    """
    st = fresh_state()
    run(st, Plan("p1", builds=[BuildOrder("0,0", PlotType.RESOURCE, 1)]))
    p = st.players["p1"].plots["0,0"]
    assert p.status is PlotStatus.RUNNING, f"1 季工程当季完工，实得 {p.status}"
    assert p.construction_left == 0, "当季完工后剩余工期应为 0"
    assert p.level == 1, "完工后等级落地"
    assert p.running_quarters == 0, "★ 磨合从【首次可用】起算（完工本季 = 0）"


def test_multi_quarter_construction_waits():
    """多季工期要等够季数才算完工（新建一级工业 = 2 季）。

    2026-10-17 用户拍板：投钱投料的当季算第 1 季
    → 2 季工程建成时 left=1，下季空跑后完工。
    """
    st = fresh_state()
    run(st, Plan("p1", builds=[BuildOrder("0,0", PlotType.INDUSTRY, 1, "G")]))
    p = st.players["p1"].plots["0,0"]
    assert p.status is PlotStatus.CONSTRUCTING, "2 季工程当季不完工"
    assert p.construction_left == 1, "投钱投料的当季算第 1 季 → 剩余 1"

    run(st, Plan("p1"))
    p = st.players["p1"].plots["0,0"]
    assert p.status is PlotStatus.RUNNING, "第 2 季应完工"
    assert p.level == 1


def test_upgrade_raises_level():
    """升级：等级从 1 抬到 2，产出保持（建后不可更换）。"""
    st = fresh_state(**{"0,0": mature("0,0", PlotType.AGRICULTURE, 1, product="A")})
    run(st, Plan("p1", builds=[BuildOrder("0,0", PlotType.AGRICULTURE, 2, "A")]))

    p = st.players["p1"].plots["0,0"]
    assert p.status is PlotStatus.CONSTRUCTING
    assert p.target_level == 2
    assert p.level == 1, "升级中，当前等级还是 1"
    assert p.product == "A", "★ 产出建后不可更换"

    run(st, Plan("p1"))
    run(st, Plan("p1"))                      # 农业 1→2 = 2 季
    assert p.level == 2, f"应升到 2 级，实得 {p.level}"
    assert p.status is PlotStatus.RUNNING


def test_materials_are_booked_into_construction_not_inventory():
    """★ 建设物资**离开普通库存**，记进工程的 `construction_materials`。

    ⚠️ 这条对应 §11.2 工程材料分账，也是修过的一个真实 bug 的同族问题：
       材料若留在库存里，会被第 7.5 步的粮食结算当成"能吃的"吃掉。
    """
    st = fresh_state()
    run(st, Plan("p1", builds=[BuildOrder("0,0", PlotType.AGRICULTURE, 1, "A")]))

    p = st.players["p1"].plots["0,0"]
    assert p.construction_materials == {"H-A": 1.0}, (
        f"投入的材料应记进 construction_materials，实得 {p.construction_materials}"
    )
    # H-A 不该出现在玩家的口粮库存里
    assert "H-A" not in st.players["p1"].inventory, (
        f"★ 建设物资不该进库存（会被当口粮吃掉），实得 {st.players['p1'].inventory}"
    )


# ══════════════════════════════════════════════════════════════════
# 四、与校验 / 状态机的配合
# ══════════════════════════════════════════════════════════════════


def test_build_while_constructing_is_rejected():
    """★ 在建的地块**不能再下一条建设指令**（由 validate 挡住）。

    ⚠️ 2026-10-17 起"投钱投料当季算一季工期"→ 1 季工程建成即完工，
       不再处于"在建"状态。故改用 **2 季工期**的新建一级工业（G）
       来构造"在建中"的场景，保住这条意图。
    """
    from engine import validate

    cfg = load_config()
    st = fresh_state()
    run(st, Plan("p1", builds=[BuildOrder("0,0", PlotType.INDUSTRY, 1, "G")]))
    p = st.players["p1"].plots["0,0"]
    assert p.status is PlotStatus.CONSTRUCTING, "2 季工业建成当季应在建"

    player = st.players["p1"]
    plan2 = Plan("p1", builds=[BuildOrder("0,0", PlotType.INDUSTRY, 2, "H-A")])
    issues = validate.validate_plan(plan2, player, cfg)
    assert {i.code for i in issues} == {"plot_already_constructing"}, (
        f"实得：{[(i.code, i.message[:40]) for i in issues]}"
    )


def test_constructing_plot_cannot_run():
    """★ 在建的地块**不能开工**（不提供邻接加成、不产东西）。"""
    st = fresh_state()
    run(st, Plan("p1", builds=[BuildOrder("0,0", PlotType.AGRICULTURE, 1, "A")]))

    r = run(st, Plan("p1", plot_plans={}))
    assert r.production == [], "在建地块不该产出"


def test_insufficient_cash_for_build_raises():
    """钱不够建 → 第 5 步明确报错（不允许部分执行，§11.3）。"""
    from engine.pipeline import SettlementError

    st = fresh_state(cash=100.0)     # 新建一级农业要 144
    try:
        run(st, Plan("p1", builds=[BuildOrder("0,0", PlotType.AGRICULTURE, 1, "A")]))
    except SettlementError as e:
        assert "资金不足" in str(e)
    else:
        raise AssertionError("钱不够应报错")


def test_agriculture_new_build_requires_product():
    """农业新建**必须选产出**（§5.1 建造时选定，建后不可更换）。"""
    from engine import validate

    cfg = load_config()
    st = fresh_state()
    plan = Plan("p1", builds=[BuildOrder("0,0", PlotType.AGRICULTURE, 1, product=None)])
    issues = validate.validate_plan(plan, st.players["p1"], cfg)
    assert {i.code for i in issues} == {"build_product_required"}


def test_build_on_occupied_land_is_upgrade_not_new():
    """已有地块上再下达指令 → 走**升级**表，不是新建表。"""
    cfg = load_config()
    existing = mature("0,0", PlotType.RESOURCE, 1)
    # 新建一级资源 100 现金；升级 1→2 是 120
    w = C.find_work_for_build(
        cfg, existing, BuildOrder("0,0", PlotType.RESOURCE, 2)
    )
    assert w.work_id == "res_1_2" and abs(w.cash - 120) < TOL

    w_new = C.find_work_for_build(
        cfg, None, BuildOrder("0,0", PlotType.RESOURCE, 1)
    )
    assert w_new.work_id == "res_new" and abs(w_new.cash - 100) < TOL


# ══════════════════════════════════════════════════════════════════
# 五、★ 开局分类减免名额（§5.3；2026-10-16 用户裁定：每类 3 × 0.50）
# ══════════════════════════════════════════════════════════════════
#
# 语义（《规则基线》§5.3）：名额按地块类型**独立**、只作用于**新建一级**、
# 只打**现金费**（物资原样）、一次性且**跨季累计**、按 `plan.builds` 顺序消耗。


def test_opening_discount_quota_is_configured():
    """配置落地：每类 3 个名额 × 0.50（用户 2026-10-16 正式裁定）。"""
    cfg = load_config()
    assert cfg.opening_quota == {
        "agriculture": 3, "resource": 3, "industry": 3, "service": 3,
    }, f"每类名额应为 3，实得 {cfg.opening_quota}"
    assert abs(cfg.opening_discount_rate - 0.5) < TOL, (
        f"折扣率应为 0.50，实得 {cfg.opening_discount_rate}"
    )


def test_discount_fourth_new_agriculture_pays_full_price():
    """★ (a) 同一季四块空地新建一级农业：前 3 块各付 40，第 4 块付原价 80。"""
    st = fresh_state(cash=1200.0)
    ids = ["0,0", "0,1", "0,2", "0,3"]
    plan = Plan("p1", builds=[
        BuildOrder(pid, PlotType.AGRICULTURE, 1, "A") for pid in ids
    ])
    # 逐条实付（顺序 = plan.builds 顺序，名额依次消耗）
    # ⚠️ **必须在 `run()` 之前问** —— `build_cash_plan` 是纯函数，同一个 plan
    #    在**结算之后**再问一次，看到的是"名额已被这一季用掉"的状态，
    #    于是四条都变成原价（这正是它该有的确定性语义，不是 bug）。
    #    引擎在 pipeline 第 5 步里也是"同一个 plan、同一次调用"里算钱+扣钱。
    cfg = load_config()
    rows = C.build_cash_plan(plan, st.players["p1"], cfg)
    assert len(rows) == 4, f"应给 4 条明细，实得 {len(rows)}"
    got = [(row["plot_id"], row["cash"], row["discounted"]) for row in rows]
    want = [
        ("0,0", 40.0, True), ("0,1", 40.0, True),
        ("0,2", 40.0, True), ("0,3", 80.0, False),
    ]
    assert got == want, f"逐条实付应为 {want}，实得 {got}"

    r = run(st, plan)

    # 现金费：40 + 40 + 40 + 80 = 200（原价合计 320）
    assert abs(r.construction_cash - 200.0) < TOL, (
        f"四条农业现金费应为 200（3×40 + 80），实得 {r.construction_cash}"
    )
    # 物资：4 块 × H-A×1 = 4 × 64 = 256（物资不打折）
    assert r.construction == {"H-A": 4.0}, f"物资应为 H-A×4，实得 {r.construction}"
    assert abs(r.materials_cost - 256.0) < TOL, (
        f"物资款应为 256，实得 {r.materials_cost}"
    )
    # 总分摊 = 200 + 256 = 456
    assert abs(r.total_deduction - 456.0) < TOL, (
        f"总扣款应为 456（= 现金 200 + 物资 256），实得 {r.total_deduction}"
    )
    # ★ 2026-10-18 S1：补贴季初发放、不进结算 → 期末 = 744 本身
    assert abs(r.subsidy) < TOL, f"S1 起结算报告 subsidy 应为 0，实得 {r.subsidy}"
    assert abs(st.players["p1"].cash - (1200.0 - 456.0)) < TOL, (
        f"期末现金应为 744（S1 起补贴不进账），实得 {st.players['p1'].cash}"
    )
    # 用满 3 个名额
    assert st.players["p1"].opening_quota_used.get("agriculture") == 3, (
        f"农业已用名额应为 3，实得 {st.players['p1'].opening_quota_used}"
    )
    assert C.opening_quota_remaining(st.players["p1"], cfg, PlotType.AGRICULTURE) == 0


def test_upgrade_pays_full_price_no_discount():
    """★ (b) 升级不打折：已有一级农业做二级升级，实付 = 工程表**原价** 80。"""
    cfg = load_config()
    existing = mature("0,0", PlotType.AGRICULTURE, 1, product="A")
    st = fresh_state(cash=1200.0, **{"0,0": existing})

    r = run(st, Plan("p1", builds=[BuildOrder("0,0", PlotType.AGRICULTURE, 2, "A")]))

    work = C.find_work_for_build(
        cfg, existing, BuildOrder("0,0", PlotType.AGRICULTURE, 2, "A")
    )
    assert work.work_id == "agri_1_2", f"应走升级表，实得 {work.work_id}"
    assert abs(r.construction_cash - work.cash) < TOL, (
        f"升级实付应为原价 {work.cash}，实得 {r.construction_cash}"
    )
    assert abs(r.construction_cash - 80.0) < TOL, f"agri_1_2 现金费应为 80，实得 {r.construction_cash}"
    # 升级不吃名额
    assert st.players["p1"].opening_quota_used.get("agriculture", 0) == 0, (
        f"升级不该消耗名额，实得 {st.players['p1'].opening_quota_used}"
    )


def test_materials_are_never_discounted():
    """★ (c) 物资不打折：新建一级农业投入仍是 H-A×1（与不打折时相同）。"""
    st = fresh_state(cash=1200.0)
    r = run(st, Plan("p1", builds=[BuildOrder("0,0", PlotType.AGRICULTURE, 1, "A")]))

    assert r.construction == {"H-A": 1.0}, f"物资应为 H-A×1，实得 {r.construction}"
    assert abs(r.materials_cost - 64.0) < TOL, (
        f"物资款应仍为 H-A 参考价 64（不打折），实得 {r.materials_cost}"
    )
    p = st.players["p1"].plots["0,0"]
    assert p.construction_materials == {"H-A": 1.0}, (
        f"地块记录的投入物资应不打折，实得 {p.construction_materials}"
    )


def test_quota_is_per_plot_type_not_shared():
    """★ (d) 名额按类型独立：农业名额用满后，新建一级资源仍享折扣。"""
    cfg = load_config()
    st = fresh_state(cash=1200.0)
    agri_ids = ["0,0", "0,1", "0,2"]
    run(st, Plan("p1", builds=[
        BuildOrder(pid, PlotType.AGRICULTURE, 1, "A") for pid in agri_ids
    ]))
    assert st.players["p1"].opening_quota_used.get("agriculture") == 3, "农业名额应用满"
    assert C.opening_quota_remaining(st.players["p1"], cfg, PlotType.AGRICULTURE) == 0

    # 同季再建一级资源（res_new 原价 100 → 折后 50）
    plan = Plan("p1", builds=[BuildOrder("5,5", PlotType.RESOURCE, 1)])
    r = run(st, plan)
    assert abs(r.construction_cash - 50.0) < TOL, (
        f"农业名额用满不影响资源，资源应仍享折扣付 50，实得 {r.construction_cash}"
    )
    assert st.players["p1"].opening_quota_used.get("resource") == 1, (
        f"资源名额应记为 1，实得 {st.players['p1'].opening_quota_used}"
    )
    # 农业名额仍是满的、没被挪用
    assert C.opening_quota_remaining(st.players["p1"], cfg, PlotType.AGRICULTURE) == 0


def test_quota_accumulates_across_quarters():
    """★ (e) 名额跨季累计：两季各建 2 块农业 → 第 4 块已无折扣。"""
    st = fresh_state(cash=1200.0)
    r1 = run(st, Plan("p1", builds=[
        BuildOrder("0,0", PlotType.AGRICULTURE, 1, "A"),
        BuildOrder("0,1", PlotType.AGRICULTURE, 1, "A"),
    ]))
    # 第 1 季：2 块都打折 → 40 + 40 = 80
    assert abs(r1.construction_cash - 80.0) < TOL, (
        f"第 1 季应两块都打折（80），实得 {r1.construction_cash}"
    )

    r2 = run(st, Plan("p1", builds=[
        BuildOrder("0,2", PlotType.AGRICULTURE, 1, "A"),
        BuildOrder("0,3", PlotType.AGRICULTURE, 1, "A"),
    ]))
    # 第 2 季：第 3 块是最后 1 个名额 → 40；第 4 块已超名额 → 原价 80
    assert abs(r2.construction_cash - 120.0) < TOL, (
        f"第 2 季应为 40 + 80 = 120（名额跨季累计），实得 {r2.construction_cash}"
    )
    assert st.players["p1"].opening_quota_used.get("agriculture") == 3, (
        f"跨季累计应停在 3（不多记），实得 {st.players['p1'].opening_quota_used}"
    )


def test_opening_quota_used_survives_save_load_roundtrip():
    """★ (f) 存档往返后已用名额仍在（否则读档就能无限刷折扣）。"""
    st = fresh_state(cash=1200.0)
    run(st, Plan("p1", builds=[BuildOrder("0,0", PlotType.AGRICULTURE, 1, "A")]))
    assert st.players["p1"].opening_quota_used.get("agriculture") == 1

    data = st.to_dict()
    assert data["players"]["p1"]["opening_quota_used"] == {"agriculture": 1}, (
        f"存档里应带上已用名额，实得 {data['players']['p1'].get('opening_quota_used')}"
    )

    st2 = GameState.from_dict(data)
    assert st2.players["p1"].opening_quota_used == {"agriculture": 1}, (
        f"读档后已用名额应保持，实得 {st2.players['p1'].opening_quota_used}"
    )
    # 读档后继续消耗：还剩 2 个名额
    cfg = load_config()
    assert C.opening_quota_remaining(st2.players["p1"], cfg, PlotType.AGRICULTURE) == 2
    r = run(st2, Plan("p1", builds=[BuildOrder("1,1", PlotType.AGRICULTURE, 1, "A")]))
    assert abs(r.construction_cash - 40.0) < TOL, (
        f"读档后第 2 块仍应打折，实得 {r.construction_cash}"
    )
    assert st2.players["p1"].opening_quota_used.get("agriculture") == 2, (
        f"读档后名额应累加，实得 {st2.players['p1'].opening_quota_used}"
    )


# ══════════════════════════════════════════════════════════════════
# 命令行汇总
# ══════════════════════════════════════════════════════════════════


def _run_all() -> int:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    mod = sys.modules[__name__]
    names = sorted(n for n in dir(mod) if n.startswith("test_"))
    ok = fail = 0
    print("=" * 68)
    print("建设 / 升级对账（§5.1 / §5.2）")
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
