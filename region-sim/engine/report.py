"""状态报告 —— 给前端的 `/state` JSON（**引擎算好，前端零计算**）。

依据：
  · 《规则基线》§0（程序决定事实）、§11.4（AI 只解释）
  · 《界面要显示的变量清单》附录「数据从哪来」（`/state` 的形状）

═══════════════════════════════════════════════════════════════════
★ 本模块**不重写任何公式**
═══════════════════════════════════════════════════════════════════

这是本文件最重要的一条纪律。

`README` 与《项目架构-总》都写死了：**`preview` 与 `advance` 必须共用同一份实现**，
否则"AI 报的数"与"真实结算"会不一致。

report.py 是**第三个**会显示数字的地方（前端）。如果它自己算一遍产量/价格/幸福度，
就出现了**第二份实现** —— 数字迟早对不上，而且极难查。

所以本模块的每一个数都来自**既有函数**：

| 这个数字 | 来自 |
|---|---|
| 地块效率 E + 四分量拆解 | `efficiency.compute_efficiency()` |
| L / N / ḡ / m / F_基准 / F_优秀 | `population.compute_population()` |
| 产量 / 投入 / 工资 / 经营费 | `production.run_plot()` |
| 本季求价 `P_t`、粮食判定、收支 | `pipeline.preview()`（= 试算整条流水线） |

⚠️ **因此本模块没有一行"重新推导"的算式。** 要加新数字，先问：
"这个数在引擎里已经算过了吗？" 已经算过 → 从报告里取，不要重算。

═══════════════════════════════════════════════════════════════════
只读
═══════════════════════════════════════════════════════════════════

`build_state()` **绝不改状态**：试算走 `preview()`（它内部深拷贝后跑真引擎）。

═══════════════════════════════════════════════════════════════════
`can_settle` 的口径（✅ 2026-10-16 用户拍板）
═══════════════════════════════════════════════════════════════════

**「不安排 = 不开工」，是合法的。** 玩家有权让某块地空转一季。

《前端设计规范》§2.3 曾建议"每块地都已安排"才能点「推进本季度」，
但那一节自己标着「（建议）」，引的《回合循环与状态机》已删除，
《规则基线》里**没有**这条规则。用户已裁定按"不安排=不开工"处理。

所以 `can_settle` 只看三件事：

    ① 计划已经提交           （没提交 → 没什么可推进的）
    ② 计划能过 `validate`     （§11.3）
    ③ 试算跑得通、现金不为负  （真正的判据在第 5 步一次性扣款）
"""

from __future__ import annotations

from typing import Optional

from engine import finance, production, validate
from engine.loader import Config
from engine.models import GameState, Plan, PlotType
from engine.pipeline import QuarterReport, SettlementError, preview


# ══════════════════════════════════════════════════════════════════
# glB 地块资产契约（占位接口 —— 现在不出 3D，只把"键"定死）
# ══════════════════════════════════════════════════════════════════
#
# ★ 这是给"以后接 glB 模型"预留的契约，完整约定见
#   docs/03-架构/glB地块资产接口.md。三条要点：
#
#   ① 每块地有一个**资产键（asset key）**，唯一决定该用哪个模型文件；
#   ② 模型文件固定放 `web/assets/plots/<key>.glb`，**文件名 = 键名**，
#      把 .glb 丢进目录就生效，引擎不用改；
#   ③ 键缺失 / 文件缺失时前端**回退到现有 2D 渲染**（asset = null）。
#
# 键的命名规则（稳定、小写、可预测）：
#
#       <类型短码>_l<等级>[ _<产出短码> ]
#
#   · 类型短码：agri / resource / industry / service / research
#   · 等级：1 / 2 / 3（研究当前只有 1 级，仍占 l1 一键）
#   · 产出短码：**只在该类型同级需要区分模型时才进键** ——
#     眼下 15 张美术图是"5 类型 × 3 级"各一张，同级产出**共用一张图**
#     （农业 A/B/C/D 同图、工业同级各路线同图），所以**产出当前不进键**，
#     15 键覆盖全部 29 个"类型×等级×产出"组合。
#   · 将来若某类型同级要按产出拆模型（例：industry_l2_machine），
#     只须在该类型的 `_PRODUCT_IN_KEY` 登记产出 → 短码映射，键自动变长，
#     旧键废弃。这是**有计划的扩展点**，不是临时补丁。

