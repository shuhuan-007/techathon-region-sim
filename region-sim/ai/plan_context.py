"""AI 可行动作快照 —— 让模型看见「能往哪建、建得起什么、哪块能开工、要多少口粮」。

依据《AI 秘书 · 有效起草方案（2026-10-18）》§三.2：真实故障里，模型只看到
玩家**自己的**地块，于是把已有的 `4,5` 等写成 `builds`（校验报
`build_not_an_upgrade`）；也看不到建造成本 / 工程物资 / 人口口粮需求。

对外接口：

    describe_capabilities(state, player_id, cfg, *, max_empty=10) -> dict
        结构化快照。数字全部来自 engine / config，**不改任何状态**。

    format_capabilities(cap) -> str
        紧凑中文文本，块名固定（`[现金/预算]` `[空地]` `[推荐空地]` `[可建]`
        `[可开工]` `[可升级]` `[在建]` `[人口/口粮]` `[口粮规则]`），
        供提示词直接拼接。

    recommend_build_cells(state, player_id, cfg, *, limit=5, plot_type=None) -> list
        空地按**引擎真实邻接加成**排序（2026-10-18 新增）。

    adjacency_bonus_for_new_plot(state, player_id, cfg, plot_id, plot_type)
        「在这块空地新建一个 X 能吃到多少加成」——编译器自动选址用。

═══════════════════════════════════════════════════════════════════
2026-10-18 新增的两块事实（用户反馈：AI 会填，但完全不理解机制）
═══════════════════════════════════════════════════════════════════

    A. 口粮怎么算 —— 真跑里模型把「22 人」写成 `food={"A": 22}`。
       现在快照直接给 `food_rule`：F_基准 / F_优秀 与两档逐商品需求量，
       并附一句人话（F 是等效篮位数；A/B 基准，C/D 只优秀档；多买无仓库
       会丢弃）。文本块 `[口粮规则]` 把当前数字与公式一起写出来。

    B. 建在哪 —— AI 总选左上角 `0,0`，不会贴着已有地块吃邻接加成。
       现在快照给 `recommended_build_cells`：按**引擎的 compute_adjacency**
       （八邻格 / 关联表 / 服务按等级取率 / 封顶）对每块空地算「新建后能
       吃到多少加成」，取前 5 个。数字全部来自引擎，不在这里重写公式。

⚠️ 本模块不做的事（边界）：

    · 不扣钱、不建地块、不写存档 —— 纯只读，与 `ai/tools.py` 的只读工具同层；
    · 不替引擎判断「这条建造合不合法」（那是 `engine/validate.py` 的事）；
    · 不发明数字：材料价一律走 `state.world.last_price(g)`（**没有历史价时它
      自己退回参考价 P0**，见 `engine/models.py::WorldState.last_price`），
      人口 / 口粮一律走 `engine.population.compute_population()`。

⚠️ 已知对齐点（本快照如实转述，不在这一层修）：

    · 工业二级「研究线」在引擎里用 `product="研究"` 表示，但
      `validate.buildable_products()` 不列研究线 —— 本快照的 `products`
      按该函数**原样**给，不自己补一个「研究」（该快照只列新建一级工程，
      当前配置下新建工业只有 `G`）；
    · 地块的 `product` 原样给。资源 / 服务 / 研究地块通常为 `None`，
      文本里显示「—」。

═══════════════════════════════════════════════════════════════════
关于展示值（只影响 format_capabilities 的文本，不影响 dict）
═══════════════════════════════════════════════════════════════════

dict 里的数字一律是引擎原值（不四舍五入）。但浮点会带尾巴
（如 `0.4 × 1.2 = 0.48000000000000004`），直接塞给模型既难读又容易
被当成两个不同的数。所以**仅展示**用 `round(x, 3)` 抹掉浮点噪声，
并在文本里保留三位以内的小数；这不是改值，账以 dict / 引擎为准。
"""

from __future__ import annotations

from typing import Any, Optional

from engine import construction as C
from engine import efficiency as E
from engine import population as P
from engine.loader import Config
from engine.models import GameState, Plot, PlotStatus, PlotType, parse_plot_id
from engine.validate import buildable_products


# ══════════════════════════════════════════════════════════════════
# 一、小工具：稳定排序 / 展示格式
# ══════════════════════════════════════════════════════════════════


def _sort_key(plot_id: str) -> tuple:
    """地块 ID 的排序键：**按行列顺序**（`0,2` 在 `0,10` 之前）。

    字符串排序在 10×10 地图上恰好也一致，但那是巧合；这里用
    `parse_plot_id` 显式按 (行, 列) 排，换地图尺寸也稳。
    坏 ID 不炸整份快照 —— 退到末尾，按字符串排。
    """
    try:
        row, col = parse_plot_id(plot_id)
    except (TypeError, ValueError):
        return (1, 0, 0, str(plot_id))
    return (0, row, col, "")


