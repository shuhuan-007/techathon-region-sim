"""AI 主机层与 HTTP 只读路由 —— **离线**测试（不打真网关）。

跑法（在 region-sim/ 下）：

    python tests/test_ai_host.py

依据《规则基线》§11.4：AI 可解释 / 可试算 / 可起草 → 玩家确认 → 提交；
**AI 无 commit**。本文件钉死三件事：

    ①  **只读**：advise 前后 session 的 `state_for` 逐字相同；
    ②  **绝不 submit**：`plans` 与 `phase` 一个字不动；
    ③  **优雅降级**：网关没起 / 回了垃圾 → `ok: false` + 明确原因，**不抛**。

离线手法：`advise(..., client=假客户端)`（签名请以
`server/ai_host.py` 的实现为准 —— 本文件按它的 `client` 关键字参数注入，
不 monkeypatch 环境、不开端口、不打真网关）。
"""

from __future__ import annotations

import copy
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage import MemoryStorage  # noqa: E402  (sys.path 就位后再 import)
from engine.loader import load_config  # noqa: E402
from server import ai_host, llm_client  # noqa: E402
from server.app import App, dispatch  # noqa: E402
from server.registry import GameRegistry  # noqa: E402

TOL = 1e-9

#: 开局现金的锚点（tests/test_server.py::test_new_game_creates_playable_opening
#: 口径）。evidence 断言"含具体数字且与 state 一致"就靠它。
OPENING_CASH = 1200.0


# ══════════════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════════════


def _fresh_app() -> App:
    cfg = load_config()
    registry = GameRegistry(cfg, MemoryStorage())
    return App(cfg, registry)


def _fresh_session(app: App, gid: str = "g1"):
    """开一局新的，返回已挂进 registry 的 session。"""
    app.registry.create(gid, "p1")
    return app.registry.get(gid)


class FakeClient:
    """一个不打网络的假 LLM 客户端：记下发来的 messages，回一个固定结果。

    `result` 支持两种形状（ai_host 两种都收）：
      · `llm_client.LLMResult(...)`（dataclass）；
      · 等价 dict（`{"ok": ..., "content": ..., ...}`）。
    要模拟"客户端自己炸了"：`raise_exc=SomeException(...)`。
    """

    def __init__(self, *, result=None, raise_exc=None):
        self.result = result if result is not None else llm_client.LLMResult(
            ok=True, model="fake-kimi", content="【假客户端固定建议】先保粮食，再扩产。"
        )
        self.raise_exc = raise_exc
        self.calls: list[list[dict]] = []

    def chat_completions(self, messages, **_kw):
        self.calls.append(messages)
        if self.raise_exc is not None:
            raise self.raise_exc
        return self.result


def test_empty_model_reply_is_failure_not_fake_success():
    """★ 防回归：模型回**空正文**时必须报失败，不许报 ok=True。

    ⚠️ 2026-10-17 凌晨真跑网关时抓到的缺陷：
       模型回了 HTTP 200 但 `content` 为空（网关偶发/被截断/只回了思考块），
       而当时的实现会一路走到 `_ok_advice(summary="")` —— 于是返回
       **`ok: True` 却没有任何建议**。那比报错更坏：前端会显示
       「AI 说：（空）」，玩家以为 AI 没想法，真相却是「这次没拿到回答」。

    正确行为：`ok: False` + `error: "empty_response"` + 一句能看懂的说明。
    """
    app = _fresh_app()
    session = _fresh_session(app)

    for blank in ("", "   ", "\n\t  \n"):
        fake = FakeClient(result=llm_client.LLMResult(
            ok=True, model="fake-kimi", content=blank,
        ))
        out = ai_host.advise(session, "p1", client=fake)
        assert out.get("ok") is False, (
            f"空回答（{blank!r}）必须报失败，实得 ok={out.get('ok')!r}、"
            f"summary={out.get('summary')!r}"
        )
        assert out.get("error") == "empty_response", (
            f"错误分类应为 empty_response，实得 {out.get('error')!r}"
        )
        assert str(out.get("detail") or "").strip(), "必须给一句人话说明"
        # 不许把空回答包装成"有建议"
        assert not out.get("summary"), f"失败时不该带 summary：{out.get('summary')!r}"


