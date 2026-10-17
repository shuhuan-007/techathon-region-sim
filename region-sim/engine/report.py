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

from engine import finance, news, production, validate
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

        # ── ★ 新闻/财报**留位**（2026-10-17，engine/news.py）。
        #    今天 = 纯事实搬运（news_facts）；明天接 AI 叙事（narrate）。
        "news": _news(state, player_id, cfg),

        # ── ★ 开局减免名额速览（2026-10-17，§5.3）—— 已用/总数/剩余
        #    都是引擎给的，前端零计算。
        "opening_quota": _opening_quota(player, cfg),

        # ── 本季收支预览 ──
        "projection": _projection(trial, trial_error, cfg),
    }

    # ── ★ 本季草稿里**未提交**建设指令的**折后现金报价**（2026-10-17）──
    #   ⚠️ 不是 /state 的固定字段：只在玩家真的提交了带 builds 的计划
    #      时才挂这个键。数字全部来自 `build_cash_plan`（纯函数），与第
    #      5 步扣款**同一套公式**；不记名额、不扣钱。
    if plan is not None and plan.builds:
        out["build_quote"] = build_quote(
            state, player_id, cfg,
            builds=[{
                "plot_id": b.plot_id,
                "plot_type": b.plot_type.value,
                "target_level": b.target_level,
                "product": b.product,
            } for b in plan.builds],
        )

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


def _news(state: GameState, player_id: str, cfg: Config) -> Optional[dict]:
    """新闻/财报事实包（引擎口 `news_facts`）+ AI 叙事**空位**。

    返回::

        {
          "facts":  news_facts(...)  | None,   # 还没结算过 = None（不编造）
          "narrative": None,                    # ★ 留位：接 narrate()，
                                                #   今天**恒 None**，明天给正文
        }

    ⚠️ 这里**不调 narrate()**——它尚未实现且**绝不**偷偷返回编造文本
       （§11.4）。今天 `narrative` 恒为 `None`，前端 `#ai-panel` 显示
       「AI 建议（待接）」即可；明天只在 `_news()` 这一处换成
       `narrate(facts, cfg)`，调用方与前端**一行不改**。

    状态层两个数（上季 H、合同账本）在这一层递，理由见 news.py docstring：
    财报只记本季，缺这两个就**诚实缺席**。
    """
    report = None
    if state.raw_reports and player_id in state.raw_reports:
        report = state.raw_reports[player_id]
    if report is None:
        return {"facts": None, "narrative": None}

    # 上季 H：状态层 `player.last_happiness` 在**结算后**就是本季 H，
    # 没有独立的"上季"位 → 诚实给 None（不臆造）。
    previous_happiness: Optional[float] = None

    facts = news.news_facts(
        report,
        cfg,
        contracts=list(state.contracts.values()) if state.contracts else None,
        previous_happiness=previous_happiness,
    )
    return {"facts": facts, "narrative": None}


def _quota_types(player, cfg: Config, this_plan_used: dict[str, int] | None = None) -> dict:
    """开局减免名额**逐类汇总**（§5.3）。 数字全来自引擎函数，不自算。

        {"agriculture": {"used": n, "total": 3, "remaining": m,
                         "this_plan": k}, ...}

      · used      —— 存档里已登记（`player.opening_quota_used`，跨季累计）
      · this_plan —— 本季这份计划**本身**会用掉的（`k` 条，来自 `builds`）
      · remaining —— `max(0, total − used − this_plan)`：再下一条还能不能打折，
                     **前端只显示、绝不重新判定**（判定在 `build_cash_plan` 里）。
    """
    from engine import construction as _C

    used_map = getattr(player, "opening_quota_used", None) or {}
    this_plan_used = this_plan_used or {}
    rows: dict[str, dict] = {}
    for ptype in PlotType:
        used = int(used_map.get(ptype.value, 0))
        total = int(_C._quota_of(cfg, ptype) or 0)
        plan_use = int(this_plan_used.get(ptype.value, 0))
        rows[ptype.value] = {
            "used": used,
            "total": total,
            "this_plan": plan_use,
            "remaining": max(0, total - used - plan_use),
        }
    return rows


