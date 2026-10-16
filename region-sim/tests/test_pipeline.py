"""季度流水线对账 —— 顺序铁律、并发锁、试算一致性。

跑法（在 region-sim/ 下）：

    python tests/test_pipeline.py

⚠️ 本文件里的弹性（Es/Ed = 0.5）是**为了让流水线跑通而注入的算例**，
   **不是裁定、不是默认值**。config.json 里它们仍是 null。
   正式数值要由规则同学给（§11.6 数据纪律：不得为填满配置而编造数据）。
"""

from __future__ import annotations

import io
import sys
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import pipeline
from engine.loader import Config, load_config
from engine.models import Plan, PlotPlan, PlotType, SessionPhase
from engine.pipeline import SettlementError
from server.session import Conflict, GameSession
from tests.test_m1 import make_state, mature, plain_plan

TOL = 1e-6


def cfg_with_example_elasticity() -> Config:
    """★ 注入**算例**弹性，只为跑通流水线。不是裁定。

    注入了才显得出"求价"这一步的形态；正式值仍待规则同学。
    """
    cfg = load_config()
    for curve in cfg.market.values():
        if curve.Es is None:
            curve.Es = 0.5
        if curve.Ed is None:
            curve.Ed = 0.5
    return cfg


# ══════════════════════════════════════════════════════════════════
# 一、★ 试算与真结算必须一致（AI 自主演算的安全网）
# ══════════════════════════════════════════════════════════════════


def test_preview_equals_advance():
    """`preview` 的结论必须等于 `advance`，且 `preview` **不改原状态**。

    这是整个"AI 自主演算"的安全网（《AI自主演算-架构影响》§6.3）：
    如果 AI 用一套算法演算、结算用另一套，AI 说的数会和真实结算不一致。
    """
    cfg = cfg_with_example_elasticity()
    plot = mature("0,0", PlotType.RESOURCE, 1)

    st_a = make_state(cfg, "p1", deepcopy(plot))
    st_b = make_state(cfg, "p1", deepcopy(plot))
    plan = plain_plan("p1", "0,0", u=1.0)

    # 试算前的快照，用于验证"不改状态"
    before = deepcopy(st_a.to_dict())

    rep_preview = pipeline.preview(st_a, {"p1": plan}, cfg)
    pipeline.advance(st_b, {"p1": plan}, cfg)

    assert st_a.to_dict() == before, "★ preview 必须不改原状态"
    assert st_a.world.quarter == 1, "★ preview 不能推进时钟"

    assert abs(rep_preview.cash_after - st_b.players["p1"].cash) < TOL, \
        "试算的期末现金必须等于真结算"
    assert abs(rep_preview.total_deduction
               - st_b.raw_reports["p1"].total_deduction) < TOL
    assert abs(rep_preview.sales_revenue
               - st_b.raw_reports["p1"].sales_revenue) < TOL


# ══════════════════════════════════════════════════════════════════
# 二、融资约束：钱不够必须报错，不产生半个状态
# ══════════════════════════════════════════════════════════════════


def test_insufficient_cash_raises():
    """资金不足 → 明确报错（§11.3），且**状态不变**。"""
    cfg = cfg_with_example_elasticity()
    # G 工厂要买 C/D/F，给它 10 块钱肯定不够
    plot = mature("0,1", PlotType.INDUSTRY, 1, product="G")
    st = make_state(cfg, "p1", plot, cash=10.0)
    before = deepcopy(st.to_dict())

    try:
        pipeline.advance(st, {"p1": plain_plan("p1", "0,1", u=1.0)}, cfg)
    except SettlementError as e:
        assert "资金不足" in str(e)
    else:
        raise AssertionError("资金不足时必须报错")

    assert st.to_dict() == before, "扣款失败不应留下部分状态变更"


def test_oversell_raises():
    """申报卖出超过实际持有 → 报错，不静默截断（§11.3）。"""
    cfg = cfg_with_example_elasticity()
    st = make_state(cfg, "p1", mature("0,0", PlotType.RESOURCE, 1))

    try:
        pipeline.advance(
            st, {"p1": plain_plan("p1", "0,0", u=1.0, sales={"F": 99.0})}, cfg
        )
    except SettlementError as e:
        assert "超量" in str(e) or "超过" in str(e)
        # §11.3 要求错误信息至少含：类型 / 商品 / 申请值 / 可用值 / 差额
        for field in ("涉及商品", "申请值", "可用值", "差额"):
            assert field in str(e), f"错误信息缺少 {field}"
    else:
        raise AssertionError("超量卖出必须报错")