# ══════════════════════════════════════════════════════════════════
# 一、★ 只读 + 绝不 submit（最重要的两条）
# ══════════════════════════════════════════════════════════════════


def test_advise_is_readonly_and_never_submits():
    """① advise 前后，`state_for` 逐字相同（json 级比对）；
    ② `plans` 没变、`phase` 没变（跑完 AI 还是"什么都没发生"）。"""
    app = _fresh_app()
    session = _fresh_session(app)
    fake = FakeClient()

    before_state = json.dumps(
        session.state_for("p1"), ensure_ascii=False, sort_keys=True, default=str
    )
    before_plans = copy.deepcopy(session.plans)
    before_phase = session.phase

    out = ai_host.advise(session, "p1", "这季先做什么？", client=fake)

    assert out["ok"] is True, f"假客户端回 ok，主机层却判失败：{out}"
    after_state = json.dumps(
        session.state_for("p1"), ensure_ascii=False, sort_keys=True, default=str
    )
    assert after_state == before_state, (
        "★ 只读被破坏：advise 前后 state_for 不一致"
    )
    assert session.plans == before_plans, "★ advise 动了 plans（绝不 submit）"
    assert session.phase == before_phase, "★ advise 动了 phase（绝不 submit）"


def test_advise_passes_rule_and_question_to_model():
    """发给模型的 messages 里应含规则摘要节号与玩家问题 —— 让 AI 有语境。"""
    app = _fresh_app()
    session = _fresh_session(app)
    fake = FakeClient()
    out = ai_host.advise(session, "p1", "我该先建什么？", client=fake)
    assert out["ok"] is True
    assert fake.calls, "假客户端应被调过一次"
    user = fake.calls[0][1]["content"]
    assert "11.4" in user, "messages 应带上基线 §11.4 的规则摘要"
    assert "我该先建什么？" in user, "messages 应带上玩家的问题"
    # 把局面递给模型是为了让它引用真数字，不许编
    assert "1200" in user, "messages 应带上开局现金 1200（否则 AI 只能瞎猜）"


# ══════════════════════════════════════════════════════════════════
# 二、★ 优雅降级（不抛异常，明确失败）
# ══════════════════════════════════════════════════════════════════


def test_unavailable_client_returns_clear_failure_not_raise():
    """网关没起（连接被拒）→ `ok: false` + 明确原因，**不抛异常**。"""
    app = _fresh_app()
    session = _fresh_session(app)
    # 模拟 llm_client 的"代理没起"结果（不真去连端口，离线）
    fake = FakeClient(result=llm_client.LLMResult(
        ok=False, model="kimi-k3", error="proxy_down",
        detail="连不上本地网关（127.0.0.1:8787）。是不是代理没起？",
    ))
    out = ai_host.advise(session, "p1", client=fake)   # ★ 不抛即过——下面再断形状
    assert out["ok"] is False
    assert out["error"] == "proxy_down"
    assert "代理没起" in out["detail"]


def test_real_gateway_probe_reports_shape():
    """真网关可达性探测 → **可跳过**（本测试默认跳过，离线环境不受影响）。

    想看真链路是否通：`AI_PROBE=1 python tests/test_ai_host.py`。
    其余测试全部离线，不依赖这条。
    """
    import os
    if not os.environ.get("AI_PROBE"):
        print("    (skip：默认跳过真网关探测；AI_PROBE=1 可开)            ", end="")
        return
    app = _fresh_app()
    session = _fresh_session(app)
    out = ai_host.advise(session, "p1", "做个最小测试")
    assert isinstance(out.get("ok"), bool)
    if not out["ok"]:
        # 不在线也得是**明确失败**，不能是空建议
        assert out["error"] and out["detail"]


