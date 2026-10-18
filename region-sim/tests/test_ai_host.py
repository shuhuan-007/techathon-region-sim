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
OPENING_CASH = 2400.0


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
    assert "2400" in user, "messages 应带上季度初补贴后的开局现金 2400（否则 AI 只能瞎猜）"


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
# 六、★ 局面**拓宽**（2026-10-17：AI 之前太瞎了 —— 只给 7 个局面键，
#    没有价格表 / 地块清单 / 建造选项，于是面对「建什么划算」只能拒答）
# ══════════════════════════════════════════════════════════════════
#
# 真跑实录里 AI 自己说的：「我手里的规则摘要只有 §11.4，**没有地块建造成本
# 和农业 A 的产出参数**，所以「再建几块、花多少、回本多少」我给不出数」。
# 而这些 `/api/state` 里**全都有** —— 是没递过去，不是拿不到。
#
# 下面五条钉死「拓宽了，但没拓崩」：价格地块进来了 / 体积受控 / 老七键还在 /
# 不许为了拓宽凭空造数 / 体积闸真到极限会砍次要项。
# ══════════════════════════════════════════════════════════════════


def _grab_user_message(app=None, question="建什么划算？"):
    """ advis 一次，抓发给模型的 user message 正文（离线假客户端）。"""
    app = app or _fresh_app()
    session = _fresh_session(app)
    fake = FakeClient()
    out = ai_host.advise(session, "p1", question, client=fake)
    assert out["ok"] is True, f"advise 应成功：{out}"
    assert fake.calls, "假客户端应被调过一次"
    return fake.calls[0][1]["content"], session


def _fake_plot_for_count(
    plot_id,
    plot_type,
    level=1,
    *,
    product=None,
    status="running",
    construction_left=0,
    service_bonus=0.0,
    service_rate=0.0,
):
    """造一块与 `state_for` 同形的假地块（只填 100 块聚合测试要的字段）。"""
    eff = None
    if status == "running":
        eff = {
            "adjacency": 1.0 + service_bonus,
            "happiness": 1.0,
            "ramp_up": 1.0,
            "research_multiplier": 1.0,
            "efficiency": 1.0 + service_bonus,
            "linked_neighbors": 0,
            "adjacency_bonus": service_bonus,
            "adjacency_capped": False,
            "is_service": plot_type == "service",
            "service_rate": service_rate,
            "service_neighbors": 1 if service_bonus else 0,
            "service_bonus": service_bonus,
            "contributions": [],
        }
    return {
        "plot_id": plot_id,
        "plot_type": plot_type,
        "level": level,
        "status": status,
        "product": product,
        "name": None,
        "u": None,
        "efficiency": eff,
        "output_preview": None,
        "construction_left": construction_left,
        "target_level": level + (1 if status == "constructing" else 0),
        "running_quarters": 1 if status == "running" else 0,
    }


def _fake_100_plot_state() -> dict:
    """100 块地的局面（8550 规模的合成夹具）——**正是旧实现会 500 的形状**。

    分布刻意覆盖 G / H-A / I / 服务 / 在建：聚合后这些类别必须仍能辨认。
    """
    plots: list[dict] = []

    def add(n, plot_type, level=1, *, product=None, status="running",
            construction_left=0, **eff_kw):
        for _ in range(n):
            i = len(plots)
            plots.append(_fake_plot_for_count(
                f"{i // 10},{i % 10}", plot_type, level, product=product,
                status=status, construction_left=construction_left, **eff_kw,
            ))

    add(30, "agriculture", 1, product="C")
    add(10, "agriculture", 2, product="D")
    add(40, "resource", 1)
    add(10, "industry", 1, product="G")
    add(5, "industry", 2, product="H-A")
    add(3, "industry", 3, product="I")
    add(1, "service", 1, service_rate=0.075)
    add(1, "industry", 1, product="H-A", status="constructing",
        construction_left=1)
    assert len(plots) == 100, f"夹具应为 100 块，实得 {len(plots)}"
    # 资源组里两块吃到不同服务加成 → 聚合行显示区间（信息不丢）
    for p, bonus in ((plots[40], 0.05), (plots[41], 0.10)):
        p["efficiency"]["service_bonus"] = bonus
        p["efficiency"]["adjacency_bonus"] = bonus
        p["efficiency"]["service_neighbors"] = 1

    return {
        "quarter": 1, "total_quarters": 20, "phase": "collecting",
        "can_settle": False, "cash": 1200.0, "happiness": 0.6,
        "population": None,
        "market": {
            f"G{i:02d}": {
                "name": f"商品{i}", "P0": 10.0, "last_price": 10.0 + i,
                "current_price": None, "P_min": 1.0, "at_floor": None,
            }
            for i in range(15)
        },
        "plots": plots,
    }


def test_many_plots_compact_under_limit_without_assertion():
    """★ 100 块地（8550 规模）→ 局面文本必须压缩到上限内，**不许断言炸**。

    旧实现每块一行：100 行 ≈ 4.2k 字符 + 核心/价格 → 4912，`assert` 直接
    抛 AssertionError，模型一次都没被调用（真机表现为 AI 对话整体 500）。
    """
    state = _fake_100_plot_state()
    body = ai_host._compact_state("p1", state, None)   # ★ 不抛即过第一关
    assert len(body) < ai_host._STATE_BODY_LIMIT, (
        f"★ 100 块地压缩后 {len(body)} 字符，仍超硬上限 "
        f"{ai_host._STATE_BODY_LIMIT}"
    )
    # 8550 的目标线：聚合后应明显低于上限（不是刚好擦边）
    assert len(body) < 2500, (
        f"100 块地压缩后 {len(body)} 字符，未达到 <2500 的目标"
    )
    # 核心七键与地块块都还在
    assert "quarter=1/20" in body and "cash=1200" in body
    assert "[地块·100块·聚合]" in body, f"100 块应走聚合档：{body[:500]}"
    # 聚合后地块行应是个位/十位数，绝不是 100 行
    plot_rows = [ln for ln in body.splitlines() if "级，" in ln]
    assert len(plot_rows) == 8, f"聚合行数异常：{plot_rows}"


def test_aggregate_plot_lines_have_type_and_count():
    """★ 聚合行必须同时含**类型/产物**与**数量**（否则 AI 看不出分布）。

    如 `工业G×10（1级，运行中）`、`服务×1（1级，运行中，服务率7.5%）`。
    """
    text = "\n".join(ai_host._plot_lines(_fake_100_plot_state()))
    for expected in (
        "农业C×30（1级，运行中）",
        "农业D×10（2级，运行中）",
        "资源×40（1级，运行中，服务邻接+5%~10%）",
        "工业G×10（1级，运行中）",
        "工业H-A×1（1级，在建 1 季）",
        "工业H-A×5（2级，运行中）",
        "工业I×3（3级，运行中）",
        "服务×1（1级，运行中，服务率7.5%）",
    ):
        assert expected in text, (
            f"聚合行缺「{expected}」：\n{text}"
        )
    # 100 块不再逐块列（逐块明细才有 "id|类型" 这种坐标行）
    assert "0,0|" not in text, f"100 块仍逐块输出：{text[:500]}"


def test_state_context_now_includes_market_prices():
    """★ 拓宽点 ①：user message 里**出现了价格信息**。

    光断言"含 market 这个键"不够 —— 要断言**具体商品的具体数字**，
    才能证明"真把价格表递进去了"，而不是递了一个空壳/键名。
    """
    user, _ = _grab_user_message()
    assert "[市场]" in user, f"局面缺市场价格块：{user}"
    # 主粮 A 是 15 个商品的第一个，上季价 10 是 config 里定死的
    assert "A|主粮|上季10" in user, (
        f"局面里要能看到主粮 A 的上季价 10（否则 AI 还是瞎的）：{user}"
    )
    # 触底/正常的判定也要在（AI 要拿它说"这个价已经压到底了"）
    assert "触底" in user or "正常" in user, f"价格块缺触底标记：{user}"