def _sorted_plot_ids(plot_ids) -> list[str]:
    """地块 ID 列表 → 按行列顺序排好的副本。"""
    return sorted(plot_ids, key=_sort_key)


def _display_num(value: Any) -> str:
    """**仅展示**用的数字格式：整数去掉 `.0`，小数最多三位（见模块顶注）。"""
    if isinstance(value, bool):
        return str(value)
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    if num.is_integer():
        return str(int(num))
    return f"{round(num, 3):g}"


def _material_text(materials: dict[str, float]) -> str:
    """工程物资 → `H-A×1` 这种短写法；没有物资写 `—`。"""
    if not materials:
        return "—"
    return ",".join(f"{goods}×{_display_num(qty)}" for goods, qty in sorted(materials.items()))


#: 地块类型的中文名（**只用于提示词**；`PlotType.value` 仍是机读键）
PLOT_TYPE_NAMES: dict[PlotType, str] = {
    PlotType.AGRICULTURE: "农业",
    PlotType.RESOURCE: "资源",
    PlotType.INDUSTRY: "工业",
    PlotType.SERVICE: "服务",
    PlotType.RESEARCH: "研究",
}


def plot_type_name(value: Any) -> str:
    """`PlotType` / 字符串 → 中文名；认不出就原样转字符串（不炸快照）。"""
    try:
        return PLOT_TYPE_NAMES[PlotType(value)]
    except (ValueError, KeyError):
        return str(value)


def format_neighbors(neighbors: list[dict], limit: int = 3) -> str:
    """邻接贡献 → `5,6 农业+5% / 5,5 工业+5%`（最多 limit 个，余下写 …）。"""
    neighbors = list(neighbors)
    shown = neighbors[: max(0, int(limit))]
    text = " / ".join(
        f"{n.get('plot_id')} {plot_type_name(n.get('plot_type'))}"
        f"+{_display_num(n.get('bonus_pct', 0))}%"
        for n in shown
    )
    if len(neighbors) > len(shown):
        text += " …"
    return text or "—"


# ══════════════════════════════════════════════════════════════════
# 二、快照的四个子块（纯函数，只读 state / player / cfg）
# ══════════════════════════════════════════════════════════════════


def _plot_rows(player) -> list[dict]:
    """该玩家自己的地块，按 plot_id 行列顺序。

    `can_run` / `can_upgrade` 是**引擎口径**，不是"看着像"：

        can_run     = status == running
        can_upgrade = running 且 level < 3（在建 / 空地都不能升级指令）
        next_level  = 能升时 level + 1，否则 None
    """
    rows: list[dict] = []
    for plot_id in _sorted_plot_ids(player.plots):
        plot = player.plots[plot_id]
        running = plot.status is PlotStatus.RUNNING
        can_upgrade = running and plot.level < 3
        rows.append({
            "plot_id": plot.plot_id,
            "plot_type": plot.plot_type.value,
            "level": int(plot.level),
            "status": plot.status.value,
            "product": plot.product,
            "can_run": bool(running),
            "can_upgrade": bool(can_upgrade),
            "next_level": int(plot.level) + 1 if can_upgrade else None,
        })
    return rows


def _quota_table(player, cfg: Config) -> dict[str, dict]:
    """开局减免名额：`{类型: {used, total, remaining}}`。

    名额**按类型独立、跨季累计**（§5.3），所以 `used` 读存档里的
    `player.opening_quota_used`，`remaining` 直接复用
    `construction.opening_quota_remaining()`。

    ⚠️ 只列出 config 里配了 `opening.discount_quota` 的类型 —— 没配的类型
       就是"没有名额"，不凭空造一个 0/0 的条目。
    """
    table: dict[str, dict] = {}
    for name in sorted(cfg.opening_quota or {}):
        ptype = PlotType(name)
        table[name] = {
            "used": int((getattr(player, "opening_quota_used", None) or {}).get(name, 0)),
            # total 复用 `_quota_of()`（`opening_quota_remaining()` 内部用的是同一份），
            # 避免在这里再抄一遍 `max(0, int(...))` 造成两处口径漂移。
            "total": int(C._quota_of(cfg, ptype)),
            "remaining": int(C.opening_quota_remaining(player, cfg, ptype)),
        }
    return table


