# -*- coding: utf-8 -*-
"""GDP 最大化挂机玩家 **v3**（2026-10-18 第八轮参数：服务加强 + 机械/高精尖提价 + 原料降价）。

    v2 → v3 只有**参数**变了（策略旋钮与打分口径一字未改）：

        · 原料降价   C 16→12、D 26→20、F 24→18
        · 机械提价   H-A 64→78、H-F 68→82、H-G 73→88、H-J 64→78、H-*+ → 330 档、I 225→280
        · 服务加强   邻接 5/7.5/10%（★第九轮已回调；第八轮曾 8%/12%/16%）；每邻收入 6/9/12 → **12/20/28**
        · 市场 Q0 不变（G300 / H-*150 / H-*+60 / I60 / C260 / D200 / F300）

    以上全部从 config 读，**工具里不写死**；v3 的默认落盘改成
    `.scratch/gdp_replay_v3.json` + `saves-live/8550c.json`。

═══════════════════════════════════════════════════════════════════
（以下为 v2 原文，机制说明仍然成立）
打分贪心（v2 起）—— v1 只看"这块地造不造得起"，不看它值多少 GDP。

    score = 剩余窗口内的 ΔGDP / 这一条工程的现金成本

ΔGDP 的估算口径（全部来自引擎里的真实公式，不是另写一套）：

    单季增加值 VA(E) = e·E·P0(产出) − Σ input·E·P0(原料)
                       − (0.7c + 0.3c·E)          ← 经营费算中间投入
                       − 口粮成本（GDP 里粮食扣减也算中间投入）
    工资 **不进** GDP（见 engine/finance.py 模块头）
    服务地块：VA = 服务收入 − 经营费（它 0→0，产出侧恒 0）

窗口：新建/升级都在**下一季**才产出（第 6 步动工，第 4 步生产已过），
所以窗口 = Q20 − q 个生产季，前两季吃磨合 0.5 / 0.75，之后 1.0 —— 见 `_window()`。

═══════════════════════════════════════════════════════════════════
新机制下几条必须体现在策略里的东西
═══════════════════════════════════════════════════════════════════

1. **服务**：给八邻格**非服务**运行地块各 +5%/7.5%/10%（1/2/3 级；
   ★第九轮回调，第八轮曾 8%/12%/16%），与关联表加成**相加**后**不封顶**
   （第九轮删掉 40% 上限）；服务自己不吃邻接、服务之间也不加成。
   服务收入 = 相邻运行非服务地块数 × (12/20/28 现金/季)，进 GDP。
   → 一个服务最多服务 8 块，所以**选址＝数邻居**（第九轮起不封顶：服务对
     每个邻居都按自身等级全额生效，不再有"已封顶吃不到余量"的问题）。

2. **市场变深**：G 300 / H-* 150 / H-*+ 60 / I 60 / C 260 / D 200 / F 300。
   ⚠️ 但 **GDP 用固定参考价 P0**（finance.py §7.5），市场深度只影响**现金**，
   不影响 GDP。所以"价格崩了"在这一版的打分里**不扣分**，只通过
   现金约束间接生效 —— 这是引擎口径，不是本工具的简化。

3. **研究线**：工业 1→2 研究 4 季、2→3 I 线 5 季（共 9 季）。
   Q3 开投 → Q7 研究跑一季 → Q12 起产 I（"完工当季不能开工"吃一季）。
   第八轮后 I 的 VA(≈746/季，P0 280) 是 H-G(≈364) 的 2.1 倍，
   所以研究线**值不值**是这一轮要实测的核心问题，
   做成参数 `--research-max` / `--research-from`。

4. **现金硬约束**：单季总扣款 ≤ 期初现金（扣款在销售回款之前）。
   本工具的做法：先用"不带建设"的 preview 量出本季**经营侧要扣多少**，
   剩下的才是建设预算；下完单再真 preview 一次；最后做 1 季前瞻。

═══════════════════════════════════════════════════════════════════
用法
═══════════════════════════════════════════════════════════════════

    .\\.venv\\Scripts\\python.exe -X utf8 tools/gdp_play.py
        主策略 → .scratch/gdp_replay_v3.json + saves-live/8550c.json

    ... --baseline          全躺平基线（不新建不升级，只维持+喂饱）→ 写进同一个 JSON
    ... --debug             打印每个候选的分数与淘汰原因
    ... --research-max 3    最多几块走研究线（默认 6）
    ... --research-from 4   第几季起允许开研究线（默认 3）
    ... --srv-max 99        最多几块服务
    ... --srv-slots 3       每季进候选池的服务地块数（默认 3）
    ... --dump-cands        每季候选全表 dump 进输出 JSON（研究线/资源/农业对比用）
    ... --out-json X        改输出路径
    ... --save-name 8550c   改落盘档号（默认 8550c，绝不覆盖 8550/8550b）
"""

from __future__ import annotations

import copy
import json
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.loader import load_config                      # noqa: E402
from engine.models import (                                # noqa: E402
    BuildOrder, GameState, Plan, Plot, PlotPlan, PlotStatus, PlotType,
)
from engine.validate import validate_plan                  # noqa: E402
from server.session import GameSession                     # noqa: E402
from storage import JsonFileStorage                        # noqa: E402

PLAYER = "p1"
TOTAL_Q = 20
CFG_PATH = ROOT / ".scratch/config_dsh_gdp.json"
ABS_SAVE = ROOT / ".scratch/saves_dsh_gdp/8550_abs.json"
WORK_DIR = ROOT / ".scratch/saves_dsh_gdp"
WORK_NAME = "8550c"
SAVE_LIVE = ROOT / "saves-live"

#: 本季结算后至少要留下的现金（前瞻还会再兜一层）
RESERVE = 150.0
#: 每季最多采纳几条工程（防止一季砸太多、把现金流打断）
MAX_BUILDS_PER_Q = 8

MACHINE_GOODS = ("H-A", "H-F", "H-G", "H-J")
AUTOMATED_OF = {g: g + "+" for g in MACHINE_GOODS}

DEBUG = "--debug" in sys.argv


# ══════════════════════════════════════════════════════════════════
# 参数
# ══════════════════════════════════════════════════════════════════


@dataclass
class Params:
    """策略旋钮。全部可命令行覆盖，方便"实测研究线值不值"这类对比。"""

    research_max: int = 6          # 最多几块改走研究线（1→2 研究）
    research_from: int = 3         # 第几季起允许开研究线
    srv_max: int = 99              # 最多几块服务
    top_k: int = 14                # 每季进候选池的条数上限
    max_builds: int = MAX_BUILDS_PER_Q
    reserve: float = RESERVE
    #: 按"本季总扣款"的比例留安全垫（防规模变大后被打穿、下一季被迫停工）
    reserve_frac: float = 0.0
    food_tier: str = "good"        # good / base
    wc_weight: float = 1.0         # 周转占用在打分分母里的权重
    score_mode: str = "sqrt"       # ratio（ΔGDP/资本）/ sqrt / abs（ΔGDP 绝对值）
    allow_agri: bool = True        # 是否允许新建农业（纯资本轻，用于末期铺量）
    #: 每季进候选池的服务地块数（服务加强后，一季可能同时落 2~3 块）
    srv_slots: int = 3
    #: 每季把候选全表 dump 进输出 JSON（研究线 vs 资源/农业的对比数字取证用）
    dump_cands: bool = False
    #: 在该季强行塞入最高分的整条研究链（0 = 关；实测 I 线值不值用）
    force_chain_at: int = 0


