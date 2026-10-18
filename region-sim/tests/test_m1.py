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
    """造一个单人局：一块成熟地块、指定现金、开局状态。

    ★ **把 `last_happiness` 设为 0.8**，让 `E_幸福 = 1.00`。

    M1 的题设是"**效率均为 1**"，但按真实规则
    `E = E_邻接 × E_幸福 × E_磨合 × (1+研究)`，
    开局 `H₀ = 0.6` → `E_幸福 = 0.90`，**E 永远到不了 1**。

    要让 `E_幸福 = 1.00`：`0.90 + (H−0.6)×0.5 = 1.00` → `H = 0.8`。
    所以这里把上季 H 设成 0.8 —— 这是**题设给定的测试条件**，不是规则。
    """
    from engine.models import PlayerState

    world = cfg.new_world()
    world.phase = WorldPhase.RUNNING
    player = PlayerState(player_id=player_id, cash=cash, plots={plot.plot_id: plot})
    player.last_happiness = 0.8        # → E_幸福 = 1.00（见 docstring）
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
    """一个最简计划：只有一块地开工，什么都不买。"""
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


def test_market_depth_high_tier_is_shallow():
    """★ 市场深度：高级商品的市场相对较浅（结构性事实，供调平衡参考）。

    临界量（价格打到地板所需的净供给）= `0.9 × (Es+|Ed|) × Q0`。

    ★ 2026-10-18 第七轮：用户要求"市场放大、升级要有意义"，Q0 上调
    （G 200→300、H-* 30→150、H-*+ 10→60、I 15→60、C/D/F 上调）。
    本测试同步新 Q0 的临界量。

    ⚠️ 这条**不是断言"设计对"**，而是把数值后果**固定下来**：
    `I` 75.6、`H-A+` 108 单位才触底（分别约 13 / 19 块满负荷三级地块一季）；
    而农产品 A 要 1800 单位。这个梯度仍来自 `Q0` 的设定（60 vs 2000）。
    """
    cfg = load_config()
    cases = {
        "A": 1800.0, "F": 270.0, "G": 486.0,
        "H-A": 364.5, "I": 75.6, "H-A+": 108.0,
    }
    for g, expected in cases.items():
        c = cfg.market[g]
        s, d = c.slopes()
        crit = (c.P0 - c.P_min) * (s + d)
        assert abs(crit - expected) < 0.1, (
            f"{g} 的临界供给量应为 {expected}，实得 {crit}"
        )

    # 高级商品确实比农产品浅得多（H-A+ 仍不到 A 的 1/10）
    assert cases["H-A+"] < cases["A"] / 10, "H+ 市场应远浅于农产品市场"


def test_price_floor_still_sells_everything():
    """触底时**按 `P_min` 全量收购**，成交量不截断（§4.1）。

    ✅ 2026-10-16 深夜用户裁定："价格碰地板判定为全按地板价卖出就行了。
       就当是全国统一收购。" —— 推翻了此前的"不保证全量收购"。

    触底现在退化为**纯粹的价格惩罚**：货一定卖得掉，只是卖得便宜。
    """
    from engine import market as M

    cfg = load_config()
    curve = cfg.market["H-A+"]
    r = M.solve_price(curve, bought=0.0, sold=300.0)

    assert r.at_floor, "卖 300 远超临界量 18，应当触底"
    assert abs(r.price - curve.P_min) < TOL, "触底时应返回 P_min"
    assert r.sellable == 300.0, "★ 触底也必须全量成交（全国统一收购）"

    # 对照：不触底时同样是全量
    r2 = M.solve_price(curve, bought=0.0, sold=5.0)
    assert not r2.at_floor and r2.sellable == 5.0


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


