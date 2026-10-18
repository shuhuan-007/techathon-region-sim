"""AI 策略提示层 —— 让秘书「稍微有点脑子」的**确定性算数**（2026-10-18）。

═══════════════════════════════════════════════════════════════════
为什么需要这一层
═══════════════════════════════════════════════════════════════════

本轮引擎把**服务**做成「给八邻格里的非服务地块按自身等级加 5%/7.5%/10%、
并给每邻 6/9/12 现金/季」的基础设施，市场也放大了（Q0 变大、研究线
6→4 / 8→5 季）。但秘书 AI 的上下文只有「可建 / 可升级 / 推荐空地」——
**没有任何经济判断**：不会算升级划不划算、不会摆服务、不知道市场还能吃
多少。模型于是只能拍脑袋（第七轮用户原话：「注意到你不怎么喜欢升级」）。

本模块补的正是这一层：**代码算数，模型只读结论**。

═══════════════════════════════════════════════════════════════════
边界（比功能重要）
═══════════════════════════════════════════════════════════════════

    · **纯函数、只读**：不改 state、不扣钱、不建地块、不写存档；
    · **只给建议文本**：最终 Plan 仍由模型起草、走同一条 `validate` /
      `preview`（AI 无 commit，§11.4）；
    · **不发明数字**：价格走 `world.last_price()`（无历史价时它自己回退
      P0）；效率走 `engine.efficiency.compute_efficiency()`；配方走
      `cfg.recipe_for()` / `cfg.recipe()`；工程表走
      `construction.load_works()`；经营费拆分走 `cfg.production_operating_*`。
    · **不替引擎判合法性**：那是 `engine/validate.py` 的事。

═══════════════════════════════════════════════════════════════════
四个函数 + 一个合成器
═══════════════════════════════════════════════════════════════════

    service_hint(state, player_id, cfg) -> str|None
        找「八邻格里运行中的非服务地块最多」的空位，给一句建服务的理由；
        服务已有 ≥3 块、或最好的空位邻居 <3 块 → `None`。

    upgrade_economics(state, player_id, cfg, *, limit=3) -> list[dict]
        一级地块（农业 / 资源 / 工业 G）升级的经济性，按
        **ΔGDP ÷ 现金成本** 排序；没有可升级地块 → `[]`。

    market_headroom(state, cfg, *, limit=3) -> list[dict]
        对 G/H-A/H-A+/I/F/C/D：按曲线斜率算「还能多卖多少单位才跌破成本」。

    cash_ceiling_hint(state, player_id, cfg) -> str|None
        上季总扣款 vs 当前现金：本季还能再加多少现金的工程。

    strategy_lines(state, player_id, cfg, *, limit=4) -> list[str]
        把上面四个压成 ≤4 行（每行一句），`ai/plan_context.py` 的
        `[策略提示]` 直接拿来渲染（块内每行以 `- ` 开头）。

═══════════════════════════════════════════════════════════════════
口径（三处最容易算错的地方）
═══════════════════════════════════════════════════════════════════

① **GDP 不含工资**（`finance.value_added`：工资属于增加值，不是中间消耗）。
   所以这里的「每季毛利」是**增加值型毛利**：

        MC    = (Σ input×P_t + 0.7c + 0.3c×Ê) / e
        margin = e × Ê × (P_t − MC)                   ← 近似，见下

   其中 0.7c / 0.3c 来自 `cfg.production_operating_fixed_share` /
   `production_operating_variable_share`（经营费 = 0.7c + 0.3c·u·E，取 u=1）。
   **不含工资** —— 所以 margin 是「这块地每季给 GDP 添多少」，不是会计利润。

   ⚠️ 这是**近似**：把 `0.7c + 0.3c·Ê` 整块摊进单位成本再乘 `e·Ê`，
      与「收入 − 投料·Ê − 经营费」相差 `0.7c·(Ê−1)`（Ê≈1 时几乎为 0）。
      指示性提示用足够；要精确账请走 `pipeline.preview`。

② **P_t 用上季结算价**（`world.price_history` 最后一格，经
   `world.last_price()`；首季没有历史价时它自己退回 P0）—— 与
   「买入按上季价」同口径。而 `service_hint` 里那一项按 **P0** 算，
   因为 GDP 是统计口径（`finance.output_value` 用固定参考价）。

③ **服务自己不吃邻接加成**（§3.6）：服务之间也不加成。所以服务那块地
   的「价值」只体现在 ①给邻居的加成 + ②服务收入，本模块照此口径算。

⚠️ 本模块**不评估「服务升级」**（L1→L2 的 6→9 收入、5%→7.5% 加成）——
   任务书只要求「服务选址提示」，升级经济性只覆盖产商品的地块。
   需要时可以在此加一个分支，别在别处偷偷算。
"""