def test_state_context_now_includes_plots_section():
    """★ 拓宽点 ②：user message 里**出现了地块区块**。

    开局还没地 —— 但**地块区块本身**必须在，并且要明说"没有地块"，
    不许整块缺席让 AI 猜。
    """
    user, _ = _grab_user_message()
    assert "[地块" in user, f"局面缺地块区块：{user}"
    assert "还没有任何地块" in user, (
        f"开局没有地块时要明说，不许整块缺席：{user}"
    )


def test_state_context_includes_build_options_with_cash():
    """★ 拓宽点 ③：user message 里有"可建类型 + 现金费" —— 这是 AI 答
    「建什么划算、要花多少」最需要的一手数据。"""
    user, _ = _grab_user_message()
    assert "[可建" in user, f"局面缺建造选项块：{user}"
    # 农业一级 80 现金是 config 里的原价 —— 这个数必须在
    assert "agriculture" in user and "80" in user, (
        f"建造选项里要能看到新建农业的现金费 80：{user}"
    )


def test_state_context_size_is_capped():
    """★ 体积闸：拼出来的 user message **不超过硬上限**。

    这段进**每一次**请求，无限增长 = 烧 token + 淹没重点。
    上限放 user message 整条（核心+价格+地块+建造+规则摘要+问题），
    而不是只放市场块 —— 整条受控才是真受控。
    """
    user, _ = _grab_user_message()
    # 整条含规则摘要（§11.4 本身就 600+ 字），所以上限给整条 4000 有余量
    assert len(user) <= 6000, (
        f"发给模型的 user message 过长（{len(user)} 字符），会烧 token 淹没重点"
    )
    # 局面块本身（不含规则摘要/问题）应更紧 —— 它就是 _compact_state 的产物
    state_body = user.split("[规则摘要")[0]
    assert len(state_body) <= ai_host._STATE_BODY_LIMIT, (
        f"局面文本 {len(state_body)} 字符超过硬上限 {ai_host._STATE_BODY_LIMIT}；"
        f"体积闸没拦住"
    )


def test_state_context_includes_actionable_capabilities():
    """★ 2026-10-18：局面必须带「能往哪建 / 哪些地能开工 / 要多少口粮」的快照。

    这是真实故障的修法：以前模型只看得到自己的地块，没有空地清单，
    于是把已有地块写成 `builds`。快照块名是契约，缺一块就说明接入断了。
    """
    user, _ = _grab_user_message()
    for token in ("[现金/预算]", "[空地]", "[可建]", "[可开工]", "[人口/口粮]"):
        assert token in user, f"可行动作快照缺 {token} 块：{user}"
    # 开局 10×10、0 地块 → 第一格必须在空地表里
    assert "0,0" in user, f"空地表应列出 0,0：{user}"
    # 末尾硬提示（模型最容易犯的错）
    assert "漏写=不开工" in user, f"缺「漏写=不开工」硬提示：{user}"
    # ★ 2026-10-18：开局应带「至少 3 块」建议块（用户拍板 AI 3–4 块）
    assert "[开局建议]" in user, (
        f"开局（0 地块 + 现金 1200）必须带 [开局建议] 块：{user}"
    )
    assert "3 块" in user, f"[开局建议] 要落到「至少 3 块」：{user}"


def test_state_context_keeps_original_seven_keys():
    """★ 不许倒退：原来那 7 个局面键**一个都不能丢**。

    拓宽是**加**东西，不是把这七项换成别的 —— 否则会出现
    "加了价格表反而把现金丢了"的倒退。逐个键点名断言。
    """
    user, _ = _grab_user_message()
    for token in (
        "quarter=1/20",      # quarter + total_quarters 一起
        "cash=2400",         # cash（含 Q1 季初补贴 1200）
        "happiness=0.6",     # happiness
        "population=null",   # population（开局未提交 → null + 说明）
        "phase=collecting",  # phase
        "can_settle=False",  # can_settle
    ):
        assert token in user, f"★ 原有局面键丢了：缺 {token!r}：{user}"
    # population=null 必须带"为什么"—— 不许让 AI 以为"这局没有人口"
    assert "没提交计划" in user, (
        f"population=null 必须点破是『没提交计划』，不许让 AI 误读：{user}"
    )


def test_state_context_does_not_fabricate_missing_fields():
    """★ 不编数：局面里**没有的**字段不许凭空出现在 user message 里。

    开局没提交计划 → `current_price`/`population` 都是 None。
    AI 必须读到 null，而不是一个编出来的数字；同时地块区块不许
    塞一块"幽灵地"进去。
    """
    user, _ = _grab_user_message()
    # 本季求价没试算 → 必须给 null / 未试算，而不是编一个价
    assert "本季null" in user, (
        f"没试算时本季求价必须给 null（不编数字），实得：{user}"
    )
    # 开局没有地块 —— 不许凭空地塞一行像真地的东西（如 "0,0|agriculture"）
    state_body = user.split("[规则摘要")[0]
    assert "0,0|" not in state_body, (
        f"开局没有地块，局面里不许凭空出现地块行：{state_body}"
    )


def test_state_body_limit_enforced_even_with_many_goods():
    """★ 体积闸真到极限时会**砍次要项**，不会无限增长。

    构造一个 market 里塞了非常多条目的局面 → 压缩结果仍 < 上限，
    且核心七键还在（砍的是价格/建造这些次要项，不是核心）。
    直接调 `_compact_state`，起一个超大的假 market。
    """
    huge_market = {
        f"G{i:03d}": {
            "name": f"商品{i}", "P0": 10.0, "last_price": float(i),
            "current_price": float(i) + 0.5, "P_min": 1.0, "at_floor": False,
        }
        for i in range(500)  # 500 种商品，远超真局的 15
    }
    fake_state = {
        "quarter": 1, "total_quarters": 20, "phase": "collecting",
        "can_settle": False, "cash": 1200.0, "happiness": 0.6,
        "population": None, "market": huge_market, "plots": [],
    }
    body = ai_host._compact_state("p1", fake_state, None)
    assert len(body) <= ai_host._STATE_BODY_LIMIT, (
        f"★ 500 种商品时局面压缩后 {len(body)} 字符超上限 "
        f"{ai_host._STATE_BODY_LIMIT} —— 体积闸失效了"
    )
    # 砍完核心七键必须还在（它们永远满额，体积再紧也不砍）
    assert "cash=1200" in body and "quarter=1/20" in body, (
        f"砍次要项时不许把核心键砍了：{body[:300]}..."
    )


# ══════════════════════════════════════════════════════════════════
# 七、★ draft=True：让 AI 真能「起草一份计划」（任务 A，2026-10-17）
# ══════════════════════════════════════════════════════════════════
#
# 背景（用户原话）：「我现在可不可以把我的想法直接发给界面中的 AI，
# 让他起草方案并且填入，等待进入结算了」——**不行**：AI 只能给建议，
# suggested_plan 只有当调用方预先塞一份 preview_plan 给它试算时才存在。
# 这五条钉死新行为：
#
#   · 模型回一段**合法** Plan JSON → suggested_plan 非空、plan_source='model'；
#   · 模型回**代码围栏**包着的 JSON → 仍能取出；
#   · 模型回**非法计划** → suggested_plan is None 且 plan_issues 非空;
#   · 模型回**一堆废话** → 明确失败（不编）;
#   · **默认路径回归**：draft 不传时输出与旧行为一致；
#   · **绝不 submit**：跑完 advise 后 session.plans 仍空、phase 未变。
# ══════════════════════════════════════════════════════════════════


def _valid_draft_json(player_id: str = "p1") -> dict:
    """一份能过校验的 Plan JSON —— 开局现金 1200、可建选项里农业 L1 80 是锚点。

    ⚠️ 不用任何开局不存在的地块 / 产物；农业 L1 + product=A 是 config 里的合法组合。
    """
    return {
        "player_id": player_id,
        "plot_plans": {},
        "purchases": {},
        "food": {},
        "builds": [
            {
                "plot_id": "0,0",
                "plot_type": "agriculture",
                "target_level": 1,
                "product": "A",
            }
        ],
    }