def _materials_value(state: GameState, materials: dict[str, float]) -> float:
    """工程物资按**上季价**折成现金（没有历史价 → `last_price` 自己退回 P0）。

    原样乘加，不取整、不替换商品；缺商品会让 `last_price` 报错（数据问题
    就该显形，不能悄悄按 0 元算）。
    """
    total = 0.0
    for goods, qty in materials.items():
        total += float(qty) * float(state.world.last_price(goods))
    return total


def _build_options(state: GameState, player, cfg: Config, quota: dict[str, dict]) -> list[dict]:
    """只列「新建」工程（`from_level == 0`），按 plot_type 排序。

    折后现金的判定与 `construction.is_opening_discount()` 同口径：

        · 只对**新建一级**（`w.is_new and w.to_level == 1`）；
        · 该类型名额还有剩（`remaining > 0`）；
        · 折扣率 < 1（没配折扣率 = 原价，不猜）。

    ⚠️ 同一季建第 4 块会恢复原价 —— 那是**按 plan.builds 顺序**逐条消耗名额
       算出来的；本快照只说"现在还剩几个"，不预演本季会建几块。
    """
    works = C.load_works(cfg)
    rate = C._discount_rate(cfg)
    rows: list[dict] = []

    for work in sorted(
        works.values(),
        key=lambda w: (w.plot_type.value, w.to_level, w.work_id),
    ):
        if not work.is_new:
            continue

        remaining = int(quota.get(work.plot_type.value, {}).get("remaining", 0))
        discounted = (
            work.is_new
            and work.to_level == 1
            and remaining > 0
            and rate < 1.0
        )
        cash_after = float(work.cash * rate) if discounted else float(work.cash)

        materials = {goods: float(qty) for goods, qty in work.materials.items()}
        materials_cost = _materials_value(state, materials)

        rows.append({
            "plot_type": work.plot_type.value,
            "label": work.label,
            "target_level": int(work.to_level),
            "cash": float(work.cash),
            "cash_after_discount": cash_after,
            "materials": materials,
            "materials_cost": materials_cost,
            "total_cost": cash_after + materials_cost,
            # 可选产出 / 路线：**原样**取自 validate.buildable_products()。
            # 资源 / 服务 / 研究没有多路线 → []（文本里显示 —）。
            "products": list(buildable_products(work.plot_type, work.to_level)),
            "quota_remaining": remaining,
        })
    return rows


#: 研究地块的内部编码（与 server / validate 同口径，但**不 import server**）
RESEARCH_PRODUCT = "研究"


def _upgrade_options(state: GameState, player, cfg: Config) -> list[dict]:
    """每块**可升级**（running 且 level<3）地块当前能走的工程。

    背景（2026-10-18）：AI 秘书只看到「新建工程」，看不到自己已有地块的
    升级选项，于是永远不会主动起草「G→二级机械 / G→二级研究 / 二级→三级」。
    本块把这张表喂进去。

    行形状（每项字段齐）：

        plot_id / plot_type / level / product          —— 起点地块
        work_id / label / target_level                 —— 工程行（来自 construction.works）
        cash / materials / quarters                    —— 工程数值（原样，无名额折扣）
        products          —— 需要玩家选的产出（无下拉 → []）
        fixed_product     —— 建后固定的产出（G→研究→「研究」、研究→三级→I）
        product_from_current —— True 时 target = 当前 product + '+'（ind_2_3_machine）

    判定全部走 `construction.load_works()`（按 plot_type + from_level 筛）——
    工业的特殊性（1→2 两路线互斥、2→3 由**当前**二级类型决定）在这里
    用与 `find_work_for_build` 同口径的规则拆：

        · 工业 L1（产 G）：同时给 machine（products=H-A/H-F/H-G/H-J）
          与 research（fixed_product=研究，6 季）两条；
        · 工业 L2 产「研究」：只给 ind_2_3_high（fixed_product=I）；
        · 工业 L2 产 H-A/H-F/H-G/H-J：只给 ind_2_3_machine
          （product_from_current=True，目标是当前 product + '+'，不可互转）；
        · 农业 / 资源 / 服务：一条线升到底；农业每档可重选 A/B/C/D，
          资源 / 服务 products=[]。

    ⚠️ **只读**：不改 state；升级工程不吃开局减免名额（§5.3 只作用于新建
       一级），所以这里不列折后价。
    """
    works = C.load_works(cfg)
    rows: list[dict] = []
    for plot_id in _sorted_plot_ids(player.plots):
        plot = player.plots[plot_id]
        running = plot.status is PlotStatus.RUNNING
        if not (running and plot.level < 3):
            continue
        candidates = [
            w for w in works.values()
            if not w.is_new
            and w.plot_type is plot.plot_type
            and w.from_level == int(plot.level)
        ]
        for w in sorted(candidates, key=lambda x: (x.to_level, x.work_id)):
            products: list[str] = []
            fixed_product: Optional[str] = None
            product_from_current = False

            if w.plot_type is PlotType.INDUSTRY:
                if w.route == "machine" and w.from_level == 1:
                    # 二级机械：四选一（H-A/H-F/H-G/H-J），选定不可互转
                    products = list(buildable_products(
                        w.plot_type, w.to_level, route=w.route,
                    ))
                elif w.route == "research" and w.from_level == 1:
                    # 二级研究线：固定产出「研究」，不产商品
                    fixed_product = RESEARCH_PRODUCT
                elif w.route == "research" and w.from_level == 2:
                    # 研究→三级高技术：固定 I；**只有**研究地块能走
                    if plot.product != RESEARCH_PRODUCT:
                        continue
                    fixed_product = "I"
                elif w.route == "machine" and w.from_level == 2:
                    # 机械→对应 + ：**由当前地块类型决定**，跨类型被
                    # build_product_mismatch 拦；研究地块不能走这条
                    if plot.product in (None, RESEARCH_PRODUCT):
                        continue
                    product_from_current = True
            elif w.plot_type is PlotType.AGRICULTURE:
                # 升级时允许重选 A/B/C/D（与 /api/build_options 同口径）
                products = list(buildable_products(
                    w.plot_type, w.to_level, route=w.route,
                ))
            # 资源 / 服务：没有多路线 → products=[]

            rows.append({
                "plot_id": plot.plot_id,
                "plot_type": plot.plot_type.value,
                "level": int(plot.level),
                "product": plot.product,
                "work_id": w.work_id,
                "label": w.label,
                "target_level": int(w.to_level),
                "cash": float(w.cash),
                "materials": {g: float(q) for g, q in w.materials.items()},
                "quarters": int(w.quarters),
                "products": products,
                "fixed_product": fixed_product,
                "product_from_current": product_from_current,
            })
    return rows