#: 类型 → 键里的短码（顺序固定 = PlotType 声明顺序，保证 manifest 确定性）
_TYPE_CODE: dict[PlotType, str] = {
    PlotType.AGRICULTURE: "agri",
    PlotType.RESOURCE: "resource",
    PlotType.INDUSTRY: "industry",
    PlotType.SERVICE: "service",
    PlotType.RESEARCH: "research",
}

#: 哪些类型的"同级不同产出"需要不同的模型 → 产出进键。
#: 当前为空：15 张图里同级产出共用一张（见上）。要拆分时在这里登记
#: `{PlotType.INDUSTRY: {"H-A": "machine_agri", ...}}`，键即变为
#: `industry_l2_machine_agri`。**没登记的类型产出永远不进键。**
_PRODUCT_IN_KEY: dict[PlotType, dict[str, str]] = {}

#: 模型文件目录（相对 web 根；manifest 里给的就是这个相对路径前缀）
ASSETS_DIR = "assets/plots"
#: 模型文件扩展名（换格式只改这一处 + 目录里的文件）
ASSETS_EXT = ".glb"


def asset_key(plot_type: PlotType, level: int, product: Optional[str] = None) -> str:
    """一块地的资产键。**契约的唯一实现，前端不许自己拼。**

    参数与 `Plot` 的三个字段一一对应；`product` 只在
    `_PRODUCT_IN_KEY` 登记过该类型时才影响键。
    """
    try:
        code = _TYPE_CODE[plot_type]
    except KeyError:
        raise ValueError(f"没有类型 {plot_type!r} 的资产键短码") from None
    key = f"{code}_l{level}"
    product_map = _PRODUCT_IN_KEY.get(plot_type)
    if product_map is not None and product is not None:
        suffix = product_map.get(product)
        if suffix:
            key += f"_{suffix}"
    return key


def all_asset_keys() -> list[str]:
    """**当前生效的全部资产键**（确定性排序）。

    没有登记产出拆分时 = 5 类型 × 3 级 = **15 个键**。
    这是美术要交付的模型清单，也是测试"键集合恰好 15 个"的基准。
    """
    keys: list[str] = []
    for ptype in PlotType:  # Enum 声明顺序，确定性
        for level in (1, 2, 3):
            keys.append(asset_key(ptype, level))
    return keys


def asset_manifest() -> dict[str, dict]:
    """键 → 模型文件信息。**给前端的 manifest，`/api/map` 一并下发。**

    形状::

        { "<key>": { "file": "<key>.glb", "path": "assets/plots/<key>.glb" } }

    ⚠️ manifest 只**声明**路径约定，**不检查文件是否存在** ——
       模型还没进来时 manifest 照样给全，前端拿不到文件就回退 2D。
    """
    return {
        key: {"file": key + ASSETS_EXT, "path": f"{ASSETS_DIR}/{key}{ASSETS_EXT}"}
        for key in all_asset_keys()
    }


# ══════════════════════════════════════════════════════════════════
# 主入口
# ══════════════════════════════════════════════════════════════════