def test_elasticity_loaded_from_teammate_doc():
    """★ 弹性已由规则同学提供（源文件 docs/05-原始资料/队友弹性参数.docx）。

    15 个商品全部有值。`Ed` 是**负值**（经济学惯例），
    程序用 `d = |Ed| × Q0 / P0` 取绝对值，与基线 §4.1 一致。
    """
    cfg = load_config()

    # 全 15 个商品都要有弹性
    missing = [g for g, c in cfg.market.items() if c.Es is None or c.Ed is None]
    assert not missing, f"这些商品仍缺弹性：{missing}"

    # 逐档核对队友给的值
    expected = {
        "A": (0.5, -0.5), "B": (0.5, -0.5), "C": (0.5, -0.5), "D": (0.5, -0.5),
        "F": (0.5, -0.5),
        "G": (1.0, -0.8),
        "H-A": (1.5, -1.2), "H-F": (1.5, -1.2), "H-G": (1.5, -1.2), "H-J": (1.5, -1.2),
        "I": (0.8, -0.6),
        "H-A+": (1.0, -1.0), "H-F+": (1.0, -1.0),
        "H-G+": (1.0, -1.0), "H-J+": (1.0, -1.0),
    }
    for g, (es, ed) in expected.items():
        c = cfg.market[g]
        assert abs(c.Es - es) < TOL, f"{g} 的 Es 应为 {es}，实得 {c.Es}"
        assert abs(c.Ed - ed) < TOL, f"{g} 的 Ed 应为 {ed}，实得 {c.Ed}"

    # Ed 为负时，斜率 d 必须取成正的（否则需求曲线方向就反了）
    s, d = cfg.market["F"].slopes()
    assert s > 0 and d > 0, "斜率必须为正：s = Es·Q0/P0，d = |Ed|·Q0/P0"


def test_missing_elasticity_still_raises():
    """★ 弹性**缺失时仍然必须报错**，不许偷偷用默认值。

    现在 config 里已填满，所以这里**手工造一个缺失的曲线**来验纪律。
    见 §11.6：不得为填满配置而编造数据。
    """
    from engine.models import MarketCurve

    curve = MarketCurve(goods="ZZ", P0=10.0, Q0=100.0, Es=None, Ed=None)
    try:
        curve.slopes()
    except ValueError as e:
        assert "弹性" in str(e) or "Es" in str(e)
    else:
        raise AssertionError("弹性未填时必须报错，不能返回默认斜率")


# ══════════════════════════════════════════════════════════════════
# 六、★ M1 完整账（弹性到位后，结算价与期末现金都能真算了）
# ══════════════════════════════════════════════════════════════════


def test_m1_full_account():
    """★ M1 完整账 —— 这一条是"账本跑通"的最终验收。

    题设：各 1000 现金、效率 1、工资税 10%、上期价 = 参考价。
    弹性到位后，`P_t`、销售收入、期末现金**全部可算**。
    """
    from engine import pipeline

    cfg = load_config()
    cases = [
        # 名称, 类型, 等级, 产出, 结算价, 销售收入, 期末现金, 增加值
        ("甲", PlotType.RESOURCE, 1, None, 23.68, 94.72, 1032.32, 66.0),
        ("乙", PlotType.INDUSTRY, 1, "G", 55.48148148148148,
         277.4074074074074, 1066.9074074074074, 110.0),
    ]

    for name, ptype, level, product, price, revenue, cash, va in cases:
        st = make_state(cfg, "p1", mature("0,0", ptype, level, product=product))
        pipeline.advance(st, {"p1": plain_plan("p1", "0,0", u=1.0)}, cfg)
        r = st.raw_reports["p1"]
        g = r.production[0].output_goods

        assert abs(r.production[0].efficiency - 1.0) < TOL, f"{name} 题设要求 E=1"
        assert abs(r.prices[g]["price"] - price) < 1e-9, (
            f"{name} 结算价应为 {price}，实得 {r.prices[g]['price']}"
        )
        assert abs(r.sales_revenue - revenue) < 1e-9, (
            f"{name} 销售收入应为 {revenue}，实得 {r.sales_revenue}"
        )
        # ★ 2026-10-18 S1：补贴改为**季初发放、不进结算** —— 引擎路径……
        #   （pipeline.advance、不走 server/session 的 GameSession）里 subsidy 恒 0，
        #   于是账面现金就是运营锚点本身，一个数字都不用改。
        assert abs(r.subsidy) < 1e-9, (
            f"{name} 报告里的补贴应为 0（S1 起结算不再发钱），实得 {r.subsidy}"
        )
        assert abs(st.players["p1"].cash - cash) < 1e-9, (
            f"{name} 期末现金应为运营锚点 {cash}（S1 起补贴不进账），"
            f"实得 {st.players['p1'].cash}"
        )
        assert abs(r.value_added - va) < TOL, (
            f"{name} 增加值应为 {va}，实得 {r.value_added}"
        )