def _parse_argv() -> tuple[Params, dict]:
    p = Params()
    opt = {
        "baseline": "--baseline" in sys.argv,
        "out_json": str(ROOT / ".scratch/gdp_replay_v3.json"),
        "save_name": WORK_NAME,
        "write_save": "--no-save" not in sys.argv,
    }
    i = 0
    while i < len(sys.argv):
        a = sys.argv[i]
        nxt = sys.argv[i + 1] if i + 1 < len(sys.argv) else None
        if a == "--research-max" and nxt:
            p.research_max = int(nxt); i += 2; continue
        if a == "--research-from" and nxt:
            p.research_from = int(nxt); i += 2; continue
        if a == "--srv-max" and nxt:
            p.srv_max = int(nxt); i += 2; continue
        if a == "--srv-slots" and nxt:
            p.srv_slots = int(nxt); i += 2; continue
        if a == "--dump-cands":
            p.dump_cands = True; i += 1; continue
        if a == "--force-chain" and nxt:
            p.force_chain_at = int(nxt); i += 2; continue
        if a == "--top-k" and nxt:
            p.top_k = int(nxt); i += 2; continue
        if a == "--max-builds" and nxt:
            p.max_builds = int(nxt); i += 2; continue
        if a == "--reserve" and nxt:
            p.reserve = float(nxt); i += 2; continue
        if a == "--reserve-frac" and nxt:
            p.reserve_frac = float(nxt); i += 2; continue
        if a == "--food-tier" and nxt:
            p.food_tier = nxt; i += 2; continue
        if a == "--wc-weight" and nxt:
            p.wc_weight = float(nxt); i += 2; continue
        if a == "--score-mode" and nxt:
            p.score_mode = nxt; i += 2; continue
        if a == "--out-json" and nxt:
            opt["out_json"] = nxt; i += 2; continue
        if a == "--save-name" and nxt:
            opt["save_name"] = nxt; i += 2; continue
        if a == "--no-agri":
            p.allow_agri = False; i += 1; continue
        i += 1
    return p, opt


# ══════════════════════════════════════════════════════════════════
# 会话
# ══════════════════════════════════════════════════════════════════


def load_session(save_name: str = WORK_NAME) -> GameSession:
    """从 8550_abs.json（Q3 初）载入，落一份工作副本到 .scratch/saves_dsh_gdp/。

    ★ 这里有一处**必须说明的存档迁移**：

      8550_abs.json 是上一轮（旧市场 Q0）存的，`world.market` 里**冻着旧曲线**
      （G 200 / H-* 30 / I 15 / C 200 / D 150 / F 200）。引擎求价读的是
      `state.world.market`，不是 config —— 不换掉的话，这一轮跑的还是旧市场，
      与"新机制"不符。

      所以载入后**用 config 的市场曲线覆盖 world.market**（P0/弹性/Q0 全部取新值），
      `price_history` 原样保留（那是真实发生过的历史价，不能篡改）。
      这一步写在报告里，不藏。
    """
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    # ⚠️ 工作副本必须按 **save_name** 命名：写死 WORK_NAME 会让"基线那一轮"
    #    把策略那一轮的最终存档覆盖掉（两边都是 8550b.json，基线的 storage
    #    却写 8550b-base.json）。
    dst = WORK_DIR / f"{save_name}.json"
    shutil.copyfile(ABS_SAVE, dst)
    cfg = load_config(CFG_PATH)
    # 源档带 BOM（utf-8-sig），不能按 utf-8 硬读
    raw = json.loads(dst.read_text(encoding="utf-8-sig"))
    state = GameState.from_dict(raw["state"])

    # ── 存档迁移：市场曲线换成新机制（见 docstring） ──
    state.world.market = {g: copy.deepcopy(c) for g, c in cfg.market.items()}

    storage = JsonFileStorage(WORK_DIR)
    return GameSession(state=state, cfg=cfg, player_ids=[PLAYER],
                       storage=storage, game_id=save_name)


def clone(sess: GameSession) -> GameSession:
    """深拷贝会话（不落盘）。用于试算与前瞻。"""
    st = copy.deepcopy(sess.state)
    return GameSession(state=st, cfg=sess.cfg, player_ids=[PLAYER],
                       storage=None, game_id=sess.game_id)


# ══════════════════════════════════════════════════════════════════
# 时间窗口 / 磨合
# ══════════════════════════════════════════════════════════════════


def _ramp_seq(dur: int, n: int) -> list[float]:
    """生产窗口内的 E_磨合 序列（引擎实测口径）。

    ★ 两段式，来自 `pipeline._advance_construction` / `_invest_construction`
      与 `_ramp_seq` 的实测对账（见 `.scratch/gdp_probe_ramp.txt`）：

        1 季工程（agri_new / res_new / srv_new）
            第 6 步当季完工 → 当季不能开工（计划里它还是 CONSTRUCTING）
            → 首个生产季 rq=0 → 0.5，第二季 0.75，之后 1.0

        ≥2 季工程（ind_new 与全部 1→2 / 2→3 升级）
            下个季度**第 1 步**完工，当季**不能开工**（计划提交时它还在建，
            validate 的 `plot_not_running` 会挡）→ 但第 5.9 步照样给它
            rq += 1（"有生产窗口"，哪怕没真产出）→ 首个生产季 rq=1 → **0.75**，
            之后 1.0。

    ⚠️ 这条是最容易算错的一条：**工期不增加现金成本，却实实在在吃掉一季产量**，
       而且 ≥2 季工程的磨合起点还比别人高一档（0.75 vs 0.5）。
    """
    if n <= 0:
        return []
    head = (0.5, 0.75) if dur <= 1 else (0.75,)
    seq = list(head[:n])
    while len(seq) < n:
        seq.append(1.0)
    return seq


def _first_prod_q(q: int, dur: int) -> int:
    """动工季 q + 工期 dur = 首个能产出的季度。"""
    return q + max(1, dur)


def _window(q: int, dur: int, end: int = TOTAL_Q) -> float:
    """从第 q 季动工算起、到 end 季为止的**有效生产季数**（含磨合折扣）。"""
    first = _first_prod_q(q, dur)
    n = end - first + 1
    return sum(_ramp_seq(dur, n))


def _prod_quarters(q: int, dur: int, end: int = TOTAL_Q) -> int:
    """动工后还剩几个生产季（不含磨合折扣）。"""
    return max(0, end - _first_prod_q(q, dur) + 1)



# ══════════════════════════════════════════════════════════════════
# 配方 / VA / 口粮成本 —— 全部照抄引擎口径
# ══════════════════════════════════════════════════════════════════


def recipe_of(cfg, ptype: PlotType, level: int, product: Optional[str]):
    """按 (类型, 等级, 产物) 取配方 —— 与 `Config.recipe_for()` 同口径。

    研究地块的存储编码是 `INDUSTRY + L2 + '研究'`，配方走 `research.maintain`。
    """
    if ptype is PlotType.RESEARCH:
        return cfg.recipes[(PlotType.RESEARCH, 1)]
    if ptype is PlotType.INDUSTRY and level == 2 and product == "研究":
        return cfg.recipes[(PlotType.RESEARCH, 1)]
    return cfg.recipe(ptype, level, product)


def va_of(cfg, ptype: PlotType, level: int, product: Optional[str],
          eff: float, svc_neighbors: int = 0) -> tuple[float, float]:
    """单季增加值（GDP 口径）、"变动底数"、以及周转占用。

    返回 ``(VA, X, WC)``：

        VA = e·E·P0 − Σ input·E·P0 − op           （op = 0.7c + 0.3c·E）
        X  = e·E·P0 − Σ input·E·P0 − 0.3c·E       （= dVA/dE 的底数）
        WC = Σ input·E·P0 + wage + op             （本季总扣款里"这块地占用"的那部分）

    `WC` 是这一版的第二个约束：**扣款在回款之前**，所以每一块地都要占着
    它一个季度的进料＋工资＋经营费。地块越多，这笔"周转金"越大，
    它和建设现金一样吃现金池 —— 见 `score` 里的 `wc_weight`。

    服务地块特殊：产出恒 0、收入 = 相邻非服务地块数 × 每邻收入（**不乘 E、不看磨合**），
    所以 VA = 服务收入 − op。
    """
    rec = recipe_of(cfg, ptype, level, product)
    if rec.output:
        out = rec.e * eff * cfg.price_of(rec.output)
        inp = sum(q * eff * cfg.price_of(g) for g, q in rec.input.items())
    else:
        out = 0.0
        inp = 0.0
    op = 0.7 * rec.c + 0.3 * rec.c * eff
    va = out - inp - op
    if ptype is PlotType.SERVICE:
        va += svc_neighbors * cfg.service_income_rate(level)
    x = out - inp - 0.3 * rec.c * eff
    # 工资：服务按 service 档，其余按 (类型, 等级, 产物) 档
    try:
        wage_rate = cfg.wage_for_plot(Plot(
            plot_id="_", plot_type=ptype, level=level, product=product))
    except Exception:                                   # noqa: BLE001
        wage_rate = 0.0
    wc = inp + rec.d * wage_rate + op
    return va, x, wc