def build_state(
    state: GameState,
    player_id: str,
    cfg: Config,
    plan: Optional[Plan] = None,
    phase: Optional[str] = None,
) -> dict:
    """造 `/state` 的完整 JSON。**只读，不改状态。**

    参数
    ----
    state     : 当前游戏状态
    player_id : 要看谁的（红线 1：永远用 player_id 索引）
    cfg       : 静态配置
    plan      : 该玩家**本季已提交的计划**。`None` = 还没提交 →
                仍然给出地图/市场/上季报告，但**不给本季预览数**。
    phase     : 会话阶段（`collecting` / `resolving` / `report`），
                由服务层传；不传则不输出该字段。

    ⚠️ `plan=None` 时**不要**伪造"本季产量 0"之类的数 ——
       那会被读成"本季确实不产"。本季预览一律给 `None`，
       并说明"尚未提交计划"。
    """
    player = state.player(player_id)

    # ══════════════════════════════════════════════════════════════
    # ★ 上季报告有**两个来源**，要都认
    # ══════════════════════════════════════════════════════════════
    #
    #   raw_reports —— 刚结算完那一刻的**对象**形态（进内存才有）
    #   report      —— **dict** 形态，存档里存的就是它
    #
    # ⚠️ 只认 `raw_reports` 是个真实的 bug：
    #    存档不保存 `raw_reports`（它是中间产物），所以**读档后**
    #    `raw_reports` 是空的 → 界面上的"📋 上季报告"变成空白，
    #    玩家会以为程序把账吞了。
    #    `storage/base.py` 已经把 `report` 存下来了，这里必须回退去取。
    last_report: Optional[dict] = None
    if state.raw_reports and player_id in state.raw_reports:
        last_report = state.raw_reports[player_id].to_dict()
    elif state.report and player_id in state.report:
        last_report = state.report[player_id]

    # ══════════════════════════════════════════════════════════════
    # ★ 先校验，再试算（顺序不能反）
    # ══════════════════════════════════════════════════════════════
    #
    # 一开始写成了"先 preview、后 validate"，结果一份非法计划
    # （例如买了不存在的商品）会在 preview 里**直接抛 KeyError**，
    # 把 `/state` 这个**只读端点**打成 500。
    #
    # 正确顺序：计划不合法就**根本不要拿去试算** —— 试算它没有意义，
    # 而且失败方式不可预测。校验通过才试算。
    issues: list[validate.PlanIssue] = []
    if plan is not None:
        issues = validate.validate_plan(plan, player, cfg, player_id)

    trial: Optional[QuarterReport] = None
    trial_error: Optional[str] = None
    if plan is not None and not issues:
        try:
            trial = preview(state, {player_id: plan}, cfg)
        except SettlementError as e:
            # 资金不足等 —— 这是**正常的业务结果**，不是 bug
            trial_error = str(e)
        except Exception as e:  # noqa: BLE001
            # ⚠️ 收网兜底：`/state` 是**只读端点，绝不能崩**。
            #    任何试算失败都降级成一条 blocker。
            #    带上异常类型名，**不掩盖真 bug** —— 它会在界面上显形。
            trial_error = f"试算失败（{type(e).__name__}）：{e}"

    u_map = _u_map(plan)

    out = {
        "quarter": state.world.quarter,
        "total_quarters": state.world.total_quarters,
        "season": _season(state.world.quarter),
        "phase": phase,
        "player_id": player_id,

        "cash": player.cash,
        "happiness": player.last_happiness,

        # ── 人口与粮食（有计划就用计划算，没计划给 None）──
        "population": _population_block(state, player_id, cfg, plan, trial),

        # ── 效率速览 ──
        "efficiency": _average_efficiency(state, player_id, cfg, u_map),

        # ── 地块 ──
        "plots": _plots(state, player_id, cfg, plan, trial, u_map),

        # ── 市场 ──
        "market": _market(state, cfg, trial),

        # ── 上季报告（原样给，前端零计算）──
        "report": last_report,

        # ── 本季收支预览 ──
        "projection": _projection(trial, trial_error, cfg),
    }

    # ── 能不能推进（口径见模块头）──
    out.update(_settle_blockers(state, player, cfg, plan, issues, trial_error))
    return out


# ══════════════════════════════════════════════════════════════════
# 分块
# ══════════════════════════════════════════════════════════════════


def _u_map(plan: Optional[Plan]) -> dict[str, float]:
    """地块 → 本季开工比例。**计划里没提的地块 = 0（不开工）**。"""
    if plan is None:
        return {}
    return {pid: pp.effective_u for pid, pp in plan.plot_plans.items()}


def _season(quarter: int) -> str:
    """季节名。复用 `clock`，不另写一套。"""
    from engine.clock import season_of

    return season_of(quarter)


def _population_block(
    state: GameState,
    player_id: str,
    cfg: Config,
    plan: Optional[Plan],
    trial: Optional[QuarterReport],
) -> Optional[dict]:
    """L / N / ḡ / m / F_基准 / F_优秀（§3.3）。

    ⚠️ `N` **不取整**（引擎存小数），显示层负责取整 —— 见界面清单 §三。
    """
    if plan is None:
        return None
    if trial is not None and trial.population is not None:
        return trial.population.to_dict()

    # 有计划的形状但试算失败（如钱不够）：人口仍能单独算出来，
    # 因为它只依赖 u 与地块等级，与资金无关。
    from engine.population import compute_population

    return compute_population(state, player_id, _u_map(plan), cfg).to_dict()