def build_quote(state: GameState, player_id: str, cfg: Config, builds: list) -> dict:
    """一组**未提交**建设指令的报价（**引擎公式真算** → 前端零计算）。

    ⚠️ **只读**：`build_cash_plan` 本就是纯函数，不扣钱、不登记名额、
       不改状态。报的价以**结算第 5 步**为准（`pipeline` 用的是同一公式）。

    ★ 2026-10-17 增补 `quota_by_type`：名额条与报价行**同源**（§5.3），
       前端不再读 `STATE.opening_quota`（那个只看存档、看不到"本季这份
       计划会用掉几名"）。每个类型给 `used / total / this_plan / remaining`。
    """
    from engine import construction as _C
    from engine.models import BuildOrder

    player = state.player(player_id)
    orders = []
    for b in builds or []:
        orders.append(BuildOrder(
            plot_id=b["plot_id"],
            plot_type=PlotType(b["plot_type"]),
            target_level=int(b["target_level"]),
            product=b.get("product"),
        ))
    plan = Plan(player_id=player_id, builds=orders)

    rows: list[dict] = []
    total = 0.0
    error: Optional[str] = None
    discounted_any = False
    this_plan_used: dict[str, int] = {}
    try:
        for r in _C.build_cash_plan(plan, player, cfg):
            this_cash = float(r["cash"])
            rows.append({
                "plot_id": r["plot_id"],
                "plot_type": r["plot_type"].value,
                "list_cash": float(r["list_cash"]),
                "cash": this_cash,
                "discounted": bool(r["discounted"]),
                "consumed_after": int(r["consumed_after"]),
            })
            total += this_cash
            if r["discounted"]:
                discounted_any = True
                pt_key = r["plot_type"].value
                # 只数**本季这份计划里**真吃到名额的指令数（跨季累计已
                # 经在 `player.opening_quota_used` 里，不能重复算）。
                this_plan_used[pt_key] = this_plan_used.get(pt_key, 0) + 1
    except Exception as e:  # noqa: BLE001
        # 引擎抛什么 -> 原样告诉玩家。到这里都是业务错误
        # （缺产出 / 在建中 / 没这工程 / 不存在这格……）。
        error = f"{type(e).__name__}: {e}"

    return {
        "rows": rows,
        "total": round(total, 6),
        "discount_rate": _C._discount_rate(cfg),
        "discounted_any": discounted_any,
        "error": error,
        # ★ 与报价行**同源**的名额速览：used 是存档跨季累计，
        #   this_plan 是这份计划本身用掉的，remaining 已扣两者（只读）。
        "quota_by_type": _quota_types(player, cfg, this_plan_used),
    }


def _opening_quota(player, cfg: Config) -> dict:
    """开局减免名额速览（§5.3）。**数字全来自引擎函数，不自算。**

        {"rate": 0.5,
         "types": {"agriculture": {"used": n, "total": 3, "remaining": m}, ...}}
    """
    from engine import construction as _C

    used_map = getattr(player, "opening_quota_used", None) or {}
    rows: dict[str, dict] = {}
    for ptype in PlotType:
        used = int(used_map.get(ptype.value, 0))
        total = int(_C._quota_of(cfg, ptype) or 0)
        rows[ptype.value] = {
            "used": used,
            "total": total,
            "remaining": max(0, total - used),
        }
    return {"rate": _C._discount_rate(cfg), "types": rows}


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


