"""M1 对账 —— 《规则基线》§8.1 的最小账本。

跑法（在 region-sim/ 下）：

    python -m pytest tests/test_m1.py -v
    # 或不用 pytest，直接跑这个文件看对账表：
    python tests/test_m1.py

═══════════════════════════════════════════════════════════════════
★ 2026-10-16 重算记录
═══════════════════════════════════════════════════════════════════

原 §8.1 的 M1 表写「乙区原料费 = 72、生产总支出 = 157、增加值 = 93」，
与**同文件 §8.2 的「G 季度成本 = 215」自相矛盾**。

按**当前配方表**（§2.3）与参考价算：

    工业 1 级 G 的投入 = C×2 + D×1 + F×3
    参考价 C=16 / D=26 / F=24
    → 2×16 + 1×26 + 3×24 = 32 + 26 + 72 = **130**

而 §8.2 自己写的是「G 季度成本 = **215**」：

    130（原料）+ 45（工资）+ 40（经营费）= 215  ✅ 与 130 一致

**所以 §8.1 与 §8.2 互相矛盾，而 §8.2 与配方表一致。**
旧表的 72 只算了 F×3，漏了 C×2 与 D×1。

《规则基线》§11.6 数据纪律：

    历史测试数字只属于其对应规则版本，应根据最终规则**重新生成测试预期**，
    **不能反向修改规则迁就旧数字**。

所以 §8.1 已按配方表**重新生成**（2026-10-16），本文件跟随：

    甲：原料 0    总支出 66    增加值 66
    乙：原料 130  总支出 215   增加值 110
    增加值合计 = 176

**结算价与期末现金**依赖弹性 `Es`/`Ed`（config 里仍为 `null`），
故以公式表示，弹性到位后可直接代入：

    甲：期末现金 = 1000 − 66  + 4·P_F + 3.60
    乙：期末现金 = 1000 − 215 + 5·P_G + 4.50
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.loader import Config, load_config
from engine.models import (
    GameState,
    Plan,
    Plot,
    PlotPlan,
    PlotStatus,
    PlotType,
    WorldPhase,
)
from engine import production as P

TOL = 1e-6


# ══════════════════════════════════════════════════════════════════
# 测试夹具：M1 的两个区域
# ══════════════════════════════════════════════════════════════════


def make_state(cfg: Config, player_id: str, plot: Plot, cash: float = 1000.0) -> GameState:
    """造一个单人局：一块成熟地块、指定现金、开局状态。"""
    from engine.models import PlayerState

    world = cfg.new_world()
    world.phase = WorldPhase.RUNNING
    player = PlayerState(player_id=player_id, cash=cash, plots={plot.plot_id: plot})
    return GameState(world=world, players={player_id: player})


def mature(plot_id: str, ptype: PlotType, level: int = 1, product=None) -> Plot:
    """一块**成熟**地块：已过磨合（running_quarters 足够大 → E_磨合 = 1.0）。"""
    return Plot(
        plot_id=plot_id,
        plot_type=ptype,
        level=level,
        status=PlotStatus.RUNNING,
        running_quarters=99,        # 磨合已满 → E_磨合 = 1.0
        product=product,
    )


def plain_plan(player_id: str, plot_id: str, u: float = 1.0, **kw) -> Plan:
    return Plan(
        player_id=player_id,
        plot_plans={plot_id: PlotPlan(plot_id=plot_id, active=u > 0, u=u)},
        **kw,
    )


# ══════════════════════════════════════════════════════════════════
# 一、甲区：资源地块（F）—— 文档锚点，全部可断言
# ══════════════════════════════════════════════════════════════════


def test_jia_production_matches_anchor():
    """甲区生产三量：F×4、工资 36、经营费 30、总支出 66。"""
    cfg = load_config()
    plot = mature("0,0", PlotType.RESOURCE, 1)
    rec = cfg.recipe(PlotType.RESOURCE, 1)
    wage = cfg.wage_for(PlotType.RESOURCE, 1)

    o = P.run_plot(plot, rec, u=1.0, efficiency=1.0, cfg=cfg, wage_rate=wage)

    assert o.output_goods == "F"
    assert abs(o.output_qty - 4.0) < TOL, f"产量应为 4，实得 {o.output_qty}"
    assert o.inputs == {}, f"资源地块无商品投入，实得 {o.inputs}"
    assert abs(o.labor - 3.0) < TOL, f"劳动应为 3（d=3×u=1），实得 {o.labor}"
    assert abs(o.wage - 36.0) < TOL, f"工资应为 36（3×12），实得 {o.wage}"
    assert abs(o.operating_cost - 30.0) < TOL, f"经营费应为 30，实得 {o.operating_cost}"
    assert abs(o.wage + o.operating_cost - 66.0) < TOL, "甲区总支出应为 66"


# ══════════════════════════════════════════════════════════════════
# 二、乙区：工业 G —— 工资 45、经营费 40 可断言；原料费有分歧
# ══════════════════════════════════════════════════════════════════


def test_yi_wage_and_operating_match_anchor():
    """乙区：工资 45、经营费 40 —— 这两项与文档一致。"""
    cfg = load_config()
    plot = mature("0,1", PlotType.INDUSTRY, 1, product="G")
    rec = cfg.recipe(PlotType.INDUSTRY, 1, "G")
    wage = cfg.wage_for(PlotType.INDUSTRY, 1, "G")

    o = P.run_plot(plot, rec, u=1.0, efficiency=1.0, cfg=cfg, wage_rate=wage)

    assert o.output_goods == "G"
    assert abs(o.output_qty - 5.0) < TOL, f"产量应为 5，实得 {o.output_qty}"
    assert abs(o.labor - 3.0) < TOL
    assert abs(o.wage - 45.0) < TOL, f"工资应为 45（3×15），实得 {o.wage}"
    assert abs(o.operating_cost - 40.0) < TOL, f"经营费应为 40，实得 {o.operating_cost}"


def test_yi_material_cost_is_130():
    """★ 乙区原料费 = **130**（配方 §2.3 + 参考价），不是旧表的 72。

    工业 1 级 G 的投入 = C×2 + D×1 + F×3
    参考价 C=16 / D=26 / F=24
    → 2×16 + 1×26 + 3×24 = 32 + 26 + 72 = **130**

    这与 **§8.2 静态成本四点**的「G 季度成本 = 215」自洽：
        130（原料）+ 45（工资）+ 40（经营费）= 215  ✅

    旧表的 72 只算了 F×3，漏了 C、D。按 §11.6 数据纪律
    「历史测试数字只属于其对应规则版本，应**重新生成**测试预期，
    **不能反向修改规则迁就旧数字**」，此处以配方表为准。
    """
    cfg = load_config()
    plot = mature("0,1", PlotType.INDUSTRY, 1, product="G")
    rec = cfg.recipe(PlotType.INDUSTRY, 1, "G")
    wage = cfg.wage_for(PlotType.INDUSTRY, 1, "G")
    o = P.run_plot(plot, rec, u=1.0, efficiency=1.0, cfg=cfg, wage_rate=wage)

    P0 = {g: c.P0 for g, c in cfg.market.items()}
    material = o.material_cost(P0)

    assert o.inputs == {"C": 2.0, "D": 1.0, "F": 3.0}
    assert abs(material - 130.0) < TOL, f"按配方应为 130，实得 {material}"

    # 与 §8.2 静态成本四点自洽（这是**独立**的文档锚点）
    total = material + o.wage + o.operating_cost
    assert abs(total - 215.0) < TOL, f"G 季度成本应为 215（§8.2），实得 {total}"

    # 旧表的 72/157 已被证伪（漏算 C、D），不应再复现
    assert abs(material - 72.0) > 1.0, "72 是漏算 C、D 的旧值"
    assert abs(total - 157.0) > 1.0, "157 是漏算 C、D 的旧值"


def test_value_added_matches_regenerated_anchor():
    """增加值（GDP）：甲 66 / 乙 110 / **合计 176**（§8.1 重算值）。

        增加值 = 有效产出价值（**固定参考价**） − 中间投入（原料 + 经营费 c）

    ⚠️ 工资**不**作中间投入再扣（§7.5）。
    """
    cfg = load_config()
    P0 = {g: c.P0 for g, c in cfg.market.items()}

    cases = [
        ("甲", PlotType.RESOURCE, 1, None, 66.0),
        ("乙", PlotType.INDUSTRY, 1, "G", 110.0),
    ]
    total_va = 0.0
    for name, ptype, level, product, expected in cases:
        plot = mature("0,0", ptype, level, product=product)
        rec = cfg.recipe(ptype, level, product)
        wage_rate = cfg.wage_for(ptype, level, product)
        o = P.run_plot(plot, rec, 1.0, 1.0, cfg, wage_rate)

        out_value = o.output_qty * P0[o.output_goods]
        material = o.material_cost(P0)
        va = out_value - material - o.operating_cost
        assert abs(va - expected) < TOL, (
            f"{name} 增加值应为 {expected}，实得 {va}"
            f"（产出价值 {out_value} − 原料 {material} − 经营费 {o.operating_cost}）"
        )
        total_va += va

    assert abs(total_va - 176.0) < TOL, f"增加值合计应为 176，实得 {total_va}"


def test_static_cost_four_points():
    """§8.2 静态成本四点：G 215 / H-A 376 / I 545 / H-A+ 821。

    这条**不依赖弹性**，可以直接验：固定效率 1、参考价。
    """
    cfg = load_config()
    P0 = {g: c.P0 for g, c in cfg.market.items()}

    cases = [
        ("G",     PlotType.INDUSTRY, 1, "G",     215.0),
        ("H-A",   PlotType.INDUSTRY, 2, "H-A",   376.0),
        ("I",     PlotType.INDUSTRY, 3, "I",     545.0),
        ("H-A+",  PlotType.INDUSTRY, 3, "H-A+",  821.0),
    ]
    for name, ptype, level, product, expected in cases:
        plot = mature("0,0", ptype, level, product=product)
        rec = cfg.recipe(ptype, level, product)
        wage_rate = cfg.wage_for(ptype, level, product)
        o = P.run_plot(plot, rec, u=1.0, efficiency=1.0, cfg=cfg, wage_rate=wage_rate)
        total = o.material_cost(P0) + o.wage + o.operating_cost
        assert abs(total - expected) < TOL, (
            f"{name} 季度成本应为 {expected}，实得 {total}"
            f"（原料 {o.material_cost(P0)} + 工资 {o.wage} + 经营费 {o.operating_cost}）"
        )


# ══════════════════════════════════════════════════════════════════
# 三、顺序铁律（不依赖弹性，可直接验）
# ══════════════════════════════════════════════════════════════════


def test_labor_does_not_scale_with_efficiency():
    """★ 铁律 4：劳动 = d×u，**不乘 E**（§3.4）。

    效率 1.0 / 1.5 / 2.0 下，产量与投入按比例变，**工资不变**。
    """
    cfg = load_config()
    plot = mature("0,1", PlotType.INDUSTRY, 1, product="G")
    rec = cfg.recipe(PlotType.INDUSTRY, 1, "G")
    wage_rate = cfg.wage_for(PlotType.INDUSTRY, 1, "G")

    base = P.run_plot(plot, rec, 1.0, 1.0, cfg, wage_rate)
    for eff in (0.5, 1.5, 2.0):
        o = P.run_plot(plot, rec, 1.0, eff, cfg, wage_rate)
        assert abs(o.output_qty - base.output_qty * eff) < TOL, "产量应随 E 放大"
        assert abs(o.inputs["F"] - base.inputs["F"] * eff) < TOL, "投入应随 E 放大"
        assert abs(o.labor - base.labor) < TOL, f"★ 劳动不该随 E 变（E={eff}）"
        assert abs(o.wage - base.wage) < TOL, f"★ 工资不该随 E 变（E={eff}）"


def test_u_zero_costs_nothing():
    """u = 0 → 产出 0、投入 0、劳动 0、**经营费 0**（§3.4）。

    经营费为 0 是"关停地块能止血"这条玩法的基础。
    """
    cfg = load_config()
    plot = mature("0,1", PlotType.INDUSTRY, 1, product="G")
    rec = cfg.recipe(PlotType.INDUSTRY, 1, "G")
    o = P.run_plot(plot, rec, 0.0, 1.0, cfg, 15.0)

    assert o.output_qty == 0.0
    assert o.inputs == {}
    assert o.labor == 0.0
    assert o.wage == 0.0
    assert o.operating_cost == 0.0, "u=0 时经营费必须为 0，不是 0.7c"


def test_operating_cost_split():
    """经营费 = 0.7c + 0.3c·u·E（§3.4）。"""
    cfg = load_config()
    plot = mature("0,1", PlotType.INDUSTRY, 1, product="G")
    rec = cfg.recipe(PlotType.INDUSTRY, 1, "G")   # c = 40

    o = P.run_plot(plot, rec, 0.5, 2.0, cfg, 15.0)
    # 0.7×40 + 0.3×40×0.5×2.0 = 28 + 12 = 40
    assert abs(o.operating_cost - 40.0) < TOL, f"实得 {o.operating_cost}"


# ══════════════════════════════════════════════════════════════════
# 四、弹性未填时必须报错（数据纪律，§11.6）
# ══════════════════════════════════════════════════════════════════


def test_missing_elasticity_raises_not_defaults():
    """★ 弹性未填 → **明确报错**，不许偷偷用默认值。

    §11.6：不得为填满配置而编造数据。
    """
    cfg = load_config()
    curve = cfg.market["F"]
    assert curve.Es is None and curve.Ed is None, \
        "config 里 F 的弹性应为 null（待规则同学填）"
    try:
        curve.slopes()
    except ValueError as e:
        assert "弹性" in str(e) or "Es" in str(e)
    else:
        raise AssertionError("弹性未填时必须报错，不能返回默认斜率")


# ══════════════════════════════════════════════════════════════════
# 五、命令行对账（不用 pytest 也能看）
# ══════════════════════════════════════════════════════════════════


def _print_reconciliation() -> None:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    cfg = load_config()
    P0 = {g: c.P0 for g, c in cfg.market.items()}

    print("=" * 70)
    print("M1 对账（按当前《规则基线》§8.1 重算值 + config.json 实算）")
    print("=" * 70)

    cases = [
        ("甲 (F)", PlotType.RESOURCE, 1, None, 0.0, 36.0, 30.0, 66.0, 66.0),
        ("乙 (G)", PlotType.INDUSTRY, 1, "G", 130.0, 45.0, 40.0, 215.0, 110.0),
    ]
    for name, ptype, level, product, d_mat, d_wage, d_op, d_total, d_va in cases:
        plot = mature("0,0", ptype, level, product=product)
        rec = cfg.recipe(ptype, level, product)
        w = cfg.wage_for(ptype, level, product)
        o = P.run_plot(plot, rec, 1.0, 1.0, cfg, w)
        mat = o.material_cost(P0)
        tot = mat + o.wage + o.operating_cost
        out_value = o.output_qty * P0[o.output_goods]
        va = out_value - mat - o.operating_cost

        def mark(actual, doc):
            return "" if abs(actual - doc) < TOL else "   <<< 不一致"

        print(f"\n【{name}】u=1  E=1")
        print(f"  产出        {o.output_goods} x {o.output_qty}")
        print(f"  商品投入    {o.inputs or '无'}")
        print(f"  原料费      {mat:<8} §8.1 写 {d_mat}{mark(mat, d_mat)}")
        print(f"  工资        {o.wage:<8} §8.1 写 {d_wage}{mark(o.wage, d_wage)}")
        print(f"     └ 劳动 {o.labor} × 单价 {o.wage_rate}")
        print(f"  经营费      {o.operating_cost:<8} §8.1 写 {d_op}{mark(o.operating_cost, d_op)}")
        print(f"  生产总支出  {tot:<8} §8.1 写 {d_total}{mark(tot, d_total)}")
        print(f"  增加值      {va:<8} §8.1 写 {d_va}{mark(va, d_va)}")
        print(f"     └ = 有效产出价值 {out_value} − 原料 {mat} − 经营费 {o.operating_cost}")

    print("\n" + "-" * 70)
    print("2026-10-16 重算说明：旧表的乙区原料费 72 只算了 F×3，")
    print("  漏了 C×2 + D×1。配方 §2.3 = C×2+D×1+F×3 = 2×16+1×26+3×24 = 130，")
    print("  与 §8.2 的「G = 215」= 130+45+40 自洽。")
    print("  按 §11.6，应重新生成 M1 预期，不迁就旧数字。")
    print("-" * 70)
    print("\n价格一步：Es/Ed 仍未填，无法求价（这是设计如此，不是 bug）")
    print("  弹性到位后即可代入：")
    print("    甲：期末现金 = 1000 − 66  + 4·P_F + 3.60")
    print("    乙：期末现金 = 1000 − 215 + 5·P_G + 4.50")
    for g in ("F", "G"):
        c = cfg.market[g]
        print(f"  {g}: P0={c.P0} Q0={c.Q0} Es={c.Es} Ed={c.Ed}")


if __name__ == "__main__":
    _print_reconciliation()
