"""本地 LLM 网关的**薄客户端**（OpenAI 兼容 /v1/chat/completions）。

只用标准库（`urllib.request` + `json`），零第三方依赖。

═══════════════════════════════════════════════════════════════════
★ 优雅降级（本模块存在的全部理由）
═══════════════════════════════════════════════════════════════════

本地代理经常会**没起**；DashScope 单次调用虽快
（qwen-flash 通常 5–15 秒），但公共网络仍会**超时**；任何网关都
偶发回**非 200**。这些都不是 bug，是常态。

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
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

# ══════════════════════════════════════════════════════════════════
# 配置（.env / 环境变量可覆盖；优先级见 `_lookup` 注释）
# ══════════════════════════════════════════════════════════════════

#: 默认的 OpenAI 兼容端点。**AI_BASE_URL 可覆盖**（环境变量 > .env）。
#: 值就是路径全形式（…/v1/chat/completions），调用方不再拼。
#:
#: 2026-10-17 换阿里 DashScope（通义千问 qwen-flash）：原 kimi-k3 每次
#: 40–90 秒太慢，且学校网关偶发 500。DashScope 的 compatible-mode 端点
#: 与 OpenAI 形状完全兼容，只换 URL + key 即可。
DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"

#: 默认模型名。**AI_MODEL 可覆盖**（环境变量 > .env）。
#: qwen-flash 是阿里通义千问的轻量模型，按量付费、延迟低；非推理模型。
DEFAULT_MODEL = "qwen-flash"

#: 默认超时（秒）。**AI_TIMEOUT 可覆盖**（环境变量 > .env）。
#: ⚠️ 历史：kimi-k3 推理路单次要 100+ 秒，120 被掐掉过一次，才抬到 300。
#:    qwen-flash 非推理模型、正常 5–15 秒，300 秒纯属兜底；.env 实际写 90。
DEFAULT_TIMEOUT = 300.0


# ══════════════════════════════════════════════════════════════════
# .env 加载器（纯标准库，免 dotenv）
# ══════════════════════════════════════════════════════════════════
#
# 优先级铁律（高 → 低）：
#     ① 真实环境变量
#     ② region-sim/.env
#     ③ 本文件 DEFAULT_* 常量
#
# 找不到 .env 不报错——退回环境变量 / 默认值。

#: .env 文件位置 = 本文件的**上两级**目录（region-sim/server/llm_client.py
#: → parents[1] 就是 region-sim/）。不跟 cwd、不跟 sys.path，最稳。
_ENV_PATH: Path = Path(__file__).resolve().parents[1] / ".env"

#: 进程内缓存（解析一次，整个进程复用）。None = 还没加载过。
_ENV_CACHE: dict[str, str] | None = None


def _strip_quotes(v: str) -> str:
    """去掉值**两侧**成对的单/双引号；不成对的不动。"""
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    return v


def _load_dotenv(path: Path | None = None) -> dict[str, str]:
    """解析 .env → dict。任何失败（不存在 / 读不了 / 编码炸）都返回 {}。

    只认 `KEY=VALUE` 行；空行、纯空白行、`#` 开头注释行忽略。
    允许 `KEY = VALUE`（等号两侧空白）；值两侧的成对引号会被剥掉。
    """
    p = path if path is not None else _ENV_PATH
    out: dict[str, str] = {}
    try:
        if not p.is_file():
            return out
        text = p.read_text(encoding="utf-8")
    except OSError:
        return out

    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if "=" not in s:
            # 宽容：无等号的行直接跳过（有些模板里会有占位说明行）
            continue
        key, _, val = s.partition("=")
        key = key.strip()
        val = _strip_quotes(val.strip())
        if key:
            out[key] = val
    return out


def _dotenv() -> dict[str, str]:
    global _ENV_CACHE
    if _ENV_CACHE is None:
        _ENV_CACHE = _load_dotenv()
    return _ENV_CACHE


def _reset_dotenv_cache() -> None:
    """测试钩子：清掉 .env 缓存，下次 `_dotenv()` 会重读磁盘。"""
    global _ENV_CACHE
    _ENV_CACHE = None


def _lookup(name: str, default: str) -> str:
    """按 "真实环境变量 > .env > 默认值" 取值；空串视为未设置。"""
    v = os.environ.get(name)
    if v:
        return v
    v = _dotenv().get(name)
    if v:
        return v
    return default


def base_url() -> str:
    return _lookup("AI_BASE_URL", DEFAULT_BASE_URL)


def model_name() -> str:
    return _lookup("AI_MODEL", DEFAULT_MODEL)


def timeout_seconds() -> float:
    raw = _lookup("AI_TIMEOUT", str(DEFAULT_TIMEOUT))
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT


def api_key() -> str:
    """AI_API_KEY 的值；空串 = 无 key（本地代理那种不需要鉴权的场景）。

    ⚠️ 调用方只把这个值**塞进 Authorization 头**——不许打印、不许拼进
       detail / 异常消息 / 日志。
    """
    return _lookup("AI_API_KEY", "")


def max_tokens_default() -> int:
    """AI_MAX_TOKENS 的值（可覆盖默认 2000）；非正/非整数 → 2000。

    2026-10-17 改：kimi-k3 时代默认 6000，因为它是**推理模型**——
    `max_tokens=2000` 会被全部烧在内部 reasoning（实测
    `usage.completion_tokens_details.reasoning_tokens=2000、text_tokens=0、
    finish_reason="length"`），content 一个字吐不出来，必须给 6000 留余量。

    qwen-flash **不是**推理模型，2000 写正文已远远够；再大就是白白按量计费。
    """
    raw = _lookup("AI_MAX_TOKENS", "")
    if not raw:
        return 2000
    try:
        n = int(raw)
        return n if n > 0 else 2000
    except ValueError:
        return 2000


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
    error: str = ""          # 机器可读分类：proxy_down / timeout / http_error / bad_response / empty_content
    detail: str = ""         # 给人看的原因
    raw: dict = field(default_factory=dict)   # 成功时的原始 JSON（调试用）

    # ── 2026-10-17 追加的诊断字段（只加，不改既有含义）──────────────────
    #   起因：report 路真跑两次，第二次 200 ok 但 content 是空串。没有
    #   finish_reason / usage / 耗时，根本分不出是「网关超时截断」还是
    #   「模型把答案放进了 reasoning」。
    finish_reason: str = ""     # choices[0].finish_reason（stop / length / ...）
    usage: dict = field(default_factory=dict)   # usage 原样（prompt/completion/total tokens）
    elapsed_ms: int = 0         # 从 urlopen 发起到读完 body 的壁钟毫秒
    raw_text_len: int = 0       # HTTP body 字节数（判断"根本没回 vs 回了但解析丢"）
    retried: bool = False       # 本结果是否经历过一次「空正文重试」（防递归轰炸）


# ══════════════════════════════════════════════════════════════════
# 客户端本体
# ══════════════════════════════════════════════════════════════════

def _build_headers() -> dict[str, str]:
    """构造请求头。

    鉴权口径（2026-10-17）：
      · 有 `AI_API_KEY`（环境变量 > .env）→ `Authorization: Bearer <key>`；
      · 没 key → 不塞这个头（本地代理那种不需要鉴权的场景仍可用）。
    ⚠️ key 永远不出现在日志 / detail / 抛出的异常里。
    """
    headers = {"Content-Type": "application/json"}
    k = api_key()
    if k:
        headers["Authorization"] = "Bearer " + k
    return headers


def chat_completions(
    messages: list[dict],
    *,
    model: str | None = None,
    url: str | None = None,
    timeout: float | None = None,
    temperature: float = 0.3,
    max_tokens: int | None = None,
    _retried: bool = False,
) -> LLMResult:
    """调一次 /v1/chat/completions。**任何失败都返回 `ok=False`，不抛。**

    `messages` 是 OpenAI 形状：`[{"role": "system"/"user", "content": str}, ...]`。
    返回 `LLMResult` —— 调用方**只查 `.ok`**，别想当然去取 `.content`。

    `max_tokens=None` 时按 `AI_MAX_TOKENS` / 默认 2000 取（见
    `max_tokens_default`）；显式传值时一率用调用方的。

    ⚠️ 2026-10-17 新增：拿到 200 但 content 是**空串**时，发起**至多一次**
    同参数重试（`_retried` 递归闸，绝不无限）。`LLMResult.retried` 如实上报
    「这次结果是重试后才拿到的」——上层不必关心，但日志看得出来。
    """
    used_model = model or model_name()
    used_url = url or base_url()
    used_timeout = timeout if timeout is not None else timeout_seconds()
    used_max_tokens = max_tokens if max_tokens is not None else max_tokens_default()

    payload = {
        "model": used_model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": used_max_tokens,
        "stream": False,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        used_url,
        data=body,
        method="POST",
        headers=_build_headers(),
    )

    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=used_timeout) as resp:
            status = resp.status
            raw_bytes = resp.read()
    except urllib.error.HTTPError as e:
        # 网关回了，但不是 2xx（401 key 错 / 404 路径错 / 500 网关自己炸了 / …）
        # ⚠️ 绝不把 key 放进 detail（401 时也不）。
        return LLMResult(
            ok=False, model=used_model, error="http_error",
            detail=f"网关回了 HTTP {e.code}（{e.reason}）。"
                   f"key 或路径是不是写错了？",
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
    except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
        # ── 连接层失败。分两类，给玩家不同的提示 ─────────────
        elapsed_ms = int((time.monotonic() - started) * 1000)
        reason = getattr(e, "reason", e)
        # 超时（read/connect 都归这里）
        if isinstance(e, (socket.timeout, TimeoutError)) or "timed out" in str(reason):
            return LLMResult(
                ok=False, model=used_model, error="timeout",
                detail=f"超过 {used_timeout:.0f} 秒没等到回答（远端思考常要几十秒）。"
                       f"可以稍后再试，或把超时调大（AI_TIMEOUT）。",
                elapsed_ms=elapsed_ms,
            )
        # 其余（连接被拒 / 主机不可达 / DNS …）→ "代理没起/外网断了"
        return LLMResult(
            ok=False, model=used_model, error="proxy_down",
            detail=f"连不上 LLM 端点（{used_url}）：{reason}。"
                   f"网络是不是断了？先恢复网络再让 AI 出主意。",
            elapsed_ms=elapsed_ms,
        )
    except OSError as e:  # 兜底：任何残余的底层错误，别让它穿出去
        return LLMResult(
            ok=False, model=used_model, error="proxy_down",
            detail=f"网络层错误：{e}",
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    elapsed_ms = int((time.monotonic() - started) * 1000)

    if status != 200:
        return LLMResult(
            ok=False, model=used_model, error="http_error",
            detail=f"网关回了 HTTP {status}。",
            elapsed_ms=elapsed_ms, raw_text_len=len(raw_bytes),
        )

    # ── 解析：网关可能回一个字都看不懂的东西 ─────────────────────
    try:
        data = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        return LLMResult(
            ok=False, model=used_model, error="bad_response",
            detail=f"网关回的不是合法 JSON（{e}）。这是网关的锅，不是模型的锅。",
            elapsed_ms=elapsed_ms, raw_text_len=len(raw_bytes),
        )

    # OpenAI 兼容形状：choices[0].message.content
    # ── 顺带采集 finish_reason / usage / 响应体字节数（诊断用）─
    finish_reason = ""
    usage: dict = {}
    if isinstance(data, dict):
        u = data.get("usage")
        if isinstance(u, dict):
            usage = u
        ch = data.get("choices")
        if isinstance(ch, list) and ch:
            c0 = ch[0]
            if isinstance(c0, dict):
                fr = c0.get("finish_reason")
                if isinstance(fr, str):
                    finish_reason = fr

    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        return LLMResult(
            ok=False, model=used_model, error="bad_response",
            detail=f"网关回的 JSON 没有 choices[0].message.content（{e}）。"
                   f"形状：{list(data)[:5] if isinstance(data, dict) else type(data)}",
            elapsed_ms=elapsed_ms, raw_text_len=len(raw_bytes),
            finish_reason=finish_reason, usage=usage,
        )

    # ── ★ 空正文（200 ok 但 content 是空/纯空白）──────────────────────
    #   拿到 200 但 content 是空串时，发起**至多一次**同参数重试（`_retried`
    #   递归闸，绝不无限）。`LLMResult.retried` 如实上报。其它错误一律不重试
    #   （网关对其它错有自己的语义，乱重试是轰炸）。
    if isinstance(content, str) and not content.strip() and not _retried:
        retry = chat_completions(
            messages,
            model=used_model, url=used_url, timeout=used_timeout,
            temperature=temperature, max_tokens=used_max_tokens,
            _retried=True,
        )
        retry.retried = True
        # 把第一次的诊断字段也透出来（调用方看得到「第一次也是空的」）
        if retry.ok and not retry.finish_reason:
            retry.finish_reason = finish_reason
        return retry

    return LLMResult(
        ok=True, model=data.get("model", used_model), content=str(content), raw=data,
        elapsed_ms=elapsed_ms, raw_text_len=len(raw_bytes),
        finish_reason=finish_reason, usage=usage,
        retried=_retried,
    )
