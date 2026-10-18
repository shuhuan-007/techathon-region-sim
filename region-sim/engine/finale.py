"""终局报告生成器（20 季经营总结）—— 事实打包 + 图表 + 兜底模板 + 模型编排。

★ 本模块**绝不动状态、绝不调模型** —— 「调模型」那一步在
   `ai/narrate.py::narrate_finale_report`（`generate_finale_report` 惰性 import）。

四条契约（与 `ai/prompts/finale.md`、`tests/test_finale_report.py`、子代理 C 前端同钉）：

    ① 每季 GDP = `history[i].value_added`（§7.5 增加值）。
    ② 每季幸福度 = `history[i].food.happiness`（`engine/settlement.py::FoodResult.happiness`）。
       终局幸福度 = `PlayerState.last_happiness` = 第 20 季的 `food.happiness`。
       **0—1 连续刻度，绝不转百分制**；food 为 None → null，不许补 0 / 插值。
    ③ 终局现金 = `final_report.cash_after`。
    ④ 终局报告四顶层键 `headline / body / charts / facts_used` **永远齐**；
       模型给的 charts 与 facts_used **不可信**——以本模块 `chart_points(overview)`
       与「由 facts 抽出的溯源表」为唯一权威源替换回去（防模型插值/编造）。

口径说明（definitions 块，模型/模板都得念）：
    · gdp = value_added（增加值，§7.5），不是总产值、不是销售额。
    · 销售 ≠ GDP：销售收入是成交口径，增加值是生产口径。
    · 现金 ≠ 利润：现金是水位，利润是本季经营结果。
    · 产出 ≠ 成交量：产出可能因合同/口粮截留不进公开市场。
    · 幸福度 0—1、固定刻度，不是百分制。

出处：2026-10-18 用户「20 季终局结算」三裁定之一（正文烧 token + 确定性兜底）。
"""

from __future__ import annotations

import datetime as _dt
from typing import Any, Optional

# ══════════════════════════════════════════════════════════════════
# 常量与工具
# ══════════════════════════════════════════════════════════════════

#: 终局总额定季数（config 里 world.total_quarters 兜底也是 20，见 models.py）。
DEFAULT_TOTAL_QUARTERS = 20

#: 兜底正文中**禁出现**的标签词（别贴定性标签）。
_BANNED_WORDS = ("最赚钱", "惨败", "完美", "天才", "完胜", "血亏", "崩盘")


def _num(x: Any) -> Optional[float]:
    """把输入安到 float | None：int/float → float；None/其他 → None。"""
    if x is None:
        return None
    if isinstance(x, bool):
        return None  # bool 是 int 子类，但语义上不是数值
    if isinstance(x, (int, float)):
        return float(x)
    return None


def _row_of(history: list, quarter: int) -> Optional[dict]:
    """从 history 里找 quarter==N 的那一行。**只看点里写明的 quarter 字段**。"""
    for row in history or []:
        if isinstance(row, dict) and row.get("quarter") == quarter:
            return row
    return None


def _food_happiness(row: Optional[dict]) -> Optional[float]:
    """每季幸福度真路径：row.food.happiness（见 settlement.FoodResult.to_dict）。

    ⚠️ food 可能是 None（未结算口粮链）→ None；绝不补 0。
    """
    if not isinstance(row, dict):
        return None
    food = row.get("food")
    if not isinstance(food, dict):
        return None
    return _num(food.get("happiness"))


def _population_count(row: Optional[dict]) -> Optional[float]:
    """每季人口：row.population.population（PopulationResult.to_dict 的真键）。

    ⚠️ population 块可能是 None → 返回 None；**不取整**（引擎存小数）。
    """
    if not isinstance(row, dict):
        return None
    pop = row.get("population")
    if not isinstance(pop, dict):
        return None
    return _num(pop.get("population"))


# ══════════════════════════════════════════════════════════════════
# ① 事实包：build_finale_facts
# ══════════════════════════════════════════════════════════════════