def food_cost_per_labor(cfg) -> float:
    """每个单位劳动（Σd）带来的**口粮 GDP 成本**。

    L = Σd（非服务地块，服务不吃粮）→ N = 2L → F_基准 = (N/5)·m = 0.4·L·m
    优秀档口粮按 P0 折值 = F_优秀 × Σ(quota_g × P0_g)
       A 0.4×1.5×10 + B 0.2×1.5×7 + C 0.1×1.5×16 + D 0.02×1.5×26 = 11.28 / 单位 F
    所以每劳动单位 ≈ 0.4 × m × 11.28 ≈ 4.51（m=1）。
    """
    per_f = 0.0
    for g, ratio in cfg.food.good_quota.items():
        per_f += ratio * cfg.population.good_tier_multiplier * cfg.price_of(g)
    if cfg.food.baseline_quota and not cfg.food.good_quota:
        per_f = sum(r * cfg.price_of(g) for g, r in cfg.food.baseline_quota.items())
    return 0.4 * per_f


# ══════════════════════════════════════════════════════════════════
# 现场快照（每季算一次，供所有候选打分用）
# ══════════════════════════════════════════════════════════════════


@dataclass
class PlotView:
    """一块运行中地块的当前估值（满磨合口径）。"""

    plot_id: str
    plot_type: PlotType
    level: int
    product: Optional[str]
    eff: float          # 满磨合 E
    bonus: float        # 当前 E_邻接 的加成（封顶后）
    va: float           # 满磨合单季 VA
    x: float            # dVA/dE 底数
    wc: float           # 单季周转占用（进料 + 工资 + 经营费）
    d: float            # 劳动（喂人口/口粮；服务不计）
    svc_n: int          # 相邻运行非服务地块数（服务地块才有意义）


@dataclass
class Ctx:
    """一季的现场快照。"""

    q: int
    eff_h: float             # E_幸福（本季，来自上一季 H）
    eff_r: float             # 研究加成乘数
    cap: float               # 邻接封顶
    per_nb: float            # 关联表每邻加成
    svc_rate: dict           # 服务等级 → 加成率
    svc_income: dict         # 服务等级 → 每邻收入
    views: dict[str, PlotView]
    free: list[str]
    food_per_labor: float
    m_level: float           # ḡ 的等级乘数 m（估口粮用）
    n_research_running: int


def build_context(sess: GameSession) -> Ctx:
    state, cfg = sess.state, sess.cfg
    player = state.players[PLAYER]
    eff_cfg = cfg.efficiency

    # 满磨合 E：临时把 running_quarters 顶到 99 再算，算完还原
    views: dict[str, PlotView] = {}
    saved: dict[str, int] = {}
    running = [p for p in player.plots.values() if p.is_running]
    for p in running:
        saved[p.plot_id] = p.running_quarters
        p.running_quarters = 99
    try:
        from engine.efficiency import compute_efficiency
        for p in running:
            e = compute_efficiency(state, PLAYER, p, cfg)
            rec = cfg.recipe_for(p)
            svc_n = 0
            if p.plot_type is PlotType.SERVICE:
                for nid in state.world.neighbor_ids(p.plot_id):
                    np_ = player.plots.get(nid)
                    if np_ is not None and np_.is_running \
                            and np_.plot_type is not PlotType.SERVICE:
                        svc_n += 1
            va, x, wc = va_of(cfg, p.plot_type, p.level, p.product,
                              e.efficiency, svc_n)
            views[p.plot_id] = PlotView(
                plot_id=p.plot_id, plot_type=p.plot_type, level=p.level,
                product=p.product, eff=e.efficiency,
                bonus=e.adjacency_bonus, va=va, x=x, wc=wc,
                d=rec.d if p.plot_type is not PlotType.SERVICE else 0.0,
                svc_n=svc_n,
            )
    finally:
        for p in running:
            p.running_quarters = saved[p.plot_id]

    owned = set(player.plots)
    free = [f"{r},{c}" for r in range(state.world.rows)
            for c in range(state.world.cols) if f"{r},{c}" not in owned]

    # ḡ 的口粮等级乘数
    levels = [p.level for p in running]
    avg_level = (sum(levels) / len(levels)) if levels else 1.0

    return Ctx(
        q=state.world.quarter,
        eff_h=eff_cfg.happiness_factor(player.last_happiness),
        eff_r=1.0 + min(eff_cfg.research_bonus_per_plot * sum(
            1 for p in running if p.is_research), eff_cfg.research_bonus_cap),
        cap=eff_cfg.adjacency_cap,
        per_nb=eff_cfg.adjacency_per_neighbor,
        svc_rate={lv: eff_cfg.service_rate(lv) for lv in (1, 2, 3)},
        svc_income={lv: cfg.service_income_rate(lv) for lv in (1, 2, 3)},
        views=views,
        free=free,
        food_per_labor=food_cost_per_labor(cfg),
        m_level=cfg.population.level_multiplier(avg_level),
        n_research_running=sum(1 for p in running if p.is_research),
    )


def _neighbors_of(state, cell: str) -> list[str]:
    return state.world.neighbor_ids(cell)


#: 关联表缓存（`{本块类型: {允许的邻居类型}}`），由 `run()` 填一次。
_ADJ_TABLE_CACHE: dict[PlotType, set] = {}


def adj_target(state, ctx: Ctx, cell: str, ptype: PlotType) -> float:
    """新建一块地在**满磨合**时的 E_邻接（1 + 加成；第九轮起不封顶）。

    与 `engine/efficiency.compute_adjacency` 同一套口径：
      非服务：0.05 × 关联表邻居 + Σ(相邻运行服务按等级取率)，不封顶
              （旧口径 cap=0.40 时截断；第九轮 cap=None → 直接相加）
      服务  ：恒 1.0（不吃邻接）
    """
    if ptype is PlotType.SERVICE:
        return 1.0
    bonus = 0.0
    allowed = _ADJ_TABLE_CACHE.get(ptype, set())
    for nid in _neighbors_of(state, cell):
        v = ctx.views.get(nid)
        if v is None:
            continue
        if v.plot_type is PlotType.SERVICE:
            bonus += ctx.svc_rate[v.level]
        elif v.plot_type in allowed:
            bonus += ctx.per_nb
    if ctx.cap is None:
        return 1.0 + bonus
    return 1.0 + min(bonus, ctx.cap)


def neighbor_gain(state, ctx: Ctx, cell: str, ptype: PlotType,
                  level: int) -> float:
    """新地块给**八邻格已有运行地块**带来的单季 ΔGDP（只算加成，不算它自己）。

    对每个邻居：
      · 新块是服务 → 邻居拿 rate(level)；只有 cap 非 None 时才受旧
        "40% 余量"限制（第九轮 cap=None → 全额计入）；
      · 新块是普通地块 → 邻居若在同一张关联表里，拿 per_nb（同上限制）。
    ΔVA ≈ Δbonus × X（X 见 `va_of`）。
    """
    gain = 0.0
    for nid in _neighbors_of(state, cell):
        v = ctx.views.get(nid)
        if v is None:
            continue
        if ptype is PlotType.SERVICE:
            if v.plot_type is PlotType.SERVICE:
                continue
            add = (ctx.svc_rate[level] if ctx.cap is None
                   else min(ctx.svc_rate[level], max(0.0, ctx.cap - v.bonus)))
        else:
            if v.plot_type is PlotType.SERVICE:
                continue
            if v.plot_type not in _ADJ_TABLE_CACHE.get(ptype, set()):
                continue
            add = (ctx.per_nb if ctx.cap is None
                   else min(ctx.per_nb, max(0.0, ctx.cap - v.bonus)))
        if add > 1e-12:
            gain += add * v.x
    return gain


# ══════════════════════════════════════════════════════════════════
# 候选
# ══════════════════════════════════════════════════════════════════