from __future__ import annotations

from typing import Optional

from engine import construction as C
from engine import efficiency as E
from engine.loader import Config, ConfigError
from engine.models import GameState, Plot, PlotType, parse_plot_id


# ══════════════════════════════════════════════════════════════════
# 一、常量（都是**提示层自己的判断阈值**，不是引擎规则）
# ══════════════════════════════════════════════════════════════════

#: 建议建服务的最少邻居数。少于它 → 不值得占一格（任务书 §交付1-1）。
SERVICE_NEIGHBOR_MIN: int = 3

#: 自己已有多少块服务后就不再提示新建（服务之间不加成，堆多了没意义）。
SERVICE_PLOT_CAP: int = 3

#: 市场余量看哪些商品（任务书 §交付1-3 点名的那 7 个）。
HEADROOM_GOODS: tuple[str, ...] = ("G", "H-A", "H-A+", "I", "F", "C", "D")

#: 这些商品由谁生产（用来算单位成本）。`None` = 该配方不需要选产物。
PRODUCERS: dict[str, tuple[PlotType, int, Optional[str]]] = {
    "G": (PlotType.INDUSTRY, 1, "G"),
    "H-A": (PlotType.INDUSTRY, 2, "H-A"),
    "H-A+": (PlotType.INDUSTRY, 3, "H-A+"),
    "I": (PlotType.INDUSTRY, 3, "I"),
    "F": (PlotType.RESOURCE, 1, None),
    "C": (PlotType.AGRICULTURE, 1, "C"),
    "D": (PlotType.AGRICULTURE, 1, "D"),
}

#: 市场余量用的中性效率：说明的是**市场**能吃多少，不掺玩家自己的地形。
NEUTRAL_EFFICIENCY: float = 1.0

#: 上季总扣款达到现金的这个比例 → 提前告警（>=100% 是**已超支**，另说）。
CASH_WARN_RATIO: float = 0.9


# ══════════════════════════════════════════════════════════════════
# 二、小工具
# ══════════════════════════════════════════════════════════════════


def _plot_order(plot_id: str) -> tuple:
    """地块 ID 的排序键：按 (行, 列) —— `0,2` 在 `0,10` 之前。

    `ai/plan_context.py` 里有一个同名同义的 `_sort_key`。这里**不 import
    它**：plan_context 要 import 本模块（形成反向依赖），复制这十行比
    制造循环 import 便宜，也比「谁先谁后」的口头约定可靠。
    """
    try:
        row, col = parse_plot_id(plot_id)
    except (TypeError, ValueError):
        return (1, 0, 0, str(plot_id))
    return (0, row, col, "")


def fmt_num(value: float, digits: int = 1) -> str:
    """**仅展示**用的数字格式：整数去掉 `.0`，小数留 `digits` 位。

    与 `plan_context._display_num` 同口径（提示词里 `467.0` 比 `467` 吵）。
    """
    num = float(value)
    if abs(num - round(num)) < 1e-9:
        return str(int(round(num)))
    return f"{num:.{digits}f}"


def _last_price(world, goods: str) -> Optional[float]:
    """上季结算价（无历史 → P0）。商品根本不在市场里 → `None`（跳过，不炸）。"""
    try:
        return float(world.last_price(goods))
    except KeyError:
        return None


def _recipe(cfg: Config, plot_type: PlotType, level: int, product: Optional[str]):
    """查配方；查不到 → `None`（提示层跳过这一行，不让快照炸掉）。"""
    try:
        return cfg.recipe(plot_type, level, product)
    except ConfigError:
        return None


def _recipe_for(cfg: Config, plot: Plot):
    """按地块查配方（研究地块会重定向到 `research.maintain`，与生产同口径）。"""
    try:
        return cfg.recipe_for(plot)
    except ConfigError:
        return None