def build_finale_facts(overview: dict, cfg: Any = None) -> dict:
    """给模型用的**只读事实包**。不新增派生指标（累计/平均/增长率一律不算）。

    形状（键固定，缺值给 None/空表，**不为缺季造数**）：

        scope / player_id / quarter / total_quarters
        game_over / quarters_completed / missing_quarters / history_complete
        final:     {cash_after, value_added, happiness, population}
        series:    [{quarter, gdp, value_added, happiness, cash_after,
                     sales_revenue, population, operating_profit, food} × total]
        extremes:  {gdp: {max:{quarters:[…],value}, min:{…}},
                    happiness: {…}}    # **并列季全列**
        events:    {at_floor_quarters[{quarter,goods}],
                    policy_quarters[{quarter,name}],
                    contract_shortfalls[…]}    # 仅当 history 里真有字段才给
        definitions: 口径字典（稳态键）
    """
    ov = overview or {}
    scope = ov.get("scope") or "player"
    player_id = ov.get("player_id")
    total_quarters = int(ov.get("total_quarters") or DEFAULT_TOTAL_QUARTERS)
    quarter_now = ov.get("quarter")
    game_over = bool(ov.get("game_over"))
    quarters_completed = int(ov.get("quarters_completed") or 0)
    missing_quarters = list(ov.get("missing_quarters") or [])
    history_complete = bool(ov.get("history_complete"))

    history = list(ov.get("history") or [])
    final_report = ov.get("final_report")
    if not isinstance(final_report, dict):
        final_report = None

    # ── final 块：只从 overview 真实取值 ────────────────────────
    final_block: dict[str, Any] = {
        "cash_after": _num(final_report.get("cash_after")) if final_report else None,
        "value_added": _num(final_report.get("value_added")) if final_report else None,
        "happiness": _food_happiness(final_report),
        "population": _population_count(final_report),
    }

    # ── series：total_quarters 个点，逐季升序，缺季全 None ──────
    series: list[dict] = []
    for q in range(1, total_quarters + 1):
        row = _row_of(history, q)
        food_block = None
        if isinstance(row, dict) and isinstance(row.get("food"), dict):
            f = row["food"]
            # 只搬「简短块」：决不带 deducted/available 全表（太大）
            food_block = {
                "happiness": _num(f.get("happiness")),
                "achieved_level": _num(f.get("achieved_level")),
                "baseline_met": f.get("baseline_met"),
                "good_met": f.get("good_met"),
            }
        series.append({
            "quarter": q,
            "gdp": _num(row.get("value_added")) if isinstance(row, dict) else None,
            "value_added": _num(row.get("value_added")) if isinstance(row, dict) else None,
            "happiness": _food_happiness(row),
            "cash_after": _num(row.get("cash_after")) if isinstance(row, dict) else None,
            "sales_revenue": _num(row.get("sales_revenue")) if isinstance(row, dict) else None,
            "population": _population_count(row),
            "operating_profit": _num(row.get("operating_profit")) if isinstance(row, dict) else None,
            "food": food_block,
        })

    # ── extremes：gdp 与 happiness 的最高/最低。**并列季全列** ──
    def _extremes(metric_key: str, vals: list[Optional[float]]) -> dict:
        pairs = [(q, v) for q, v in enumerate(vals, start=1) if v is not None]
        if not pairs:
            return {"metric": metric_key, "max": None, "min": None}
        vmax = max(v for _, v in pairs)
        vmin = min(v for _, v in pairs)
        max_qs = [q for q, v in pairs if v == vmax]
        min_qs = [q for q, v in pairs if v == vmin]
        return {
            "metric": metric_key,
            "max": {"quarters": max_qs, "value": vmax},
            "min": {"quarters": min_qs, "value": vmin},
        }

    extremes = {
        "gdp": _extremes("gdp", [pt["gdp"] for pt in series]),
        "happiness": _extremes("happiness", [pt["happiness"] for pt in series]),
    }

    # ── events：已记录事项，**仅当 history 里真有字段** ─────────
    #   1) 市场触底季：report.prices[g].at_floor 为真
    #   2) 政策各季名称：report.policy{.announcement / .policy_type}
    #   3) 合同缺口：report 没真字段（合同账本挂 GameState.contracts，不在
    #      report.to_dict），从 history 里**就推不出来** → 老实给空表。
    at_floor: list[dict] = []
    policy_qs: list[dict] = []
    contract_shortfalls: list[dict] = []   # overview 不含 → 空
    for row in history:
        if not isinstance(row, dict):
            continue
        q = row.get("quarter")
        prices = row.get("prices")
        if isinstance(prices, dict):
            for g, blk in sorted(prices.items()):
                if isinstance(blk, dict) and blk.get("at_floor") is True:
                    at_floor.append({"quarter": q, "goods": g,
                                     "price": _num(blk.get("price"))})
        pol = row.get("policy")
        if isinstance(pol, dict) and pol:
            name = pol.get("announcement") or pol.get("policy_type") or ""
            policy_qs.append({"quarter": q, "name": name,
                              "policy_type": pol.get("policy_type"),
                              "target_goods": pol.get("target_goods"),
                              "parameter": _num(pol.get("parameter"))})

    events = {
        "at_floor_quarters": at_floor,
        "policy_quarters": policy_qs,
        "contract_shortfalls": contract_shortfalls,
    }

    # ── definitions：口径说明（稳态键，模型/模板照念） ─────────
    definitions = {
        "gdp": "value_added，增加值（§7.5 口径）= 产出按 P0 计价 − 中间投入按 P0 计价；不是总产值、不是营业额。",
        "happiness_scale": "0—1 连续刻度；0.6 为基准，1.0 封顶；不转百分制。",
        "cash_vs_profit": "现金是水位，利润是本季经营结果；现金下降不等于亏损。",
        "sales_vs_gdp": "销售收入是公开市场成交口径；增加值（GDP）是生产口径；二者不相等。",
        "output_vs_sales": "产出不等于成交量：口粮截留与合同交付会让产出中一部分不进公开市场。",
        "policy_vs_result": "政策乘数是规则效果，不直接等于本季实际增减；不把乘数说成结果。",
    }

    return {
        "scope": scope,
        "player_id": player_id,
        "quarter": quarter_now,
        "total_quarters": total_quarters,
        "game_over": game_over,
        "quarters_completed": quarters_completed,
        "missing_quarters": missing_quarters,
        "history_complete": history_complete,
        "final": final_block,
        "series": series,
        "extremes": extremes,
        "events": events,
        "definitions": definitions,
    }


