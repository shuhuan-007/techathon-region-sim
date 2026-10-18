"""生成"开局 10w 现金"demo 存档（2026-10-18 用户点单）。

- game_id = "a1"（最短、好敲）。
- 玩家 p1 现金 = 100000.0。
- 0 地块、Q1、 phase=setup（玩家第一步还能选"开新局"风格的开局动画；同时
  引擎把本局补贴当作 Q1 已发——q1 现金 = 100000，不再 +1200，免得 10w 被打乱）。
- 总季数 20。
- 政策 mode=regular（恒为常规环境，评委看起来稳定）。
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Windows 上缺 tzdata 时 ZoneInfo("Asia/Shanghai") 会炸，直接用固定 +08:00
CN_TZ = timezone(timedelta(hours=8))

ROOT = Path(__file__).resolve().parent.parent  # region-sim/
sys.path.insert(0, str(ROOT))

from engine.models import GameState  # noqa: E402


def main() -> None:
    cfg_path = ROOT / "config" / "config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    goods = cfg["goods"]

    market = {}
    for code, g in goods.items():
        market[code] = {
            "goods": code,
            "P0": float(g["P0"]),
            "Q0": float(g["Q0"]),
            "Es": None if g.get("Es") is None else float(g["Es"]),
            "Ed": None if g.get("Ed") is None else float(g["Ed"]),
            "P_min_ratio": 0.1,
            "external_purchase": 0.0,
        }

    now = datetime.now(tz=CN_TZ).isoformat(timespec="seconds")
    save = {
        "schema_version": 1,
        "game_id": "a1",
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
                "policy_state": {"seed": 20261018, "seq": [], "mode": "regular"},
            },
            "players": {
                "p1": {
                    "player_id": "p1",
                    "cash": 100000.0,            # ★ 用户点单：开局 10 万
                    "plots": {},                  # 0 地块
                    "inventory": {},
                    "debts": [],
                    "credit_flag": False,
                    "last_happiness": 0.6,
                    "opening_quota_used": {
                        "agriculture": 0, "resource": 0, "industry": 0,
                        "service": 0, "research": 0,
                    },
                }
            },
            "contracts": {},
        },
        "report": {},
    }

    out_path = ROOT / "saves-live" / "a1.json"
    out_path.write_text(
        json.dumps(save, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # 自校验：能被 storage 层接受
    sys.path.insert(0, str(ROOT))
    from storage.base import unpack
    again = json.loads(out_path.read_text(encoding="utf-8"))
    st = unpack(again)
    assert st.world.quarter == 1
    assert st.player("p1").cash == 100000.0
    assert len(st.player("p1").plots) == 0
    assert len(st.world.market) == 15
    print(f"[make_fresh_start_save] {out_path}  现金={st.player('p1').cash}")


if __name__ == "__main__":
    main()