def materials_cash(world, materials: dict[str, float]) -> Optional[float]:
    """工程物资按**上季价**折成现金；有商品查不到价 → `None`。"""
    total = 0.0
    for goods, qty in materials.items():
        price = _last_price(world, goods)
        if price is None:
            return None
        total += float(qty) * price
    return total


def unit_cost(
    world,
    cfg: Config,
    recipe,
    efficiency: float,
) -> Optional[float]:
    """单位成本 `MC`（**不含工资**，见模块头的口径①）。

        MC = (Σ input×P_t + 0.7c + 0.3c×E) / e

    `e <= 0`（服务 / 研究不产商品）或某个投料查不到价 → `None`。
    """
    if recipe is None or recipe.e <= 0:
        return None
    inputs = 0.0
    for goods, qty in recipe.input.items():
        price = _last_price(world, goods)
        if price is None:
            return None
        inputs += float(qty) * price
    operating = (
        float(cfg.production_operating_fixed_share) * recipe.c
        + float(cfg.production_operating_variable_share) * recipe.c * float(efficiency)
    )
    return (inputs + operating) / float(recipe.e)


# ══════════════════════════════════════════════════════════════════
# 三、1/4：服务选址提示
# ══════════════════════════════════════════════════════════════════


def service_hint(state: GameState, player_id: str, cfg: Config) -> Optional[str]:
    """「现在该在哪建一块一级服务」——一句话；不值得建就说 `None`。

    判定（全部来自引擎，不另立规则）：

      · 数**空地**里每一格八邻格中的「自己运行中的非服务地块」；
        服务之间互不加成，所以相邻的服务**不计**（与 `compute_adjacency`
        同一口径）；
      · 取邻居最多的一格（同分取行优先的那格，确定性）；
      · 邻居 < `SERVICE_NEIGHBOR_MIN`（3）→ `None`（占一格不划算）；
      · 自己已有 ≥ `SERVICE_PLOT_CAP`（3）块服务 → `None`；
      · 地图已满（没有空地）→ `None`。

    「≈ N GDP/季」怎么来：这些邻居里每一块**已经运行中**的地块，新建的
    一级服务会让它的产出涨 `rate`（默认 5%）—— 涨的那部分按 **P0** 计价
    就是 GDP 增量（GDP 用固定统计价，见 `finance.output_value`）。
    ⚠️ 已经吃到 40% 封顶的邻居**不会**再涨，这里按 `min(rate, cap − 已有)`
      逐块扣过；显示的「+X% 产出」仍是名义值 `邻居数 × rate`（方便读）。
    """
    player = state.player(player_id)

    service_plots = [
        plot for plot in player.plots.values()
        if plot.plot_type is PlotType.SERVICE
    ]
    if len(service_plots) >= SERVICE_PLOT_CAP:
        return None

    occupied = {
        plot_id
        for other in state.players.values()
        for plot_id in other.plots
    }
    empty = [
        plot_id for plot_id in state.world.all_plot_ids()
        if plot_id not in occupied
    ]
    if not empty:
        return None

    rate = float(cfg.efficiency.service_rate(1))
    cap = float(cfg.efficiency.adjacency_cap)
    income_rate = float(cfg.service_income_rate(1))

    best_plot_id: Optional[str] = None
    best_neighbors: list[Plot] = []
    for plot_id in empty:                      # all_plot_ids() 已按行列顺序
        neighbors: list[Plot] = []
        for neighbor_id in state.world.neighbor_ids(plot_id):
            neighbor = player.plots.get(neighbor_id)
            if neighbor is None or not neighbor.is_running:
                continue
            if neighbor.plot_type is PlotType.SERVICE:
                continue
            neighbors.append(neighbor)
        if len(neighbors) > len(best_neighbors):
            best_plot_id = plot_id
            best_neighbors = neighbors

    if best_plot_id is None or len(best_neighbors) < SERVICE_NEIGHBOR_MIN:
        return None

    gain_gdp = 0.0
    for neighbor in best_neighbors:
        recipe = _recipe_for(cfg, neighbor)
        if recipe is None or not recipe.output or recipe.e <= 0:
            continue
        breakdown = E.compute_efficiency(state, player_id, neighbor, cfg)
        delta_rate = max(0.0, min(rate, cap - float(breakdown.adjacency_bonus)))
        gain_gdp += (
            float(recipe.e)
            * float(breakdown.efficiency)
            * delta_rate
            * float(cfg.price_of(recipe.output))
        )

    income = len(best_neighbors) * income_rate
    nominal_pct = rate * 100.0 * len(best_neighbors)
    return (
        f"建议在 {best_plot_id} 建 1 级服务：邻 {len(best_neighbors)} 块"
        f"（+{fmt_num(nominal_pct, 0)}% 产出 ≈ {fmt_num(gain_gdp, 0)} GDP/季，"
        f"服务收入 {fmt_num(income, 0)}/季）"
    )


