"""glB 地块资产契约 —— **集成**测试（2026-10-17 中文目录名 20 键契约）。

跑法（在 region-sim/ 下）：

    python -X utf8 -m pytest tests/test_assets.py -v

与 ``engine/assets.py`` 的 doctest **不重叠**：doctest 已经覆盖了
``asset_key(plot_type, level, product)`` 的全部单元键映射（36 例）。本文件
锚**集成**契约：

  ① `report.all_asset_keys()` 键集合**恰好**是那 20 个中文目录名；
  ② `report.asset_manifest()` 形状：`{<key>: {"file","path","exists"}}`、
     path 固定为 ``assets/plots/<key>.glb``、20 个 glb 已交付 → 全部 ``exists=True``；
  ③ `report.build_map()` 对几种典型 plot（agriculture lv1 / lv2、industry
     lv1/lv2/lv3、research lv1/lv2、service lv3）cell.asset 是正确的中文键或 None；
  ④ 空地 asset=None、cell.asset ∈ manifest 键（前端拿键必查得到路径）；
  ⑤ `build_map` 响应的**整体结构形状不变**（rows/cols/cells/assets/virtual_plots），
     cell 仍带 plot_id/owner/mine 三键 —— 前端契约没有少字段；
  ⑥ 在建（CONSTRUCTING）与前影（pending plan builds）的 asset 按**目标等级/产出**
     计算（与"在建虚影"语义一致）。
"""

from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import report
from engine.loader import load_config
from engine.models import (
    BuildOrder,
    GameState,
    Plan,
    PlayerState,
    Plot,
    PlotStatus,
    PlotType,
    WorldPhase,
)

# ══════════════════════════════════════════════════════════════════
# 权威 20 键（与 web/assets/plots/ 实际交付的中文目录名一一对应）
# ══════════════════════════════════════════════════════════════════
EXPECTED_20 = {
    # 简单三类型（<前缀><level>，各 3 级）
    "农业1", "农业2", "农业3",
    "资源1", "资源2", "资源3",
    "服务1", "服务2", "服务3",
    # 工业一级（研究线/机械线共用一档建模）
    "工业1",
    # 工业二级（四条机械路线）
    "工业农业2", "工业工业2", "工业资源2", "工业服务2",
    # 工业三级（四条机械路线升级版 + 研究线 I）
    "工业农业3", "工业工业3", "工业资源3", "工业服务业3", "工业高技术3",
    # 研究只有一级
    "研究",
}


# ══════════════════════════════════════════════════════════════════
# 工厂
# ══════════════════════════════════════════════════════════════════

def _state_with(*plots: Plot) -> GameState:
    cfg = load_config()
    world = cfg.new_world()
    world.phase = WorldPhase.RUNNING
    player = PlayerState(
        player_id="p1", cash=1000.0,
        plots={p.plot_id: p for p in plots},
    )
    return GameState(world=world, players={"p1": player})


def _plot(
    pid: str,
    ptype: PlotType,
    level: int,
    product=None,
    status: PlotStatus = PlotStatus.RUNNING,
    target_level=None,
    construction_left: int = 0,
) -> Plot:
    return Plot(
        plot_id=pid, plot_type=ptype, level=level,
        status=status, running_quarters=99, product=product,
        target_level=target_level, construction_left=construction_left,
    )


def _cell(m: dict, pid: str) -> dict:
    for c in m["cells"]:
        if c["plot_id"] == pid:
            return c
    raise AssertionError(f"地图响应里找不到 cell {pid!r}")


# ══════════════════════════════════════════════════════════════════
# ① all_asset_keys()：键集合恰好 20 个中文目录名
# ══════════════════════════════════════════════════════════════════

def test_all_asset_keys_exactly_20_chinese_dirs():
    keys = report.all_asset_keys()
    assert set(keys) == EXPECTED_20
    assert len(keys) == 20
    # 无重复
    assert len(set(keys)) == len(keys)


def test_expected_asset_keys_constant_matches_all_asset_keys():
    """`EXPECTED_ASSET_KEYS` 常量与 `all_asset_keys()` 是同一份。"""
    assert set(report.EXPECTED_ASSET_KEYS) == set(report.all_asset_keys())