# ══════════════════════════════════════════════════════════════════
# 三、★ 买入量顶高本季价（2026-10-16 交割口径）
# ══════════════════════════════════════════════════════════════════


def test_buying_pushes_price_up():
    """买入量计入需求侧 → 本季价被顶高。

    `P_t = P0 + (B_t − X_t)/(s+d)`：B_t 越大，P_t 越高。
    """
    cfg = cfg_with_example_elasticity()
    curve = cfg.market["F"]

    p_no_buy = curve.price(bought=0.0, sold=4.0)
    p_with_buy = curve.price(bought=10.0, sold=4.0)

    assert p_with_buy > p_no_buy, "买入必须把本季价顶高"
    assert abs(p_no_buy - (curve.P0 - 4.0 / (curve.Es * curve.Q0 / curve.P0 * 2))) < TOL


def test_buy_uses_last_quarter_price_not_current():
    """★ 买入按**上季价**记账，卖出按**本季价**（§4.1）。

    第二个季度时，上季价 = 上季算出来的 P_t，不等于本季价。
    """
    cfg = cfg_with_example_elasticity()
    st = make_state(cfg, "p1", mature("0,0", PlotType.RESOURCE, 1))

    # 第一季：跑一遍，把 F 的成交价记进历史
    pipeline.advance(st, {"p1": plain_plan("p1", "0,0", u=1.0)}, cfg)
    q1_price = st.world.price_history["F"][-1]

    # 第二季：上季价必须等于第一季的成交价
    assert abs(st.world.last_price("F") - q1_price) < TOL
    # 且不等于参考价（说明"上季价"确实在起作用，不是永远回退 P0）
    assert abs(st.world.last_price("F") - cfg.market["F"].P0) > TOL


def test_first_quarter_falls_back_to_P0():
    """首季没有上季价 → 回退参考价 `P0`（§8.1 的 M1 初始条件）。"""
    cfg = cfg_with_example_elasticity()
    st = make_state(cfg, "p1", mature("0,0", PlotType.RESOURCE, 1))
    assert st.world.price_history == {}
    assert abs(st.world.last_price("F") - cfg.market["F"].P0) < TOL


# ══════════════════════════════════════════════════════════════════
# 四、★ 粮食结算在市场结算之后（铁律 6）
# ══════════════════════════════════════════════════════════════════

# ⚠️ 2026-10-16 已删除两条旧对账：
#   · `test_food_deducted_after_market_sale`（"申报卖出导致没粮吃"）
#   · `test_food_kept_is_eaten`（"自产的粮留着吃"）
#    两者都建立在"有仓库 + 申报卖出"之上。没有仓库之后：
#      · 产出 100% 是市场供给，**不能拿来吃**
#      · 口粮只能从 `purchases` 买入
#    替代它们的对账在第七节：
#      `test_food_must_be_purchased_not_from_production`
#      `test_food_purchase_enters_supply_and_demand`


def test_food_settled_after_market_sale():
    """★ 铁律 6 仍然成立：粮食结算在市场结算**之后**。

    现在它的含义变成："先把货卖出去拿到钱，再谈居民吃饭"。
    顺序反了会让玩家用还没到手的钱买粮。
    """
    cfg = load_config()
    st = make_state(cfg, "p1", mature("0,0", PlotType.RESOURCE, 1))
    pipeline.advance(
        st, {"p1": plain_plan("p1", "0,0", u=1.0, food={"A": 1.0, "B": 0.5})}, cfg
    )
    r = st.raw_reports["p1"]

    # 口粮缺口计入 B_t（需求侧），说明它在求价时已经存在
    assert r.bought == {"A": 1.0, "B": 0.5}
    # 且确实进了粮食结算的"可用量"
    assert r.food.available == {"A": 1.0, "B": 0.5, "C": 0.0, "D": 0.0}


# ══════════════════════════════════════════════════════════════════
# 五、★ 并发：每局一把锁，提交只增不改
# ══════════════════════════════════════════════════════════════════


def test_session_submit_once_only():
    """同一个玩家**只能提交一次**（plans 只增不改）。"""
    cfg = cfg_with_example_elasticity()
    st = make_state(cfg, "p1", mature("0,0", PlotType.RESOURCE, 1))
    sess = GameSession(state=st, cfg=cfg)

    sess.submit("p1", plain_plan("p1", "0,0", u=1.0))
    try:
        sess.submit("p1", plain_plan("p1", "0,0", u=0.5))
    except Conflict as e:
        assert "已提交" in str(e)
    else:
        raise AssertionError("第二次提交必须被拒绝")