def _population_rows(state: GameState, player_id: str, cfg: Config) -> tuple[dict, dict]:
    """人口五项 + 两档口粮需求（纯来自 `compute_population` 与 config 配比）。"""
    pop = P.compute_population(state, player_id, {}, cfg)
    population = {
        "labor_total": float(pop.labor_total),
        "labor_food": float(pop.labor_food),
        "population": float(pop.population),
        "baseline_food": float(pop.baseline_food),
        "good_food": float(pop.good_food),
    }
    food_need = {
        "baseline": {
            goods: float(ratio) * population["baseline_food"]
            for goods, ratio in sorted((cfg.food.baseline_quota or {}).items())
        },
        "good": {
            goods: float(ratio) * population["good_food"]
            for goods, ratio in sorted((cfg.food.good_quota or {}).items())
        },
    }
    return population, food_need


def _food_rule(population: dict, food_need: dict, cfg: Config) -> dict:
    """口粮规则（结构化）—— 让模型看得懂「怎么算」，而不是把人数当口粮数。

    用户反馈：真跑里模型把「22 人」写成 `food={"A": 22}`。
    这里把**当前数字**（F_基准 / F_优秀）与**两档逐商品需求量**一起给出，
    再附一句人话说明：F 是等效篮位数、A/B 是基准档、C/D 只在优秀档、
    多买没有仓库会被丢弃。

    ⚠️ 数字全部来自 `_population_rows()`（其上游是 `compute_population`），
       本函数只做「按 config 配比乘 F」，不另算一套。
    """
    baseline_food = float(population.get("baseline_food", 0.0))
    good_food = float(population.get("good_food", 0.0))
    baseline_need = {
        goods: float(qty) for goods, qty in (food_need.get("baseline") or {}).items()
    }
    good_need = {
        goods: float(qty) for goods, qty in (food_need.get("good") or {}).items()
    }

    def quota_text(quota: dict) -> str:
        return "、".join(
            f"{goods}×{_display_num(qty)}" for goods, qty in sorted(quota.items())
        ) or "—"

    base_goods = set(cfg.food.baseline_quota or {})
    extra_goods = [g for g in sorted(cfg.food.good_quota or {}) if g not in base_goods]
    tiers = f"{'/'.join(sorted(base_goods)) or '—'} 基准"
    if extra_goods:
        tiers += f"，{'/'.join(extra_goods)} 只优秀档"
    note = (
        "F 是等效篮位数；每档按配额乘 F："
        f"基准档（{quota_text(cfg.food.baseline_quota or {})}）× F_基准，"
        f"优秀档（{quota_text(cfg.food.good_quota or {})}）× F_优秀"
        f"（{tiers}）；多买没有仓库、结算会丢弃。"
    )

    return {
        "baseline_food": baseline_food,
        "good_food": good_food,
        "baseline_need": baseline_need,
        "good_need": good_need,
        "note": note,
    }


