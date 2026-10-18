# -*- coding: utf-8 -*-
"""一次性脚本：生成"全图 3 级、打开即富裕"存档 region-sim/saves/full_tier3.json
（game_id="full_tier3"）。

口径（2026-10-18 黑客松演示档；只新增 saves/full_tier3.json，不动任何代码/其他存档）：
- world / market / policy_state 等全局字段照抄 make_city_save.py → city.json 的形态
  （quarter=1 / rows=cols=10 / total_quarters=20 / phase="setup" / policies=[] /
   price_history={} / policy_state={"seed":20261016,"seq":[],"mode":"regular"}）。
- players.p1：cash=500000.0（几十万，宽裕）、inventory={}、debts=[]、
  credit_flag=false、last_happiness=0.6、opening_quota_used={}。
- 100 块地全部 status="running"、construction_left=0、running_quarters=3、
  target_level=level、name=None —— 全部已完工，无建造中/空地。
- 等级分布：≥80 块 3 级、其余 2 级；五种 zone 齐全；每种 zone ≥2 块 3 级。
- construction_materials 沿用 make_city_save.py 的"升到当前等级累计工程物资"口径：
    农业 L2 {"H-A":2} / L3 {"H-A":2,"H-A+":1}
    资源 L2 {"H-F":3} / L3 {"H-F":3,"H-F+":1}
    工业机械线 L2 {"H-G":5}（product∈H-A/H-F/H-G/H-J）
    工业自动化 L3 {"H-G":8}（product∈H-A+/H-F+/H-G+/H-J+）
    工业 I L3 {"H-G":10}（research 路线 product="I"）
    服务 L2 {"H-J":2} / L3 {"H-J":2,"H-J+":1}
    研究（plot_type="research", level=1）：{}（工程表外，维护态）
      ※ 本档为保"≥2 级"底线，research 给到 L2/L3，materials 同样为 {}（工程表外）。

跑法（在 region-sim 目录下）：
    .venv\\Scripts\\python.exe tools\\make_full_tier3_save.py
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
OUT = ROOT / "saves" / "full_tier3.json"

# ── market：从 1.json 原样复制（15 个商品的完整 MarketCurve）──────────────────
with ONE_JSON.open(encoding="utf-8") as f:
    one = json.load(f)
MARKET = copy.deepcopy(one["state"]["world"]["market"])
assert len(MARKET) == 15, f"market 应有 15 个商品，实际 {len(MARKET)}"

# ── 通用 plot 构造 ─────────────────────────────────────────────────────────────

def plot(r: int, c: int, plot_type: str, level: int, product: str | None,
         materials: dict[str, float]) -> dict:
    """每个 plot 的通用字段：status=running, running_quarters=3,
    construction_left=0, target_level=level, name=None —— 与 city.json 一致。"""
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

# ── 各路累计工程物资（沿用 make_city_save.py 口径）────────────────────────────
AGRI = {2: {"H-A": 2.0}, 3: {"H-A": 2.0, "H-A+": 1.0}}
RES  = {2: {"H-F": 3.0}, 3: {"H-F": 3.0, "H-F+": 1.0}}
IND_MACHINE2 = {"H-G": 5.0}    # 工业二级机械线 = 2 + 3
IND_AUTO3    = {"H-G": 8.0}    # 工业三级自动化 H-*+ = 2 + 3 + 3
IND_I3       = {"H-G": 10.0}   # 工业三级 I（research 路线）= 2 + 3 + 5
SRV  = {2: {"H-J": 2.0}, 3: {"H-J": 2.0, "H-J+": 1.0}}
RESEARCH_MAT: dict[str, float] = {}   # 研究地块（工程表外，维护态）—— L2/L3 同此

# 工业 L3 的 product 轮换（4 条自动化 + 1 条研究线 I）
IND_L3_PRODUCTS = ["H-A+", "H-F+", "H-G+", "H-J+", "I"]


def agri(r, c, level, product):  return plot(r, c, "agriculture", level, product, AGRI[level])
def res(r, c, level):            return plot(r, c, "resource",    level, "F",     RES[level])
def srv(r, c, level):            return plot(r, c, "service",     level, None,    SRV[level])


def ind(r, c, level, product):
    if level == 2:
        return plot(r, c, "industry", 2, product, IND_MACHINE2)           # product ∈ H-A/H-F/H-G/H-J
    if product == "I":
        return plot(r, c, "industry", 3, "I", IND_I3)
    return plot(r, c, "industry", 3, product, IND_AUTO3)                  # product ∈ H-*+


def research(r, c, level):
    # 研究地块：工程表外 → materials 恒为 {}；本档给 L2/L3 以满足"≥2 级"。
    return plot(r, c, "research", level, None, RESEARCH_MAT)


# ── 100 格布局（行优先 r=0..9, c=0..9）─────────────────────────────────────────
# 规划：agriculture 20 / resource 20 / industry 20 / service 16 / research 24 = 100
# 等级：3 级 = 83 块（≥80 ✓），2 级 = 17 块；每种 zone 的 3 级数：
#   agri 16 / res 16 / ind 16 / srv 12 / research 23 —— 均 ≥2 ✓
plots: list[dict] = []

AGRI_PRODUCTS = ["A", "B", "C", "D"]

# 第 0..1 行 · 农业 20 块：每行前 2 块 L2，后 8 块 L3 → agri L2=4, L3=16
# (注：原写 c<3 得 L2=6/L3=14，三行合计 L3=77 < 80 断言不过；改成 c<2 让 L3=83 达标)
for r in (0, 1):
    product = AGRI_PRODUCTS[r % len(AGRI_PRODUCTS)]
    for c in range(10):
        level = 2 if c < 2 else 3
        plots.append(agri(r, c, level, product))

# 第 2..3 行 · 资源 20 块：每行前 2 块 L2，后 8 块 L3 → res L2=4, L3=16
for r in (2, 3):
    for c in range(10):
        level = 2 if c < 2 else 3
        plots.append(res(r, c, level))

# 第 4..5 行 · 工业 20 块：前 2 块 L2 机械线，后 8 块 L3（4 条自动化 + I 轮换）
IND_L2_PRODUCTS = ["H-A", "H-F", "H-G"]
for r in (4, 5):
    for c in range(10):
        if c < 2:
            plots.append(ind(r, c, 2, IND_L2_PRODUCTS[c % len(IND_L2_PRODUCTS)]))
        else:
            product = IND_L3_PRODUCTS[(r * 10 + c) % len(IND_L3_PRODUCTS)]
            plots.append(ind(r, c, 3, product))
# ind: L2=4, L3=16

# 第 6 行 · 服务 10 块：c=0 补 1 块 L3、c=1 补 1 块 L2，c=2..9 按 2×(L2×3+L3×7)
plots.append(srv(6, 0, 3))                       # srv 补 L3
plots.append(srv(6, 1, 2))                       # srv 补 L2
for c in range(2, 10):
    level = 2 if (c - 2) < 3 else 3              # (c-2)=0,1,2 → L2；3..7 → L3
    plots.append(srv(6, c, level))
# srv 本行：L2 = 1 + 3 = 4，L3 = 1 + 5 = 6

# 第 7 行 · 服务余 6 块全 L3 + 研究 4 块：c=9 补 1 块 L2，c=6..8 补 L3
for c in range(0, 6):
    plots.append(srv(7, c, 3))                   # 6× srv L3
plots.append(research(7, 6, 3))
plots.append(research(7, 7, 3))
plots.append(research(7, 8, 3))
plots.append(research(7, 9, 2))                  # research 唯一 1 块 L2
# srv：L2=4, L3=12；research：L2=1, L3=3

# 第 8..9 行 · 研究 20 块全 L3 → research：L2=1, L3=23
for r in (8, 9):
    for c in range(10):
        plots.append(research(r, c, 3))

# ── 组装校验 ───────────────────────────────────────────────────────────────────
assert len(plots) == 100, f"应有 100 块地，实际 {len(plots)}"
ids = [p["plot_id"] for p in plots]
assert len(set(ids)) == 100, "plot_id 有重复"
expected_ids = {f"{r},{c}" for r in range(10) for c in range(10)}
assert set(ids) == expected_ids, "plot_id 没有铺满 10×10"

from collections import Counter
by_type = Counter(p["plot_type"] for p in plots)
by_level = Counter(p["level"] for p in plots)
tier3_by_type = Counter(p["plot_type"] for p in plots if p["level"] == 3)
assert set(by_type) == {"agriculture", "resource", "industry", "service", "research"}, \
    f"五种 zone 不齐：{set(by_type)}"
assert by_level[3] >= 80, f"3 级地块应 ≥80，实际 {by_level[3]}"
assert all(p["level"] >= 2 for p in plots), "存在低于 2 级的地块"
assert all(tier3_by_type[t] >= 2 for t in by_type), \
    f"每种 zone 需 ≥2 块 3 级：{dict(tier3_by_type)}"
assert all(p["status"] == "running" and p["construction_left"] == 0 for p in plots), \
    "存在未完工地块"

plots_dict = {p["plot_id"]: p for p in plots}   # dict 键即 plot_id，与 city.json 形态一致

# ── 顶层存档（pack() 结构，照抄 city.json 样式）────────────────────────────────
saved_at = datetime.now(timezone(timedelta(hours=8))).replace(microsecond=0).isoformat()

save = {
    "schema_version": 1,
    "game_id": "full_tier3",
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
                "cash": 500000.0,
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
print(f"  zone 分布: {dict(sorted(by_type.items()))}")
print(f"  等级分布: L2={by_level[2]}  L3={by_level[3]}  (total={sum(by_level.values())})")
print(f"  各 zone 3 级数: {dict(sorted(tier3_by_type.items()))}")
