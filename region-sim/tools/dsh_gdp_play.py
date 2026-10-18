# -*- coding: utf-8 -*-
"""dsh_gdp_play —— 离线智能玩家：以累计 GDP (value_added) 最大化为目标玩存档 8550。

设计（2026-10-18）：
  · 备份 .scratch/saves_dsh_gdp/8550_abs.json  →  现场 8550.json（隔离目录，
    不碰 saves/ 与 saves-live/）。
  · Q3–Q20 每季：Beam Search + preview 引擎试算选计划，ladu commit_end
    （一个候选=当季的 build/upgrade 指令序列+开工 set）。
  · 目标值 J = 当折现累计 GDP + 终端启发值（避免"种地躺平"短视）。
  · 完全复用正在跑的端口 8550？不 —— 离线直进，不走 HTTP。

学习流动：build_plan(...) 构造 Plan → validate → session.simulate/preview…
     最终 commit_end 提交+结算，存档落 8550.json（可在 8312 上复盘）。
"""

from __future__ import annotations

import copy
import json
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = ROOT / ".scratch"
CFG_PATH = SCRATCH / "config_dsh_gdp.json"
ABS_SAVE = SCRATCH / "saves_dsh_gdp" / "8550_abs.json"
LIVE_SAVE = SCRATCH / "saves_dsh_gdp" / "8550.json"

sys.path.insert(0, str(ROOT))

from engine.loader import load_config  # noqa: E402
from engine.models import (  # noqa: E402
    BuildOrder, Plan, PlotPlan, PlotType,
)
from server.session import GameSession  # noqa: E402
from storage import JsonFileStorage  # noqa: E402

PLAYER = "p1"
TOTAL_Q = 20

# ── 分区抽象布局 ──────────────────────────────────────────────
AGRI = ["4,3", "3,4", "3,5", "3,3"]       # 农业补位（产品 A）
IND_PAR = [(2, 3), (2, 5), (1, 4), (2, 2)]  # (G产出, F产出) 成对
CLUSTER = (4, 6)                           # 中子（geometric center），先被占→延 8 邻
UPGRADE_TARGET_IND = [                     # 机械路线 H-A 路线；首选能达成的高 PLAYER 产值
    ("2,3", "H-A"),
]
UPGRADE_AGRI = ["4,4"]                     # 农业升级目标（A2）

FOOD_KEYS = ("A", "B", "C", "D")

# 束搜索参数
TOP_M = 6          # preview 后保留的深度
BEAM_K = 8         # 展开的候选数
HEUR_PER_COMMODITY = 120.0   # 每满配 commodity 地块的终端折价（GDP 等价）
HEUR_UPGRADE_IND = 300.0     # 工业升级完成的终端折价
HEUR_AGRI_MULT = 30.0        # 每块满配农业的终端折价
HEUR_POP_FACTOR = 0.0        # population 边际 GDP 因子（高效时约 0）

PATH_LEVELLEMENT = None  # 占位防 linters


# ── 存档/会话工具 ─────────────────────────────────────────────

def fresh_session() -> GameSession:
    """从绝对备份 8550_abs.json 复原一个全新会话（Q3 collecting）。"""
    shutil.copyfile(ABS_SAVE, LIVE_SAVE)
    cfg = load_config(CFG_PATH)
    storage = JsonFileStorage(LIVE_SAVE.parent)
    return GameSession.load(cfg, storage, "8550")


def gs_of(sess: GameSession):
    return sess.state


def player(sess: GameSession):
    return sess.state.players[PLAYER]


def border_cells(sess: GameSession) -> list[str]:
    """6,6 延 8 邻的可用空位（当中子被占时的溢出区）。"""
    world = sess.state.world
    taken = set(player(sess).plots)
    out = []
    for cid in world.neighbor_ids("6,6"):
        if cid not in taken:
            out.append(cid)
    return out


def cluster_free_cell(sess: GameSession) -> str | None:
    """返回 (6,6) 或其 8-邻空位；都占用则 None。"""
    taken = set(player(sess).plots)
    if "6,6" not in taken:
        return "6,6"
    cells = border_cells(sess)
    return cells[0] if cells else None


# ── 食物申报（自底向上手动配平；正好等于需求，回避丢弃）───────

