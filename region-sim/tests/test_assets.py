"""glB 地块资产契约 —— 键的形状、覆盖、manifest（docs/03-架构/glB地块资产接口.md）。

跑法（在 region-sim/ 下）：

    python -X utf8 tests/test_assets.py

断言三件事：
  ① 地图 JSON 里每块**有地块的格子**都有合法的 asset key（空地 = None）；
  ② 资产键集合**恰好 15 个**（5 类型 × 3 级，同级产出共用一张模型）；
  ③ manifest 覆盖全部键、路径符合 `assets/plots/<key>.glb` 约定。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import report
from engine.models import (
    GameState,
    PlayerState,
    Plot,
    PlotStatus,
    PlotType,
    WorldPhase,
)
from engine.loader import load_config

#: 键的合法形状（小写 + 数字 + 下划线；产出段可选）
KEY_RE = re.compile(r"^[a-z0-9]+(_[a-z0-9]+)*$")

#: 15 个键，写死在这里 —— 谁把键改多/改少，这个测试就该红
EXPECTED_15 = {
    "agri_l1", "agri_l2", "agri_l3",
    "resource_l1", "resource_l2", "resource_l3",
    "industry_l1", "industry_l2", "industry_l3",
    "service_l1", "service_l2", "service_l3",
    "research_l1", "research_l2", "research_l3",
}

RESULTS: list[tuple[str, bool]] = []


def check(name: str, cond: bool) -> None:
    RESULTS.append((name, cond))
    print(f"  {'pass' if cond else 'FAIL'}  {name}")


def _state_with(*plots: Plot) -> GameState:
    cfg = load_config()
    world = cfg.new_world()
    world.phase = WorldPhase.RUNNING
    player = PlayerState(
        player_id="p1", cash=1000.0,
        plots={p.plot_id: p for p in plots},
    )
    return GameState(world=world, players={"p1": player})


def _plot(pid: str, ptype: PlotType, level: int, product=None) -> Plot:
    return Plot(plot_id=pid, plot_type=ptype, level=level,
                status=PlotStatus.RUNNING, running_quarters=99, product=product)


def main() -> int:
    print("== glB 地块资产契约 ==")

    # ── ① 键集合恰好是那 15 个 ──
    keys = report.all_asset_keys()
    check("all_asset_keys() 恰好 15 个", len(keys) == 15)
    check("all_asset_keys() 内容 == 写死的 15 键", set(keys) == EXPECTED_15)
    check("键全部匹配小写下划线形状", all(KEY_RE.match(k) for k in keys))
    check("键无重复", len(set(keys)) == len(keys))

    # ── ② 同级不同产出 → 同一个键（产出当前不进键）──
    check(
        "农业 A/B/C/D 同级共用一键",
        len({report.asset_key(PlotType.AGRICULTURE, 1, p) for p in ("A", "B", "C", "D")}) == 1,
    )
    check(
        "工业 2 级四条路线共用一键",
        len({report.asset_key(PlotType.INDUSTRY, 2, p) for p in ("H-A", "H-F", "H-G", "H-J")}) == 1,
    )
    check(
        "product=None 不炸（服务/研究）",
        report.asset_key(PlotType.SERVICE, 3) == "service_l3",
    )

    # ── ③ 29 组合 → 15 键的映射（地块总表 §二）──
    combos_29: list[tuple[PlotType, int, str | None]] = []
    for lvl in (1, 2, 3):
        for prod in ("A", "B", "C", "D"):
            combos_29.append((PlotType.AGRICULTURE, lvl, prod))          # 农业 12
        combos_29.append((PlotType.RESOURCE, lvl, "F"))                  # 资源 3
        combos_29.append((PlotType.SERVICE, lvl, None))                  # 服务 3
    combos_29.append((PlotType.INDUSTRY, 1, "G"))                        # 工业一级 1
    for prod in ("H-A", "H-F", "H-G", "H-J"):
        combos_29.append((PlotType.INDUSTRY, 2, prod))                   # 工业二级 4
    for prod in ("H-A+", "H-F+", "H-G+", "H-J+", "I"):
        combos_29.append((PlotType.INDUSTRY, 3, prod))                   # 工业三级 5
    combos_29.append((PlotType.RESEARCH, 1, None))                       # 研究 1
    check("对照组合恰好 29 个", len(combos_29) == 29)
    mapped = {report.asset_key(t, l, p) for t, l, p in combos_29}
    check("29 组合映射到的键 ⊆ 15 键", mapped <= EXPECTED_15)
    # 29 组合实际落到 13 个键：agri/resource/service/industry 各 3 级（12）+ research_l1。
    # research_l2 / research_l3 是**预留键**（配方层研究只有 1 级，美术层按 3 级备图）。
    check(
        "29 组合实际只用到 13 键（research_l2/l3 是预留）",
        len(mapped) == 13,
    )

    # ── ④ manifest 覆盖全部键、路径贴合约定 ──
    man = report.asset_manifest()
    check("manifest 键集 == all_asset_keys()", set(man) == set(keys))
    check(
        "manifest 每条都给 file + path，且 path = assets/plots/<key>.glb",
        all(
            m.get("file") == k + ".glb" and m.get("path") == f"assets/plots/{k}.glb"
            for k, m in man.items()
        ),
    )

    # ── ⑤ 地图 JSON：每块地都有合法键，空地 = None，键 ⊆ manifest ──
    st = _state_with(
        _plot("0,0", PlotType.AGRICULTURE, 1, "A"),
        _plot("0,1", PlotType.AGRICULTURE, 2, "C"),     # 同类型不同产出 → 与 A 同级不同键？仅键内同级一致
        _plot("1,0", PlotType.INDUSTRY, 2, "H-G"),
        _plot("1,1", PlotType.SERVICE, 3),
        _plot("2,2", PlotType.RESEARCH, 1),
    )
    m = report.build_map(st, "p1")
    check("build_map 响应里有 assets 子对象", isinstance(m.get("assets"), dict))
    check("响应 assets == asset_manifest()", m["assets"] == man)

    cells = m["cells"]
    check("地图仍是 10×10 = 100 格", len(cells) == 100)
    occupied = [c for c in cells if c["owner"] is not None]
    empty = [c for c in cells if c["owner"] is None]
    check("有地的 5 格 asset 都不是 None", all(c["asset"] for c in occupied))
    check("空地 asset 是 None（回退信号）", all(c["asset"] is None for c in empty))
    check(
        "每格 asset ∈ manifest（前端拿键必查得到路径）",
        all(c["asset"] is None or c["asset"] in m["assets"] for c in cells),
    )
    check(
        "cell 里的键都匹配形状",
        all(c["asset"] is None or KEY_RE.match(c["asset"]) for c in cells),
    )
    # 同级不同产出在 cell 上也落同一个键
    agri_keys = {c["asset"] for c in occupied if c["plot_id"] in ("0,0",)}
    check("农业 1 级格子 asset == agri_l1", agri_keys == {"agri_l1"})
    ind = next(c for c in occupied if c["plot_id"] == "1,0")
    check("工业 2 级 H-G 格子 asset == industry_l2", ind["asset"] == "industry_l2")

    # ── 汇总 ──
    failed = [n for n, ok in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
    if failed:
        for n in failed:
            print(f"  FAIL: {n}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