def test_raising_client_reaches_caller_asis():
    """**按 ai_host 的实现为准**：客户端 raise（非"回失败结果"）时，
    异常**会穿过** advise 到调用方 —— 本测试把这个边界钉死，
    并断言"穿过路由层时被兜成 500，**不会**被前端当成一次成功"。"""
    app = _fresh_app()
    session = _fresh_session(app)
    fake = FakeClient(raise_exc=TimeoutError("read timed out"))

    # ① ai_host 自己不兜 raise（它只兜"网关回了 ok:false"）—— 这是既定实现，
    #    由路由层（dispatch 的收网）翻译成 HTTP，见②。
    raised = False
    try:
        ai_host.advise(session, "p1", client=fake)
    except TimeoutError:
        raised = True
    assert raised, "客户端 raise 应穿过 ai_host（它只兜 ok:False 结果，不兜异常）"

    # ② 路由层**：`dispatch` 收网成 JSON，HTTP 不带时不会把前端当成功。
    app2 = _fresh_app()
    app2.registry.create("g1", "p1")
    orig = ai_host.advise  # 暂存真函数，monkeypatch 成会 raise 的版本
    import server.app as app_mod
    app_mod.ai_host.advise = lambda *a, **k: (_ for _ in ()).throw(
        TimeoutError("read timed out")
    )
    try:
        code, body = dispatch(
            app2, "POST", "/api/ai/advise", {}, {"game": "g1", "player": "p1"}
        )
    finally:
        app_mod.ai_host.advise = orig
    assert code == 500, f"客户端 raise 应被路由翻成 500，实得 {code}：{body}"
    assert body["ok"] is False and body["kind"] == "internal"
    assert body["error"] == "TimeoutError"


def test_garbage_reply_is_clear_error_not_crash():
    """模型回了垃圾（非 JSON 自由文本）→ 不崩、明确失败，**不许当成建议**。"""
    app = _fresh_app()
    session = _fresh_session(app)
    fake = FakeClient(result=llm_client.LLMResult(
        ok=False, model="kimi-k3", error="bad_response",
        detail="网关回的不是合法 JSON。这是网关的锅，不是模型的锅。",
    ))
    out = ai_host.advise(session, "p1", client=fake)
    assert out["ok"] is False
    assert out["error"] == "bad_response"
    assert out["detail"], "垃圾回答要有一句给人看的明确原因"


# ══════════════════════════════════════════════════════════════════
# 三、★ evidence 必须来自局面 / 试算，不是模型编的
# ══════════════════════════════════════════════════════════════════


def test_evidence_comes_from_state_not_fabricated():
    """`evidence` 要含**具体数字且与 session.state_for 一致**（不许编）。"""
    app = _fresh_app()
    session = _fresh_session(app)
    out = ai_host.advise(session, "p1", client=FakeClient())
    assert out["ok"] is True
    ev = out["evidence"]
    assert ev, "advice 必须带 evidence（引用要能指回局面）"
    cash_row = next((e for e in ev if e.get("field") == "cash"), None)
    assert cash_row is not None, "evidence 里应含 cash（引用的具体数字）"
    state_cash = session.state_for("p1")["cash"]
    assert abs(cash_row["value"] - state_cash) < TOL, (
        f"★ evidence 的 cash 与 state_for 不一致：{cash_row['value']} != {state_cash}"
    )
    assert abs(cash_row["value"] - OPENING_CASH) < TOL
    assert cash_row["source"] == "state", "cash 的来源要标 state，不是模型编的"


# ══════════════════════════════════════════════════════════════════
# 四、★ 路由层（dispatch 直测，不起 socket）
# ══════════════════════════════════════════════════════════════════


