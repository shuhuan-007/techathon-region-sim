"""终局报告生成器（B 单接力）—— pytest 函数风格，与 test_pipeline.py 一致。

跑法（在 region-sim/ 下，用 DSH 捆绑 Python）：

    先在 PowerShell 里把打包好的 Python 绑到 $py：
        $py = 'D:/dsh/resources/runtime/primary-runtime/dependencies/python/python.exe'
    然后（在 region-sim/ 下）跑本文件：
        & $py -m pytest tests/test_finale_report.py -q
    回归（确认没碰坏 A 单）：
        & $py -m pytest tests/test_finale_report.py tests/test_game_finale.py -q

覆盖 B 单任务书钉好的六条：
  1. 迷你局打完 → narrate_finale ok=True，四键齐全；charts 两条、点数 == total_quarters、
     GDP 曲线第 i 点 == history[i].value_added，幸福度 == history[i].food.happiness。
  2. 模型坏 JSON → ok=True 兜底；charts 仍正确；warning 非空；body 含真实终局数据。
  3. 缺季（手工删 history 一行）→ 该季 chart point value == null；
     missing_quarters 如实出现在 body 或 facts_used。
  4. 未打完（game_over False）→ body 不宣称「游戏已结束/圆满收官」；如实说明未完成。
  5. facts_used 覆盖关键事实；值原始类型（int/float/str）；路径含 history.<i>.<field>。
  6. GET /api/finale（server.app.dispatch 真路径）→ 200，且含 finale 与 narrative 两键。

注意：本文件与 tests/test_finale_report_engine.py **互补**——engine 那份
   （unittest 类风格，前一棒写的）管引擎层形状/兜底/模型替换的八条精细用例；
   本份（pytest 函数风格，本棒补）管 B 单任务书六条＋服务端并联键。
   两份并存，不互相替代。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine import finale as F                  # noqa: E402
from engine.models import (                     # noqa: E402
    Plan,
    PlotPlan,
    PlotType,
    SessionPhase,
    WorldPhase,
)
from server.app import App, dispatch            # noqa: E402
from server.registry import GameRegistry        # noqa: E402
from server.session import GameSession          # noqa: E402
from storage import MemoryStorage               # noqa: E402
from tests.test_m1 import make_state, mature, plain_plan  # noqa: E402
from tests.test_pipeline import cfg_with_example_elasticity  # noqa: E402

TOL = 1e-9


# ================================================================
# 夹具
# ================================================================


def _play_mini_game(*, total_quarters: int = 2, omit_quarters=()):
    """走真引擎 / 真 GameSession 路径把一局迷你局打到终局。

    返回 (session, pid)。omit_quarters 给要**手工删掉**的季号（缺季用例）。
    """
    cfg = cfg_with_example_elasticity()
    cfg.total_quarters = total_quarters
    plot = mature("0,0", PlotType.RESOURCE, 1)
    st = make_state(cfg, "p1", plot, cash=1000.0)
    st.world.total_quarters = total_quarters
    session = GameSession(state=st, cfg=cfg, player_ids=["p1"])
    pid = "p1"

    for _ in range(total_quarters):
        session.submit(pid, Plan(player_id=pid, plot_plans={
            "0,0": PlotPlan(plot_id="0,0", active=True, u=1.0),
        }))
        session.end_quarter(pid)
        if session.phase is SessionPhase.REPORT:
            session.start_next_quarter()

    if omit_quarters:
        rows = session.state.history[pid]
        session.state.history[pid] = [
            r for r in rows if r.get("quarter") not in set(omit_quarters)
        ]
    return session, pid


class FakeOK:
    """合法的假客户端：回一份**四键齐具**的 JSON（charts 形状过校验）。

    charts 填的是**全 None**——能不能信由引擎侧决定：generate_finale_report
    会用引擎版 chart_points(overview) 替换回来。这里只要能过形状校验。
    """

    def __init__(self, total: int = 2):
        self.total = total
        self.calls: list = []

    def chat_completions(self, messages, **_kw):
        self.calls.append(messages)
        gdp_pts = [{"quarter": q, "value": None} for q in range(1, self.total + 1)]
        hap_pts = [{"quarter": q, "value": None} for q in range(1, self.total + 1)]
        payload = {
            "headline": "《p1的2季经营终局报告（假）》",
            "body": "## 一、终局概览\n本份由假客户端给出的正文，占位。",
            "charts": [
                {"id": "gdp", "title": "第1—2季度 本季GDP 变化",
                 "metric": "gdp", "unit": None, "points": gdp_pts},
                {"id": "happiness", "title": "第1—2季度 幸福度 变化",
                 "metric": "happiness", "unit": None, "points": hap_pts},
            ],
            "facts_used": {"fake.marker": True},
        }
        return {"ok": True, "model": "fake-model-ok",
                "content": json.dumps(payload, ensure_ascii=False)}


class FakeBadJSON:
    """回**坏 JSON**——解析不出来，理应落 deterministic_body 兜底。"""

    def chat_completions(self, messages, **_kw):
        return {"ok": True, "model": "fake-model-bad",
                "content": "这不是 JSON；也没有可解析对象 {{ 残缺"}


class FakeCrash:
    """`chat_completions` **直接抛异常**——narrate 收网为 client_crashed，
    generate_finale_report 落兜底。"""

    def chat_completions(self, messages, **_kw):
        raise RuntimeError("fake gateway down")


# ================================================================
# 1 - 迷你局打完 -> ok=True，四键齐；charts 两条且点值与 history 对齐
# ================================================================


def test_mini_game_finale_ok_four_keys_and_charts_match():
    session, pid = _play_mini_game(total_quarters=2)
    overview = session.finale_overview(pid)
    assert overview["game_over"] is True
    assert overview["total_quarters"] == 2
    assert overview["missing_quarters"] == []

    client = FakeOK(total=2)
    out = F.generate_finale_report(overview, session.cfg, client=client)
    assert out["ok"] is True
    for k in ("headline", "body", "charts", "facts_used"):
        assert k in out, "narrative 缺四键之一：{k}".format(k=k)
    assert len(client.calls) == 1, "模型应被调一次（迷你局完整终局）"

    charts = out["charts"]
    assert isinstance(charts, list) and len(charts) == 2
    by_id = {c["id"]: c for c in charts}
    assert set(by_id) == {"gdp", "happiness"}
    for chart in by_id.values():
        assert len(chart["points"]) == 2
        assert [p["quarter"] for p in chart["points"]] == [1, 2]

    # 关键对齐：charts 第 i 点 == history[i]（按 quarter 升序）
    history = sorted(overview["history"], key=lambda r: r["quarter"])
    for i, pt in enumerate(by_id["gdp"]["points"]):
        assert pt["quarter"] == history[i]["quarter"]
        assert pt["value"] == history[i]["value_added"]
    for i, pt in enumerate(by_id["happiness"]["points"]):
        assert pt["quarter"] == history[i]["quarter"]
        assert pt["value"] == history[i]["food"]["happiness"]


# ================================================================
# 2 - 模型坏 JSON / 模型炸 -> 仍 ok=True（兜底），charts 正确 + warning + body 含真实数据
# ================================================================


def _assert_fallback_package(overview, out):
    assert out["ok"] is True
    assert out.get("source") == "fallback"
    assert out.get("warning"), "应带 warning 标明模型正文不可用"
    for k in ("headline", "body", "charts", "facts_used"):
        assert k in out

    # charts 引擎版替换 —— 数据仍正确
    charts = out["charts"]
    assert len(charts) == 2
    by_id = {c["id"]: c for c in charts}
    history = sorted(overview["history"], key=lambda r: r["quarter"])
    for i, pt in enumerate(by_id["gdp"]["points"]):
        assert pt["value"] == history[i]["value_added"]
    for i, pt in enumerate(by_id["happiness"]["points"]):
        assert pt["value"] == history[i]["food"]["happiness"]

    # body 必含真实终局数据：最终现金 + 终局幸福度原值字符串
    fr = overview["final_report"]
    cash_after = float(fr["cash_after"])
    hap = float(fr["food"]["happiness"])
    body = out["body"]
    assert ("{:.2f}".format(cash_after) in body
            or "{:.1f}".format(cash_after) in body), (
        "body 应出现终局现金原值 {} 的格式化字符串".format(cash_after)
    )
    assert "{:.2f}".format(hap) in body, (
        "body 应出现终局幸福度原值 {} 的格式化字符串".format(hap)
    )
    # 兜底红线：不得出现禁词与货币单位
    for w in ("最赚钱", "惨败", "完美", "天才", "完胜", "血亏"):
        assert w not in body
    for sym in ("¥", "￥", "$"):
        assert sym not in body


def test_model_bad_json_falls_back_with_real_data():
    session, pid = _play_mini_game(total_quarters=2)
    overview = session.finale_overview(pid)
    out = F.generate_finale_report(overview, session.cfg, client=FakeBadJSON())
    _assert_fallback_package(overview, out)


def test_model_crash_falls_back_with_real_data():
    session, pid = _play_mini_game(total_quarters=2)
    overview = session.finale_overview(pid)
    out = F.generate_finale_report(overview, session.cfg, client=FakeCrash())
    _assert_fallback_package(overview, out)


# ================================================================
# 3 - 缺季：该季 chart point value == null + missing_quarters 如实披露
# ================================================================


def test_missing_quarter_chart_point_null_and_disclosed():
    session, pid = _play_mini_game(total_quarters=3, omit_quarters=(2,))
    overview = session.finale_overview(pid)
    assert overview["game_over"] is True
    assert overview["total_quarters"] == 3
    assert 2 in overview["missing_quarters"]

    out = F.generate_finale_report(overview, session.cfg, client=FakeBadJSON())
    assert out["ok"] is True
    by_id = {c["id"]: c for c in out["charts"]}
    assert set(by_id) == {"gdp", "happiness"}

    # 每条曲线仍覆盖 1..total_quarters（3 点，quarter 严格 1/2/3 升序）
    for chart in by_id.values():
        assert len(chart["points"]) == 3
        assert [p["quarter"] for p in chart["points"]] == [1, 2, 3]

    gdp_q2 = by_id["gdp"]["points"][1]
    hap_q2 = by_id["happiness"]["points"][1]
    assert gdp_q2["quarter"] == 2 and gdp_q2["value"] is None
    assert hap_q2["quarter"] == 2 and hap_q2["value"] is None

    # 1、3 季仍在（不补、不删）
    assert by_id["gdp"]["points"][0]["value"] is not None
    assert by_id["gdp"]["points"][2]["value"] is not None
    assert by_id["happiness"]["points"][0]["value"] is not None
    assert by_id["happiness"]["points"][2]["value"] is not None

    # missing_quarters 如实披露：body 里明说第 2 季缺失，或 facts_used 含
    body = out["body"]
    body_hit = ("第2季" in body) or ("缺失" in body) or ("未提供" in body)
    facts_hit = any(
        (isinstance(v, list) and 2 in v) or v == 2
        for v in out["facts_used"].values()
    )
    assert body_hit or facts_hit, (
        "missing_quarters 应如实出现在 body 或 facts_used 中"
    )


# ================================================================
# 4 - 未打完（game_over=False）-> body 不宣称「游戏已结束/圆满收官」，如实说明未完成
# ================================================================


def test_unfinished_game_does_not_claim_game_over():
    cfg = cfg_with_example_elasticity()
    cfg.total_quarters = 20
    plot = mature("0,0", PlotType.RESOURCE, 1)
    st = make_state(cfg, "p1", plot, cash=1000.0)
    st.world.total_quarters = 20
    session = GameSession(state=st, cfg=cfg, player_ids=["p1"])
    pid = "p1"
    for _ in range(2):  # 只打完 2/20 季
        session.submit(pid, Plan(player_id=pid, plot_plans={
            "0,0": PlotPlan(plot_id="0,0", active=True, u=1.0),
        }))
        session.end_quarter(pid)
        if session.phase is SessionPhase.REPORT:
            session.start_next_quarter()
    overview = session.finale_overview(pid)
    assert overview["game_over"] is False
    assert overview["quarters_completed"] == 2

    out = F.generate_finale_report(overview, session.cfg, client=FakeBadJSON())
    assert out["ok"] is True
    assert out["source"] == "fallback"   # game_over 非 True -> 提前走模板分支

    body = out["body"]
    assert any(kw in body for kw in
               ("尚不具备", "仍在进行中", "未结束", "阶段性汇总", "未结算")), (
        "body 应如实说明未完成"
    )
    assert "游戏已结束" not in body
    assert "圆满收官" not in body
    assert "20个季度的经营到这里告一段落" not in body
    assert "20 个季度的经营到这里告一段落" not in body


# ================================================================
# 5 - facts_used 形状：覆盖关键事实、值原始类型、路径含 history.<i>.<field>
# ================================================================


def test_facts_used_covers_key_facts_with_raw_types_and_history_paths():
    session, pid = _play_mini_game(total_quarters=2)
    overview = session.finale_overview(pid)
    out = F.generate_finale_report(overview, session.cfg, client=FakeBadJSON())
    fu = out["facts_used"]
    assert isinstance(fu, dict) and fu

    # 顶层事实
    assert fu["player_id"] == pid
    assert fu["game_over"] is True
    assert fu["quarters_completed"] == 2
    assert isinstance(fu["quarters_completed"], int)

    # charts/headline/body 用到的 history.<i>.<field> 必须存在且原值相等
    history = sorted(overview["history"], key=lambda r: r["quarter"])
    for i, row in enumerate(history):
        for k in ("quarter", "value_added", "cash_before", "cash_after",
                  "sales_revenue", "operating_profit"):
            path = "history.{}.{}".format(i, k)
            assert path in fu, "缺 {}".format(path)
            orig = row.get(k)
            got = fu[path]
            assert isinstance(got, (int, float, str, bool)) or got is None, (
                "{} 类型被错改：{}".format(path, type(got).__name__)
            )
            assert got == orig
        # 嵌套路径：food.happiness / population.population
        hap_path = "history.{}.food.happiness".format(i)
        assert hap_path in fu and fu[hap_path] == row["food"]["happiness"]
        pop_path = "history.{}.population.population".format(i)
        assert pop_path in fu and fu[pop_path] == row["population"]["population"]

    # final_report 顶层路径存在（B 单正文「已提供的正式结果」引用点）
    assert "final_report.cash_after" in fu
    assert "final_report.food.happiness" in fu
    assert "final_report.population.population" in fu


# ================================================================
# 6 - GET /api/finale 真路径：回 200，同时含 finale 与 narrative 两个键
# ================================================================


def test_finale_endpoint_returns_200_with_both_keys():
    cfg = cfg_with_example_elasticity()
    cfg.total_quarters = 2
    registry = GameRegistry(cfg, MemoryStorage())
    app = App(cfg, registry)
    gid, pid = "g1", "p1"
    code, body = dispatch(app, "POST", "/api/new_game", {},
                          {"game_id": gid, "player_id": pid})
    assert code == 200, body

    session = registry.get(gid)
    plot = mature("0,0", PlotType.RESOURCE, 1)
    session.state.players[pid].plots[plot.plot_id] = plot
    session.state.world.phase = WorldPhase.RUNNING
    session.state.players[pid].last_happiness = 0.8
    session.state.world.total_quarters = 2

    # 走真 API 路径打完 2 季（迷你局）
    for _ in range(2):
        code_s, body_s = dispatch(app, "POST", "/api/submit", {},
                                  {"game": gid, "player": pid, "plan": {
                                      "player_id": pid,
                                      "plot_plans": {"0,0": {"active": True, "u": 1.0}},
                                  }})
        assert code_s == 200, body_s
        code_e, body_e = dispatch(app, "POST", "/api/end_quarter", {},
                                  {"game": gid, "player": pid})
        assert code_e == 200, body_e
        if session.phase is SessionPhase.REPORT:
            dispatch(app, "POST", "/api/next_quarter", {},
                     {"game": gid, "player": pid})

    assert session.phase is SessionPhase.FINISHED

    code, body = dispatch(app, "GET", "/api/finale",
                          {"game": gid, "player": pid}, {})
    assert code == 200, body
    assert body["ok"] is True
    assert "finale" in body, "缺 finale 键（A 单契约）"
    assert "narrative" in body, "缺 narrative 键（B 单并联契约）"

    fin = body["finale"]
    assert fin["game_over"] is True
    assert fin["total_quarters"] == 2
    assert fin["quarter"] == 3
    assert isinstance(fin["history"], list) and len(fin["history"]) == 2

    nar = body["narrative"]
    assert isinstance(nar, dict)
    for k in ("ok", "headline", "body", "charts", "facts_used"):
        assert k in nar, "narrative 缺键 {}".format(k)
    assert nar["ok"] is True
    source = nar.get("source")
    if source not in ("error_app_fallback", "error_no_engine"):
        charts = nar["charts"]
        assert isinstance(charts, list) and len(charts) == 2
        for chart in charts:
            assert len(chart["points"]) == 2
            assert [p["quarter"] for p in chart["points"]] == [1, 2]


# ================================================================
# main
# ================================================================
#
# ⚠️ 2026-10-18 改：原来这里是 `sys.exit(pytest.main([__file__, "-q"]))`，
#    但**本机没有 pytest**（DSH 捆绑 3.12.14 只带标准库；`.venv` 里装了 pip
#    却没有可用的 pytest）→ 这个文件直接 `ModuleNotFoundError` 跑不起来，
#    连同它覆盖的 7 条终局断言一起"看起来是绿的"（exit code 被 -q 吃掉）。
#    本项目其余 28 个测试全是「标准库 runner + 直接 python 跑」的惯例，改为一致。
#
#    本文件**没有**用到任何 pytest 特性（无 parametrize / fixture），
#    所以只需换掉这个入口，测试正文一行不用动。

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

