"""新闻/财报**留位**对账 —— facts 每个数字都能**在报告里找到同值**，
narrate **明确未实现**而不是偷偷返回编造文本。

跑法（在 region-sim/ 下）：

    python tests/test_news.py

依据《规则基线》§0（程序决定事实）、§11.4（AI 只解释、不编造）与
`engine/news.py` 自己的 docstring（数字原样取自 report / cfg / 状态层，
不许新公式）。
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import news
from engine.loader import load_config
from engine.models import (
    Contract,
    GameState,
    Plan,
    PlayerState,
    Plot,
    PlotPlan,
    PlotStatus,
    PlotType,
    WorldPhase,
)
from engine.pipeline import advance

TOL = 1e-6


# ══════════════════════════════════════════════════════════════════
# 夹具（照抄 test_report.py 的最简单人局）
# ══════════════════════════════════════════════════════════════════


def plot(pid: str, ptype: PlotType = PlotType.RESOURCE, level: int = 1,
         product=None) -> Plot:
    return Plot(plot_id=pid, plot_type=ptype, level=level,
                status=PlotStatus.RUNNING, running_quarters=99, product=product)


def state_with(*plots: Plot, cash: float = 1000.0) -> GameState:
    """`last_happiness = 0.8` → E_幸福 = 1.00（M1 题设）。"""
    cfg = load_config()
    player = PlayerState(player_id="p1", cash=cash,
                         plots={p.plot_id: p for p in plots})
    player.last_happiness = 0.8
    world = cfg.new_world()
    world.phase = WorldPhase.RUNNING
    return GameState(world=world, players={"p1": player})


def full_plan(*plot_ids: str, **kw) -> Plan:
    return Plan(player_id="p1",
                plot_plans={pid: PlotPlan(plot_id=pid, u=1.0) for pid in plot_ids},
                **kw)


def run_one_quarter():
    """打一季，返回 `(state, report, cfg)`。"""
    cfg = load_config()
    st = state_with(plot("0,0"), plot("0,1", PlotType.INDUSTRY, product="G"))
    plan = full_plan("0,0", "0,1", purchases={"C": 2.0, "D": 1.0, "F": 3.0})
    advance(st, {"p1": plan}, cfg)
    rep = st.raw_reports["p1"]
    return st, rep, cfg


# ══════════════════════════════════════════════════════════════════
# 一、facts 每个数字都能在 report 里找到同值
# ══════════════════════════════════════════════════════════════════


def test_facts_scalars_match_report():
    """现金、扣款、销售、增加值、工资、经营费、退税 —— 一个不差。"""
    _, rep, cfg = run_one_quarter()
    f = news.news_facts(rep, cfg)

    assert f["quarter"] == rep.quarter
    assert f["player_id"] == rep.player_id
    for key in ("cash_before", "cash_after", "total_deduction",
                "materials_cost", "wages", "operating",
                "construction_cash", "sales_revenue",
                "wage_tax_refund", "value_added"):
        want = getattr(rep, key)
        assert abs(f[key] - want) < TOL, (
            f"facts[{key}]={f[key]} ≠ report.{key}={want}"
        )


def test_facts_prices_match_report():
    """每一本季成交价、触底位都是报告原文；商品名来自 cfg（不瞎编）。"""
    _, rep, cfg = run_one_quarter()
    f = news.news_facts(rep, cfg)

    assert set(f["prices"].keys()) == set(rep.prices.keys()), "商品集合要对齐"
    for g, pr in rep.prices.items():
        got = f["prices"][g]
        assert abs(got["price"] - pr["price"]) < TOL, f"{g} 本季价不等"
        assert bool(got["at_floor"]) == bool(pr["at_floor"]), f"{g} 触底位不等"
        # 商品名来自 cfg.goods_name（没有注册的商品名退化为代码本身）
        assert got["name"] == cfg.goods_name.get(g, g), "商品名必须来自 cfg"


def test_facts_production_match_report():
    """每块地产量原样搬运（不汇总、不改写）。"""
    _, rep, cfg = run_one_quarter()
    f = news.news_facts(rep, cfg)

    assert len(f["production"]) == len(rep.production)
    by_id = {o.plot_id: o for o in rep.production}
    for row in f["production"]:
        o = by_id[row["plot_id"]]
        assert row["plot_type"] == o.plot_type.value
        assert row["level"] == o.level
        assert row["product"] == o.product
        assert row["output_goods"] == o.output_goods
        assert abs(row["output_qty"] - o.output_qty) < TOL


def test_facts_happiness_match_report():
    """幸福度直接就是 `report.food.happiness`。"""
    _, rep, cfg = run_one_quarter()
    f = news.news_facts(rep, cfg)
    assert rep.food is not None, "夹具该有粮食结算"
    assert abs(f["happiness"] - rep.food.happiness) < TOL


def test_facts_policy_match_report():
    """政策块原样（单人局基本是无政策 → None；形状仍是 dict-or-None）。"""
    _, rep, cfg = run_one_quarter()
    f = news.news_facts(rep, cfg)
    if rep.policy is None:
        assert f["policy"] is None
    else:
        assert f["policy"]["name"] == rep.policy.get("name")
        assert f["policy"]["policy_type"] == rep.policy.get("policy_type")


def test_facts_unfulfilled_from_contracts():
    """未履约明细**只认**调用方递来的合同账本（shortfall > 0 才进）。

    给一条有缺口的合同 → 旗子=True、明细含该缺口；
    不给账本 → 诚实空清单（**绝不**编造缺口）。
    """
    _, rep, cfg = run_one_quarter()

    # 不给 → 干净
    f0 = news.news_facts(rep, cfg)
    assert f0["unfulfilled"]["has_unfulfilled"] is False
    assert f0["unfulfilled"]["contracts"] == []

    # 给一条全额履约 + 一条有缺口
    full = Contract(contract_id="c-full", seller_id="p1", buyer_id="p2",
                    goods="F", qty=10.0, price=5.0, quarter=1,
                    delivered=10.0, settled_cash=50.0, fulfilled=True)
    short = Contract(contract_id="c-short", seller_id="p1", buyer_id="p2",
                     goods="F", qty=60.0, price=5.0, quarter=1,
                     delivered=14.0, settled_cash=70.0, fulfilled=False)
    f1 = news.news_facts(rep, cfg, contracts=[full, short])
    assert f1["unfulfilled"]["has_unfulfilled"] is True
    rows = f1["unfulfilled"]["contracts"]
    assert len(rows) == 1, "shortfall=0 的合同不该进清单"
    assert rows[0]["contract_id"] == "c-short"
    assert rows[0]["goods"] == "F"
    assert abs(rows[0]["shortfall"] - 46.0) < TOL, "shortfall 来自 Contract 派生量"


def test_facts_previous_happiness_pass_through():
    """上季 H 是状态层的事：传就传、不传就 None（不可编造）。"""
    _, rep, cfg = run_one_quarter()
    assert news.news_facts(rep, cfg)["previous_happiness"] is None
    f = news.news_facts(rep, cfg, previous_happiness=0.8)
    assert abs(f["previous_happiness"] - 0.8) < TOL


# ══════════════════════════════════════════════════════════════════
# 二、narrate 必须**明确未实现**（绝不偷偷编造文本）
# ══════════════════════════════════════════════════════════════════


def test_narrate_raises_not_implemented():
    """红线（§11.4）：没提示词就**没**正文 —— 绝不编造。"""
    _, rep, cfg = run_one_quarter()
    facts = news.news_facts(rep, cfg)
    try:
        news.narrate(facts)
    except NotImplementedError as e:
        # 报文自己要点名"这是留位、哪天接、接哪条通道"，
        # 避免有人把它当成 bug 瞒过去。
        msg = str(e)
        assert "2026-10-18" in msg, "报文要写明'提示词哪天到位'"
    else:
        raise AssertionError(
            "narrate() 在没有提示词时**必须**抛 NotImplementedError，"
            "许它偷偷返回编造文本就是 §11.4 红线。"
        )


def test_narrate_not_silently_faked():
    """返回值形状已写死在 docstring 里；今天 callable 只能抛，不能回字符串。"""
    _, rep, cfg = run_one_quarter()
    facts = news.news_facts(rep, cfg)
    for args in ({"facts": facts}, {}, {"facts": None}, ):
        try:
            out = news.narrate(args.get("facts"))
        except NotImplementedError:
            pass
        else:
            raise AssertionError(
                f"narrate 在 facts={args.get('facts')!r} 下居然返回了 "
                f"{type(out).__name__}：{out!r} —— 编造文本，红线"
            )


# ══════════════════════════════════════════════════════════════════
# 收尾
# ══════════════════════════════════════════════════════════════════

CASES = [
    ("1 facts 标量与 report 对齐", test_facts_scalars_match_report),
    ("2 facts 价格与 report 对齐", test_facts_prices_match_report),
    ("3 facts 产量与 report 对齐", test_facts_production_match_report),
    ("4 facts 幸福度与 report 对齐", test_facts_happiness_match_report),
    ("5 facts 政策块与 report 对齐", test_facts_policy_match_report),
    ("6 facts 未履约只认合同账本", test_facts_unfulfilled_from_contracts),
    ("7 facts 上季幸福度诚实缺席", test_facts_previous_happiness_pass_through),
    ("8 narrate 明确未实现", test_narrate_raises_not_implemented),
    ("9 narrate 不偷偷编造文本", test_narrate_not_silently_faked),
]


def main() -> int:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    passed = 0
    for name, fn in CASES:
        try:
            fn()
        except AssertionError as e:
            print(f"  FAILED  {name}\n    {e}")
        except Exception as e:  # noqa: BLE001
            print(f"  ERROR   {name}\n    {type(e).__name__}: {e}")
        else:
            passed += 1
            print(f"  passed  {name}")
    print(f"\n{passed}/{len(CASES)} 通过")
    return 0 if passed == len(CASES) else 1


if __name__ == "__main__":
    raise SystemExit(main())