# ══════════════════════════════════════════════════════════════════
# ② 图表：chart_points
# ══════════════════════════════════════════════════════════════════


def chart_points(overview: dict) -> list:
    """返回**恰好两个**图表对象（顺序固定：GDP 在前）。

    形状（与契约钉死）::

        [
          {"id": "gdp", "title": "第1—20季度 本季GDP 变化",
           "metric": "gdp", "unit": None,
           "points": [{"quarter": 1, "value": ...}, ...]},
          {"id": "happiness", "title": "第1—20季度 幸福度 变化",
           "metric": "happiness", "unit": None,
           "points": [...]},
        ]

    ⚠️ 缺值一律 None（前端拿到 None 就**真断开**，不补 0 / 不插值）。
    ⚠️ unit **永远是 None**（显示 0—1 刻度由前端 happiness id 自带纵轴契约去画）。
    """
    ov = overview or {}
    total = int(ov.get("total_quarters") or DEFAULT_TOTAL_QUARTERS)
    title_range = f"第1—{total}季度"

    history = list(ov.get("history") or [])

    gdp_points = []
    hap_points = []
    for q in range(1, total + 1):
        row = _row_of(history, q)
        gdp = _num(row.get("value_added")) if isinstance(row, dict) else None
        hap = _food_happiness(row)
        gdp_points.append({"quarter": q, "value": gdp})
        hap_points.append({"quarter": q, "value": hap})

    return [
        {"id": "gdp", "title": f"{title_range} 本季GDP 变化",
         "metric": "gdp", "unit": None, "points": gdp_points},
        {"id": "happiness", "title": f"{title_range} 幸福度 变化",
         "metric": "happiness", "unit": None, "points": hap_points},
    ]


# ══════════════════════════════════════════════════════════════════
# ③ 兜底模板：deterministic_body
# ══════════════════════════════════════════════════════════════════

#: 段标（四段，连序号一起给出，便于测试按标题级别匹配）。
_SEC_1 = "## 一、终局概览"
_SEC_2 = "## 二、GDP与幸福度曲线解读"
_SEC_3 = "## 三、经营历程回顾"
_SEC_4 = "## 四、完整数据回顾"


def _fmt(v: Optional[float], nd: int = 2) -> str:
    """把 float 或 None 格式化成两位小数 / em-dash。"""
    if v is None:
        return "—"
    return f"{v:.{nd}f}"


def _fmt_h(v: Optional[float]) -> str:
    if v is None:
        return "—"
    return f"{v:.2f}"


def _describe_trajectory(series: list[dict]) -> str:
    """一至两句**有事实依据**的总体轨迹描述。只陈述，不揣测因果。"""
    vals = [(p["quarter"], p["gdp"]) for p in series if p["gdp"] is not None]
    hvals = [(p["quarter"], p["happiness"]) for p in series if p["happiness"] is not None]
    bits: list[str] = []
    if vals:
        first_q, first_v = vals[0]
        last_q, last_v = vals[-1]
        if last_v > first_v:
            gdp_trend = "上行"
        elif last_v < first_v:
            gdp_trend = "下行"
        else:
            gdp_trend = "持平"
        bits.append(
            f"本季GDP从第{first_q}季的 {first_v:.2f} 走到第{last_q}季的 "
            f"{last_v:.2f}，整体呈{gdp_trend}走势"
        )
    if hvals:
        first_q, first_v = hvals[0]
        last_q, last_v = hvals[-1]
        if last_v > first_v + 1e-9:
            h_trend = "缓步上行"
        elif last_v < first_v - 1e-9:
            h_trend = "逐步回落"
        else:
            h_trend = "基本持平"
        bits.append(
            f"幸福度从第{first_q}季的 {first_v:.2f} 走到第{last_q}季的 "
            f"{last_v:.2f}，{h_trend}"
        )
    if not bits:
        return "由于完整记录不足，本次不作总体轨迹判断。"
    return "；".join(bits) + "。"