def test_old_15_english_keys_abolished():
    """旧的 15 个英文键（agri_l1 等）**不得**出现在契约的任何角落。"""
    old = {
        "agri_l1", "agri_l2", "agri_l3",
        "resource_l1", "resource_l2", "resource_l3",
        "industry_l1", "industry_l2", "industry_l3",
        "service_l1", "service_l2", "service_l3",
        "research_l1", "research_l2", "research_l3",
    }
    keys = set(report.all_asset_keys())
    assert not (keys & old)


# ══════════════════════════════════════════════════════════════════
# ② asset_manifest()：形状 + exists
# ══════════════════════════════════════════════════════════════════

def test_manifest_keys_exactly_expected_20():
    man = report.asset_manifest()
    assert set(man) == EXPECTED_20
    assert len(man) == 20


def test_manifest_shape_file_path_exists():
    man = report.asset_manifest()
    for key, entry in man.items():
        assert entry["file"] == f"{key}.glb"
        assert entry["path"] == f"assets/plots/{key}.glb"
        # 20 个 glb 已交付 → exists 全 True
        assert entry["exists"] is True, f"{key}.glb 应该在 web/assets/plots/ 下"
        # 键 = 文件名（不含 .glb 后缀）：前端按 cell.asset 拼 URL
        assert entry["file"].startswith(key)


def test_manifest_path_prefix_and_ext_constants():
    """常量契约：换目录/换扩展只改这两处。"""
    assert report.ASSETS_DIR == "assets/plots"
    assert report.ASSETS_EXT == ".glb"


# ══════════════════════════════════════════════════════════════════
# ③ report.asset_key() 适配器（枚举 ↔ 字符串都可），轻量 spot check
#    （穷举映射由 engine/assets.py doctest 负责，**这里不重复**）
# ══════════════════════════════════════════════════════════════════

#: 2026-10-17：本文件原本写成 pytest 风格（`@pytest.mark.parametrize`），
#: 但**本机没有装 pytest**，且项目其余 16 个测试全是"标准库 runner + 直接跑"的惯例。
#: 所以把参数化改成**函数内循环**，并补一个 `__main__` runner —— 零依赖，`python tests/test_assets.py` 直接跑。
_ASSET_KEY_CASES = [
    (PlotType.AGRICULTURE, 1, "A", "农业1"),
    (PlotType.AGRICULTURE, 2, "B", "农业2"),
    (PlotType.RESOURCE, 3, "F", "资源3"),
    (PlotType.SERVICE, 1, None, "服务1"),
    (PlotType.RESEARCH, 1, None, "研究"),
    (PlotType.RESEARCH, 2, None, None),               # research lv≥2 → None
    (PlotType.RESEARCH, 3, None, None),
    (PlotType.INDUSTRY, 1, "G", "工业1"),
    (PlotType.INDUSTRY, 1, "H-A", None),              # industry lv1 product≠"G" → None
    (PlotType.INDUSTRY, 1, None, None),
    (PlotType.INDUSTRY, 2, "H-A", "工业农业2"),
    (PlotType.INDUSTRY, 2, "H-A+", "工业农业2"),      # 工单宽松：剥 + 按基础路线
    (PlotType.INDUSTRY, 2, "H-J", "工业服务2"),       # ⚠️ 二级**少**"业"字
    (PlotType.INDUSTRY, 2, None, None),
    (PlotType.INDUSTRY, 2, "G", None),
    (PlotType.INDUSTRY, 3, "H-J+", "工业服务业3"),    # ⚠️ 三级**多**"业"字（美术笔误，保持）
    (PlotType.INDUSTRY, 3, "I", "工业高技术3"),
    (PlotType.INDUSTRY, 3, "H-A", None),              # lv3 机械路线必须带 +
    # ★ 2026-10-18：研究地块（最小存储改动）—— industry L2 product='研究'
    #    → 「研究」模型；内部仍编码为 INDUSTRY/L2，但建模用独立「研究」目录。
    (PlotType.INDUSTRY, 2, "研究", "研究"),
]


def test_asset_key_adapter_accepts_plot_type_enum():
    """`asset_key` 对 18 组 (类型, 等级, 产物) 的映射 —— 原 parametrize 的等价展开。"""
    for ptype, level, product, expected in _ASSET_KEY_CASES:
        got = report.asset_key(ptype, level, product)
        assert got == expected, (
            f"asset_key({ptype!r}, {level}, {product!r}) 应为 {expected!r}，实得 {got!r}"
        )


