"""地块资产键生成：`(plot_type, level, product)` → 建模目录名。

契约变化 2026-10-17：旧的 ``<type>_l<level>`` 15 键契约**作废**，
新契约直接用 ``建模/`` 下的中文目录名作 key（建模目录名 = .glb 文件名，不含后缀）。

返回 ``None`` 表示该组合**无对应建模**（如 research level 2/3、
industry level 2/3 且 ``product=None`` —— 研究线二级地不产商品，用研究目录）。

⚠️ 本模块是**纯查表**，不做任何 IO、不读建模目录是否存在 ——
    目录与文件的对应是美术侧的责任，引擎只负责"这块地该用哪个键"。
"""

from __future__ import annotations

from typing import Optional

# ══════════════════════════════════════════════════════════════════
# 工业路线查表（含 H-A / H-F / H-G / H-J 四条机械路线
# 与独立的 level 3 研究线 product=I）
#
# ⚠️ 命名不一致点（美术侧笔误，已按实际目录对齐）：
#    · `工业服务2` ↔ `工业服务业3`：二级少个"业"字
#    · `工业高技术3`：只 level 3 一个目录（研究线 1→2→3 直通，
#      一级用 `工业1`、二级用 `研究`、三级用 `工业高技术3`）
# ══════════════════════════════════════════════════════════════════
INDUSTRY_ROUTE_TO_DIR: dict[tuple[str, int], str] = {
    ("H-A", 2): "工业农业2",
    ("H-A", 3): "工业农业3",
    ("H-G", 2): "工业工业2",
    ("H-G", 3): "工业工业3",
    ("H-F", 2): "工业资源2",
    ("H-F", 3): "工业资源3",
    ("H-J", 2): "工业服务2",
    ("H-J", 3): "工业服务业3",   # ⚠️ 注意美术目录命名笔误
    ("I", 3):  "工业高技术3",
}

# 简单类型的中文目录前缀（agriculture / resource / service 都是 `<前缀><level>`）
_SIMPLE_PREFIX = {
    "agriculture": "农业",
    "resource":    "资源",
    "service":     "服务",
}