def _analyze_curve(points: list[Optional[float]], label: str) -> str:
    """对一条 20 点曲线做**分段方向**与**极值**描述。只陈述，不揣测因果。"""
    indexed = [(q, v) for q, v in enumerate(points, start=1) if v is not None]
    if not indexed:
        return f"{label}在各季均未提供。"

    # 分段：把相邻同向的连续段合并
    segs: list[tuple[int, int, str]] = []
    if len(indexed) == 1:
        q, _v = indexed[0]
        segs.append((q, q, "单点"))
    else:
        cur_start = indexed[0][0]
        cur_dir: Optional[str] = None
        prev_q, prev_v = indexed[0]
        for q, v in indexed[1:]:
            d = "升" if v > prev_v else ("降" if v < prev_v else "平")
            if cur_dir is None:
                cur_dir = d
                cur_start = prev_q
            elif d != cur_dir:
                segs.append((cur_start, prev_q, cur_dir))
                cur_start = prev_q
                cur_dir = d
            prev_q, prev_v = q, v
        if cur_dir is not None:
            segs.append((cur_start, prev_q, cur_dir))

    seg_desc = "；".join(
        f"第{a}—{b}季{d}" if a != b else f"第{a}季{d}"
        for a, b, d in segs
    )

    vals = [v for _, v in indexed]
    vmax = max(vals)
    vmin = min(vals)
    qs_max = [q for q, v in indexed if v == vmax]
    qs_min = [q for q, v in indexed if v == vmin]

    def _fmt_qs(qs: list[int]) -> str:
        return "、".join(f"第{q}季" for q in qs)

    last_q, last_v = indexed[-1]
    first_q, first_v = indexed[0]
    pos = ""
    if last_q != qs_max[0] and last_q != qs_min[0]:
        # 描述第 20 季相对位置
        if last_v == vmax:
            pos = f"第{last_q}季与峰值持平（{last_v:.2f}）"
        elif last_v == vmin:
            pos = f"第{last_q}季触及最低值（{last_v:.2f}）"
        elif last_v > first_v:
            pos = f"第{last_q}季收于 {last_v:.2f}，高于首季（{first_v:.2f}）"
        elif last_v < first_v:
            pos = f"第{last_q}季收于 {last_v:.2f}，低于首季（{first_v:.2f}）"
    else:
        pos = f"第{last_q}季收于 {last_v:.2f}"

    extremes_txt = (
        f"最高值 {vmax:.2f}（{_fmt_qs(qs_max)}）；"
        f"最低值 {vmin:.2f}（{_fmt_qs(qs_min)}）。"
    )
    return f"{label}走势：{seg_desc}。{extremes_txt}{pos}。"


def _turning_phases(series: list[dict]) -> list[str]:
    """按**真实数据**挑有意义的阶段/转折。

    返回若干条「阶段描述」短语（不机械 5 季一段；不声称玩家做了某次
    扩张/升级/转产，除非 history 有证）。

    目前只看 GDP 方向的变化点（peak-valley-peak 结构）与 happiness 的
    显著跳变 (>0.1)。**只陈述发生了什么，不揣测原因。**
    """
    out: list[str] = []
    gdp_pts = [(p["quarter"], p["gdp"]) for p in series if p["gdp"] is not None]
    h_pts = [(p["quarter"], p["happiness"]) for p in series if p["happiness"] is not None]

    # GDP 方向变化点：升→降 / 降→升
    if len(gdp_pts) >= 3:
        for i in range(1, len(gdp_pts) - 1):
            q0, v0 = gdp_pts[i - 1]
            q1, v1 = gdp_pts[i]
            q2, v2 = gdp_pts[i + 1]
            if q1 != q0 + 1 or q2 != q1 + 1:
                continue  # 中间有缺季，不算转折（缺证据）
            if v1 > v0 and v1 > v2:
                out.append(
                    f"第{q1}季是本季GDP曲线上的一个高点（{v1:.2f}，第{q0}季 "
                    f"{v0:.2f} → 第{q1}季 {v1:.2f} → 第{q2}季 {v2:.2f}）"
                )
            elif v1 < v0 and v1 < v2:
                out.append(
                    f"第{q1}季是本季GDP曲线上的一个低点（{v1:.2f}，第{q0}季 "
                    f"{v0:.2f} → 第{q1}季 {v1:.2f} → 第{q2}季 {v2:.2f}）"
                )

    # 幸福度显著跳变
    for i in range(1, len(h_pts)):
        q0, v0 = h_pts[i - 1]
        q1, v1 = h_pts[i]
        if q1 != q0 + 1:
            continue
        dv = v1 - v0
        if dv >= 0.1:
            out.append(
                f"第{q0}季到第{q1}季幸福度明显上行（{v0:.2f} → {v1:.2f}）"
            )
        elif dv <= -0.1:
            out.append(
                f"第{q0}季到第{q1}季幸福度明显回落（{v0:.2f} → {v1:.2f}）"
            )

    return out


