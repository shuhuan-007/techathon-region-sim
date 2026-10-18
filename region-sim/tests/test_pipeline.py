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


def test_single_player_contract_is_mechanically_impossible():
    """★ 合同通道（§7.2 A 方案）：**单人局里合同机制上签不成**，不是报错。

    用户 2026-10-16 深夜裁定：
      选 A（**玩家对玩家**：双方各自提交、互相确认才生效），
      并且**第一期合同实现不了** —— 现在就是单人局，永远没有第二个玩家去提交镜像。
    ⇒ 所以单人局的正解是：**照常结算、账本一字不留、不转账不扣货**，
      而不是像旧版那样 raise（旧版是"功能未实现"的占位，本轮已换成真机制）。

    ⚠️ 这条测试**同时钉住三件事**：不报错、`state.contracts` 为空、现金与库存都没动。
       （多人局里"单方提交"是另一条路径：整季报错，见 `_settle_contracts` 的说明。
         本文件是单人局夹具，验的是单人局那条。）
    """
    from engine.models import ContractAction

    cfg = load_config()
    st = make_state(cfg, "p1", mature("0,0", PlotType.RESOURCE, 1))
    plan = plain_plan("p1", "0,0", u=1.0)
    plan.contracts.append(
        ContractAction(action="deliver", side="sell", counterparty="p2",
                       goods="F", qty=1.0, price=20.0, quarter=1)
    )

    cash_before = st.players["p1"].cash
    inv_before = dict(st.players["p1"].inventory)

    # ① 不报错：单人局签不成合同不是"计划有问题"
    pipeline.advance(st, {"p1": plan}, cfg)

    # ② 账本一字不留
    assert st.contracts == {}, f"单人局不该落任何合同账：{st.contracts}"

    # ③ 没有货物往来；货照旧全部进公开市场
    assert dict(st.players["p1"].inventory) == inv_before, (
        "合同签不成 → 不该有任何货物往来"
    )
    rep = st.raw_reports["p1"]
    assert rep.contracted_out == {}, f"不该有合同交付量：{rep.contracted_out}"


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
    own_eaten = r.food.deducted["A"]
    assert r.bought_breakdown["household_food_from_own"] == {"A": own_eaten}, (
        "只有居民**实际吃掉**的自产粮才从供给里扣；多申报的余量应继续可售"
    )
    assert r.bought_breakdown["household_food_shortfall"] == {"B": 0.6}, "B 自家没有 → 缺口"
    assert abs(r.sold["A"] - (produced_a - own_eaten)) < TOL, (
        "★ 余量进公开市场：X_t = 产出 − 居民实际吃掉的自产粮"
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


def test_zero_demand_keeps_previous_happiness():
    """★ 零需求边界：`F_基准 = 0` 时 **保持上季 H**，不是白送 1.0。

    2026-10-16 用户拍板。这是修掉一个真漏洞的回归测试：

        旧行为：一块地都没有 / 建了地整季不申报开工 → `F_基准 = 0`
                → 结算白给 `H = 1.0` → 下季 `E_幸福 = 1.10`（开局本该 0.90）
        新行为：保持上季 H；首季没有上季，取 `H₀ = 0.6`（基线 §3.3.5）

    ⚠️ 注意**不能**走第 1 段公式 `0.6 × F / F_基准` —— 分母为 0 会除零。
    """
    from engine.settlement import happiness_from_level

    cfg = load_config()

    # 不传 prev_happiness → 用首季默认值 0.6
    assert abs(happiness_from_level(0.0, 0.0, 0.0, cfg) - 0.6) < TOL, (
        "零需求时应回落到 H₀ = 0.6，而不是 1.0"
    )

    # 传了上季 H → 原样保持（既不给奖也不给罚）
    for prev in (0.0, 0.35, 0.6, 0.83, 1.0):
        got = happiness_from_level(0.0, 0.0, 0.0, cfg, prev)
        assert abs(got - prev) < TOL, f"上季 H={prev} 时应原样保持，实得 {got}"

    # 边界：只要 F_基准 > 0，就必须走正常三段，不能被这条短路吃掉
    assert abs(happiness_from_level(0.0, 2.0, 3.0, cfg, 0.9) - 0.0) < TOL, (
        "有需求而完全没投粮时 H 应算作 0.00，不是保持上季值"
    )


# ══════════════════════════════════════════════════════════════════
# 九、★ 已拍板的一局参数（2026-10-16 用户拍板）—— 钉住配置，防止被改回去
# ══════════════════════════════════════════════════════════════════
#
# 这三条**规则上早已定**（基线 §11.9.1 / §11.9.2 / §3.6），
# 但 `config.json` 曾长期与它们不一致（40 / null / null）。
# 本轮（2026-10-16 晚）已把配置对齐，这里加测试**钉住**，
# 否则以后有人改回 `null` 或 40，不会有任何东西报警。


def test_total_quarters_is_20():
    """★ 一局 **20 季**（基线 §11.9.2，2026-10-16 用户再次确认）。

    ⚠️ **已知取舍**：高技术路线总工期 14 季（§5.2），20 季局里
       只有"开局即投"才勉强走得完。这条**明确记录为已知取舍、不再复议**——
       所以**不要**为了让高技术路线跑得舒服而把 20 改回 40。
    """
    cfg = load_config()
    assert cfg.total_quarters == 20, (
        f"一局总季数应为 20（基线 §11.9.2），实得 {cfg.total_quarters}。\n"
        f"  ⚠️ 若确实要改这个数，请先改《规则基线》§11.9.2，再改 config。"
    )

    # 世界状态也要跟着（new_world 会把 config 的值写进去，别让两处脱钩）
    world = cfg.new_world()
    assert world.total_quarters == 20, (
        f"WorldState 的总季数应与 config 一致，实得 {world.total_quarters}"
    )

    # clock 的兜底缺省值同样是 20（UI 画"第 7 / 20 季"用）
    from engine.clock import DEFAULT_TOTAL_QUARTERS
    assert DEFAULT_TOTAL_QUARTERS == 20, (
        f"clock.DEFAULT_TOTAL_QUARTERS 应为 20，实得 {DEFAULT_TOTAL_QUARTERS}"
    )

    # ★ `WorldState` 的 dataclass 兜底缺省值也必须是 20。
    # ⚠️ 这里曾漏了一处：config 与 clock 都改成 20 了，
    #    `models.WorldState.total_quarters` 却还写着 40（旧交接书建议）。
    #    直接 `WorldState()` 造出来的世界会以为一局有 40 季——
    #    `new_world()` 会覆盖它，所以正常路径看不出来，属于"潜伏的不一致"。
    from engine.models import WorldState
    assert WorldState().total_quarters == 20, (
        f"models.WorldState 的缺省总季数应为 20，实得 {WorldState().total_quarters}。\n"
        f"  ⚠️ 缺省值不能与 config/基线脱钩（正常路径被 new_world 覆盖，看不出来）。"
    )


def test_opening_cash_is_1200():
    """★ 开局现金 **1200**（基线 §11.9.1，队友 v1.1 交付已采纳）。

    ⚠️ **不得把 1200 当 M1 输入**：M1 夹具按**各 1000**（那是测试题设）。
       本测试只验"正式开局值"，不碰 M1 的表。
    """
    cfg = load_config()
    assert cfg.opening_cash is not None, (
        "开局现金不能是 null —— 那会让 new_player() 直接报错，一局开不了。\n"
        "  基线 §11.9.1 已定案 1200。"
    )
    assert abs(cfg.opening_cash - 1200) < TOL, (
        f"开局现金应为 1200（基线 §11.9.1），实得 {cfg.opening_cash}"
    )

    # 真的能造出一个开局玩家（之前 opening_cash=None 时这里会抛 ConfigError）
    player = cfg.new_player("p1")
    assert abs(player.cash - 1200) < TOL, f"开局玩家现金应为 1200，实得 {player.cash}"


def test_service_adjacency_is_capped_at_40pct():
    """★ 服务邻接**封顶 +40%**（基线 §3.6，2026-10-16 晚拍板）。

    这是本轮修掉的一个**真实配置漏洞**：`service_cap` 曾是 `null`（不封顶），
    于是三级服务在 8 邻居时给出 `+80%`（`E = 1.80`），而规则说最多 `+40%`。

    封顶后：`E_邻接(服务) = 1 + min(rate(等级) × 邻居数, 0.40)`

    | 服务等级 | 3 邻居 | 8 邻居 |
    |---:|---:|---:|
    | 1 级 | 1.15 | **1.40**（0.05×8=0.40，恰好不截） |
    | 2 级 | 1.225 | **1.40**（0.075×8=0.60 → 截到 0.40） |
    | 3 级 | 1.30 | **1.40**（0.10×8=0.80 → 截到 0.40） |

    ⚠️ **已知取舍**（基线 §3.6 自己承认）：封顶后 1/2/3 级在 **≥8 邻居时全都撞到 +40%**，
       也就是"邻居多的时候，服务等级没区别"、服务升级的边际收益变小。
       这是**有意接受**的——规则说要与普通地块的 +40% 上限一致。
       **不要**因为这条测试的存在就以为封顶写错了。
    """
    cfg = load_config()
    eff = cfg.efficiency

    assert eff.service_cap is not None, (
        "service_cap 不能是 null（= 不封顶）。\n"
        "  基线 §3.6 已拍板：服务邻接封顶 +40%，与普通地块一致。\n"
        "  ⚠️ 不封顶时三级服务 8 邻居会给出 +80%（E=1.80），是错的。"
    )
    assert abs(eff.service_cap - 0.40) < TOL, (
        f"服务邻接封顶应为 0.40，实得 {eff.service_cap}"
    )

    # ── 封顶前（3 邻居）：按等级取率，还没撞到顶 ──────────
    expected_3 = {1: 1.15, 2: 1.225, 3: 1.30}
    for level, want in expected_3.items():
        got = eff.service_adjacency(level, 3)
        assert abs(got - want) < TOL, (
            f"{level} 级服务 / 3 邻居：E_邻接 应为 {want}，实得 {got}"
        )

    # ── 封顶后（8 邻居）：三级都要被截到 1.40 ────────────
    for level in (1, 2, 3):
        got = eff.service_adjacency(level, 8)
        assert abs(got - 1.40) < TOL, (
            f"★ {level} 级服务 / 8 邻居：E_邻接 应被截到 1.40，实得 {got}\n"
            f"  （未截时 1 级=1.40、2 级=1.60、3 级=1.80 —— 2/3 级就是本测试要挡的 bug）"
        )

    # ── 非服务的通用地块仍按关联表 +5%/邻居、封顶 +40%（两条路互不影响）──
    assert abs(eff.adjacency_cap - 0.40) < TOL, "普通地块的 +40% 上限不应被动过"
    assert abs(eff.adjacency_per_neighbor - 0.05) < TOL, "普通地块的每邻居 +5% 不应被动过"


def test_service_cap_end_to_end():
    """★ 端到端：真摆一块三级服务 + 8 个邻居，走 `compute_efficiency` 验封顶。

    上面那条测的是 `service_adjacency()`（纯算式）；
    这条测**真实地块摆法**能不能走到同一个数 —— 防止
    "算式封顶了、但 `efficiency.py` 的调用路径忘了传" 这类脱钩。
    """
    from engine.efficiency import compute_efficiency
    from engine.models import Plot, PlotStatus

    cfg = load_config()

    # 地图中央 (1,1)，八邻格全在界内 → 恰好 8 个邻居
    center = "1,1"
    plot = Plot(
        plot_id=center, plot_type=PlotType.SERVICE, level=3,
        status=PlotStatus.RUNNING, running_quarters=99,   # 磨合满 → E_磨合 = 1.0
    )
    st = make_state(cfg, "p1", plot)
    st.players["p1"].last_happiness = 0.8                 # → E_幸福 = 1.00
    st.players["p1"].plots = {center: plot}

    # 铺满八邻格（全都正常运行，服务对任何邻居都算）
    offsets = [(-1, -1), (-1, 0), (-1, 1), (0, -1),
               (0, 1), (1, -1), (1, 0), (1, 1)]
    for i, (dr, dc) in enumerate(offsets):
        nid = f"{1 + dr},{1 + dc}"
        st.players["p1"].plots[nid] = Plot(
            plot_id=nid, plot_type=PlotType.RESOURCE, level=1,
            status=PlotStatus.RUNNING, running_quarters=99,
        )

    br = compute_efficiency(st, "p1", plot, cfg)

    assert br.is_service, "这块地应被认成服务"
    assert br.linked_neighbors == 8, f"应有 8 个邻居，实得 {br.linked_neighbors}"
    assert br.adjacency_capped, "★ 8 邻居 × 10% = 80% > 40%，必须被标记为已封顶"
    assert abs(br.adjacency_bonus - 0.40) < TOL, (
        f"封顶后加成应为 0.40，实得 {br.adjacency_bonus}"
    )
    assert abs(br.adjacency - 1.40) < TOL, (
        f"★ E_邻接 应为 1.40，实得 {br.adjacency}（未封顶时会是 1.80）"
    )


def test_config_ready_no_longer_flags_settled_items():
    """`validate_config_ready()` 的缺失清单里，**不该再出现**已结清的三项。

    ★ 2026-10-16 深夜：**三项现在全部结清**（开局减免名额已由用户正式裁定：
    每类 3 个名额 × 折扣 0.50）。所以这条从「还差一项」改成「一项都不该差」。

    ⚠️ 任何一项在清单里重现，都说明配置被改回去了。
    """
    from engine.loader import validate_config_ready

    cfg = load_config()
    problems = validate_config_ready(cfg)
    joined = " | ".join(problems)

    assert "开局现金" not in joined, (
        f"开局现金已拍板为 1200，不该再报缺失。当前清单：{problems}"
    )
    assert "construction" not in joined and "建设" not in joined, (
        f"★ 建设表已按基线 §5.2 录入 14 行，不该再报缺失。当前清单：{problems}\n"
        f"  ⚠️ 若这里失败，说明 construction.works 又被清空了。"
    )
    assert "减免名额" not in joined, (
        f"★ 开局减免名额已于 2026-10-16 正式裁定（每类 3 个 × 0.50），"
        f"不该再报未填。当前清单：{problems}\n"
        f"  ⚠️ 若这里失败，说明 config.json 的 opening.discount_quota / "
        f"discount_rate 又被清成 null 了。"
    )
    assert cfg.opening_quota == {
        "agriculture": 3, "resource": 3, "industry": 3, "service": 3,
    }, f"每类名额应为 3，实得 {cfg.opening_quota}"
    assert abs(cfg.opening_discount_rate - 0.5) < 1e-9, (
        f"折扣率应为 0.50，实得 {cfg.opening_discount_rate}"
    )


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