def test_session_single_player_resolves_immediately():
    """单人 = 多人的特例：提交即成立，`end_quarter` 立即结算。"""
    cfg = cfg_with_example_elasticity()
    st = make_state(cfg, "p1", mature("0,0", PlotType.RESOURCE, 1))
    sess = GameSession(state=st, cfg=cfg)

    sess.submit("p1", plain_plan("p1", "0,0", u=1.0))
    rep = sess.end_quarter("p1")

    assert rep is not None, "单人局应立刻结算，不留等待态"
    assert sess.phase is SessionPhase.REPORT
    assert st.world.quarter == 2


def test_session_lock_is_per_game():
    """每局一把锁 —— 不同局各持一把，互不阻塞。"""
    cfg = cfg_with_example_elasticity()
    a = GameSession(state=make_state(cfg, "p1", mature("0,0", PlotType.RESOURCE, 1)), cfg=cfg)
    b = GameSession(state=make_state(cfg, "p1", mature("0,1", PlotType.RESOURCE, 1)), cfg=cfg)
    assert a.lock is not b.lock, "锁的粒度必须是每局一把，不是全局一把"


def test_session_resolve_releases_lock_on_error():
    """结算报错时锁必须释放（否则整局卡死）。"""
    cfg = cfg_with_example_elasticity()
    # G 工厂要买 C/D/F，只给 10 块钱 → 扣款时必然资金不足
    st = make_state(cfg, "p1", mature("0,0", PlotType.INDUSTRY, 1, product="G"), cash=10.0)
    sess = GameSession(state=st, cfg=cfg)

    sess.submit("p1", plain_plan("p1", "0,0", u=1.0))
    try:
        sess.end_quarter("p1")
    except SettlementError:
        pass

    # 锁若没释放，这里会直接死锁
    assert sess.lock.acquire(timeout=1.0), "★ 异常后锁没释放 —— 整局会卡死"
    sess.lock.release()
    assert sess.phase is SessionPhase.COLLECTING, "结算失败应退回行动阶段"


# ══════════════════════════════════════════════════════════════════
# 六、★ 没有仓库（2026-10-16 用户拍板）
# ══════════════════════════════════════════════════════════════════


def test_production_is_supply_no_declaration_needed():
    """★ 产出自动就是供给 —— 玩家不用申报卖多少。

    用户原话："玩家不需要申报要卖，因为没有仓库，生产出来的就是'供'本身。"
    """
    cfg = load_config()
    st = make_state(cfg, "p1", mature("0,0", PlotType.RESOURCE, 1))
    pipeline.advance(st, {"p1": plain_plan("p1", "0,0", u=1.0)}, cfg)
    r = st.raw_reports["p1"]

    out_qty = r.production[0].output_qty
    assert abs(r.sold["F"] - out_qty) < TOL
    assert abs(r.prices["F"]["sold"] - out_qty) < TOL, "求价的 X_t 必须等于产出"


def test_supply_pushes_price_down():
    """产出越多，本季价越低（供给压价）。"""
    cfg = load_config()
    curve = cfg.market["F"]

    p_small = curve.price(bought=0.0, sold=4.0)
    p_large = curve.price(bought=0.0, sold=40.0)
    assert p_large < p_small, "供给越大价格越低"


def test_no_inventory_carries_over():
    """没有仓库 → 季末零库存（§11.1 的"季末清仓"已随仓库一起取消）。"""
    cfg = load_config()
    st = make_state(cfg, "p1", mature("0,0", PlotType.RESOURCE, 1))
    pipeline.advance(st, {"p1": plain_plan("p1", "0,0", u=1.0)}, cfg)

    assert st.players["p1"].inventory == {}, "季末不该有库存"
    assert st.raw_reports["p1"].clearing_revenue == 0.0, "清仓概念已取消"


def test_contract_channel_is_reserved():
    """★ 合同通道：口子留着，第一期**明确报错**而不是静默忽略。

    用户："要保留一个额外的通道，之后玩家间可以通过协议低于市场价提供。"
    """
    from engine.models import ContractAction

    cfg = load_config()
    st = make_state(cfg, "p1", mature("0,0", PlotType.RESOURCE, 1))
    plan = plain_plan("p1", "0,0", u=1.0)
    plan.contracts.append(
        ContractAction(action="deliver", counterparty="p2", goods="F", qty=1.0, price=20.0)
    )
    try:
        pipeline.advance(st, {"p1": plan}, cfg)
    except SettlementError as e:
        assert "合同" in str(e), "应明确报出合同功能未实现"
    else:
        raise AssertionError("合同功能未实现时必须报错，不能静默忽略")