def test_route_advise_dispatch_shape():
    """`POST /api/ai/advise` 的形状：200 + `{ok, advice}` —— **AI 可用与否都
    是 200**，前端只查 `ok`。

    人工离线：monkeypatch `server.app.ai_host.advise` 成一个"网关没起"的固定
    失败，把形状钉死（不依赖真网关可不可达）。
    """
    app = _fresh_app()
    app.registry.create("g1", "p1")

    import server.app as app_mod
    orig = app_mod.ai_host.advise
    app_mod.ai_host.advise = lambda *a, **k: {
        "ok": False, "summary": "", "suggested_plan": None,
        "evidence": [], "model": "",
        "rule_key": "11.4", "error": "proxy_down",
        "detail": "连不上本地网关（127.0.0.1:8787）。是不是代理没起？",
    }
    try:
        code, body = dispatch(
            app, "POST", "/api/ai/advise", {},
            {"game": "g1", "player": "p1", "question": "先做什么？"},
        )
    finally:
        app_mod.ai_host.advise = orig

    assert code == 200, f"AI 不可用也应回 200（不是 5xx），实得 {code}：{body}"
    assert body["ok"] is False
    assert body["advice"]["ok"] is False
    assert body["advice"]["error"] == "proxy_down"
    assert "代理没起" in body["advice"]["detail"]


def test_route_passes_preview_plan_through():
    """★ 路由必须把 `preview_plan` **原样透传**给 ai_host（2026-10-17 修的缺口）。

    ⚠️ 这个缺口曾经的后果：秘书拿到的是「没有试算数字」的局面，于是它只能空口
       回答。真跑实录里 AI 自己说「本次没有跑任何试算，所以我手里没有任何成本、
       价格、配方、收益数字」—— 玩家问「我这样安排会怎样」，它答不了。

    这里只钉「透传」这一件事（离线、假 ai_host）：
      · 传了 → ai_host 收到的 preview_plan 与路由收到的**同一个对象内容**；
      · 没传 → ai_host 收到的仍是 None（向后兼容）。
    """
    app = _fresh_app()
    app.registry.create("g1", "p1")

    import server.app as app_mod
    orig = app_mod.ai_host.advise
    captured: dict = {}

    def spy(session, player_id="p1", question=None, **kw):
        captured.clear()
        captured.update({"player_id": player_id, "question": question, **kw})
        return {"ok": True, "summary": "假建议", "suggested_plan": None,
                "evidence": [], "model": "fake", "error": ""}

    plan = {"player_id": "p1", "builds": [
        {"plot_id": "0,0", "plot_type": "agriculture",
         "target_level": 1, "product": "A"}]}

    app_mod.ai_host.advise = spy
    try:
        # ① 带 preview_plan
        code, body = dispatch(app, "POST", "/api/ai/advise", {},
                              {"game": "g1", "player": "p1",
                               "question": "这样安排行吗？",
                               "preview_plan": plan})
        assert code == 200 and body["ok"] is True, f"路由应成功：{code} {body}"
        assert captured.get("preview_plan") == plan, (
            f"preview_plan 没透传到位：收到 {captured.get('preview_plan')!r}"
        )
        assert captured.get("question") == "这样安排行吗？"

        # ② 不带 preview_plan → None（旧行为不变）
        captured.clear()
        dispatch(app, "POST", "/api/ai/advise", {},
                 {"game": "g1", "player": "p1"})
        assert captured.get("preview_plan") is None, (
            "不传 preview_plan 时 ai_host 应收到 None，"
            f"实得 {captured.get('preview_plan')!r}"
        )
    finally:
        app_mod.ai_host.advise = orig


def test_route_advise_accepts_missing_question():
    """`question` 可省（让模型直给一版建议）—— 不应 400。"""
    app = _fresh_app()
    app.registry.create("g1", "p1")
    code, body = dispatch(
        app, "POST", "/api/ai/advise", {}, {"game": "g1", "player": "p1"}
    )
    assert code == 200, f"question 可省，实得 {code}：{body}"


