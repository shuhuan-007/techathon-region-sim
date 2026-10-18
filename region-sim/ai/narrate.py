"""叙事生成：装提示词 → 调模型 → 校验 JSON。**本层唯一的模型调用口。**

一套接缝、两份提示词
    · `narrate_news(facts, cfg=...)`   —— 用 `prompts/news.md`
    · `narrate_report(report_dict, cfg=...)` —— 用 `prompts/report.md`

两条铁律
    ① **绝不编造**：模型给不出可解析的 JSON 就如实报失败（`ok:false`），
       绝不用一段自己写的文字顶替（§11.4、`tests/test_news.py` 两条断言钉着）。
    ② **本层不 import server 侧**：`llm_client` 用惰性 import 取（`ai/` 的
       import 边界自检盯着 `sys.modules`，模块顶层不许把 server 拉起来）。

调用方（`server/session.py`）负责"生成时机 + 按季缓存"，本模块只管生成一次。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Optional

#: 提示词目录（与本模块同级）。
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

#: kind → 提示词文件名。
_PROMPT_FILES = {"news": "news.md", "report": "report.md"}

# ★ 2026-10-18 终局报告：单独**追加一行**注册，而不是把上面那两个的
#   字面字典改成「包含 finale 的 dict」—— 这样既有 news/report 两路
#   的代码行完全不动，回归测试（tests/test_narrate.py）一字都不用改。
#   `load_prompt(kind)` 走 `_PROMPT_FILES.get(kind)`，与追加与否无关。
_PROMPT_FILES["finale"] = "finale.md"

#: ⚠️ **默认在线**（2026-10-18 用户要求：财报/新闻必须能生成）。
#: 测试全部注入 FakeClient；要强制离线调试，显式设 `NARRATE_OFFLINE=1`。
#:
#: 以前默认关，是为了避免 `tests/test_server.py` 这类本该离线的测试
#: 去连网关；现在测试都注入 client，不再依赖这个默认值。
_OFFLINE_DEFAULT = False


def is_offline() -> bool:
    """当前是否处于"离线模式"（不真调模型）。"""
    raw = os.environ.get("NARRATE_OFFLINE")
    if raw is None:
        return _OFFLINE_DEFAULT
    return str(raw).strip() not in ("0", "false", "False", "no")


class NarrateError(RuntimeError):
    """提示词缺失等**本层自身**的问题（不是模型的问题）。"""


# ══════════════════════════════════════════════════════════════════
# 提示词装载
# ══════════════════════════════════════════════════════════════════


def load_prompt(kind: str) -> str:
    """读出某个 kind 的提示词全文。

    ⚠️ **逐字返回**：提示词是用户朋友写的，一个字都不许在本层改写
       （要改只能改文件本身，且要留痕）。
    """
    name = _PROMPT_FILES.get(kind)
    if name is None:
        raise NarrateError(f"没有这种叙事：{kind!r}（只有 {sorted(_PROMPT_FILES)}）")
    path = PROMPTS_DIR / name
    if not path.is_file():
        raise NarrateError(f"提示词文件不存在：{path}")
    return path.read_text(encoding="utf-8")


# ══════════════════════════════════════════════════════════════════
# 解析：模型回的东西**皮实地**取出 JSON
# ══════════════════════════════════════════════════════════════════

#: ```json ... ``` 或 ``` ... ``` 围栏。非贪婪、跨行。
_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


def _balanced_json_span(text: str) -> Optional[str]:
    """从一段自由文本里取出**第一个平衡的** JSON 对象（考虑字符串与转义）。

    为什么要自己数括号而不是 `json.loads` 直接试：
    模型常写成「好的，这是结果：{...} 希望对你有帮助」——直接解析必失败，
    但里面其实有一个完好对象。也**不能**只看第一个 `{` 到最后一个 `}`，
    因为后面可能还有别的括号（例如正文里提到 `{}`）。
    """
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        escaped = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    try:
                        json.loads(candidate)
                    except Exception:  # noqa: BLE001
                        break  # 这段不是好 JSON → 从下一个 { 再来
                    return candidate
        start = text.find("{", start + 1)
    return None


def parse_model_json(content: str) -> tuple[Optional[dict], str]:
    """把模型返回的文本解析成 dict。返回 `(对象或 None, 问题说明)`。

    容忍：Markdown 代码围栏、前后废话、纯 JSON。**不抛异常**。
    """
    if content is None:
        return None, "模型没有返回正文"
    text = str(content).strip()
    if not text:
        return None, "模型返回了空正文"

    # ① 原样就是 JSON（提示词要求的最正常情况）
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj, ""
        if obj is None:
            return None, "模型返回了 JSON null"
        return None, f"模型返回的 JSON 顶层不是对象，而是 {type(obj).__name__}"
    except Exception:  # noqa: BLE001
        pass

    # ② 剥代码围栏
    m = _FENCE_RE.search(text)
    if m:
        try:
            obj = json.loads(m.group(1).strip())
            if isinstance(obj, dict):
                return obj, ""
        except Exception:  # noqa: BLE001
            pass

    # ③ 从自由文本里捞第一个平衡对象
    span = _balanced_json_span(text)
    if span is not None:
        try:
            obj = json.loads(span)
            if isinstance(obj, dict):
                return obj, ""
        except Exception as e:  # noqa: BLE001
            return None, f"取出的片段仍不是合法 JSON（{type(e).__name__}）"

    return None, "模型返回的内容里找不到合法的 JSON 对象"


# ══════════════════════════════════════════════════════════════════
# 核心：一份提示词 + 一份输入 → 一个叙事
# ══════════════════════════════════════════════════════════════════


def _default_client() -> Any:
    """取 `server/llm_client`（**惰性**，见模块 docstring 的铁律②）。"""
    from server import llm_client  # noqa: PLC0415  —— 故意放在函数内

    return llm_client


def _failure(kind: str, quarter: Any, error: str, detail: str,
             model: str = "", diag: Optional[dict] = None) -> dict:
    """统一失败形状。**不含任何编造文本**。

    `diag`（可选，2026-10-17 加）：llm_client 透上来的诊断字段
    （finish_reason / usage / elapsed_ms / raw_text_len / retried）。
    失败原因本来就难定位，把它们一并写进返回里，方便事后追。
    """
    out = {
        "kind": kind,
        "quarter": quarter,
        "ok": False,
        "error": error,
        "detail": detail,
        "model": model,
    }
    if diag:
        out.update(diag)
    return out


def _diag_of(result: Any) -> dict:
    """从 llm_client.LLMResult（或同款 dict）里抽诊断字段。**只加不改。**"""
    keys = ("finish_reason", "usage", "elapsed_ms", "raw_text_len", "retried")
    out: dict = {}
    for k in keys:
        v = (result.get(k) if isinstance(result, dict)
             else getattr(result, k, None))
        # 只写有内容的；不覆盖既有语义（retried=False 就不写）
        if v not in (None, "", False, {}, 0):
            out[k] = v
    return out


def _strip_currency_units(text: str) -> str:
    """删掉模型爱加的货币单位（元/人民币/¥/￥/$）；只保留裸数字。

    ⚠️ 这是**最后一道兜底**，不是免死金牌：news.md / report.md 提示词里
    已明确"金额、价格一律只写裸数字，绝对不得出现 元/人民币/¥/$"——
    清洗只是兜住漏网之鱼，**绝不**因此放松提示词约束。
    （2026-10-18 用户反馈：之前的财报解读里冒过"元"。）
    """
    if not isinstance(text, str):
        return text
    out = re.sub(r"\s*(?:人民币|元|¥|￥|\$)\s*", " ", text)
    out = re.sub(r"\s+([，。；、])", r"\1", out)
    return out.strip()


def _generate(
    kind: str,
    payload: dict,
    *,
    quarter: Any,
    client: Optional[Callable] = None,
) -> dict:
    """共用核心：装提示词 → 调模型 → 解析 → 收敛成统一叙事形状。"""
    # ⚠️ 离线闸：默认不真调模型（见 is_offline 的说明）。
    #    注入了 client（测试用的假客户端，不联网）时不受影响。
    if client is None and is_offline():
        return _failure(
            kind, quarter, "offline",
            "离线模式（默认）：这次没有真调模型。要真跑请在启动进程时设 "
            "NARRATE_OFFLINE=0。",
        )
    if client is None:
        try:
            client = _default_client()
        except Exception as e:  # noqa: BLE001
            return _failure(kind, quarter, "client_unavailable",
                            f"取不到模型客户端（{type(e).__name__}：{e}）。")

    try:
        prompt = load_prompt(kind)
    except NarrateError as e:
        return _failure(kind, quarter, "prompt_missing", str(e))

    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]

    try:
        result = client.chat_completions(messages)
    except Exception as e:  # noqa: BLE001
        # 客户端**正常**情况下从不抛（llm_client 全失败路径都返回结果），
        # 但注入的假客户端可能抛 —— 这里兜住，绝不让它穿到 /state。
        return _failure(kind, quarter, "client_crashed",
                        f"模型客户端抛了异常（{type(e).__name__}：{e}）。")

    ok = result.get("ok") if isinstance(result, dict) else getattr(result, "ok", False)
    model = (result.get("model") if isinstance(result, dict)
             else getattr(result, "model", "")) or ""
    diag = _diag_of(result)      # 2026-10-17：诊断字段透传（只加不改）
    if not ok:
        err = (result.get("error") if isinstance(result, dict)
               else getattr(result, "error", "")) or "model_unavailable"
        detail = (result.get("detail") if isinstance(result, dict)
                  else getattr(result, "detail", "")) or "模型没给出可用回答。"
        return _failure(kind, quarter, str(err), str(detail), model=model, diag=diag)

    content = (result.get("content") if isinstance(result, dict)
               else getattr(result, "content", "")) or ""
    obj, problem = parse_model_json(content)

    if obj is None:
        # ⚠️ 两路的**提示词契约本来就不一样**（2026-10-17 真跑后修的）：
        #    · news.md  要求「只返回一个合法 JSON 对象，只有三个顶层字段」
        #      → 解析不出来就是没拿到，**绝不**把那段自由文本当叙事返回。
        #    · report.md 要求「输出供玩家阅读的**中文 Markdown 解读**」
        #      → **它本来就不该是 JSON**！第一次真跑时模型老老实实给了
        #        一份很像样的 Markdown，我们却按 JSON 判它 bad_response，
        #        把好东西扔了 —— 那是**我们的对接错误**，不是模型不听话。
        #      所以 report 这一路：拿不到 JSON 就**把 Markdown 原文当正文收下**。
        if kind == "report" and str(content).strip():
            out = {
                "kind": "report",
                "quarter": quarter,
                "ok": True,
                "model": model,
                "text": _strip_currency_units(str(content).strip()),
                "facts_used": {},          # 纯 Markdown 返回值里没有溯源表
                "warning": "模型按提示词返回了 Markdown（非 JSON），已按正文接收；"
                           "本份没有 facts_used 溯源。",
            }
            out.update(diag)
            return out
        return _failure(kind, quarter, "bad_response",
                        f"{problem}。原文前 200 字：{str(content).strip()[:200]}",
                        model=model, diag=diag)

    facts_used = obj.get("facts_used")
    if not isinstance(facts_used, dict):
        # 正文仍有价值 → 不整份失败，但**如实标出瑕疵**。
        facts_used = {}
        problem = (problem + "；" if problem else "") + "模型没给出可用的 facts_used"

    base = {
        "kind": kind,
        "quarter": quarter,
        "ok": True,
        "model": model,
        "facts_used": facts_used,
    }
    base.update(diag)     # 2026-10-17：诊断字段（重试标记、finish_reason…）
    if problem:
        base["warning"] = problem

    if kind == "news":
        headline = obj.get("headline")
        body = obj.get("body")
        if not isinstance(headline, str) or not isinstance(body, str):
            return _failure(kind, quarter, "bad_response",
                            "JSON 解析成功但缺 headline / body（或类型不对）。",
                            model=model)
        base["headline"] = _strip_currency_units(headline)
        base["body"] = _strip_currency_units(body)
    else:
        text = obj.get("text")
        if not isinstance(text, str):
            # 财报那边提示词要求的是 Markdown 正文；容忍它用别的键名。
            for alt in ("body", "markdown", "content"):
                if isinstance(obj.get(alt), str):
                    text = obj[alt]
                    base["warning"] = ((base.get("warning") or "") +
                                       f"（模型用了 {alt} 键，已按正文明读）").strip("；")
                    break
        if not isinstance(text, str):
            return _failure(kind, quarter, "bad_response",
                            "JSON 解析成功但没有正文（缺 text）。", model=model)
        base["text"] = _strip_currency_units(text)

    return base


# ══════════════════════════════════════════════════════════════════
# 两个入口（签名由 `server/session.py` 钉定，别改）
# ══════════════════════════════════════════════════════════════════


def narrate_news(facts: Optional[dict], cfg: Any = None, *,
                 client: Optional[Callable] = None) -> Optional[dict]:
    """新闻叙事。`facts` 为 None → **返回 None**（提示词要求：不许生成）。"""
    if facts is None:
        return None
    quarter = facts.get("quarter") if isinstance(facts, dict) else None
    return _generate("news", facts, quarter=quarter, client=client)


def narrate_report(report_dict: Optional[dict], cfg: Any = None, *,
                   client: Optional[Callable] = None) -> Optional[dict]:
    """财报解读。`report_dict` 为 None → **返回 None**。"""
    if report_dict is None:
        return None
    quarter = (report_dict.get("quarter")
               if isinstance(report_dict, dict) else None)
    return _generate("report", report_dict, quarter=quarter, client=client)


# ══════════════════════════════════════════════════════════════════
# 三、终局报告入口（2026-10-18 追加，**不改既有函数体**）
# ══════════════════════════════════════════════════════════════════
#
# 契约（与 engine/finale.py 与子代理 C 前端同钉）：
#     · facts 是 `build_finale_facts(overview)` 返回的事实包。
#     · 输出四顶层键：headline / body / charts / facts_used（缺一键即 bad_response）。
#     · charts **恰好 2 个对象**，每个 points 长度 = total_quarters（或 20）、
#       quarter 严格 1..N 升序、unit 必为 null。
#     · headline / body 走 `_strip_currency_units`。
#     · 失败兜底**不在这里做**——offline/model_unavailable/bad_response 如实
#       返回给调用方（engine/finale.py::generate_finale_report 接到后再回落
#       到 deterministic_body）。


def _validate_finale_charts(charts: Any, total_quarters: Optional[int]) -> tuple[bool, str]:
    """终局 charts 形状校验。返回 (是否合法, 不合法原因)。

    合法要求：
        · 是 list 且 **恰好 2 个对象**；
        · 每个对象含 id/title/metric/unit/points 五键，unit 必须为 None，
          metric ∈ {"gdp", "happiness"}；
        · points 是 list，长度 = total_quarters（缺省 20）；
        · points[i]["quarter"] == i+1（严格 1..N 升序）；
        · 每个点的 value 可为 None 或 int/float（不补 0、不插值由引擎做，这里只管形状）。
    """
    N = int(total_quarters or 20)
    if not isinstance(charts, list) or len(charts) != 2:
        return False, f"charts 必须是恰好 2 个对象的数组，实得 {type(charts).__name__}"
    want_metrics = ("gdp", "happiness")
    for i, ch in enumerate(charts):
        if not isinstance(ch, dict):
            return False, f"charts[{i}] 不是对象"
        for k in ("id", "title", "metric", "points"):
            if k not in ch:
                return False, f"charts[{i}] 缺键 {k!r}"
        if ch.get("metric") not in want_metrics:
            return False, f"charts[{i}].metric 必须是 'gdp'/'happiness'，实得 {ch.get('metric')!r}"
        if ch.get("unit") is not None:
            return False, f"charts[{i}].unit 必须为 null，实得 {ch.get('unit')!r}"
        pts = ch.get("points")
        if not isinstance(pts, list) or len(pts) != N:
            return False, (f"charts[{i}].points 必须是长度 {N} 的数组，"
                           f"实得 {0 if not isinstance(pts, list) else len(pts)}")
        for j, p in enumerate(pts):
            if not isinstance(p, dict) or p.get("quarter") != j + 1:
                return False, f"charts[{i}].points[{j}] 的 quarter 必须是 {j+1}（升序）"
            v = p.get("value")
            if v is not None and not isinstance(v, (int, float)):
                return False, f"charts[{i}].points[{j}].value 必须为 null 或数值"
    if charts[0].get("metric") != "gdp" or charts[1].get("metric") != "happiness":
        return False, "charts 顺序必须是 GDP 在前、幸福度在后"
    return True, ""


def narrate_finale_report(facts: Optional[dict], cfg: Any = None, *,
                          client: Optional[Callable] = None) -> Optional[dict]:
    """终局报告。`facts` 为 None → **返回 None**（参照 news 的做法）。

    返回 dict（ok=True 分支）含四个顶层键::

        headline / body / charts / facts_used
        + ok=True, model=...
        （ charts/facts_used 只作形状校验——最终采用的**引擎版**
           由调用方 engine/finale.py::generate_finale_report 替换回去。）

    失败统一走 `_failure`：
        · `offline` / `model_unavailable` / `client_crashed` / `prompt_missing`
        · `bad_response`：解析不出 JSON / 顶层不是对象 / 缺四键之一 /
          charts 形状不合法（`detail` 指明哪一项不妥）。
    """
    if facts is None:
        return None
    quarter = facts.get("total_quarters") if isinstance(facts, dict) else None

    # ⚠️ 为什么不复用 `_generate("finale", ...)`：
    #    `_generate` 的尾部按 kind 二分（"news" 抽 headline/body、else 抽 text），
    #    不认识 finale 的四键形状（headline/body/charts/facts_used）。
    #    把它塞进通用函数就要**改 `_generate` 的函数体**——违背本任务
    #    「不改既有函数体」的红线。所以 finale **单走这条路**，
    #    只调用、不改写既有辅助函数（load_prompt / parse_model_json /
    #    _failure / _strip_currency_units / _diag_of），形状校验走
    #    `_validate_finale_charts`（本文件新增的私有函数，同样只加不改）。
    #
    # —— 离线 / client 缺失：与 `_generate` 完全相同的判定 ——
    if client is None and is_offline():
        return _failure(
            "finale", quarter, "offline",
            "离线模式（默认）：这次没有真调模型。要真跑请在启动进程时设 "
            "NARRATE_OFFLINE=0。",
        )
    if client is None:
        try:
            client = _default_client()
        except Exception as e:  # noqa: BLE001
            return _failure("finale", quarter, "client_unavailable",
                            f"取不到模型客户端（{type(e).__name__}：{e}）。")

    try:
        prompt = load_prompt("finale")
    except NarrateError as e:
        return _failure("finale", quarter, "prompt_missing", str(e))

    payload_text = json.dumps(facts, ensure_ascii=False)
    messages = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": payload_text},
    ]

    try:
        raw = client.chat_completions(messages)
    except Exception as e:  # noqa: BLE001
        return _failure("finale", quarter, "client_crashed",
                        f"模型客户端抛了异常（{type(e).__name__}：{e}）。")

    ok = raw.get("ok") if isinstance(raw, dict) else getattr(raw, "ok", False)
    model = (raw.get("model") if isinstance(raw, dict)
             else getattr(raw, "model", "")) or ""
    diag = _diag_of(raw)
    if not ok:
        err = (raw.get("error") if isinstance(raw, dict)
               else getattr(raw, "error", "")) or "model_unavailable"
        detail = (raw.get("detail") if isinstance(raw, dict)
                  else getattr(raw, "detail", "")) or "模型没给出可用回答。"
        return _failure("finale", quarter, str(err), str(detail),
                        model=model, diag=diag)

    content = (raw.get("content") if isinstance(raw, dict)
               else getattr(raw, "content", "")) or ""
    obj, problem = parse_model_json(content)
    if obj is None:
        return _failure("finale", quarter, "bad_response",
                        f"{problem}。原文前 200 字：{str(content).strip()[:200]}",
                        model=model, diag=diag)

    # ── 四键齐全校验（**缺一即 bad_response，绝不降级**）─────────
    missing: list[str] = []
    for k in ("headline", "body", "charts", "facts_used"):
        if k not in obj:
            missing.append(k)
    if missing:
        return _failure(
            "finale", quarter, "bad_response",
            f"JSON 解析成功但缺四顶层键：{', '.join(missing)}。",
            model=model, diag=diag)

    headline = obj["headline"]
    body = obj["body"]
    charts = obj["charts"]
    facts_used = obj["facts_used"]

    if not isinstance(headline, str) or not isinstance(body, str):
        return _failure("finale", quarter, "bad_response",
                        "headline 与 body 必须是字符串。", model=model, diag=diag)
    if not isinstance(facts_used, dict):
        return _failure("finale", quarter, "bad_response",
                        "facts_used 必须是对象。", model=model, diag=diag)

    total_q = facts.get("total_quarters") if isinstance(facts, dict) else None
    ok_charts, why = _validate_finale_charts(charts, total_q)
    if not ok_charts:
        return _failure("finale", quarter, "bad_response",
                        f"charts 形状不合法：{why}", model=model, diag=diag)

    out = {
        "kind": "finale",
        "quarter": quarter,
        "ok": True,
        "model": model,
        "headline": _strip_currency_units(headline),
        "body": _strip_currency_units(body),
        "charts": charts,
        "facts_used": facts_used,
        "facts_used_source": "model",   # 提醒调用方：这里是模型给的，**不可信**
    }
    if problem:
        out["warning"] = problem
    out.update(diag)
    return out