@dataclass
class Cand:
    kind: str                 # "new" / "up" / "chain"
    cell: str
    ptype: PlotType
    target_level: int
    product: Optional[str]
    cash: float               # 工程现金费（已计开局减免）+ 工程物资 × 上季价
    wc: float                 # 这块地**新增**的单季周转占用（升级记增量）
    dgdp: float               # 窗口内 ΔGDP（估算）
    score: float              # dgdp / (cash + wc_weight × wc)
    note: str = ""
    order: Optional[BuildOrder] = None


def _last_price(state, goods: str) -> float:
    return float(state.world.last_price(goods))


def _work_cash(cfg, plot: Optional[Plot], order: BuildOrder,
               state, player) -> tuple[float, object]:
    """一条工程要花多少现金（物资按上季价折）。

    ⚠️ 与结算第 5 步同一个 `build_cash_plan` 口径（含 §5.3 开局减免），
       只是这里**单独一条**算，没算同季其它工程抢名额的情况；
       真正的现金在 `preview` 里核。
    """
    from engine import construction as C
    work = C.find_work_for_build(cfg, plot, order)
    disc = False
    if work.is_new and work.to_level == 1:
        rate = cfg.opening_discount_rate
        quota = int((cfg.opening_quota or {}).get(work.plot_type.value, 0) or 0)
        used = int((player.opening_quota_used or {}).get(work.plot_type.value, 0))
        if rate is not None and rate < 1.0 and used < quota:
            disc = True
    cash = work.cash * (float(cfg.opening_discount_rate) if disc else 1.0)
    cash += sum(q * _last_price(state, g) for g, q in work.materials.items())
    return cash, work


def _mk_order(cell, ptype, target_level, product) -> BuildOrder:
    return BuildOrder(plot_id=cell, plot_type=ptype,
                      target_level=target_level, product=product)


def _score(dgdp: float, cash: float, wc: float) -> float:
    """候选排序分。

    `mode = "ratio"`（默认）：`ΔGDP / (建设现金 + wc_weight × 新增周转占用)`
        —— "每一份要压住的钱换来多少 GDP"，在**现金紧**时是对的。

    `mode = "abs"`：直接按窗口内 ΔGDP 绝对值排
        —— 10×10 只有 100 格、地块是有限资源，现金宽松时"占一格换多少 GDP"
        才是该最大化的东西。

    `mode = "sqrt"`：`ΔGDP / cost^0.5` —— 两者折中。
    """
    cost = cash + _WC_WEIGHT * max(0.0, wc)
    if cost <= 0:
        return -1e9
    if _SCORE_MODE == "abs":
        return dgdp
    if _SCORE_MODE == "sqrt":
        return dgdp / (cost ** 0.5)
    return dgdp / cost


#: 周转权重（`--wc-weight`）。1.0 = 把一季周转当成与建设现金等价的资本占用。
_WC_WEIGHT: float = 1.0
#: 排序模式（`--score-mode`）：ratio / abs / sqrt
_SCORE_MODE: str = "ratio"


def cand_new(sess: GameSession, ctx: Ctx, cell: str,
             ptype: PlotType, product: Optional[str], level: int = 1) -> Optional[Cand]:
    """一个"新建"候选的打分。"""
    state, cfg = sess.state, sess.cfg
    player = state.players[PLAYER]
    order = _mk_order(cell, ptype, level, product)
    try:
        cash, _work = _work_cash(cfg, None, order, state, player)
    except Exception:                                   # noqa: BLE001
        return None
    if cash <= 0:
        return None

    eff_adj = adj_target(state, ctx, cell, ptype)
    eff = eff_adj * ctx.eff_h * ctx.eff_r
    svc_n = 0
    if ptype is PlotType.SERVICE:
        svc_n = sum(1 for nid in _neighbors_of(state, cell)
                    if (v := ctx.views.get(nid)) is not None
                    and v.plot_type is not PlotType.SERVICE)
    va, _x, wc = va_of(cfg, ptype, level, product, eff, svc_n)
    rec = recipe_of(cfg, ptype, level, product)
    d_labor = 0.0 if ptype is PlotType.SERVICE else rec.d
    va -= ctx.food_per_labor * ctx.m_level * d_labor        # 口粮是中间投入

    dur = int(getattr(_work, "quarters", 1) or 1)
    w = _window(ctx.q, dur)
    # 邻居的加成是"从新地块完工起一直有"，邻居自己的磨合不受影响
    # → 邻居那一侧只按**生产季数**算，不打磨合折扣。
    dgdp = va * w + neighbor_gain(state, ctx, cell, ptype, level) \
        * _prod_quarters(ctx.q, dur)
    return Cand("new", cell, ptype, level, product, cash, wc, dgdp,
                _score(dgdp, cash, wc),
                f"dur={dur} E={eff:.3f} va={va:.1f} wc={wc:.0f}", order)


def cand_upgrade(sess: GameSession, ctx: Ctx, view: PlotView,
                 new_level: int, new_product: Optional[str]) -> Optional[Cand]:
    """一个"升级"候选的打分。

        ΔGDP = VA_new × W(q+dur)  −  VA_old × (Q20 − q)
    （旧地块本来就能跑到 Q20，所以旧那一侧不折磨合 —— 它早就满了。）
    """
    state, cfg = sess.state, sess.cfg
    player = state.players[PLAYER]
    plot = player.plots[view.plot_id]
    order = _mk_order(plot.plot_id, plot.plot_type, new_level, new_product)
    try:
        cash, _work = _work_cash(cfg, plot, order, state, player)
    except Exception:                                   # noqa: BLE001
        return None
    if cash <= 0:
        return None
    dur = int(getattr(_work, "quarters", 2) or 2)

    # 旧地块还能跑几季（q+1 … Q20）；新形态从 q+dur 起产出
    n_old = max(0, TOTAL_Q - ctx.q)
    w = _window(ctx.q, dur)
    n_new = _prod_quarters(ctx.q, dur)
    if n_old <= 0:
        return None

    eff = view.eff                                     # 升级不改邻接
    if plot.plot_type is PlotType.SERVICE:
        va_new, _x, wc_new = va_of(cfg, plot.plot_type, new_level, None,
                                   eff, view.svc_n)
        va_old = view.va
        # 服务升级还改变"给邻居的加成率"：Δbonus × 邻居 X
        bonus_new = ctx.svc_rate[new_level]
        bonus_old = ctx.svc_rate[view.level]
        dgain = 0.0
        for nid in _neighbors_of(state, plot.plot_id):
            v = ctx.views.get(nid)
            if v is None or v.plot_type is PlotType.SERVICE:
                continue
            head = (float("inf") if ctx.cap is None
                    else max(0.0, ctx.cap - v.bonus))
            dgain += (min(bonus_new, head) - min(bonus_old, head)) * v.x
        dgdp = va_new * n_new - va_old * n_old + dgain * n_new
        dwc = wc_new - view.wc
        return Cand("up", plot.plot_id, plot.plot_type, new_level, None,
                    cash, dwc, dgdp, _score(dgdp, cash, dwc),
                    f"dur={dur} svc L{view.level}->L{new_level}", order)

    rec_new = recipe_of(cfg, plot.plot_type, new_level, new_product)
    va_new, _x, wc_new = va_of(cfg, plot.plot_type, new_level, new_product, eff)
    va_new -= ctx.food_per_labor * ctx.m_level * rec_new.d
    va_old = view.va - ctx.food_per_labor * ctx.m_level * view.d
    dgdp = va_new * w - va_old * n_old
    dwc = wc_new - view.wc
    return Cand("up", plot.plot_id, plot.plot_type, new_level, new_product,
                cash, dwc, dgdp, _score(dgdp, cash, dwc),
                f"dur={dur} L{view.level}->L{new_level} "
                f"va {va_old:.0f}->{va_new:.0f}", order)


