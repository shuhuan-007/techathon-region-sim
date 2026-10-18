"""AI 秘书的三个只读端点对账 —— /api/simulate、/api/validate、/api/rules。

依据《规则基线》§11.4（AI 可解释 → 可试算 → 可起草 → 玩家确认 → 提交；
**AI 无 commit**）。三个端点**全部只读**：不落存档、不改 GameState、不动 plans。

跑法（在 region-sim/ 下）：

    python tests/test_ai_endpoints.py

★ 本文件的测法与 test_server.py 相同 —— 直接调 `dispatch()`，不起 socket。
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.loader import load_config
from engine.pipeline import SettlementError
from server import app as server_app
from server.app import App, dispatch
from server.registry import GameRegistry
from storage import MemoryStorage

TOL = 1e-9


# ══════════════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════════════


class Client:
    def __init__(self):
        self.cfg = load_config()
        self.registry = GameRegistry(self.cfg, MemoryStorage())
        self.app = App(self.cfg, self.registry)

    def get(self, path, **params):
        return dispatch(self.app, "GET", path, params, {})

    def post(self, path, body=None):
        return dispatch(self.app, "POST", path, {}, body or {})

    def new_game(self, gid="g1"):
        return self.post("/api/new_game", {"game_id": gid})


# ══════════════════════════════════════════════════════════════════
# 一、★ /api/simulate —— 试算**不改状态**（最关键）
# ══════════════════════════════════════════════════════════════════


def test_simulate_returns_report_and_state_unchanged():
    """★ 试算前后 `/api/state` **逐字相同**（本项目 /state 无时间戳字段）。

    这是整个 AI 端点组的命门：试算必须**深拷贝后跑**，
    任何"顺手改一下原状态"都会立刻被这条测出来。
    """
    c = Client()
    c.new_game("g1")

    before = c.get("/api/state", game="g1", player="p1")[1]["state"]

    plan = {"player_id": "p1", "builds": [{
        "plot_id": "3,4", "plot_type": "agriculture",
        "target_level": 1, "product": "A",
    }]}
    code, body = c.post("/api/simulate", {
        "game": "g1", "player": "p1", "plan": plan,
    })
    assert code == 200, body
    assert body["ok"] is True
    report = body["report"]
    # 报告要长得像 QuarterReport：有 quarter、有 cash 账
    assert report["quarter"] == 1
    assert report["player_id"] == "p1"
    for key in ("cash_before", "cash_after", "total_deduction"):
        assert key in report, f"报告缺季度账关键字段 {key}"

    after = c.get("/api/state", game="g1", player="p1")[1]["state"]

    # ★ 逐字相同（json.dumps 排序后字符串相等 == 字典完全相等）
    assert json.dumps(before, sort_keys=True, ensure_ascii=False) == \
           json.dumps(after,  sort_keys=True, ensure_ascii=False), (
        "试算改了状态！/state 前后不一致"
    )
    # 关键字段再独立点一遍（防止 dumps 巧合掩盖了什么）
    assert after["cash"] == before["cash"]
    assert after["quarter"] == before["quarter"]
    assert after["plots"] == before["plots"], "试算凭空造出了地块"
    assert after["phase"] == before["phase"] == "collecting", "试算改了阶段"


def test_simulate_invalid_plan_is_400_not_500():
    """计划非法 → 400 `plan_rejected` + 五要素 issues，**不是** 500。"""
    c = Client()
    c.new_game("g1")
    code, body = c.post("/api/simulate", {
        "game": "g1", "player": "p1",
        "plan": {"player_id": "p1", "purchases": {"ZZZ": 1.0}},
    })
    assert code == 400, f"应为 400，实得 {code} {body}"
    assert body["kind"] == "plan_rejected"
    assert body["issues"], "应带 issues"
    issue = body["issues"][0]
    for key in ("code", "target", "message", "requested", "available", "shortfall"):
        assert key in issue, f"issues 缺五要素字段 {key}"


def test_simulate_out_of_phase_is_409_conflict():
    """时机不对（不在行动阶段）→ 409 `conflict`，与 /api/submit 同口径。"""
    c = Client()
    c.new_game("g1")
    # 先走到 REPORT 阶段
    c.post("/api/submit", {"game": "g1", "player": "p1",
                           "plan": {"player_id": "p1"}})
    c.post("/api/end_quarter", {"game": "g1", "player": "p1"})

    code, body = c.post("/api/simulate", {
        "game": "g1", "player": "p1",
        "plan": {"player_id": "p1"},
    })
    assert code == 409, f"应为 409，实得 {code} {body}"
    assert body["kind"] == "conflict"


# ══════════════════════════════════════════════════════════════════
# 二、/api/validate —— 只校验，与 /api/submit 完全同形状
# ══════════════════════════════════════════════════════════════════


def test_validate_ok_returns_valid_true():
    c = Client()
    c.new_game("g1")
    code, body = c.post("/api/validate", {
        "game": "g1", "player": "p1",
        "plan": {"player_id": "p1", "builds": [{
            "plot_id": "3,4", "plot_type": "agriculture",
            "target_level": 1, "product": "A",
        }]},
    })
    assert code == 200, body
    assert body == {"ok": True, "valid": True, "issues": []}


def test_validate_invalid_matches_submit_shape_exactly():
    """非法计划 → /api/validate 与 /api/submit 的错误**形状完全一致**。"""
    plan = {"player_id": "p1", "purchases": {"ZZZ": 1.0}}

    c1 = Client()
    c1.new_game("g1")
    code_v, body_v = c1.post("/api/validate", {
        "game": "g1", "player": "p1", "plan": plan,
    })

    c2 = Client()
    c2.new_game("g1")
    code_s, body_s = c2.post("/api/submit", {
        "game": "g1", "player": "p1", "plan": plan,
    })

    assert code_v == code_s == 400
    # 关键字段：两套都要带上 plan_rejected + 结构化 issues 五要素
    assert body_v["kind"] == body_s["kind"] == "plan_rejected"
    assert set(body_v) == set(body_s), (
        f"两个端点的键不一致：validate={sorted(body_v)} submit={sorted(body_s)}"
    )
    assert json.dumps(body_v["issues"], sort_keys=True, ensure_ascii=False) == \
           json.dumps(body_s["issues"], sort_keys=True, ensure_ascii=False), (
        "两个端点的 issues 内容不一致"
    )
    issue = body_v["issues"][0]
    assert issue["code"] == "unknown_goods"
    for key in ("code", "target", "message", "requested", "available", "shortfall"):
        assert key in issue


def test_validate_does_not_register_plan():
    """★ validate 是**只读**：调完之后状态里没有这份计划。"""
    c = Client()
    c.new_game("g1")
    before = c.get("/api/state", game="g1", player="p1")[1]["state"]

    code, _ = c.post("/api/validate", {
        "game": "g1", "player": "p1",
        "plan": {"player_id": "p1", "builds": [{
            "plot_id": "3,4", "plot_type": "agriculture",
            "target_level": 1, "product": "A",
        }]},
    })
    assert code == 200

    after = c.get("/api/state", game="g1", player="p1")[1]["state"]
    assert json.dumps(before, sort_keys=True) == json.dumps(after, sort_keys=True), (
        "validate 改了状态"
    )
    # /state 里通过 projection/can_settle 会暴露"已提交计划"，所以上面的 dumps 已足够钉死
    # 再补一刀：validate 之后还能再 submit（说明计划没入库）
    code, _ = c.post("/api/submit", {
        "game": "g1", "player": "p1", "plan": {"player_id": "p1"},
    })
    assert code == 200, "validate 之后还能正常提交 —— 说明 validate 没把计划塞进 plans"


# ══════════════════════════════════════════════════════════════════
# 三、/api/rules —— 把《规则基线》原样给出去
# ══════════════════════════════════════════════════════════════════


def test_rules_returns_full_text_and_sections():
    c = Client()
    c.new_game("g1")
    code, body = c.get("/api/rules")
    assert code == 200, body
    assert body["ok"] is True
    assert body["source"] == "docs/01-规范/规则基线.md"
    assert body["title"], "标题不应为空"
    # title 应是基线的第一行（去掉 `#`）
    assert "规则" in body["title"] or "基线" in body["title"]
    # 全文必须非空且长得像基线（含"程序决定事实，AI 只负责解释"铁律）
    assert len(body["text"]) > 1000, "基线全文不应这么短"
    assert "程序决定事实" in body["text"], "全文应含 §0 的铁律"
    # sections 非空：至少扫出一、二级标题
    assert body["sections"], "sections 不应为空"
    levels = {s["level"] for s in body["sections"]}
    assert 1 in levels and 2 in levels, f"应同时扫出 # 与 ## 标题，实得 {levels}"
    # 字段齐
    for s in body["sections"]:
        for key in ("title", "level", "line"):
            assert key in s, f"section 缺字段 {key}"


def test_rules_section_slice_returns_single_section():
    c = Client()
    c.new_game("g1")
    # 先拿全文找出 §11.4 的实际小节标题
    code, body = c.get("/api/rules")
    assert code == 200
    ai_sections = [s for s in body["sections"] if "11.4" in s["title"]]
    assert ai_sections, "基线里应能找到标号含 11.4 的小节"
    target = ai_sections[0]

    # 取该小节
    code, body = c.get("/api/rules", section="11.4")
    assert code == 200, body
    # 截出来的 text 必须**比全文短**
    assert len(body["text"]) < 5000, (
        f"应只回一小节，实得 {len(body['text'])} 字"
    )
    # 必须包含目标小节标题
    assert target["title"] in body["text"]
    # 不能包含下一个同级 §11.5（若有）或 §12
    assert not any(
        s["title"] in body["text"]
        for s in body["sections"]
        if s["line"] > target["line"] and s["level"] <= target["level"]
    ), "截出来的小节里不应混进下一个同级标题"


def test_rules_missing_file_is_500_not_silent_ok(monkeypatch_arg=None):
    """文件找不到 → 500 `internal`，**不许**返回 ok:true 装成功。"""
    c = Client()
    c.new_game("g1")
    original = server_app.RULES_PATH
    try:
        server_app.RULES_PATH = original.parent / "不存在的基线.md"
        code, body = c.get("/api/rules")
        assert code == 500, f"应为 500，实得 {code} {body}"
        assert body["ok"] is False
        assert body["kind"] == "internal"
        assert "不存在的基线" in body["detail"] or "规则基线不存在" in body["detail"], (
            "detail 应说明是哪个路径找不到，不能装成功"
        )
    finally:
        server_app.RULES_PATH = original


# ══════════════════════════════════════════════════════════════════
# 四、三条路由的方法限制（405）
# ══════════════════════════════════════════════════════════════════


def test_wrong_method_is_405_on_new_routes():
    """三条新路由**只接受声明的方法**。"""
    c = Client()
    c.new_game("g1")
    # simulate / validate 是 POST 路由 → GET 应 405
    code, body = c.get("/api/simulate")
    assert code == 405 and body["kind"] == "method_not_allowed", body
    code, body = c.get("/api/validate")
    assert code == 405 and body["kind"] == "method_not_allowed", body
    # rules 是 GET 路由 → POST 应 405
    code, body = c.post("/api/rules", {})
    assert code == 405 and body["kind"] == "method_not_allowed", body


def test_simulate_business_blocker_is_200_not_500():
    """★ 资金不足是**试算结果**，不是服务端 500。

    2026-10-18 真跑：玩家攒了一份超预算计划，`/api/simulate` 抛
    `SettlementError` → 旧 `_ERROR_MAP` 没有它 → 500 → 前端只能显示
    “预览暂不可用”。现在返回 200 + `report:null` + `blocker`，前端可显示
    “资金不足：需扣 X / 手上有 Y”。
    """
    c = Client()
    c.new_game("g1")
    s = c.registry.get("g1")
    orig = s.simulate

    def boom(plan):
        raise SettlementError(
            "资金不足：p1 本季需扣 472.21，手上只有 416.00。"
        )

    s.simulate = boom
    try:
        code, body = c.post("/api/simulate", {
            "game": "g1", "player": "p1",
            "plan": {"player_id": "p1"},
        })
    finally:
        s.simulate = orig

    assert code == 200, f"业务失败不该是 5xx，实得 {code}：{body}"
    assert body["ok"] is True
    assert body["report"] is None, "算不出来时不许编一份报告"
    assert body["kind"] == "settlement_failed"
    assert "资金不足" in body["blocker"]
    assert "472.21" in body["blocker"] and "416.00" in body["blocker"]


def test_simulate_overbudget_uses_real_preview_not_p0_estimate():
    """★ 超预算计划：不能让 P0 粗估抢先判 `plan_rejected`。

    `/api/simulate` 的权威是**真引擎试算**；`validate` 里的
    `insufficient_cash_estimate` 只是“按 P0 的预警”，可能误判。
    放它过去后，preview 会给出**真实**资金缺口。

    ★ 2026-10-18 第八轮新 P0（H-G 73→88）后：
        10 条 ind_new，前 3 条吃开局减免（90 + H-G×2×88 = 266），
        后 7 条原价（180 + H-G×2×88 = 356）
        → 3×266 + 7×356 = **3290**（旧值 2990）。
    """
    c = Client()
    c.new_game("g1")
    plan = {
        "player_id": "p1",
        "builds": [
            {"plot_id": f"0,{i}", "plot_type": "industry",
             "target_level": 1, "product": "G"}
            for i in range(10)
        ],
    }
    code, body = c.post("/api/simulate", {
        "game": "g1", "player": "p1", "plan": plan,
    })
    assert code == 200, f"超预算应走真实试算 blocker，不该 400：{body}"
    assert body["ok"] is True
    assert body["report"] is None
    assert body["kind"] == "settlement_failed"
    assert "资金不足" in body["blocker"]
    assert "3290" in body["blocker"] or "实际" in body["blocker"]


# ══════════════════════════════════════════════════════════════════
# 命令行汇总
# ══════════════════════════════════════════════════════════════════


def _run_all() -> int:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    mod = sys.modules[__name__]
    names = sorted(n for n in dir(mod) if n.startswith("test_"))
    ok = fail = 0
    print("=" * 68)
    print("AI 只读端点对账（simulate / validate / rules）")
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