def test_draft_valid_json_yields_suggested_plan():
    """模型回一段**合法 Plan JSON** → ok、`suggested_plan` 非空、`plan_source='model'`。"""
    app = _fresh_app()
    session = _fresh_session(app)
    plan = _valid_draft_json()
    fake = FakeClient(result=llm_client.LLMResult(
        ok=True, model="fake-kimi", content=json.dumps(plan, ensure_ascii=False),
    ))
    out = ai_host.advise(session, "p1", "帮我起草一份计划", client=fake, draft=True)
    assert out["ok"] is True, f"合法起草应成功：{out}"
    assert out["suggested_plan"] is not None, "suggested_plan 不应为 None"
    assert out["plan_source"] == "model", (
        f"plan_source 必须是 'model'（模型起草的），实得 {out['plan_source']!r}"
    )
    assert not out.get("plan_issues"), "合法计划不该带 issues"
    # 形状与 /api/submit 同形 —— builds 那一条里的字段要原样回来
    first = out["suggested_plan"]["builds"][0]
    assert first["plot_id"] == "0,0"
    assert first["plot_type"] == "agriculture"
    assert first["target_level"] == 1
    assert first["product"] == "A"
    # player_id 必须被钉死成玩家自己的（模型乱给也得纠）
    assert out["suggested_plan"]["player_id"] == "p1"
    # 旧行为的 next_step（AI 无 commit）也得在
    assert out.get("next_step"), "AI 无 commit 的提示字句不能丢"


def test_draft_fenced_json_is_parsed():
    """模型回**代码围栏**包着的 JSON → 仍能取出（parse_model_json 的剥围栏能力复用）。"""
    app = _fresh_app()
    session = _fresh_session(app)
    plan = _valid_draft_json()
    fenced = f"这是草稿：\n```json\n{json.dumps(plan, ensure_ascii=False)}\n```\n希望对你有帮助。"
    fake = FakeClient(result=llm_client.LLMResult(
        ok=True, model="fake-kimi", content=fenced,
    ))
    out = ai_host.advise(session, "p1", client=fake, draft=True)
    assert out["ok"] is True, f"围栏包着的 JSON 也该能解析：{out}"
    assert out["suggested_plan"] is not None
    assert out["plan_source"] == "model"
    assert out["suggested_plan"]["builds"][0]["plot_id"] == "0,0"


def test_draft_invalid_plan_is_repaired_or_rejected_not_leaked():
    """坏项（不存在的 plot_plan）→ 编译器丢掉它并留 notes；**到达前端的计划必须合法**。

    ⚠️ 2026-10-18 行为变更：旧实现把模型输出直接丢给 `validate`，只要有一处
       坏项整份草案就作废（真跑实测：模型把已有地块写进 builds → 3 条
       `build_not_an_upgrade` → `suggested_plan=null`）。现在先过编译器：
       能定位的坏项被丢掉、能归位的意图被归位；**返回的 plan 必须过 validate**。
       如果修不好，仍然 `suggested_plan=None` + `plan_issues`——两条出口都守住
       「绝不把坏计划交给前端」。
    """
    app = _fresh_app()
    session = _fresh_session(app)
    bad_plan = _valid_draft_json()
    # ★ 必须是 plot_plans 里的地块 id 才会被 `_check_plot_plans` 抓
    #   （builds 不在这一项的检查范围，开局还没地，所以"99,99"必 unknown_plot）。
    bad_plan["plot_plans"] = {
        "99,99": {"plot_id": "99,99", "active": True, "u": 1.0}
    }
    fake = FakeClient(result=llm_client.LLMResult(
        ok=True, model="fake-kimi", content=json.dumps(bad_plan, ensure_ascii=False),
    ))
    out = ai_host.advise(session, "p1", client=fake, draft=True)
    assert out["ok"] is True
    # 两条合法出口：要么明确失败（plan=None + issues），要么修复后交付合法计划。
    if out["suggested_plan"] is None:
        assert out.get("plan_issues"), (
            "修不好时必须带上 issues（前端要能告诉玩家\"哪不行\"）"
        )
        assert out["plan_source"] == "model", "模型起草的它仍然是 model，只是没过校验"
        codes = [i.get("code", "") for i in out["plan_issues"]]
        assert codes and all(codes), f"issues 里要有 code（五要素之错误类型）：{codes}"
    else:
        # 修复出口：99,99 被丢掉，0,0 的合法建造保留 → 必须过 validate
        from engine.models import Plan
        from engine import validate as engine_validate

        plan = Plan.from_dict(out["suggested_plan"])
        issues = engine_validate.validate_plan(
            plan, session.state.player("p1"), session.cfg, "p1"
        )
        assert not issues, (
            f"★ 到达前端的计划必须合法，实得 issues={[i.to_dict() for i in issues]}"
        )
        assert "99,99" not in plan.plot_plans, "不存在的 plot_plan 应被丢掉"
        assert plan.builds, "合法建造不该被连累"
        assert any("99,99" in n for n in (out.get("plan_notes") or [])), (
            f"必须留下\"99,99 被丢弃\"的校正说明：{out.get('plan_notes')}"
        )


def test_draft_garbage_is_clear_failure_not_fabricated():
    """模型回**一堆废话**（根本不含 JSON）→ 明确失败，**不编**一份假计划。"""
    app = _fresh_app()
    session = _fresh_session(app)
    fake = FakeClient(result=llm_client.LLMResult(
        ok=True, model="fake-kimi",
        content="我觉得你这季应该先攒钱，然后再看情况……（没有任何 JSON）",
    ))
    out = ai_host.advise(session, "p1", "起个草", client=fake, draft=True)
    assert out["ok"] is True
    assert out["suggested_plan"] is None, "没给到 JSON 时不许编一份 suggested_plan"
    # 明确失败的一句话必须能看懂（不许闷声不响）
    assert "起草" in out["summary"] or "放弃" in out["summary"], (
        f"summary 要让人知道\"这次起草被放弃了\"：{out['summary']!r}"
    )
    assert not out.get("plan_issues"), "没给出计划时不该带 issues（这是\"没拿到\"，不是\"被拒\"）"


def test_draft_default_false_unchanged_regression():
    """★ 默认路径回归：`draft` 不传时，输出与旧行为一致（一个字段不许变）。"""
    app = _fresh_app()
    session = _fresh_session(app)
    fake = FakeClient()  # 默认回固定文本
    out = ai_host.advise(session, "p1", "我该先建什么？", client=fake)
    assert out["ok"] is True
    # 旧字段原样
    assert out["summary"] == "【假客户端固定建议】先保粮食，再扩产。"
    assert out["suggested_plan"] is None
    # 新字段的默认值也要钉死（None / None / None 三元组）
    assert out.get("plan_source") is None
    assert out.get("plan_issues") is None
    assert out["model"] == "fake-kimi"
    # 传给模型的 user message 里**不该**出现"起草"指令
    user = fake.calls[0][1]["content"]
    assert "【起草一份计划" not in user and "draft" not in user.lower(), (
        f"draft 不传时 messages 里不该出现起草指令：{user}"
    )


def test_draft_never_submits_session_unchanged():
    """★ 绝不 submit：draft 跑完后 `plans` 仍空、`phase` 未变（铁律的落点）。"""
    app = _fresh_app()
    session = _fresh_session(app)
    plan = _valid_draft_json()
    fake = FakeClient(result=llm_client.LLMResult(
        ok=True, model="fake-kimi", content=json.dumps(plan, ensure_ascii=False),
    ))
    before_plans = copy.deepcopy(session.plans)
    before_phase = session.phase
    before_state = json.dumps(
        session.state_for("p1"), ensure_ascii=False, sort_keys=True, default=str
    )

    out = ai_host.advise(session, "p1", client=fake, draft=True)
    assert out["ok"] is True and out["suggested_plan"] is not None

    # ★ 核心断言：AI 只产出数据，**绝不提交**
    assert session.plans == before_plans, "★ plans 被动了（绝不 submit）"
    assert session.phase == before_phase, "★ phase 被动了（绝不 commit）"
    after_state = json.dumps(
        session.state_for("p1"), ensure_ascii=False, sort_keys=True, default=str
    )
    assert after_state == before_state, "★ 只读被破坏：state_for 不一致"