# ══════════════════════════════════════════════════════════════════
# 四、2/4：升级经济性
# ══════════════════════════════════════════════════════════════════


def _target_products(plot: Plot, work) -> list[Optional[str]]:
    """一条升级工程在**这块地**上可以产什么（给经济学算价用）。

    · 工业机械线（G→二级）：H-A/H-F/H-G/H-J 四选一 —— 全列，取最划算的；
    · 工业研究线（G→二级研究）：不产商品、没有市场价 → **不列**
      （任务书只要求按价格判经济性；研究线的价值在 1% 研究加成，
       那是另一套账，别混进来算「毛利」）；
    · 农业：只算**当前产出**（升级时可以重选 A/B/C/D，但这里评的是
      「把这块地升上去」，换品类等于换一门生意，不混在一起排）；
    · 资源：`None`（配方不需要选产物，产出固定 F）；
    · 服务：不列（本模块不评估服务升级，见模块头）。
    """
    if work.plot_type is PlotType.INDUSTRY:
        if work.route == "machine":
            return list(C.MACHINE_GOODS)
        return []
    if work.plot_type is PlotType.AGRICULTURE:
        return [plot.product] if plot.product else []
    if work.plot_type is PlotType.RESOURCE:
        return [None]
    return []


def upgrade_economics(
    state: GameState,
    player_id: str,
    cfg: Config,
    *,
    limit: int = 3,
) -> list[dict]:
    """一级地块升级的**每季 GDP 增量 ÷ 现金成本**，降序取前 `limit` 条。

    只评**一级、运行中**的地块（农业 / 资源 / 工业 G）—— 任务书划的范围；
    二级→三级不在内（那要先决定二级走哪条线，属于另一层判断）。

    每一行的形状（`[策略提示]` 的渲染只要前几个键）：

        plot_id / plot_type / from_level / to_level / work_id / label
        from_product         —— 现在产什么（G / C / D / F …）
        product              —— 升级后产什么（H-A / H-G / C …）
        price                —— P_t（上季价；无历史回退 P0）
        unit_cost            —— MC（不含工资，见模块头口径①）
        margin_gdp           —— 每季 GDP 增量 ≈ e×Ê×(P_t − MC)
        units_per_quarter    —— e×Ê（每季产多少单位）
        cash_cost            —— 工程现金 + 物资按上季价
        quarters             —— 工期（季）
        margin_ratio         —— margin_gdp ÷ cash_cost（排序键）
        profitable           —— P_t > MC

    ⚠️ **全部亏**时也返回这些行（按「亏得最少」在前），不在本函数里
       拼人话 —— 句子由 `format_upgrade_line()` 负责，数据保持可测。
    """
    player = state.player(player_id)
    world = state.world
    works = list(C.load_works(cfg).values())

    rows: list[dict] = []
    for plot_id in sorted(player.plots, key=_plot_order):
        plot = player.plots[plot_id]
        if not plot.is_running or int(plot.level) != 1:
            continue

        efficiency = float(
            E.compute_efficiency(state, player_id, plot, cfg).efficiency
        )
        current = _recipe_for(cfg, plot)
        from_product = plot.product or (current.output if current else None)

        for work in works:
            if work.is_new or work.plot_type is not plot.plot_type:
                continue
            if int(work.from_level) != int(plot.level):
                continue
            for product in _target_products(plot, work):
                recipe = _recipe(cfg, work.plot_type, int(work.to_level), product)
                if recipe is None or not recipe.output or recipe.e <= 0:
                    continue
                price = _last_price(world, recipe.output)
                if price is None:
                    continue
                mc = unit_cost(world, cfg, recipe, efficiency)
                if mc is None:
                    continue
                materials = materials_cash(world, work.materials)
                if materials is None:
                    continue

                margin = float(recipe.e) * efficiency * (price - mc)
                cash_cost = float(work.cash) + materials
                rows.append({
                    "plot_id": plot_id,
                    "plot_type": plot.plot_type.value,
                    "from_level": int(plot.level),
                    "to_level": int(work.to_level),
                    "work_id": work.work_id,
                    "label": work.label,
                    "from_product": from_product,
                    "product": recipe.output,
                    "price": price,
                    "unit_cost": mc,
                    "margin_gdp": margin,
                    "units_per_quarter": float(recipe.e) * efficiency,
                    "cash_cost": cash_cost,
                    "quarters": int(work.quarters),
                    "margin_ratio": (margin / cash_cost) if cash_cost > 0 else 0.0,
                    "profitable": bool(price > mc),
                })

    rows.sort(
        key=lambda row: (
            -row["margin_ratio"],
            _plot_order(row["plot_id"]),
            row["product"] or "",
            row["work_id"],
        )
    )
    return rows[: max(0, int(limit))]