# ══════════════════════════════════════════════════════════════════
# 二-a、[开局建议]（2026-10-18 新增）——「这局还在开局吗？建议几块？」
# ══════════════════════════════════════════════════════════════════
#
# 用户反馈（2026-10-18）：AI 开局只给 2 块地就跑不动了 —— 模型没有
# 「开局要铺 3–4 块」的先验。用户已批准「AI 3–4 块」；**数字仍以
# [可建] / [推荐空地] 为准**，本节只给人形规则。
#
# 触发条件（三个同时满足）：
#   · 自己地块 0–1 块（开局刚过 / 还没真铺）；
#   · 现金 ≥ 1000（按 config.world.opening_cash 给 1200 是稳的）；
# 触发时输出一句「至少 3 块、优先 1 农业 + 1 资源 + 1 工业」的提示；
# 现金低于 1000 / 自己地块已经 ≥ 2 → **不再输出本节**（说"没钱铺 3 块"是添乱）。


def opening_advice(state: GameState, player_id: str, cfg: Config) -> Optional[dict]:
    """开局阶段的「铺几块、优先什么类型」建议（**不触发就返回 None**）。

    只读：不改 state。返回形状：

        active              —— True（触发时才有这条）
        own_plot_count      —— 自己地块数（0 或 1）
        cash                —— 当前现金
        suggested_plot_count —— 建议本季铺的总数（等于 3）
        priority_types      —— 「农业 / 资源 / 工业」三键各 1（顺序即优先级）
        note                —— 一句人话（数字仍以 [可建]/[推荐空地] 为准）
    """
    player = state.player(player_id)
    own_plot_count = len(player.plots)
    cash = float(player.cash)
    if own_plot_count >= 2 or cash < 1000.0:
        return None
    return {
        "active": True,
        "own_plot_count": int(own_plot_count),
        "cash": cash,
        "suggested_plot_count": 3,
        "priority_types": ["agriculture", "resource", "industry"],
        "note": (
            "开局建议至少铺 3 块（优先 1 农业 + 1 资源 + 1 工业），"
            "不要只建 2 块就收手；具体建哪格 / 建得起哪几种，仍以 "
            "[可建] 与 [推荐空地] 为准。"
        ),
    }


# ══════════════════════════════════════════════════════════════════
# 二-b、推荐空地（邻接）—— 引擎怎么算，就怎么用
# ══════════════════════════════════════════════════════════════════
#
# 用户反馈（B）：AI 建场地总选左上角第一格，不会贴着已有地块吃邻接加成。
#
# 本模块**不自己发明邻接规则**，只把引擎的 `compute_adjacency()` 拿来问
# 「如果在这块空地上新建一个 X，能吃到多少加成」——探测用的 `Plot` 是
# 临时对象，不写回 `state`。于是八邻格、关联表、服务按自身等级取率、
# 封顶这些细节全部与结算时一致。


def _new_build_types(cfg: Config) -> list[PlotType]:
    """能从空地**新建**（0→1）的地块类型 —— 从工程表推，不写死。"""
    works = C.load_works(cfg)
    return sorted(
        {w.plot_type for w in works.values() if w.is_new},
        key=lambda t: t.value,
    )


def adjacency_bonus_for_new_plot(
    state: GameState,
    player_id: str,
    cfg: Config,
    plot_id: str,
    plot_type: PlotType,
) -> tuple[float, list[dict]]:
    """「在这块空地上新建一个 `plot_type` 一级地块」能吃到的邻接加成。

    直接调 `engine.efficiency.compute_adjacency()`（引擎真实规则），
    **不在这里重写公式**。返回 `(加成, 邻居明细)`：
    加成是**封顶后**的原始比例（0.05 = +5%），明细为
    `[{plot_id, plot_type, level, bonus_pct}]`，只含真正计入的邻居。

    ⚠️ 探测用的 `Plot` 是临时对象：不进 `state.players[...].plots`、
       不改任何状态。邻接只认**自己的**运行中地块（与引擎同口径）。
    """
    probe = Plot(
        plot_id=plot_id,
        plot_type=plot_type,
        level=1,
        status=PlotStatus.RUNNING,
    )
    _adjacency, contribs, _counted, bonus, _capped, rate = E.compute_adjacency(
        state, player_id, probe, cfg,
    )
    per_pct = (
        rate if plot_type is PlotType.SERVICE
        else cfg.efficiency.adjacency_per_neighbor
    ) * 100.0
    neighbors = [
        {
            "plot_id": c.plot_id,
            "plot_type": c.plot_type.value,
            "level": c.level,
            "bonus_pct": per_pct,
        }
        for c in contribs
        if c.counts and c.plot_type is not None
    ]
    return float(bonus), neighbors