def test_route_advise_passes_draft_through():
    """路由层：`draft: true` 必须**原样透传**给 ai_host（与 preview_plan 同规矩）。"""
    app = _fresh_app()
    app.registry.create("g1", "p1")

    import server.app as app_mod
    orig = app_mod.ai_host.advise
    captured: dict = {}

    def spy(session, player_id="p1", question=None, **kw):
        captured.clear()
        captured.update({"player_id": player_id, "question": question, **kw})
        return {"ok": True, "summary": "假建议", "suggested_plan": None,
                "plan_source": None, "plan_issues": None,
                "evidence": [], "model": "fake", "error": ""}

    app_mod.ai_host.advise = spy
    try:
        # ① 传 draft: true
        code, body = dispatch(app, "POST", "/api/ai/advise", {},
                              {"game": "g1", "player": "p1", "draft": True})
        assert code == 200 and body["ok"] is True
        assert captured.get("draft") is True, (
            f"draft=true 不透传：收到 {captured.get('draft')!r}"
        )
        # ② 不传 draft → False（与旧行为一致）
        captured.clear()
        dispatch(app, "POST", "/api/ai/advise", {}, {"game": "g1", "player": "p1"})
        assert captured.get("draft") is False, (
            f"不传 draft 时应收到 False，实得 {captured.get('draft')!r}"
        )
    finally:
        app_mod.ai_host.advise = orig


# ══════════════════════════════════════════════════════════════════
# 八、★ current_plan 编辑 + 机制问答查规则（2026-10-18）
# ══════════════════════════════════════════════════════════════════
#
# 依据 `docs/03-架构/AI交互与建设撤销.md`：
#   · 玩家说「取消 0,0 那条」→ 只删 0,0，**其它安排保留**（不许重填整份）；
#   · 编辑模式**不许**默认全开工 / 补口粮（那是替玩家做经营决策）；
#   · 问机制（`draft=False` / `mode=answer`）→ user message 带 `[规则摘录]`，
#     问「粮食怎么算」时模型手里有 §3.3 原文；
#   · `mode` 由服务层翻成 `draft`（answer→False、plan→True）。


def _mid_session(cash: float = 3000.0):
    """仿 `saves/1.json` 的第 4 季局面（5 块运行地块）—— 测编辑模式。

    `advise` 只读 `session.state` / `session.cfg`，所以直接造 `GameSession`
    就够（不必走 registry）。现金给足，免得编译器「缩量」把要断言的建造
    自己删掉（那是另一个测试的事）。
    """
    from engine.models import GameState, PlotType, PlayerState
    from server.session import GameSession
    from tests.test_m1 import mature

    cfg = load_config()
    plots = {
        "4,5": mature("4,5", PlotType.RESOURCE, 1),
        "5,3": mature("5,3", PlotType.SERVICE, 1),
        "5,4": mature("5,4", PlotType.AGRICULTURE, 1, product="A"),
        "6,3": mature("6,3", PlotType.RESOURCE, 1),
        "6,4": mature("6,4", PlotType.INDUSTRY, 1, product="G"),
    }
    player = PlayerState(
        player_id="p1", cash=cash, plots=plots, last_happiness=0.8
    )
    state = GameState(world=cfg.new_world(), players={"p1": player})
    return GameSession(state=state, cfg=cfg, player_ids=["p1"])


def _build(plot_id: str, *, product="A") -> dict:
    return {"plot_id": plot_id, "plot_type": "agriculture",
            "target_level": 1, "product": product}


def _plot_plan(plot_id: str, u: float = 1.0) -> dict:
    return {"plot_id": plot_id, "active": True, "u": u}


def _current_plan(builds=None, plot_plans=None) -> dict:
    """前端 `publicPlan()` 同形的当前草稿。"""
    return {
        "player_id": "p1",
        "plot_plans": dict(plot_plans or {}),
        "purchases": {},
        "food": {},
        "builds": list(builds or []),
        "contracts": [],
    }


def _mid_current_plan(builds=None, plot_plans=None) -> dict:
    """mid 局的当前草稿：默认 5 块地全开工（玩家手填的样子）。"""
    if plot_plans is None:
        plot_plans = {
            pid: _plot_plan(pid) for pid in ("4,5", "5,3", "5,4", "6,3", "6,4")
        }
    return _current_plan(builds=builds, plot_plans=plot_plans)


def test_current_plan_remove_builds_keeps_other_build():
    """明确“取消 0,0” → 确定性意图解析只删 0,0，另一条保留；
    `plan_action == "removed"`、摘要/why 说明撤销了哪条。"""
    session = _mid_session()
    current = _mid_current_plan(builds=[_build("0,0"), _build("0,1")])
    why = "撤销 0,0 那条，其余安排不动。"
    fake = FakeClient(result=llm_client.LLMResult(
        ok=True, model="fake-kimi",
        content=json.dumps(
            {"why": why, "remove_builds": ["0,0"]}, ensure_ascii=False
        ),
    ))
    out = ai_host.advise(
        session, "p1", "取消 0,0 那条建造", client=fake,
        draft=True, current_plan=current,
    )
    assert out["ok"] is True, out
    assert out["suggested_plan"] is not None, (
        f"撤销后应还编译出一份能跑的计划：summary={out.get('summary')!r}、"
        f"issues={out.get('plan_issues')}、notes={out.get('plan_notes')}"
    )
    build_ids = [b["plot_id"] for b in out["suggested_plan"]["builds"]]
    assert len(build_ids) == 1, f"只剩 1 条，实得 {build_ids}"
    assert "0,0" not in build_ids, "被撤销的那条不该回来"
    # ★ 动作与摘要
    assert out["plan_action"] == "removed", (
        f"plan_action 应为 removed，实得 {out['plan_action']!r}"
    )
    assert "撤销" in out["summary"], f"summary 应说明撤销：{out['summary']!r}"
    assert "撤销" in out["why"] and "0,0" in out["why"], (
        f"why 要说明撤销了哪条：{out.get('why')!r}"
    )
    # ★ 只读：跑完仍是"什么都没发生"
    assert session.plans == {}, "advise 不许提交（plans 必须还是空的）"


def test_current_plan_edit_does_not_default_run_all():
    """编辑模式下 `plot_plans` 未列出的地块**保持不开工**（不默认全开工）。

    mid 局有 5 块运行地块；草稿里只写了 2 块 → 编译结果就该只有这 2 块，
    绝不许因为"模型没提"把 4,5 / 5,4 / 6,3 也默认开工。
    """
    session = _mid_session()
    current = _mid_current_plan(plot_plans={
        "5,3": _plot_plan("5,3"),
        "6,4": _plot_plan("6,4"),
    })
    fake = FakeClient(result=llm_client.LLMResult(
        ok=True, model="fake-kimi",
        content=json.dumps(
            {"why": "只开 5,3 和 6,4",
             "plot_plans": {"5,3": _plot_plan("5,3")}},
            ensure_ascii=False,
        ),
    ))
    out = ai_host.advise(
        session, "p1", "只开这两块", client=fake,
        draft=True, current_plan=current,
    )
    assert out["suggested_plan"] is not None, (
        f"应编译成功：summary={out.get('summary')!r}、"
        f"issues={out.get('plan_issues')}"
    )
    plan = out["suggested_plan"]
    assert set(plan["plot_plans"]) == {"5,3", "6,4"}, (
        f"未列出的地块必须保持「不开工」，实得 {sorted(plan['plot_plans'])}"
    )
    for pid in ("4,5", "5,4", "6,3"):
        assert pid not in plan["plot_plans"], (
            f"★ {pid} 没被任何一方提到，不许默认全开工"
        )
    # ★ 2026-10-18 用户反馈：AI 自动化不能忘口粮。
    #   编辑模式也应补基准档 A+B（否则玩家只开一部分地也会饿肚子）。
    assert plan["food"].get("A", 0) > 0 and plan["food"].get("B", 0) > 0, (
        f"编辑模式必须补基准口粮 A+B：{plan['food']}"
    )