def food_need(sess: GameSession, extra_labor: float) -> dict[str, float]:
    """按(含计划新增岗位后的)人口推 A/B/C/D 申报量。

    达优秀：A=0.4F、B=0.2F、C=0.1F、D=0.02F（F=F_基准+新增）。
    实际不足：A=0.4F、B 用实际可负担量补齐 0.6F，C/D 放弃。
    """
    pop_data = None
    try:
        pop_data = sess.state  # 经 report.populate_food 所需字段量已在 baseline_food
    except Exception:
        pass
    # 直接用引擎内嵌口径：F_基准 从 Session 的 /state 结构太贵；
    # 这里从 forecast 的 trick：population = L_food*2，F_基准=(N/5)*m。
    # L_food = sum(d for c 含服务)（公式见 population.py）。
    from engine.population import compute_population  # 延迟导入，避免循环
    gs = sess.state
    existing_pop = compute_population(gs, PLAYER, sess.cfg)
    N_extra = extra_labor * cfg_pop_r(sess.cfg)
    m_extra = extra_avg_level_extra(sess, extra_labor)
    N = existing_pop.population + N_extra
    m = existing_pop.level_multiplier + m_extra
    F_base = (N / 5.0) * m
    Fb = F_base
    # 尝试优秀
    food = {"A": 0.4 * Fb, "B": 0.2 * Fb, "C": 0.1 * Fb, "D": 0.02 * Fb}
    return food, Fb


def cfg_pop_r(cfg) -> float:
    return float(cfg.population.r)


def extra_avg_level_extra(sess: GameSession, extra_L: float) -> float:
    """近似：新增岗位不改变平均等级 m（全部 1 级）→ 0。

    若升级了地块则 m 会变化，但我们近似忽略（影响小）。
    """
    return 0.0


def affordable_food(sess: GameSession, food: dict[str, float],
                      cash_budget: float, Fb: float) -> dict[str, float]:
    """按上季价估算食物成本，超预算则降档。"""
    prices = last_prices(sess)
    def cost(f: dict[str, float]) -> float:
        return sum(prices.get(g, 0.0) * q for g, q in f.items())
    if cost(food) <= cash_budget:
        return food
    # 降档：保 A+B 达基准（A=0.4F、B=0.2F），C/D 归零
    food = {k: (v if k in ("A", "B") else 0.0) for k, v in food.items()}
    if cost(food) <= cash_budget:
        return food
    # 再降：A=0.4F，B 用剩余预算补
    a_q = 0.4 * Fb
    rem = cash_budget - prices.get("A", 10.0) * a_q
    b_q = max(0.0, rem / max(prices.get("B", 7.0), 1e-6))
    return {"A": a_q, "B": b_q, "C": 0.0, "D": 0.0}


def last_prices(sess: GameSession) -> dict[str, float]:
    ph = sess.state.world.price_history
    out = {}
    for g, hist in ph.items():
        if hist:
            out[g] = hist[-1]
    return out


# ── 候选动作空间 ─────────────────────────────────────────────

@dataclass(frozen=True)
class Cand:
    key: str
    builds: tuple = ()      # tuple[(plot_id, plot_type_str, target_level, product)]
    halt: tuple = ()        # 停工地块 id
    desc: str = ""


def service_chain(actives, level: int) -> tuple | None:
    """从当前服务等级建到目标 level 的服务链（6,6 或其邻位）。"""
    cur = service_level(actives)
    if cur is None or cur >= level:
        return None
    # service 只能逐级升
    cell = cluster_free_cell_global.get("cell")
    if cell is None:
        return None
    builds = []
    for lv in range(cur + 1, level + 1):
        builds.append((cell, "service", lv, None))
    return tuple(builds)


def service_level(actives) -> int | None:
    for p in actives.values():
        pass
    return None


# 全局占位（service_chain 需要的 cell 由 generate 注入）
cluster_free_cell_global: dict = {"cell": None}


