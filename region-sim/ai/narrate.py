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
import re
from pathlib import Path
from typing import Any, Callable, Optional

#: 提示词目录（与本模块同级）。
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

#: kind → 提示词文件名。
_PROMPT_FILES = {"news": "news.md", "report": "report.md"}


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
             model: str = "") -> dict:
    """统一失败形状。**不含任何编造文本**。"""
    return {
        "kind": kind,
        "quarter": quarter,
        "ok": False,
        "error": error,
        "detail": detail,
        "model": model,
    }


def _generate(
    kind: str,
    payload: dict,
    *,
    quarter: Any,
    client: Optional[Callable] = None,
) -> dict:
    """共用核心：装提示词 → 调模型 → 解析 → 收敛成统一叙事形状。"""
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
    if not ok:
        err = (result.get("error") if isinstance(result, dict)
               else getattr(result, "error", "")) or "model_unavailable"
        detail = (result.get("detail") if isinstance(result, dict)
                  else getattr(result, "detail", "")) or "模型没给出可用回答。"
        return _failure(kind, quarter, str(err), str(detail), model=model)

    content = (result.get("content") if isinstance(result, dict)
               else getattr(result, "content", "")) or ""
    obj, problem = parse_model_json(content)

    if obj is None:
        # ⚠️ 这里**绝不**把模型那段自由文本当作叙事返回 —— 提示词要求的是
        #    「只有三个顶层字段的合法 JSON」，解析不出来就是没拿到，如实报错。
        return _failure(kind, quarter, "bad_response",
                        f"{problem}。原文前 200 字：{str(content).strip()[:200]}",
                        model=model)

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
    if problem:
        base["warning"] = problem

    if kind == "news":
        headline = obj.get("headline")
        body = obj.get("body")
        if not isinstance(headline, str) or not isinstance(body, str):
            return _failure(kind, quarter, "bad_response",
                            "JSON 解析成功但缺 headline / body（或类型不对）。",
                            model=model)
        base["headline"] = headline
        base["body"] = body
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
        base["text"] = text

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