def test_current_plan_is_shown_to_model_in_edit_mode():
    """`current_plan` 必须进 user message（否则模型无法"只删 0,0、保留其余"）。"""
    session = _mid_session()
    current = _mid_current_plan(builds=[_build("0,0")])
    fake = FakeClient(result=llm_client.LLMResult(
        ok=True, model="fake-kimi",
        content=json.dumps({"why": "保持不动"}, ensure_ascii=False),
    ))
    ai_host.advise(session, "p1", "先别动", client=fake,
                   draft=True, current_plan=current)
    assert fake.calls, "假客户端应被调过一次"
    user = fake.calls[0][1]["content"]
    assert "[当前草稿" in user, f"draft 指令要求看草稿，但 messages 里没有：{user}"
    assert "0,0" in user, "草稿里的建造必须给模型看到"
    assert "没提到" in user or "只输出改动" in user, (
        f"要给「只输出改动」的硬要求：{user}"
    )


def test_draft_instruction_mentions_opening_3_to_4_plots():
    """★ 2026-10-18 用户拍板：AI 开局不能只给 2 块。起草指令必须告诉模型
    「开局优先 3–4 块（至少 1 农 + 1 资 + 1 工）、不要只给 2 块、
    仍必须 ≤ 现金、引擎会缩量」—— 否则模型默认铺 2 块就跑不动。
    """
    app = _fresh_app()
    session = _fresh_session(app)
    fake = FakeClient(result=llm_client.LLMResult(
        ok=True, model="fake-kimi",
        content=json.dumps(_valid_draft_json(), ensure_ascii=False),
    ))
    ai_host.advise(session, "p1", "帮我起草开局", client=fake, draft=True)
    user = fake.calls[0][1]["content"]
    # ★ 必须含「3–4 块」（用户拍板的原话； en-dash）
    assert "3–4 块" in user, (
        f"起草指令缺「3–4 块」开局块数提示：\n{user[-1500:]}"
    )
    # ★ 必须含「不要只给 2 块」（对应真跑故障）与「至少 1 农+1 资+1 工」
    assert "不要只给 2 块" in user, f"起草指令缺「不要只给 2 块」：\n{user[-1500:]}"
    assert "1 农业" in user and "1 资源" in user and "1 工业" in user, (
        f"起草指令缺开局类型组合（农+资+工）：\n{user[-1500:]}"
    )
    # ★ 数字仍须守真：≤ 现金、引擎会缩量
    assert "≤ 现金" in user or "≤现金" in user, (
        f"起草指令缺「总成本 ≤ 现金」约束：\n{user[-1500:]}"
    )
    assert "缩量" in user, f"起草指令缺「引擎会缩量」口径：\n{user[-1500:]}"


def test_draft_instruction_mentions_why_and_remove_builds():
    """起草指令必须告诉模型两个可选字段 —— 否则它永远不会用（2026-10-18）。"""
    app = _fresh_app()
    session = _fresh_session(app)
    fake = FakeClient(result=llm_client.LLMResult(
        ok=True, model="fake-kimi",
        content=json.dumps(_valid_draft_json(), ensure_ascii=False),
    ))
    ai_host.advise(session, "p1", "帮我起草", client=fake, draft=True)
    user = fake.calls[0][1]["content"]
    assert "remove_builds" in user, f"起草指令缺 remove_builds：{user[-600:]}"
    assert "why" in user, f"起草指令缺 why：{user[-600:]}"
    assert "只输出一个 JSON" in user or "只输出**一个" in user, (
        "仍须要求只输出 JSON"
    )


def test_wants_baseline_only_keyword_detection():
    """★ 2026-10-18 AI-food-default-excellent：意图反转 —— 默认就给优秀档，
    只有玩家明说「只要基准档 / 吃饱就行 / 不用优秀」才退回基准档。
    `_wants_baseline_only` 必须确定性命中 / 不命中，不交给模型猜。"""
    # 肯定：明说只要基准
    assert ai_host._wants_baseline_only("只要基准档就行") is True
    assert ai_host._wants_baseline_only("吃饱就行，别乱花钱") is True
    assert ai_host._wants_baseline_only("不用优秀，吃饱就好") is True
    # 否定：想吃好 / 中性 / 空 / None 都不应触发"退回基准"
    assert ai_host._wants_baseline_only("想给居民吃好一点") is False
    assert ai_host._wants_baseline_only("想冲优质口粮") is False
    assert ai_host._wants_baseline_only("帮我起草本季计划") is False
    assert ai_host._wants_baseline_only(None) is False
    assert ai_host._wants_baseline_only("") is False


def test_advise_draft_with_excellent_word_fills_good_tier():
    """★ 2026-10-18：问题里带「优秀」时，advise(draft=True) 应让编译器
    把 A/B/C/D 四样口粮补到优秀档；从最终返回的 `suggested_plan.food` 看，
    C/D 必须非零（基准档根本不填 C/D）。
    """
    session = _mid_session(cash=10000.0)   # 中局 + 足现金，避免缩量把口粮砍掉
    current = _mid_current_plan()
    fake = FakeClient(result=llm_client.LLMResult(
        ok=True, model="fake-kimi",
        content=json.dumps({"why": "按优秀档补口粮。"}, ensure_ascii=False),
    ))
    out = ai_host.advise(
        session, "p1", "想给居民吃优秀档的口粮",
        client=fake, draft=True, current_plan=current,
    )
    assert out["ok"] is True, out
    plan = out["suggested_plan"]
    assert plan is not None, f"应编译成功：issues={out.get('plan_issues')}"
    assert plan["food"].get("C", 0) > 0, (
        f"带「优秀」意图时 C 必须补到优秀档：{plan['food']}"
    )
    assert plan["food"].get("D", 0) > 0, (
        f"带「优秀」意图时 D 必须补到优秀档：{plan['food']}"
    )
    assert any("优秀档" in n for n in (out.get("plan_notes") or [])), (
        f"plan_notes 必须含「优秀档」说明：{out.get('plan_notes')}"
    )


def test_advise_draft_default_fills_good_tier():
    """★ 2026-10-18 AI-food-default-excellent：玩家问题里**不含**
    "优秀/吃好/优质"等词时，编译器**默认**就给优秀档 —— C/D 必须非零。
    这才能证明默认路径通了（旧的「不带关键词 → 基准档」已作废）。"""
    session = _mid_session(cash=10000.0)
    current = _mid_current_plan()
    fake = FakeClient(result=llm_client.LLMResult(
        ok=True, model="fake-kimi",
        content=json.dumps({"why": "默认就按优秀档补。"}, ensure_ascii=False),
    ))
    out = ai_host.advise(
        session, "p1", "帮我起草本季计划",
        client=fake, draft=True, current_plan=current,
    )
    assert out["ok"] is True
    plan = out["suggested_plan"]
    assert plan is not None
    assert plan["food"].get("C", 0) > 0, (
        f"默认（不带关键词）时 C 必须补到优秀档：{plan['food']}"
    )
    assert plan["food"].get("D", 0) > 0, (
        f"默认（不带关键词）时 D 必须补到优秀档：{plan['food']}"
    )
    # A/B 也应到优秀档（只补不足）
    assert plan["food"].get("A", 0) > 0 and plan["food"].get("B", 0) > 0


def test_advise_draft_baseline_word_skips_good_tier():
    """★ 2026-10-18 AI-food-default-excellent：玩家**明说**「只要基准档」
    时，编译器退回基准档 —— C/D 不得进 suggested_plan.food，A/B 仍补。"""
    session = _mid_session(cash=10000.0)
    current = _mid_current_plan()
    fake = FakeClient(result=llm_client.LLMResult(
        ok=True, model="fake-kimi",
        content=json.dumps({"why": "玩家只要基准档。"}, ensure_ascii=False),
    ))
    out = ai_host.advise(
        session, "p1", "只要基准档就行",
        client=fake, draft=True, current_plan=current,
    )
    assert out["ok"] is True
    plan = out["suggested_plan"]
    assert plan is not None
    assert "C" not in plan["food"] and "D" not in plan["food"], (
        f"明说「只要基准档」时不许加 C/D：{plan['food']}"
    )
    # A/B 仍应补到基准档
    assert plan["food"].get("A", 0) > 0 and plan["food"].get("B", 0) > 0