def cand_research_chain(sess: GameSession, ctx: Ctx, view: PlotView) -> Optional[Cand]:
    """**整条研究线**（G L1 → 研究 L2 → I L3）作为一个候选来打分。

    ⚠️ 为什么必须整条打分：单看第一步（L2 研究）它是**纯负收益**
       —— 研究地块 0→0，只有 0.7c+0.3c·E 的经营费进 GDP。
       贪心若按单步看，永远不会迈出第一步，I 线就永远起不来。

    时间线（引擎实测，`.scratch/gdp_probe_ramp.txt`）：
        q      动工 ind_1_2_research（4 季）
        q+3    完工（第 1 步），当季不能开工
        q+4    研究跑一季（VA ≈ −60）；同季第 6 步动工 ind_2_3_high（5 季）
        q+8    完工（第 1 步），当季不能开工
        q+9    起产 I（磨合 0.75，次季 1.0）

    所以 `first_prod = q + dur_l2 + dur_l3`，窗口按 `dur ≥ 2` 的磨合序列。
    """
    state, cfg = sess.state, sess.cfg
    player = state.players[PLAYER]
    plot = player.plots[view.plot_id]
    o2 = _mk_order(plot.plot_id, PlotType.INDUSTRY, 2, "研究")
    o3 = _mk_order(plot.plot_id, PlotType.INDUSTRY, 3, "I")
    try:
        cash2, w2 = _work_cash(cfg, plot, o2, state, player)
        # 第三步要按"届时它已经是二级研究地块"来查工程
        tmp = Plot(plot_id=plot.plot_id, plot_type=PlotType.INDUSTRY, level=2,
                   status=PlotStatus.RUNNING, product="研究")
        cash3, w3 = _work_cash(cfg, tmp, o3, state, player)
    except Exception:                                   # noqa: BLE001
        return None
    dur = int(w2.quarters) + int(w3.quarters)
    first = _first_prod_q(ctx.q, dur)
    if first > TOTAL_Q:
        return None
    cash = cash2 + cash3
    w = _window(ctx.q, dur)
    n_new = _prod_quarters(ctx.q, dur)
    n_old = max(0, TOTAL_Q - ctx.q)
    if n_old <= 0:
        return None

    eff = view.eff
    va_i, _x, wc_i = va_of(cfg, PlotType.INDUSTRY, 3, "I", eff)
    va_i -= ctx.food_per_labor * ctx.m_level * 6          # I 的 d = 6
    va_res, _x2, wc_res = va_of(cfg, PlotType.INDUSTRY, 2, "研究", eff)
    va_res -= ctx.food_per_labor * ctx.m_level * 2        # 研究 d = 2
    va_old = view.va - ctx.food_per_labor * ctx.m_level * view.d

    # 研究形态只跑 1 季（q+dur_l2+1），其余时间在建或在产 I
    dgdp = va_i * w + va_res * 1.0 - va_old * n_old
    dwc = wc_i - view.wc
    return Cand("chain", plot.plot_id, PlotType.INDUSTRY, 3, "I",
                cash, dwc, dgdp, _score(dgdp, cash, dwc),
                f"链 L1→研究L2→I（{dur} 季，Q{first} 起产 I）"
                f" va={va_i:.0f}", o2)


def gen_candidates(sess: GameSession, ctx: Ctx, params: Params) -> list[Cand]:
    """枚举本季全部候选（新建 + 升级 + 研究链），返回**已打分**的列表。"""
    state, cfg = sess.state, sess.cfg
    player = state.players[PLAYER]
    q = ctx.q

    n_srv = sum(1 for v in ctx.views.values() if v.plot_type is PlotType.SERVICE)
    n_research = sum(1 for v in ctx.views.values() if v.plot_type is PlotType.INDUSTRY
                     and v.level == 2 and v.product == "研究")

    cands: list[Cand] = []

    # ── 新建 ────────────────────────────────────────────
    #   ⚠️ 同一个地块只能进一次 `plan.builds`（validate `duplicate_build_plot`），
    #      所以候选按"地块"去重 —— 工业给多个名额是为了多几个**不同**的格子。
    specs: list[tuple[PlotType, Optional[str], int]] = []
    if params.allow_agri:
        specs += [(PlotType.AGRICULTURE, "C", 3),
                  (PlotType.AGRICULTURE, "D", 3),
                  (PlotType.AGRICULTURE, "A", 2),
                  (PlotType.AGRICULTURE, "B", 2)]
    specs += [(PlotType.RESOURCE, None, 3),
              (PlotType.INDUSTRY, "G", 5)]
    if n_srv < params.srv_max:
        specs += [(PlotType.SERVICE, None, params.srv_slots)]

    for ptype, product, slots in specs:
        if ptype is PlotType.SERVICE and n_srv >= params.srv_max:
            continue
        scored: list[Cand] = []
        for cell in ctx.free:
            c = cand_new(sess, ctx, cell, ptype, product)
            if c is not None:
                scored.append(c)
        scored.sort(key=lambda c: -c.score)
        cands.extend(scored[:slots])

    # ── 升级 ────────────────────────────────────────────
    for pid in sorted(ctx.views):
        v = ctx.views[pid]
        if v.plot_type is PlotType.INDUSTRY:
            if v.level == 1:
                for g in MACHINE_GOODS:
                    c = cand_upgrade(sess, ctx, v, 2, g)
                    if c:
                        cands.append(c)
                # 研究链：整条打分（见 `cand_research_chain`）
                if q >= params.research_from and n_research < params.research_max:
                    c = cand_research_chain(sess, ctx, v)
                    if c:
                        cands.append(c)
            elif v.level == 2 and v.product == "研究":
                c = cand_upgrade(sess, ctx, v, 3, "I")
                if c:
                    cands.append(c)
            elif v.level == 2 and v.product in MACHINE_GOODS:
                c = cand_upgrade(sess, ctx, v, 3, AUTOMATED_OF[v.product])
                if c:
                    cands.append(c)
        elif v.plot_type is PlotType.RESOURCE:
            c = cand_upgrade(sess, ctx, v, v.level + 1, None)
            if c:
                cands.append(c)
        elif v.plot_type is PlotType.AGRICULTURE:
            c = cand_upgrade(sess, ctx, v, v.level + 1, v.product)
            if c:
                cands.append(c)
        elif v.plot_type is PlotType.SERVICE:
            c = cand_upgrade(sess, ctx, v, v.level + 1, None)
            if c:
                cands.append(c)

    cands.sort(key=lambda c: -c.score)
    if DEBUG:
        for c in cands[:params.top_k]:
            print(f"[Q{q:02d}]   cand {c.kind} {c.cell} {c.ptype.value}"
                  f"{(':' + c.product) if c.product else ''} L{c.target_level} "
                  f"Δgdp={c.dgdp:8.0f} cash={c.cash:7.0f} score={c.score:6.3f} "
                  f"({c.note})")
    return cands


# ══════════════════════════════════════════════════════════════════
# 计划
# ══════════════════════════════════════════════════════════════════


def declared_food(state, cfg, halted=()) -> dict[str, float]:
    """优秀档口粮申报（四样齐全 → H = 1.0）。

    ⚠️ 要预判**本季完工**的地块：`construction_left <= 1` 的在建地块下一季
       第 1 步就完工、当季就要吃饭（人口按已建成地块的 Σd 走）。
    """
    player = state.players[PLAYER]
    pop_cfg = cfg.population
    labor_food = 0.0
    levels: list[int] = []

    for pid, p in player.plots.items():
        if pid in halted:
            continue
        if p.status is PlotStatus.RUNNING:
            lv = p.level
        elif p.status is PlotStatus.CONSTRUCTING and (p.construction_left or 0) <= 1:
            lv = p.target_level or p.level
        else:
            continue
        try:
            # ⚠️ 必须按 **target_level + 目标产物** 查配方：
            #    升级中的地块此刻 `plot.level` 还是旧等级、`plot.product`
            #    已经是新产物（`start_work` 写进去的），直接 `recipe_for(p)`
            #    会查「工业 1 级产 H-G」这种不存在的配方 → 被 except 吞掉
            #    → 这块地既不进 labor_food、也不进 ḡ。
            #    实测后果：每次有升级完工，申报口粮就少一大截 → H 掉回 0.60
            #    → 下季 E_幸福 0.90（本可 1.10），全盘效率 -18%。
            rec = recipe_of(cfg, p.plot_type, lv, p.product)
        except Exception:                               # noqa: BLE001
            continue
        levels.append(lv)
        if p.plot_type is not PlotType.SERVICE:
            labor_food += rec.d

    n = pop_cfg.population(labor_food)
    avg_level = (sum(levels) / len(levels)) if levels else 1.0
    fb = pop_cfg.baseline_food(n, avg_level) * 1.05
    food = {"A": 0.6 * fb, "B": 0.3 * fb, "C": 0.15 * fb, "D": 0.03 * fb}
    return {g: q for g, q in food.items() if q > 1e-9}