def test_asset_key_adapter_accepts_string_plot_type():
    """`report.asset_key` 也接受字符串（给 plan.builds / vplot 用）。"""
    assert report.asset_key("agriculture", 1, "A") == "农业1"
    assert report.asset_key("research", 2, None) is None
    assert report.asset_key("industry", 3, "H-J+") == "工业服务业3"


def test_industry_l2_research_product_maps_to_research_dir():
    """★ 2026-10-18 最小存储改动：研究地块内部编码为
    `industry L2 product='研究'`，但建模必须用「研究」目录，不是机械二级。
    """
    assert report.asset_key(PlotType.INDUSTRY, 2, "研究") == "研究"
    assert report.asset_key("industry", 2, "研究") == "研究"


def test_industry_service3_keeps_art_typo():
    """⚠️ 美术目录名笔误：工业服务**业**3 —— 不"修正"为 `工业服务3`。

    这是工单钦定的键：**保持真名**，与磁盘上的 ``工业服务业3.glb`` 一致。
    """
    assert report.asset_key(PlotType.INDUSTRY, 3, "H-J+") == "工业服务业3"
    assert "工业服务业3" in EXPECTED_20
    assert "工业服务3" not in EXPECTED_20  # 不要"修正"


# ══════════════════════════════════════════════════════════════════
# ④ build_map：cell.asset 与 manifest 挂接
# ══════════════════════════════════════════════════════════════════

def _representative_state() -> GameState:
    return _state_with(
        _plot("0,0", PlotType.AGRICULTURE, 1, "A"),     # 农业1
        _plot("0,1", PlotType.AGRICULTURE, 2, "B"),     # 农业2
        _plot("0,2", PlotType.RESOURCE, 3, "F"),        # 资源3
        _plot("0,3", PlotType.SERVICE, 3, None),        # 服务3
        _plot("1,0", PlotType.INDUSTRY, 1, "G"),        # 工业1
        _plot("1,1", PlotType.INDUSTRY, 2, "H-A"),      # 工业农业2
        _plot("1,2", PlotType.INDUSTRY, 2, "H-A+"),     # 工业农业2（宽松剥+）
        _plot("1,3", PlotType.INDUSTRY, 2, "H-J"),      # 工业服务2（笔误的"基础"形）
        _plot("2,0", PlotType.INDUSTRY, 3, "I"),        # 工业高技术3
        _plot("2,1", PlotType.RESEARCH, 1, None),       # 研究
        _plot("2,2", PlotType.RESEARCH, 2, None),       # ⚠️ research lv≥2 → None
        _plot("2,3", PlotType.RESEARCH, 3, None),       # ⚠️ research lv≥2 → None
        _plot("3,0", PlotType.INDUSTRY, 1, "H-A"),      # ⚠️ 非法组合 → None
    )


def test_build_map_cells_have_correct_chinese_asset_keys():
    m = report.build_map(_representative_state(), "p1")
    expected = {
        "0,0": "农业1",
        "0,1": "农业2",
        "0,2": "资源3",
        "0,3": "服务3",
        "1,0": "工业1",
        "1,1": "工业农业2",
        "1,2": "工业农业2",
        "1,3": "工业服务2",
        "2,0": "工业高技术3",
        "2,1": "研究",
        # research lv≥2 / industry lv1 product≠"G" → None（回退 2D）
        "2,2": None,
        "2,3": None,
        "3,0": None,
    }
    for pid, want in expected.items():
        assert _cell(m, pid)["asset"] == want, (
            f"cell {pid}: want asset={want!r}, got {_cell(m, pid)['asset']!r}"
        )


def test_build_map_empty_cells_have_none_asset():
    m = report.build_map(_representative_state(), "p1")
    empties = [c for c in m["cells"] if c["owner"] is None]
    assert empties, "10×10 上应当有空地"
    assert all(c["asset"] is None for c in empties)