def _builds_to_virtual_plots(pending: Plan, builder_id: Optional[str] = None) -> list:
    """把本季 `plan.builds` 折成"前影" Plot（状态机之外的**显示层**）。

    ★ `builder_id`（可选）：递交这份计划的玩家 id —— 虚地块**带归属**，
    让地图在结算前就能把那一格画成"我的"（`owner`/`mine`）。
    ⚠️ 只加不改：真实地块的归属仍由 `build_map` 里的真路径推导。

    与 `_plots()` 的形状一一对应，专门给 `/api/map` 与 `/api/state` 用：
    计划**递交后、结算前**，玩家已经指着地图下过指令，但那一格还是空地
    （第 2 步不改状态）。这一层"前影"就是让界面马上把那块地画成
    `constructing`，把本季已用名额也算进取景框——一结算，实体 Plot
    由 `pipeline` 补上，这层前影自动消失（因为 `plan.plans` 已清空）。

    ⚠️ 只用于读端点（**不改状态**）；产物的 `status` 一律
    `PlotStatus.CONSTRUCTING`（新建一级）或保留旧
    `construction_left` 但在 `status` 上仍记 `CONSTRUCTING`（升级）。
    """
    from engine.models import PlotStatus

    out = []
    for b in pending.builds or []:
        # 与 `to_dict()` 形状一致。结算后由真 Plot 取代。
        out.append({
            "plot_id": b.plot_id,
            "owner": builder_id,            # 这条建设指令是谁的（前影归属）
            "plot_type": b.plot_type.value,
            "level": 0,                    # 新建一级的“影子”还没有 level，
            "status": PlotStatus.CONSTRUCTING.value,
            "product": b.product,
            "u": None,                     # 本季预览：没有 u（尚未试算/不必试算）
            "efficiency": None,
            "output_preview": None,
            "construction_left": None,     # 引擎没真表格，前端别显示工期
            "target_level": int(b.target_level),
            "running_quarters": 0,
        })
    return out


def build_map(
    state: GameState,
    player_id: str,
    pending: Optional[Plan] = None,
) -> dict:
    """整张地图，含**不属于该玩家**的格子（多人时看邻居要用）。

    前端画地图需要"这一格是谁的"，所以这里给全集，
    而不是只给 `player.plots`。

    ★ 每个 cell 带 `asset`（glB 资产键，契约见模块头）：
      · 有地块（无论是谁的）→ 键字符串，如 ``agri_l1``；
      · 空地 → ``None``（前端的**回退信号**：画现有 2D 格子）。
      响应另附 `assets` manifest（键 → glB 文件路径），
      前端拿键查 manifest 得路径，**不自己拼**。

    ★ `pending`（可选）：本季该玩家**已递交的计划**。递交后、结算前
      那几条 `builds` 也要在地图上显影（否则"已经下指令"的格子
      长得跟空地一样）。引擎这层用 `_builds_to_virtual_plots` 折出
      与 `plots` 同形的"前影"挂到 cells 上；**不改真实状态**。
    """
    # plot_id → ( owner, 地块本身 )——算 asset 要用类型/等级/产出
    cell_plot: dict[str, tuple[str, object]] = {}
    for pid, pl in state.players.items():
        for plot_id, plot in pl.plots.items():
            cell_plot[plot_id] = (pid, plot)

    # 已递交（未结算）建设指令 → 虚地块，叠在 cell 上（带归属）
    virtual = _builds_to_virtual_plots(pending, builder_id=player_id) if pending is not None else []
    v_by_pid = {v["plot_id"]: v for v in virtual}

    cells = []
    for plot_id in state.world.all_plot_ids():
        entry = cell_plot.get(plot_id)
        vplot = v_by_pid.get(plot_id)
        if entry is None:
            owner_id, plot, key = None, None, None
        else:
            owner_id, plot = entry
            key = asset_key(plot.plot_type, plot.level, plot.product)
        cell = {
            "plot_id": plot_id,
            "owner": owner_id,
            "mine": owner_id == player_id,
            "asset": key,
        }
        if vplot is not None:
            # 只在 cell 上**追加**字段，不删/不改既有键
            if entry is None and vplot.get("owner") is not None:
                cell["owner"] = vplot["owner"]             # 前影的归属（建设指令的发件人）
                cell["mine"] = vplot["owner"] == player_id
            cell["status"] = vplot["status"]
            cell["level"] = vplot["level"]
            cell["product"] = vplot["product"]
            cell["plot_type"] = vplot["plot_type"]
        elif plot is not None:
            cell["status"] = plot.status.value
            cell["level"] = plot.level
            cell["product"] = plot.product
            cell["plot_type"] = plot.plot_type.value
        cells.append(cell)
    return {
        "rows": state.world.rows,
        "cols": state.world.cols,
        "cells": cells,
        "assets": asset_manifest(),
        "virtual_plots": virtual,   # 本季已下建设指令（未结算）；结算后自动消失
    }