def _transition_text(row: dict) -> str:
    """升级方向的一句话：产物变了写 `G→H-A`，只升等级写 `F 1→2 级`。

    资源 / 农业升二级时产物不变（仍产 F / C），写成 `F→F` 会让人以为没变化。
    """
    before = row.get("from_product")
    after = row.get("product")
    if before and after and before != after:
        return f"{before}→{after}"
    label = after or before or "—"
    return f"{label} {row['from_level']}→{row['to_level']} 级"


def format_upgrade_line(rows: list[dict]) -> Optional[str]:
    """把 `upgrade_economics()` 的行压成**一句**（`[策略提示]` 的一行）。

    有赚的：
        `升级 4,5 G→H-A：H-A 价 65.6 > 成本 24.7，+327 GDP/季，459 现金，2 季`
        （同类还有别的可升时追加「（同类还 N 块）」）
    全亏：
        `当前价格下升级不划算：1,2 G→H-G 亏 136/季（H-G 价 20 < 成本 44），先别升`
    没有可升级地块 → `None`。
    """
    rows = list(rows or [])
    if not rows:
        return None

    best = rows[0]
    same_target = sum(
        1 for row in rows
        if row["product"] == best["product"]
        and row["to_level"] == best["to_level"]
        and row["plot_type"] == best["plot_type"]
    )

    if not best["profitable"]:
        worst = min(rows, key=lambda row: row["margin_gdp"])
        return (
            f"当前价格下升级不划算：{worst['plot_id']} "
            f"{_transition_text(worst)} 亏 "
            f"{fmt_num(abs(worst['margin_gdp']), 0)}/季"
            f"（{worst['product']} 价 {fmt_num(worst['price'])} < 成本 "
            f"{fmt_num(worst['unit_cost'])}），先别升"
        )

    text = (
        f"升级 {best['plot_id']} {_transition_text(best)}："
        f"{best['product']} 价 {fmt_num(best['price'])} > 成本 "
        f"{fmt_num(best['unit_cost'])}，+{fmt_num(best['margin_gdp'], 0)} GDP/季，"
        f"{fmt_num(best['cash_cost'], 0)} 现金，{best['quarters']} 季"
    )
    if same_target > 1:
        text += f"（同类还 {same_target - 1} 块可升）"
    return text


# ══════════════════════════════════════════════════════════════════
# 五、3/4：市场余量
# ══════════════════════════════════════════════════════════════════


