"""AI 起草 → 填入 → 提交 → 结算：**端到端必须有结果**。

跑法（在 region-sim/ 下）：

    python tests/test_ai_draft_e2e.py

这是「AI 秘书成不成立」的最终验收：
  · 模型回真实故障输出（把已有地块写成 builds、漏写 plot_plans）；
  · `advise(draft=True)` 必须给出 `suggested_plan`，且同时过 validate + preview；
  · 前端拿到的 plan 能走 `/api/submit` 的同一入口 `session.submit`；
  · `end_quarter` 必须成功（不报资金不足 / build_not_an_upgrade / 路线错误）；
  · AI 仍然**绝不 commit**：advise 前后 `plans` 为空、phase 不变。
"""

from __future__ import annotations

import copy
import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import pipeline, validate
from engine.loader import load_config
from engine.models import GameState, PlayerState, SessionPhase
from server import ai_host, llm_client
from server.app import plan_from_dict
from server.session import GameSession
from tests.test_m1 import mature

TOL = 1e-9


# ══════════════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════════════


def _opening_session(cfg) -> GameSession:
    return GameSession(
        state=GameState(world=cfg.new_world(), players={"p1": cfg.new_player("p1")}),
        cfg=cfg,
    )


def _mid_session(cfg) -> GameSession:
    """仿 `saves/1.json`：第 4 季、5 块运行地块、现金 416、H=0。"""
    from engine.models import PlotType

    plots = {
        "4,5": mature("4,5", PlotType.RESOURCE, 1, product="A"),
        "5,3": mature("5,3", PlotType.SERVICE, 1),
        "5,4": mature("5,4", PlotType.AGRICULTURE, 1, product="A"),
        "6,3": mature("6,3", PlotType.RESOURCE, 1),
        "6,4": mature("6,4", PlotType.INDUSTRY, 1, product="G"),
    }
    player = PlayerState(player_id="p1", cash=416.0, plots=plots, last_happiness=0.0)
    state = GameState(world=cfg.new_world(), players={"p1": player})
    return GameSession(state=state, cfg=cfg)


class FakeModel:
    """回一份固定的 Plan JSON —— 模拟真实模型的坏输出。"""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def chat_completions(self, messages, **_kw):
        self.calls.append(messages)
        return llm_client.LLMResult(
            ok=True, model="fake-kimi",
            content=json.dumps(self.payload, ensure_ascii=False),
        )


def _assert_advice_plan_is_settleable(session, advice, cfg):
    """校验“填进游戏之前”的计划：validate + preview 都必须过。"""
    assert advice["suggested_plan"] is not None, (
        f"AI 没给计划：summary={advice.get('summary')!r}、"
        f"issues={advice.get('plan_issues')!r}"
    )
    plan = plan_from_dict(advice["suggested_plan"])
    plan.player_id = "p1"
    issues = validate.validate_plan(
        plan, session.state.player("p1"), cfg, "p1"
    )
    assert not issues, (
        f"★ 填进游戏前的计划必须合法，实得 {[i.to_dict() for i in issues]}"
    )
    rep = pipeline.preview(session.state, {"p1": plan}, cfg)
    assert rep is not None
    assert rep.cash_after >= -TOL, "试算后现金不能为负"
    return plan


# ══════════════════════════════════════════════════════════════════
# 一、真实故障：已有地块当 builds + 漏写 plot_plans
# ══════════════════════════════════════════════════════════════════


def test_midgame_ai_draft_can_be_submitted_and_settled():
    cfg = load_config()
    session = _mid_session(cfg)
    before_plans = copy.deepcopy(session.plans)
    before_phase = session.phase
    before_state = json.dumps(session.state.to_dict(), ensure_ascii=False, sort_keys=True)

    # 真跑抓到的模型输出（save 1.json，qwen-flash）
    payload = {
        "player_id": "p1",
        "builds": [
            {"plot_id": "4,5", "plot_type": "resource", "target_level": 1, "product": None},
            {"plot_id": "5,4", "plot_type": "agriculture", "target_level": 1, "product": "A"},
            {"plot_id": "6,3", "plot_type": "service", "target_level": 1, "product": None},
        ],
        "plot_plans": {},
        "purchases": {},
        "food": {},
    }
    fake = FakeModel(payload)
    advice = ai_host.advise(session, "p1", "这季怎么安排？帮我起草一份计划。",
                            draft=True, client=fake)

    # ① AI 只起草，不提交
    assert advice["ok"] is True, advice
    assert session.plans == before_plans, "★ advise 动了 plans（AI 无 commit）"
    assert session.phase == before_phase, "★ advise 动了 phase"
    assert json.dumps(session.state.to_dict(), ensure_ascii=False, sort_keys=True) == before_state, (
        "★ advise 改了状态"
    )

    # ② 计划已修复：没有 build，5 块地都开工
    assert advice["suggested_plan"]["builds"] == [], (
        f"已有地块不能再写成建造：{advice['suggested_plan']['builds']}"
    )
    assert len(advice["suggested_plan"]["plot_plans"]) == 5
    assert advice.get("plan_notes"), "必须留下校正说明"
    assert advice.get("plan_preview"), "必须带试算摘要"

    # ③ 填进游戏前的双重检查
    plan = _assert_advice_plan_is_settleable(session, advice, cfg)

    # ④ 玩家确认 → 提交（与 /api/submit 同一入口）
    session.submit("p1", plan)
    assert "p1" in session.plans

    # ⑤ 结算必须成功（真实故障里这里会炸）
    rep = session.end_quarter("p1")
    assert rep is not None
    assert session.phase is SessionPhase.REPORT
    assert session.state.player("p1").cash >= -TOL
    d = rep.to_dict()
    assert d["cash_after"] >= -TOL
    assert d["total_deduction"] >= 0


