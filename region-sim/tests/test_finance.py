"""税收与 GDP 对账 —— §7.1 工资税、§7.5 增加值。

跑法（在 region-sim/ 下）：

    python tests/test_finance.py

这两块原先散在 `pipeline.py` 里（第 8 步的三行税 + `_value_added()`），
2026-10-16 第 4 步抽到 `engine/finance.py`，为的是**能单独对账**。

★ 本文件最重要的一条是 `test_wages_are_not_intermediate_input`：
  **工资不能当中间投入扣掉** —— 写错这条，甲区增加值会从 66 变成 30。
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import finance as F
from engine import pipeline
from engine.loader import load_config
from engine.models import PlotType
from tests.test_m1 import make_state, mature, plain_plan

TOL = 1e-9


# ══════════════════════════════════════════════════════════════════
# 夹具：跑完一个季度的报告
# ══════════════════════════════════════════════════════════════════


def run_quarter(ptype: PlotType, level: int = 1, product=None, **plan_kw):
    """跑一个完整季度，返回 `(state, report)`。"""
    cfg = load_config()
    st = make_state(cfg, "p1", mature("0,0", ptype, level, product=product))
    plan = plain_plan("p1", "0,0", u=1.0, **plan_kw)
    pipeline.advance(st, {"p1": plan}, cfg)
    return st, st.raw_reports["p1"]


# ══════════════════════════════════════════════════════════════════
# 一、§7.1 工资税
# ══════════════════════════════════════════════════════════════════


def test_wage_tax_rate_is_10pct():
    """`工资税 = W × wage_tax_rate`（§7.1，测试参数 10%）。"""
    cfg = load_config()
    assert abs(cfg.wage_tax_rate - 0.10) < TOL, "工资税率应为 10%"
    assert abs(F.wage_tax(100.0, cfg) - 10.0) < TOL
    assert abs(F.wage_tax(36.0, cfg) - 3.6) < TOL      # 甲区工资 36 → 税 3.6


def test_wage_tax_net_is_90pct():
    """税后净支出 = `W × 90%`（派生量，供显示用）。"""
    cfg = load_config()
    assert abs(F.wage_tax_net(100.0, cfg) - 90.0) < TOL
    assert abs(F.wage_tax_net(36.0, cfg) - 32.4) < TOL


def test_wage_tax_uses_config_not_literal():
    """★ 税率**从 config 读**，不是写死的 0.10。

    改配置就该改行为 —— 否则又是一个"配置形同虚设"。
    """
    cfg = load_config()
    original = cfg.wage_tax_rate
    try:
        cfg.wage_tax_rate = 0.25
        assert abs(F.wage_tax(100.0, cfg) - 25.0) < TOL, "税率应随 config 变"
    finally:
        cfg.wage_tax_rate = original


def test_pipeline_records_tax_as_refund():
    """★ 账面口径：扣款扣**税前全额**，税记**回流收入**（§7.1）。

    ⚠️ 关键是**两笔都记** —— 不是直接扣 90%。
       实测甲区：工资 36 全扣，税 3.6 回流，净支出 32.4。
    """
    st, r = run_quarter(PlotType.RESOURCE, 1)

    assert abs(r.wages - 36.0) < TOL, f"工资（税前总额）应为 36，实得 {r.wages}"
    assert abs(r.wage_tax_refund - 3.6) < TOL, (
        f"工资税回流应为 3.6，实得 {r.wage_tax_refund}"
    )
    # 期末现金 = 1000 − 66 + 94.72 + 3.60 = 1032.32（M1 锚点，运营口径）
    # ★ 2026-10-18 S1：补贴改为季初发放、不进结算 → 账面 = 运营锚点本身。
    assert abs(r.subsidy) < 1e-9, f"S1 起结算报告 subsidy 应为 0，实得 {r.subsidy}"
    assert abs(st.players["p1"].cash - 1032.32) < 1e-9, (
        f"期末现金应为 1032.32（S1 起补贴不进账），实得 {st.players['p1'].cash}"
    )


def test_tax_is_not_baked_into_deduction():
    """★ 扣款总额里的工资是**税前全额**（36），不是税后的 32.4。

    如果哪天有人"简化"成 `cash -= wages * 0.9`，这条会立刻报警。
    """
    st, r = run_quarter(PlotType.RESOURCE, 1)

    # total_deduction = 物资款 0 + 工资 36 + 经营费 30 = 66
    assert abs(r.total_deduction - 66.0) < TOL, (
        f"扣款总额应为 66（工资按税前 36 算），实得 {r.total_deduction}"
    )
    assert abs(r.materials_cost - 0.0) < TOL
    assert abs(r.operating - 30.0) < TOL


# ══════════════════════════════════════════════════════════════════
# 二、★ §7.5 增加值 —— 工资不是中间投入
# ══════════════════════════════════════════════════════════════════


def test_wages_are_not_intermediate_input():
    """★★ **工资不能当中间投入扣掉**（§7.5）—— 本项目最容易写反的一条。

    甲区：产出价值 96（F×4 @ P0=24）− 原料 0 − 经营费 30 − 粮食 0 = **66**。

    ⚠️ 若把工资 36 也扣掉 → 30 ≠ 66，**M1 锚点立刻对不上**。
       GDP 的"增加值"法里，工资属于**增加值**（劳动者报酬），
       不是中间消耗 —— 它不是"买来的东西"。
    """
    st, r = run_quarter(PlotType.RESOURCE, 1)

    assert abs(r.value_added - 66.0) < TOL, (
        f"★ 甲区增加值应为 66，实得 {r.value_added}\n"
        f"  ⚠️ 若实得 30，说明工资被当成中间投入扣掉了（36 = 66 − 30）。"
    )

    # 直接断言"不是把工资扣掉的那个数"
    wrong = 66.0 - 36.0
    assert abs(r.value_added - wrong) > 1.0, "增加值不该等于「产出 − 原料 − 经营 − 工资」"


def test_value_added_formula_is_output_minus_materials():
    """`增加值 = 有效产出价值 − 中间投入`（§7.5 原文），逐项核对。"""
    cfg = load_config()
    st, r = run_quarter(PlotType.RESOURCE, 1)

    out_v = F.output_value(r.production, cfg)
    mat_v = F.material_value(r, cfg)

    assert abs(out_v - 96.0) < TOL, f"产出价值（F×4 @24）应为 96，实得 {out_v}"
    assert abs(mat_v - 30.0) < TOL, f"中间投入（经营费 30）应为 30，实得 {mat_v}"
    assert abs(F.value_added(r, cfg) - (out_v - mat_v)) < TOL
    assert abs(F.value_added(r, cfg) - r.value_added) < TOL, (
        "finance.value_added 必须等于流水线记进报告的值"
    )


def test_value_added_uses_p0_not_settlement_price():
    """★ GDP 用**固定参考价 `P0`**，不随季度成交价变（§7.5）。

    甲区结算价 23.68 ≠ P0 24。若有人误用成交价，
    产出价值会变成 `4 × 23.68 = 94.72`（那是**销售收入**，不是 GDP）。
    """
    cfg = load_config()
    st, r = run_quarter(PlotType.RESOURCE, 1)

    assert abs(r.sales_revenue - 94.72) < 1e-9, "销售收入按成交价 23.68 算"
    assert abs(F.output_value(r.production, cfg) - 96.0) < TOL, (
        "★ GDP 的产出价值必须按 P0=24 算，不是成交价 23.68"
    )
    assert abs(F.output_value(r.production, cfg) - r.sales_revenue) > 1.0, (
        "统计口径与现金口径是两个数，不该相等"
    )


def test_yi_value_added_matches_anchor():
    """乙区增加值 = **110**（M1 锚点）。

    产出价值 280（G×5 @56）− 原料 130（C2/D1/F3）− 经营费 40 − 粮食 0 = 110。

    ⚠️ 这一条同时验"**原料要扣**"：漏扣原料会得到 240。
    """
    cfg = load_config()
    st, r = run_quarter(PlotType.INDUSTRY, 1, product="G")

    assert abs(r.value_added - 110.0) < TOL, (
        f"乙区增加值应为 110，实得 {r.value_added}\n"
        f"  ⚠️ 若实得 240，说明原料 130 没扣。"
    )
    assert abs(F.material_value(r, cfg) - 170.0) < TOL, (
        f"乙区中间投入应为 原料 130 + 经营费 40 = 170，实得 {F.material_value(r, cfg)}"
    )


def test_total_value_added_is_176():
    """★ M1 验收锚点：**增加值合计 = 176**（甲 66 + 乙 110）。"""
    cfg = load_config()
    total = 0.0
    for ptype, level, product in ((PlotType.RESOURCE, 1, None),
                                  (PlotType.INDUSTRY, 1, "G")):
        st, r = run_quarter(ptype, level, product=product)
        total += r.value_added

    assert abs(total - 176.0) < TOL, f"增加值合计应为 176，实得 {total}"


def test_service_contributes_no_output_value():
    """★ 服务地块不产出商品 → 对 GDP 的**产出侧**贡献为 0。

    它 0→0，价值体现在**别的地块的效率**上，不能在这里再算一遍
    （否则同一份价值收两次）。
    """
    cfg = load_config()
    st, r = run_quarter(PlotType.SERVICE, 1)

    assert abs(F.output_value(r.production, cfg) - 0.0) < TOL, (
        "服务不该有产出价值"
    )
    # 但它的经营费/工资照花 —— 增加值因此为负（这是对的：纯投入）
    assert r.value_added < 0, (
        f"服务只有投入没有产出，增加值应为负，实得 {r.value_added}"
    )


def test_value_added_includes_service_revenue():
    """★ 2026-10-18 服务/市场重做：GDP = 产出价值 + **服务收入** − 中间投入。

    一块二级服务 + 3 个运行中的非服务邻居：
        服务收入 = 3 × 9 = 27
        中间投入 = 经营费 16（服务 c=16，E=1.0 → 0.7×16 + 0.3×16 = 16）
        增加值   = 0 + 27 − 16 = **11**

    ⚠️ 服务收入是**服务业增加值**，必须进 GDP；但它不是商品产出，
       不能塞进 `output_value()`（那会混淆统计口径）。
    """
    from engine.models import Plan, Plot, PlotPlan, PlotStatus, PlotType

    cfg = load_config()

    def running(pid, ptype, level=1, product=None):
        return Plot(
            plot_id=pid, plot_type=ptype, level=level,
            status=PlotStatus.RUNNING, running_quarters=99, product=product,
        )

    svc = running("1,1", PlotType.SERVICE, 2)
    st = make_state(cfg, "p1", svc)
    st.players["p1"].plots = {
        "1,1": svc,
        "1,0": running("1,0", PlotType.RESOURCE, 1),
        "0,1": running("0,1", PlotType.AGRICULTURE, 1, product="A"),
        "2,1": running("2,1", PlotType.INDUSTRY, 1, product="G"),
    }
    plan = Plan(
        player_id="p1",
        plot_plans={"1,1": PlotPlan(plot_id="1,1", active=True, u=1.0)},
    )
    pipeline.advance(st, {"p1": plan}, cfg)
    r = st.raw_reports["p1"]

    assert abs(r.service_revenue - 27.0) < TOL, (
        f"服务收入应为 3×9=27，实得 {r.service_revenue}"
    )
    assert abs(F.output_value(r.production, cfg) - 0.0) < TOL, "服务不产商品"
    assert abs(F.material_value(r, cfg) - 16.0) < TOL, (
        f"中间投入应只有经营费 16，实得 {F.material_value(r, cfg)}"
    )
    assert abs(F.value_added(r, cfg) - 11.0) < TOL, (
        f"★ GDP 应含服务收入：0 + 27 − 16 = 11，实得 {F.value_added(r, cfg)}"
    )
    assert abs(r.value_added - 11.0) < TOL, "报告里的增加值必须与 finance 重算一致"

    # 旧报告（没有 service_revenue 字段）→ getattr 兜底 0，不炸
    class _OldReport:
        production = []
        bought_breakdown = {}
        operating = 0.0
        food = None

    assert abs(F.value_added(_OldReport(), cfg) - 0.0) < TOL, (
        "value_added 必须能用 getattr 兜底旧报告"
    )



    """经营费 `c` **算**中间投入（§7.5）。"""
    cfg = load_config()
    st, r = run_quarter(PlotType.RESOURCE, 1)
    assert abs(r.operating - 30.0) < TOL
    # 中间投入里含这 30
    assert abs(F.material_value(r, cfg) - 30.0) < TOL


def test_food_deduction_is_intermediate_input():
    """粮食扣减**也算**中间投入（§4.3.4 / §7.5）。"""
    cfg = load_config()
    st, r = run_quarter(PlotType.RESOURCE, 1, food={"A": 1.2, "B": 0.6})

    assert r.food is not None, "申报了口粮就该有粮食结算结果"
    food_value = sum(q * cfg.price_of(g) for g, q in r.food.deducted.items())
    assert food_value > 0, "本季应确实吃掉了粮"

    # 中间投入 = 经营费 30 + 粮食
    assert abs(F.material_value(r, cfg) - (30.0 + food_value)) < TOL, (
        "粮食扣减应计入中间投入"
    )


# ══════════════════════════════════════════════════════════════════
# 三、抽出来没改变行为（回归）
# ══════════════════════════════════════════════════════════════════


def test_pipeline_value_added_equals_finance():
    """★ 流水线记进报告的值，必须等于 `finance.value_added()`。

    第 4 步是**纯搬迁**（算式一字未改），这条钉住"搬完还是同一个数"。
    """
    cfg = load_config()
    for ptype, level, product in ((PlotType.RESOURCE, 1, None),
                                  (PlotType.INDUSTRY, 1, "G"),
                                  (PlotType.SERVICE, 1, None)):
        st, r = run_quarter(ptype, level, product=product)
        assert abs(r.value_added - F.value_added(r, cfg)) < TOL, (
            f"{ptype.value} 报告值 {r.value_added} ≠ finance 重算 {F.value_added(r, cfg)}"
        )


def test_pipeline_tax_equals_finance():
    """工资税同理：报告里的回流额必须等于 `finance.wage_tax()`。"""
    cfg = load_config()
    for ptype in (PlotType.RESOURCE, PlotType.INDUSTRY, PlotType.SERVICE):
        st, r = run_quarter(ptype, 1, product="G" if ptype is PlotType.INDUSTRY else None)
        assert abs(r.wage_tax_refund - F.wage_tax(r.wages, cfg)) < TOL


def test_legacy_value_added_shim_still_works():
    """旧的 `pipeline._value_added()` 薄壳仍可用（不打断外部调用）。"""
    cfg = load_config()
    st, r = run_quarter(PlotType.RESOURCE, 1)
    assert abs(pipeline._value_added(r, cfg) - r.value_added) < TOL


# ══════════════════════════════════════════════════════════════════
# 四、给 UI 的一整块
# ══════════════════════════════════════════════════════════════════


def test_finance_summary_shape():
    """`finance_summary()` 的字段齐全（前端零计算）。"""
    cfg = load_config()
    st, r = run_quarter(PlotType.RESOURCE, 1)
    s = F.finance_summary(r, cfg)

    for key in ("wages", "wage_tax", "wage_net", "operating", "materials_cost",
                "total_deduction", "sales_revenue",
                "output_value_at_p0", "material_value_at_p0", "value_added"):
        assert key in s, f"finance_summary 缺 {key}"

    # 自洽：净工资 = 工资 − 税
    assert abs(s["wage_net"] - (s["wages"] - s["wage_tax"])) < TOL
    # 甲区具体值
    assert abs(s["wages"] - 36.0) < TOL
    assert abs(s["wage_tax"] - 3.6) < TOL
    assert abs(s["wage_net"] - 32.4) < TOL
    assert abs(s["value_added"] - 66.0) < TOL
    assert abs(s["output_value_at_p0"] - 96.0) < TOL


def test_finance_summary_does_not_recompute_pipeline_values():
    """`finance_summary` 里的流水线字段是**原样搬运**，不是重算。"""
    cfg = load_config()
    st, r = run_quarter(PlotType.INDUSTRY, 1, product="G")
    s = F.finance_summary(r, cfg)

    assert abs(s["total_deduction"] - r.total_deduction) < TOL
    assert abs(s["sales_revenue"] - r.sales_revenue) < TOL
    assert abs(s["materials_cost"] - r.materials_cost) < TOL
    assert abs(s["operating"] - r.operating) < TOL
    assert abs(s["value_added"] - r.value_added) < TOL


# ══════════════════════════════════════════════════════════════════
# 命令行汇总
# ══════════════════════════════════════════════════════════════════


def _run_all() -> int:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    mod = sys.modules[__name__]
    names = sorted(n for n in dir(mod) if n.startswith("test_"))
    ok = fail = 0
    print("=" * 68)
    print("税收与 GDP 对账（§7.1 / §7.5）")
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
