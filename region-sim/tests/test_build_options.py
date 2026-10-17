"""`/api/build_options` 契约 —— 每行的 `products / fixed_product / product_from_current`。

跑法（在 region-sim/ 下）：

    python tests/test_build_options.py

★ 2026-10-18 新增（用户对「工业二级是**不同地块类型**」的裁定）：

    · 一级工业 G 升二级时，选的是**二级地块类型**：H-A/H-F/H-G/H-J，
      四种**不同地块、不同建模**，选定后**不能互转**；
    · 二级机械 H-* 升三级 → 产物**由当前地块决定**（`当前 product + '+'`），
      不是玩家在"三级下拉框"里挑的 —— 所以这条线的 `products` 必须空、`product_from_current=True`；
    · 研究线（ind_1_2_research → ind_2_3_high）**固定**产物是「研究」→「I」，
      本轮不进 UI 下拉 —— `fixed_product` 已钉死、`products` 必须空；
    · 新建工业一级固定产 **G**（没有下拉）；
    · 服务/资源/研究**不产出商品**，`products` 必须空、`fixed_product=None`；
    · 农业仍可选 A/B/C/D。

⚠️ 本文件只测**引擎给出的三个字段**，不模拟真实地块 —
   UI 用 `fixed_product` 显示"固定产物"标记、用 `product_from_current` 显示
   "由当前地块决定"标记，用 `products` 决定是否渲染下拉框，三者**互斥**。
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.loader import load_config
from server.app import App
from server.registry import GameRegistry
from storage import MemoryStorage

TOL = 1e-9


# ══════════════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════════════


def make_app() -> App:
    cfg = load_config()
    registry = GameRegistry(cfg, MemoryStorage())
    return App(cfg, registry)


def works_by_id(app: App) -> dict[str, dict]:
    body = app.build_options({})
    assert body.get("ok") is True, f"/api/build_options 必须 ok，实得：{body}"
    return {row["work_id"]: row for row in body["works"]}


# ══════════════════════════════════════════════════════════════════
# 一、整体形状：14 行 + 三个字段都在
# ══════════════════════════════════════════════════════════════════


def test_fourteen_works_carry_three_product_fields():
    """每行必须有 `fixed_product` / `product_from_current` / `products` 三个字段。"""
    rows = works_by_id(make_app())
    assert len(rows) == 14, f"应为 14 条工程，实得 {len(rows)}"
    for work_id, row in rows.items():
        for key in ("fixed_product", "product_from_current", "products"):
            assert key in row, f"{work_id} 缺字段 {key!r}"


def test_three_fields_mutually_exclusive():
    """一行**最多只能有**一种"产物来源"——固定 / 由当前 / 下拉。

    固定的行 `product_from_current=False` 且 `products=[]`；
    由当前决定的行 `fixed_product=None` 且 `products=[]`；
    下拉的行 `fixed_product=None` 且 `product_from_current=False`。
    （全 None/False/[] 的行也是合法的：服务/资源**不产出**。）
    """
    rows = works_by_id(make_app())
    for work_id, row in rows.items():
        has_fixed = row["fixed_product"] is not None
        has_current = row["product_from_current"] is True
        has_dropdown = row["products"] != []
        n_active = sum((has_fixed, has_current, has_dropdown))
        assert n_active <= 1, (
            f"{work_id} 的产物来源超过一种：fixed_product={row['fixed_product']!r} "
            f"product_from_current={row['product_from_current']} "
            f"products={row['products']}"
        )


# ══════════════════════════════════════════════════════════════════
# 二、固定产物（fixed_product）
# ══════════════════════════════════════════════════════════════════


def test_ind_new_fixed_product_is_G():
    """新建工业一级固定产 **G**，没有下拉。"""
    row = works_by_id(make_app())["ind_new"]
    assert row["fixed_product"] == "G", (
        f"ind_new.fixed_product 应为 'G'，实得 {row['fixed_product']!r}"
    )
    assert row["products"] == [], (
        f"工业一级没有下拉（固定 G），实得 {row['products']}"
    )
    assert row["product_from_current"] is False


def test_research_second_fixed_product_is_yanjiu():
    """研究线二级固定产物是「研究」（本轮不入 UI 下拉）。"""
    row = works_by_id(make_app())["ind_1_2_research"]
    assert row["fixed_product"] == "研究", (
        f"ind_1_2_research.fixed_product 应为 '研究'，实得 {row['fixed_product']!r}"
    )
    assert row["products"] == [], (
        f"研究线是固定路线，没有下拉，实得 {row['products']}"
    )
    assert row["product_from_current"] is False


def test_high_tech_third_fixed_product_is_I():
    """研究线三级固定产物是「I」（由研究线二级升上来）。"""
    row = works_by_id(make_app())["ind_2_3_high"]
    assert row["fixed_product"] == "I", (
        f"ind_2_3_high.fixed_product 应为 'I'，实得 {row['fixed_product']!r}"
    )
    assert row["products"] == [], (
        f"研究线三级固定 I，没有下拉，实得 {row['products']}"
    )
    assert row["product_from_current"] is False


# ══════════════════════════════════════════════════════════════════
# 三、由当前地块决定（product_from_current）
# ══════════════════════════════════════════════════════════════════


def test_ind_2_3_machine_product_from_current_is_True():
    """机械二级升三级：产物 = 当前二级 product + '+'，**由当前地块决定**。

    这条**没有** `fixed_product`（不同二级地块升到**不同**三级）；
    也**没有** `products` 下拉（三级不是玩家挑的，是当前地块的延续）。
    """
    row = works_by_id(make_app())["ind_2_3_machine"]
    assert row["product_from_current"] is True, (
        f"ind_2_3_machine.product_from_current 应为 True，实得 {row['product_from_current']}"
    )
    assert row["fixed_product"] is None, (
        f"机械三级不同二级类型产物不同，不该钉一个固定值，实得 {row['fixed_product']!r}"
    )
    assert row["products"] == [], (
        f"三级没有下拉（H-A 升 H-A+，不是玩家挑的），实得 {row['products']}"
    )


# ══════════════════════════════════════════════════════════════════
# 四、下拉可选（products）
# ══════════════════════════════════════════════════════════════════


def test_agriculture_has_ABCD_dropdown():
    """农业三级全部给 A/B/C/D 下拉（建后只读，所以下拉必须齐）。"""
    rows = works_by_id(make_app())
    for wid in ("agri_new", "agri_1_2", "agri_2_3"):
        row = rows[wid]
        assert row["products"] == ["A", "B", "C", "D"], (
            f"{wid}.products 应为 [A,B,C,D]，实得 {row['products']}"
        )
        assert row["fixed_product"] is None
        assert row["product_from_current"] is False


def test_industry_1_2_machine_has_four_second_level_types():
    """机械二级下拉给**四种二级地块类型**（H-A/H-F/H-G/H-J）。

    这四种是**不同地块类型**，选定后不可互转 —— 所以它们是下拉项
    （玩家要"选类型"），而不是"选产物路线"。
    """
    row = works_by_id(make_app())["ind_1_2_machine"]
    assert row["products"] == ["H-A", "H-F", "H-G", "H-J"], (
        f"ind_1_2_machine.products 应为四种二级类型，实得 {row['products']}"
    )
    assert row["fixed_product"] is None
    assert row["product_from_current"] is False


# ══════════════════════════════════════════════════════════════════
# 五、不产出商品的类型：products 必须空、fixed_product=None
# ══════════════════════════════════════════════════════════════════


def test_service_and_resource_have_no_dropdown_no_fixed():
    """服务（×3）/ 资源（×3）**不产出商品**：products 必须空、fixed_product=None。"""
    rows = works_by_id(make_app())
    for wid in ("srv_new", "srv_1_2", "srv_2_3",
                "res_new", "res_1_2", "res_2_3"):
        row = rows[wid]
        assert row["products"] == [], (
            f"{wid}.products 应为 []，实得 {row['products']}"
        )
        assert row["fixed_product"] is None, (
            f"{wid}.fixed_product 应为 None，实得 {row['fixed_product']!r}"
        )
        assert row["product_from_current"] is False


def test_industry_third_level_has_no_dropdown():
    """工业三级（机械/研究两条）都**不给三级下拉**：

      · `ind_2_3_machine`：三级由**当前二级**决定（product_from_current=True）
      · `ind_2_3_high`：三级固定是 I（fixed_product='I'）

    两条都不在产品和"由当前决定"之外的第三状态。
    """
    rows = works_by_id(make_app())
    assert rows["ind_2_3_machine"]["products"] == []
    assert rows["ind_2_3_high"]["products"] == []


# ══════════════════════════════════════════════════════════════════
# 命令行汇总（与 tests/test_server.py 同款 runner）
# ══════════════════════════════════════════════════════════════════


def _run_all() -> int:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    mod = sys.modules[__name__]
    names = sorted(n for n in dir(mod) if n.startswith("test_"))
    ok = fail = 0
    print("=" * 68)
    print("/api/build_options 三字段契约（fixed_product / product_from_current / products）")
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