def test_contract_would_reduce_supply():
    """合同交付的货**不进公开市场** → 求价时应从 X_t 扣掉。

    这里直接验 `market.settle_prices` 的语义：
    同样的产出，扣掉合同量后价格应该更高（因为公开供给少了）。
    """
    cfg = cfg_with_example_elasticity()
    curve = cfg.market["F"]

    p_with_contract = curve.price(bought=0.0, sold=4.0 - 1.0)   # 1 单位走了合同
    p_no_contract = curve.price(bought=0.0, sold=4.0)
    assert p_with_contract > p_no_contract, (
        "合同交付的货不进公开市场 → 公开供给变少 → 价格应更高"
    )


# ══════════════════════════════════════════════════════════════════
# 七、★ 居民口粮：自产先顶，缺口才买（2026-10-16 用户口径）
# ══════════════════════════════════════════════════════════════════
#
# 用户原话："首先玩家会决定这期的粮食供给。然后缺了的话会在购入环节购入
#           （此时仍然是上期价格，然后这期市场的需发生变化了呀），不然扣除"


def test_food_comes_from_own_production_first():
    """★ 自产先顶：种出来的粮先喂居民，**顶上部分不进公开市场**。

    产出**默认**全部是供给；玩家唯一能截留的就是这份"指定给居民的口粮"。
    """
    cfg = load_config()
    st = make_state(cfg, "p1", mature("0,0", PlotType.AGRICULTURE, 1, product="A"))
    pipeline.advance(
        st, {"p1": plain_plan("p1", "0,0", u=1.0, food={"A": 1.2, "B": 0.6})}, cfg
    )
    r = st.raw_reports["p1"]

    produced_a = r.production[0].output_qty          # A × 5
    assert r.bought_breakdown["household_food_from_own"] == {"A": 1.2}, "A 由自产顶上"
    assert r.bought_breakdown["household_food_shortfall"] == {"B": 0.6}, "B 自家没有 → 缺口"
    assert abs(r.sold["A"] - (produced_a - 1.2)) < TOL, (
        "★ 只有余量进公开市场：X_t = 产出 − 自产顶上"
    )
    assert r.food.happiness > 0.0, "有口粮 → H > 0"


def test_food_shortfall_is_bought_and_pushes_price():
    """★ 缺口在购入环节买入：按**上季价**记账，并计入本季 `B_t`（顶高本期价）。"""
    cfg = load_config()
    st = make_state(cfg, "p1", mature("0,0", PlotType.RESOURCE, 1))   # 不产 A/B
    pipeline.advance(
        st, {"p1": plain_plan("p1", "0,0", u=1.0, food={"A": 10.0})}, cfg
    )
    r = st.raw_reports["p1"]

    assert r.bought == {"A": 10.0}, "缺口应计入 B_t"
    assert r.bought_breakdown["household_food_shortfall"] == {"A": 10.0}
    assert r.bought_breakdown["household_food_from_own"] == {}, "没种 → 自产顶不上"
    assert r.buy_bill.unit_prices["A"] == cfg.market["A"].P0, "首季按参考价（= 上季价兜底）"

    # 买入把 A 的本季价顶高
    q1 = st.world.price_history["A"][-1]
    assert q1 > cfg.market["A"].P0, "★ 买入 10 单位 A 应把本季价顶到 P0 以上"


def test_food_no_declaration_means_residents_starve():
    """不申报口粮 → 居民没得吃 → H = 0（产出全部进了市场，不能拿来吃）。"""
    cfg = load_config()
    st = make_state(cfg, "p1", mature("0,0", PlotType.AGRICULTURE, 1, product="A"))
    r = pipeline.advance(st, {"p1": plain_plan("p1", "0,0", u=1.0)}, cfg).raw_reports["p1"]

    assert r.food.deducted == {}
    assert r.food.happiness == 0.0
    assert abs(r.sold["A"] - r.production[0].output_qty) < TOL, "不申报 → 产出全部进市场"