# ══════════════════════════════════════════════════════════════════
# 二、超预算：10 条工业 → 缩量后仍能提交 + 结算
# ══════════════════════════════════════════════════════════════════


def test_overbudget_ai_draft_is_shrunk_then_settles():
    cfg = load_config()
    session = _opening_session(cfg)
    payload = {
        "player_id": "p1",
        "builds": [
            {"plot_id": f"0,{i}", "plot_type": "industry",
             "target_level": 1, "product": "G"}
            for i in range(10)
        ],
        "plot_plans": {},
        "purchases": {},
        "food": {},
    }
    advice = ai_host.advise(session, "p1", "先建 10 个工业",
                            draft=True, client=FakeModel(payload))
    assert advice["ok"] is True, advice
    plan = _assert_advice_plan_is_settleable(session, advice, cfg)
    assert len(plan.builds) < 10, "超预算时必须缩量"

    session.submit("p1", plan)
    rep = session.end_quarter("p1")
    assert rep is not None
    assert session.state.player("p1").cash >= -TOL
    assert rep.to_dict()["cash_after"] >= -TOL


# ══════════════════════════════════════════════════════════════════
# 三、开局一块农地：最小可用计划也能真结算
# ══════════════════════════════════════════════════════════════════


def test_opening_ai_draft_can_be_settled():
    cfg = load_config()
    session = _opening_session(cfg)
    payload = {
        "player_id": "p1",
        "builds": [{"plot_id": "0,0", "plot_type": "agriculture",
                    "target_level": 1, "product": "A"}],
        "plot_plans": {}, "purchases": {}, "food": {},
    }
    advice = ai_host.advise(session, "p1", "先建一块农地",
                            draft=True, client=FakeModel(payload))
    assert advice["ok"] is True
    plan = _assert_advice_plan_is_settleable(session, advice, cfg)
    session.submit("p1", plan)
    rep = session.end_quarter("p1")
    assert rep is not None
    assert len(session.state.player("p1").plots) == 1
    assert session.state.player("p1").plots["0,0"].level == 1


# ══════════════════════════════════════════════════════════════════
# 四、整条 HTTP 路由：/api/ai/advise → /api/submit → /api/end_quarter
# ══════════════════════════════════════════════════════════════════


def test_http_route_draft_submit_settle():
    """用户真实路径：点 AI 起草 → 计划落表 → 提交 → 结算。

    用 `dispatch()` 直测路由（不起 socket），只把模型客户端 monkeypatch 成
    固定回包 —— 这样测的是**路由 + ai_host + 编译器 + submit + 结算**，
    不是网络。
    """
    from server.app import App, dispatch
    from server.registry import GameRegistry
    from storage import MemoryStorage

    cfg = load_config()
    app = App(cfg, GameRegistry(cfg, MemoryStorage()))
    app.registry.create("g1", "p1")

    payload = {
        "player_id": "p1",
        "builds": [
            {"plot_id": "0,0", "plot_type": "agriculture",
             "target_level": 1, "product": "A"},
            {"plot_id": "0,1", "plot_type": "agriculture",
             "target_level": 1, "product": "A"},
        ],
        # 模型常犯的错：把“开工”也写进 plot_plans，但这两块地本季才建
        "plot_plans": {
            "0,0": {"plot_id": "0,0", "active": True, "u": 1.0},
            "0,1": {"plot_id": "0,1", "active": True, "u": 1.0},
        },
        "purchases": {},
        "food": {},
    }
    fake = FakeModel(payload)

    orig = llm_client.chat_completions
    llm_client.chat_completions = fake.chat_completions
    try:
        code, body = dispatch(
            app, "POST", "/api/ai/advise", {},
            {"game": "g1", "player": "p1", "question": "建两块农地", "draft": True},
        )
        assert code == 200, (code, body)
        assert body["ok"] is True, body
        advice = body["advice"]
        assert advice["suggested_plan"] is not None, advice
        assert advice["plan_notes"], "路由响应里要带校正说明"

        code, sub = dispatch(
            app, "POST", "/api/submit", {},
            {"game": "g1", "player": "p1", "plan": advice["suggested_plan"]},
        )
        assert code == 200, (code, sub)

        code, end = dispatch(
            app, "POST", "/api/end_quarter", {},
            {"game": "g1", "player": "p1"},
        )
        assert code == 200, (code, end)
        assert end["report"] is not None
        assert end["state"]["cash"] >= -TOL
    finally:
        llm_client.chat_completions = orig


# ══════════════════════════════════════════════════════════════════
# runner
# ══════════════════════════════════════════════════════════════════


def _run_all() -> int:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    mod = sys.modules[__name__]
    names = sorted(n for n in dir(mod) if n.startswith("test_"))
    ok = fail = 0
    print("=" * 68)
    print("AI 起草 → 提交 → 结算（端到端）")
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
