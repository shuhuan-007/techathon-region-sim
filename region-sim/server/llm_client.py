"""本地 LLM 网关的**薄客户端**（OpenAI 兼容 /v1/chat/completions）。

只用标准库（`urllib.request` + `json`），零第三方依赖。

═══════════════════════════════════════════════════════════════════
★ 优雅降级（本模块存在的全部理由）
═══════════════════════════════════════════════════════════════════

本地代理（默认 http://127.0.0.1:8787）经常会**没起**；学校网关单次
思考要几十秒，会**超时**；网关偶发会回**非 200**。这些都不是 bug，
是常态。

所以本模块的承诺只有一条：

    **任何失败都变成 `LLMResult(ok=False, error=...)`，绝不抛穿到
    调用方 / 绝不编造回答。**

调用方只查 `result.ok`；失败原因在 `result.error` / `result.detail`，
是给人看的、能分情形（代理没起 / 超时 / 非 200 / 回了垃圾）。

⚠️ **不重试、不并发压测**：本项目明确禁止对该网关并发压测；
   失败一次就老老实实报不可用，把决定权交回给玩家。
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field

# ══════════════════════════════════════════════════════════════════
# 配置（环境变量可覆盖）
# ══════════════════════════════════════════════════════════════════

#: 本地网关的 OpenAI 兼容端点。**环境变量可覆盖**（AI_BASE_URL）。
#: 值就是路径全形式（…/v1/chat/completions），调用方不再拼。
DEFAULT_BASE_URL = "http://127.0.0.1:8787/v1/chat/completions"

#: 默认模型名。**环境变量可覆盖**（AI_MODEL）。
DEFAULT_MODEL = "kimi-k3"

#: 默认超时（秒）。学校网关单次思考常要几十秒，给足一点。
#: ⚠️ 宁可超时久一点，也别 30 秒就掐死一个其实快想完的回答。
#:    环境变量 AI_TIMEOUT 可覆盖。
DEFAULT_TIMEOUT = 120.0


def _env(name: str, default: str) -> str:
    v = os.environ.get(name)
    return v if v else default


def base_url() -> str:
    return _env("AI_BASE_URL", DEFAULT_BASE_URL)


def model_name() -> str:
    return _env("AI_MODEL", DEFAULT_MODEL)


def timeout_seconds() -> float:
    raw = _env("AI_TIMEOUT", str(DEFAULT_TIMEOUT))
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT


# ══════════════════════════════════════════════════════════════════
# 结果形状（调用方只认这个，不碰异常）
# ══════════════════════════════════════════════════════════════════


@dataclass
class LLMResult:
    """一次 chat completion 的结果。**失败不进 `raise`，全在这里。**

    · `ok=True`  → `content` 是模型回的文本（**未解析**，解析是 ai_host 的事）；
    · `ok=False` → `error` 是机器可读的分类，`detail` 是给人看的一句话。
    """

    ok: bool
    content: str = ""
    model: str = ""
    error: str = ""          # 机器可读分类：proxy_down / timeout / http_error / bad_response
    detail: str = ""         # 给人看的原因
    raw: dict = field(default_factory=dict)   # 成功时的原始 JSON（调试用）


# ══════════════════════════════════════════════════════════════════
# 客户端本体
# ══════════════════════════════════════════════════════════════════

#: 本地代理默认不校验 key ——

#: 但 OpenAI 兼容端点一般要一个**非空** Authorization 头，给个占位即可。
_PLACEHOLDER_KEY = "Bearer local"


def chat_completions(
    messages: list[dict],
    *,
    model: str | None = None,
    url: str | None = None,
    timeout: float | None = None,
    temperature: float = 0.3,
    max_tokens: int = 2000,
) -> LLMResult:
    """调一次 /v1/chat/completions。**任何失败都返回 `ok=False`，不抛。**

    `messages` 是 OpenAI 形状：`[{"role": "system"/"user", "content": str}, ...]`。
    返回 `LLMResult` —— 调用方**只查 `.ok`**，别想当然去取 `.content`。
    """
    used_model = model or model_name()
    used_url = url or base_url()
    used_timeout = timeout if timeout is not None else timeout_seconds()

    payload = {
        "model": used_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        used_url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": _PLACEHOLDER_KEY,
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=used_timeout) as resp:
            status = resp.status
            raw_bytes = resp.read()
    except urllib.error.HTTPError as e:
        # 网关回了，但不是 2xx（404 路径错 / 500 网关自己炸了 / …）
        return LLMResult(
            ok=False, model=used_model, error="http_error",
            detail=f"网关回了 HTTP {e.code}（{e.reason}）。路径是不是写错了？",
        )
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
        # ── 连接层失败。分两类，给玩家不同的提示 ─────────────
        reason = getattr(e, "reason", e)
        # 超时（read/connect 都归这里）
        if isinstance(e, (socket.timeout, TimeoutError)) or "timed out" in str(reason):
            return LLMResult(
                ok=False, model=used_model, error="timeout",
                detail=f"超过 {used_timeout:.0f} 秒没等到回答（学校网关思考常要几十秒）。"
                       f"可以稍后再试，或把超时调大（AI_TIMEOUT）。",
            )
        # 其余（连接被拒 / 主机不可达 / DNS …）→ "代理没起"
        return LLMResult(
            ok=False, model=used_model, error="proxy_down",
            detail=f"连不上本地网关（{used_url}）：{reason}。"
                   f"是不是代理没起？先把它拉起来再让 AI 出主意。",
        )
    except OSError as e:  # 兜底：任何残余的底层错误，别让它穿出去
        return LLMResult(
            ok=False, model=used_model, error="proxy_down",
            detail=f"网络层错误：{e}",
        )

    if status != 200:
        return LLMResult(
            ok=False, model=used_model, error="http_error",
            detail=f"网关回了 HTTP {status}。",
        )

    # ── 解析：网关可能回一个字都看不懂的东西 ─────────────────────
    try:
        data = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        return LLMResult(
            ok=False, model=used_model, error="bad_response",
            detail=f"网关回的不是合法 JSON（{e}）。这是网关的锅，不是模型的锅。",
        )

    # OpenAI 兼容形状：choices[0].message.content
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        return LLMResult(
            ok=False, model=used_model, error="bad_response",
            detail=f"网关回的 JSON 没有 choices[0].message.content（{e}）。"
                   f"形状：{list(data)[:5] if isinstance(data, dict) else type(data)}",
        )

    return LLMResult(
        ok=True, model=data.get("model", used_model), content=str(content), raw=data,
    )
