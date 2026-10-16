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

#: 让模型**别**假装能提交的系统提示核心句。
_SYSTEM_PREAMBLE = (
    "你是这个区域经营游戏的 AI 秘书。你的全部权限是：解释规则、试算、起草。"
    "**你永远不能提交计划、不能结算、不能改任何状态**——最后一脚永远由玩家"
    "走 /api/submit。你只输出**建议**，引用数字必须来自我给你的局面与试算，"
    "**不许编数字**。拿到的报告里没有的字段就说「拿不到」。"
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
      · `client`        — LLM 客户端（**可注入假客户端做测试**，默认真网关）。

    返回 dict（形状见文件底部 `_ok_advice` / `_failure`）：

        ok / summary / suggested_plan / evidence / model / error

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
    #    而真相是"这次没拿到回答"。
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

    # ── 4. 把模型回的自由文本**收进建议形状**（不假装它必是 JSON）───
    return _ok_advice(
        summary=content,
        suggested_plan=preview_plan,   # 建议带的 plan＝AI 先试算过的那版（可为 None）
        evidence=evidence,
        model=mdl or llm_client.model_name(),
        rule_key=rule.get("key"),
        question=question,
    )


# ══════════════════════════════════════════════════════════════════
# 内部：组装 messages
# ══════════════════════════════════════════════════════════════════


def _build_messages(
    *,
    player_id: str,
    state: dict,
    rule: dict,
    question: Optional[str],
    preview_report: Optional[dict],
) -> list[dict]:
    """把局面 / 规则摘要 / 试算数字 / 玩家问题拼成 OpenAI messages。"""
    lines = [
        f"[当前局面 · 玩家 {player_id}]",
        _compact_state(state),
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

    return [
        {"role": "system", "content": _SYSTEM_PREAMBLE},
        {"role": "user", "content": user_body},
    ]


def _compact_state(state: dict) -> str:
    """局面压缩成模型读得动的几行（**只挑已算好的数字**，不整包倒给它）。"""
    keys = (
        "quarter", "total_quarters", "cash", "happiness",
        "population", "phase", "can_settle",
    )
    picked = {k: state.get(k) for k in keys if k in state}
    import json

    return json.dumps(picked, ensure_ascii=False, default=str)


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
        "evidence": evidence,
        "model": model,
        "rule_key": rule_key,
        "question": question,
        "error": "",
        # 权限阶梯的落点，前端照着这句话做按钮就行
        "next_step": "玩家确认后由前端走 /api/submit 提交（AI 无 commit）",
    }


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