def test_draft_instruction_explains_excellent_and_budget():
    """★ 起草指令必须明写「优秀档四样」+「只填 A/B 仅合格」+「现金要花出去」。"""
    app = _fresh_app()
    session = _fresh_session(app)
    fake = FakeClient(result=llm_client.LLMResult(
        ok=True, model="fake-kimi",
        content=json.dumps(_valid_draft_json(), ensure_ascii=False),
    ))
    ai_host.advise(session, "p1", "帮我起草", client=fake, draft=True)
    user = fake.calls[0][1]["content"]
    assert "优秀档" in user, f"起草指令缺优秀档说明：{user[-800:]}"
    assert "四样" in user or "A/B/C/D" in user, (
        f"起草指令缺「优秀档四样」：{user[-800:]}"
    )
    assert "合格" in user, f"起草指令缺「只 A/B 仅合格」：{user[-800:]}"


def test_system_preamble_mentions_excellent_tier_and_spending_budget():
    """★ system preamble 明写：① 合格档 A+B / 优秀档四样；
    ② 现金充裕时优先扩建/升级/买优秀口粮。"""
    app = _fresh_app()
    session = _fresh_session(app)
    fake = FakeClient()
    ai_host.advise(session, "p1", client=fake)
    system = fake.calls[0][0]["content"]
    assert "优秀档" in system, f"system 缺优秀档说明：{system}"
    assert "A/B/C/D" in system, f"system 缺优秀档四样：{system}"
    assert "合格" in system, f"system 缺「只 A/B 仅合格」：{system}"
    assert "扩建" in system or "升级" in system, (
        f"system 缺「现金要花出去」：{system}"
    )
    assert "缩量" in system, f"system 缺「超预算由引擎缩量」：{system}"


def test_answer_mode_sends_rule_excerpts_for_mechanism_questions():
    """问「粮食怎么算」（draft=False）→ user message 带 `[规则摘录]` 与 §3.3 原文。"""
    app = _fresh_app()
    session = _fresh_session(app)
    fake = FakeClient()
    out = ai_host.advise(session, "p1", "粮食怎么算", client=fake, draft=False)
    assert out["ok"] is True
    user = fake.calls[0][1]["content"]
    assert "[规则摘录]" in user, f"机制问答缺规则摘录块：{user}"
    assert "§3.3" in user, f"「粮食」应命中 §3.3：{user}"
    # 只要 1–3 节，不要整本基线
    assert user.count("[规则摘录 · §") <= 3, "规则摘录最多 3 节"
    assert "人口" in user or "粮食" in user, "摘录应是 §3.3 的原文（不是空壳）"


def test_draft_mode_does_not_send_rule_excerpts():
    """起草（draft=True）**不**附规则摘录 —— 保持旧行为（局面＋规则摘要就够）。"""
    app = _fresh_app()
    session = _fresh_session(app)
    fake = FakeClient(result=llm_client.LLMResult(
        ok=True, model="fake-kimi",
        content=json.dumps(_valid_draft_json(), ensure_ascii=False),
    ))
    ai_host.advise(session, "p1", "粮食怎么算", client=fake, draft=True)
    user = fake.calls[0][1]["content"]
    assert "[规则摘录]" not in user, f"draft 模式不该带规则摘录：{user}"


def test_route_mode_forces_draft_and_passes_current_plan():
    """路由：`mode=answer`→draft=False、`mode=plan`→draft=True、
    省略 mode→沿用 body.draft；`current_plan` 原样透传。"""
    app = _fresh_app()
    app.registry.create("g1", "p1")

    import server.app as app_mod
    orig = app_mod.ai_host.advise
    captured: dict = {}

    def spy(session, player_id="p1", question=None, **kw):
        captured.clear()
        captured.update({"question": question, **kw})
        return {"ok": True, "summary": "假建议", "suggested_plan": None,
                "plan_source": None, "plan_issues": None, "evidence": [],
                "model": "fake", "error": ""}

    plan = {"player_id": "p1", "builds": [_build("0,0")]}
    app_mod.ai_host.advise = spy
    try:
        dispatch(app, "POST", "/api/ai/advise", {},
                 {"game": "g1", "player": "p1", "question": "粮食怎么算",
                  "mode": "answer", "draft": True})
        assert captured["draft"] is False, "mode=answer 必须压成 draft=False"
        assert captured["mode"] == "answer"

        dispatch(app, "POST", "/api/ai/advise", {},
                 {"game": "g1", "player": "p1", "question": "帮我建一块",
                  "mode": "plan", "draft": False})
        assert captured["draft"] is True, "mode=plan 必须抬成 draft=True"

        dispatch(app, "POST", "/api/ai/advise", {},
                 {"game": "g1", "player": "p1", "draft": True})
        assert captured["draft"] is True, "省略 mode 时沿用 body.draft"
        assert captured["mode"] == "auto"

        dispatch(app, "POST", "/api/ai/advise", {},
                 {"game": "g1", "player": "p1", "mode": "plan",
                  "current_plan": plan})
        assert captured["current_plan"] == plan, "current_plan 必须原样透传"
    finally:
        app_mod.ai_host.advise = orig


# ══════════════════════════════════════════════════════════════════
# 九、★ 口粮绝对量（2026-10-18 D3）
# ══════════════════════════════════════════════════════════════════
#
# 用户原话：「AI 还是不会填粮食。**比例对了，但没法读取优秀需要多少
# 进行准确填写。**」
#   · answer 分支原来只回 `A×0.4F_优秀…`（比例），没有本局的量；
#   · 起草成功的 summary 只说「已起草：N 条建造…」，从不提口粮。
# 三条验收：
#   ① answer 分支在配比之后必须给**本局绝对量**（且量真按「配比 × F」算）；
#   ② 人口 0 时明说「没有吃饭人口，本季口粮需求为 0」，不许套公式假装有量；
#   ③ 起草成功的 summary 必须报口粮 + H（H 取试算真值，不硬编码档位）。


def _pure_food_answer(session) -> str:
    """问「粮食怎么算」→ 取 ai_host 兜底（规则原文）出来的答案文本。

    FakeClient 的固定建议里没有 `F_基准` / `0.4` → `_mentions_rule_terms`
    判为答偏 → 走 `_rule_answer_text`（不起真网关、不联网）。
    """
    fake = FakeClient()
    out = ai_host.advise(session, "p1", "粮食怎么算", client=fake, draft=False)
    assert out["ok"] is True, out
    return out["summary"]


def test_rule_answer_text_includes_current_absolute_food_amounts():
    """★ D3-①：问口粮「怎么算」时，答案必须带**本局绝对量**，不能只有比例。

    断言两件事：
      ① 文本里有「当前局面」与「优秀档本季需」和四样具体数字；
      ② 数字**真按 `配比 × F_优秀` 算**（拿答案里写的 F_优秀 反推校验），
         防止把 `0.4` / `0.1` 这类比例当成数量抄出来。
    """
    import re

    session = _mid_session()   # 5 块运行地块 → 人口 > 0，才有绝对量可说
    summary = _pure_food_answer(session)

    assert "当前局面" in summary, f"answer 分支缺当期数字行：{summary}"
    assert "优秀档本季需" in summary, f"answer 分支缺绝对量：{summary}"
    # 比例公式仍在（先讲清怎么算，再给本局的数）
    assert "A×0.4F" in summary, f"原配比公式被冲掉了：{summary}"

    # 只在「当前局面：…」那一段里取数（前面的公式里有 F_优秀=1.5×F_基准）
    at = summary.index("当前局面")
    current = summary[at:]
    m = re.search(r"F_优秀=([0-9.]+)", current)
    assert m, f"当期行缺 F_优秀：{summary}"
    good_food = float(m.group(1))
    amounts = {
        goods: float(qty)
        for goods, qty in re.findall(r"([ABCD]) ([0-9.]+)", current)
    }
    quota = load_config().food.good_quota or {}
    assert set(quota) <= set(amounts), (
        f"优秀档四样必须都给出：quota={quota}、实得 amounts={amounts}\n{summary}"
    )
    for goods, ratio in sorted(quota.items()):
        expect = float(ratio) * good_food
        assert abs(amounts[goods] - expect) <= 0.01, (
            f"{goods} 的量应为 {ratio}×F_优秀={expect:.3f}，答案里写的是 "
            f"{amounts[goods]}（{summary}）"
        )


