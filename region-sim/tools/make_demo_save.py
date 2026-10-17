"""生成"打开即所有地块已建成"演示存档 region-sim/saves/pizzademo.json。

背景：
- 正常开局现金 1200 建不出"各种地块齐聚"的景象。
- 这份存档是**演示用**：跳过开局现金限制，直接给玩家 p1 配好 31 块已建成
  （status=running, construction_left=0, running_quarters=2 → 磨合 E=1.0）的地块，
  覆盖**农业 4 产物 × 三级**、**资源三级**、**服务三级**、**工业 G + 四条机械路线二级/三级 + 研究路线 I**、
  **研究 × 3**。
- 地图 = 10×10。

跑法：
    python region-sim/tools/make_demo_save.py

脚本做三件自校验：
  1. 每地块 dict 能被 engine.models.Plot.from_dict 接受；
  2. config 里 15 项商品能塞进 market dict（Es/Ed 为 None 时 fallback external_purchase=0）；
  3. 落盘 JSON 可以被再次打开 parse。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# 让脚本能 import 项目内部的 engine 包
ROOT = Path(__file__).resolve().parent.parent  # region-sim/
sys.path.insert(0, str(ROOT))

from engine.models import Plot, PlotType, PlotStatus  # noqa: E402

# ──────────────────────────────────────────────────────────────────
# 一、地图布局：(plot_id, plot_type, level, product)
# 工业 L1 → "G"；L2 ∈ {H-A,H-F,H-G,H-J}；L3 ∈ {H-A+,H-F+,H-G+,H-J+,I}
# 农 A/B/C/D；资源/服务/研究 product=None
# ──────────────────────────────────────────────────────────────────
PLOTS = [
    # 农业 4 产物 × 3 等级 —— 第 0~3 行，列 0-2
    ("0,0", "agriculture", 1, "A"), ("0,1", "agriculture", 2, "A"), ("0,2", "agriculture", 3, "A"),
    ("1,0", "agriculture", 1, "B"), ("1,1", "agriculture", 2, "B"), ("1,2", "agriculture", 3, "B"),
    ("2,0", "agriculture", 1, "C"), ("2,1", "agriculture", 2, "C"), ("2,2", "agriculture", 3, "C"),
    ("3,0", "agriculture", 1, "D"), ("3,1", "agriculture", 2, "D"), ("3,2", "agriculture", 3, "D"),

    # 服务三级 —— 第 0 行，列 4-6
    ("0,4", "service", 1, None),
    ("0,5", "service", 2, None),
    ("0,6", "service", 3, None),

    # 资源三级 —— 第 1 行，列 4-6
    ("1,4", "resource", 1, None),
    ("1,5", "resource", 2, None),
    ("1,6", "resource", 3, None),

    # 工业基础材料线 —— 第 2 行，列 4-6
    ("2,4", "industry", 1, "G"),
    ("2,5", "industry", 2, "H-G"),
    ("2,6", "industry", 3, "H-G+"),

    # 工业机械/研究分支二级 —— 第 6 行，列 2-4
    ("6,2", "industry", 2, "H-A"),
    ("6,3", "industry", 2, "H-F"),
    ("6,4", "industry", 2, "H-J"),

    # 对应第三级（自动化） —— 第 7 行，列 2-4
    ("7,2", "industry", 3, "H-A+"),
    ("7,3", "industry", 3, "H-F+"),
    ("7,4", "industry", 3, "H-J+"),

    # 研究路线三级 I（高技术产品） —— 第 8 行，列 3
    ("8,3", "industry", 3, "I"),

    # 研究地块 × 3（吃满 +3% 研究加成） —— 第 9 行，列 4-6
    ("9,4", "research", 1, None),
    ("9,5", "research", 1, None),
    ("9,6", "research", 1, None),
]


def build_plot_dict(plot_id: str, plot_type: str, level: int, product):
    """构造一份"已建成、已运营一段"的地块 JSON。
    running_quarters=2 → 磨合 E=1.0；construction_left=0；target_level=level。
    construction_materials 留空 dict（引擎不依赖这些物资来判定加成）。
    """
    data = {
        "plot_id": plot_id,
        "plot_type": plot_type,
        "level": level,
        "status": "running",
        "running_quarters": 2,
        "construction_left": 0,
        "target_level": level,
        "construction_materials": {},
        "product": product,
    }
    # 自校验 1：让 Plot.from_dict 接受
    Plot.from_dict(data)
    return data


def build_market(cfg_goods: dict) -> dict:
    """从 config.goods 构造 15 项 market 条目。"""
    market = {}
    for code, g in cfg_goods.items():
        market[code] = {
            "goods": code,
            "P0": float(g["P0"]),
            "Q0": float(g["Q0"]),
            "Es": None if g.get("Es") is None else float(g["Es"]),
            "Ed": None if g.get("Ed") is None else float(g["Ed"]),
            "P_min_ratio": 0.1,
            "external_purchase": 0.0,
        }
    return market


def main() -> None:
    cfg_path = ROOT / "config" / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    goods = cfg["goods"]
    market = build_market(goods)

    plots = {}
    for plot_id, plot_type, level, product in PLOTS:
        plots[plot_id] = build_plot_dict(plot_id, plot_type, level, product)

    now = datetime.now(tz=ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
    save = {
        "schema_version": 1,
        "game_id": "pizzademo",
        "saved_at": now,
        "quarter": 1,
        "total_quarters": 20,
        "state": {
            "world": {
                "quarter": 1,
                "rows": 10,
                "cols": 10,
                "total_quarters": 20,
                "phase": "setup",
                "market": market,
                "policies": [],
                "price_history": {},
                "policy_state": {"seed": 20261016, "seq": [], "mode": "regular"},
            },
            "players": {
                "p1": {
                    "player_id": "p1",
                    "cash": 1200.0,
                    "plots": plots,
                    "inventory": {},
                    "debts": [],
                    "credit_flag": False,
                    "last_happiness": 0.6,
                    "opening_quota_used": {
                        "agriculture": 0,
                        "resource": 0,
                        "industry": 0,
                        "service": 0,
                        "research": 0,
                    },
                }
            },
            "contracts": {},
        },
        "report": {},
    }

    out_path = ROOT / "saves" / "pizzademo.json"
    out_path.write_text(
        json.dumps(save, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 自校验 2：可被再次打开、关键键齐
    again = json.loads(out_path.read_text(encoding="utf-8"))
    assert again["schema_version"] == 1
    assert set(again["state"]) == {"world", "players", "contracts"}
    assert len(again["state"]["players"]["p1"]["plots"]) == 31
    assert len(again["state"]["world"]["market"]) == 15

    # 自校验 3：每种 plot_type 分布统计
    from collections import Counter
    by_type = Counter(p["plot_type"] for p in again["state"]["players"]["p1"]["plots"].values())
    print(f"OK -> {out_path}")
    print(f"  存档包含 {len(plots)} 个已建成地块")
    for t, n in sorted(by_type.items()):
        print(f"    {t:<11} x {n}")


if __name__ == "__main__":
    main()