def make_plan(state, cfg, builds, halted=(), tier: str = "good") -> Plan:
    player = state.players[PLAYER]
    plot_plans = {
        pid: PlotPlan(plot_id=pid, active=True, u=1.0)
        for pid, p in player.plots.items()
        if p.status is PlotStatus.RUNNING and pid not in halted
    }
    food = declared_food(state, cfg, halted) if tier == "good" else {}
    if tier != "good":
        # 基准档：只要 A/B（0.4F / 0.2F，F 按基准档算）
        player_labor = 0.0
        levels = []
        for pid, p in player.plots.items():
            if p.status is not PlotStatus.RUNNING or pid in halted:
                continue
            rec = cfg.recipe_for(p)
            levels.append(p.level)
            if p.plot_type is not PlotType.SERVICE:
                player_labor += rec.d
        n = cfg.population.population(player_labor)
        avg = (sum(levels) / len(levels)) if levels else 1.0
        fb = cfg.population.baseline_food(n, avg) * 1.05
        food = {"A": 0.4 * fb, "B": 0.2 * fb}
    return Plan(player_id=PLAYER, plot_plans=plot_plans, purchases={},
                food=food, builds=list(builds), contracts=[])


def try_plan(sess: GameSession, builds, halted=(), tier="good"):
    state = sess.state
    plan = make_plan(state, sess.cfg, builds, halted, tier)
    issues = validate_plan(plan, state.players[PLAYER], sess.cfg, PLAYER)
    if issues:
        return None, f"validate:{issues[0].code}"
    try:
        return sess.simulate(plan), ""
    except Exception as e:                              # noqa: BLE001
        return None, f"simulate:{type(e).__name__}: {e}"


def next_quarter_ok(sess: GameSession, builds, halted=(), tier="good",
                    min_cash: float = 0.0) -> tuple[bool, str]:
    """真结算本季（深拷贝），再看下一季"什么都不建"是否付得起、还剩多少。"""
    plan = make_plan(sess.state, sess.cfg, builds, halted, tier)
    try:
        c = clone(sess)
        c.submit(PLAYER, plan)
        c.end_quarter(PLAYER)
        c.start_next_quarter()
        rep2 = c.simulate(make_plan(c.state, c.cfg, [], halted, tier))
    except Exception as e:                              # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
    if float(rep2.cash_after) < min_cash:
        return False, f"next cash {rep2.cash_after:.0f} < {min_cash:.0f}"
    return True, ""


def halt_order(sess: GameSession) -> list[str]:
    """现金救急时的**停工顺序**：先停"每损失 1 点 GDP 能省最多现金"的地块。

    ⚠️ 不能按"现金边际最低"停（v1 的做法）：那样会先停掉农业（本来就薄利），
       而真正该停的是**进料重、GDP 薄**的地块 —— 停一块 G/H-G 省下的进料
       够养三块农业。指标：`省下的现金 / 损失的 GDP`，大的先停。
    """
    from engine import production as P
    state, cfg = sess.state, sess.cfg
    prices = {g: float(h[-1]) for g, h in state.world.price_history.items() if h}
    rows: list[tuple[str, float]] = []
    for pid, p in state.players[PLAYER].plots.items():
        if not p.is_running:
            continue
        v = _VIEW_CACHE.get(pid)
        va = v.va if v is not None else 1.0
        try:
            rec = cfg.recipe_for(p)
            eff = v.eff if v is not None else 1.0
            o = P.run_plot(p, rec, 1.0, eff, cfg, cfg.wage_for_plot(p))
            saved = (sum(q * prices.get(g, cfg.price_of(g)) for g, q in o.inputs.items())
                     + o.wage + o.operating_cost)
        except Exception:                               # noqa: BLE001
            saved = 0.0
        rows.append((pid, saved / max(va, 1e-6)))
    rows.sort(key=lambda x: -x[1])
    return [pid for pid, _ in rows]


#: 最近一季的现场快照（`halt_order` 复用，省一次满磨合计算）
_VIEW_CACHE: dict[str, "PlotView"] = {}


def plot_margins(sess: GameSession) -> dict[str, float]:
    """按上季价估每块地本季的现金边际（收入 − 投入 − 工资 − 经营费）。"""
    from engine.efficiency import compute_efficiency
    from engine.production import run_plot

    state, cfg = sess.state, sess.cfg
    prices = {g: float(h[-1]) for g, h in state.world.price_history.items() if h}
    out: dict[str, float] = {}
    for pid, p in state.players[PLAYER].plots.items():
        if p.status is not PlotStatus.RUNNING:
            continue
        try:
            eff = compute_efficiency(state, PLAYER, p, cfg)
            rec = cfg.recipe_for(p)
            o = run_plot(p, rec, 1.0, eff.efficiency, cfg, cfg.wage_for_plot(p))
            rev = (o.output_qty * prices.get(o.output_goods, 0.0)
                   if o.output_goods else 0.0)
            cost = sum(q * prices.get(g, 0.0) for g, q in o.inputs.items())
            out[pid] = rev - cost - o.wage - o.operating_cost
        except Exception:                               # noqa: BLE001
            out[pid] = -1e9
    return out