def _full_table(series: list[dict]) -> str:
    """完整数据回顾：一张 N 行核心指标表（缺值 `—`）。

    列：季度 | GDP(value_added) | 幸福度 | 期末现金 | 销售收入 | 人口
    （增加值与 GDP 合并一列，并在表头注明「即增加值」）。
    """
    lines = []
    header = (
        "| 季度 | GDP（即增加值 value_added） | 幸福度 | 期末现金 | 销售收入 | 人口 |\n"
        "| --- | --- | --- | --- | --- | --- |"
    )
    lines.append(header)
    for p in series:
        q = p["quarter"]
        gdp = _fmt(p["gdp"])
        hap = _fmt_h(p["happiness"])
        cash = _fmt(p["cash_after"])
        sales = _fmt(p["sales_revenue"])
        pop = _fmt(p["population"], nd=1) if p["population"] is not None else "—"
        lines.append(f"| {q} | {gdp} | {hap} | {cash} | {sales} | {pop} |")
    return "\n".join(lines)


def deterministic_body(overview: dict, facts: dict) -> str:
    """兜底中文 Markdown 正文（模型失败/离线/不合规时使用）。

    结构（四段 + 收尾，**绝不编造、绝不空夸、不贴标签**）：
        # 《{player_id}的{N}季经营终局报告》
        ## 一、终局概览
        ## 二、GDP与幸福度曲线解读
        ## 三、经营历程回顾
        ## 四、完整数据回顾
        （一到两段收束）

    ⚠️ **缺季明示「第N季记录缺失」**；任一指标没数据就老实不出现该项。
    ⚠️ 金额/价格**只写裸数字**，绝不写「元/人民币/¥/$」。
    """
    ov = overview or {}
    player_id = facts.get("player_id") or ov.get("player_id") or "玩家"
    total = int(facts.get("total_quarters") or DEFAULT_TOTAL_QUARTERS)
    game_over = bool(facts.get("game_over"))
    quarters_completed = int(facts.get("quarters_completed") or 0)
    missing_quarters: list[int] = list(facts.get("missing_quarters") or [])

    series: list[dict] = list(facts.get("series") or [])
    final_block: dict = dict(facts.get("final") or {})

    # ── 一、终局概览 ────────────────────────────────────────────
    lines: list[str] = []
    title = f"《{player_id}的{total}季经营终局报告》"
    lines.append(f"# {title}")
    lines.append("")
    lines.append(_SEC_1)
    lines.append("")

    if not game_over:
        # 尚不具备终局条件：明说，不宣称结束
        cur_q = quarters_completed if quarters_completed else "若干"
        lines.append(
            f"本份报告是基于**尚在进行中**的经营过程生成的阶段性汇总，**尚不具备**"
            f"完整的{total}季终局报告条件。当前进度：已结算 {cur_q}/{total} 季；"
            f"以下曲线与指标仅覆盖已提供记录的部分，未结算季显示为「未提供」。"
        )
        lines.append("")
    else:
        lines.append(
            f"本报告覆盖范围：玩家 **{player_id}** 的完整 {total} 季经营"
            f"（第 1 季至第 {total} 季）。所有数值来自引擎逐季结算账本，"
            f"本模板不含任何模型推断。"
        )
        lines.append("")
        # 关键结果（已提供的正式结果；没有就老实不出现该项）
        summary_lines: list[str] = []
        if final_block.get("cash_after") is not None:
            summary_lines.append(f"- 终局最终现金：**{_fmt(final_block['cash_after'])}**")
        if final_block.get("value_added") is not None:
            summary_lines.append(f"- 终局第{total}季 GDP（增加值）：**{_fmt(final_block['value_added'])}**")
        if final_block.get("happiness") is not None:
            summary_lines.append(f"- 终局幸福度：**{_fmt_h(final_block['happiness'])}**")
        if final_block.get("population") is not None:
            summary_lines.append(f"- 终局人口：**{final_block['population']:.1f}**")
        if summary_lines:
            lines.extend(summary_lines)
            lines.append("")
        # 总体轨迹（一两句，有事实依据）
        lines.append(_describe_trajectory(series))
        lines.append("")

    # 缺季明示
    if missing_quarters:
        miss_list = "、".join(f"第{q}季" for q in missing_quarters)
        lines.append(
            f"**记录缺失提示**：以下季度的账本未提供，图表与表格里对应位置"
            f"已明示为「未提供 / —」：{miss_list}。"
        )
        lines.append("")
    elif not game_over:
        # 尚未结束时，把「未覆盖的季」也标出来（不宣称缺失，只说不存在）
        unmet = [p["quarter"] for p in series if p["gdp"] is None]
        if unmet:
            unmet_list = "、".join(f"第{q}季" for q in unmet)
            lines.append(f"以下季度尚无结算记录：{unmet_list}。")
            lines.append("")

    # ── 二、曲线解读 ────────────────────────────────────────────
    lines.append(_SEC_2)
    lines.append("")
    gdp_curve = _analyze_curve([p["gdp"] for p in series], "本季GDP")
    hap_curve = _analyze_curve([p["happiness"] for p in series], "幸福度")
    lines.append(gdp_curve)
    lines.append("")
    lines.append(hap_curve)
    lines.append("")
    lines.append(
        "上述两条曲线并列呈现，仅描述**同时期发生的事实**，不对二者之间"
        "是否存在因果作出断言。"
    )
    lines.append("")

    # ── 三、经营历程回顾 ────────────────────────────────────────
    lines.append(_SEC_3)
    lines.append("")
    phases = _turning_phases(series)
    if phases:
        for s in phases:
            lines.append(f"- {s}。")
        lines.append("")
        lines.append(
            "以上各阶段均按账本可考的事实归纳；对于其背后的具体操作决策，"
            "本报告不妄加揣测。"
        )
    else:
        lines.append(
            "按现有逐季账本，本季GDP与幸福度**未见显著拐点**；整体经营过程"
            "较为平稳，未出现大幅起伏。"
        )
    lines.append("")

    # ── 四、完整数据回顾 ────────────────────────────────────────
    lines.append(_SEC_4)
    lines.append("")
    lines.append("下表汇总已提供记录的逐季核心指标（缺值以「—」明示）。"
                 "逐地块、逐商品与逐合同的明细见附录（若提供）。")
    lines.append("")
    lines.append(_full_table(series))
    lines.append("")

    # ── 收束段（一至两段） ─────────────────────────────────────
    if game_over:
        hap_now = final_block.get("happiness")
        gdp_now = final_block.get("value_added")
        cash_now = final_block.get("cash_after")
        last_bits: list[str] = []
        if cash_now is not None:
            last_bits.append(f"终局现金收于 {_fmt(cash_now)}")
        if gdp_now is not None:
            last_bits.append(f"第{total}季 GDP {_fmt(gdp_now)}")
        if hap_now is not None:
            last_bits.append(f"终局幸福度 {_fmt_h(hap_now)}")
        if last_bits:
            lines.append(
                "、".join(last_bits) + "。"
            )
            lines.append("")
        lines.append(
            f"{total}个季度的经营到这里告一段落。这份回顾既记录账面上"
            f"每一次现金与产出的起落，也记下幸福度曲线里居民的反应。"
            f"它不评判玩家的选择是否最优，只是把发生过的事放在一处，"
            f"供重新翻看时对照。"
        )
    else:
        lines.append(
            "当前经营仍在进行中。待满足终局条件后，这里会给出一份覆盖"
            f"全部{total}季的完整回顾。"
        )

    body = "\n".join(lines).strip() + "\n"

    # 兜底红线自检：绝不出现禁词与货币单位
    for w in _BANNED_WORDS:
        if w in body:
            raise AssertionError(f"兜底正文出现禁词 {w!r}，模板坏了")
    # 货币单位符号：¥/￥/$ 永远不该出现；"元/人民币" 只在**邻近数字**时是单位
    import re as _re
    for sym in ("¥", "￥", "$"):
        if sym in body:
            raise AssertionError(f"兜底正文出现货币单位符号 {sym!r}，模板坏了")
    if _re.search(r"\d\s*人民币|\d\s*元[\s，。；、,.]|人民币\s*\d|\d+\s*元$", body, _re.M):
        raise AssertionError("兜底正文把「元/人民币」写到数字旁边，模板坏了")

    return body


