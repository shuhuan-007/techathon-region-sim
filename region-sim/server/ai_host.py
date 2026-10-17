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
from ai.plan_compile import compile_draft
from ai.plan_edit import (
    extract_why,
    merge_edits,
    plan_action_of,
    plan_has_content,
)
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
    current_plan: Optional[dict] = None,
    mode: str = "auto",
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
      · `current_plan`  — 前端表单里的**当前草稿**（未提交也算）。非空时走
                          **编辑模式**（`AI交互与建设撤销.md` §三）：以它为 base
                          合并模型的 `remove_builds` / `builds` / `plot_plans`，
                          并且**不默认全开工、不补基准口粮**；
      · `mode`          — `"auto" | "answer" | "plan"`，由服务层翻成 `draft`
                          （本层只收，不重复判关键词）；
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
    # ⚠️ `with_narratives=False`：起草只需要数字事实，**不需要**新闻/财报叙事。
    #    默认的 `state_for()` 会在 quarter>1 时顺手调两路模型生成叙事
    #    （实测单次可到 1–3 分钟），把「点一下 AI 起草」拖成几分钟；
    #    而叙事正文根本不进 `_compact_state`，纯属白等。
    try:
        state = session.state_for(player_id, with_narratives=False)
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
    # ★ 可行动作快照（2026-10-18）：空地 / 可建材料 / 可开工 / 口粮需求。
    #   模型之前只能看到“自己的地块”，于是把已有地块写成 builds —— 这份快照
    #   就是给它一份“能往哪建、要花什么、哪些地能开工”的 ground truth。
    cap_text = ""
    try:
        from ai.plan_context import describe_capabilities, format_capabilities

        cap_text = format_capabilities(
            describe_capabilities(session.state, player_id, session.cfg)
        )
    except Exception as e:  # noqa: BLE001 —— 快照拿不到就诚实缺席，不编
        cap_text = (
            f"[可行动作] 生成失败（{type(e).__name__}: {e}）；"
            f"不要臆造空地/成本，按上面已有的局面回答。"
        )

    # 机制问答的规则摘录：**先算好**（后面还要用它兜底校验模型有没有答到公式）
    rule_excerpts = [] if draft else _rule_excerpts(question)

    # ★ 纯机制题（“粮食怎么算/公式是什么”）**直接回规则原文**，不调模型。
    #   2026-10-18 真跑：即使把 §3.3.3 原文塞进 user message，qwen-flash 仍答成
    #   “当前人口=0 ⇒ 口粮=0”，完全不给 F_基准/0.4 配额。机制题要的是准确，
    #   不是文采；直接给基线原文最稳（也最快，0 模型调用）。
    #   ⚠️ 只在默认真客户端上走这条；测试注入 FakeClient 时仍走原链路。
    if (
        not draft
        and rule_excerpts
        and _is_pure_rule_question(question)
        and client is llm_client
    ):
        return _ok_advice(
            summary=_rule_answer_text(rule_excerpts),
            suggested_plan=None,
            plan_source=None,
            plan_issues=None,
            plan_action=None,
            evidence=evidence,
            model="规则原文",
            rule_key=rule_excerpts[0].get("key") or rule.get("key"),
            question=question,
        )

    messages = _build_messages(
        player_id=player_id,
        state=state,
        rule=rule,
        question=question,
        preview_report=preview_report,
        cfg=getattr(session, "cfg", None),
        draft=draft,
        cap_text=cap_text,
        # ★ 机制问答查规则（2026-10-18）：draft=False 时按问题关键词附 1–3 节
        #   基线**原文**摘录（问"粮食怎么算"就有 §3.3.3 的原文在手）；
        #   draft=True 不传 —— 起草只要局面 + 规则摘要，保持旧行为。
        rule_excerpts=rule_excerpts,
        # ★ 编辑模式（2026-10-18）：把玩家**表单里的草稿**给模型看，
        #   否则它无法"只删 0,0、保留其余"（只能重填整份 = 覆盖玩家手填）。
        current_plan=current_plan,
    )

    # ★ 明确撤销建造：**确定性意图解析**，不依赖模型。
    #   玩家说“取消 9,9 的建造”时，模型仍可能回一份整单计划/什么都不删；
    #   这里先用正则取出问题里的 plot_id，和 current_plan 的 builds 求交，
    #   命中就直接生成 `remove_builds` 编辑，让编译器只删这几条、保留其余。
    explicit_cancel_ids: list[str] = []
    if draft:
        explicit_cancel_ids = _explicit_cancel_ids(question, current_plan)

    if explicit_cancel_ids:
        import json as _json
        content = _json.dumps(
            {
                "remove_builds": explicit_cancel_ids,
                "why": "已按你的要求撤销 "
                       + "、".join(explicit_cancel_ids)
                       + " 的建造，其余安排保持不变。",
            },
            ensure_ascii=False,
        )
        mdl = "规则/意图解析"
        result = {"ok": True, "model": mdl, "content": content}
    else:
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

    # ★ 机制问答的**确定性兜底**（2026-10-18 真跑抓到）：
    #   即使把 §3.3.3 原文塞进 user message，qwen-flash 仍会答成
    #   “当前人口=0 ⇒ 口粮=0”，完全不提 F_基准 / 0.4 配额。
    #   对“怎么算/公式/规则”这类**纯机制题**，如果模型回答没覆盖摘录里的
    #   关键公式，就直接用基线原文替换答案 —— 宁可不“会说话”，也不能答错规则。
    if rule_excerpts and not draft:
        content = _ensure_rule_answer(content, question, rule_excerpts)

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
        compiled = None
        plan_candidate_error: Optional[str] = None

        # 4a. 皮实解析（复用 narrate 的 parser：剥围栏 / 跳前言 / 数括号）
        try:
            from ai.narrate import parse_model_json
            obj, problem = parse_model_json(content)
        except Exception as e:  # noqa: BLE001 —— parser 自己都不能崩
            obj, problem = None, f"parse_model_json 抛异常（{type(e).__name__}: {e}）"

        # 4b. ★ 编译器（2026-10-18）：模型给的是“意图”，引擎给的是“事实”。
        #     `compile_draft` 负责把已有地块当建造、漏写开工、超预算、非法产物
        #     等模型常见错误规整掉，并且**只有 validate + preview 都过**才返回 plan。
        #     —— 这一步取代了旧的 `plan_from_dict → tool_validate` 裸转换：
        #        旧路径只要模型写错一个字，整份草案就作废（真跑实测的故障）。
        # ★ 编辑模式（2026-10-18，《AI交互与建设撤销.md》§三）：
        #    `current_plan` 非空 → 先把模型的轻量编辑合并到玩家草稿上，
        #    再编译。默认值按草稿内容决定：
        #      · 玩家已写 plot_plans → default_run_all=False，绝不改未列出的地块；
        #      · 玩家只写了 builds（plot_plans 空）→ default_run_all=True，
        #        否则“让 AI 取消一条建造”会把现有生产全停；
        #      · 同理，plot_plans 空且 food 空才补基准口粮；写了就原样保留。
        #    `current_plan` 为空 → 旧路径，一个参数都不加（默认 True）。
        editing = plan_has_content(current_plan)
        protected_ids: set[str] = set()
        # 编辑模式的两个默认值：
        #   · 玩家**已经写了** plot_plans → 绝不替他补/改未提到的地块；
        #   · 玩家只写了 builds（plot_plans 空）→ 说明他还没排开工，默认全开
        #     是安全的，否则“只让 AI 取消一条建造”会把所有现有地块停掉。
        #   口粮同理：没写 food 才补基准档；写了就原样保留。
        default_run_all = True
        default_baseline_food = True
        if editing and isinstance(current_plan, dict):
            for b in (current_plan.get("builds") or []):
                if isinstance(b, dict):
                    pid = b.get("plot_id")
                    if isinstance(pid, str) and pid.strip():
                        protected_ids.add(pid.strip())
            default_run_all = not bool(current_plan.get("plot_plans"))
            # 只写了 builds（连 plot_plans 都没有）时，才补基准口粮；
            # 玩家已经排了开工、只是没填 food → 尊重“这季不买口粮”。
            default_baseline_food = (
                not bool(current_plan.get("plot_plans"))
                and not bool(current_plan.get("food"))
            )
        if obj is not None:
            try:
                if editing:
                    # 玩家草稿里**没有** plot_plans（只排了建造 / 只让 AI 撤一条）时，
                    # 不许模型顺手把“全部地块 u=0”写进来 —— 那会把现有生产全停。
                    # 这种情形下开工安排交给 default_run_all，模型只改 builds。
                    if (
                        isinstance(obj, dict)
                        and not (current_plan or {}).get("plot_plans")
                    ):
                        obj = {k: v for k, v in obj.items() if k != "plot_plans"}
                    request = merge_edits(obj, current_plan)
                    compiled = compile_draft(
                        request, session.state, player_id, session.cfg,
                        default_run_all=default_run_all,
                        default_baseline_food=default_baseline_food,
                        # ★ 编辑模式**不搬玩家/模型已选的格**：玩家草稿里的
                        #   9,8 不能因为“零邻接”被偷偷搬到 1,0，否则保护名单
                        #   按 plot_id 对不上，缩量时还是会把它删掉（真跑抓到）。
                        auto_place=False,
                        protected_build_ids=protected_ids,
                    )
                else:
                    compiled = compile_draft(
                        obj, session.state, player_id, session.cfg
                    )
            except Exception as e:  # noqa: BLE001 —— 编译器自己都不能崩
                compiled = None
                plan_candidate_error = f"{type(e).__name__}: {e}"

        return _draft_advice(
            player_id,
            compiled=compiled,
            parse_problem=problem,
            shape_error=plan_candidate_error,
            evidence=evidence,
            model=mdl or llm_client.model_name(),
            rule_key=rule.get("key"),
            question=question,
            why=extract_why(obj),
            plan_action=plan_action_of(obj, current_plan),
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
        plan_action=None,   # 问答分支没有"对草稿做了什么"
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
    '  "builds": [\n'
    "    {\n"
    '      "plot_id": "<坐标 r,c>",\n'
    '      "plot_type": "<类型>",\n'
    '      "target_level": <目标等级>,\n'
    '      "product": "<产物/路线或 null>"\n'
    "    }\n"
    "  ],\n"
    '  "plot_plans": {"<已建成地块的 plot_id>": {"plot_id": "<同上>", '
    '"active": true, "u": 1.0}},\n'
    '  "purchases": {"<商品>": <数量>},\n'
    '  "food": {"<商品>": <数量>}\n'
    "}\n"
    "★ 两条最容易错、必须遵守：\n"
    "① `builds` 只能建在 **[空地]** 列出的格子上；你**已有**的地块绝不能写进 "
    "builds（想让它开工就写 `plot_plans`）。\n"
    "② **要开工必须写进 `plot_plans`；漏写 = 本季不开工。** 建议把 [可开工] "
    "里的地块都写上 u=1.0（不想开就写 0）。\n"
    "要求：\n"
    "· 类型/产物只能从 [可建] 选：农业 product 填 A/B/C/D；工业 1 级填 G；"
    "2 级填 H-A/H-F/H-G/H-J（研究线填 `研究`）；3 级填 "
    "H-A+/H-F+/H-G+/H-J+/I；\n"
    "· 空地新建只能 `target_level: 1`；升级才是 2/3，且一次只能升一级；\n"
    "· 现金/材料/口粮都要钱：优先只建 [可建] 里总成本估得起的数量；"
    "引擎会试算，超预算会自动缩量（但不会替你选路线）；\n"
    "· 口粮可省略（引擎按 [人口/口粮] 的基准档补）；想冲优秀档就按优秀档填 "
    "A/B/C/D；\n"
    "· 用不到的字段可以省略或留空，但 **player_id 必须写「{player_id}」**。\n"
    "★ 两个**可选**字段（玩家要撤销 / 要一句解释时很有用）：\n"
    '· "why": "一句话：做了什么 + 为什么"（**只进聊天摘要，不参与规则**）；\n'
    '· "remove_builds": ["0,0"] —— 玩家要求**撤销**某些建造时，只列这些格子，'
    "**不要重填整份计划**。\n"
    "⚠️ 如果上面给了 **[当前草稿]**：那是玩家**已有的草稿**（未提交）。"
    "只输出**改动**——要撤销的格子 / 要追加或替换的 builds / 要改的 "
    "plot_plans·purchases·food；**没提到的东西一律保持原样，不要重填整份**。\n"
    "⚠️ 仍然只输出一个 JSON 对象：不要解释、不要代码围栏。"
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
    cap_text: str = "",
    rule_excerpts: Optional[list[dict]] = None,
    current_plan: Optional[dict] = None,
) -> list[dict]:
    """把局面 / 可行动作 / 规则摘要 / 规则摘录 / 试算数字 / 当前草稿 / 问题
    拼成 OpenAI messages。"""
    lines = [
        f"[当前局面 · 玩家 {player_id}]",
        _compact_state(player_id, state, cfg),
    ]
    if cap_text:
        lines += ["", cap_text]
    lines += [
        "",
        f"[规则摘要 · 基线 §{rule.get('key')}]",
        rule.get("summary", ""),
    ]
    # ★ 机制问答（draft=False）：按问题关键词附 1–3 节**基线原文**。
    #   draft=True 时 advise 传空 —— 起草只要局面 + 规则摘要，保持旧行为。
    excerpts = [e for e in (rule_excerpts or []) if isinstance(e, dict)][:3]
    if excerpts:
        lines += [
            "",
            "[规则摘录] 下面是从《规则基线》按你的问题取的**原文**"
            "（节号即出处；没取到的不编）。",
        ]
        for ex in excerpts:
            headline = f"[规则摘录 · §{ex.get('key')}]"
            title = str(ex.get("title") or "").strip()
            if title:
                headline += f" {title}"
            lines += [headline, str(ex.get("summary") or "")]
        # ⚠️ 真跑教训：只给原文，模型仍会答“当前人口=0 ⇒ 口粮=0”。
        #    机制题必须**先讲规则公式**，再用当前局面举例。
        lines += [
            "",
            "[回答要求] 这是机制解释题：先引用 [规则摘录] 的节号与公式，"
            "再用当前局面数字举例；不要把“当前人口=0/当前没有地块”"
            "这类局面值当成规则本身。",
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

    # ★ 当前草稿（2026-10-18，编辑模式）：把玩家表单里**已有的安排**给模型看。
    #   不给这块，模型只能重填整份 —— 那就一定会覆盖玩家手填的东西
    #   （用户明确要求："自己填不要改动玩家已经手动填的"）。
    if draft and plan_has_content(current_plan):
        lines += [
            "",
            "[当前草稿 · 玩家表单里的安排（未提交；请在此基础上**只输出改动**）]",
            _compact_current_plan(current_plan),
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


# ══════════════════════════════════════════════════════════════════
# ★ 机制问答：按问题关键词取基线原文（《AI交互与建设撤销.md》§四）
# ══════════════════════════════════════════════════════════════════
#
# 口径：**只取原文，不编**。关键词命中哪几节就取哪几节，最多 3 节；
# 一节都没命中就返回空列表（模型按旧行为自由回答，不许自己"补"规则）。
# 顺序即优先级（排前面的先取）。
_RULE_KEYWORD_SECTIONS: tuple[tuple[tuple[str, ...], str], ...] = (
    # ⚠️ 粮食/口粮必须取 **3.3.3 两档粮食需求**：公式与 A/B/C/D 配额都在
    #    这一小节；取 3.3 只会拿到本节开头两句，“怎么算”答不出公式
    #    —— 2026-10-18 真跑抓到（模型答成“当前人口=0，所以口粮=0”）。
    (("粮食", "口粮"), "3.3.3"),
    (("幸福",), "3.3.4"),
    (("人口",), "3.3.1"),
    (("邻接", "加成"), "3.2"),
    (("价格", "市场"), "4.1"),
    (("建造", "工程"), "5.1"),
    (("效率", "磨合"), "3.1"),
    (("税",), "7.1"),
    (("合同",), "7.2"),
    (("开局", "减免"), "5.3"),
    (("AI 边界", "AI边界", "边界", "权限", "越权"), "11.4"),
)

#: 一次最多附几节原文（多了会淹掉问题本身）
_RULE_EXCERPTS_MAX = 3


def _rule_excerpts(question: Optional[str]) -> list[dict]:
    """按问题关键词取 1–3 节基线**原文摘要**（`rules_digest.digest`）。

    返回 `[{key, title, summary}, ...]`；关键词一个都没命中、或基线里取不到
    → **空列表**（不编、不猜）。节号/正文全部来自基线文件本身。
    """
    if not question or not isinstance(question, str):
        return []

    keys: list[str] = []
    for words, key in _RULE_KEYWORD_SECTIONS:
        if len(keys) >= _RULE_EXCERPTS_MAX:
            break
        if key in keys:
            continue
        if any(word and word in question for word in words):
            keys.append(key)

    out: list[dict] = []
    for key in keys:
        try:
            digest = rules_digest.digest(key)
        except Exception:  # noqa: BLE001 —— 这一节拿不到就跳过，不编
            continue
        summary = str(digest.get("summary") or "").strip()
        if not summary:
            continue
        out.append({
            "key": digest.get("key") or key,
            "title": str(digest.get("title") or ""),
            "summary": summary,
        })
    return out[:_RULE_EXCERPTS_MAX]


#: 纯机制题的识别词（只问规则本身，不像在问“我这局为什么”）。
_PURE_RULE_WORDS: tuple[str, ...] = (
    "怎么算", "怎么计算", "如何计算", "公式", "规则是什么", "机制是什么",
)

#: 一旦出现这些词，就是“结合我这局”的问题，不用规则原文硬替换模型的回答。
_PERSONAL_WORDS: tuple[str, ...] = (
    "我", "当前", "这局", "本局", "本季", "为什么",
)


def _is_pure_rule_question(question: Optional[str]) -> bool:
    """是不是“纯问规则公式”的机制题（不是“我这局为什么…”）。"""
    s = str(question or "")
    if not any(w in s for w in _PURE_RULE_WORDS):
        return False
    return not any(w in s for w in _PERSONAL_WORDS)


def _mentions_rule_terms(content: str, excerpts: list[dict]) -> bool:
    """模型回答有没有覆盖摘录里的关键公式/节号。

    · §3.3.3（粮食）要求出现 `F_基准`，且出现 `0.4`；
    · 其它节：至少出现节号（`§3.2` / `3.2`）或标题里的关键词。
    没覆盖 → 视为“答偏了”，用原文替换。
    """
    text = str(content or "")
    for ex in excerpts:
        key = str(ex.get("key") or "")
        if key == "3.3.3":
            if "F_基准" in text and ("0.4" in text or "0.40" in text):
                return True
        elif key:
            title = str(ex.get("title") or "")
            words = [w for w in title.replace("（", " ").replace("）", " ").split()
                     if len(w) >= 2]
            if key in text or (f"§{key}" in text):
                return True
            if words and any(w in text for w in words[:4]):
                return True
    return False


def _rule_answer_text(excerpts: list[dict]) -> str:
    """把规则摘录拼成一段可直接给玩家看的答案（原文，不改写）。"""
    parts = ["按《规则基线》原文："]
    for ex in excerpts:
        key = str(ex.get("key") or "")
        title = str(ex.get("title") or "").strip()
        summary = str(ex.get("summary") or "").strip().replace("```", "")
        if title.startswith(key):
            head = title
        else:
            head = f"§{key}" + (f" {title}" if title else "")
        parts.append(head + "\n" + summary)
    return "\n\n".join(parts)


def _ensure_rule_answer(
    content: str,
    question: Optional[str],
    excerpts: list[dict],
) -> str:
    """纯机制题：模型答偏时用基线原文替换；否则保留模型回答。"""
    if not excerpts:
        return content
    if not _is_pure_rule_question(question):
        return content
    if _mentions_rule_terms(content, excerpts):
        return content
    return _rule_answer_text(excerpts)


_CANCEL_WORDS: tuple[str, ...] = ("取消", "撤销", "删", "移除", "不要", "停掉")


def _explicit_cancel_ids(
    question: Optional[str],
    current_plan: Optional[dict],
) -> list[str]:
    """从“取消 9,9 的建造”这类问题里取 plot_id，且只保留草稿里真有的。"""
    import re

    if not question or not isinstance(current_plan, dict):
        return []
    if not any(w in question for w in _CANCEL_WORDS):
        return []
    current = [
        str(b.get("plot_id", "")).replace(" ", "")
        for b in (current_plan.get("builds") or [])
        if isinstance(b, dict)
    ]
    # “取消 9,9，保留 9,8”里两个 ID 都在，但只有 9,9 是要删的。
    # 先切掉“保留/留着/其余/但”之后的部分，避免把保留项也删掉。
    head = re.split(r"保留|留着|其余|但是|但", question)[0]
    found = [
        m.group(1).replace(" ", "")
        for m in re.finditer(r"(?<!\d)(\d{1,2}\s*,\s*\d{1,2})(?!\d)", head)
    ]
    out: list[str] = []
    for pid in found:
        if pid in current and pid not in out:
            out.append(pid)
    return out


def _compact_current_plan(plan: Optional[dict]) -> str:
    """玩家当前草稿 → 紧凑几行（**只列非空项**，给模型看，不参与规则）。"""
    if not isinstance(plan, dict):
        return "（空）"
    lines: list[str] = []

    builds = plan.get("builds")
    builds = builds if isinstance(builds, list) else []
    if builds:
        lines.append(f"builds（{len(builds)} 条）:")
        for b in builds:
            if isinstance(b, dict):
                lines.append(
                    f"  {b.get('plot_id')}|{b.get('plot_type')}|"
                    f"L{b.get('target_level', 1)}|{b.get('product')}"
                )
            else:
                lines.append(f"  {b}")
    else:
        lines.append("builds:（无）")

    plot_plans = plan.get("plot_plans")
    if isinstance(plot_plans, dict) and plot_plans:
        rows = []
        for pid, item in plot_plans.items():
            if isinstance(item, dict):
                u = item.get("u", 1.0)
                active = item.get("active", True)
                rows.append(f"{pid}|u={u}|{'开工' if active else '停工'}")
            else:
                rows.append(f"{pid}|{item}")
        lines.append("plot_plans：" + "、".join(rows))

    for key in ("purchases", "food"):
        value = plan.get(key)
        if isinstance(value, dict) and value:
            lines.append(key + "：" + "、".join(f"{g}×{q}" for g, q in value.items()))
    return "\n".join(lines)


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
    plan_notes: Optional[list[str]] = None,
    plan_preview: Optional[dict] = None,
    plan_action: Optional[str] = None,
    why: str = "",
    evidence: list[dict],
    model: str,
    rule_key: Optional[str],
    question: Optional[str],
) -> dict:
    """成功的建议形状。**`suggested_plan` 可为 None**（模型没起草出计划时）。

    ⚠️ 这里**不收空 summary**：调用方必须先确认正文非空（见 `advise` 里的
       `empty_response` 分支）。留这道断言是为了让"假装成功"不可能再溜进来。

    ★ 2026-10-18 新增两个**只加不改**的字段（前端用于显示“引擎改了什么”）：

        `plan_notes`   —— 编译器校正说明（字符串数组，可能为空）
        `plan_preview` —— 编译后真跑 `preview` 的摘要（dict 或 None）

    ★ 同日新增 `plan_action`（`AI交互与建设撤销.md` §二）：
      `"created"` / `"edited"` / `"removed"` / `"none"` / None —— 前端据此
      提示「AI 新建 / 编辑 / 撤销了计划」；**不是**权限字段，AI 仍无 commit。
    """
    assert summary.strip(), "空 summary 不许走成功路径 —— 应报 empty_response"
    return {
        "ok": True,
        "summary": summary,
        "suggested_plan": suggested_plan,
        "plan_source": plan_source,
        "plan_issues": plan_issues,
        "plan_notes": list(plan_notes or []),
        "plan_preview": plan_preview,
        "plan_action": plan_action,
        # ★ 模型自己写的「做了什么 + 为什么」（原话，不进规则）。
        #   前端把它单独显示成「为什么：」一行；没有就是空串（不是编的）。
        "why": why,
        "evidence": evidence,
        "model": model,
        "rule_key": rule_key,
        "question": question,
        "error": "",
        # 权限阶梯的落点，前端照着这句话做按钮就行
        "next_step": "玩家确认后由前端走 /api/submit 提交（AI 无 commit）",
    }


def _draft_advice(
    player_id: str,
    *,
    compiled,
    parse_problem: Optional[str],
    shape_error: Optional[str],
    evidence: list[dict],
    model: str,
    rule_key: Optional[str],
    question: Optional[str],
    why: str = "",
    plan_action: Optional[str] = None,
) -> dict:
    """draft=True 的收尾：编译器结果 → 建议形状。

    四条出口（对应 §11.4 与《AI秘书-有效起草方案》的验收）：

      ① 模型连 JSON 都没给出（一堆废话 / 全不是 JSON）→ **明确失败**（不编）；
      ② 编译器自己抛异常 → 明确失败（不编）；
      ③ 编译器说 `ok=False`（validate 或 preview 过不了）→
         `suggested_plan=None` + `plan_issues`（**绝不把坏计划交给前端**）；
      ④ 编译器说 `ok=True` → `suggested_plan=编译后的 Plan`、
         `plan_source='model'`、`plan_notes=校正说明`、`plan_preview=试算摘要`。
    """
    # ── ① 模型连 JSON 都没给（废话 / 全不是 JSON）→ 明确失败（不编）───
    if compiled is None and parse_problem:
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
            plan_action=plan_action,
        )

    # ── ② 编译器自己抛了异常（输入形状完全不对等）───
    if compiled is None:
        return _ok_advice(
            summary=(
                f"模型给的 JSON 编译器处理不了（{shape_error or '未知形状错误'}）。"
                "这次起草被放弃——玩家仍得自己填表，或再点一次重新起草。"
            ),
            suggested_plan=None,
            plan_source=None,
            plan_issues=None,
            evidence=evidence,
            model=model,
            rule_key=rule_key,
            question=question,
            plan_action=plan_action,
        )

    notes = list(compiled.notes or [])

    # ── ③ 编译器说不过：suggested_plan=None + issues（绝不交坏计划）───
    if not compiled.ok:
        issues = list(compiled.issues or [])
        if issues:
            summary = (
                f"模型起草的计划没过引擎校验（{len(issues)} 条问题），"
                "这次起草被放弃——玩家仍得自己填表，或再点一次重新起草。"
            )
        else:
            summary = (
                f"模型起草的计划试算没跑通（{compiled.settle_error or '原因未明'}），"
                "这次起草被放弃——玩家仍得自己填表，或再点一次重新起草。"
            )
        return _ok_advice(
            summary=summary,
            suggested_plan=None,
            plan_source="model",          # 它**确实**是模型起草的，只是不过
            plan_issues=issues,
            plan_notes=notes,
            plan_preview=None,
            evidence=evidence,
            model=model,
            rule_key=rule_key,
            question=question,
            plan_action=plan_action,
        )

    # ── ④ 编译器说能跑：plan 已过 validate + preview ──
    plan_dict = compiled.plan.to_dict()
    n_builds = len(plan_dict.get("builds") or [])
    n_plots = sum(
        1 for pp in (plan_dict.get("plot_plans") or {}).values()
        if pp.get("active", True) and float(pp.get("u", 0) or 0) > 0
    )
    if why:
        # ★ 2026-10-18：模型给了 `why` 就**用它当摘要** —— 玩家只要一句
        #   「做了什么 + 为什么」(AI交互与建设撤销.md §五.4)，不贴 JSON。
        #   ⚠️ 但引擎可能已按预算/校验改了这份计划（例如把新建造撤掉、
        #   把 u 降下来）。摘要必须点明“这是模型原意 + 引擎已校正”，
        #   否则会出现“说建了、表单里却没有”的自相矛盾（真跑抓到）。
        summary = why
        if notes:
            summary += f"（引擎已按预算/校验校正 {len(notes)} 处，见 plan_notes）"
    else:
        summary = (
            "模型起草了一份计划" +
            (f"（含 {n_builds} 条建造、{n_plots} 块地开工）"
             if (n_builds or n_plots) else "")
        )
        if notes:
            summary += f"；引擎校正 {len(notes)} 处（见 plan_notes）"
        summary += "。下方 `suggested_plan` 就是它，玩家确认后可一键复制走 /api/submit。"

    preview = compiled.preview or None
    if isinstance(preview, dict):
        # 只留前端要显示的两三个数（完整报告太长，进对话会淹掉重点）
        preview = {
            k: preview.get(k) for k in
            ("total_deduction", "cash_after", "sales_revenue", "value_added")
            if k in preview
        }

    return _ok_advice(
        summary=summary,
        suggested_plan=plan_dict,
        plan_source="model",
        plan_issues=None,
        plan_notes=notes,
        plan_preview=preview,
        evidence=evidence,
        model=model,
        rule_key=rule_key,
        question=question,
        plan_action=plan_action,
        why=why,
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