def recommend_build_cells(
    state: GameState,
    player_id: str,
    cfg: Config,
    *,
    limit: int = 5,
    plot_type: Optional[PlotType] = None,
) -> list[dict]:
    """把「当前空地」按**引擎真实邻接加成**排序，返回前 `limit` 个可建格。

    元素形状：`{plot_id, score, bonus_pct, neighbors:[{...}]}`
    （`score` / `bonus_pct` 都是**加成**：0.05 = 5%）。

    · 只考虑**当前空地**（任何人未占的格子）；已有地块不出现；
    · `plot_type=None` → 对每种**可新建**类型各算一遍、取最好的一档
      （提示词只需要知道「这块地值得建」，具体类型由模型选）；
    · `plot_type=X` → 只算 X（编译器自动选址用：必须真搬得划算）；
    · 加成为 0 的格子不进推荐（开局没有邻居 → 空列表 → 文本写「暂无」）；
    · 排序：加成降序，同分按 (行,列) 升序 —— 稳定、可复现。
    """
    occupied = {
        plot_id
        for other in state.players.values()
        for plot_id in other.plots
    }
    types = [plot_type] if plot_type is not None else _new_build_types(cfg)
    if not types:
        return []

    rows: list[dict] = []
    for pid in _sorted_plot_ids(state.world.all_plot_ids()):
        if pid in occupied:
            continue
        best_bonus = 0.0
        best_neighbors: list[dict] = []
        for ptype in types:
            bonus, neighbors = adjacency_bonus_for_new_plot(
                state, player_id, cfg, pid, ptype,
            )
            if bonus > best_bonus + 1e-12:
                best_bonus = bonus
                best_neighbors = neighbors
        if best_bonus <= 1e-12:
            continue
        rows.append({
            "plot_id": pid,
            "score": best_bonus,
            "bonus_pct": best_bonus * 100.0,
            "neighbors": best_neighbors,
        })

    rows.sort(key=lambda row: (-row["score"], _sort_key(row["plot_id"])))
    return rows[: max(0, int(limit))]


# ══════════════════════════════════════════════════════════════════
# 三、公开接口 1：结构化快照
# ══════════════════════════════════════════════════════════════════


def describe_capabilities(
    state: GameState,
    player_id: str,
    cfg: Config,
    *,
    max_empty: int = 10,
) -> dict:
    """把"这个玩家本季能做什么"压成一份 dict（**只读**，不改 state）。

    返回键（形状是主实现依赖的，别删键）：

        player_id / quarter / cash
        empty_total / empty_cells      —— 全图空地（任何人未占的格子）
        plots                          —— 自己的地块（含 can_run / can_upgrade）
        build_options                  —— 新建工程（含材料价与折后现金）
        upgrade_options                —— 已有地块的**升级**工程（工业分两条线）
        quota                          —— 开局减免名额
        population / food_need         —— 岗位、人口、两档口粮需求
        food_rule                      —— 口粮规则（F 与两档逐商品需求量 + 人话）
        opening_advice                 —— ★ 开局「至少 3 块、优先农+资+工」建议（不触发为 None）
        recommended_build_cells        —— 按邻接加成排序的推荐空地（前 5）

    `max_empty` 只影响 `empty_cells` 的截断；`empty_total` 永远是全量。
    """
    player = state.player(player_id)

    # ── 空地：全图格子 − 所有玩家已占的格子（不区分地块状态）──────────
    occupied = {
        plot_id
        for other in state.players.values()
        for plot_id in other.plots
    }
    empty_all = [
        plot_id
        for plot_id in _sorted_plot_ids(state.world.all_plot_ids())
        if plot_id not in occupied
    ]
    limit = max(0, int(max_empty))

    quota = _quota_table(player, cfg)
    population, food_need = _population_rows(state, player_id, cfg)

    return {
        "player_id": player_id,
        "quarter": int(state.world.quarter),
        "cash": float(player.cash),
        "empty_total": len(empty_all),
        "empty_cells": empty_all[:limit],
        "plots": _plot_rows(player),
        "build_options": _build_options(state, player, cfg, quota),
        "upgrade_options": _upgrade_options(state, player, cfg),
        "quota": quota,
        "population": population,
        "food_need": food_need,
        "food_rule": _food_rule(population, food_need, cfg),
        "opening_advice": opening_advice(state, player_id, cfg),
        "recommended_build_cells": recommend_build_cells(state, player_id, cfg),
    }