def test_build_map_every_occupied_asset_in_manifest():
    """前端契约：**拿键必查得到路径**（除非 None）。"""
    m = report.build_map(_representative_state(), "p1")
    for c in m["cells"]:
        if c["asset"] is not None:
            assert c["asset"] in m["assets"], (
                f"cell {c['plot_id']} asset={c['asset']!r} 不在 manifest 里"
            )


# ══════════════════════════════════════════════════════════════════
# ⑤ /api/map 响应结构形状不变（前端契约没缺字段）
# ══════════════════════════════════════════════════════════════════

def test_build_map_response_structure_unchanged():
    m = report.build_map(_representative_state(), "p1")

    # 顶层五键：rows / cols / cells / assets / virtual_plots
    assert set(m) == {"rows", "cols", "cells", "assets", "virtual_plots"}
    assert m["rows"] == 10 and m["cols"] == 10
    assert len(m["cells"]) == 100
    assert isinstance(m["virtual_plots"], list)

    # manifest 形状
    assert set(m["assets"]) == EXPECTED_20

    # cell 必备三键（带地块时还有 status/level/product/plot_type）
    for c in m["cells"]:
        assert {"plot_id", "owner", "mine", "asset"} <= set(c)

    occ = next(c for c in m["cells"] if c["plot_id"] == "0,0")
    assert occ["plot_type"] == "agriculture"
    assert occ["level"] == 1
    assert occ["product"] == "A"
    assert occ["mine"] is True


# ══════════════════════════════════════════════════════════════════
# ⑥ 在建（CONSTRUCTING）与"前影"（pending plan builds）的 asset
# ══════════════════════════════════════════════════════════════════

def test_constructing_plot_asset_uses_target_level():
    """在建地块的 asset 按**目标等级**计算（与"在建虚影"语义一致）。

    旧代码给 ``level=0`` 的前影算 asset 会落到废弃的 ``agri_l0`` 上；
    新代码应当用 ``target_level``。本测试把"在建"插到真实地块上验证。
    """
    st = _state_with(
        _plot("0,0", PlotType.AGRICULTURE, 1, "A",
              status=PlotStatus.CONSTRUCTING,
              target_level=2, construction_left=2),
    )
    m = report.build_map(st, "p1")
    # 在建升到 2 级 → asset 应当是 "农业2"（目标），而不是 "农业1"（当前）
    assert _cell(m, "0,0")["asset"] == "农业2"


def test_pending_plan_builds_shadow_cells_have_target_asset():
    """计划递交后、结算前的"前影"虚地块：asset 按目标等级/产出算。"""
    st = _state_with()       # 全空地
    plan = Plan(
        player_id="p1",
        builds=[
            BuildOrder(plot_id="5,5", plot_type=PlotType.AGRICULTURE,
                       target_level=1, product="A"),
            BuildOrder(plot_id="6,6", plot_type=PlotType.INDUSTRY,
                       target_level=2, product="H-J"),
            BuildOrder(plot_id="7,7", plot_type=PlotType.RESEARCH,
                       target_level=1, product=None),
        ],
    )
    m = report.build_map(st, "p1", pending=plan)

    # 前影落到 cell 上
    assert _cell(m, "5,5")["asset"] == "农业1"
    assert _cell(m, "6,6")["asset"] == "工业服务2"
    assert _cell(m, "7,7")["asset"] == "研究"
    # 前影显影语义：状态/主人都到位
    assert _cell(m, "5,5")["status"] == "constructing"
    assert _cell(m, "5,5")["owner"] == "p1"
    assert _cell(m, "5,5")["mine"] is True
    # virtual_plots 形状不变
    assert len(m["virtual_plots"]) == 3


# ══════════════════════════════════════════════════════════════════
# 标准库 runner（2026-10-17 补）
# ══════════════════════════════════════════════════════════════════
#
# ⚠️ 本文件原本是 pytest 风格；但**本机没装 pytest**，项目其余 16 个测试
#    全是「直接 python tests/xxx.py」的零依赖惯例。这里补上同样的 runner，
#    让 `python -X utf8 tests/test_assets.py` 能独立跑完并给出 pass/fail。
#
# 另外顶部那句 `import pytest` 已不再需要（唯一用处是 parametrize，已改成循环）。

def _run_all() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
            passed += 1
        except Exception as e:  # noqa: BLE001
            print(f"  FAIL  {name}\n          {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
