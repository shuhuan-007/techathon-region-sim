"""`ai/plan_context.py` 的独立测试 —— **AI 可行动作快照**。

跑法（在 `region-sim/` 下）：

    python tests/test_plan_context.py

守护《AI 秘书 · 有效起草方案（2026-10-18）》§三.2 的落点：

    · 开局：100 格空地、4 种新建工程、农业新建折后现金 40、人口为 0；
    · 中局：空地少 1 格、running 地块可开工可升级、岗位 = 配方 d；
    · 在建：不能开工也不能升级，但 `[在建]` 必须列出它；
    · 文本：不含「起草」/「draft」（默认路径回归），长度不失控；
    · 没有可开工地块时，`[可开工]` 必须**明说**，整块不能缺席；
    · 口粮规则（2026-10-18）：`food_rule` 两档数字必须与 `compute_population`
      一致（22 人 → A×1.76 / B×0.88；优秀档 A×2.64 … D×0.132）；
    · 推荐空地（2026-10-18）：分数 = 引擎 `compute_adjacency` 的封顶加成，
      只推空地、同分按行列稳定排序。

⚠️ 这里用的是 `tests/test_m1.py` 的 `make_state / mature` 夹具（同一套口径），
   不重新造一套地图/地块，避免两份夹具漂移。
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.efficiency import compute_adjacency
from engine.loader import load_config
from engine.models import GameState, Plot, PlotStatus, PlotType
from engine.population import compute_population

from ai.plan_context import describe_capabilities, format_capabilities
from tests.test_m1 import make_state, mature

TOL = 1e-9


# ══════════════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════════════


def _opening_state(cfg) -> GameState:
    """开局：**零地块**、现金 1200（`config.world.opening_cash`）。"""
    return GameState(cfg.new_world(), players={"p1": cfg.new_player("p1")})


def _close(actual, expected, tol: float = TOL) -> None:
    """浮点断言（不用 pytest，自己写一个足够用的）。"""
    assert abs(float(actual) - float(expected)) <= tol, (
        f"期望 {expected}，实得 {actual}（差 {float(actual) - float(expected)}）"
    )


# ══════════════════════════════════════════════════════════════════
# 一、开局：空地 / 可建 / 折后现金 / 人口为 0
# ══════════════════════════════════════════════════════════════════


def test_opening_snapshot_empty_builds_and_zero_population():
    cfg = load_config()
    cap = describe_capabilities(_opening_state(cfg), "p1", cfg)

    assert cap["player_id"] == "p1"
    assert cap["quarter"] == 1
    _close(cap["cash"], 1200)

    # ── 空地：10×10 全空；默认截前 10 个 ─────────────────────────
    assert cap["empty_total"] == 100, f"开局空地应为 100，实得 {cap['empty_total']}"
    assert cap["empty_cells"][:3] == ["0,0", "0,1", "0,2"], cap["empty_cells"]
    assert len(cap["empty_cells"]) == 10, "默认只截前 10 个示例"
    assert cap["plots"] == [], "开局没有自己的地块"

    # ── 可建：4 种新建工程，按 plot_type 排序 ────────────────────
    opts = cap["build_options"]
    assert len(opts) == 4, f"开局应有 4 种新建工程，实得 {[o['plot_type'] for o in opts]}"
    assert [o["plot_type"] for o in opts] == [
        "agriculture", "industry", "resource", "service",
    ], [o["plot_type"] for o in opts]

    # 农业新建：现金 80、材料 H-A×1、上季价缺失 → P0 64
    agri = opts[0]
    assert agri["target_level"] == 1
    _close(agri["cash"], 80)
    _close(agri["cash_after_discount"], 40)       # 80 × 0.50
    assert agri["quota_remaining"] == 3, "农业开局名额应剩 3"
    assert agri["materials"] == {"H-A": 1.0}, agri["materials"]
    _close(agri["materials_cost"], 64)            # P0(H-A) = 64
    _close(agri["total_cost"], 104)               # 40 + 64
    assert agri["products"] == ["A", "B", "C", "D"], "农业新建必须给出可选产出"

    # 工业新建：产线只有 G；资源没有多路线 → []
    industry = next(o for o in opts if o["plot_type"] == "industry")
    assert industry["products"] == ["G"], industry["products"]
    resource = next(o for o in opts if o["plot_type"] == "resource")
    assert resource["products"] == [], "资源没有多路线，products 必须是 []"
    _close(resource["materials_cost"], 68)        # P0(H-F) = 68

    # ── 名额表：每类型独立，3 个全额 ─────────────────────────────
    assert cap["quota"]["agriculture"] == {"used": 0, "total": 3, "remaining": 3}

    # ── 人口 / 口粮：0 地块 → 全 0 ───────────────────────────────
    assert cap["population"] == {
        "labor_total": 0.0,
        "labor_food": 0.0,
        "population": 0.0,
        "baseline_food": 0.0,
        "good_food": 0.0,
    }, cap["population"]
    assert set(cap["food_need"]["baseline"]) == {"A", "B"}
    assert set(cap["food_need"]["good"]) == {"A", "B", "C", "D"}
    assert all(v == 0 for v in cap["food_need"]["good"].values())

    # ── 文本块：主实现依赖的块名必须在 ───────────────────────────
    text = format_capabilities(cap)
    for block in ("[现金/预算]", "[空地]", "[可建]", "[可开工]", "[在建]", "[人口/口粮]"):
        assert block in text, f"formatter 缺块 {block}：\n{text}"
    assert "共100格" in text
    # 预算是提示词的硬约束句（主实现会在别的段落引用同一口径）
    assert "先保证 建造现金+材料+工资+经营费+口粮 ≤ cash" in text, text
    # [可建] 的表头与「材料写成 H-A×1」是契约
    assert "[可建] 类型|名|现金|材料|材料价|折后|可产" in text, text
    assert "H-A×1" in text, text
    # 最后一句必须是硬提示（漏写 plot_plans；builds 可新建也可升级）
    assert text.splitlines()[-1] == (
        "要开工必须写进 plot_plans，漏写=不开工；"
        "builds 用于 [空地] 新建，也用于 [可升级] 里已有地块的升级。"
    ), text


# ══════════════════════════════════════════════════════════════════
# 二、中局：空地少 1 格、可开工 / 可升级、岗位 = 配方 d
# ══════════════════════════════════════════════════════════════════


def test_midgame_running_plot_and_labor():
    cfg = load_config()
    st = make_state(cfg, "p1", mature("0,0", PlotType.RESOURCE, 1))
    cap = describe_capabilities(st, "p1", cfg)

    assert cap["empty_total"] == 99, f"占 1 格后应剩 99，实得 {cap['empty_total']}"
    assert "0,0" not in cap["empty_cells"], "自己的地块不能再出现在空地清单里"

    assert len(cap["plots"]) == 1
    plot = cap["plots"][0]
    assert plot["plot_id"] == "0,0"
    assert plot["plot_type"] == "resource"
    assert plot["status"] == "running"
    assert plot["level"] == 1
    assert plot["can_run"] is True
    assert plot["can_upgrade"] is True
    assert plot["next_level"] == 2

    recipe = cfg.recipe(PlotType.RESOURCE, 1, None)
    population = cap["population"]
    _close(population["labor_total"], recipe.d)
    _close(population["labor_food"], recipe.d)
    _close(population["population"], recipe.d * 2)              # N = L × 2
    _close(population["baseline_food"], (recipe.d * 2) / 5)     # (N/5) × m，m=1
    _close(population["good_food"], 1.5 * population["baseline_food"])

    _close(cap["food_need"]["baseline"]["A"], 0.4 * population["baseline_food"])
    _close(cap["food_need"]["good"]["D"], 0.02 * population["good_food"])

    text = format_capabilities(cap)
    assert "[可开工] 0,0(resource L1" in text, text
    assert "[在建] 无" in text, text


# ══════════════════════════════════════════════════════════════════
# 三、在建地块：不能开工、不能升级，但必须出现在 [在建]
# ══════════════════════════════════════════════════════════════════


def test_constructing_plot_is_blocked_but_listed():
    cfg = load_config()
    plot = Plot(
        plot_id="3,3",
        plot_type=PlotType.INDUSTRY,
        level=0,                       # 新建、还没完工（与 pipeline 一致）
        status=PlotStatus.CONSTRUCTING,
        construction_left=1,
        target_level=1,
        product="G",
    )
    cap = describe_capabilities(make_state(cfg, "p1", plot), "p1", cfg)

    assert cap["empty_total"] == 99
    row = cap["plots"][0]
    assert row["status"] == "constructing"
    assert row["can_run"] is False, "在建地块不能开工"
    assert row["can_upgrade"] is False, "在建地块不能再下升级指令"
    assert row["next_level"] is None

    text = format_capabilities(cap)
    assert "[在建] 3,3(industry L0 在建)" in text, text
    assert "[可开工] 没有可开工地块" in text, "在建地块不算可开工"


# ══════════════════════════════════════════════════════════════════
# 四、文本卫生：不含「起草」/「draft」，长度不失控
# ══════════════════════════════════════════════════════════════════


def test_formatter_has_no_draft_words_and_stays_short():
    cfg = load_config()
    caps = [
        describe_capabilities(_opening_state(cfg), "p1", cfg),
        describe_capabilities(
            make_state(cfg, "p1", mature("0,0", PlotType.RESOURCE, 1)), "p1", cfg,
        ),
    ]
    for cap in caps:
        text = format_capabilities(cap)
        assert "起草" not in text, f"默认路径会拼这段文本，不许出现「起草」：\n{text}"
        assert "draft" not in text.lower(), f"默认路径会拼这段文本，不许出现 draft：\n{text}"
        assert len(text) <= 2500, f"formatter 过长：{len(text)} 字符"
        assert len(text) >= 300, f"文本短得可疑（{len(text)} 字符），块可能缺了：\n{text}"


# ══════════════════════════════════════════════════════════════════
# 五、没有可开工地块：[可开工] 必须明说，不能整块缺席
# ══════════════════════════════════════════════════════════════════


def test_no_runnable_plots_is_said_explicitly():
    cfg = load_config()
    cap = describe_capabilities(_opening_state(cfg), "p1", cfg)
    text = format_capabilities(cap)

    assert "[可开工]" in text, f"[可开工] 整块缺席：\n{text}"
    assert "没有可开工地块" in text, f"[可开工] 没有明说：\n{text}"
    assert "[在建] 无" in text, f"[在建] 没有写「无」：\n{text}"
    assert "[空地] 没有空地" not in text, "开局有 100 格空地，不该说没有空地"


# ══════════════════════════════════════════════════════════════════
# 六、边角：max_empty 只截示例 / 名额用完恢复原价 / 别人的地块不算空地
# ══════════════════════════════════════════════════════════════════


def test_max_empty_truncates_examples_only():
    cfg = load_config()
    cap = describe_capabilities(_opening_state(cfg), "p1", cfg, max_empty=2)
    assert cap["empty_total"] == 100, "max_empty 只截示例，不动总数"
    assert cap["empty_cells"] == ["0,0", "0,1"]


def test_discount_respects_remaining_quota():
    cfg = load_config()
    st = _opening_state(cfg)
    st.players["p1"].opening_quota_used = {"agriculture": 3}

    cap = describe_capabilities(st, "p1", cfg)
    agri = next(o for o in cap["build_options"] if o["plot_type"] == "agriculture")
    assert agri["quota_remaining"] == 0
    _close(agri["cash_after_discount"], agri["cash"])   # 名额用完必须是原价
    assert cap["quota"]["agriculture"] == {"used": 3, "total": 3, "remaining": 0}


def test_other_players_plots_are_not_empty():
    cfg = load_config()
    st = GameState(cfg.new_world(), players={
        "p1": cfg.new_player("p1"),
        "p2": cfg.new_player("p2"),
    })
    st.players["p2"].plots["0,0"] = mature("0,0", PlotType.RESOURCE, 1)

    cap = describe_capabilities(st, "p1", cfg)
    assert cap["empty_total"] == 99, "别的玩家占的格子也不能算空地"
    assert "0,0" not in cap["empty_cells"]
    assert cap["plots"] == [], "只列自己的地块"


def test_describe_capabilities_is_readonly():
    """★ 快照是给提示词用的，**一个字段都不许改**（AI 无 commit 的前置）。"""
    cfg = load_config()
    st = make_state(cfg, "p1", mature("0,0", PlotType.RESOURCE, 1))
    before = st.to_dict()
    describe_capabilities(st, "p1", cfg)
    assert st.to_dict() == before, "describe_capabilities 改了状态"


# ══════════════════════════════════════════════════════════════════
# 七、口粮规则（A）：两档需求数字必须与 compute_population 一致
# ══════════════════════════════════════════════════════════════════


def test_food_rule_matches_compute_population_22_people():
    """按用户真跑的例子造 22 人：3 资源(d=3) + 1 农业(d=2) → L=11、N=22。

    期望（与用户举例一致）：
        F_基准 = 22/5 = 4.4 → A×1.76 B×0.88
        F_优秀 = 6.6        → A×2.64 B×1.32 C×0.66 D×0.132
    """
    cfg = load_config()
    st = make_state(cfg, "p1", mature("0,0", PlotType.RESOURCE, 1))
    plots = st.player("p1").plots
    plots["0,1"] = mature("0,1", PlotType.RESOURCE, 1)
    plots["0,2"] = mature("0,2", PlotType.RESOURCE, 1)
    plots["0,3"] = mature("0,3", PlotType.AGRICULTURE, 1, product="A")

    pop = compute_population(st, "p1", {}, cfg)
    _close(pop.labor_food, 11)          # 3+3+3+2
    _close(pop.population, 22)
    _close(pop.baseline_food, 4.4)
    _close(pop.good_food, 6.6)

    cap = describe_capabilities(st, "p1", cfg)
    rule = cap["food_rule"]
    _close(rule["baseline_food"], pop.baseline_food)
    _close(rule["good_food"], pop.good_food)

    # 两档商品集合：基准只有 A/B，优秀才有 C/D
    assert set(rule["baseline_need"]) == {"A", "B"}, rule["baseline_need"]
    assert set(rule["good_need"]) == {"A", "B", "C", "D"}, rule["good_need"]
    for goods, ratio in cfg.food.baseline_quota.items():
        _close(rule["baseline_need"][goods], ratio * pop.baseline_food)
    for goods, ratio in cfg.food.good_quota.items():
        _close(rule["good_need"][goods], ratio * pop.good_food)
    # 与用户举例的精确数对照（同时说明「22」不可能是一档口粮量）
    _close(rule["baseline_need"]["A"], 1.76)
    _close(rule["baseline_need"]["B"], 0.88)
    _close(rule["good_need"]["A"], 2.64)
    _close(rule["good_need"]["B"], 1.32)
    _close(rule["good_need"]["C"], 0.66)
    _close(rule["good_need"]["D"], 0.132)
    assert rule["good_need"]["A"] < 3, "优秀档 A 只有 2.64，不是 22"

    # note 是一句人话：提到 F、A/B、C/D、丢弃
    note = rule["note"]
    assert isinstance(note, str) and note, rule
    for piece in ("F", "A", "B", "C", "D", "丢弃"):
        assert piece in note, f"口粮规则 note 缺 {piece}：{note}"

    # 文本块：直接写当前数字 + 公式
    text = format_capabilities(cap)
    assert "[口粮规则]" in text, text
    for piece in (
        "人口=22", "F_基准=4.4", "A×1.76", "B×0.88",
        "F_优秀=6.6", "A×2.64", "B×1.32", "C×0.66", "D×0.132",
        "多买会被丢弃",
    ):
        assert piece in text, f"口粮规则文本缺 {piece}：\n{text}"


# ══════════════════════════════════════════════════════════════════
# 七-b、完工后人口（★ 2026-10-18 R2）
#   结算第 1 步先完工、第 4 步才算人口 → 快照的 F 与逐商品需求必须同口径。
# ══════════════════════════════════════════════════════════════════


def test_food_rule_uses_post_completion_population():
    """`construction_left == 1` 的地块本季完工 → 快照的两档 F 必须含它。

    旧口径只看当前 RUNNING：实测 `8550`（4 块本季完工）给 A=66.41 而不是
    69.83，模型照着填就掉到 H=0.6（`good_met=False`）。
    """
    from ai.plan_compile import population_after_completion

    cfg = load_config()
    st = make_state(cfg, "p1", mature("5,5", PlotType.AGRICULTURE, 1, product="A"))
    pre = compute_population(st, "p1", {}, cfg)
    st.player("p1").plots["5,6"] = Plot(
        plot_id="5,6", plot_type=PlotType.AGRICULTURE, level=1,
        status=PlotStatus.CONSTRUCTING, running_quarters=0,
        construction_left=1, target_level=2, product="A",
    )
    post = population_after_completion(st, "p1", cfg)
    assert post.good_food > pre.good_food + 1e-9, "夹具必须真的本季完工"

    cap = describe_capabilities(st, "p1", cfg)
    _close(cap["population"]["labor_food"], post.labor_food)
    _close(cap["population"]["population"], post.population)
    _close(cap["food_rule"]["baseline_food"], post.baseline_food)
    _close(cap["food_rule"]["good_food"], post.good_food)
    for goods, ratio in cfg.food.baseline_quota.items():
        _close(cap["food_rule"]["baseline_need"][goods], ratio * post.baseline_food)
    for goods, ratio in cfg.food.good_quota.items():
        _close(cap["food_rule"]["good_need"][goods], ratio * post.good_food)

    # 文本块写出的也是完工后数字（模型照抄 `[口粮规则]` 那一行就够）
    text = format_capabilities(cap)
    assert f"人口={post.population:g}" in text, text
    for goods, ratio in cfg.food.good_quota.items():
        shown = f"{goods}×{round(ratio * post.good_food, 3):g}"
        assert shown in text, f"[口粮规则] 缺完工后数字 {shown}：\n{text}"


# ══════════════════════════════════════════════════════════════════
# 八、推荐空地（B）：真加成、只推空地、排序稳定
# ══════════════════════════════════════════════════════════════════


def _probe(plot_id: str, ptype: PlotType) -> Plot:
    """探测用临时地块（模拟「在这块空地上新建一个 X」）。"""
    return Plot(plot_id=plot_id, plot_type=ptype, level=1, status=PlotStatus.RUNNING)


def test_recommended_build_cells_use_engine_adjacency_and_stable_order():
    cfg = load_config()
    st = make_state(cfg, "p1", mature("5,5", PlotType.AGRICULTURE, 1, product="A"))
    cap = describe_capabilities(st, "p1", cfg)
    cells = cap["recommended_build_cells"]

    assert cells, "有一块农地时，周边空地必须被推荐"
    assert len(cells) == 5, f"默认给前 5 个，实得 {len(cells)}"
    # 5,5 的八邻格全部只挨着这一块农地 → 同分，按 (行,列) 升序取前 5
    assert [c["plot_id"] for c in cells] == ["4,4", "4,5", "4,6", "5,4", "5,6"], [
        c["plot_id"] for c in cells
    ]

    own_plots = set(st.player("p1").plots)
    for cell in cells:
        assert cell["plot_id"] not in own_plots, "推荐里不能出现已有地块"
        assert cell["score"] > 0, "零加成的格子不该进推荐"
        assert cell["neighbors"], "有加成就必须能说出是哪个邻居给的"

        # ★ 分数 = 引擎 compute_adjacency 的封顶加成（四种可新建类型取最高）
        best = max(
            compute_adjacency(st, "p1", _probe(cell["plot_id"], t), cfg)[3]
            for t in (
                PlotType.AGRICULTURE, PlotType.RESOURCE,
                PlotType.INDUSTRY, PlotType.SERVICE,
            )
        )
        _close(cell["score"], best)
        _close(cell["bonus_pct"], best * 100)

        for n in cell["neighbors"]:
            assert n["plot_id"] in st.world.neighbor_ids(cell["plot_id"])
            assert n["plot_type"] == PlotType.AGRICULTURE.value, n
            assert n["level"] == 1, n
            _close(n["bonus_pct"], 5.0)   # 当前配置：每个关联邻居 +5%

    # 第一个推荐格的邻居明细必须能对上（邻 5,5 农业 +5%）
    assert [n["plot_id"] for n in cells[0]["neighbors"]] == ["5,5"], cells[0]
    assert cells[0]["neighbors"][0]["plot_type"] == "agriculture"

    # 稳定：同一局面重复调用，顺序逐字相同
    again = describe_capabilities(st, "p1", cfg)["recommended_build_cells"]
    assert [c["plot_id"] for c in again] == [c["plot_id"] for c in cells]
    assert [c["score"] for c in again] == [c["score"] for c in cells]

    # 开局没有邻居 → 空列表（而不是硬推 0,0）
    opening = describe_capabilities(_opening_state(cfg), "p1", cfg)
    assert opening["recommended_build_cells"] == [], opening["recommended_build_cells"]


# ══════════════════════════════════════════════════════════════════
# 九、文本：新增 [口粮规则] / [推荐空地]，旧 [人口/口粮] 不许删
# ══════════════════════════════════════════════════════════════════


def test_formatter_has_food_rule_and_recommended_blocks():
    cfg = load_config()
    cap = describe_capabilities(
        make_state(cfg, "p1", mature("5,5", PlotType.AGRICULTURE, 1, product="A")),
        "p1", cfg,
    )
    text = format_capabilities(cap)

    assert "[口粮规则]" in text, text
    assert "[推荐空地]" in text, text
    assert "[人口/口粮]" in text, "旧块不许删（主实现与旧测试依赖）"
    assert "[推荐空地] 暂无" not in text, "有邻居时不该说暂无"
    assert "5,5 农业+5%" in text, text
    assert "优先从 [推荐空地] 里选 build 的格子" in text, text

    # 开局没有邻居 → [推荐空地] 明说「先建第一块」，且不给软提示
    text0 = format_capabilities(describe_capabilities(_opening_state(cfg), "p1", cfg))
    assert "[推荐空地] 暂无（先建第一块）" in text0, text0
    assert "优先从 [推荐空地]" not in text0, text0
    assert "[口粮规则]" in text0, text0


# ══════════════════════════════════════════════════════════════════
# 十、命令行 runner（不用 pytest）
# ══════════════════════════════════════════════════════════════════


def _print_samples() -> None:
    """把开局 / 中局两段 formatter 文本打出来（人眼核对 + 汇报样例）。"""
    cfg = load_config()
    samples = [
        ("开局（0 地块、现金 1200）", describe_capabilities(_opening_state(cfg), "p1", cfg)),
        ("中局（0,0 一块 running 资源 L1）", describe_capabilities(
            make_state(cfg, "p1", mature("0,0", PlotType.RESOURCE, 1)), "p1", cfg,
        )),
    ]
    for title, cap in samples:
        text = format_capabilities(cap)
        print("\n" + "-" * 68)
        print(f"【{title}】{len(text)} 字符")
        print("-" * 68)
        print(text)


def test_upgrade_options_cover_industry_routes_and_research():
    """AI 必须看见已有地块的升级选项：一级 G 两条、二级机械只对应 +、研究只 I。"""
    cfg = load_config()
    st = GameState(cfg.new_world(), players={"p1": cfg.new_player("p1")})
    plots = {
        "3,4": mature("3,4", PlotType.INDUSTRY, 1, "G"),
        "4,4": mature("4,4", PlotType.INDUSTRY, 2, "H-A"),
        "5,4": mature("5,4", PlotType.INDUSTRY, 2, "研究"),
        "6,4": mature("6,4", PlotType.AGRICULTURE, 1, "A"),
    }
    for pid, p in plots.items():
        st.player("p1").plots[pid] = p

    cap = describe_capabilities(st, "p1", cfg)
    rows = cap["upgrade_options"]
    by_plot: dict[str, list[dict]] = {}
    for row in rows:
        by_plot.setdefault(row["plot_id"], []).append(row)

    l1 = {r["work_id"]: r for r in by_plot["3,4"]}
    assert set(l1) == {"ind_1_2_machine", "ind_1_2_research"}, l1
    assert l1["ind_1_2_machine"]["products"] == ["H-A", "H-F", "H-G", "H-J"]
    assert l1["ind_1_2_research"]["fixed_product"] == "研究"
    assert l1["ind_1_2_research"]["products"] == []

    h_a = by_plot["4,4"]
    assert [r["work_id"] for r in h_a] == ["ind_2_3_machine"], h_a
    assert h_a[0]["product_from_current"] is True
    assert h_a[0]["target_level"] == 3

    research = by_plot["5,4"]
    assert [r["work_id"] for r in research] == ["ind_2_3_high"], research
    assert research[0]["fixed_product"] == "I"

    text = format_capabilities(cap)
    assert "[可升级]" in text and "二级研究" in text, text
    assert "[可升级] 无" not in text, text


def test_opening_advice_triggers_only_when_opening():
    """★ 2026-10-18 用户拍板：AI 开局不能只给 2 块。`opening_advice` 在
    「自己 0–1 块地 + 现金 ≥ 1000」时触发，快照与文本都得带 `[开局建议]`。
    """
    cfg = load_config()

    # ① 开局（0 地块 + 现金 1200）→ 触发
    cap0 = describe_capabilities(_opening_state(cfg), "p1", cfg)
    advice = cap0["opening_advice"]
    assert isinstance(advice, dict) and advice.get("active") is True, advice
    assert advice["own_plot_count"] == 0
    assert advice["suggested_plot_count"] == 3
    assert advice["priority_types"] == ["agriculture", "resource", "industry"], advice
    note = advice["note"]
    for piece in ("3 块", "农业", "资源", "工业", "2 块"):
        assert piece in note, f"开局建议 note 缺 {piece}：{note}"
    text0 = format_capabilities(cap0)
    assert "[开局建议]" in text0, f"开局文本要带 [开局建议] 块：\n{text0}"
    assert "3 块" in text0 and "1 农业" in text0 and "1 资源" in text0 and "1 工业" in text0, text0
    assert "[可建]" in text0 and "[推荐空地]" in text0, "提示不许盖掉数字块"

    # ② 1 块地 + 现金 1000 整 → 仍触发（边缘）
    st1 = _opening_state(cfg)
    st1.players["p1"].cash = 1000.0
    st1.players["p1"].plots["5,5"] = mature("5,5", PlotType.AGRICULTURE, 1, product="A")
    cap1 = describe_capabilities(st1, "p1", cfg)
    assert cap1["opening_advice"]["active"] is True, cap1["opening_advice"]
    assert cap1["opening_advice"]["own_plot_count"] == 1

    # ③ ≥ 2 块地 → 不再触发（开局已过，提示是添乱）
    st2 = _opening_state(cfg)
    st2.players["p1"].plots["5,5"] = mature("5,5", PlotType.AGRICULTURE, 1, product="A")
    st2.players["p1"].plots["5,6"] = mature("5,6", PlotType.RESOURCE, 1)
    cap2 = describe_capabilities(st2, "p1", cfg)
    assert cap2["opening_advice"] is None, cap2["opening_advice"]
    text2 = format_capabilities(cap2)
    assert "[开局建议]" not in text2, f"非开局时 [开局建议] 应整块缺席：\n{text2}"

    # ④ 0 块地但现金 500 → 不触发（没钱铺 3 块，提示是空头支票）
    st3 = _opening_state(cfg)
    st3.players["p1"].cash = 500.0
    cap3 = describe_capabilities(st3, "p1", cfg)
    assert cap3["opening_advice"] is None, cap3["opening_advice"]
    assert "[开局建议]" not in format_capabilities(cap3)


# ══════════════════════════════════════════════════════════════════
# 十一、★ 第七轮 C：`[策略提示]` 块 — 存在、位置、行格式、不许缺席
# ══════════════════════════════════════════════════════════════════


def test_strategy_block_sits_between_recommended_and_build():
    """`[策略提示]` 必须排在 `[推荐空地]`（建哪）之后、`[可建]`（建什么）之前。

    契约来源：第七轮 C 任务书「块名即契约，排在 [推荐空地] 后、[可建] 前」；
    `server/ai_host.py` 的起草要求会按这个名字指代它。
    """
    cfg = load_config()
    st = GameState(cfg.new_world(), players={"p1": cfg.new_player("p1")})
    plots = {
        "4,4": mature("4,4", PlotType.INDUSTRY, 1, "G"),
        "4,5": mature("4,5", PlotType.INDUSTRY, 1, "G"),
        "5,4": mature("5,4", PlotType.INDUSTRY, 1, "G"),
    }
    for plot_id, plot in plots.items():
        st.player("p1").plots[plot_id] = plot
    # 现金 950、上季总扣款 900（= 94.7% ≥ 90%）→ 现金红线那一条也会出
    st.player("p1").cash = 950.0
    st.report = {"p1": {"total_deduction": 900.0, "construction_cash": 0.0}}

    cap = describe_capabilities(st, "p1", cfg)
    assert "strategy_lines" in cap, "describe_capabilities 必须给 strategy_lines 键"
    lines = cap["strategy_lines"]
    assert isinstance(lines, list) and lines, lines
    assert len(lines) <= 4, f"最多 4 行（[策略提示] 的合同）：{lines}"

    text = format_capabilities(cap)
    i_rec = text.index("[推荐空地]")
    i_str = text.index("[策略提示]")
    i_build = text.index("[可建]")
    assert i_rec < i_str < i_build, (
        f"块序必须是 [推荐空地] → [策略提示] → [可建]：\n{text}"
    )
    assert text.count("[策略提示]") == 1, "块名只该出现一次"

    # 块内每行以 `- ` 开头（模型一眼能看出是条目）
    block = text[i_str:i_build].strip().splitlines()
    assert block[0] == "[策略提示]", block
    content = [line for line in block[1:] if line.strip()]
    assert content, "有内容时不能让块空着"
    assert len(content) <= 4, content
    for line in content:
        assert line.startswith("- "), f"块内每行必须 `- ` 开头：{line!r}"

    # 这一局四件事都有：服务（3 邻）/ 升级（G→H-*）/ 市场余量 / 现金红线
    assert any("建议在 5,5" in line for line in content), content
    assert any("升级 " in line for line in content), content
    assert any("市场余量" in line for line in content), content
    assert any("总扣款" in line for line in content), content


def test_strategy_block_never_absent():
    """没有可说的 → 整块写 `[策略提示] 暂无`（**不许缺席**，与其它块同口径）。"""
    cfg = load_config()
    cap0 = describe_capabilities(_opening_state(cfg), "p1", cfg)
    text0 = format_capabilities(cap0)
    assert "[策略提示]" in text0, f"开局也必须带 [策略提示]：\n{text0}"

    # 直接喂一个空 strategy_lines 的最小 cap：渲染器只能写「暂无」
    minimal = {
        "cash": 0,
        "quarter": 1,
        "empty_total": 0,
        "empty_cells": [],
        "strategy_lines": [],
    }
    text = format_capabilities(minimal)
    assert "[策略提示] 暂无" in text, text
    assert "\n- " not in text, f"没有内容时不许有 `- ` 行：\n{text}"


def test_strategy_block_caps_at_four_bullets():
    """渲染器自己也要截断：给 6 条只出 4 条（防止上游多塞把上下文撑爆）。"""
    minimal = {
        "cash": 0,
        "quarter": 1,
        "empty_total": 0,
        "empty_cells": [],
        "strategy_lines": [f"第{i}条" for i in range(1, 7)],
    }
    text = format_capabilities(minimal)
    block = text.split("[策略提示]", 1)[1].split("[可建]", 1)[0]
    bullets = [line for line in block.splitlines() if line.startswith("- ")]
    assert len(bullets) == 4, bullets
    assert bullets[0] == "- 第1条" and bullets[-1] == "- 第4条", bullets


def _run_all() -> int:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    mod = sys.modules[__name__]
    names = sorted(n for n in dir(mod) if n.startswith("test_"))
    ok = fail = 0
    print("=" * 68)
    print("AI 可行动作快照（ai/plan_context.py）")
    print("=" * 68)
    for name in names:
        try:
            getattr(mod, name)()
            print(f"  PASS  {name}")
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"  FAIL  {name}\n        {type(e).__name__}: {e}")
            fail += 1
    print(f"\n{ok} passed, {fail} failed, {len(names)} total")
    return 1 if fail else 0


if __name__ == "__main__":
    rc = _run_all()
    _print_samples()
    raise SystemExit(rc)
