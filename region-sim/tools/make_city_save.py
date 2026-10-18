# -*- coding: utf-8 -*-
"""一次性脚本：生成铺满 10×10 地图的存档 region-sim/saves/city.json（game_id="city"）。

口径来自用户 2026-10-18 的明确硬编码（17:00 冻结期，不改任何代码/其他存档/不 commit）：
- world 照 saves/1.json 开局形态：quarter=1/rows=cols=10/total_quarters=20/phase="setup"、
  market 从 1.json 原样复制（15 个商品 MarketCurve）、policies=[]、price_history={}、
  policy_state={"seed":20261016,"seq":[],"mode":"regular"}
- players.p1：cash=200000.0、100 块地、inventory={}、debts=[]、credit_flag=false、
  last_happiness=0.6、opening_quota_used={}
- construction_materials = 升到当前等级的累计工程物资（每路各自，config.json construction.works）：
    农业 L1 {"H-A":1} / L2 {"H-A":2} / L3 {"H-A":2,"H-A+":1}
    资源 L1 {"H-F":1} / L2 {"H-F":3} / L3 {"H-F":3,"H-F+":1}
    工业 G L1 {"H-G":2}
    工业机械线 L2（product∈H-A/H-F/H-G/H-J） {"H-G":5}     (=ind_new 2 + ind_1_2_machine 3)
    工业自动化 H-*+ L3（machine 路线）       {"H-G":8}     (=2+3+3)
    工业研究线 L2（product="研究"）          {"H-G":5}     (=2+3)
    工业 I L3（research 路线）              {"H-G":10}    (=2+3+5)
    服务 L1 {"H-J":1} / L2 {"H-J":2} / L3 {"H-J":2,"H-J+":1}
    研究地块（plot_type="research", level=1）：{} （工程表外，维护态）

跑法（在 region-sim 目录下）：
    .venv\\Scripts\\python.exe tools\\make_city_save.py
（或任意系统 python3。只依赖标准库。）
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent          # region-sim/tools
ROOT = HERE.parent                              # region-sim/
ONE_JSON = ROOT / ".scratch" / "zip-1json" / "1.json"   # 1.json 的只读副本（从备份 zip 解出）
OUT = ROOT / "saves" / "city.json"

# ── market：从 1.json 原样复制（15 个商品的完整 MarketCurve）──────────────────
with ONE_JSON.open(encoding="utf-8") as f:
    one = json.load(f)
MARKET = copy.deepcopy(one["state"]["world"]["market"])
assert len(MARKET) == 15, f"market 应有 15 个商品，实际 {len(MARKET)}"

# ── 通用 plot 构造 ─────────────────────────────────────────────────────────────

def plot(r: int, c: int, plot_type: str, level: int, product: str | None,
         materials: dict[str, float]) -> dict:
    """每个 plot 的通用字段：status=running, running_quarters=3,
    construction_left=0, target_level=level, name=None, product 按表。"""
    return {
        "plot_id": f"{r},{c}",
        "plot_type": plot_type,
        "level": level,
        "status": "running",
        "running_quarters": 3,
        "construction_left": 0,
        "target_level": level,
        "construction_materials": dict(materials),
        "product": product,
        "name": None,
    }

# ── 各路累计工程物资（见模块头口径）────────────────────────────────────────────
AGRI = {1: {"H-A": 1.0}, 2: {"H-A": 2.0}, 3: {"H-A": 2.0, "H-A+": 1.0}}
RES  = {1: {"H-F": 1.0}, 2: {"H-F": 3.0}, 3: {"H-F": 3.0, "H-F+": 1.0}}
IND_G        = {"H-G": 2.0}    # 工业一级 G（ind_new）
IND_MACHINE2 = {"H-G": 5.0}    # 工业二级机械线 = 2 + 3
IND_AUTO3    = {"H-G": 8.0}    # 工业三级自动化 H-*+ = 2 + 3 + 3
IND_RESEARCH = {"H-G": 5.0}    # 工业二级研究线（product="研究"）= 2 + 3
IND_I3       = {"H-G": 10.0}   # 工业三级 I（research 路线）= 2 + 3 + 5
SRV  = {1: {"H-J": 1.0}, 2: {"H-J": 2.0}, 3: {"H-J": 2.0, "H-J+": 1.0}}
RESEARCH_MAT: dict[str, float] = {}   # 研究地块（工程表外，维护态）


def agri(r, c, level, product):  return plot(r, c, "agriculture", level, product, AGRI[level])
def res(r, c, level):            return plot(r, c, "resource",   level, "F",     RES[level])
def srv(r, c, level):            return plot(r, c, "service",    level, None,    SRV[level])
def research(r, c):              return plot(r, c, "research",   1,     None,    RESEARCH_MAT)


# ── 100 格布局（行优先 r=0..9, c=0..9）─────────────────────────────────────────
plots: list[dict] = []

# 第 0 行 · 农业 A 三级 + 农业 B 一级（10）
plots += [
    agri(0, 0, 1, "A"), agri(0, 1, 2, "A"), agri(0, 2, 3, "A"),
]
plots += [agri(0, c, 1, "B") for c in range(3, 10)]            # 7× B L1

# 第 1 行 · 农业 B/C/D（10）
plots += [
    agri(1, 0, 2, "B"), agri(1, 1, 3, "B"),
    agri(1, 2, 1, "C"), agri(1, 3, 2, "C"), agri(1, 4, 3, "C"),
    agri(1, 5, 1, "D"), agri(1, 6, 2, "D"), agri(1, 7, 3, "D"),
]
plots += [agri(1, c, 1, "A") for c in range(8, 10)]            # 2× A L1

# 第 2 行 · 资源 9 块 + 工业 G 一块（10）
plots += [res(2, c, 1) for c in (0, 1, 2)]
plots += [res(2, c, 2) for c in (3, 4, 5)]
plots += [res(2, c, 3) for c in (6, 7, 8)]
plots += [plot(2, 9, "industry", 1, "G", IND_G)]

# 第 3 行 · 工业一级 9 块 + 二级机械开头（10）
plots += [plot(3, c, "industry", 1, "G", IND_G) for c in range(0, 9)]
plots += [plot(3, 9, "industry", 2, "H-A", IND_MACHINE2)]

# 第 4 行 · 工业二级其余 + 三级自动化（10）
plots += [
    plot(4, 0, "industry", 2, "H-F", IND_MACHINE2),
    plot(4, 1, "industry", 2, "H-G", IND_MACHINE2),
    plot(4, 2, "industry", 2, "H-J", IND_MACHINE2),
    plot(4, 3, "industry", 2, "研究", IND_RESEARCH),          # 研究线二级
    plot(4, 4, "industry", 3, "H-A+", IND_AUTO3),
    plot(4, 5, "industry", 3, "H-F+", IND_AUTO3),
    plot(4, 6, "industry", 3, "H-G+", IND_AUTO3),
    plot(4, 7, "industry", 3, "H-J+", IND_AUTO3),
    plot(4, 8, "industry", 3, "I", IND_I3),
    srv(4, 9, 1),
]

# 第 5 行 · 服务 3 块 + 研究 3 块 + 农业填充（10）
plots += [srv(5, 0, 2), srv(5, 1, 3)]
plots += [research(5, c) for c in (2, 3, 4)]                  # 3× research L1
plots += [agri(5, c, 1, "A") for c in range(5, 10)]           # 5× A L1

# 第 6..9 行 · 凑满 100 的填充（40）——全部 agriculture L1 product=A
for r in range(6, 10):
    plots += [agri(r, c, 1, "A") for c in range(0, 10)]

# ── 组装校验 ───────────────────────────────────────────────────────────────────
assert len(plots) == 100, f"应有 100 块地，实际 {len(plots)}"
ids = [p["plot_id"] for p in plots]
assert len(set(ids)) == 100, "plot_id 有重复"
expected_ids = {f"{r},{c}" for r in range(10) for c in range(10)}
assert set(ids) == expected_ids, "plot_id 没有铺满 10×10"

plots_dict = {p["plot_id"]: p for p in plots}   # dict 键即 plot_id，与 1.json 形态一致

# ── 顶层存档（pack() 结构）────────────────────────────────────────────────────
saved_at = datetime.now(timezone(timedelta(hours=8))).replace(microsecond=0).isoformat()

save = {
    "schema_version": 1,
    "game_id": "city",
    "saved_at": saved_at,
    "quarter": 1,
    "total_quarters": 20,
    "state": {
        "world": {
            "quarter": 1,
            "rows": 10,
            "cols": 10,
            "total_quarters": 20,
            "phase": "setup",
            "market": MARKET,
            "policies": [],
            "price_history": {},
            "policy_state": {"seed": 20261016, "seq": [], "mode": "regular"},
        },
        "players": {
            "p1": {
                "player_id": "p1",
                "cash": 200000.0,
                "plots": plots_dict,
                "inventory": {},
                "debts": [],
                "credit_flag": False,
                "last_happiness": 0.6,
                "opening_quota_used": {},
            }
        },
        "contracts": {},
    },
    "report": {},
}

OUT.parent.mkdir(parents=True, exist_ok=True)
with OUT.open("w", encoding="utf-8") as f:
    json.dump(save, f, ensure_ascii=False, indent=2)
    f.write("\n")

print(f"WROTE {OUT}  ({OUT.stat().st_size} bytes)  saved_at={saved_at}")