def _average_efficiency(
    state: GameState, player_id: str, cfg: Config, u_map: dict[str, float]
) -> Optional[float]:
    """**开工地块**的平均 E（§一 第 5 条"本季效率"）。

    ⚠️ 只统计 `u > 0` 的地块：把停工/空地的 E 也算进去会把均值拉低，
       玩家会以为"效率掉了"，其实只是有几块地关着。
       一块开工的都没有 → `None`（**不是 0**，0 会显示成"效率为零"）。
    """
    from engine.efficiency import compute_efficiency

    player = state.player(player_id)
    vals: list[float] = []
    for pid in sorted(player.plots):
        if u_map.get(pid, 0.0) <= 0.0:
            continue
        plot = player.plots[pid]
        if not plot.is_running:
            continue
        vals.append(compute_efficiency(state, player_id, plot, cfg).efficiency)

    if not vals:
        return None
    return sum(vals) / len(vals)


def _plots(
    state: GameState,
    player_id: str,
    cfg: Config,
    plan: Optional[Plan],
    trial: Optional[QuarterReport],
    u_map: dict[str, float],
) -> list[dict]:
    """每块地一行（界面清单 §四 第 18–24 条）。

    ★ 产出预览与效率拆解按地块 ID 排序 —— **确定性**（§0 同输入同输出）。
    """
    from engine.efficiency import compute_efficiency

    player = state.player(player_id)
    out_by_plot: dict[str, production.PlotOutcome] = {}
    if trial is not None:
        out_by_plot = {o.plot_id: o for o in trial.production}

    rows: list[dict] = []
    for pid in sorted(player.plots):
        plot = player.plots[pid]
        u = u_map.get(pid, 0.0) if plan is not None else None

        # 效率：**不开工也算** —— 玩家想知道"这块地效率多少、该不该开"
        # （界面清单第 19 条要求鼠标悬停看四分量拆解，与开不开工无关）
        eff = None
        if plot.is_running:
            eff = compute_efficiency(state, player_id, plot, cfg).to_dict()

        # 产出预览：只在有计划时有意义
        preview_block = None
        if u is not None:
            outcome = out_by_plot.get(pid)
            if outcome is not None:
                preview_block = {
                    "goods": outcome.output_goods,
                    "qty": outcome.output_qty,
                    "inputs": dict(outcome.inputs),
                    "labor": outcome.labor,
                    "wage": outcome.wage,
                    "operating_cost": outcome.operating_cost,
                }
            else:
                preview_block = {
                    "goods": None, "qty": 0.0, "inputs": {},
                    "labor": 0.0, "wage": 0.0, "operating_cost": 0.0,
                }

        rows.append({
            "plot_id": pid,
            "plot_type": plot.plot_type.value,
            "level": plot.level,
            "status": plot.status.value,
            "product": plot.product,
            "u": u,
            "efficiency": eff,
            "output_preview": preview_block,
            "construction_left": plot.construction_left,
            "target_level": plot.target_level,
            "running_quarters": plot.running_quarters,
        })
    return rows


def _market(
    state: GameState, cfg: Config, trial: Optional[QuarterReport]
) -> dict:
    """各商品的价格块（界面清单 §五 第 25–27 条）。

        P0            参考价（对比用）
        last_price    上季结算价 —— 也是**本季买入记账价**（§4.1）
        current_price 本季求价 `P_t`；**没试算时为 None**（不编造）
        P_min         价格下限 = 0.1 × P0，接近时前端要警告
        at_floor      是否已触底（**只表示价格压到地板**，不代表卖不掉）
                      ✅ 2026-10-16 深夜裁定：触底也**全量收购**，销量不受影响
    """
    world = state.world
    prices = trial.prices if trial is not None else {}

    out: dict[str, dict] = {}
    for goods in cfg.all_goods():
        curve = cfg.market[goods]
        pr = prices.get(goods)
        out[goods] = {
            "name": cfg.goods_name.get(goods, goods),
            "P0": curve.P0,
            "last_price": world.last_price(goods),
            "current_price": pr["price"] if pr else None,
            "P_min": curve.P_min_ratio * curve.P0,
            "at_floor": bool(pr["at_floor"]) if pr else None,
        }
    return out