def test_m1_subsidy_is_transfer_not_output():
    """★ 补贴（S1: Q1–Q7 各 1200、Q8 起 300）是**转移支付**，且**季初发放**。

    ★ 2026-10-18 S1 用户拍板：补贴**不进入结算**。本路径（pipeline.advance）
       里 `r.subsidy` 恒为 0 —— 真正的发钱已挪到 `server/session` 的季初
       （见 server/session.py::credit_opening_subsidy）。

    断言三件事：
      1. `r.subsidy` 恒为 0（结算不再给现金，报告里如实记录 0）；
      2. 补贴**不**出现在 `sales_revenue`（销售收入只来自卖货）；
      3. 补贴**不**出现在 `value_added`（GDP 不含转移支付）。
    """
    from engine import pipeline

    cfg = load_config()
    st = make_state(cfg, "p1", mature("0,0", PlotType.RESOURCE, 1))
    pipeline.advance(st, {"p1": plain_plan("p1", "0,0", u=1.0)}, cfg)
    r = st.raw_reports["p1"]

    assert abs(r.subsidy) < 1e-9, (
        f"报告里的补贴应为 0（S1 起季初发放、结算不记），实得 {r.subsidy}"
    )
    # 甲区运营锚点：销售收入 94.72、增加值 66 —— 都不含补贴
    assert abs(r.sales_revenue - 94.72) < 1e-9, (
        f"★ 补贴不得混进销售收入：应为 94.72，实得 {r.sales_revenue}"
    )
    assert abs(r.value_added - 66.0) < TOL, (
        f"★ 补贴不得混进增加值：应为 66，实得 {r.value_added}"
    )


def test_subsidy_for_quarter_tiers():
    """★ S1（2026-10-18）：`Config.subsidy_for_quarter(q)` 的两档切换**纯函数**。

    补贴发钱已挪到 `server/session.credit_opening_subsidy()`（季初），
    pipeline 不再消费这条公式 —— 但它仍是**唯一口径**（开局/推进都用它），
    所以保留单独守护。

    本测试**不依赖** config 的具体数值：
      1. q ≤ early_subsidy_quarters → early_quarterly_subsidy，
         q >  early_subsidy_quarters → quarterly_subsidy；
      2. 边界 q == early_subsidy_quarters 仍在 early 档，q+1 回落基准档。
    """
    cfg = load_config()
    # config 必须确实配了两档 —— 否则这条用例就是在打空气，立刻报警。
    assert cfg.early_subsidy_quarters >= 1, (
        "本用例要求 config.economy.early_subsidy_quarters ≥ 1；S1 配置是 7。"
    )
    assert cfg.early_quarterly_subsidy != cfg.quarterly_subsidy, (
        "本用例要求 early 档与基准档不同值；S1 配置是 1200 vs 300。"
    )

    # ── 纯函数本身 ─────────────────────────────────────
    assert abs(cfg.subsidy_for_quarter(1) - cfg.early_quarterly_subsidy) < 1e-9
    assert abs(cfg.subsidy_for_quarter(cfg.early_subsidy_quarters)
               - cfg.early_quarterly_subsidy) < 1e-9, (
        "边界 quarter == early_subsidy_quarters 仍在 early 档"
    )
    assert abs(cfg.subsidy_for_quarter(cfg.early_subsidy_quarters + 1)
               - cfg.quarterly_subsidy) < 1e-9, (
        "边界 quarter == early_subsidy_quarters + 1 应回落到基准档"
    )


def test_pipeline_subsidy_always_zero():
    """★ S1（2026-10-18）：**结算报告里 subsidy 恒 0** —— preview 也不含补贴。

    这是"补贴挪到季初发放"在引擎侧的**镜像守护**：
    pipeline.advance / pipeline.preview 的任意一季报告都必须给 0；
    真发钱在 server/session 层（见 server/session.py）。
    """
    from engine import pipeline

    cfg = load_config()
    st = make_state(cfg, "p1", mature("0,0", PlotType.RESOURCE, 1))

    # ── Q1（early 档季度）：advance 报告 subsidy = 0 ──
    pipeline.advance(st, {"p1": plain_plan("p1", "0,0", u=1.0)}, cfg)
    r1 = st.raw_reports["p1"]
    assert abs(r1.subsidy) < 1e-9, (
        f"S1 起结算报告 subsidy 应恒 0（Q1 也不例外），实得 {r1.subsidy}"
    )
    # Q1（early 档）的 sales_revenue / value_added 不受补贴影响
    assert abs(r1.sales_revenue - 94.72) < 1e-9, "Q1 销售收入不含补贴"
    assert abs(r1.value_added - 66.0) < TOL, "Q1 增加值不含补贴"

    # ── preview 同源（试算也必须给 0）─────────────────
    st2 = make_state(cfg, "p1", mature("0,0", PlotType.RESOURCE, 1))
    preview_report = pipeline.preview(st2, {"p1": plain_plan("p1", "0,0", u=1.0)}, cfg)
    assert abs(preview_report.subsidy) < 1e-9, (
        f"S1 起试算报告 subsidy 也应为 0，实得 {preview_report.subsidy}"
    )

    # ── Q8（基准档季度）：把 quarter 推到 early 边界之后，同样 0 ──
    st3 = make_state(cfg, "p1", mature("0,0", PlotType.RESOURCE, 1))
    st3.world.quarter = cfg.early_subsidy_quarters + 1     # 7+1 = 8（Q8 起 300）
    pipeline.advance(st3, {"p1": plain_plan("p1", "0,0", u=1.0)}, cfg)
    r8 = st3.raw_reports["p1"]
    assert abs(r8.subsidy) < 1e-9, (
        f"S1 起 Q{cfg.early_subsidy_quarters + 1}（基准档）结算报告 subsidy 也应为 0，"
        f"实得 {r8.subsidy}"
    )
    assert abs(r8.sales_revenue - 94.72) < 1e-9, "Q8+ 销售收入不受补贴影响"
    assert abs(r8.value_added - 66.0) < TOL, "Q8+ 增加值不受补贴影响"