# ══════════════════════════════════════════════════════════════════
# 四、公开接口 2：紧凑中文文本（提示词直接拼）
# ══════════════════════════════════════════════════════════════════
#
# ⚠️ 块名（[现金/预算] [空地] [推荐空地] [可建] [可开工] [可升级] [在建]
#    [人口/口粮] [口粮规则]）是**契约**：主实现（server/ai_host.py 的起草
#    要求）会按这些名字指代，别改字；已有的 [人口/口粮] 块**不许删**
#    （旧测试依赖）。
# ⚠️ 文本里**不许**出现「起草」/「draft」：默认路径的回归测试要保证
#    不传 draft 时 user message 里没有这两个词（本快照默认也会拼进去）。


def _budget_line(cap: dict) -> str:
    return (
        f"[现金/预算] cash={_display_num(cap['cash'])} quarter={cap['quarter']}；"
        f"先保证 建造现金+材料+工资+经营费+口粮 ≤ cash"
        f"（建造看下表「折后」与「材料价」；材料/口粮按上季价，"
        f"首季无历史价用 P0，结算时一次扣；生产原料另计）"
    )


def _empty_line(cap: dict) -> str:
    total = int(cap["empty_total"])
    cells = list(cap["empty_cells"])
    if total <= 0:
        return "[空地] 没有空地（地图已满，builds 无处可建）"
    if not cells:
        return f"[空地] 共{total}格（本次未列示例，builds 只能用地空格子）"
    if len(cells) < total:
        return (
            f"[空地] 共{total}格（示例前{len(cells)}）：" + " ".join(cells)
            + f" …（其余 {total - len(cells)} 格也是空地）"
        )
    return f"[空地] 共{total}格（全部列出）：" + " ".join(cells)


def _build_lines(cap: dict) -> list[str]:
    options = cap.get("build_options") or []
    if not options:
        return ["[可建] 没有可建工程"]

    lines = ["[可建] 类型|名|现金|材料|材料价|折后|可产"]
    for opt in options:
        lines.append(
            f"{opt['plot_type']}|{opt['label']}|{_display_num(opt['cash'])}|"
            f"{_material_text(opt['materials'])}|{_display_num(opt['materials_cost'])}|"
            f"{_display_num(opt['cash_after_discount'])}|"
            f"{'/'.join(opt['products']) or '—'}"
        )

    quota = cap.get("quota") or {}
    if quota:
        parts = " ".join(
            f"{name} 剩{info['remaining']}/{info['total']}"
            for name, info in sorted(quota.items())
        )
        lines.append(
            f"（合计价 = 折后 + 材料价；升级工程不在本表。开局减免名额：{parts}）"
        )
    return lines


def _running_line(cap: dict) -> str:
    running = [plot for plot in cap.get("plots") or [] if plot.get("can_run")]
    if not running:
        return "[可开工] 没有可开工地块（先建造，完工后才能开工）"
    parts = [
        f"{plot['plot_id']}({plot['plot_type']} L{plot['level']} "
        f"产{plot['product'] or '—'})"
        for plot in running
    ]
    return "[可开工] " + " ".join(parts) + "（u 取 0–1）"


def _upgrade_lines(cap: dict) -> list[str]:
    """`[可升级]` —— 已有地块的升级选项（不是新建；新建看 [可建]）。

    形如：

        [可升级] 3,4 工业1 G → 二级机械(H-A/H-F/H-G/H-J,2季/240) / 二级研究(6季/300)；
                  5,4 农业1 A → 农业1→2(产出A/B/C/D,2季/80)

    · 一地块一段，多工程用「/」分隔，地块之间用「；」；
    · 没有可升级地块写 `[可升级] 无`（**不许整块缺席**，与 [可开工] 同口径）；
    · 工业 L2 机械 → 目标是「当前产物+」（由当前决定，不能换线）；
    · 数字 = 工程表原值（升级不吃开局减免名额）。
    """
    rows = cap.get("upgrade_options") or []
    if not rows:
        return ["[可升级] 无"]

    by_plot: dict[str, list[dict]] = {}
    for row in rows:
        by_plot.setdefault(row["plot_id"], []).append(row)

    def one_work(row: dict) -> str:
        cost = f"{_display_num(row['quarters'])}季/{_display_num(row['cash'])}"
        if row["product_from_current"]:
            target = f"{row['product']}+"
            return f"{row['label']}(由当前产{row['product']}→{target},{cost})"
        if row.get("fixed_product"):
            return f"{row['label']}({cost})"
        if row["products"]:
            options = "/".join(row["products"])
            return f"{row['label']}({options},{cost})"
        return f"{row['label']}({cost})"

    parts = []
    for plot_id in sorted(by_plot, key=_sort_key):
        items = by_plot[plot_id]
        head = items[0]
        product = head["product"] or "—"
        title = (
            f"{plot_id} {plot_type_name(head['plot_type'])}{head['level']} {product}"
        )
        parts.append(f"{title} → " + " / ".join(one_work(r) for r in items))

    return ["[可升级] " + "；".join(parts)]