def market_headroom(state: GameState, cfg: Config, *, limit: int = 3) -> list[dict]:
    """「这个商品还能多卖多少单位，价格才跌到成本线」。

    用市场曲线自己的斜率（`MarketCurve.slopes()` → s、d）：

        ΔQ = (P_t − MC) × (s + d)

    ——把价格从 `P_t` 压到 `MC` 需要**多出来**的供给量。超过它，
    再卖就跌破成本（这也是「保本规模」的另一种说法）。

    每行：

        goods / price / unit_cost / slack      —— slack = P_t − MC（正=还有肉）
        headroom_units                         —— 还能多卖多少单位（负数截 0）
        headroom_plots                         —— 折成几块**最典型的生产地块**
        units_per_quarter / slope_sum / profitable

    ⚠️ 三条口径：
      · `MC` 用**中性效率 E=1**、按该商品的典型生产地块算（G→工业 L1、
        H-A→工业 L2、H-A+→工业 L3、I→工业 L3、F→资源 L1、C/D→农业 L1）——
        说的是**市场**能吃多少，不掺玩家自己的地形；
      · `P_t` 用上季价，投料也按上季价（ceteris paribus：只算「再多卖这些
        量」的边际影响，不预测别人下一季怎么动）；
      · 缺弹性（`Es/Ed` 未填）的商品**跳过**，不在提示层里编一个斜率。
    """
    world = state.world
    rows: list[dict] = []

    for goods in HEADROOM_GOODS:
        curve = world.market.get(goods)
        producer = PRODUCERS.get(goods)
        if curve is None or producer is None:
            continue
        plot_type, level, product = producer
        recipe = _recipe(cfg, plot_type, level, product)
        if recipe is None or recipe.output != goods or recipe.e <= 0:
            continue
        try:
            s, d = curve.slopes()
        except ValueError:                 # 弹性未填 → 跳过，不编斜率
            continue
        slope_sum = float(s) + float(d)
        if slope_sum <= 0:
            continue
        price = _last_price(world, goods)
        if price is None:
            continue
        mc = unit_cost(world, cfg, recipe, NEUTRAL_EFFICIENCY)
        if mc is None:
            continue

        slack = price - mc
        units = max(0.0, slack) * slope_sum
        rows.append({
            "goods": goods,
            "price": price,
            "unit_cost": mc,
            "slack": slack,
            "headroom_units": units,
            "headroom_plots": units / float(recipe.e),
            "units_per_quarter": float(recipe.e),
            "slope_sum": slope_sum,
            "profitable": bool(slack > 0),
        })

    rows.sort(key=lambda row: (-row["headroom_units"], row["goods"]))
    return rows[: max(0, int(limit))]


def format_market_line(rows: list[dict]) -> Optional[str]:
    """把 `market_headroom()` 的行压成一句多商品（`[策略提示]` 的一行）。

        `市场余量：G：现价 31.7 / 成本 23.9，还能卖 ≈76 单位（≈15 块）；…`
    已跌破成本的商品写成「现价 X < 成本 Y，别再扩产」。空 → `None`。
    """
    parts: list[str] = []
    for row in rows or []:
        if row.get("profitable"):
            parts.append(
                f"{row['goods']}：现价 {fmt_num(row['price'])} / 成本 "
                f"{fmt_num(row['unit_cost'])}，还能卖 ≈{fmt_num(row['headroom_units'], 0)} 单位"
                f"（≈{fmt_num(row['headroom_plots'], 0)} 块）"
            )
        else:
            parts.append(
                f"{row['goods']}：现价 {fmt_num(row['price'])} < 成本 "
                f"{fmt_num(row['unit_cost'])}，别再扩产"
            )
    if not parts:
        return None
    return "市场余量：" + "；".join(parts)


# ══════════════════════════════════════════════════════════════════
# 六、4/4：现金红线
# ══════════════════════════════════════════════════════════════════


def _report_entry(state: GameState, player_id: str) -> Optional[dict]:
    """取该玩家**上一季**的财报（dict 形状）。

    `state.report` 在真实链路上是 `{player_id: report_dict}`
    （`pipeline` 第 9 步写的），读档后 `storage.unpack` 原样放回；
    活会话里也是 dict。这里额外容忍直接给一个 `QuarterReport` 对象
    （有些调用方会拿 `preview()` 的报告来问「这版计划扛不扛得住」）。
    """
    report = getattr(state, "report", None)
    if isinstance(report, dict):
        entry = report.get(player_id)
        if isinstance(entry, dict):
            return entry
        if "total_deduction" in report:      # 直接就是一份报告
            return report
        return None
    if report is None:
        return None
    owner = getattr(report, "player_id", None)
    if owner not in (None, player_id):
        return None
    return {
        "total_deduction": getattr(report, "total_deduction", 0.0),
        "construction_cash": getattr(report, "construction_cash", 0.0),
    }