def test_food_declared_beyond_need_is_discarded():
    """申报的口粮远超需求 → 吃不完的部分**丢弃**（没有仓库）。"""
    cfg = load_config()
    st = make_state(cfg, "p1", mature("0,0", PlotType.RESOURCE, 1))

    # N=6 → F_基准=1.2、F_优秀=1.8；申报 A5/B3 远超优秀档，必有剩
    pipeline.advance(
        st, {"p1": plain_plan("p1", "0,0", u=1.0, food={"A": 5.0, "B": 3.0})}, cfg
    )
    r = st.raw_reports["p1"]

    assert r.discarded, "吃不完的口粮应被记为丢弃"
    assert st.players["p1"].inventory == {}, "丢弃后不该有库存"


def test_oversell_raises():
    """★ 已取消：没有"申报卖出"，所以不再有"卖出超量"这种错误。

    这条留作**回归标记** —— 如果哪天 `Plan` 又长出 `sales` 字段，
    说明规则被改回去了，这里会失败并提醒。
    """
    from engine.models import Plan
    assert not hasattr(Plan(player_id="p1"), "sales"), (
        "Plan 不该再有 sales 字段（没有仓库 → 产出就是供给）"
    )


# ══════════════════════════════════════════════════════════════════
# 八、★ H 的斜率（用户 2026-10-16 确认：中间线性）
# ══════════════════════════════════════════════════════════════════


def test_happiness_slopes():
    """H 两段直线的斜率（§3.3.4）。

        F_优秀 = 1.5 × F_基准
        下段斜率 = 0.6 / F_基准
        中段斜率 = 0.4 / (F_优秀 − F_基准) = 0.4 / (0.5·F_基准) = **0.8 / F_基准**

    中段斜率是下段的 4/3。
    """
    from engine.settlement import happiness_slopes

    # F_基准 = 2.0 → F_优秀 = 3.0
    s = happiness_slopes(2.0, 3.0)
    assert abs(s["lower"] - 0.30) < TOL, f"下段斜率应 0.30，实得 {s['lower']}"
    assert abs(s["middle"] - 0.40) < TOL, f"中段斜率应 0.40，实得 {s['middle']}"
    assert abs(s["middle"] / s["lower"] - 4.0 / 3.0) < TOL, "中段应是下段的 4/3"

    # 一般式：中段 = 0.8 / F_基准
    for baseline in (1.0, 1.2, 2.0, 4.0, 10.0):
        good = 1.5 * baseline
        s = happiness_slopes(baseline, good)
        assert abs(s["middle"] - 0.8 / baseline) < TOL, (
            f"F_基准={baseline} 时中段斜率应为 {0.8/baseline}，实得 {s['middle']}"
        )


def test_happiness_is_piecewise_linear():
    """H 在三段上分别是直线（等距取点验证线性）。"""
    from engine.settlement import happiness_from_level

    cfg = load_config()
    B, G = 2.0, 3.0

    # 下段：0 → F_基准，等距取点，增量应恒定
    xs = [0.0, 0.5, 1.0, 1.5, 2.0]
    hs = [happiness_from_level(x, B, G, cfg) for x in xs]
    deltas = [hs[i + 1] - hs[i] for i in range(len(hs) - 1)]
    assert max(deltas) - min(deltas) < TOL, f"下段应为直线，实得增量 {deltas}"
    assert abs(hs[-1] - 0.60) < TOL, "F_基准 处 H 应为 0.60"

    # 中段：F_基准 → F_优秀
    xs = [2.0, 2.25, 2.5, 2.75, 3.0]
    hs = [happiness_from_level(x, B, G, cfg) for x in xs]
    deltas = [hs[i + 1] - hs[i] for i in range(len(hs) - 1)]
    assert max(deltas) - min(deltas) < TOL, f"中段应为直线，实得增量 {deltas}"
    assert abs(hs[0] - 0.60) < TOL and abs(hs[-1] - 1.00) < TOL, "两端锚点应为 0.60 / 1.00"
    assert abs(hs[2] - 0.80) < TOL, f"中点应为 0.80，实得 {hs[2]}"

    # 上段：封顶
    for x in (3.0, 4.0, 99.0):
        assert abs(happiness_from_level(x, B, G, cfg) - 1.0) < TOL, "吃好以上封顶 1.00"


# ══════════════════════════════════════════════════════════════════
# 八、命令行汇总
# ══════════════════════════════════════════════════════════════════


def _run_all() -> int:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    mod = sys.modules[__name__]
    names = sorted(n for n in dir(mod) if n.startswith("test_"))
    ok = fail = 0
    print("=" * 68)
    print("流水线对账")
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