def _projection(
    trial: Optional[QuarterReport], trial_error: Optional[str], cfg: Config
) -> Optional[dict]:
    """本季收支预览（界面清单 §一 第 6 条「本季盈亏」）。

    ⚠️ 全部取自试算报告，**没有一行自己算的**。
       GDP 那几项走 `finance.finance_summary()`（§7.1 / §7.5 的唯一实现）。

    三种情况：

      · 没有计划（`trial` 与 `trial_error` 都是 None）→ `None`（不编数字）
      · 试算失败 → 只给 `{"error": 原因}`，**不编半个数字**
      · 试算成功 → 完整块
    """
    if trial is None:
        if trial_error:
            # 失败也要让前端有东西可显示（"本季收支：无法计算 —— 原因"）
            return {"error": trial_error}
        return None

    out = {
        "total_deduction": trial.total_deduction,
        "materials_cost": trial.materials_cost,
        "wages": trial.wages,
        "operating": trial.operating,
        "sales_revenue": trial.sales_revenue,
        "wage_tax_refund": trial.wage_tax_refund,
        "value_added": trial.value_added,
        "cash_before": trial.cash_before,
        "cash_after": trial.cash_after,
        "net_cash": trial.cash_after - trial.cash_before,
        "food": trial.food.to_dict() if trial.food else None,
        "error": trial_error,
    }

    # 补上 finance 独有的几个派生量（税后净工资、GDP 的 P0 口径拆解）。
    # ⚠️ 与上面重复的键以**报告原值**为准，不用 finance 重算的那份 ——
    #    避免"同一件事两个来源"，将来对不上时无法判断该信谁。
    fin = finance.finance_summary(trial, cfg)
    for key in ("wage_tax", "wage_net", "output_value_at_p0", "material_value_at_p0"):
        out[key] = fin[key]

    return out


def _settle_blockers(
    state: GameState,
    player,
    cfg: Config,
    plan: Optional[Plan],
    issues: list,
    trial_error: Optional[str],
) -> dict:
    """`can_settle` + `settle_blockers`（口径见模块头）。

    三件事：① 计划已提交 ② 计划过 validate ③ 试算跑得通且现金不为负。

    ⚠️ `issues` 由 `build_state` 算好传进来，**不在这里重算** ——
       重算等于把校验跑两遍，而且可能与实际用的那份不一致。
    """
    blockers: list[str] = []

    if plan is None:
        blockers.append("尚未提交计划")
    else:
        # ② 计划本身是否合法（§11.3）
        for issue in issues:
            blockers.append(issue.message)

        # ③ 试算是否跑得通（真正的判据：第 5 步一次性扣款）
        if trial_error:
            blockers.append(trial_error)

    # 现金为负（理论上不该发生：第 5 步会拦住，但前端要能显示）
    if player.cash < 0:
        blockers.append(f"现金为负（{player.cash:.2f}）—— 不允许透支")

    return {
        "can_settle": not blockers,
        "settle_blockers": blockers,
    }


# ══════════════════════════════════════════════════════════════════
# 地图（整张 10×10，给前端画格子用）
# ══════════════════════════════════════════════════════════════════


def build_map(state: GameState, player_id: str) -> dict:
    """整张地图，含**不属于该玩家**的格子（多人时看邻居要用）。

    前端画地图需要"这一格是谁的"，所以这里给全集，
    而不是只给 `player.plots`。

    ★ 每个 cell 带 `asset`（glB 资产键，契约见模块头）：
      · 有地块（无论是谁的）→ 键字符串，如 ``agri_l1``；
      · 空地 → ``None``（前端的**回退信号**：画现有 2D 格子）。
      响应另附 `assets` manifest（键 → glB 文件路径），
      前端拿键查 manifest 得路径，**不自己拼**。
    """
    # plot_id → ( owner, 地块本身 )——算 asset 要用类型/等级/产出
    cell_plot: dict[str, tuple[str, object]] = {}
    for pid, pl in state.players.items():
        for plot_id, plot in pl.plots.items():
            cell_plot[plot_id] = (pid, plot)

    cells = []
    for plot_id in state.world.all_plot_ids():
        entry = cell_plot.get(plot_id)
        if entry is None:
            owner_id, plot, key = None, None, None
        else:
            owner_id, plot = entry
            key = asset_key(plot.plot_type, plot.level, plot.product)
        cells.append({
            "plot_id": plot_id,
            "owner": owner_id,
            "mine": owner_id == player_id,
            "asset": key,
        })
    return {
        "rows": state.world.rows,
        "cols": state.world.cols,
        "cells": cells,
        "assets": asset_manifest(),
    }
