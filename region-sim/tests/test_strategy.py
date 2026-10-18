"""`ai/strategy.py` 的独立测试 —— **AI 的策略提示层**（第七轮 C）。

跑法（在 `region-sim/` 下，**stdlib runner，不用 pytest**）：

    python tests/test_strategy.py

守护任务书《秘书 AI「稍微有点脑子」任务书》的四条：

    1. 服务选址：3 块工业围着 `5,5` → 必须推荐 `5,5`，且写清邻数 / +15%；
       邻居不足 3 块、已有 3 块服务、地图已满 → `None`；
    2. 升级经济性：造一块工业 G，把 H-A 价顶到成本之上 → 判「划算」，
       并给出 MC / 每季 GDP / 现金成本；四个 H-* 全砸到成本之下 → 判「不划算」；
    3. 市场余量：P0 下为正、把 G 价压到成本之下 → slack 转负且余量截 0；
       缺弹性的商品**跳过而不是炸**；
    4. 现金红线：`total_deduction > cash` → 有提醒；贴线（≥90%）→ 有提醒；
       远低于 → `None`；没有报告 → `None`。

还守两条边界：**纯只读**（`state.to_dict()` 前后逐字相同）、
**`strategy_lines` 最多 4 行**（`[策略提示]` 的合同）。

⚠️ 夹具复用 `tests/test_m1.py` 的 `make_state / mature`（同一套口径），
   不另造一份地图，避免两份夹具漂移。
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.loader import load_config
from engine.models import GameState, PlotType

from ai import strategy as ST
from tests.test_m1 import make_state, mature

TOL = 1e-6


def _close(actual, expected, tol: float = TOL) -> None:
    """浮点断言（与 test_plan_context 同写法，不引 pytest）。"""
    assert abs(float(actual) - float(expected)) <= tol, (
        f"期望 {expected}，实得 {actual}（差 {float(actual) - float(expected)}）"
    )


def _industry_row(cfg, plot_ids=("4,4", "4,5", "5,4")):
    """三块**相邻**的一级工业 G 围着 `5,5`（服务选址的标准夹具）。"""
    first = plot_ids[0]
    st = make_state(cfg, "p1", mature(first, PlotType.INDUSTRY, 1, "G"))
    for plot_id in plot_ids[1:]:
        st.player("p1").plots[plot_id] = mature(plot_id, PlotType.INDUSTRY, 1, "G")
    return st


# ══════════════════════════════════════════════════════════════════
# 一、服务选址
# ══════════════════════════════════════════════════════════════════


def test_service_hint_picks_the_cell_with_three_running_neighbors():
    """3 块工业围着 5,5 → 推荐 5,5，邻 3 块、+15%、服务收入 3×6=18/季。"""
    cfg = load_config()
    st = _industry_row(cfg)

    hint = ST.service_hint(st, "p1", cfg)
    assert hint, "3 个邻居应当值得建服务"
    assert "5,5" in hint, f"应当推荐 5,5（八邻格唯一同时挨着三块地）：{hint}"
    assert "邻 3 块" in hint, hint
    assert "+15%" in hint, f"邻 3 × 5% = +15%：{hint}"
    assert "服务收入 18" in hint, f"3 邻 × 6 现金 = 18/季：{hint}"
    # GDP 增量必须是真的正数（1 级服务给别人 +5% 产出，按 P0 折 GDP）
    assert "GDP/季" in hint, hint
    assert "≈ 0 GDP/季" not in hint, f"3 块工业的 +5% 不可能折成 0 GDP：{hint}"


def test_service_hint_is_none_when_not_worth_it():
    """邻居 <3 / 已有 3 块服务 / 地图已满 → 一律 `None`（不硬凑一句）。"""
    cfg = load_config()

    # ① 只有一个邻居（1 块资源地）→ 不值得占一格
    st1 = make_state(cfg, "p1", mature("0,0", PlotType.RESOURCE, 1))
    assert ST.service_hint(st1, "p1", cfg) is None

    # ② 已有 3 块服务 → 服务之间不加成，不再提示新建
    st2 = _industry_row(cfg)
    for plot_id in ("0,0", "0,1", "0,2"):
        st2.player("p1").plots[plot_id] = mature(plot_id, PlotType.SERVICE, 1)
    assert ST.service_hint(st2, "p1", cfg) is None, "服务已满 3 块还提示新建"

    # ③ 地图已满（没有空地）→ None
    st3 = make_state(cfg, "p1", mature("0,0", PlotType.RESOURCE, 1))
    st3.world.rows, st3.world.cols = 1, 1          # 1×1 地图，唯一格子已被占
    assert ST.service_hint(st3, "p1", cfg) is None


# ══════════════════════════════════════════════════════════════════
# 二、升级经济性
# ══════════════════════════════════════════════════════════════════


def test_upgrade_economics_marks_profitable_and_uses_configured_cost():
    """H-A 价 100 ≫ 成本 37 → 「划算」，且该行确实按工程表 + 上季价算钱。

    锚点（全部可手算，config 值）：
        MC(H-A) = (3×P0(G)=56 + 2×P0(F)=24 + 0.7×80 + 0.3×80) / 8 = 37
        margin  = e×Ê×(100−37) = 8×1×(63) = 504 GDP/季
        现金    = 工程 240 + H-G×3 = 240 + 3×73 = 459
    """
    cfg = load_config()
    st = make_state(cfg, "p1", mature("4,5", PlotType.INDUSTRY, 1, "G"))
    st.world.price_history["H-A"] = [100.0]

    rows = ST.upgrade_economics(st, "p1", cfg)
    assert rows, "一级工业 G 必须出现在升级候选里"
    best = rows[0]
    assert best["plot_id"] == "4,5" and best["from_product"] == "G"
    assert best["product"] == "H-A", f"H-A 价 100 时应当选 H-A：{best['product']}"
    assert best["to_level"] == 2 and best["quarters"] == 2
    _close(best["unit_cost"], 37.0)
    _close(best["margin_gdp"], 504.0)
    _close(best["cash_cost"], 459.0)
    assert best["profitable"] is True
    _close(best["price"], 100.0)

    # 排序 = ΔGDP / 现金成本 降序（H-A 1.098 > H-G .505 > H-F .505 > H-J .471）
    ratios = [row["margin_ratio"] for row in rows]
    assert ratios == sorted(ratios, reverse=True), ratios

    line = ST.format_upgrade_line(rows)
    assert line and "H-A" in line and ">" in line, line
    assert "升级 4,5 G→H-A" in line, f"行的形状是契约：{line}"
    assert "+504 GDP/季" in line, line
    assert "459 现金" in line and "2 季" in line, line


def test_upgrade_economics_says_loss_when_price_below_cost():
    """四个 H-* 全价 10 < 成本 → 每行都 `profitable=False`，句子写「不划算」。"""
    cfg = load_config()
    st = make_state(cfg, "p1", mature("4,5", PlotType.INDUSTRY, 1, "G"))
    for goods in ("H-A", "H-F", "H-G", "H-J"):
        st.world.price_history[goods] = [10.0]

    rows = ST.upgrade_economics(st, "p1", cfg)
    assert rows, "全亏也要给出行（要能说清谁亏多少）"
    assert all(row["profitable"] is False for row in rows), rows
    assert all(row["margin_gdp"] < 0 for row in rows), rows

    line = ST.format_upgrade_line(rows)
    assert line and "不划算" in line, line
    assert "亏" in line and "< 成本" in line, line


def test_upgrade_economics_skips_research_route_and_non_level1():
    """只评一级地块；研究线（不产商品、没有市场价）不进候选。"""
    cfg = load_config()
    st = GameState(cfg.new_world(), players={"p1": cfg.new_player("p1")})
    st.players["p1"].plots["3,3"] = mature("3,3", PlotType.AGRICULTURE, 2, "A")   # 二级
    st.players["p1"].plots["3,4"] = mature("3,4", PlotType.RESOURCE, 1)           # 一级资源
    rows = ST.upgrade_economics(st, "p1", cfg)
    assert {row["plot_id"] for row in rows} == {"3,4"}, "二级地块不该进升级候选"
    assert all(row["plot_type"] == "resource" for row in rows), rows
    # 产物不变的升级写成等级迁移（`F 1→2 级`），不能写成 `F→F`
    line = ST.format_upgrade_line(rows)
    assert line and "F 1→2 级" in line, line


# ══════════════════════════════════════════════════════════════════
# 三、市场余量
# ══════════════════════════════════════════════════════════════════


def test_market_headroom_positive_at_p0_and_zero_when_under_cost():
    """P0 口径：G 有肉（slack=22）；把 G 价压到 10 → slack<0、余量截 0。"""
    cfg = load_config()
    st = make_state(cfg, "p1", mature("0,0", PlotType.RESOURCE, 1))

    rows = ST.market_headroom(st, cfg, limit=len(ST.HEADROOM_GOODS))
    by_goods = {row["goods"]: row for row in rows}
    assert "G" in by_goods, f"G 必须被评估：{sorted(by_goods)}"

    g = by_goods["G"]
    # MC(G) = (2×16 + 26 + 3×24 + 0.7×40 + 0.3×40) / 5 = 34；P0 = 56
    _close(g["unit_cost"], 34.0)
    _close(g["price"], 56.0)
    _close(g["slack"], 22.0)
    assert g["profitable"] is True
    # ΔQ = 22 × (s+d) = 22 × (5.357142857 + 4.285714286) ≈ 212.14
    _close(g["headroom_units"], 22.0 * (5.357142857142857 + 4.285714285714286), 1e-6)
    _close(g["headroom_plots"], g["headroom_units"] / 5.0)
    assert g["headroom_units"] > 0

    # 顶端排序 = 余量降序（G 212 > F 206 > C 179）
    units = [row["headroom_units"] for row in rows]
    assert units == sorted(units, reverse=True), units
    line = ST.format_market_line(rows[:3])
    assert line and line.startswith("市场余量："), line
    assert "还能卖" in line and "块" in line, line

    # 把 G 价砸到 10（低于成本 34）→ slack 转负、余量截 0
    st.world.price_history["G"] = [10.0]
    rows2 = {
        row["goods"]: row
        for row in ST.market_headroom(st, cfg, limit=len(ST.HEADROOM_GOODS))
    }
    g2 = rows2["G"]
    _close(g2["slack"], 10.0 - g2["unit_cost"])
    assert g2["slack"] < 0 and g2["headroom_units"] == 0.0
    assert g2["profitable"] is False
    assert "别再扩产" in ST.format_market_line([g2])


def test_market_headroom_skips_goods_without_elasticity():
    """缺 Es/Ed 的商品**跳过**（提示层不编斜率），其它商品照出不炸。"""
    cfg = load_config()
    st = make_state(cfg, "p1", mature("0,0", PlotType.RESOURCE, 1))
    st.world.market["G"].Es = None                    # 模拟弹性未填

    rows = ST.market_headroom(st, cfg, limit=len(ST.HEADROOM_GOODS))
    assert "G" not in [row["goods"] for row in rows], "缺弹性还给出余量＝编数据"
    assert rows, "其它商品的余量不该被一颗坏曲线带崩"


# ══════════════════════════════════════════════════════════════════
# 四、现金红线
# ══════════════════════════════════════════════════════════════════


def test_cash_ceiling_hint_flags_overdraft_near_line_and_quiet():
    """超支 / 贴线 / 正常 三档；没有报告 → `None`。"""
    cfg = load_config()

    def state_with(cash: float, total: float, construction: float = 0.0):
        st = make_state(cfg, "p1", mature("0,0", PlotType.RESOURCE, 1), cash=cash)
        st.report = {"p1": {
            "total_deduction": total,
            "construction_cash": construction,
        }}
        return st

    # ① 超支：扣款 500 > 现金 100
    hint = ST.cash_ceiling_hint(state_with(100.0, 500.0), "p1", cfg)
    assert hint, "扣款大于现金必须提醒"
    assert "500" in hint and "100" in hint and ">" in hint, hint
    assert "最多再加 0 现金的工程" in hint, f"已超支时工程余量必须是 0：{hint}"

    # ② 贴线：95 / 100 = 95% ≥ 90% → 仍是提醒（任务书只写「超过」，这条是预警）
    hint2 = ST.cash_ceiling_hint(state_with(100.0, 95.0, 10.0), "p1", cfg)
    assert hint2 and "95" in hint2 and "95.0%" in hint2, hint2
    assert "最多再加 15 现金的工程" in hint2, f"100 − (95 − 10) = 15：{hint2}"

    # ③ 正常：50 / 100 = 50% → 不说话
    assert ST.cash_ceiling_hint(state_with(100.0, 50.0), "p1", cfg) is None

    # ④ 没有上季报告 / 扣款为 0 → 不说话
    st4 = make_state(cfg, "p1", mature("0,0", PlotType.RESOURCE, 1), cash=100.0)
    assert ST.cash_ceiling_hint(st4, "p1", cfg) is None
    st4.report = {"p1": {"total_deduction": 0.0, "construction_cash": 0.0}}
    assert ST.cash_ceiling_hint(st4, "p1", cfg) is None

    # ⑤ 也认 `QuarterReport` 对象（有些调用方直接拿 preview 的报告来问）
    st5 = make_state(cfg, "p1", mature("0,0", PlotType.RESOURCE, 1), cash=100.0)
    st5.report = SimpleNamespace(
        player_id="p1", total_deduction=500.0, construction_cash=0.0,
    )
    assert ST.cash_ceiling_hint(st5, "p1", cfg), "对象形状的报告也必须能读"


# ══════════════════════════════════════════════════════════════════
# 五、合成器：≤4 行、顺序固定、只读
# ══════════════════════════════════════════════════════════════════


def test_strategy_lines_composes_at_most_four_in_fixed_order():
    """服务 → 升级 → 市场 → 现金，最多 4 行；`limit` 生效。"""
    cfg = load_config()
    st = _industry_row(cfg)
    st.players["p1"].cash = 100.0
    st.report = {"p1": {"total_deduction": 99.0, "construction_cash": 0.0}}

    lines = ST.strategy_lines(st, "p1", cfg)
    assert len(lines) == 4, f"四件事都该有话说，实得 {len(lines)}：{lines}"
    assert lines[0].startswith("建议在 "), lines[0]
    assert "升级 " in lines[1], lines[1]
    assert lines[2].startswith("市场余量："), lines[2]
    assert "总扣款" in lines[3], lines[3]
    assert all("\n" not in line for line in lines), "一行一句，不能自带换行"

    assert len(ST.strategy_lines(st, "p1", cfg, limit=2)) == 2
    assert ST.strategy_lines(st, "p1", cfg, limit=0) == []


def test_strategy_functions_are_readonly():
    """四个函数 + 合成器都不许改 state（AI 无 commit 的前置）。"""
    cfg = load_config()
    st = _industry_row(cfg)
    st.report = {"p1": {"total_deduction": 500.0, "construction_cash": 0.0}}
    before = st.to_dict()

    ST.service_hint(st, "p1", cfg)
    ST.upgrade_economics(st, "p1", cfg)
    ST.market_headroom(st, cfg)
    ST.cash_ceiling_hint(st, "p1", cfg)
    ST.strategy_lines(st, "p1", cfg)

    assert st.to_dict() == before, "ai/strategy.py 改了状态"


# ══════════════════════════════════════════════════════════════════
# 六、命令行 runner（不用 pytest）
# ══════════════════════════════════════════════════════════════════


def _print_samples() -> None:
    """打两段真实输出（人眼核对 + 汇报用）。"""
    cfg = load_config()
    samples = [
        ("三块工业围着 5,5", _industry_row(cfg)),
        ("一块资源地（邻居不足）",
         make_state(cfg, "p1", mature("0,0", PlotType.RESOURCE, 1))),
    ]
    for title, st in samples:
        print("\n" + "-" * 68)
        print(f"【{title}】")
        print("-" * 68)
        for line in ST.strategy_lines(st, "p1", cfg):
            print(" -", line)
        if not ST.strategy_lines(st, "p1", cfg):
            print(" -（无内容 → [策略提示] 暂无）")


def _run_all() -> int:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    mod = sys.modules[__name__]
    names = sorted(n for n in dir(mod) if n.startswith("test_"))
    ok = fail = 0
    print("=" * 68)
    print("AI 策略提示层（ai/strategy.py）")
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