def choose_builds(sess: GameSession, params: Params, halted=(),
                  projects: Optional[dict] = None) \
        -> tuple[list[BuildOrder], object, list[Cand]]:
    """按 score 贪心挑本季工程。**每一步都用真引擎核过**。

    流程：
      ① **已开工的链**先续上（研究 L2 跑起来了 → 立刻排 L3 的 I）；
      ② 先量"不带建设"的经营侧扣款（`rep0.total_deduction`）→ 得出建设预算；
      ③ 按 score 从高到低累加，现金预算用完就停；
      ④ 整批 preview 一次：付不起 / 现金见底 → 砍掉最后一条重试；
      ⑤ 1 季前瞻：下一季不建还付得起吗 → 付不起继续砍。
    """
    projects = projects if projects is not None else {}
    player = sess.state.players[PLAYER]
    ctx = build_context(sess)
    cands = gen_candidates(sess, ctx, params)
    cash = float(player.cash)

    # ── ① 链的第二步：研究二级已经跑起来 → 本季排 L3（产物 I） ──
    forced: list[BuildOrder] = []
    for pid, stage in list(projects.items()):
        plot = player.plots.get(pid)
        if plot is None:
            projects.pop(pid, None)
            continue
        if stage == "l2":
            if plot.is_running and plot.level == 2 and plot.product == "研究":
                forced.append(_mk_order(pid, PlotType.INDUSTRY, 3, "I"))
                projects[pid] = "l3"
            elif plot.level >= 3:
                projects.pop(pid, None)
        elif stage == "l3":
            if plot.level >= 3:
                projects.pop(pid, None)

    rep0, why0 = try_plan(sess, forced, halted, params.food_tier)
    if rep0 is None:
        return [], None, cands
    # ★ 安全垫：除了固定预留，还按"本季总扣款"的比例留一手。
    #   只留固定值会在规模变大后被打穿 —— 实测 60 块时一个季度的总扣款
    #   能到 1.2 万，固定的 150 预留等于没有，下一季就被迫停工止损。
    buffer = max(params.reserve,
                 params.reserve_frac * float(rep0.total_deduction))
    budget = cash - float(rep0.total_deduction) - buffer

    chosen: list[Cand] = []
    spent = 0.0
    used_cells: set[str] = {b.plot_id for b in forced}

    # ★ `--force-chain Q`：在指定季把**最高分的整条研究链**强行塞进去。
    #   只为回答"研究线值不值"：同一条贪心、同一批候选，唯一差别是
    #   该季先花 1,400 现金把一块 G 改成 I 线，然后看 18 季累计 GDP 差多少。
    if params.force_chain_at and ctx.q == params.force_chain_at:
        chains = [c for c in cands if c.kind == "chain"
                  and c.cell not in used_cells and c.dgdp > 0]
        if chains:
            c = chains[0]
            chosen.append(c)
            used_cells.add(c.cell)
            spent += c.cash + _WC_WEIGHT * max(0.0, c.wc)
            if DEBUG:
                print(f"[Q{ctx.q:02d}]   **强制研究链** {c.cell} "
                      f"Δgdp={c.dgdp:.0f} cash={c.cash:.0f}")

    for c in cands:
        if len(chosen) >= params.max_builds:
            break
        # ⚠️ 同一块地/同一个格子一季只能下一条指令（validate 会报
        #    `duplicate_build_plot`）；跨类型的候选也会撞同一格，必须去重。
        if c.cell in used_cells:
            continue
        # 窗口内 ΔGDP ≤ 0 的候选一律不采纳：末期开工的工程永远产不出来，
        # 只会白扔现金、还把正在产出的地块按进工地（服务尤其明显）。
        if c.dgdp <= 0.0:
            continue
        # ★ 花的不是"建设现金"一项：这块地还要**永久占住**一季度的
        #   进料＋工资＋经营费（扣款在回款之前）。只按建设现金算预算，
        #   实测会在 3~4 季后被打穿 —— 那时现金 < 总扣款，只能停工止损，
        #   一次性丢掉一大片产量。所以预算与打分用**同一口径**的成本。
        cost = c.cash + _WC_WEIGHT * max(0.0, c.wc)
        if spent + cost > budget:
            continue
        chosen.append(c)
        used_cells.add(c.cell)
        spent += cost

    def orders() -> list[BuildOrder]:
        return forced + [c.order for c in chosen]

    rep = rep0
    while chosen:
        r, why = try_plan(sess, orders(), halted, params.food_tier)
        if r is None:
            if DEBUG:
                print(f"[Q{ctx.q:02d}]   整批被拒（{why}），砍 {chosen[-1].cell}")
            chosen.pop()
            continue
        if float(r.cash_after) < buffer:
            if DEBUG:
                print(f"[Q{ctx.q:02d}]   cash_after {r.cash_after:.0f} < 预留 "
                      f"{buffer:.0f}，砍 {chosen[-1].cell}")
            chosen.pop()
            continue
        rep = r
        break

    # 1 季前瞻（最后一季没必要：结算已经过了）
    while chosen and ctx.q < TOTAL_Q:
        ok, why = next_quarter_ok(sess, orders(), halted, params.food_tier,
                                  min_cash=buffer)
        if ok:
            break
        if DEBUG:
            print(f"[Q{ctx.q:02d}]   前瞻砍 {chosen[-1].cell}（{why}）")
        chosen.pop()
        r, _ = try_plan(sess, orders(), halted, params.food_tier)
        if r is not None:
            rep = r

    # 记下本季新开的研究链（后面几季要接着排 L3）
    for c in chosen:
        if c.kind == "chain":
            projects[c.cell] = "l2"
    return orders(), rep, cands


# ══════════════════════════════════════════════════════════════════
# 主循环
# ══════════════════════════════════════════════════════════════════


def _prices_of(rep) -> dict[str, float]:
    return {g: round(float(v.get("price", 0.0)), 2)
            for g, v in (rep.prices or {}).items() if v}


def _composition(player) -> dict[str, int]:
    comp: dict[str, int] = {}
    for p in player.plots.values():
        key = p.plot_type.value
        if p.product:
            key += f":{p.product}"
        key += f":L{p.level}"
        comp[key] = comp.get(key, 0) + 1
    return dict(sorted(comp.items()))