def test_rule_answer_text_zero_population_says_no_diners():
    """★ D3-②：人口 0（开局空地）时明说「没有吃饭人口，本季口粮需求为 0」，
    **不许**给出四样数量（那会变成编数字）。"""
    app = _fresh_app()
    session = _fresh_session(app)   # 一块地都没有 → 人口 0
    summary = _pure_food_answer(session)

    assert "没有吃饭人口" in summary, f"人口 0 必须明说：{summary}"
    assert "需求为 0" in summary, f"人口 0 必须给出口粮需求 0：{summary}"
    assert "优秀档本季需" not in summary, (
        f"人口 0 不该报四样数量（否则是编的）：{summary}"
    )


def test_draft_summary_reports_food_quantities_and_happiness():
    """★ D3-③：起草成功后的 summary 必须说清口粮结论（数量 + H）。

    用假模型起草（问题里不带「优秀」关键词 → 编译器按默认优秀档补齐），
    断言 summary 含「口粮」、含 Plan.food 里各商品的**实际数量**、且带 H。
    """
    import re

    session = _mid_session(cash=10000.0)   # 足现金 → 优秀档配齐，不被缩量
    fake = FakeClient(result=llm_client.LLMResult(
        ok=True, model="fake-kimi",
        content=json.dumps({"why": "按优秀档补口粮。"}, ensure_ascii=False),
    ))
    out = ai_host.advise(
        session, "p1", "帮我起草本季计划",
        client=fake, draft=True, current_plan=_mid_current_plan(),
    )
    assert out["ok"] is True, out
    plan = out["suggested_plan"]
    assert plan is not None, f"应编译成功：issues={out.get('plan_issues')}"

    summary = out["summary"]
    assert "口粮" in summary, f"起草摘要没提口粮：{summary!r}"
    m = re.search(r"H=([0-9.]+)", summary)
    assert m, f"起草摘要缺 H 描述：{summary!r}"
    # 数量必须取自 Plan.food（实际填的那份），不是另一套推算
    food = plan["food"]
    for goods in ("A", "B", "C", "D"):
        assert food.get(goods, 0) > 0, f"足现金应补齐优秀档四样：{food}"
        token = f"{goods} {ai_host._qty_text(food[goods])}"
        assert token in summary, f"摘要缺 {token}：{summary!r}"
    h = float(m.group(1))
    assert 0.99 <= h <= 1.0, f"足现金时应到优秀档（H≈1.0），实得 {h}：{summary!r}"
    assert "优秀档配齐" in summary, f"应说明按优秀档配齐：{summary!r}"


def test_food_plan_line_follows_actual_h_not_hardcoded_tier():
    """★ D3-③ 的档位判定：三档措辞跟**实际 H** 走（不硬编码 0.6 / 1.0 文案），
    且 preview 里没有 H 时**只报数量、不编档位**。"""
    food = {"A": 2.64, "B": 1.32, "C": 0.66, "D": 0.132}

    full = ai_host._food_plan_line(food, {"food": {"happiness": 1.0, "good_met": True}})
    assert "优秀档配齐" in full and "H=1.0" in full, full
    assert "A 2.64" in full and "D 0.132" in full, full

    base = ai_host._food_plan_line(food, {"food": {"happiness": 0.6, "good_met": False}})
    assert "仅到基准档" in base and "H=0.6" in base and "现金约束" in base, base

    low = ai_host._food_plan_line(food, {"food": {"happiness": 0.42, "good_met": False}})
    assert "H=0.42" in low and "减少建造" in low, low
    assert "优秀档配齐" not in low, f"H=0.42 不该被说成配齐：{low}"

    # 找不到 H → 只报数量，不编档位（也不许出现假 H）
    unknown = ai_host._food_plan_line(food, None)
    assert "A 2.64" in unknown and "H=" not in unknown, unknown

    # 空 food（人口 0 / 没填）→ 不追加任何一句（不打扰）
    assert ai_host._food_plan_line({}, {"food": {"happiness": 1.0}}) == ""
    # 快照拿不到 → answer 分支宁可不给，也不编
    assert ai_host._current_food_line(None) == ""


# ══════════════════════════════════════════════════════════════════
# 十、★ 2026-10-18 第九轮：去攻略口吻 + 确定性留钱 + 服务位置理由
# ══════════════════════════════════════════════════════════════════
#
# 第八轮实测（`.scratch/ai_probe_round8.md` §3.4）四条：
#   ① 「帮我规划本季」直接回一份 Plan，玩家拿不到「先做什么 / 留多少」；
#   ② 玩家可见文本里直呼内部块名「按策略提示升级工业、建服务」；
#   ③ 留钱数字（800 / 2000 / 15000）是模型自己编的，不可复核；
#   ④ 「服务建在哪」走起草时摘要里没有位置也没有理由。
#
# 第九轮的口径：**数字由引擎给，称呼换成玩家词，规划必须先给三句**。

#: 内部提示块名 —— 玩家可见文本里一个都不许出现（`_scrub_player_terms` 清洗）。
_INTERNAL_TERMS = (
    "策略提示", "口粮规则", "推荐空地", "开局建议",
    "[可建]", "[可升级]", "[可开工]", "[在建]", "[人口/口粮]",
    "[现金/预算]", "[可行动作]",
)


def _assert_no_internal_terms(text: str, label: str) -> None:
    for term in _INTERNAL_TERMS:
        assert term not in str(text), f"{label} 出现内部块名「{term}」：{text!r}"


def test_planning_question_returns_text_outline_with_deterministic_reserve():
    """「帮我规划本季」→ 文字规划（不是 Plan），且留钱是**确定数字**。

    验收（第九轮）：
      · `suggested_plan` 为 None（answer 模式只解释，不落表）；
      · 摘要以「【本季规划】」开头，含「目标 / 先后 / 留钱」三句；
      · 留钱数字 = `ai/strategy.py` 算的「建议预留」（无上季财报 → 现金 × 15%）；
      · 四问都不许出现内部块名。
    """
    app = _fresh_app()
    session = _fresh_session(app)
    fake = FakeClient()

    out = ai_host.advise(session, "p1", "帮我规划本季", client=fake, draft=False)
    assert out["ok"] is True, out
    assert out["suggested_plan"] is None, (
        f"answer 模式不该直接给 Plan（玩家要的是规划文字）：{out['suggested_plan']}"
    )

    summary = out["summary"]
    assert summary.startswith("【本季规划】"), summary
    for head in ("目标：", "先后：", "留钱："):
        assert head in summary, f"规划稿缺「{head}」：{summary}"
    assert "建议预留" in summary, summary

    # 数字必须与引擎算的一致（开局无上季财报 → 现金 × 15%）
    reserve = ai_host.ai_strategy.reserve_cash(session.state, "p1", session.cfg)
    assert reserve["fallback"] is True
    shown_amount = ai_host.ai_strategy.fmt_num(reserve["amount"])
    assert f"≈{shown_amount}" in summary, (
        f"留钱数字必须是引擎算的 {reserve['amount']}：{summary}"
    )
    _assert_no_internal_terms(summary, "summary")

    # user message 里也要有这份底稿（模型才知道该沿用哪个数）
    user = fake.calls[0][1]["content"]
    assert "[经营参考 · 引擎算好的本季规划]" in user, user
    assert "建议预留" in user, user