def asset_key(plot_type: str, level: int, product: Optional[str]) -> Optional[str]:
    """返回该 plot 对应的 glb 文件名（不含 .glb 后缀），无对应则 ``None``。

    规则速览：
      · agriculture / resource / service → ``<前缀><level>``
      · industry level 1 → ``工业1``（product 应为 G，其他视为非法）
      · industry level 2 → 查 ``INDUSTRY_ROUTE_TO_DIR``，键 ``(product, 2)``；
        product 应为 ``H-A/H-F/H-G/H-J`` 基础名（工单宽松处理：若带 ``+``
        也剥掉后按基础名查）。
      · industry level 3 → product **必须**带 ``+`` 后缀（在四条机械路线里），
        或 == ``I``（独立查表）；不带加号的 ``H-*`` 在 level 3 视为非法。
      · research → 只有 level 1 的 ``研究``，其他 level 返回 ``None``

    >>> asset_key("agriculture", 1, "A")
    '农业1'
    >>> asset_key("agriculture", 2, "A")
    '农业2'
    >>> asset_key("agriculture", 2, "D")
    '农业2'
    >>> asset_key("agriculture", 3, "C")
    '农业3'
    >>> asset_key("resource", 1, "F")
    '资源1'
    >>> asset_key("resource", 3, "F")
    '资源3'
    >>> asset_key("service", 1, None)
    '服务1'
    >>> asset_key("service", 2, None)
    '服务2'
    >>> asset_key("service", 3, None)
    '服务3'
    >>> asset_key("industry", 1, "G")
    '工业1'
    >>> asset_key("industry", 2, "H-A")
    '工业农业2'
    >>> asset_key("industry", 3, "H-A+")
    '工业农业3'
    >>> asset_key("industry", 2, "H-G")
    '工业工业2'
    >>> asset_key("industry", 3, "H-G+")
    '工业工业3'
    >>> asset_key("industry", 2, "H-F")
    '工业资源2'
    >>> asset_key("industry", 3, "H-F+")
    '工业资源3'
    >>> asset_key("industry", 2, "H-J")
    '工业服务2'
    >>> asset_key("industry", 3, "H-J+")
    '工业服务业3'
    >>> asset_key("industry", 3, "I")
    '工业高技术3'
    >>> asset_key("research", 1, None)
    '研究'
    >>> asset_key("research", 2, None) is None
    True
    >>> asset_key("research", 3, None) is None
    True

    # ── 边界与非法组合 ────────────────────────────────────────────
    >>> asset_key("agriculture", 0, "A") is None   # 等级越界
    True
    >>> asset_key("agriculture", 4, "A") is None
    True
    >>> asset_key("service", 0, None) is None
    True
    >>> asset_key("industry", 1, "H-A") is None    # 一级只能产 G
    True
    >>> asset_key("industry", 1, None) is None
    True
    >>> asset_key("industry", 2, None) is None     # 研究线 1→2 不产商品 → 无建模（用"研究"）
    True
    >>> asset_key("industry", 2, "G") is None      # 二级还产 G 是非法中间态
    True
    >>> asset_key("industry", 3, "H-A") is None    # 三级应带加号；不带视作非法
    True
    >>> asset_key("industry", 3, "H-G") is None
    True
    >>> asset_key("industry", 3, "I+") is None     # I 没有"+"版本
    True
    >>> asset_key("industry", 4, "I") is None      # 没有 level 4
    True
    >>> asset_key("nonsense", 1, None) is None     # 未知类型
    True

    ⚠️ ``H-A+`` 这种"加号后缀"在 level 2 也**允许**映射到对应基础目录 —
       按 2026-10-17 工单裁决："按基础路线对待"。但同裁决的下一句话是
       **加号只在 level 3 出现**，合法状态里 level 2 不会有 ``H-A+``。
       这里宽松处理：剥掉 ``+`` 后按 level 查表，查不到就 ``None``。
    >>> asset_key("industry", 2, "H-A+")   # 宽松：剥+按基础查 level2
    '工业农业2'
    >>> asset_key("industry", 2, "H-J+")
    '工业服务2'
    """
    # ── 等级下限 ─────────────────────────────────────────────────
    if not isinstance(level, int) or level < 1:
        return None

    # ── research：只有 level 1 ───────────────────────────────────
    if plot_type == "research":
        return "研究" if level == 1 else None

    # ── 简单三类型：<前缀><level> ────────────────────────────────
    if plot_type in _SIMPLE_PREFIX:
        if level > 3:
            return None
        return f"{_SIMPLE_PREFIX[plot_type]}{level}"

    # ── industry：分 level 处理 ──────────────────────────────────
    if plot_type == "industry":
        if level > 3:
            return None
        if level == 1:
            # 一级只有一条线，产出必是 G
            return "工业1" if product == "G" else None
        if not product:
            return None
        # ★ level 3 的合法 product 只有两类：
        #     · "I"（研究线三级产物，独立查表）
        #     · "H-*+"（机械路线的三级升级版，必须带 +）
        #   "H-*" 不带加号、"I+" 加号、"I" 出现在 level 2 —— 都是非法中间态。
        #   level 2 的合法 product 是 "H-*" 基础名（工单宽松：若带 + 也剥掉
        #   按基础路线对待）。
        base_product = product.rstrip("+")
        if not base_product:
            return None
        if base_product == "I":
            # I 路线只在 level 3；且不接受 "I+" / "I++" 这类变形
            if level != 3 or product != "I":
                return None
            return INDUSTRY_ROUTE_TO_DIR.get(("I", 3))
        # 机械路线 H-A / H-F / H-G / H-J
        if base_product not in ("H-A", "H-F", "H-G", "H-J"):
            return None
        if level == 2:
            # 二级宽松：剥 + 后查；不带 + 直接查
            return INDUSTRY_ROUTE_TO_DIR.get((base_product, 2))
        # level == 3：必须显式带 +
        if not product.endswith("+"):
            return None
        return INDUSTRY_ROUTE_TO_DIR.get((base_product, 3))

    # ── 未知类型 ─────────────────────────────────────────────────
    return None
