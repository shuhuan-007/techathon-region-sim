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
    assert p.status is PlotStatus.CONSTRUCTING
    assert p.construction_left == 1, "一级农业工期 1 季"
    assert p.target_level == 1
    assert p.product == "A", "★ 产出建造时选定"


def test_build_charges_cash_and_materials():
    """扣款 = 工程现金费 + 物资款（两者分开记，§5.1）。"""
    st = fresh_state(cash=1200.0)
    r = run(st, Plan("p1", builds=[BuildOrder("0,0", PlotType.AGRICULTURE, 1, "A")]))

    assert abs(r.construction_cash - 80.0) < TOL, f"现金费应为 80，实得 {r.construction_cash}"
    assert r.construction == {"H-A": 1.0}, f"物资应为 H-A×1，实得 {r.construction}"
    # 物资款 = H-A 参考价 64
    assert abs(r.materials_cost - 64.0) < TOL, f"物资款应为 64，实得 {r.materials_cost}"
    # 总扣款 = 80 + 64 = 144（= 基线 §5.2 的合计列）
    assert abs(r.total_deduction - 144.0) < TOL, (
        f"总扣款应为 144（= 现金 80 + 物资 64），实得 {r.total_deduction}"
    )
    assert abs(st.players["p1"].cash - 1056.0) < TOL, (
        f"期末现金应为 1200 − 144 = 1056，实得 {st.players['p1'].cash}"
    )


def test_construction_completes_after_quarters():
    """★ 工期一到就完工，等级落地、磨合从完工起算。"""
    st = fresh_state()
    run(st, Plan("p1", builds=[BuildOrder("0,0", PlotType.RESOURCE, 1)]))
    p = st.players["p1"].plots["0,0"]
    assert p.construction_left == 1, "一级资源工期 1 季"

    run(st, Plan("p1"))                      # 空计划 = 只推进工期
    p = st.players["p1"].plots["0,0"]
    assert p.status is PlotStatus.RUNNING, f"应已完工，实得 {p.status}"
    assert p.level == 1, "完工后等级落地"
    assert p.running_quarters == 0, "★ 磨合从【首次可用】起算（完工本季 = 0）"


def test_multi_quarter_construction_waits():
    """多季工期要等够季数才算完工（新建一级工业 = 2 季）。"""
    st = fresh_state()
    run(st, Plan("p1", builds=[BuildOrder("0,0", PlotType.INDUSTRY, 1, "G")]))
    p = st.players["p1"].plots["0,0"]
    assert p.construction_left == 2, "工业 G 工期 2 季"

    run(st, Plan("p1"))
    p = st.players["p1"].plots["0,0"]
    assert p.status is PlotStatus.CONSTRUCTING, "才 1 季，不该完工"
    assert p.construction_left == 1

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
    """★ 在建的地块**不能再下一条建设指令**（由 validate 挡住）。"""
    from engine import validate

    cfg = load_config()
    st = fresh_state()
    run(st, Plan("p1", builds=[BuildOrder("0,0", PlotType.AGRICULTURE, 1, "A")]))

    player = st.players["p1"]
    plan2 = Plan("p1", builds=[BuildOrder("0,0", PlotType.AGRICULTURE, 2, "A")])
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