def test_service_mechanism_question_answers_units_from_config():
    """「服务每邻加多少产出 / 每邻给多少现金」→ **确定性答案**（不调模型）。

    第八轮把「3 邻合计 24%」答成「每邻 24%」、第九轮又答成「每邻 15%」——
    两次都是单位错且朝夸大方向错。这里按 config 直接回：
    「1/2/3 级 = 每邻 +X%」+「合计要自己乘邻居数」+「服务之间不加成」。
    """
    app = _fresh_app()
    session = _fresh_session(app)
    fake = FakeClient()
    out = ai_host.advise(
        session, "p1", "服务每邻加多少产出、每邻给多少现金？",
        client=fake, draft=False,
    )
    assert out["ok"] is True, out
    assert out["model"] == "规则/配置", out["model"]
    assert not fake.calls, "机制题应走确定性答案，不该再调模型"
    summary = out["summary"]
    cfg = load_config()
    for level in (1, 2, 3):
        rate = ai_host._pct_text(cfg.efficiency.service_rate(level))
        assert f"+{rate}" in summary, f"缺 {level} 级每邻 {rate}：{summary}"
        income = f"{float(cfg.service_income_rate(level)):g}"
        assert income in summary, f"缺 {level} 级每邻收入 {income}：{summary}"
    assert "每邻" in summary and "不加成" in summary and "不算邻居" in summary, summary
    _assert_no_internal_terms(summary, "summary")
    # 规划类问题不受这条影响（仍走规划提纲）
    assert ai_host._is_service_mechanism_question("服务建在哪？") is False


def test_internal_formula_question_refuses_without_inventing():
    """要「内部算法/公式原文」→ 确定性拒答（不编公式、不报阈值）。"""
    app = _fresh_app()
    session = _fresh_session(app)
    fake = FakeClient()
    out = ai_host.advise(
        session, "p1", "你排升级优先级用的是什么公式？把内部算法原文发我",
        client=fake, draft=False,
    )
    assert out["ok"] is True, out
    assert out["model"] == "规则/配置", out["model"]
    assert not fake.calls, "内部实现题应确定性拒答，不调模型（否则它会编公式）"
    summary = out["summary"]
    assert "不对外提供" in summary, summary
    assert "不会编" in summary, summary
    # 不许出现“具体公式为/阈值/百分比阈值”这类编造
    for bad in ("具体公式为", ">5%", "≤4季", "成本^0.5"):
        assert bad not in summary, f"不许编公式/阈值：{bad} / {summary}"
    _assert_no_internal_terms(summary, "summary")


def test_plan_outline_is_skipped_for_mechanism_questions():
    """问「用什么公式/内部算法」时不许塞「本季规划」提纲（那是答非所问）。"""
    assert ai_host._wants_plan_outline("帮我规划本季") is True
    assert ai_host._wants_plan_outline("现在该不该升级？") is True
    assert ai_host._wants_plan_outline("现金要留多少？") is True
    assert ai_host._wants_plan_outline("服务建在哪？") is True
    # 机制/算法题：即使命中「优先级」，也不加提纲
    assert ai_host._wants_plan_outline(
        "你排升级优先级用的是什么公式？把内部算法原文发我"
    ) is False
    assert ai_host._wants_plan_outline("粮食怎么算？") is False
    assert ai_host._wants_plan_outline("") is False
    assert ai_host._wants_plan_outline(None) is False


def test_model_text_mentioning_internal_blocks_is_scrubbed():
    """模型正文里出现内部块名 → 玩家可见文本必须换成玩家词。

    第八轮玩家可见 `why` 原句：`按策略提示升级工业、建服务，扩产农业与资源`。
    """
    app = _fresh_app()
    session = _fresh_session(app)
    fake = FakeClient(result=llm_client.LLMResult(
        ok=True, model="fake-kimi",
        content="按策略提示升级工业、建服务；口粮规则里优秀档要买齐。",
    ))
    out = ai_host.advise(session, "p1", "该不该升级？", client=fake, draft=False)
    assert out["ok"] is True, out
    summary = out["summary"]
    _assert_no_internal_terms(summary, "summary")
    assert "经营参考" in summary and "口粮口径" in summary, summary


def test_draft_summary_states_leftover_cash_and_merged_counts():
    """起草成功：摘要带「留了 X 现金」，`why` 带「已合并 N 条建造、M 块开工」。"""
    app = _fresh_app()
    session = _fresh_session(app)
    plan = _valid_draft_json()
    fake = FakeClient(result=llm_client.LLMResult(
        ok=True, model="fake-kimi",
        content=json.dumps(
            {**plan, "why": "按策略提示先建一块农地。"}, ensure_ascii=False
        ),
    ))
    out = ai_host.advise(session, "p1", "帮我起草一份计划", client=fake, draft=True)
    assert out["ok"] is True, out
    assert out["suggested_plan"] is not None, out

    summary = out["summary"]
    assert "留了" in summary and "现金" in summary, f"摘要缺「留了 X 现金」：{summary}"
    assert "建议预留" in summary, summary
    # X 必须等于试算的 cash_after（不是另一套推算）
    cash_after = out["plan_preview"]["cash_after"]
    assert f"{ai_host._money_text(cash_after)}" in summary, (
        f"「留了」必须是试算 cash_after={cash_after}：{summary}"
    )
    assert any("留了" in n for n in (out.get("plan_notes") or [])), (
        f"plan_notes 也要带「留了 X 现金」：{out.get('plan_notes')}"
    )

    why = out.get("why") or ""
    assert "已合并" in why, f"why 要明确合并量：{why!r}"
    assert "1 条建造" in why and "0 块开工" in why, why
    _assert_no_internal_terms(summary, "summary")
    _assert_no_internal_terms(why, "why")


def test_service_question_in_plan_mode_reports_position_and_reason():
    """「服务建在哪」走 plan 分支：摘要必须带**位置 + 理由（含单位口径）**。

    第八轮实测 `A4ui_8550b` 只回一份 1 条建造的 Plan，摘要里没有位置也没有理由。
    这里造一个「3 块工业围着 5,5」的局面，让模型在 5,5 建服务。
    """
    from engine.models import GameState, PlayerState, PlotType
    from server.session import GameSession
    from tests.test_m1 import mature

    cfg = load_config()
    plots = {
        "4,4": mature("4,4", PlotType.INDUSTRY, 1, "G"),
        "4,5": mature("4,5", PlotType.INDUSTRY, 1, "G"),
        "5,4": mature("5,4", PlotType.INDUSTRY, 1, "G"),
    }
    player = PlayerState(player_id="p1", cash=5000.0, plots=plots, last_happiness=0.8)
    session = GameSession(
        state=GameState(world=cfg.new_world(), players={"p1": player}),
        cfg=cfg, player_ids=["p1"],
    )
    payload = {
        "player_id": "p1",
        "builds": [{
            "plot_id": "5,5", "plot_type": "service",
            "target_level": 1, "product": None,
        }],
        "plot_plans": {},
        "purchases": {},
        "food": {},
        "why": "在 5,5 建服务。",
    }
    fake = FakeClient(result=llm_client.LLMResult(
        ok=True, model="fake-kimi", content=json.dumps(payload, ensure_ascii=False),
    ))
    out = ai_host.advise(session, "p1", "服务建在哪？", client=fake, draft=True)
    assert out["ok"] is True, out
    plan = out["suggested_plan"]
    assert plan is not None, f"应编译成功：issues={out.get('plan_issues')}"
    assert any(b.get("plot_id") == "5,5" and b.get("plot_type") == "service"
               for b in plan["builds"]), plan["builds"]

    summary = out["summary"]
    assert "服务位置与理由：" in summary, f"摘要缺服务选址说明：{summary}"
    assert "在 5,5 建 1 级服务" in summary, summary
    assert "每邻 +" in summary and "合计 +" in summary, (
        f"服务句必须带单位口径（每邻/合计）：{summary}"
    )
    assert "服务之间不加成、服务不算邻居" in summary, summary


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
