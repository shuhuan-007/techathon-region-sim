"""ai/narrate.py 的测试：解析皮实性、绝不编造、形状正确。**全程离线**（假客户端）。"""
from __future__ import annotations

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