def gen_candidates(sess: GameSession) -> list[Cand]:
    """枚举本季可行动作（build/upgrade/停机）。"""
    taken = set(player(sess).plots)
    cluster_cell = cluster_free_cell(sess)
    cluster_free_cell_global["cell"] = cluster_cell
    ind_pairs = [p for p in IND_PAR if str(p[0]).replace(" ", "") not in taken
                 and str(p[1]).replace(" ", "") not in taken]
    # 用坐标字符串
    def cs(t): return f"{t[0]},{t[1]}"
    ind_pairs = [p for p in IND_PAR if cs(p[0]) not in taken and cs(p[1]) not in taken]
    agri_free = [a for a in AGRI if a not in taken]
    has_g = any(p.plot_type is PlotType.INDUSTRY and p.product == "G"
                and p.status.value == "running" for p in player(sess).plots.values())
    has_core_service = any(
        p.plot_type is PlotType.SERVICE and p.plot_id == cluster_cell
        for p in player(sess).plots.values()
    )
    core_service_level = next(
        (p.level for p in player(sess).plots.values()
         if p.plot_type is PlotType.SERVICE and p.plot_id in
         {cluster_cell, *border_cells(sess)}),
        None,
    )

    cands: list[Cand] = []

    # 1. 停产低效（保持 H 可从其他地维持时）
    if not taken & set(border_cells(sess)):  # 中子还没被服务占
        cands.append(Cand("gal0", halt=("4,4", "5,4"), desc="停 4,4(A)+5,4(老服务)",
                          builds=()))

    # 2. 工业对
    for i in range(0, min(2, len(ind_pairs))):
        pair = ind_pairs[i]
        gcell, fcell = cs(pair[0]), cs(pair[1])
        cands.append(Cand(f"pair{i}",
                          builds=((gcell, "industry", 1, None),
                                   (fcell, "resource", 1, None)),
                          desc=f"G/F 对 {gcell}+{fcell}"))

    # 3. 农业补位
    if agri_free:
        # 优先级按与现有农业邻接
        cell = agri_free[0]
        cands.append(Cand("agri", builds=((cell, "agriculture", 1, "A"),),
                          desc=f"A 补位 {cell}"))

    # 4. 服务链（6,6）
    if cluster_cell is not None:
        cur = core_service_level or 0
        if cur == 0:
            # 新建服务 1 或直升 3（资金够则 3）
            b1 = [(cluster_cell, "service", 1, None)]
            b3 = b1 + [(cluster_cell, "service", 2, None),
                        (cluster_cell, "service", 3, None)]
            cands.append(Cand("svc1", builds=tuple(b1), desc=f"服务1 {cluster_cell}"))
            cands.append(Cand("svc3", builds=tuple(b3), desc=f"服务3 {cluster_cell}"))
        elif cur < 3:
            nxt = [(cluster_cell, "service", lv, None) for lv in range(cur + 1, 4)]
            cands.append(Cand(f"svc{cur}to3", builds=tuple(nxt),
                              desc=f"服务 {cur}→3"))

    # 5. 工业升级机械 H-A（在 G 地块上到 2 级）
    g_plots = [p for p in player(sess).plots.values()
               if p.plot_type is PlotType.INDUSTRY and p.level == 1
               and p.status.value == "running" and p.product == "G"]
    if g_plots:
        gp = g_plots[0]
        cands.append(Cand("upg_ind_HA",
                          builds=((gp.plot_id, "industry", 2, "H-A"),),
                          desc=f"工业 1→2 H-A @{gp.plot_id}"))

    # 6. 农业升级 A→A2（选中一个 A 地块）
    a1_plots = [p for p in player(sess).plots.values()
                if p.plot_type is PlotType.AGRICULTURE and p.level == 1
                and p.status.value == "running" and p.product == "A"]
    if a1_plots:
        ap = sorted(a1_plots, key=lambda x: x.plot_id)[0]
        cands.append(Cand("upg_agri",
                          builds=((ap.plot_id, "agriculture", 2, None),),
                          desc=f"农业 1→2 @{ap.plot_id}"))

    # 7. 纯开工（保底候选：不建不升）
    cands.append(Cand("run", builds=(), desc="仅满开工"))

    return cands


# ── 构造 Plan ────────────────────────────────────────────────

def plan_from_cand(sess: GameSession, cand: Cand) -> Plan:
    plots = player(sess).plots
    plot_plans = {}
    extra_labor = 0.0
    for pid, p in plots.items():
        active = p.status.value == "running" and pid not in cand.halt
        plot_plans[pid] = PlotPlan(plot_id=pid, active=active, u=1.0)
    builds = [BuildOrder(plot_id=b[0], plot_type=PlotType(b[1]),
                         target_level=b[2], product=b[3]) for b in cand.builds]

    # 新增岗位估计 → 口粮申报
    for b in cand.builds:
        # 新增/升级带来的 d 增量
        extra_labor += d_delta(sess.cfg, b)

    food, Fb = food_need(sess, extra_labor)
    # 口粮预算：留 5%现金作为安全边际；总预算 = 现金*0.95 − 预估工程费
    cash = player(sess).cash
    eng_est = build_cash_est(sess, builds)
    budget = max(0.0, (cash - eng_est) * 0.95)
    food = affordable_food(sess, food, budget, Fb)
    food = {g: q for g, q in food.items() if q > 1e-9}

    return Plan(player_id=PLAYER, plot_plans=plot_plans,
                purchases={}, food=food, builds=builds, contracts=[])