# ══════════════════════════════════════════════════════════════════
# ④ 编排入口：generate_finale_report（先 model，败则 fallback）
# ══════════════════════════════════════════════════════════════════


def _fallback_facts_used(facts: dict, overview: Optional[dict] = None) -> dict:
    """由 facts 包**引擎侧**抽出一份溯源表（模型给的 facts_used 不可信）。

    约定路径（与 chart_points / deterministic_body 同源）：
        scope / player_id / game_over / quarters_completed
        final.cash_after / final.value_added / final.happiness / final.population
        extremes.gdp.max.value / extremes.gdp.max.quarters[i] / extremes.gdp.min.*
        extremes.happiness.*
        history.<i>.<field>   —— 逐行原始键（从 0 下标），满足 B 单测 5 的形态钉。
    """
    out: dict = {}
    out["scope"] = facts.get("scope")
    out["player_id"] = facts.get("player_id")
    out["game_over"] = facts.get("game_over")
    out["quarters_completed"] = facts.get("quarters_completed")
    final = facts.get("final") or {}
    for k in ("cash_after", "value_added", "happiness", "population"):
        if final.get(k) is not None:
            out[f"final.{k}"] = final[k]
    # 平铺 extremes 的 quarters 与 value（数组用从 0 下标）
    ex = facts.get("extremes") or {}
    for metric in ("gdp", "happiness"):
        blk = ex.get(metric) or {}
        for side in ("max", "min"):
            sblk = blk.get(side) or {}
            v = sblk.get("value")
            qs = sblk.get("quarters")
            if v is not None:
                out[f"extremes.{metric}.{side}.value"] = v
            if isinstance(qs, list):
                for i, q in enumerate(qs):
                    out[f"extremes.{metric}.{side}.quarters[{i}]"] = q

    # ── 逐季 orig 原始路径：history.<i>.<field>（B 单测 5 钉） ──────
    #   每行是 `rep.to_dict()` 的全键 dict。这里挑正文/曲线/表格会引用到的
    #   关键字段，按下标写入溯源表。**缺季（行不存在）不造数、不 stitch**。
    ov = overview or {}
    history = list(ov.get("history") or [])
    # B 单 body 要引用的列：GDP/幸福度/现金（期初+期末）/销售收入/经营盈亏/人口/政策名。
    _KEYS = (
        "quarter", "value_added", "cash_before", "cash_after", "sales_revenue",
        "operating_profit", "subsidy", "wage_tax_refund",
        "construction_materials_cost", "total_deduction",
    )
    for i, row in enumerate(history):
        if not isinstance(row, dict):
            continue
        for k in _KEYS:
            v = row.get(k)
            # 保留原始类型与数值：int/float/str/bool 原样；None 也写
            # （null ≠ 0，据实披露"这一行这一项没有"）。
            if isinstance(v, (int, float, str, bool)) or v is None:
                out[f"history.{i}.{k}"] = v
        # 嵌套：food.happiness / population.population / policy.name
        food = row.get("food")
        if isinstance(food, dict):
            hv = food.get("happiness")
            if isinstance(hv, (int, float)) or hv is None:
                out[f"history.{i}.food.happiness"] = hv
        pop = row.get("population")
        if isinstance(pop, dict):
            pv = pop.get("population")
            if isinstance(pv, (int, float)) or pv is None:
                out[f"history.{i}.population.population"] = pv
        pol = row.get("policy")
        if isinstance(pol, dict):
            nm = pol.get("name") or pol.get("policy_type")
            if isinstance(nm, str):
                out[f"history.{i}.policy.name"] = nm

    # ── final_report 原始路径（B 单正文「已提供的正式结果」引用点） ──
    #   final_report 就是 overview 的本季报告 dict；同样原样搬运关键键。
    fr = ov.get("final_report")
    if isinstance(fr, dict):
        for k in ("quarter", "value_added", "cash_before", "cash_after",
                  "sales_revenue", "operating_profit"):
            v = fr.get(k)
            if isinstance(v, (int, float)) or v is None:
                out[f"final_report.{k}"] = v
        fr_food = fr.get("food")
        if isinstance(fr_food, dict):
            hv = fr_food.get("happiness")
            if isinstance(hv, (int, float)) or hv is None:
                out["final_report.food.happiness"] = hv
        fr_pop = fr.get("population")
        if isinstance(fr_pop, dict):
            pv = fr_pop.get("population")
            if isinstance(pv, (int, float)) or pv is None:
                out["final_report.population.population"] = pv
    return out