def test_route_advise_requires_game_and_player():
    """缺 `game` / `player` → 400 bad_request（这是请求不齐，不是 AI 的锅）。"""
    app = _fresh_app()
    app.registry.create("g1", "p1")
    code, body = dispatch(app, "POST", "/api/ai/advise", {}, {"player": "p1"})
    assert code == 400 and body["kind"] == "bad_request"
    code, body = dispatch(app, "POST", "/api/ai/advise", {}, {"game": "g1"})
    assert code == 400 and body["kind"] == "bad_request"


def test_route_advise_unknown_game_is_404():
    """局不存在 → 404（reading 一个不存在的局不该顺手新开一局）。"""
    app = _fresh_app()
    code, body = dispatch(
        app, "POST", "/api/ai/advise", {}, {"game": "nope", "player": "p1"}
    )
    assert code == 404 and body["kind"] == "not_found"


def test_route_advise_wrong_method_is_405():
    c_app = _fresh_app()
    code, body = dispatch(c_app, "GET", "/api/ai/advise",
                          {"game": "g1", "player": "p1"}, {})
    assert code == 405 and body["kind"] == "method_not_allowed"


# ══════════════════════════════════════════════════════════════════
# 五、★ 回答口径（2026-10-17 用户拍板：「放开：可推算但须标依据」）
# ══════════════════════════════════════════════════════════════════


def test_system_preamble_allows_derived_numbers_with_basis():
    """★ 口径已放开：system message 必须给「自行推算」开口，并要求标依据。"""
    app = _fresh_app()
    session = _fresh_session(app)
    fake = FakeClient()
    out = ai_host.advise(session, "p1", "建 3 块要多少钱？", client=fake)
    assert out["ok"] is True
    assert fake.calls, "假客户端应被调过一次"
    system = fake.calls[0][0]["content"]
    # 推算的口子必须打开（否则 AI 答「建 3 块要多少钱」只会说「给不出」）
    assert "推算" in system, (
        f"system message 缺『推算』口径——用户已拍板放开：可推算但须标依据：{system}"
    )
    # 推算必须带依据——放开的不是「随便算」，是「算了要写清怎么算的」
    assert "依据" in system, (
        f"system message 缺『依据』要求——推算必须写清依据/口径/假设：{system}"
    )
    # 无依据仍不许编
    assert "拿不到" in system, (
        f"system message 缺『拿不到』分支——无任何依据时仍须明说：{system}"
    )


def test_system_preamble_keeps_permission_boundary():
    """★ 边界没被放宽掉：提交仍永远由玩家走 /api/submit，AI 无 commit。"""
    app = _fresh_app()
    session = _fresh_session(app)
    fake = FakeClient()
    out = ai_host.advise(session, "p1", client=fake)
    assert out["ok"] is True
    system = fake.calls[0][0]["content"]
    assert "/api/submit" in system, (
        f"system message 必须守住『提交永远由玩家走 /api/submit』：{system}"
    )
    assert "commit" in system, (
        f"system message 必须明确 AI 无 commit：{system}"
    )


def test_system_preamble_stays_compact():
    """★ 这段会进每次请求的 system role，太长会挤占上下文——长度有硬上限。"""
    app = _fresh_app()
    session = _fresh_session(app)
    fake = FakeClient()
    out = ai_host.advise(session, "p1", client=fake)
    assert out["ok"] is True
    system = fake.calls[0][0]["content"]
    assert len(system) < 1200, (
        f"system message 过长（{len(system)} 字，上限 1200）会挤占上下文"
    )


# ══════════════════════════════════════════════════════════════════
# runner（与 tests/test_ai_layer.py 同写法）
# ══════════════════════════════════════════════════════════════════


def _run_all() -> int:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    mod = sys.modules[__name__]
    names = sorted(n for n in dir(mod) if n.startswith("test_"))
    ok = fail = 0
    print("=" * 68)
    print("AI 主机层 + 路由（离线）")
    print("=" * 68)
    for n in names:
        try:
            getattr(mod, n)()
            print(f"  PASS  {n}")
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"  FAIL  {n}\n        {type(e).__name__}: {e}")
            fail += 1
    print(f"\n{ok} passed, {fail} failed, {len(names)} total")
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