def d_delta(cfg, build_tuple) -> float:
    """新增岗位（d 差值）。"""
    pid, ptype, target, _ = build_tuple if isinstance(build_tuple, tuple) else build_tuple.plot_id, build_tuple.plot_type.value, build_tuple.target_level, build_tuple.product
    if target == 1:
        return d_of(cfg, ptype, 1)
    # 升级：d 新 − d 旧
    return max(0.0, d_of(cfg, ptype, target) - d_of(cfg, ptype, target - 1))


def d_of(cfg, ptype: str, level: int) -> float:
    try:
        recs = cfg.recipes.recipes_for(PlotType(ptype))
        # recipes 对 agriculture 按 product 分；取 A 作为默认
        if ptype == "agriculture":
            return float(recs["A"][str(level)]["d"])
        if ptype == "resource":
            return float(recs["F"][str(level)]["d"])
        if ptype == "service":
            return float(recs[str(level)]["d"])
        if ptype == "industry":
            if level == 1:
                return float(recs["G_L1"]["d"])
            # 工业 2/3 级 d 在路线层；机械 H-A_L2=4
            return float(recs.get(f"H-A_L{level}", recs.get(f"I_L{level}", {"d": 0}))["d"])
    except Exception:
        return 0.0
    return 0.0


def build_cash_est(sess: GameSession, builds: list[BuildOrder]) -> float:
    """粗估工程现金费（含开工折扣）。"""
    from engine.construction import find_work_for_build
    pl = player(sess)
    total = 0.0
    used = dict(pl.opening_quota_used) if pl.opening_quota_used else {}
    for b in builds:
        target_plot = pl.plots.get(b.plot_id)
        try:
            w = find_work_for_build(sess.cfg, target_plot, b)
            fee = float(w.cash)
            # 开局折扣（新建一级）
            if target_plot is None and b.target_level == 1:
                pt = b.plot_type.value
                quota = sess.cfg.opening_quota(pt) if hasattr(sess.cfg, 'opening_quota') else 3
                if used.get(pt, 0) < quota:
                    fee *= float(sess.cfg.opening_discount_rate) if hasattr(sess.cfg, 'opening_discount_rate') else 0.5
                    used[pt] = used.get(pt, 0) + 1
            total += fee
        except Exception:
            total += 200.0  # 保守估
    return total


# ── 评估与模拟 ───────────────────────────────────────────────

def heuristic(sess: GameSession) -> float:
    """终端启发值（把"建成的产能"折算成 GDP 等价）。"""
    pl = player(sess)
    score = 0.0
    for p in pl.plots.values():
        if p.status.value != "running":
            continue
        if p.plot_type.value in ("agriculture", "resource", "industry"):
            if p.product in ("G", "F", "A"):
                score += HEUR_PER_COMMODITY * (1 + 0.4 * (p.level - 1))
            elif p.product and p.product.startswith("H-"):
                score += HEUR_UPGRADE_IND
        elif p.plot_type.value == "service":
            score += HEUR_AGRI_MULT * p.level  # 服务等级作为 agronomy 乘数（粗略）
    return score


def simulate_cand(sess: GameSession, cand: Cand):
    """在 sess 的深拷贝上按 cand 提交并 preview。"""
    sess2 = copy.deepcopy(sess)
    plan = plan_from_cand(sess2, cand)
    try:
        issues = sess2.validate_only(plan, PLAYER)
    except Exception as e:
        return None, f"validate_raise:{e}", issues if 'issues' in dir() else []
    issues = [i for i in issues if i.code != "insufficient_cash_estimate"]
    if issues:
        return None, f"validate:{issues[0].code}", issues
    try:
        rep = sess2.simulate(plan)
    except Exception as e:
        return None, f"simulate:{e}", []
    return rep, None, []


# ── Beam Search ──────────────────────────────────────────────

@dataclass
class Node:
    sess: GameSession
    gdp_cum: float = 0.0
    path: list = field(default_factory=list)


