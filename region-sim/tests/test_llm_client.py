"""server/llm_client.py 的**离线**单元测试。

覆盖四件事（2026-10-17 任务书）：

  1. `.env` 解析：`KEY=VALUE`、引号剥掉、注释/空行忽略、缺文件不报错；
  2. 优先级：真实环境变量 > .env > 代码默认值；
  3. Authorization 头：有 key 时带 `Bearer <key>`，无 key 时**没有**这个头；
  4. 报错信息里**不含 key 明文**（401 时也不行）。

⚠️ 全部离线——用 monkeypatch 替换 `urllib.request.urlopen`，绝不真发网络。

跑法（在 region-sim/ 下）：

    python tests/test_llm_client.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server import llm_client


# ══════════════════════════════════════════════════════════════════
# 工具：隔离 .env 缓存 + 隔离 os.environ
# ══════════════════════════════════════════════════════════════════

class _EnvIsolation:
    """with 一个块：进时快照明细，出时恢复；同时清 llm_client 的 .env 缓存。"""

    KEYS = ("AI_API_KEY", "AI_BASE_URL", "AI_MODEL", "AI_TIMEOUT", "AI_MAX_TOKENS")

    def __enter__(self):
        self._backup = {k: os.environ.get(k) for k in self.KEYS}
        for k in self.KEYS:
            os.environ.pop(k, None)
        llm_client._reset_dotenv_cache()
        return self

    def __exit__(self, *exc):
        for k, v in self._backup.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        llm_client._reset_dotenv_cache()
        return False


def _write_env(tmpdir: Path, text: str) -> Path:
    p = Path(tmpdir) / ".env"
    p.write_text(text, encoding="utf-8")
    return p


def _use_env_file(p: Path | None):
    """把 llm_client 的 .env 路径指到 p；之后清缓存让它生效。"""
    if p is None:
        llm_client._ENV_PATH = Path(tempfile.gettempdir()) / "definitely-not-exists-xyz.env"
    else:
        llm_client._ENV_PATH = p
    llm_client._reset_dotenv_cache()


# ══════════════════════════════════════════════════════════════════
# 一、.env 解析
# ══════════════════════════════════════════════════════════════════

def test_dotenv_parses_key_value_lines():
    with _EnvIsolation(), tempfile.TemporaryDirectory() as td:
        p = _write_env(
            td,
            "AI_API_KEY=sk-abcdef\n"
            "AI_BASE_URL=https://example.com/v1/chat/completions\n"
            "AI_MODEL=qwen-flash\n",
        )
        _use_env_file(p)
        assert llm_client.api_key() == "sk-abcdef", llm_client.api_key()
        assert llm_client.base_url() == "https://example.com/v1/chat/completions"
        assert llm_client.model_name() == "qwen-flash"


def test_dotenv_strips_quotes():
    with _EnvIsolation(), tempfile.TemporaryDirectory() as td:
        p = _write_env(
            td,
            'AI_API_KEY="sk-with-double"\n'
            "AI_MODEL='qwen-single'\n"
            # 不成对的引号不剥
            "AI_BASE_URL=https://unmatched.example/x\"tail\n",
        )
        _use_env_file(p)
        assert llm_client.api_key() == "sk-with-double"
        assert llm_client.model_name() == "qwen-single"
        # 不成对：保留原样
        assert llm_client.base_url().endswith('"tail')


def test_dotenv_ignores_comments_and_blank_lines():
    with _EnvIsolation(), tempfile.TemporaryDirectory() as td:
        p = _write_env(
            td,
            "# 这是注释\n"
            "\n"
            "   \n"
            "#另一个注释\n"
            "AI_MODEL=qwen-flash\n"
            "   # 前面有空白也算注释\n"
            "AI_NO_EQUALS_LINE\n"          # 无等号 → 跳过
            "AI_API_KEY = spaced-value   \n",   # 等号与值两侧空白 → 容忍
        )
        _use_env_file(p)
        assert llm_client.model_name() == "qwen-flash"
        assert llm_client.api_key() == "spaced-value"


def test_dotenv_missing_file_returns_defaults():
    with _EnvIsolation():
        _use_env_file(None)   # 指到一个绝对不存在的路径
        assert llm_client.api_key() == ""
        assert llm_client.base_url() == llm_client.DEFAULT_BASE_URL
        assert llm_client.model_name() == llm_client.DEFAULT_MODEL
        assert llm_client.timeout_seconds() == llm_client.DEFAULT_TIMEOUT
        assert llm_client.max_tokens_default() == 2000


def test_default_is_dashscope_qwen_flash():
    """默认值本身就得指向 DashScope / qwen-flash（任务书明确要求）。"""
    with _EnvIsolation():
        _use_env_file(None)
        assert "dashscope.aliyuncs.com" in llm_client.DEFAULT_BASE_URL
        assert llm_client.DEFAULT_MODEL == "qwen-flash"


# ══════════════════════════════════════════════════════════════════
# 二、优先级：env > .env > default
# ══════════════════════════════════════════════════════════════════

def test_env_var_overrides_dotenv():
    with _EnvIsolation(), tempfile.TemporaryDirectory() as td:
        p = _write_env(td, "AI_MODEL=from-dotenv\n")
        _use_env_file(p)
        os.environ["AI_MODEL"] = "from-real-env"
        assert llm_client.model_name() == "from-real-env"


def test_dotenv_overrides_default():
    with _EnvIsolation(), tempfile.TemporaryDirectory() as td:
        p = _write_env(td, "AI_MODEL=from-dotenv\n")
        _use_env_file(p)
        assert llm_client.model_name() == "from-dotenv"


def test_empty_env_var_falls_back_to_dotenv():
    """空串的环境变量视为未设置 → 继续看 .env。"""
    with _EnvIsolation(), tempfile.TemporaryDirectory() as td:
        p = _write_env(td, "AI_MODEL=from-dotenv\n")
        _use_env_file(p)
        os.environ["AI_MODEL"] = ""   # 空串
        assert llm_client.model_name() == "from-dotenv"


def test_timeout_from_dotenv():
    with _EnvIsolation(), tempfile.TemporaryDirectory() as td:
        p = _write_env(td, "AI_TIMEOUT=90\n")
        _use_env_file(p)
        assert llm_client.timeout_seconds() == 90.0


def test_max_tokens_default_is_2000_and_overridable():
    with _EnvIsolation(), tempfile.TemporaryDirectory() as td:
        p = _write_env(td, "AI_MAX_TOKENS=4000\n")
        _use_env_file(p)
        assert llm_client.max_tokens_default() == 4000
        os.environ["AI_MAX_TOKENS"] = "1500"
        assert llm_client.max_tokens_default() == 1500
        # 垃圾值退回 2000
        os.environ["AI_MAX_TOKENS"] = "not-a-number"
        assert llm_client.max_tokens_default() == 2000


# ══════════════════════════════════════════════════════════════════
# 三、Authorization 头：有 key 带 Bearer；无 key 不带
# ══════════════════════════════════════════════════════════════════

class _FakeResp:
    """伪造 urlopen 的返回值：`with` 上下文 + .status + .read()。"""

    def __init__(self, payload: dict, status: int = 200):
        self._body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.status = status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


_OK_PAYLOAD = {
    "model": "qwen-flash",
    "choices": [
        {"message": {"role": "assistant", "content": "你好"}, "finish_reason": "stop"}
    ],
    "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
}


def _make_recorder(payload: dict = None, status: int = 200):
    """返回 (recorded: list[Request], fake_urlopen)。"""
    recorded: list = []

    def fake_urlopen(req, timeout=None):
        recorded.append(req)
        return _FakeResp(payload if payload is not None else _OK_PAYLOAD, status)

    return recorded, fake_urlopen


def test_auth_header_present_when_key_set(monkeypatch=None):
    with _EnvIsolation():
        _use_env_file(None)
        os.environ["AI_API_KEY"] = "sk-TESTKEY"
        recorded, fake = _make_recorder()
        orig = llm_client.urllib.request.urlopen
        llm_client.urllib.request.urlopen = fake  # type: ignore[assignment]
        try:
            r = llm_client.chat_completions([{"role": "user", "content": "hi"}])
        finally:
            llm_client.urllib.request.urlopen = orig  # type: ignore[assignment]
        assert r.ok and r.content == "你好"
        assert recorded, "没发任何请求"
        req = recorded[0]
        # urllib.request.Request 把头统一成 title-case
        assert req.get_header("Authorization") == "Bearer sk-TESTKEY", (
            f"headers: {req.header_items()}"
        )


def test_auth_header_absent_when_no_key():
    with _EnvIsolation():
        _use_env_file(None)
        recorded, fake = _make_recorder()
        orig = llm_client.urllib.request.urlopen
        llm_client.urllib.request.urlopen = fake  # type: ignore[assignment]
        try:
            r = llm_client.chat_completions([{"role": "user", "content": "hi"}])
        finally:
            llm_client.urllib.request.urlopen = orig  # type: ignore[assignment]
        assert r.ok
        req = recorded[0]
        assert req.get_header("Authorization") is None, (
            f"should have no Authorization: {req.header_items()}"
        )


def test_max_tokens_default_flows_into_payload():
    with _EnvIsolation():
        _use_env_file(None)
        recorded, fake = _make_recorder()
        orig = llm_client.urllib.request.urlopen
        llm_client.urllib.request.urlopen = fake  # type: ignore[assignment]
        try:
            llm_client.chat_completions([{"role": "user", "content": "hi"}])
        finally:
            llm_client.urllib.request.urlopen = orig  # type: ignore[assignment]
        req = recorded[0]
        body = json.loads(req.data.decode("utf-8"))
        assert body["max_tokens"] == 2000


# ══════════════════════════════════════════════════════════════════
# 四、报错信息里**不含 key 明文**
# ══════════════════════════════════════════════════════════════════

def test_401_detail_does_not_leak_key():
    with _EnvIsolation():
        _use_env_file(None)
        os.environ["AI_API_KEY"] = "sk-SECRET-LEAKME-NOT"

        def boom(req, timeout=None):
            raise urllib.error.HTTPError(
                url="https://dashscope.aliyuncs.com/x", code=401,
                msg="Unauthorized", hdrs=None, fp=None,
            )

        orig = llm_client.urllib.request.urlopen
        llm_client.urllib.request.urlopen = boom  # type: ignore[assignment]
        try:
            r = llm_client.chat_completions([{"role": "user", "content": "hi"}])
        finally:
            llm_client.urllib.request.urlopen = orig  # type: ignore[assignment]

        assert not r.ok and r.error == "http_error"
        assert "sk-SECRET" not in r.detail, r.detail
        assert "LEAKME" not in r.detail, r.detail


def test_timeout_detail_does_not_mention_key():
    class _TO(TimeoutError):
        pass

    def boom(req, timeout=None):
        raise _TO("timed out")

    with _EnvIsolation():
        _use_env_file(None)
        os.environ["AI_API_KEY"] = "sk-SECRET-XYZ"
        orig = llm_client.urllib.request.urlopen
        llm_client.urllib.request.urlopen = boom  # type: ignore[assignment]
        try:
            r = llm_client.chat_completions([{"role": "user", "content": "hi"}])
        finally:
            llm_client.urllib.request.urlopen = orig  # type: ignore[assignment]
        assert not r.ok and r.error == "timeout"
        assert "sk-SECRET" not in r.detail


# ══════════════════════════════════════════════════════════════════
# 测试入口
# ══════════════════════════════════════════════════════════════════

def _main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = 0
    failed = []
    for fn in fns:
        try:
            fn()
            passed += 1
            print(f"  ok    {fn.__name__}")
        except AssertionError as e:
            failed.append((fn.__name__, e))
            print(f"  FAIL  {fn.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failed.append((fn.__name__, e))
            print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
    print()
    print(f"{passed}/{len(fns)} 通过")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    _main()