def cash_ceiling_hint(state: GameState, player_id: str, cfg: Config) -> Optional[str]:
    """现金红线：上季总扣款已经把现金吃到什么程度，本季还能再加多少工程。

    两档（都按**上季口径**，因为 `state.report` 是上一季的财报）：

      · **已超支** `total_deduction > cash`：
        `本季总扣款约 X > 现金 Y：扣掉上季经常性支出（R）后最多再加 Z 现金的工程`
      · **贴线** `total_deduction / cash ≥ CASH_WARN_RATIO`（0.9）：
        同一句话，但主语改成「≈ 现金的 N%」——这是**预警**，不是超支。

    `Z = cash − R`，`R = total_deduction − construction_cash`：
    上季扣款里**经常性**的那部分（工资 + 经营费 + 原料 + 口粮）这季大概率
    还要再花一遍，只有剩下的才是能自由安排给新工程的钱。`Z` 截到 0 以上。

    ⚠️ 「贴线」那一档是**本模块自己的判断**（任务书只写了「超过」）：
        `total_deduction` 恰好比现金少一点点时，等价于「本季几乎没有工程余量」——
        AI 若还按现金总额排建造，就会写出必然超支的计划。宁可提前说。
    """
    entry = _report_entry(state, player_id)
    if not entry:
        return None
    try:
        total = float(entry.get("total_deduction") or 0.0)
        construction = float(entry.get("construction_cash") or 0.0)
    except (TypeError, ValueError):
        return None
    if total <= 0.0:
        return None

    cash = float(state.player(player_id).cash)
    recurring = max(0.0, total - construction)
    free = max(0.0, cash - recurring)

    if total > cash:
        return (
            f"本季总扣款约 {fmt_num(total)} > 现金 {fmt_num(cash)}："
            f"扣掉上季经常性支出（{fmt_num(recurring)}）后最多再加 "
            f"{fmt_num(free)} 现金的工程"
        )

    ratio = (total / cash) if cash > 0 else 1.0
    if ratio >= CASH_WARN_RATIO:
        return (
            f"本季总扣款约 {fmt_num(total)} ≈ 现金 {fmt_num(cash)} 的 "
            f"{ratio * 100.0:.1f}%：扣掉上季经常性支出（{fmt_num(recurring)}）后"
            f"最多再加 {fmt_num(free)} 现金的工程"
        )
    return None


# ══════════════════════════════════════════════════════════════════
# 七、合成器：给 `[策略提示]` 用的 ≤4 行
# ══════════════════════════════════════════════════════════════════


def strategy_lines(
    state: GameState,
    player_id: str,
    cfg: Config,
    *,
    limit: int = 4,
) -> list[str]:
    """四个函数各出**至多一行**，按「服务 → 升级 → 市场 → 现金」排，最多 4 行。

    固定顺序的理由（不是随意的）：

      · 服务 / 升级是**本季能做的事**，先说；
      · 市场余量解释「为什么值得做」；
      · 现金红线是**刹车**，放最后一句，正好压住前面三条的建议。

    没内容的那一条**整行不出**（不写「暂无」）——整块都空时由
    `plan_context` 写 `[策略提示] 暂无`（与其它块同口径）。
    """
    lines: list[str] = []

    hint = service_hint(state, player_id, cfg)
    if hint:
        lines.append(hint)

    # 升级：先拿宽一点的名次再压成一句（要数「同类还几块」）。
    upgrades = upgrade_economics(state, player_id, cfg, limit=50)
    line = format_upgrade_line(upgrades)
    if line:
        lines.append(line)

    line = format_market_line(market_headroom(state, cfg))
    if line:
        lines.append(line)

    line = cash_ceiling_hint(state, player_id, cfg)
    if line:
        lines.append(line)

    return lines[: max(0, int(limit))]


__all__ = [
    "SERVICE_NEIGHBOR_MIN",
    "SERVICE_PLOT_CAP",
    "HEADROOM_GOODS",
    "CASH_WARN_RATIO",
    "service_hint",
    "upgrade_economics",
    "format_upgrade_line",
    "market_headroom",
    "format_market_line",
    "cash_ceiling_hint",
    "materials_cash",
    "unit_cost",
    "fmt_num",
    "strategy_lines",
]
