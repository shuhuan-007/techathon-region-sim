"""ai/narrate.py 的测试：解析皮实性、绝不编造、形状正确。**全程离线**（假客户端）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai import narrate as N            # noqa: E402
from server import llm_client          # noqa: E402


class FakeClient:
    """不打网络的假客户端。`content` 就是"模型回的话"。"""

    def __init__(self, content="", *, ok=True, error="", raise_exc=None):
        self.content = content
        self.ok = ok
        self.error = error
        self.raise_exc = raise_exc
        self.calls = []

    def chat_completions(self, messages, **_kw):
        self.calls.append(messages)
        if self.raise_exc is not None:
            raise self.raise_exc
        return llm_client.LLMResult(ok=self.ok, content=self.content,
                                    model="fake-kimi", error=self.error)


NEWS_OK = '{"headline": "第 3 季新闻", "body": "本季价格触底。", "facts_used": {"prices.G.price": 23.0}}'
REPORT_OK = '{"text": "## 第 3 季度财报解读\\n\\n本期扣款 104。", "facts_used": {"total_deduction": 104.0}}'


# ══════════════════════════════════════════════════════════════════
# 一、提示词文件（逐字）
# ══════════════════════════════════════════════════════════════════


def test_prompt_files_exist_and_are_verbatim():
    """提示词必须**逐字**在盘上（它们是用户朋友写的，本层不许改写）。

    长度阈值按**字符数**定（中文一字一字符）：实测 news 2743 字 / 99 行、
    report 1819 字 / 83 行，与源 docx 的 97 / 81 段落吻合。
    阈值取实测值的八成左右 —— 只为挡住"抄漏一大半/抄成摘要"，
    不做精确比对（精确比对要靠人，见 docstring 里的来源标注）。
    """
    news = N.load_prompt("news")
    rep = N.load_prompt("report")
    assert "你不是结算引擎" in news, "news 提示词少了关键句（是不是被改写了？）"
    assert "facts_used" in news
    assert "财报解读" in rep, "report 提示词少了关键句"
    assert len(news) > 2200, f"news 提示词太短（{len(news)} 字），可能没抄全"
    assert len(rep) > 1400, f"report 提示词太短（{len(rep)} 字），可能没抄全"


def test_unknown_kind_is_explicit():
    try:
        N.load_prompt("nosuch")
    except N.NarrateError as e:
        assert "nosuch" in str(e)
        return
    raise AssertionError("未知 kind 应报 NarrateError")


# ══════════════════════════════════════════════════════════════════
# 二、解析皮实性
# ══════════════════════════════════════════════════════════════════


def test_parse_plain_json():
    obj, problem = N.parse_model_json(NEWS_OK)
    assert obj and obj["headline"] == "第 3 季新闻" and not problem


def test_parse_json_in_code_fence():
    obj, problem = N.parse_model_json(f"```json\n{NEWS_OK}\n```")
    assert obj and obj["headline"] == "第 3 季新闻", f"代码围栏没剥掉：{problem}"


def test_parse_json_with_surrounding_chatter():
    text = f"好的，这是本季新闻：\n{NEWS_OK}\n希望对你有帮助！"
    obj, problem = N.parse_model_json(text)
    assert obj and obj["headline"] == "第 3 季新闻", f"前后废话没跳过：{problem}"


def test_parse_not_json_is_explicit():
    obj, problem = N.parse_model_json("我觉得这季还不错，价格挺稳的。")
    assert obj is None and problem, "非 JSON 必须明确报出来"


def test_parse_empty_is_explicit():
    for blank in ("", "   "):
        obj, problem = N.parse_model_json(blank)
        assert obj is None and problem


# ══════════════════════════════════════════════════════════════════
# 三、新闻叙事
# ══════════════════════════════════════════════════════════════════


def test_news_happy_path():
    out = N.narrate_news({"quarter": 3}, client=FakeClient(NEWS_OK))
    assert out["ok"] is True
    assert out["kind"] == "news" and out["quarter"] == 3
    assert out["headline"] == "第 3 季新闻"
    assert out["facts_used"] == {"prices.G.price": 23.0}
    assert out["model"] == "fake-kimi"


def test_news_facts_none_returns_none():
    """提示词要求：facts 为 null 时不许生成。"""
    assert N.narrate_news(None, client=FakeClient(NEWS_OK)) is None


def test_news_bad_response_does_not_fabricate():
    """★ 核心红线：模型没给 JSON，就**不许**把那段自由文本当叙事返回。"""
    free_text = "本季价格触底，建议谨慎扩产。"     # 看着挺像人话 —— 但它不是约定形状
    out = N.narrate_news({"quarter": 3}, client=FakeClient(free_text))
    assert out["ok"] is False, "非 JSON 必须报失败"
    assert out["error"] == "bad_response"
    assert "headline" not in out and "body" not in out, "不许把自由文本塞进叙事字段"


def test_news_json_null_is_failure_not_crash():
    out = N.narrate_news({"quarter": 3}, client=FakeClient("null"))
    assert out["ok"] is False and out["error"] == "bad_response"


def test_news_missing_facts_used_warns_but_keeps_body():
    out = N.narrate_news({"quarter": 3},
                         client=FakeClient('{"headline": "h", "body": "b"}'))
    assert out["ok"] is True and out["body"] == "b"
    assert out["facts_used"] == {}
    assert "facts_used" in out.get("warning", ""), "瑕疵要如实标出来"


def test_news_missing_headline_is_failure():
    out = N.narrate_news({"quarter": 3}, client=FakeClient('{"body": "只有正文"}'))
    assert out["ok"] is False and out["error"] == "bad_response"


# ══════════════════════════════════════════════════════════════════
# 四、财报解读
# ══════════════════════════════════════════════════════════════════


def test_report_happy_path():
    out = N.narrate_report({"quarter": 4}, client=FakeClient(REPORT_OK))
    assert out["ok"] is True and out["kind"] == "report" and out["quarter"] == 4
    assert "第 3 季度财报解读" in out["text"]
    assert out["facts_used"] == {"total_deduction": 104.0}


def test_report_accepts_body_alias():
    out = N.narrate_report({"quarter": 4},
                           client=FakeClient('{"body": "用 body 键给的正文"}'))
    assert out["ok"] is True and out["text"] == "用 body 键给的正文"
    assert "body" in out.get("warning", "")


def test_report_none_returns_none():
    assert N.narrate_report(None, client=FakeClient(REPORT_OK)) is None


def test_report_accepts_plain_markdown():
    """★ 财报那一路**本来就该是 Markdown**，不是 JSON（2026-10-17 真跑后修的）。

    `report.md` 提示词原文要求「输出供玩家阅读的**中文 Markdown 解读**」。
    第一次真跑时模型照做了、给了一份很像样的 Markdown，我们却按 JSON 判它
    `bad_response` 把好东西扔了 —— 那是**我们的对接错误**。
    现在：拿不到 JSON 就把 Markdown 原文当正文收下，并如实标"无溯源表"。
    """
    md = ("# 第1季度财报解读\n\n## 季度概览\n本季主要结算事项为地块建设。\n\n"
          "- 期初现金 1200.00，期末现金 978.00\n- 扣除合计 222.00")
    out = N.narrate_report({"quarter": 1}, client=FakeClient(md))
    assert out["ok"] is True, f"Markdown 应被接收，实得 {out.get('error')}"
    assert out["text"].startswith("# 第1季度财报解读")
    assert "978.00" in out["text"], "正文必须原样保留"
    assert out["facts_used"] == {}
    assert "Markdown" in out.get("warning", ""), "要如实标出没有溯源表"


def test_news_still_rejects_free_text():
    """⚠️ 新闻那一路**仍然严格**：它的提示词要求合法 JSON，不许网开一面。

    report 放宽不等于 news 放宽 —— 两条的提示词契约本来就不同。
    """
    out = N.narrate_news({"quarter": 3}, client=FakeClient("本季价格触底。"))
    assert out["ok"] is False and out["error"] == "bad_response"
    assert "headline" not in out and "body" not in out


# ══════════════════════════════════════════════════════════════════
# 五、失败路径都不许抛穿（session 侧还要兜一层，但本层先自己兜住）
# ══════════════════════════════════════════════════════════════════


def test_model_unavailable_is_clear_failure():
    out = N.narrate_news({"quarter": 3},
                         client=FakeClient("", ok=False, error="proxy_down"))
    assert out["ok"] is False and out["error"] == "proxy_down"


def test_empty_model_reply_is_failure():
    out = N.narrate_news({"quarter": 3}, client=FakeClient("   "))
    assert out["ok"] is False


def test_client_raising_does_not_escape():
    out = N.narrate_news({"quarter": 3},
                         client=FakeClient(raise_exc=RuntimeError("boom")))
    assert out["ok"] is False and out["error"] == "client_crashed"


def test_prompt_is_sent_as_system_message():
    c = FakeClient(NEWS_OK)
    N.narrate_news({"quarter": 3}, client=c)
    msgs = c.calls[0]
    assert msgs[0]["role"] == "system"
    assert "你不是结算引擎" in msgs[0]["content"], "系统消息应为提示词全文"
    assert msgs[1]["role"] == "user"
    assert '"quarter": 3' in msgs[1]["content"], "用户消息应为输入的 JSON"


# ══════════════════════════════════════════════════════════════════
# 六、2026-10-17 加固回归：空正文策略 / 超时默认 300 / 诊断字段 / 重试闸
# ══════════════════════════════════════════════════════════════════


def test_default_timeout_is_300():
    """★ 默认超时必须是 300 秒（实测：report 单次 100+ 秒，news 曾被 120 掐掉）。"""
    assert llm_client.DEFAULT_TIMEOUT == 300.0, (
        f"默认超时应为 300（实测 report 单次 54~69 秒，news 曾被 120 掐掉），"
        f"现在是 {llm_client.DEFAULT_TIMEOUT}"
    )


def test_default_max_tokens_is_2000_and_configurable():
    """★ 2026-10-17：默认 max_tokens 必须 == 2000，且 `AI_MAX_TOKENS` 可覆盖。

    历史：kimi-k3 是推理模型，2000 会被内部 reasoning 烧光、content 吐不出字，
    所以当年把默认抬到 6000。qwen-flash **不是**推理模型，2000 已远远够——再
    大就是白白按量计费。本测试锁的是新口径。
    """
    import os
    saved = os.environ.get("AI_MAX_TOKENS")
    os.environ.pop("AI_MAX_TOKENS", None)
    try:
        llm_client._reset_dotenv_cache()
        assert llm_client.max_tokens_default() == 2000, (
            f"qwen-flash 不是推理模型，默认应为 2000，现在是 {llm_client.max_tokens_default()}"
        )
        # chat_completions 的位置参数默认应是 None（让 max_tokens_default 生效）
        import inspect
        sig = inspect.signature(llm_client.chat_completions)
        assert sig.parameters["max_tokens"].default is None, (
            f"chat_completions 的 max_tokens 默认应为 None（让 AI_MAX_TOKENS 生效），"
            f"现在是 {sig.parameters['max_tokens'].default}"
        )
        # 覆盖：环境变量 4000 → max_tokens_default 也跟着变
        os.environ["AI_MAX_TOKENS"] = "4000"
        llm_client._reset_dotenv_cache()
        assert llm_client.max_tokens_default() == 4000
    finally:
        if saved is None:
            os.environ.pop("AI_MAX_TOKENS", None)
        else:
            os.environ["AI_MAX_TOKENS"] = saved
        llm_client._reset_dotenv_cache()


def test_llm_result_has_diagnostic_fields():
    """LLMResult 必须带 2026-10-17 新加的诊断字段（只加不改）。"""
    r = llm_client.LLMResult(ok=True, content="x")
    assert hasattr(r, "finish_reason") and r.finish_reason == ""
    assert hasattr(r, "usage") and r.usage == {}
    assert hasattr(r, "elapsed_ms") and r.elapsed_ms == 0
    assert hasattr(r, "raw_text_len") and r.raw_text_len == 0
    assert hasattr(r, "retried") and r.retried is False


def test_empty_content_is_failure_with_diag():
    """★ 空正文（200 ok 但 content 空）→ 上层看到的应是 bad_response + 诊断字段。"""
    class EmptyContentClient:
        def __init__(self):
            self.calls = 0

        def chat_completions(self, messages, **_kw):
            self.calls += 1
            # 模拟 llm_client 已经做过一次重试（retried=True）还是空的情形
            return llm_client.LLMResult(
                ok=True, content="", model="fake",
                finish_reason="length",
                usage={"completion_tokens": 2000,
                       "completion_tokens_details": {"reasoning_tokens": 2000,
                                                     "text_tokens": 0}},
                elapsed_ms=54000, raw_text_len=8442, retried=True,
            )

    out = N.narrate_report({"quarter": 3}, client=EmptyContentClient())
    assert out["ok"] is False, "空正文必须报失败"
    assert out["error"] == "bad_response"
    assert "空正文" in out["detail"]
    # 诊断字段必须透传上来（演示/排查时第一眼就能看到）
    assert out.get("finish_reason") == "length"
    assert out.get("retried") is True
    assert out.get("elapsed_ms", 0) > 0


def test_empty_content_client_retry_mechanism():
    """★ llm_client 自己：拿到 200 ok 但 content 空 → 会**至多一次**重试，
    结果带 retried=True。**绝不无限重试**。"""
    # 不真发网，构造假的 urlopen 来模拟两次调用
    import io as _io
    import urllib.request as _ureq

    payload_empty = json.dumps({
        "model": "fake", "choices": [{"message": {"content": ""},
                                       "finish_reason": "length"}],
        "usage": {"completion_tokens": 2000},
    }).encode("utf-8")
    payload_good = json.dumps({
        "model": "fake", "choices": [{"message": {"content": "正文"},
                                       "finish_reason": "stop"}],
        "usage": {"completion_tokens": 10},
    }).encode("utf-8")

    class FakeResp:
        def __init__(self, status, body):
            self.status = status
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    orig_urlopen = _ureq.urlopen
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        # 第一次返回空，第二次返回有正文
        body = payload_empty if calls["n"] == 1 else payload_good
        return FakeResp(200, body)

    _ureq.urlopen = fake_urlopen
    try:
        out = llm_client.chat_completions(
            [{"role": "user", "content": "hello"}],
            url="http://127.0.0.1:1/x",  # URL 无所谓，urlopen 已被替换
        )
        assert out.ok is True
        assert out.content == "正文"
        assert out.retried is True, "重试过一次必须如实上报"
        assert calls["n"] == 2, f"应恰好调用 2 次（1 原始 + 1 重试），实际 {calls['n']}"
    finally:
        _ureq.urlopen = orig_urlopen


def test_empty_content_retry_eventually_fails_cleanly():
    """★ 两次都拿到空正文 → 失败，但**只重试一次**（不无限重试）。"""
    import urllib.request as _ureq
    payload_empty = json.dumps({
        "model": "fake", "choices": [{"message": {"content": ""},
                                       "finish_reason": "length"}],
        "usage": {},
    }).encode("utf-8")

    class FakeResp:
        status = 200

        def read(self):
            return payload_empty

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    orig_urlopen = _ureq.urlopen
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        return FakeResp()

    _ureq.urlopen = fake_urlopen
    try:
        out = llm_client.chat_completions(
            [{"role": "user", "content": "hello"}],
            url="http://127.0.0.1:1/x",
        )
        # 第二次（重试那次）拿到的仍是空 → 返回 ok=True、content=""、retried=True
        # （content 为空的判失败发生在 narrate 层，llm_client 如实把它传上去）
        assert out.ok is True
        assert out.content == ""
        assert out.retried is True
        assert calls["n"] == 2, f"★ 必须只重试一次（总调用 2），实际 {calls['n']}"
    finally:
        _ureq.urlopen = orig_urlopen


def test_news_strictness_unchanged_by_hardening():
    """★★ news 那一路的严格性**绝不能**因加固而松动：
    拿不到 JSON 仍然报 bad_response，**绝不**退化成收 Markdown。"""
    md = "# 一个看起很像正文的 Markdown\n\n本季价格触底。"
    out = N.narrate_news({"quarter": 3}, client=FakeClient(md))
    assert out["ok"] is False, "news 路拿到非 JSON 必须仍然失败"
    assert out["error"] == "bad_response"
    assert "headline" not in out and "body" not in out, (
        "★★ news 路**绝不许**把 Markdown 当正文收"
    )


def test_diag_fields_propagate_on_failure():
    """★ 失败路径也要带诊断字段（排查空正文、超时、超 max_tokens 时能一眼看出）。"""
    class TimeoutishClient:
        def __init__(self):
            pass

        def chat_completions(self, messages, **_kw):
            return llm_client.LLMResult(
                ok=False, error="timeout",
                detail="超过 300 秒没等到回答",
                model="fake",
                elapsed_ms=300000, raw_text_len=0, retried=False,
            )

    out = N.narrate_news({"quarter": 3}, client=TimeoutishClient())
    assert out["ok"] is False and out["error"] == "timeout"
    assert out.get("elapsed_ms", 0) > 0, "耗时应透传到失败结果"


# ══════════════════════════════════════════════════════════════════

def _run_all() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
            passed += 1
        except Exception as e:  # noqa: BLE001
            print(f"  FAIL  {name}\n          {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed, {passed + failed} total")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