def _constructing_line(cap: dict) -> str:
    constructing = [
        plot for plot in cap.get("plots") or []
        if plot.get("status") == PlotStatus.CONSTRUCTING.value
    ]
    if not constructing:
        return "[在建] 无"
    parts = [
        f"{plot['plot_id']}({plot['plot_type']} L{plot['level']} 在建)"
        for plot in constructing
    ]
    return "[在建] " + " ".join(parts) + "（在建本季不能开工）"


def _population_line(cap: dict) -> str:
    population = cap.get("population") or {}
    need = cap.get("food_need") or {}
    baseline = need.get("baseline") or {}
    good = need.get("good") or {}

    def pairs(items: dict) -> str:
        return " ".join(f"{goods}={_display_num(qty)}" for goods, qty in items.items()) or "—"

    return (
        f"[人口/口粮] 岗位={_display_num(population.get('labor_total', 0))} "
        f"人口={_display_num(population.get('population', 0))} "
        f"基准档 {pairs(baseline)} 优秀档 {pairs(good)}"
        f"（岗位含服务/研究；口粮只按非服务地块推）"
    )


def _recommended_lines(cap: dict) -> list[str]:
    """`[推荐空地]` 块 + 软提示（没有推荐就明说「先建第一块」）。"""
    cells = cap.get("recommended_build_cells") or []
    if not cells:
        return ["[推荐空地] 暂无（先建第一块）"]
    parts = [
        f"{cell['plot_id']}（邻 {format_neighbors(cell.get('neighbors') or [])}）"
        for cell in cells
    ]
    return [
        "[推荐空地] " + " ".join(parts),
        "优先从 [推荐空地] 里选 build 的格子（贴着已有地块才有 E_邻接 加成）。",
    ]


def _opening_advice_lines(cap: dict) -> list[str]:
    """`[开局建议]` —— 开局才触发；其它阶段整块缺席（不是写「无」）。
    
    用户反馈：AI 开局只建 2 块就停手。本节给出**至少 3 块、优先农+资+工**
    的硬提示；数字仍以 [可建] / [推荐空地] 为准（本节不发明数字）。
    """
    advice = cap.get("opening_advice") or {}
    if not advice.get("active"):
        return []
    return [
        "[开局建议] " + str(advice.get("note") or ""),
    ]


def _food_rule_line(cap: dict) -> str:
    """`[口粮规则]` —— 当前数字 + 公式，专治「把 22 人写成 food={"A":22}」。"""
    rule = cap.get("food_rule") or {}
    population = cap.get("population") or {}
    baseline = rule.get("baseline_need") or {}
    good = rule.get("good_need") or {}

    def pairs(items: dict) -> str:
        return " ".join(
            f"{goods}×{_display_num(qty)}" for goods, qty in sorted(items.items())
        ) or "—"

    return (
        f"[口粮规则] 人口={_display_num(population.get('population', 0))} "
        f"F_基准={_display_num(rule.get('baseline_food', 0))} → 合格档 A+B {pairs(baseline)}；"
        f"F_优秀={_display_num(rule.get('good_food', 0))} → 优秀档 A/B/C/D {pairs(good)}；"
        f"★ 为拿 H=1.0（优秀），本季 food 应含：{pairs(good)}；"
        f"只填 A+B 最高 H=0.6（合格档，A+B 必须同时有，缺一则 H=0）；"
        f"多买会被丢弃（没有仓库）。"
    )


def format_capabilities(cap: dict) -> str:
    """把 `describe_capabilities()` 的 dict 压成**紧凑中文文本**（给提示词）。

    固定块序（主实现按块名引用）：[现金/预算] → [空地] → [推荐空地] →
    [可建] → [可开工] → [可升级] → [在建] → [人口/口粮] → [口粮规则] →
    [开局建议]（仅开局触发）→ 最后一句硬提示。
    """
    lines = [
        _budget_line(cap),
        _empty_line(cap),
        *_recommended_lines(cap),
        *_build_lines(cap),
        _running_line(cap),
        *_upgrade_lines(cap),
        _constructing_line(cap),
        _population_line(cap),
        _food_rule_line(cap),
        *_opening_advice_lines(cap),
        "要开工必须写进 plot_plans，漏写=不开工；"
        "builds 用于 [空地] 新建，也用于 [可升级] 里已有地块的升级。",
    ]
    return "\n".join(lines)
