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
    pipeline.advance(st, {"p1": plain_plan("p1", "0,0", u=1.0, sales={"F": 3.6})}, cfg)
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


def test_food_deducted_after_market_sale():
    """粮食在市场卖出**之后**扣：卖掉的粮不能拿来吃（§6 铁律 6）。"""
    cfg = cfg_with_example_elasticity()
    plot = mature("0,0", PlotType.AGRICULTURE, 1, product="A")
    st = make_state(cfg, "p1", plot)

    # 产 A×5×E。E = 0.9（H₀=0.6）→ 4.5。申报卖 4.5，则手上无粮可吃 → H=0
    rep = pipeline.advance(
        st, {"p1": plain_plan("p1", "0,0", u=1.0, sales={"A": 4.5})}, cfg
    )
    r = st.raw_reports["p1"]
    assert r.food.happiness == 0.0, "全卖掉后无粮可吃，H 应为 0"
    assert r.food.deducted == {}, "没有粮就不该扣出东西"


def test_food_kept_is_eaten():
    """留着的粮会在结算时被吃掉，不参与卖出。"""
    cfg = cfg_with_example_elasticity()
    plot = mature("0,0", PlotType.AGRICULTURE, 1, product="A")
    st = make_state(cfg, "p1", plot)

    # 全部留着自己吃 → 清仓前先扣粮
    pipeline.advance(st, {"p1": plain_plan("p1", "0,0", u=1.0)}, cfg)
    r = st.raw_reports["p1"]

    assert r.food.deducted, "自产的 A 应被粮食结算扣掉一部分"
    assert r.food.happiness > 0.0, "有粮可吃时 H 应大于 0"


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
    st = make_state(cfg, "p1", mature("0,0", PlotType.RESOURCE, 1))
    sess = GameSession(state=st, cfg=cfg)

    sess.submit("p1", plain_plan("p1", "0,0", u=1.0, sales={"F": 99.0}))
    try:
        sess.end_quarter("p1")
    except SettlementError:
        pass

    # 锁若没释放，这里会直接死锁
    assert sess.lock.acquire(timeout=1.0), "★ 异常后锁没释放 —— 整局会卡死"
    sess.lock.release()
    assert sess.phase is SessionPhase.COLLECTING, "结算失败应退回行动阶段"


# ══════════════════════════════════════════════════════════════════
# 六、命令行汇总
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
