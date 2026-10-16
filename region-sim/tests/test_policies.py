"""政策系统测试（《规则基线》§11.9.3）。

覆盖清单（任务书要求逐条对应）：

  1. 每季恰好一项
  2. 四项各 25%（固定种子大样本）
  3. 允许连续抽到相同政策
  4. 仅当季有效、不累计
  5. 第一季也抽
  6. 同一存档重开不重抽
  7. 「生产激励」的效率 ×1.10 与工资 ×1.15 真生效（接进 pipeline 后端到端）

另外两个护栏：

  8. **常规环境 = 零修正**（乘数全 1）—— 本轮踩过：`PolicyDef.wage_multiplier`
     缺省曾是 1.1，"无修正"的政策凭空给工资加 10%。
  9. **停工不发工资的地块不因政策凭空产生工资**（§11.9.3 末条红线）

跑法（本机 python，中文加 -X utf8）::

    python -X utf8 tests/test_policies.py

直接运行会打印 passed/failed 计数；函数也可被 pytest 逐个收集。
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import pipeline
from engine import policies as POL
from engine.loader import load_config
from engine.models import (
    GameState,
    Plan,
    Plot,
    PlayerState,
    PlotPlan,
    PlotStatus,
    PlotType,
    WorldPhase,
)
from engine.rng import DEFAULT_SEED, Rng, rng_for

TOL = 1e-9


# ══════════════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════════════


def fresh_world(cfg, mode: str = POL.MODE_RANDOM, seed: int = DEFAULT_SEED):
    """一个**正式对局**的世界：每季抽政策、指定种子。

    ⚠️ `cfg.new_world()` 给的是**安全缺省**（恒常规环境），
       所以这里必须显式开随机 —— 否则测的就是"政策永远不生效"。
    """
    world = cfg.new_world()
    world.phase = WorldPhase.RUNNING
    POL.set_policy_mode(world, mode)
    POL.set_seed(world, seed)
    return world


def make_state(cfg, plot: Plot, cash: float = 100000.0,
               mode: str = POL.MODE_RANDOM, seed: int = DEFAULT_SEED) -> GameState:
    """单人局：一块成熟地块。`last_happiness = 0.8` → `E_幸福 = 1.00`。"""
    world = fresh_world(cfg, mode=mode, seed=seed)
    player = PlayerState(player_id="p1", cash=cash, plots={plot.plot_id: plot})
    player.last_happiness = 0.8
    return GameState(world=world, players={"p1": player})


def mature(plot_id: str, ptype: PlotType, level: int = 1, product=None) -> Plot:
    """已过磨合的地块（`E_磨合 = 1.0`）。"""
    return Plot(
        plot_id=plot_id, plot_type=ptype, level=level,
        status=PlotStatus.RUNNING, running_quarters=99, product=product,
    )


def plan_for(player_id: str, plot_id: str, u: float = 1.0) -> Plan:
    return Plan(
        player_id=player_id,
        plot_plans={plot_id: PlotPlan(plot_id=plot_id, active=u > 0, u=u)},
    )


def world_with(policy_id: str, quarter: int = 1, cfg=None):
    """直接造一个"本季生效 policy_id"的世界（绕开抽签，定点验证乘数）。

    ⚠️ 这是**测试专用的后门**：它绕过 `ensure_season_policy` 直接放一条政策。
       用来把"乘数接对了没有"与"抽签均不均"两件事分开测。
    """
    w = (cfg or load_config()).new_world()
    w.quarter = quarter
    from engine.models import Policy

    w.policies.append(Policy(
        policy_id=policy_id, policy_type=POL.POLICY_TYPE,
        effective_quarter=quarter, duration=POL.POLICY_DURATION,
        target_goods="ALL", target_player="ALL",
        announcement=POL.def_for(policy_id).announcement,
    ))
    return w


# ══════════════════════════════════════════════════════════════════
# 一、抽取规则
# ══════════════════════════════════════════════════════════════════


def test_each_quarter_exactly_one_policy():
    """★ 每季**恰好一项**；一局 20 季 → 20 条政策。"""
    cfg = load_config()
    world = fresh_world(cfg)

    for q in range(1, 21):
        world.quarter = q
        POL.ensure_season_policy(world)
        active = world.active_policies()
        assert len(active) == 1, f"第 {q} 季应有恰好 1 项生效，实得 {len(active)}"

    assert len(world.policies) == 20, f"20 季应留下 20 条记录，实得 {len(world.policies)}"
    # 每季各一条，且季号不重复
    quarters = [p.effective_quarter for p in world.policies]
    assert quarters == list(range(1, 21)), f"季号应 1..20 连续，实得 {quarters}"


def test_first_quarter_is_drawn_too():
    """★ **第一季也抽取**（基线原文）。"""
    cfg = load_config()
    world = fresh_world(cfg)
    assert world.quarter == 1
    assert world.policies == [], "开局不该预置政策"

    pol = POL.ensure_season_policy(world)
    assert pol is not None, "第一季必须抽到一项政策"
    assert pol.effective_quarter == 1
    assert POL.active_policy_id(world) is not None, "第一季必须有生效政策"


def test_four_policies_each_25_percent():
    """★ 四项**各 25%** —— 固定种子大样本。

    做法：对 40000 个种子各抽第 1 季，统计频次。
    ⚠️ 判定用**宽松容差**（±3%）—— 这是统计检验，不是恒等式；
       同种子必然同结果（确定性），所以这个测试**不会 flaky**。
    """
    cfg = load_config()
    world = cfg.new_world()
    N = 40000

    counter: Counter = Counter()
    for s in range(N):
        POL.set_seed(world, s)
        pid = POL._draw_for_quarter(world, 1)
        assert pid in POL.POLICY_IDS, f"抽出未知政策 {pid!r}"
        counter[pid] += 1

    assert set(counter) == set(POL.POLICY_IDS), (
        f"四项都应被抽到，实得 {sorted(counter)}"
    )
    for pid in POL.POLICY_IDS:
        share = counter[pid] / N
        assert abs(share - 0.25) < 0.03, (
            f"{pid} 占比应约 25%，实得 {share:.4%}（{counter[pid]}/{N}）"
        )


def test_same_seed_same_result():
    """★ 确定性：同种子同结果（不碰 Python 的 `random`）。"""
    cfg = load_config()
    seqs = []
    for _ in range(2):
        w = fresh_world(cfg, seed=20261016)
        seq = []
        for q in range(1, 21):
            w.quarter = q
            seq.append(POL.ensure_season_policy(w).policy_id)
        seqs.append(seq)
    assert seqs[0] == seqs[1], "同种子的两局应逐季一致"

    # 换个种子应当（大概率）不同 —— 否则说明种子根本没起作用
    w = fresh_world(cfg, seed=999)
    other = []
    for q in range(1, 21):
        w.quarter = q
        other.append(POL.ensure_season_policy(w).policy_id)
    assert other != seqs[0], "换了种子序列却完全相同 → 种子没接上"


def test_policy_allows_consecutive_same():
    """★ **允许连续抽到相同政策**（基线原文）—— 不去重。

    做法：把第 `q` 季的抽取结果按 `(seed, q)` 独立算出来，
    找出真实存在的"相邻两季同政策"的种子，再让主流程跑一遍验证它**不被改写**。
    """
    cfg = load_config()
    world = cfg.new_world()

    found = None
    for s in range(500):
        POL.set_seed(world, s)
        seq = [POL._draw_for_quarter(world, q) for q in range(1, 4)]
        if seq[0] == seq[1] or seq[1] == seq[2]:
            found = (s, seq)
            break

    assert found is not None, "500 个种子里找不到任何「相邻同政策」？（概率上不可能）"
    seed, seq = found

    # 主流程必须**原样保留**这个重复，而不是偷偷换一项
    w = fresh_world(cfg, seed=seed)
    got = []
    for q in range(1, 4):
        w.quarter = q
        got.append(POL.ensure_season_policy(w).policy_id)
    assert got == seq, (
        f"连续相同政策被改写了：期望 {seq}，实得 {got}\n"
        f"  ⚠️ 基线明写「允许连续抽到相同政策」，不得去重。"
    )
    # 而且确实出现了重复
    assert got[0] == got[1] or got[1] == got[2], "构造的样本里应真的有相邻重复"


def test_policy_only_effective_for_its_own_quarter():
    """★ **仅当季有效、不累计**（基线原文）。"""
    cfg = load_config()
    world = fresh_world(cfg, seed=20261016)

    # 抽三季，记下每季的 ID
    drawn = {}
    for q in (1, 2, 3):
        world.quarter = q
        drawn[q] = POL.ensure_season_policy(world).policy_id

    # 第 2 季时：第 1 季的政策**已失效**
    world.quarter = 2
    active = world.active_policies()
    assert len(active) == 1, f"第 2 季只应有 1 项生效，实得 {len(active)}"
    assert active[0].effective_quarter == 2, "生效的必须是本季抽的那条"

    # 第 1 季的第 1 条记录仍在存档里，但 `is_active(2)` = False
    first = world.policies[0]
    assert first.effective_quarter == 1
    assert not first.is_active(2), "第 1 季的政策不该在第 2 季仍生效（不累计）"
    assert not first.is_active(3), "更不该在第 3 季生效"

    # 走到第 4 季（还没抽）→ 无生效政策
    world.quarter = 4
    assert world.active_policies() == [], "第 4 季还没抽，不该有生效政策"
    assert POL.active_policy_id(world) is None

    # 每一步的乘数都只反映**当季**
    world.quarter = 2
    m_eff = POL.efficiency_multiplier(world, True)
    expect = POL.POLICY_TABLE[drawn[2]].efficiency_multiplier
    assert abs(m_eff - expect) < TOL, (
        f"第 2 季效率乘数应只来自第 2 季政策：期望 {expect}，实得 {m_eff}"
    )


def test_reload_same_quarter_does_not_redraw():
    """★ **读取同一季度存档不重新抽取**（幂等）。

    两种读档路径都要成立：
      ① 直接反复调 `ensure_season_policy`（= 重开界面）；
      ② `to_dict()` → `from_dict()` 往返（= 真存档）。
    """
    cfg = load_config()
    world = fresh_world(cfg, seed=20261016)
    world.quarter = 3
    first = POL.ensure_season_policy(world).policy_id

    # ① 重开界面：反复调，结果不变、条数不涨
    for _ in range(5):
        again = POL.ensure_season_policy(world)
        assert again.policy_id == first, "重开界面不该换政策"
    assert len(world.policies) == 1, f"不该重复登记，实得 {len(world.policies)}"

    # ② 存档往返：读回来再调，仍是同一条
    data = world.to_dict()
    from engine.models import WorldState

    restored = WorldState.from_dict(data)
    assert restored.quarter == 3
    assert len(restored.policies) == 1, "政策必须进存档"
    assert restored.policies[0].policy_id == first, "读档后政策 ID 要一致"

    again = POL.ensure_season_policy(restored)
    assert again.policy_id == first, "★ 读档后不该重抽"
    assert len(restored.policies) == 1, "读档后不该多出一条"

    # ③ 更狠的一条：**即使把随机状态搞乱**，(seed, quarter) 仍给出同一结果
    POL.set_seed(restored, 424242)
    assert POL._draw_for_quarter(restored, 3) != first or True  # 允许碰巧相同
    still = POL.ensure_season_policy(restored)
    assert still.policy_id == first, (
        "已经抽过的季**绝不能**被重抽 —— 种子改了也一样（存档优先）"
    )


def test_draw_is_independent_of_draw_count():
    """抽取只由 `(seed, 季)` 决定，与"已经抽过几次"无关。

    ⚠️ 这条保证的是"读档后序列不漂"：如果抽签依赖一个可变计数器，
       任何一次多余调用（比如 UI 试算）都会把后面所有季挪位。
    """
    cfg = load_config()
    w1 = fresh_world(cfg, seed=777)
    direct = {q: POL._draw_for_quarter(w1, q) for q in range(1, 11)}

    # 先乱抽一通（推进很多次），再按季取 —— 结果必须相同
    w2 = fresh_world(cfg, seed=777)
    for q in range(1, 21):
        w2.quarter = q
        POL.ensure_season_policy(w2)
    for q in range(1, 11):
        assert POL._draw_for_quarter(w2, q) == direct[q], (
            f"第 {q} 季的抽取被前面的调用影响了"
        )


def test_regular_mode_is_always_regular():
    """`regular` 模式恒为常规环境（M1 锚点用的就是它）。"""
    cfg = load_config()
    world = fresh_world(cfg, mode=POL.MODE_REGULAR)
    for q in range(1, 21):
        world.quarter = q
        pol = POL.ensure_season_policy(world)
        assert pol.policy_id == POL.REGULAR, f"第 {q} 季应是常规环境，实得 {pol.policy_id}"

    # 乘数全 1
    world.quarter = 5
    assert POL.efficiency_multiplier(world, True) == 1.0
    assert POL.wage_multiplier(world, True) == 1.0
    assert POL.operating_multiplier(world, True) == 1.0


def test_default_world_is_regular_not_random():
    """★ 安全缺省：手搓的世界 = 恒常规环境（锚点不随种子漂）。

    正式对局要走 `set_policy_mode(world, "random")`
    （或 `POL.new_game_world(cfg)`），见 `test_new_game_world_is_random`。
    """
    cfg = load_config()
    world = cfg.new_world()          # 不显式设模式
    assert POL.policy_mode(world) == POL.MODE_REGULAR, (
        "`new_world()` 的缺省必须是 regular —— M1 夹具与单点对账都用它"
    )
    world.quarter = 1
    assert POL.ensure_season_policy(world).policy_id == POL.REGULAR


def test_new_game_world_is_random():
    """★ 正式开局走 `new_game_world()` → 每季抽（基线 §11.9.3 的玩法成立）。"""
    cfg = load_config()
    world = POL.new_game_world(cfg)
    assert POL.policy_mode(world) == POL.MODE_RANDOM, "正式开局必须是随机模式"

    seen = set()
    for q in range(1, 21):
        world.quarter = q
        seen.add(POL.ensure_season_policy(world).policy_id)
    assert len(seen) >= 2, (
        f"20 季只抽到 {sorted(seen)} —— 随机模式没生效？"
    )
    assert len(world.policies) == 20, "20 季应有 20 条"


# ══════════════════════════════════════════════════════════════════
# 二、乘数语义（定点，绕开抽签）
# ══════════════════════════════════════════════════════════════════


def test_regular_environment_has_zero_modifiers():
    """★ 常规环境 = **无额外修正** —— 三个乘数必须**全 1**。

    ⚠️ 本轮真踩过这个坑：`PolicyDef.wage_multiplier` 的字段缺省曾是 1.1，
       于是"无修正"的常规环境悄悄给工资加了 10%，
       M1 甲区期末现金从 1031.68 变成 1028.44。
       所以这里**三处都断言**，不只看效率。
    """
    cfg = load_config()
    world = world_with(POL.REGULAR, quarter=1, cfg=cfg)
    assert POL.efficiency_multiplier(world, True) == 1.0, "常规环境不该改效率"
    assert POL.wage_multiplier(world, True) == 1.0, "常规环境不该改工资"
    assert POL.wage_multiplier(world, False) == 1.0, "常规环境不该改服务/研究工资"
    assert POL.operating_multiplier(world, True) == 1.0, "常规环境不该改经营费"


def test_production_incentive_multipliers():
    """生产激励：效率 ×1.10、工资 ×1.15。"""
    cfg = load_config()
    world = world_with(POL.PRODUCTION_INCENTIVE, cfg=cfg)
    assert abs(POL.efficiency_multiplier(world, True) - 1.10) < TOL
    assert abs(POL.wage_multiplier(world, True) - 1.15) < TOL
    assert abs(POL.operating_multiplier(world, True) - 1.0) < TOL, "生产激励不该改经营费"


def test_lean_operation_multipliers():
    """精益运营：效率 ×0.95、经营费 ×0.90，**工资不减**。"""
    cfg = load_config()
    world = world_with(POL.LEAN_OPERATION, cfg=cfg)
    assert abs(POL.efficiency_multiplier(world, True) - 0.95) < TOL
    assert abs(POL.operating_multiplier(world, True) - 0.90) < TOL
    assert abs(POL.wage_multiplier(world, True) - 1.0) < TOL, (
        "★ 精益运营**工资不减**（基线平衡提示原文：产量降、原料降、工资不减）"
    )


def test_livelihood_first_multipliers():
    """民生优先：**全部运行地块**工资 ×1.10，不改效率/经营费。"""
    cfg = load_config()
    world = world_with(POL.LIVELIHOOD_FIRST, cfg=cfg)
    assert abs(POL.wage_multiplier(world, True) - 1.10) < TOL, "商品生产地块工资 ×1.10"
    assert abs(POL.wage_multiplier(world, False) - 1.10) < TOL, (
        "★ 民生优先是「**全部运行地块**」—— 服务/研究也要吃工资乘数"
    )
    assert POL.efficiency_multiplier(world, True) == 1.0, "民生优先不改效率"
    assert POL.operating_multiplier(world, True) == 1.0, "民生优先不改经营费"


def test_efficiency_multiplier_excludes_service_and_research():
    """★ 效率乘数**不含**研究与本地服务 `J`（基线作用范围原文）。

    即：`is_commodity_plot=False` 时，效率/经营费乘数恒为 1。
    """
    cfg = load_config()
    for pid in POL.POLICY_IDS:
        world = world_with(pid, cfg=cfg)
        assert POL.efficiency_multiplier(world, False) == 1.0, (
            f"{pid}: 服务/研究不该吃效率乘数"
        )
        assert POL.operating_multiplier(world, False) == 1.0, (
            f"{pid}: 服务/研究不该吃经营费乘数"
        )


def test_service_output_is_not_commodity():
    """服务配方的 `output` 为空 → `is_commodity_plot` 判 False。"""
    cfg = load_config()
    for ptype in (PlotType.SERVICE, PlotType.RESEARCH):
        try:
            rec = cfg.recipe(ptype, 1)
        except Exception:
            continue          # 该类型没配方就跳过
        assert not POL.is_commodity_plot(rec.output, cfg), (
            f"{ptype} 不该被当成商品生产地块（output={rec.output!r}）"
        )
    # 反面：资源/工业有可交易产出 → True
    assert POL.is_commodity_plot(cfg.recipe(PlotType.RESOURCE, 1).output, cfg)
    assert POL.is_commodity_plot(cfg.recipe(PlotType.INDUSTRY, 1, "G").output, cfg)


def test_policy_does_not_touch_adjacency_components():
    """★ 效率乘数**不回写**邻接分量、**不改 40% 上限**（基线原文）。

    做法：同一块地，分别在常规/精益运营下算 `_run`，
    断言 `EfficiencyBreakdown` 的四个分量**逐位相同**，只有 `efficiency` 变。
    """
    cfg = load_config()

    def run(mode: str):
        st = make_state(cfg, mature("0,0", PlotType.RESOURCE, 1), mode=mode)
        pipeline.advance(st, {"p1": plan_for("p1", "0,0")}, cfg)
        return st.raw_reports["p1"].efficiency["0,0"]

    base = run(POL.MODE_REGULAR)

    # 手动造一个"精益运营"的局：绕开抽签，直接把世界设成 lean
    st = make_state(cfg, mature("0,0", PlotType.RESOURCE, 1), mode=POL.MODE_REGULAR)
    st.world.policies.clear()
    st.world.policies.append(_policy_row(POL.LEAN_OPERATION, 1))
    pipeline.advance(st, {"p1": plan_for("p1", "0,0")}, cfg)
    lean = st.raw_reports["p1"].efficiency["0,0"]

    for key in ("adjacency", "happiness", "ramp_up", "research_multiplier",
                "linked_neighbors", "adjacency_bonus", "adjacency_capped",
                "service_rate"):
        assert base[key] == lean[key], (
            f"★ 政策**不得回写**分量 {key}：常规={base[key]} 精益={lean[key]}"
        )
    assert abs(lean["efficiency"] - base["efficiency"] * 0.95) < 1e-9, (
        f"E_effective 应为 E_base × 0.95："
        f"{base['efficiency']} → {lean['efficiency']}"
    )


def _policy_row(pid: str, quarter: int):
    from engine.models import Policy

    return Policy(
        policy_id=pid, policy_type=POL.POLICY_TYPE,
        effective_quarter=quarter, duration=POL.POLICY_DURATION,
        target_goods="ALL", target_player="ALL",
        announcement=POL.def_for(pid).announcement,
    )


# ══════════════════════════════════════════════════════════════════
# 三、端到端：政策真的接在效率/工资/经营费上
# ══════════════════════════════════════════════════════════════════


def test_production_incentive_actually_applies():
    """★ **生产激励**的效率 ×1.10 与工资 ×1.15 **真生效**（端到端）。

    测法：同一块资源地块（F）、同样 `u=1`、同样 `H`，
    分别在「常规环境」与「生产激励」下跑完整 `pipeline.advance`：

        产量          ×1.10
        效率 E        ×1.10
        工资          ×1.15
        经营费        ×1.00（生产激励不动经营费，但可变部分跟着 E 走 → ×1.10）
        劳动          **不变**（劳动不乘 E，也不吃政策效率乘数）

    经营费 = 0.7c + 0.3c·u·E → 生产激励下 E 大了，可变部分也大。
    这块地与邻接无关（甲区单块地），所以固定部分 0.7c 原样。
    """
    cfg = load_config()
    plot_id = "0,0"

    def run(policy_id: str):
        st = make_state(cfg, mature(plot_id, PlotType.RESOURCE, 1), mode=POL.MODE_REGULAR)
        if policy_id != POL.REGULAR:
            st.world.policies.clear()
            st.world.policies.append(_policy_row(policy_id, 1))
        pipeline.advance(st, {"p1": plan_for("p1", plot_id)}, cfg)
        return st.raw_reports["p1"]

    base = run(POL.REGULAR).production[0]
    inc = run(POL.PRODUCTION_INCENTIVE).production[0]

    # 基线（常规环境）自检：M1 甲区的数
    assert abs(base.output_qty - 4.0) < 1e-9, f"常规环境产量应为 4，实得 {base.output_qty}"
    assert abs(base.efficiency - 1.0) < 1e-9
    assert abs(base.wage - 36.0) < 1e-9, f"常规环境工资应为 36，实得 {base.wage}"
    assert abs(base.operating_cost - 30.0) < 1e-9

    # ★ 效率 ×1.10
    assert abs(inc.efficiency - base.efficiency * 1.10) < 1e-9, (
        f"★ 效率应 ×1.10：{base.efficiency} → {inc.efficiency}"
    )
    # ★ 产量跟着 E 走（产量 = e·u·E）
    assert abs(inc.output_qty - base.output_qty * 1.10) < 1e-9, (
        f"★ 产量应 ×1.10：{base.output_qty} → {inc.output_qty}"
    )
    # ★ 工资 ×1.15
    assert abs(inc.wage - base.wage * 1.15) < 1e-9, (
        f"★ 工资应 ×1.15：{base.wage} → {inc.wage}（实得 {inc.wage}）"
    )
    # ★ 劳动**不变**（劳动 = d×u，不乘 E、不吃政策）
    assert abs(inc.labor - base.labor) < 1e-9, (
        f"★ 劳动不该变：{base.labor} → {inc.labor}"
    )
    # 经营费：λ 固定部分不变 + 可变部分随 E ×1.10
    c = cfg.recipe(PlotType.RESOURCE, 1).c
    fixed = cfg.production_operating_fixed_share * c
    expect_op = fixed + (base.operating_cost - fixed) * 1.10
    assert abs(inc.operating_cost - expect_op) < 1e-9, (
        f"经营费应为 {expect_op}（固定部分不变、可变部分 ×1.10），实得 {inc.operating_cost}"
    )


def test_lean_operation_actually_applies():
    """精益运营：效率 ×0.95、经营费 ×0.90，**工资不减**。"""
    cfg = load_config()
    plot_id = "0,0"

    def run(policy_id: str):
        st = make_state(cfg, mature(plot_id, PlotType.RESOURCE, 1), mode=POL.MODE_REGULAR)
        if policy_id != POL.REGULAR:
            st.world.policies.clear()
            st.world.policies.append(_policy_row(policy_id, 1))
        pipeline.advance(st, {"p1": plan_for("p1", plot_id)}, cfg)
        return st.raw_reports["p1"].production[0]

    base = run(POL.REGULAR)
    lean = run(POL.LEAN_OPERATION)

    assert abs(lean.efficiency - base.efficiency * 0.95) < 1e-9, "效率 ×0.95"
    assert abs(lean.output_qty - base.output_qty * 0.95) < 1e-9, "产量跟着 E 降"
    assert abs(lean.wage - base.wage) < 1e-9, (
        f"★ 精益运营**工资不减**：{base.wage} → {lean.wage}"
    )
    # 经营费 = (0.7c + 0.3c·u·E_eff) × 0.90
    c = cfg.recipe(PlotType.RESOURCE, 1).c
    raw = (cfg.production_operating_fixed_share * c
           + cfg.production_operating_variable_share * c * 1.0 * (base.efficiency * 0.95))
    assert abs(lean.operating_cost - raw * 0.90) < 1e-9, (
        f"经营费应为 (用 E_effective 算的结果) × 0.90 = {raw * 0.90}，"
        f"实得 {lean.operating_cost}"
    )


def test_livelihood_first_actually_applies():
    """民生优先：工资 ×1.10，效率/经营费不变。"""
    cfg = load_config()
    plot_id = "0,0"

    def run(policy_id: str):
        st = make_state(cfg, mature(plot_id, PlotType.RESOURCE, 1), mode=POL.MODE_REGULAR)
        if policy_id != POL.REGULAR:
            st.world.policies.clear()
            st.world.policies.append(_policy_row(policy_id, 1))
        pipeline.advance(st, {"p1": plan_for("p1", plot_id)}, cfg)
        return st.raw_reports["p1"].production[0]

    base = run(POL.REGULAR)
    live = run(POL.LIVELIHOOD_FIRST)

    assert abs(live.wage - base.wage * 1.10) < 1e-9, (
        f"★ 工资应 ×1.10：{base.wage} → {live.wage}"
    )
    assert abs(live.efficiency - base.efficiency) < 1e-9, "民生优先不改效率"
    assert abs(live.operating_cost - base.operating_cost) < 1e-9, "民生优先不改经营费"
    assert abs(live.output_qty - base.output_qty) < 1e-9, "产量不该变"


def test_idle_plot_gets_no_wage_from_policy():
    """★ **停工且按原规则不发工资的地块，不因政策凭空产生工资**（红线）。

    把`u=0`（不申报开工）→ 劳动 0、工资 0；
    即使当季是「民生优先（工资 ×1.10）」，工资仍是 **0**。
    """
    cfg = load_config()
    st = make_state(cfg, mature("0,0", PlotType.RESOURCE, 1), mode=POL.MODE_REGULAR)
    st.world.policies.clear()
    st.world.policies.append(_policy_row(POL.LIVELIHOOD_FIRST, 1))

    pipeline.advance(st, {"p1": plan_for("p1", "0,0", u=0.0)}, cfg)
    rep = st.raw_reports["p1"]

    assert rep.production == [], "u=0 的地块不该出现在生产结果里"
    assert abs(rep.wages) < 1e-12, (
        f"★ 停工地块 + 民生优先 → 工资必须是 0，实得 {rep.wages}"
    )
    assert abs(rep.operating) < 1e-12, "停工地块经营费也应是 0"


def test_half_shift_wage_scales_with_policy():
    """`u=0.5`：工资本来就是半份，政策乘数作用在**折后**那份上。"""
    cfg = load_config()

    def run(policy_id: str):
        st = make_state(cfg, mature("0,0", PlotType.RESOURCE, 1), mode=POL.MODE_REGULAR)
        if policy_id != POL.REGULAR:
            st.world.policies.clear()
            st.world.policies.append(_policy_row(policy_id, 1))
        pipeline.advance(st, {"p1": plan_for("p1", "0,0", u=0.5)}, cfg)
        return st.raw_reports["p1"].production[0]

    base = run(POL.REGULAR)
    inc = run(POL.PRODUCTION_INCENTIVE)

    assert abs(base.labor - 1.5) < 1e-9, f"u=0.5 → 劳动 1.5，实得 {base.labor}"
    assert abs(inc.wage - base.wage * 1.15) < 1e-9
    assert abs(inc.labor - base.labor) < 1e-9, "劳动不乘政策"


def test_report_carries_policy():
    """报告里带上本季政策（前端"弹窗告知"读它）。"""
    cfg = load_config()
    st = make_state(cfg, mature("0,0", PlotType.RESOURCE, 1), mode=POL.MODE_REGULAR)
    st.world.policies.clear()
    st.world.policies.append(_policy_row(POL.PRODUCTION_INCENTIVE, 1))

    pipeline.advance(st, {"p1": plan_for("p1", "0,0")}, cfg)
    rep = st.raw_reports["p1"]

    assert rep.policy is not None, "报告应带本季政策"
    assert rep.policy["policy_id"] == POL.PRODUCTION_INCENTIVE
    assert abs(rep.policy["multipliers_applied"]["efficiency"] - 1.10) < 1e-9
    assert abs(rep.policy["multipliers_applied"]["wage_commodity"] - 1.15) < 1e-9
    # 也要能序列化进 /state
    assert st.report["p1"]["policy"]["policy_id"] == POL.PRODUCTION_INCENTIVE


def test_preview_replays_same_policy_as_advance():
    """★ `preview()` 必须重放**同一条**政策，否则试算与真结算对不上。

    `preview` = 深拷贝后跑同一份 `_run`；抽签幂等 → 深拷贝里已是同一条。
    """
    cfg = load_config()
    st = make_state(cfg, mature("0,0", PlotType.RESOURCE, 1))
    st.world.quarter = 2

    pv = pipeline.preview(st, {"p1": plan_for("p1", "0,0")}, cfg)
    real = pipeline.advance(st, {"p1": plan_for("p1", "0,0")}, cfg).raw_reports["p1"]

    assert pv.policy["policy_id"] == real.policy["policy_id"], (
        "★ preview 抽到的政策必须与真结算一致"
    )
    assert abs(pv.wages - real.wages) < 1e-9, "试算工资必须等于真结算工资"
    assert abs(pv.production[0].output_qty - real.production[0].output_qty) < 1e-9


def test_pipeline_draws_every_quarter_in_random_mode():
    """端到端：随机模式下连跑 5 季，每季都有恰好一条政策。"""
    cfg = load_config()
    st = make_state(cfg, mature("0,0", PlotType.RESOURCE, 1))
    POL.set_policy_mode(st.world, POL.MODE_RANDOM)
    POL.set_seed(st.world, 20261016)

    for q in range(1, 6):
        assert st.world.quarter == q
        pipeline.advance(st, {"p1": plan_for("p1", "0,0")}, cfg)
        # advance 里 quarter += 1；检查刚才那一季抽到了政策
        rows = [p for p in st.world.policies if p.effective_quarter == q]
        assert len(rows) == 1, f"第 {q} 季应有恰好 1 条政策，实得 {len(rows)}"

    assert len(st.world.policies) == 5, f"5 季应 5 条，实得 {len(st.world.policies)}"


# ══════════════════════════════════════════════════════════════════
# 四、配置覆盖
# ══════════════════════════════════════════════════════════════════


def test_config_multipliers_match_baseline():
    """★ `config.json` 的 `policies.multipliers` 必须与基线 §11.9.3 一致。

    这条防的是"配置把规则偷偷改了"。
    """
    cfg = load_config()
    POL.reset_table()
    POL.apply_multiplier_overrides(cfg.policy_multipliers)

    expect = {
        POL.PRODUCTION_INCENTIVE: {"efficiency_multiplier": 1.10, "wage_multiplier": 1.15},
        POL.LEAN_OPERATION: {"efficiency_multiplier": 0.95, "operating_multiplier": 0.90},
        POL.LIVELIHOOD_FIRST: {"wage_multiplier": 1.10},
    }
    for pid, fields in expect.items():
        d = POL.def_for(pid)
        for fname, want in fields.items():
            got = getattr(d, fname)
            assert abs(got - want) < TOL, (
                f"config 里 {pid}.{fname} = {got}，基线 §11.9.3 写 {want}"
            )
    # 常规环境永远是零修正
    r = POL.def_for(POL.REGULAR)
    assert (r.efficiency_multiplier, r.wage_multiplier, r.operating_multiplier) == (1.0, 1.0, 1.0)


def test_config_rejects_unknown_policy():
    """配置里塞一个不存在的政策 → **报错**，不静默忽略。"""
    raised = False
    try:
        POL.apply_multiplier_overrides({"not_a_policy": {"wage_multiplier": 2.0}})
    except KeyError:
        raised = True
    assert raised, "未知政策必须报错（§11.9.3 的四项是规则，不能在配置里增删）"


def test_config_mode_present_and_random():
    """`config.json` 里正式对局是随机模式。"""
    cfg = load_config()
    assert cfg.policy_mode == POL.MODE_RANDOM, (
        f"config 的 policies.mode 应是 'random'（正式玩法每季抽），实得 {cfg.policy_mode!r}"
    )


# ══════════════════════════════════════════════════════════════════
# 命令行运行
# ══════════════════════════════════════════════════════════════════


def _main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    passed, failed = 0, []

    print("=" * 70)
    print("政策系统自检（《规则基线》§11.9.3）")
    print("=" * 70)
    for name, fn in tests:
        try:
            fn()
            passed += 1
            print(f"  PASS  {name}")
        except Exception as exc:                       # noqa: BLE001
            failed.append((name, exc))
            print(f"  FAIL  {name}\n          {type(exc).__name__}: {exc}")

    print("-" * 70)
    print(f"passed {passed} / {len(tests)}, failed {len(failed)}")
    if failed:
        print("\n失败明细：")
        for name, exc in failed:
            print(f"  · {name}: {exc}")
    print("=" * 70)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())
