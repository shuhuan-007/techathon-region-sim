"""`ai/plan_context.py` 的独立测试 —— **AI 可行动作快照**。

跑法（在 `region-sim/` 下）：

    python tests/test_plan_context.py

守护《AI 秘书 · 有效起草方案（2026-10-18）》§三.2 的落点：

    · 开局：100 格空地、4 种新建工程、农业新建折后现金 40、人口为 0；
    · 中局：空地少 1 格、running 地块可开工可升级、岗位 = 配方 d；
    · 在建：不能开工也不能升级，但 `[在建]` 必须列出它；
    · 文本：不含「起草」/「draft」（默认路径回归），长度不失控；
    · 没有可开工地块时，`[可开工]` 必须**明说**，整块不能缺席。

⚠️ 这里用的是 `tests/test_m1.py` 的 `make_state / mature` 夹具（同一套口径），
   不重新造一套地图/地块，避免两份夹具漂移。
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.loader import load_config
from engine.models import GameState, Plot, PlotStatus, PlotType

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
    # 最后一句必须是硬提示（漏写 plot_plans / builds 只能用于空地）
    assert text.splitlines()[-1] == (
        "要开工必须写进 plot_plans，漏写=不开工；"
        "builds 只能用于 [空地] 里的格子。"
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
# 七、命令行 runner（不用 pytest）
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