def run(baseline: bool = False, params: Optional[Params] = None,
        save_name: str = WORK_NAME, verbose: bool = True) -> dict:
    params = params or Params()
    t0 = time.time()

    global _WC_WEIGHT, _SCORE_MODE
    _WC_WEIGHT = params.wc_weight
    _SCORE_MODE = params.score_mode

    cfg = load_config(CFG_PATH)
    _ADJ_TABLE_CACHE.clear()
    _ADJ_TABLE_CACHE.update(cfg.efficiency.adjacency_table)

    sess = load_session(save_name)
    rows: list[dict] = []
    gdp_series: list[float] = []
    upgrades: list[dict] = []
    cand_dump: list[dict] = []         # --dump-cands：每季候选全表（取证研究线/资源/农业对比）
    projects: dict[str, str] = {}      # 研究链：{地块: "l2"|"l3"}

    while sess.state.world.quarter <= TOTAL_Q:
        q = sess.state.world.quarter
        player = sess.state.players[PLAYER]

        # ── 1) 先保证"什么都不建"能过（现金闸门 + 停工兜底） ──
        halted: set[str] = set()
        rep, why = try_plan(sess, [], halted, params.food_tier)
        if rep is None:
            global _VIEW_CACHE
            _VIEW_CACHE = build_context(sess).views     # 供 halt_order 复用
            for pid in halt_order(sess):
                halted.add(pid)
                rep, why = try_plan(sess, [], halted, params.food_tier)
                if rep is not None:
                    break
            if rep is None:
                print(f"[Q{q:02d}] 全停仍失败：{why}")
                break
            if verbose:
                print(f"[Q{q:02d}] 停工 {len(halted)} 块保现金：{sorted(halted)}")

        # ── 2) 挑本季工程 ──
        chosen: list[BuildOrder] = []
        cands: list[Cand] = []
        if not baseline:
            chosen, rep2, cands = choose_builds(sess, params, halted, projects)
            if rep2 is not None:
                rep = rep2

        builds_desc = [(b.plot_id, b.plot_type.value, b.target_level, b.product)
                       for b in chosen]
        if params.dump_cands and cands:
            cand_dump.append({
                "q": q,
                "top": [{"kind": c.kind, "cell": c.cell, "type": c.ptype.value,
                         "product": c.product, "level": c.target_level,
                         "dgdp": round(c.dgdp, 1), "cash": round(c.cash, 1),
                         "wc": round(c.wc, 1), "score": round(c.score, 3),
                         "note": c.note}
                        for c in cands[:40]],
                "chain": [{"cell": c.cell, "dgdp": round(c.dgdp, 1),
                           "cash": round(c.cash, 1), "wc": round(c.wc, 1),
                           "score": round(c.score, 3)}
                          for c in cands if c.kind == "chain"],
                "regr": [{"cell": c.cell, "type": c.ptype.value, "product": c.product,
                          "kind": c.kind, "level": c.target_level,
                          "dgdp": round(c.dgdp, 1), "cash": round(c.cash, 1),
                          "wc": round(c.wc, 1), "score": round(c.score, 3)}
                         for c in cands
                         if c.kind == "new" and (
                             c.ptype is PlotType.RESOURCE
                             or (c.ptype is PlotType.AGRICULTURE and c.product in ("C", "D"))
                             or c.ptype is PlotType.SERVICE)],
            })
        for b in chosen:
            upgrades.append({"q": q, "cell": b.plot_id,
                             "type": b.plot_type.value,
                             "to_level": b.target_level,
                             "product": b.product})

        # ── 3) 真结算 ──
        plan = make_plan(sess.state, cfg, chosen, halted, params.food_tier)
        sess.submit(PLAYER, plan)
        try:
            rep = sess.end_quarter(PLAYER)
        except Exception as e:                          # noqa: BLE001
            print(f"[Q{q:02d}] 结算失败：{e}")
            break

        gdp = float(rep.value_added)
        cash_after = float(rep.cash_after)
        gdp_series.append(round(gdp, 2))
        food_h = float(rep.food.happiness) if rep.food else 0.0
        out_val = sum(o.output_qty * cfg.price_of(o.output_goods)
                      for o in rep.production if o.output_goods)
        mat_val = sum(qty * cfg.price_of(g) for g, qty
                      in (rep.bought_breakdown.get("production_input") or {}).items())
        food_val = 0.0
        if rep.food:
            food_val = sum(q * cfg.price_of(g) for g, q in rep.food.deducted.items())

        n_srv = sum(1 for p in player.plots.values()
                    if p.plot_type is PlotType.SERVICE and p.is_running)
        n_run = sum(1 for p in player.plots.values() if p.is_running)
        rows.append({
            "q": q,
            "gdp": round(gdp, 2),
            "cash_before": round(float(rep.cash_before), 2),
            "cash_after": round(cash_after, 2),
            "happiness": round(food_h, 3),
            "builds": builds_desc,
            "build_cash": round(float(rep.construction_cash), 2),
            "halted": sorted(halted),
            "gdp_parts": {
                "output_p0": round(out_val, 2),
                "service_revenue": round(float(getattr(rep, "service_revenue", 0.0)), 2),
                "prod_material_p0": round(mat_val, 2),
                "operating": round(float(rep.operating), 2),
                "food_p0": round(food_val, 2),
            },
            "sales_revenue": round(float(rep.sales_revenue), 2),
            "service_revenue": round(float(getattr(rep, "service_revenue", 0.0)), 2),
            "wages": round(float(rep.wages), 2),
            "materials_cost": round(float(rep.materials_cost), 2),
            "plots": len(player.plots),
            "running": n_run,
            "services": n_srv,
            "sold": {g: round(v, 1) for g, v in rep.sold.items() if v > 0.01},
            "prices": _prices_of(rep),
        })
        if verbose:
            print(f"[Q{q:02d}] GDP={gdp:9.2f} cash={cash_after:9.2f} H={food_h:.2f} "
                  f"plots={len(player.plots):3d} svc={n_srv:2d} "
                  f"builds={len(chosen):2d} rev={float(rep.sales_revenue):8.0f} "
                  f"svc_rev={float(getattr(rep, 'service_revenue', 0.0)):6.1f} "
                  f"wage={float(rep.wages):7.0f} op={float(rep.operating):6.0f}")
            if builds_desc:
                print("      → " + ", ".join(
                    f"{c}({t}{':' + p if p else ''}→L{lv})"
                    for c, t, lv, p in builds_desc))
        sess.start_next_quarter()

    # ── 尾声：落盘 + 汇总 ──
    player = sess.state.players[PLAYER]
    total = sum(gdp_series)
    comp = _composition(player)

    # 技能计数：升级按"最终形态"统计
    def count(pred) -> int:
        return sum(1 for p in player.plots.values() if pred(p))

    n_ha2 = count(lambda p: p.plot_type is PlotType.INDUSTRY and p.level == 2
                  and p.product == "H-A")
    n_ha3 = count(lambda p: p.plot_type is PlotType.INDUSTRY and p.level == 3
                  and (p.product or "").startswith("H-"))
    n_i = count(lambda p: p.plot_type is PlotType.INDUSTRY and p.level == 3
                and p.product == "I")
    n_research = count(lambda p: p.is_research)
    n_srv = count(lambda p: p.plot_type is PlotType.SERVICE)
    n_srv2 = count(lambda p: p.plot_type is PlotType.SERVICE and p.level == 2)
    n_srv3 = count(lambda p: p.plot_type is PlotType.SERVICE and p.level == 3)
    n_srv1 = n_srv - n_srv2 - n_srv3
    n_industry = count(lambda p: p.plot_type is PlotType.INDUSTRY)

    # 服务收入占 GDP：逐季累加 `service_revenue` 与 `gdp`（GDP 里已含服务收入）
    svc_rev_total = round(sum(r.get("service_revenue", 0.0) for r in rows), 2)
    # 升级构成（to_level >= 2 的全是升级：新建一律 L1）
    up_by_type: dict[str, int] = {}
    for b in upgrades:
        if int(b["to_level"]) >= 2:
            key = f'{b["type"]}:{b["product"] or "-"}:L{b["to_level"]}'
            up_by_type[key] = up_by_type.get(key, 0) + 1

    summary = {
        "mode": "baseline" if baseline else "strategy",
        "params": {
            "research_max": params.research_max,
            "research_from": params.research_from,
            "srv_max": params.srv_max,
            "srv_slots": params.srv_slots,
            "reserve": params.reserve,
            "food_tier": params.food_tier,
            "score_mode": params.score_mode,
            "wc_weight": params.wc_weight,
        },
        "total_gdp": round(total, 2),
        "gdp_series": gdp_series,
        "final_quarter_gdp": gdp_series[-1] if gdp_series else 0.0,
        "peak_quarter_gdp": max(gdp_series) if gdp_series else 0.0,
        "final_cash": round(float(player.cash), 2),
        "plots": len(player.plots),
        "services": n_srv,
        "services_l1": n_srv1,
        "services_l2": n_srv2,
        "services_l3": n_srv3,
        "service_revenue_total": svc_rev_total,
        "service_share_of_gdp": round(svc_rev_total / total, 4) if total else None,
        "research_plots": n_research,
        "industry_plots": n_industry,
        "upgrade_counts": {
            "H-A_L2": n_ha2,
            "H-plus_L3": n_ha3,
            "I_L3": n_i,
            "service_L2": n_srv2,
            "service_L3": n_srv3,
        },
        "upgrades_by_type": dict(sorted(up_by_type.items())),
        "builds_total": len(upgrades),
        "builds": upgrades,
        "composition": comp,
        "rows": rows,
        "elapsed_s": round(time.time() - t0, 1),
    }
    if params.dump_cands:
        summary["cand_dump"] = cand_dump
    print(f"\n== {'BASELINE' if baseline else 'STRATEGY'} == "
          f"累计GDP={total:.2f} 终局GDP={summary['final_quarter_gdp']:.2f} "
          f"现金={summary['final_cash']:.2f} 地块={summary['plots']} "
          f"服务={n_srv}(L1 {n_srv1}/L2 {n_srv2}/L3 {n_srv3}) "
          f"服务收入={svc_rev_total:.0f}(占比{(svc_rev_total/total*100 if total else 0):.2f}%) "
          f"升级={len(upgrades)} 用时={summary['elapsed_s']}s")
    return summary


def save_final(save_name: str) -> None:
    """把工作副本转存到 saves-live/（**新档号**，不碰 8550）。"""
    src = WORK_DIR / f"{save_name}.json"
    if not src.exists():
        print(f"⚠️ 工作副本不存在：{src}")
        return
    SAVE_LIVE.mkdir(parents=True, exist_ok=True)
    raw = json.loads(src.read_text(encoding="utf-8-sig"))
    state = GameState.from_dict(raw["state"])
    JsonFileStorage(SAVE_LIVE).save(save_name, state)
    print(f"→ saves-live/{save_name}.json")


def main() -> None:
    params, opt = _parse_argv()
    cfg = load_config(CFG_PATH)
    strat = run(baseline=False, params=params, save_name=opt["save_name"])
    print(f"→ {opt['save_name']}.json（工作副本在 .scratch/saves_dsh_gdp/）")

    base = run(baseline=True, params=params, save_name=opt["save_name"] + "-base",
               verbose=False)

    payload = {
        "meta": {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "source_save": str(ABS_SAVE.relative_to(ROOT)),
            "market_refreshed": True,
            "market_curves": {
                g: {"P0": c.P0, "Q0": c.Q0, "Es": c.Es, "Ed": c.Ed}
                for g, c in sorted(cfg.market.items())
            },
            "engine_note": (
                "新机制：服务邻接 +5/7.5/10%（与关联表相加、封顶40%）+ 服务收入 "
                "6/9/12 每邻；市场 Q0 G300/H-150/H-+60/I60/C260/D200/F300；"
                "研究线 4+5 季。world.market 已按 config 覆盖"
                "（8550_abs.json 里冻的是放大前的旧曲线 C200/G200/H-A30/I15）。"
            ),
            "old_mechanism_reference": 87367.52,
        },
        "strategy": strat,
        "baseline": base,
        "comparison": {
            "old_mechanism_total_gdp": 87367.52,
            "new_baseline_total_gdp": base["total_gdp"],
            "new_strategy_total_gdp": strat["total_gdp"],
            "strategy_over_baseline": round(
                strat["total_gdp"] / base["total_gdp"], 4) if base["total_gdp"] else None,
        },
    }
    out = Path(opt["out_json"])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n→ {out}")
    print(f"   基线 {base['total_gdp']:.2f} / 策略 {strat['total_gdp']:.2f} "
          f"= {payload['comparison']['strategy_over_baseline']}×")

    if opt["write_save"]:
        save_final(opt["save_name"])


if __name__ == "__main__":
    main()
