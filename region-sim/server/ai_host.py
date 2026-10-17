"""AI 主机层 —— **谁把局面递给 AI** 的那一环。

《规则基线》§11.4 的权限阶梯在 `ai/` 是纯函数层，**自己拿不到局面**
（它的黑名单禁止 import `server.session` 与 `storage`）。局面在
`server.session.GameSession` 里。所以必须由本层 —— server 侧的主机层 ——

    去拿局面 → 喂给 ai/ 的纯函数 → 调模型 → 把**建议**递回去。

═══════════════════════════════════════════════════════════════════
★ 第一原则：ai/ 保持纯，本层包揽所有"落地"
═══════════════════════════════════════════════════════════════════

本层**可以** import `server.session` 与 `ai.*`。这也意味着一件事：

    ⚠️ **import 顺序是本模块的安全假设之一。**
    `ai/tools.py` 的边界自检以「ai 被加载那一刻的既成事实」为基线，
    之后**新出现**的黑名单模块（`server.session` / `storage`）才算
    ai/ 带进来的。所以本模块在**文件顶部只 import `ai.*`**；
    任何会拉起 `server.session` / `storage` 的 import（`GameSession`、
    `Plan`、`Config`）全部**惰性**放进函数体内，`TYPE_CHECKING`
    分支仅供类型标注，运行时不执行。

    只要这个文件在 import 时**不**抢先拉起黑名单模块，ai/ 的自检
    基线就保持干净，工具里的 `assert_import_boundary()` 不会误报。

═══════════════════════════════════════════════════════════════════
★ 产出的必须是「建议」，不是「动作」
═══════════════════════════════════════════════════════════════════

`advise()` 返回一个 dict —— 建议里**可以**带一份 `suggested_plan`
（Plan 形状的数据，供玩家参考 / 让玩家一键复制去走 `/api/submit`），
但本层**绝不**：

    · `session.submit(...)`        ← 提交
    · `session.end_quarter(...)`   ← 结算
    · `session.start_next_quarter()` / `session.simulate(...)` 之外的写
    · `storage.save(...)`          ← 落存档
    · `advance(...)`               ← 真结算

最后一脚永远由玩家走 `/api/submit`（§11.4：AI 无 commit）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

# ══════════════════════════════════════════════════════════════════
# ★ 顶部只 import ai/ 的纯函数层 —— 见模块 docstring 的 import 顺序约定
# ══════════════════════════════════════════════════════════════════
from ai import assert_import_boundary, tool_preview, tool_validate
from ai import rules_digest
from server import llm_client

if TYPE_CHECKING:  # 仅供类型标注；运行时不 import（不起 server.session）
    from server.session import GameSession

#: 默认给模型看的规则摘要节号。§11.4 是「权限阶梯」，最能约束 AI 别越权。
DEFAULT_RULE_KEY = "11.4"

#: 系统提示核心句：
#: ①守住权限边界（无 commit、不假装执行）；
#: ②数字口径三条 —— 现成数字原样引用 / **自行推算放开但须标依据**（2026-10-17 用户拍板）/ 无依据就明说拿不到。
_SYSTEM_PREAMBLE = (
    "你是区域经营游戏的 AI 秘书，只管解释、试算、起草；提交永远由玩家走 /api/submit，你无 commit，也不得声称执行过任何操作。\n"
    "用数字分三种：\n"
    "① 局面/试算里已有的数字：原样引用，不得改动、不得凑整冒充原值。\n"
    "② 自己推算的数字：允许，但必须说明「这是推算」，写清依据（用了哪个已有数字、什么算法/口径），必要时写明假设。\n"
    "③ 既没现成的、又推算无依据的数字：不许编，明说「拿不到」，说清缺什么。"
)


class AdviceError(Exception):
    """主机层自身能分的情形（局面拿不到 / 规则节号找不到 / 玩家问题相关）。"""


# ══════════════════════════════════════════════════════════════════
# 公开入口
# ══════════════════════════════════════════════════════════════════


def advise(
    session: "GameSession",
    player_id: str = "p1",
    question: Optional[str] = None,
    *,
    rule_key: str = DEFAULT_RULE_KEY,
    preview_plan: Optional[dict] = None,
    draft: bool = False,
    client=llm_client,
) -> dict:
    """组装输入 → 调模型 → 返回**建议**（结构化 dict）。

    参数：
      · `session`       — 局面来源（本层可以碰它，ai/ 不能）；
      · `player_id`     — 给谁出主意；
      · `question`      — 玩家的问题（可空＝让模型直给一版建议）；
      · `rule_key`      — 规则摘要节号（默认 '11.4' 权限阶梯）；
      · `preview_plan`  — AI 想先试算的 Plan 形状（**dict**，由服务层转成
                          `Plan`，**只读试算**，数字全部来自 `tool_preview`）；
      · `draft`         — `True` 时请模型**自己起草一份 Plan 形状**
                          （与 `/api/submit` 的 plan 同形）；
                          `False`（默认）＝行为与旧版完全一致；
      · `client`        — LLM 客户端（**可注入假客户端做测试**，默认真网关）。

    返回 dict（形状见文件底部 `_ok_advice` / `_failure`）：

        ok / summary / suggested_plan / plan_source / plan_issues /
        evidence / model / error

      · `suggested_plan` — Plan 形状的 dict 或 None；
      · `plan_source`    — `'model'`（模型起草）/ `'preview'`（玩家传入的
                          那版）/ None；
      · `plan_issues`    — 模型起草的计划过不了校验时的问题清单
                          （此时 `suggested_plan` 置 None，**绝不把坏计划
                          交给前端**）。

    **任何失败**（局面拿不到、规则节号不对、网关没起 / 超时 / 回了垃圾）
    都翻成 `ok=False` 的 dict —— **绝不抛穿到调用方 / 绝不编造建议**。
    """
    # ── 0. 拿局面（只读）───────────────────────────────────────────
    try:
        state = session.state_for(player_id)
    except Exception as e:  # noqa: BLE001 —— 局面都拿不到，明说
        return _failure(
            "no_state",
            f"拿不到 {player_id} 的局面（{type(e).__name__}: {e}）。"
            f"没局面就没法出主意，AI 不编数字。",
            model="",
        )

    # ── 1. 拿规则摘要（只读；节号找不到 → 明说）────────────────────
    try:
        rule = rules_digest.digest(rule_key)
    except Exception as e:  # noqa: BLE001 —— 找不到这节也明说，不猜
        return _failure(
            "no_rule",
            f"基线里找不到节 {rule_key!r}（{type(e).__name__}: {e}）。",
            model="",
            state=state,
        )

    # ── 2. （可选）AI 想先试算 → 数字全部来自 tool_preview（只读）───
    preview_report: Optional[dict] = None
    preview_evidence: list[dict] = []
    if preview_plan is not None:
        try:
            # ⚠️ 惰性 import：这里才拉起 server.app 的形状转换，
            #    而 server.app 会拉起 storage / server.session ——
            #    但此时 ai/ 早已加载完成，基线已固定，不会误报。
            from server.app import plan_from_dict

            plan_obj = plan_from_dict(dict(preview_plan))
            plan_obj.player_id = player_id  # 想的这份是给谁的，钉死
            cfg = session.cfg
            player = session.state.player(player_id)

            issues = tool_validate(plan_obj, player, cfg, player_id)
            if issues:
                return _failure(
                    "plan_rejected",
                    f"想试算的那版计划没通过校验（{len(issues)} 条问题），"
                    f"AI 不拿一版非法计划去算。",
                    model="",
                    state=state,
                    issues=issues,
                )
            # 只读试算：副本跑引擎，状态一个字不动
            preview_report = tool_preview(
                session.state, {player_id: plan_obj}, cfg
            )
            preview_evidence = _evidence_from_preview(player_id, preview_report)
        except Exception as e:  # noqa: BLE001
            return _failure(
                "preview_failed",
                f"试算没跑成（{type(e).__name__}: {e}）。AI 不猜一个没算过的数。",
                model="",
                state=state,
            )

    # ── 3. 组装 messages 调模型 ─────────────────────────────────────
    evidence = _evidence_from_state(player_id, state) + preview_evidence
    messages = _build_messages(
        player_id=player_id,
        state=state,
        rule=rule,
        question=question,
        preview_report=preview_report,
        cfg=getattr(session, "cfg", None),
        draft=draft,
    )

    result = client.chat_completions(messages)

    if not result.get("ok") if isinstance(result, dict) else not result.ok:
        # 优雅降级：代理没起 / 超时 / 回了垃圾 —— **原样转达，不编建议**
        err = result.get("error") if isinstance(result, dict) else result.error
        detail = result.get("detail") if isinstance(result, dict) else result.detail
        mdl = result.get("model") if isinstance(result, dict) else result.model
        return _failure(
            err or "model_unavailable",
            f"AI 不可用：{detail or '模型没给出可用回答。'}",
            model=mdl or "",
            state=state,
            evidence=evidence,
            rule=rule,
        )

    content = result.get("content") if isinstance(result, dict) else result.content
    mdl = result.get("model") if isinstance(result, dict) else result.model
    content = (content or "").strip()

    # ⚠️ **空回答要当失败**（2026-10-17 凌晨实测抓到的）：
    #    模型回了 200 但正文为空（网关偶发 / 被截断 / 上游只回了思考块）时，
    #    原来的实现会一路走到 `_ok_advice(summary="")` → **报 ok=True 却没有建议**。
    #    那比报错更坏：前端要显示"AI 说：（空）"，玩家以为 AI 没想法，
    #    而真相是"这次没拿到回答"。draft=True 同理（空 JSON 也等于没起草）。
    if not content:
        return _failure(
            "empty_response",
            "模型返回了空回答（网关偶发，或被截断）。这次没拿到建议——"
            "可以再点一次，或稍后重试。",
            model=mdl or llm_client.model_name(),
            state=state,
            evidence=evidence,
            rule=rule,
        )

    # ── 4. draft 开路：把回答当 **Plan 候选** 解析 → 校验 → 收建议 ──
    #    ⚠️ AI 只起草数据，**绝不提交**（§11.4）：
    #       · 校验过 → suggested_plan=该对象、plan_source='model'；
    #       · 校验不过 → suggested_plan=None + plan_issues（**绝不把坏计划
    #         交给前端**）；
    #       · 取不出 JSON → 明确失败，**不编**。
    #
    #    ⚠️ 形状转换必须**在 advise 主体里做**，不能放进 _draft_advice：
    #       `plan_from_dict` 会拉起 `server.app` → 连带 `server.session` /
    #       `storage`；若在 _draft_advice 里首次拉起，ai/ 的边界自检
    #       （`assert_import_boundary`，盯 sys.modules 里新出现的黑名单模块）
    #       就会误判 ai/ 越界 —— 实测 AisImportBoundaryError 就是这么炸的。
    #       放到这里做：① 与 preview_plan 分支的惰性 import 处于同一时点
    #       （advise 主体），边界基线早已固定；② preview 分支 import 过
    #       `server.app` 之后，本分支再拉一次也只是命中 sys.modules 缓存。
    if draft:
        plan_candidate = None
        plan_candidate_error: Optional[str] = None

        # 4a. 皮实解析（复用 narrate 的 parser：剥围栏 / 跳前言 / 数括号）
        try:
            from ai.narrate import parse_model_json
            obj, problem = parse_model_json(content)
        except Exception as e:  # noqa: BLE001 —— parser 自己都不能崩
            obj, problem = None, f"parse_model_json 抛异常（{type(e).__name__}: {e}）"

        # 4b. 形状转换（**只转换、不校验**；合法性由 tool_validate 说了算）
        if obj is not None:
            try:
                from server.app import plan_from_dict

                candidate = dict(obj)
                candidate["player_id"] = player_id  # 钉死：这份起草是给谁的
                plan_candidate = plan_from_dict(candidate)
                plan_candidate.player_id = player_id
            except Exception as e:  # noqa: BLE001
                plan_candidate_error = f"{type(e).__name__}: {e}"

        return _draft_advice(
            session,
            player_id,
            plan_candidate,
            parse_problem=problem,
            shape_error=plan_candidate_error,
            evidence=evidence,
            model=mdl or llm_client.model_name(),
            rule_key=rule.get("key"),
            question=question,
        )

    # ── 5. 默认路径（**行为与旧版完全一致**）：把模型回的自由文本**收进
    #    建议形状**（不假装它必是 JSON）──────────────────────────────
    return _ok_advice(
        summary=content,
        suggested_plan=preview_plan,   # 建议带的 plan＝AI 先试算过的那版（可为 None）
        plan_source=("preview" if preview_plan is not None else None),
        plan_issues=None,
        evidence=evidence,
        model=mdl or llm_client.model_name(),
        rule_key=rule.get("key"),
        question=question,
    )


# ══════════════════════════════════════════════════════════════════
# 内部：组装 messages
# ══════════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════════
# 局面体积闸 —— 拼出来的 user 局面文本不许超过这个数（字符）。
#
# ⚠️ 为什么钉死：这段会进**每一次**请求。上下文一涨，① 烧 token、
#    ② 把重点（玩家的真问题、规则摘要、试算数字）淹掉。
#    超了就砍**次要项**（先砍"没变化的价格"，绝不砍核心键），
#    绝不允许随商品数/地块数无限增长。
# ══════════════════════════════════════════════════════════════════
_STATE_BODY_LIMIT = 4000

# ══════════════════════════════════════════════════════════════════
# ★ draft 开路时的额外要求（**只放在 user message 里，不动 _SYSTEM_PREAMBLE**）
# ══════════════════════════════════════════════════════════════════
#
# 为什么单独钉一段：draft=True 时要模型**输出 JSON 而不是人话**。
# 但 system preamble 是用户刚拍板的 207 字符三段式，一字不动 ——
# 起草的额外要求（JSON 形状、只列必填字段、只输出 JSON）放 user message，
# 模型照读，旧 system 一字不碰。
_DRAFT_JSON_INSTRUCTION = (
    "\n\n【起草一份计划（draft）】本次请**你自己起草**一份计划。"
    "只输出一个 JSON 对象，不要解释，不要代码围栏，不要多余文字。\n"
    "形状（与 /api/submit 的 plan 同形）：\n"
    "{\n"
    '  "player_id": "<玩家 ID>",\n'
    '  "plot_plans": {"<plot_id>": {"plot_id": "<plot_id>", '
    '"active": true, "u": 1.0}},\n'
    '  "purchases": {"<商品>": <数量>},\n'
    '  "food": {"<商品>": <数量>},\n'
    '  "builds": [\n'
    "    {\n"
    '      "plot_id": "<坐标 r,c>",\n'
    '      "plot_type": "<类型>",\n'
    '      "target_level": <目标等级>,\n'
    '      "product": "<产物/路线或 null>"\n'
    "    }\n"
    "  ]\n"
    "}\n"
    "要求：\n"
    "· 只能从上面局面里 [可建] 选出能建的类型/产物，不要自己编；\n"
    "· player_id 必须写「{player_id}」；\n"
    "· 地块坐标用「r,c」格式（从 0 起算）；\n"
    "· 每个 builds 都要给出 plot_type 与 target_level（新建为 1），"
    "农业/工业还要给 product；\n"
    "· 现金够不够不用你在文字里算 —— 校验由引擎做，你只管起草。"
)


def _build_messages(
    *,
    player_id: str,
    state: dict,
    rule: dict,
    question: Optional[str],
    preview_report: Optional[dict],
    cfg=None,
    draft: bool = False,
) -> list[dict]:
    """把局面 / 规则摘要 / 试算数字 / 玩家问题拼成 OpenAI messages。"""
    lines = [
        f"[当前局面 · 玩家 {player_id}]",
        _compact_state(player_id, state, cfg),
        "",
        f"[规则摘要 · 基线 §{rule.get('key')}]",
        rule.get("summary", ""),
    ]
    if preview_report is not None:
        lines += [
            "",
            "[试算结果 · 这版计划跑一遍会怎样（数字全部来自引擎试算，不是编的）]",
            _compact_preview(preview_report),
        ]
    else:
        lines += [
            "",
            "[试算结果] 本次没有先试算任何计划；要引用数字只能引用上面的局面。",
        ]

    user_body = "\n".join(lines)
    if question:
        user_body += f"\n\n[玩家问题]\n{question}"
    else:
        user_body += (
            "\n\n请基于以上局面与规则，给玩家一版本季建议（人话，分点，"
            "引用的数字必须与上面给出的局面/试算一致）。"
        )

    # draft 开路时追加"输出 JSON"的硬要求（只加在 user message，
    # 不动 system preamble —— 用户拍板的 207 字三段式一字不改）。
    if draft:
        user_body += _DRAFT_JSON_INSTRUCTION.replace("{player_id}", player_id)

    return [
        {"role": "system", "content": _SYSTEM_PREAMBLE},
        {"role": "user", "content": user_body},
    ]


def _fmt(v) -> str:
    """一个数字 → 字符串，**原样转发**（不四舍五入、不换算单位、不凑整）。

    ⚠️ 这是「压缩要信息保真」的落点：引擎给的什么数，AI 读到什么数。
       唯一允许的处理是「去掉 float 的尾巴 .0」（1200.0 → "1200"），
       那**不是改值**，只是把整数的 float 显示成整数，值一字不差。
    """
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _core_lines(state: dict) -> list[str]:
    """★ 七个核心键 —— **一个都不能丢**，而且**永远满额给**，体积再紧也不砍。

    （这就是拓宽前 AI 手里仅有的全部信息。拓宽是**加**东西，不是把这七项
       换成别的 —— 否则会出现"加了价格表反而把现金丢了"的倒退。）
    """
    import json

    out = [
        f"quarter={state.get('quarter')}/{state.get('total_quarters')}"
        f" phase={state.get('phase')} can_settle={state.get('can_settle')}",
        f"cash={_fmt(state.get('cash'))} happiness={_fmt(state.get('happiness'))}",
    ]
    pop = state.get("population")
    if pop is None:
        # ⚠️ population=null 的真相是「**玩家还没提交计划**，所以拿不到」，
        #    不是「没有人口」。不点破这句，AI 会推断成"这局没人/不需要人"。
        out.append("population=null（没提交计划所以拿不到，不是没有人口）")
    else:
        out.append("population=" + json.dumps(pop, ensure_ascii=False, default=str))
    return out


#: 价格块**行数兜底**：再怎么会变，一次对话最多带这么多行商品价格。
#: （正常局 15 种全带；这个只在商品数失控时才嘎住，是终极兜底。）
_MARKET_MAX_ROWS = 25


def _market_lines(
    state: dict, *, only_changed: bool = False, max_rows: int = _MARKET_MAX_ROWS
) -> list[str]:
    """市场/价格块：每商品一行 `goods|中文名|上季价|本季求价|触没触底`。

    ★ 只给 AI 答得动的四样：中文名 / 上季结算价（=本季买入记账价）/
       本季求价 `P_t` / 是否触底。P0、P_min 都是**从这几样推出来的**,
       不重复给（省地方），AI 需要时可用试算。

    ``only_changed=True`` 是体积闸的**降级档**：超限时只留"本季求价跟上季比
    变了"或"触底"的商品。``max_rows`` 是**终极兜底**：不该发生，但商品数
    一旦失控（配置被改怪），只留前 N 行 + 明说略，**绝不让这一个块掀翻
    字数上限**。
    """
    market = state.get("market")
    if not isinstance(market, dict) or not market:
        return []
    rows: list[str] = []
    for goods in sorted(market):
        blk = market.get(goods)
        if not isinstance(blk, dict):
            continue
        cur = blk.get("current_price")
        last = blk.get("last_price")
        floor = blk.get("at_floor")
        if only_changed and cur is not None and cur == last and not floor:
            continue  # 没变化也没触底 = 次要项，体积紧时先砍它
        rows.append(
            f"{goods}|{blk.get('name')}|上季{_fmt(last)}|"
            f"本季{_fmt(cur) if cur is not None else 'null(未试算)'}|"
            f"{'触底' if floor else '正常'}"
        )
    if not rows:
        return []
    head = "[市场] goods|名|上季价|本季求价|触底"
    if len(rows) > max_rows:  # 终极兜底：截断 + 明说，绝不让这快掀翻上限
        kept = rows[:max_rows]
        kept.append(
            f"(价格行过多，只列前 {max_rows} 项，"
            f"其余 {len(rows) - max_rows} 项略，需要细节请走试算)"
        )
        return [head, *kept]
    omitted = len(market) - len(rows)
    body = [head, *rows]
    if omitted > 0:
        body.append(f"(其余 {omitted} 种商品价格无变化且未触底，略)")
    return body


def _plot_lines(state: dict) -> list[str]:
    """本地地块块：只画玩家**自己的**地（state['plots'] 本来就只有自己的）。

    ★ 每块一行：plot_id / 类型 / 等级 / 状态 / 开工 u / 本季产出预览。
       efficiency 的四分量拆解、running_quarters 那些给前端悬停看的，
       不进对话 —— AI 要细节时用试算，不把整张表灌进来。
    """
    plots = state.get("plots")
    if not isinstance(plots, list):
        return []
    rows: list[str] = []
    for p in plots:
        if not isinstance(p, dict):
            continue
        u = p.get("u")
        prev = p.get("output_preview") or {}
        qty = prev.get("qty")
        produce = (
            f"产{prev.get('goods')}×{_fmt(qty)}" if prev.get("goods") else "无产出"
        )
        rows.append(
            f"{p.get('plot_id')}|{p.get('plot_type')}|L{p.get('level')}|"
            f"{p.get('status')}|产出{p.get('product')}|"
            f"u={_fmt(u) if u is not None else 'null'}|{produce}"
        )
    if not rows:
        return ["[地块] （还没有任何地块）"]
    return [f"[地块·{len(rows)}块] id|类型|级|状态|产出|u|本季预览", *rows]


def _buildoption_lines(cfg) -> list[str]:
    """建造选项块：只给"**能新建的**"类型与**现金费**（材料费全省）。

    ★ 为什么只给新建的：`from_level=0` 是开局唯一走得通的入口（升级要
       先有地）。为什么省材料费：材料走市场、按上季价折算，AI 真要比价
       该走试算；把 15 种材料配方灌进对话是体积灾难。
       ⚠️ 现金费是**原价**；开局减免几人一名额、半价这些，规则摘要有，
          AI 会自己折算（口径：推算须标依据）—— 不在此预先打折。
    """
    if cfg is None:
        return []
    try:
        from engine import construction as _C

        works = _C.load_works(cfg)
    except Exception:  # noqa: BLE001 —— 建造表拿不全，就诚实缺席
        return []
    rows = [
        f"{w.plot_type.value}|{w.label}|现金{_fmt(w.cash)}"
        for w in sorted(works.values(), key=lambda w: (w.plot_type.value, w.to_level))
        if w.is_new
    ]
    if not rows:
        return []
    return [f"[可建·现金费·原价] {len(rows)}类（材料费另有，走试算）", *rows]


def _compact_state(player_id: str, state: dict, cfg=None) -> str:
    """局面压成模型读得动的几行 —— **核心七键 + 价格/地块/建造**。

    结构（一次成型，不做多遍重排）：
      ① 核心七键（永远满额）          ← 拓宽前就有的，一个不许丢
      ② 市场 / 本地块 / 可建选项（拓宽的新内容）
      ③ 末尾自觉体积：总字符超 `_STATE_BODY_LIMIT` 时，
         价格从「全量」降为「只留有变化/触底的」，仍超再砍建造选项，
         **核心七键与地块绝不砍**。
    """
    core = _core_lines(state)
    plots = _plot_lines(state)
    options = _buildoption_lines(cfg)
    market_full = _market_lines(state, only_changed=False)

    def _join(market_rows, opt_rows) -> str:
        return "\n".join(core + market_rows + plots + opt_rows)

    # ── 体积闸：三档降级，每档都是"砍次要项"而不是"砍上限" ──────────
    body = _join(market_full, options)
    if len(body) > _STATE_BODY_LIMIT:
        # 一档：价格只留变化的 / 触底的
        body = _join(_market_lines(state, only_changed=True), options)
    if len(body) > _STATE_BODY_LIMIT:
        # 二档：把建造选项整块拿掉（比价格次要 —— 价格影响当季买卖）
        body = _join(_market_lines(state, only_changed=True), [])
    # ★ 断言重付：到这里 **必须** < 上限。若连"核心+地块+精简价格"都超了，
    #    说明某个分项失控了 —— 让它**当场显形**，不许带病放行。
    assert len(body) <= _STATE_BODY_LIMIT, (
        f"局面压缩后仍 {len(body)} 字符（上限 {_STATE_BODY_LIMIT}）："
        f"某个分项失控，不许无限增长。"
    )
    return body


def _compact_preview(report: dict) -> str:
    """试算报告挑出「人会关心的几行」给模型（清单来自 QuarterReport.to_dict()）。"""
    keys = (
        "quarter", "cash_before", "cash_after", "total_deduction",
        "sales_revenue", "wages", "materials_cost", "value_added",
    )
    picked = {k: report.get(k) for k in keys if k in report}
    import json

    return json.dumps(picked, ensure_ascii=False, default=str)


# ══════════════════════════════════════════════════════════════════
# 内部：evidence —— AI 引用了哪些数字。**来源必须能指回去。**
# ══════════════════════════════════════════════════════════════════


def _evidence_from_state(player_id: str, state: dict) -> list[dict]:
    """局面里被递过去的数字（source='state' —— 来自 `state_for`）。"""
    out: list[dict] = []
    for k in ("cash", "quarter", "happiness", "population"):
        if k in state:
            out.append({
                "source": "state",
                "field": k,
                "value": state.get(k),
                "player_id": player_id,
            })
    return out


def _evidence_from_preview(player_id: str, report: dict) -> list[dict]:
    """试算里被递过去的数字（source='preview' —— 来自 `tool_preview`）。

    ⚠️ 这里的**键就是 QuarterReport.to_dict() 的键**：若引擎改了字段名，
       evidence 就拿不到对应值（而不是造一个出来）—— 拿不到的字段 AI 就该
       说「拿不到」，这本身就是「不许编数字」的落点。
    """
    out: list[dict] = []
    for k in ("cash_before", "cash_after", "total_deduction",
              "sales_revenue", "value_added"):
        if k in report:
            out.append({
                "source": "preview",
                "field": k,
                "value": report.get(k),
                "player_id": player_id,
            })
    return out


# ══════════════════════════════════════════════════════════════════
# 内部：两个出口（建议 / 失败）—— 形状钉死，前端只显示
# ══════════════════════════════════════════════════════════════════


def _ok_advice(
    *,
    summary: str,
    suggested_plan: Optional[dict],
    plan_source: Optional[str] = None,
    plan_issues: Optional[list[dict]] = None,
    evidence: list[dict],
    model: str,
    rule_key: Optional[str],
    question: Optional[str],
) -> dict:
    """成功的建议形状。**`suggested_plan` 可为 None**（模型没起草出计划时）。

    ⚠️ 这里**不收空 summary**：调用方必须先确认正文非空（见 `advise` 里的
       `empty_response` 分支）。留这道断言是为了让"假装成功"不可能再溜进来。
    """
    assert summary.strip(), "空 summary 不许走成功路径 —— 应报 empty_response"
    return {
        "ok": True,
        "summary": summary,
        "suggested_plan": suggested_plan,
        "plan_source": plan_source,
        "plan_issues": plan_issues,
        "evidence": evidence,
        "model": model,
        "rule_key": rule_key,
        "question": question,
        "error": "",
        # 权限阶梯的落点，前端照着这句话做按钮就行
        "next_step": "玩家确认后由前端走 /api/submit 提交（AI 无 commit）",
    }


def _draft_advice(
    session: "GameSession",
    player_id: str,
    plan_obj,
    *,
    parse_problem: Optional[str],
    shape_error: Optional[str],
    evidence: list[dict],
    model: str,
    rule_key: Optional[str],
    question: Optional[str],
) -> dict:
    """draft=True 的收尾：把模型回的 JSON 解析成 Plan 候选 → 校验 → 收建议。

    三条出口（对应任务 A 的铁律）：

      ① 模型连 JSON 都没给出（一堆废话 / 全不是 JSON）→ **明确失败**（不编）；
      ② 给了 JSON，但 **validate 不过** → `suggested_plan=None` + `plan_issues`
         （**绝不把坏计划交给前端**）；
      ③ 给了 JSON 且 **validate 通过** → `suggested_plan=该对象`、
         `plan_source='model'`。
    """
    # ── ① 模型连 JSON 都没给（废话 / 全不是 JSON）→ 明确失败（不编）───
    if plan_obj is None and parse_problem:
        return _ok_advice(
            summary=(
                f"模型没给出一份能解析的计划 JSON（{parse_problem}）。"
                "这次起草被放弃——玩家仍得自己填表，或再点一次重新起草。"
            ),
            suggested_plan=None,
            plan_source=None,
            plan_issues=None,
            evidence=evidence,
            model=model,
            rule_key=rule_key,
            question=question,
        )

    # ── ② 给了 JSON，但翻不成 Plan（乱给键名 / 非法枚举值 / 缺必填）───
    if plan_obj is None:
        return _ok_advice(
            summary=(
                f"模型给的 JSON 翻不成 Plan（{shape_error or '未知形状错误'}）。"
                "这次起草被放弃——玩家仍得自己填表，或再点一次重新起草。"
            ),
            suggested_plan=None,
            plan_source=None,
            plan_issues=None,
            evidence=evidence,
            model=model,
            rule_key=rule_key,
            question=question,
        )

    # ── ③ 引擎校验：与 /api/submit 走同一条 validate（只读）─────────────
    #    ⚠️ 为什么不能直接调 ai.tools.tool_validate：
    #       tool_validate 自带**第一次自省** `assert_import_boundary()`，
    #       用 sys.modules 倒推「ai/ 有没有把 server.session/storage 拉进来」。
    #       生产链不会炸（server.app 先起，ai 后 import，基线认得 server.* 是
    #       既成事实）；**测试链**先 import ai 再拉起 server.app（FakeClient
    #       只能这么搭），`server.registry/session` 必然后于 ai 进驻
    #       sys.modules，自检**必炸**。
    #       —— 这条自检的任务是「ai/ 内部不许越界」，不是「主机层不许校验」；
    #          ai_host 本来就是 server 层，它调 engine.validate 天经地义。
    #       所以走 tool_draft_plan 里的**同一条** engine.validate.validate_plan
    #       （任务书给的备选校验路径），与 /api/submit 一个入口、一份 issues。
    from engine import validate as _engine_validate
    try:
        cfg = session.cfg
        player = session.state.player(player_id)
        issues = [i.to_dict() for i in
                  _engine_validate.validate_plan(plan_obj, player, cfg, player_id)]
    except Exception as e:  # noqa: BLE001
        return _ok_advice(
            summary=(
                f"起草出来的计划跑校验时出了意外（{type(e).__name__}: {e}）。"
                "这次起草被放弃——玩家仍得自己填表，或再点一次重新起草。"
            ),
            suggested_plan=None,
            plan_source=None,
            plan_issues=None,
            evidence=evidence,
            model=model,
            rule_key=rule_key,
            question=question,
        )

    # ── ④ 三种情况分出口 ──────────────────────────────────────────
    if issues:
        # 校验不过：suggested_plan=None + plan_issues（**绝不把坏计划交出去**）
        plan_dict = plan_obj.to_dict()
        return _ok_advice(
            summary=(
                f"模型起草的计划没过校验（{len(issues)} 条问题），"
                "这次起草被放弃——玩家仍得自己填表，或再点一次重新起草。"
            ),
            suggested_plan=None,
            plan_source="model",          # 它**确实**是模型起草的，只是不过
            plan_issues=issues,
            evidence=evidence,
            model=model,
            rule_key=rule_key,
            question=question,
        )

    # 校验通过：suggested_plan=该对象、plan_source='model'
    # 这里的 summary 不能是 JSON 原文（太长），给玩家一句能看懂的说明。
    plan_dict = plan_obj.to_dict()
    n_builds = len(plan_dict.get("builds") or [])
    n_plots = len(plan_dict.get("plot_plans") or {})
    summary = (
        "模型起草了一份计划" +
        (f"（含 {n_builds} 条建造、{n_plots} 块地的安排）" if (n_builds or n_plots) else "") +
        "。下方 `suggested_plan` 就是它，玩家确认后可一键复制走 /api/submit。"
    )
    return _ok_advice(
        summary=summary,
        suggested_plan=plan_dict,
        plan_source="model",
        plan_issues=None,
        evidence=evidence,
        model=model,
        rule_key=rule_key,
        question=question,
    )


def _failure(
    error: str,
    detail: str,
    *,
    model: str,
    state: Optional[dict] = None,
    evidence: Optional[list[dict]] = None,
    rule: Optional[dict] = None,
    issues: Optional[list[dict]] = None,
) -> dict:
    """失败的建议形状。**ok=False + 明确原因**，让前端知道这不是建议。"""
    out: dict[str, Any] = {
        "ok": False,
        "summary": "",
        "suggested_plan": None,
        "evidence": evidence or [],
        "model": model,
        "rule_key": rule.get("key") if rule else None,
        "error": error,
        "detail": detail,
    }
    if issues:
        out["issues"] = issues
    return out