def _rule_headline(player_id: Optional[str], total_quarters: int) -> str:
    """规则式标题（兜底路径用；禁 Markdown 语法）。"""
    pid = player_id or "玩家"
    return f"《{pid}的{total_quarters}季经营终局报告》"


def _headline_ok(s: Any) -> bool:
    """模型给的 headline 是否合规（非空字符串、无 Markdown 语法）。"""
    if not isinstance(s, str) or not s.strip():
        return False
    return not any(t in s for t in ("#", "*", "`", "[", "]", "(", ")"))


def generate_finale_report(
    overview: dict,
    cfg: Any = None,
    *,
    client: Any = None,
) -> dict:
    """终局报告的最终交付。

    先调模型（`ai/narrate.py::narrate_finale_report`）；
    模型败了/离线/不合规 → 用 `deterministic_body` 兜底。

    **永远返回四顶层键齐具的 dict**：

        headline / body / charts / facts_used
        + source  ∈ {"model", "fallback"}
        + ok      （True；本函数即便模型失败也兜出可用的安全结果）
        + warning （模型失败原因，如有时）
        + generated_at

    关键安全约束：
        · 模型给的 charts 与 facts_used **不可信**，一律用引擎版替换回去；
          仅作形状校验。
        · `overview.game_over is not True` → 不宣称结束，明说「尚不具备」。
        · 兜底 body 永不含禁词、永不含货币单位。
    """
    ov = overview or {}
    facts = build_finale_facts(ov, cfg)
    charts = chart_points(ov)
    engine_facts_used = _fallback_facts_used(facts, ov)
    total = int(facts.get("total_quarters") or DEFAULT_TOTAL_QUARTERS)

    # ── 分支一：尚未完整结算 ────────────────────────────────────
    if ov.get("game_over") is not True:
        body = deterministic_body(ov, facts)
        quarters_completed = int(facts.get("quarters_completed") or 0)
        return {
            "headline": _rule_headline(facts.get("player_id"), total),
            "body": body,
            "charts": charts,
            "facts_used": engine_facts_used,
            "source": "fallback",
            "ok": True,
            "warning": (
                f"终局报告**尚不具备**完整条件：当前只结算到第 "
                f"{quarters_completed}/{total} 季，未宣布游戏结束。"
                "本份为阶段性汇总；待 game_over=true 后可生成正式终局报告。"
            ),
            "game_over": False,
            "quarters_completed": quarters_completed,
            "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        }

    # ── 分支二：先试模型 ────────────────────────────────────────
    attempt = None
    try:
        from ai import narrate as _n  # 惰性，避免顶层双向依赖
        attempt = _n.narrate_finale_report(facts, cfg=cfg, client=client)
    except Exception as e:  # noqa: BLE001  —— 绝不让模型层异常穿透到别处
        attempt = {"ok": False, "error": "narrate_crashed",
                   "detail": f"{type(e).__name__}: {e}"}

    model_ok = (
        isinstance(attempt, dict)
        and attempt.get("ok") is True
        and isinstance(attempt.get("headline"), str)
        and isinstance(attempt.get("body"), str)
        and isinstance(attempt.get("charts"), list)
        and len(attempt["charts"]) == 2
    )

    if model_ok:
        # ⚠️ charts/facts_used 用**引擎版**（防模型插值/编造），
        #    模型版只作形状校验已被 narrate_finale_report 做过。
        return {
            "headline": attempt["headline"],
            "body": attempt["body"],
            "charts": charts,                # ← 引擎版，不信模型
            "facts_used": engine_facts_used,  # ← 引擎版，不信模型
            "source": "model",
            "ok": True,
            "model": attempt.get("model"),
            "warning": attempt.get("warning"),
            "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        }

    # ── 分支三：模型败了 → 兜底模板 ─────────────────────────────
    warning: Optional[str] = None
    if isinstance(attempt, dict):
        err = attempt.get("error")
        detail = attempt.get("detail")
        if err:
            warning = f"模型路径失败（{err}）：{detail or '无详情'}"
        elif not attempt.get("ok"):
            warning = "模型路径失败（未给出错误码）。"
    elif attempt is None:
        warning = "模型路径未返回。"

    body = deterministic_body(ov, facts)
    return {
        "headline": _rule_headline(facts.get("player_id"), total),
        "body": body,
        "charts": charts,
        "facts_used": engine_facts_used,
        "source": "fallback",
        "ok": True,
        "warning": warning,
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    }


# ══════════════════════════════════════════════════════════════════
# 自检用导出（测试用）
# ══════════════════════════════════════════════════════════════════

__all__ = [
    "DEFAULT_TOTAL_QUARTERS",
    "build_finale_facts",
    "chart_points",
    "deterministic_body",
    "generate_finale_report",
]