def beam(start_sess: GameSession, depth: int = 2,
            beam_width: int = 6, branch: int = 6):
    """从 start_sess（不提交）向 depth 层搜索 (cand_1..cand_depth)，返回最优首步。"""
    root = Node(sess=start_sess)
    frontier = [root]
    q0 = start_sess.state.world.quarter
    for d in range(depth):
        q = q0 + d
        if q > TOTAL_Q:
            break
        nxt: list[Node] = []
        for node in frontier:
            cands = gen_candidates(node.sess)
            scored = []
            for c in cands:
                rep, err, _ = simulate_cand(node.sess, c)
                if rep is None:
                    continue
                gdp_q = rep.value_added
                # 不推进：只评分。本轮 GDP 用 rep.value_added（确定性）
                scored.append((gdp_q, c, rep))
            scored.sort(key=lambda x: -x[0])
            for gdp_q, c, rep in scored[:branch]:
                nxt.append(Node(sess=node.sess,
                                gdp_cum=node.gdp_cum + gdp_q,
                                path=node.path + [(q, c, gdp_q)]))
        # 按 gdp_cum + 启发（浅层看启发，深层看累计）
        nxt.sort(key=lambda n: -(n.gdp_cum + heuristic(player_proxy(n.sess)) * 0.1))
        frontier = nxt[:beam_width]
        if not frontier:
            break
    # 选择首步
    best = max(frontier, key=lambda n: n.gdp_cum)
    if not best.path:
        return None
    return best.path[0][1], best  # (cand, best_node)


def player_proxy(sess):
    return sess


# ── 主循环 ───────────────────────────────────────────────────

def main():
    t0 = time.time()
    log_lines = []
    sess = fresh_session()
    print(f"[init] quarter={sess.state.world.quarter} cash={player(sess).cash:.2f} "
          f"phase={sess.phase.value}")
    total_gdp = 0.0
    gdp_series = []
    commits = []  # 每季提交的描述

    while sess.state.world.quarter <= TOTAL_Q and sess.phase.value != "finished":
        q = sess.state.world.quarter
        pl = player(sess)
        depth = min(3 if q <= 8 else (2 if q <= 14 else 1), TOTAL_Q - q + 1)
        result = beam(sess, depth=depth, beam_width=6, branch=min(TOP_M, 6))
        if result is None:
            # 无可行候选：保底 run
            cand = Cand("run", builds=(), desc="run-fallback")
        else:
            cand, best = result
        plan = plan_from_cand(sess, cand)
        # 提交 + 结算
        try:
            sess.submit(PLAYER, plan)
        except Exception as e:
            log_lines.append(f"[Q{q}] SUBMIT_FAIL {e}; fallback run")
            sess = fresh_session_for_fallback(sess)
            cand = Cand("run", builds=(), desc="run-fallback2")
            plan = plan_from_cand(sess, cand)
            try:
                sess.submit(PLAYER, plan)
            except Exception as e2:
                log_lines.append(f"[Q{q}] SUBMIT_FAIL2 {e2}; abort")
                break
        try:
            rep = sess.end_quarter(PLAYER)
        except Exception as e:
            log_lines.append(f"[Q{q}] SETTLE_FAIL {e}; abort")
            break
        gdp_q = rep.value_added if rep else 0.0
        total_gdp += gdp_q
        gdp_series.append(round(gdp_q, 2))
        cash_after = rep.cash_after if rep else player(sess).cash
        commits.append((q, cand.desc, round(gdp_q, 2), round(cash_after, 2)))
        log_lines.append(
            f"[Q{q}] {cand.desc!r} GDP={gdp_q:.2f} cash={cash_after:.2f} "
            f"H={rep.food.happiness:.3f if rep and rep.food else '?'}")
        # 下一季（自动存档）
        try:
            sess.start_next_quarter()
        except Exception as e:
            log_lines.append(f"[Q{q}] NEXT_FAIL {e}; stop")
            break

    print("\n".join(log_lines))
    print(f"\n== 完结 == 总季数={len(gdp_series)} 累计GDP={total_gdp:.2f} "
          f"最终现金={player(sess).cash:.2f}")
    print("GDP 时间序列:", gdp_series)
    for row in commits:
        print("  Q%-2d | %-40s | GDP %8s | cash %10s" % row)
    summary = {
        "total_gdp": total_gdp, "gdp_series": gdp_series,
        "final_cash": player(sess).cash, "commits": [
            {"q": q, "desc": d, "gdp": g, "cash": c} for q, d, g, c in commits
        ],
        "elapsed_s": round(time.time() - t0, 1),
    }
    out = SCRATCH / "dsh_gdp_summary.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nsummary → {out}")
    print(f"存档 → {LIVE_SAVE}")


def fresh_session_for_fallback(sess):
    """提交失败后：同样状态（未提交）即可；无需重载。"""
    return sess


if __name__ == "__main__":
    main()