def test_m1_production_is_the_supply():
    """★ 没有仓库：本季产出**自动全部**成为市场供给（用户 2026-10-16 拍板）。

    不需要玩家申报卖多少 —— `X_t` 就是产出本身。
    """
    from engine import pipeline

    cfg = load_config()
    st = make_state(cfg, "p1", mature("0,0", PlotType.RESOURCE, 1))
    pipeline.advance(st, {"p1": plain_plan("p1", "0,0", u=1.0)}, cfg)
    r = st.raw_reports["p1"]

    assert r.sold == {"F": 4.0}, f"X_t 应等于产出，实得 {r.sold}"
    assert r.prices["F"]["sold"] == 4.0, "求价用的 X_t 也必须等于产出"
    assert st.players["p1"].inventory == {}, "没有仓库 → 季末不该有库存"


def test_m1_production_inputs_are_consumed_not_stored():
    """★ 生产原料**当场消耗**，不是库存（否则会被居民当口粮吃掉）。

    这是本轮抓到的一个真 bug：G 工厂买的 C×2 / D×1 曾经躺在库存里，
    第 7.5 步粮食结算把它们当"能吃的"吃了，增加值从 110 掉到 52。
    """
    from engine import pipeline

    cfg = load_config()
    st = make_state(cfg, "p1", mature("0,0", PlotType.INDUSTRY, 1, product="G"))
    pipeline.advance(st, {"p1": plain_plan("p1", "0,0", u=1.0)}, cfg)
    r = st.raw_reports["p1"]

    assert r.bought == {"C": 2.0, "D": 1.0, "F": 3.0}, "原料要买"
    assert r.food.deducted == {}, "★ 玩家没报口粮 → 居民就不该吃到任何东西"
    assert st.players["p1"].inventory == {}, "原料不该留在库存里"


# ══════════════════════════════════════════════════════════════════
# 五、命令行对账（不用 pytest 也能看）
# ══════════════════════════════════════════════════════════════════


def _print_reconciliation() -> None:
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
    print("\n价格一步：弹性已到位（2026-10-16），结算价与期末现金已在上方断言真算。")
    print("  对应的完整账（上方第六节断言：甲 23.68/1032.32、乙 55.481481/1066.907407）：")
    print("    甲：期末现金 = 1000 − 66  + 4·P_F + 3.60")
    print("    乙：期末现金 = 1000 − 215 + 5·P_G + 4.50")
    for g in ("F", "G"):
        c = cfg.market[g]
        print(f"  {g}: P0={c.P0} Q0={c.Q0} Es={c.Es} Ed={c.Ed}")


def _run_all() -> int:
    """跑全部 `test_*` 断言（与其他测试文件的 runner 同款写法）。

    ⚠️ 2026-10-17 修复：以前 `__main__` 只调 `_print_reconciliation()`，
       15 条断言一条都没跑过 —— M1 锚点的守护是假的。现在先跑断言、
       逐条 PASS/FAIL（失败带期望 vs 实际），再打对账表。
    """
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    mod = sys.modules[__name__]
    names = sorted(n for n in dir(mod) if n.startswith("test_"))
    ok = fail = 0
    print("=" * 68)
    print("M1 断言（锚点守护）")
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
    rc = _run_all()
    print()
    _print_reconciliation()
    raise SystemExit(rc)
