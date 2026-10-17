"""HTTP 层对账 —— 路由、错误翻译、只转发不放规则。

跑法（在 region-sim/ 下）：

    python tests/test_server.py

依据 README 五条红线之五（服务层只转发）与《项目架构-总》§二/§七。

★ 本文件的测法是**直接调 `dispatch()`，不起 socket** ——
  路由与错误翻译是纯函数，没必要为它们开端口。
  真起 HTTP 的冒烟由 `tools/smoke_http.py` 负责。
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.loader import load_config
from server.app import App, dispatch, plan_from_dict
from server.registry import GameRegistry
from storage import GameNotFound, JsonFileStorage, MemoryStorage

TOL = 1e-9


# ══════════════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════════════


class Client:
    """把 `dispatch()` 包成好看的样子：`c.get(...)` / `c.post(...)`。"""

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

    def build(self, gid, plot_id, ptype="agriculture", level=1, product="A"):
        plan = {"player_id": "p1", "builds": [{
            "plot_id": plot_id, "plot_type": ptype,
            "target_level": level, "product": product,
        }]}
        return self.post("/api/submit", {"game": gid, "player": "p1", "plan": plan})


# ══════════════════════════════════════════════════════════════════
# 一、基本路由
# ══════════════════════════════════════════════════════════════════


def test_games_list_starts_empty():
    c = Client()
    code, body = c.get("/api/games")
    assert code == 200 and body["ok"] is True
    assert body["games"] == []


def test_new_game_creates_playable_opening():
    """★ 开新局：现金 1200、**零地块**（§5.3 地图全是可建空地）。"""
    c = Client()
    code, body = c.new_game("g1")
    assert code == 200
    st = body["state"]
    assert abs(st["cash"] - 1200.0) < TOL, f"开局现金应为 1200，实得 {st['cash']}"
    assert st["plots"] == [], "开局不该有地块"
    assert st["quarter"] == 1
    assert st["total_quarters"] == 20


def test_state_has_all_sections():
    """/state 要给出界面清单要的各段（前端零计算）。"""
    c = Client()
    c.new_game("g1")
    code, body = c.get("/api/state", game="g1", player="p1")
    assert code == 200
    st = body["state"]
    for key in ("quarter", "total_quarters", "cash", "happiness", "population",
                "efficiency", "plots", "market", "report", "projection",
                "can_settle", "settle_blockers", "phase"):
        assert key in st, f"/state 缺 {key}"
    assert len(st["market"]) == 15, "市场要覆盖 15 个商品"


def test_map_is_full_grid():
    """地图给整张 10×10。"""
    c = Client()
    c.new_game("g1")
    code, body = c.get("/api/map", game="g1", player="p1")
    assert code == 200
    assert body["map"]["rows"] == 10 and body["map"]["cols"] == 10
    assert len(body["map"]["cells"]) == 100


def test_build_options_gives_fourteen_works():
    """建造面板的选项由**引擎**给（前端不许自己拼）。"""
    c = Client()
    code, body = c.get("/api/build_options")
    assert code == 200
    assert len(body["works"]) == 14, f"应为 14 条工程，实得 {len(body['works'])}"
    # 农业的可选产出要给全（建后不可更换，所以下拉必须齐）
    agri = [w for w in body["works"] if w["plot_type"] == "agriculture"][0]
    assert agri["products"] == ["A", "B", "C", "D"]


# ══════════════════════════════════════════════════════════════════
# 二、★ 完整一季走通
# ══════════════════════════════════════════════════════════════════


def test_full_quarter_cycle():
    """★ 建造 → 结算 → 读状态 → 下一季，整条链路走通。"""
    c = Client()
    c.new_game("g1")

    # 建造（新建一级农业 = 原价 80 现金，开局减免折半 → 40 + H-A×1 = 104）
    # 2026-10-16 用户裁定：开局减免 每类 3 名额 × 0.50（现金费折半）生效
    code, body = c.build("g1", "3,4")
    assert code == 200, body
    assert body["state"]["can_settle"] is True, body["state"]["settle_blockers"]

    # 结算
    code, body = c.post("/api/end_quarter", {"game": "g1", "player": "p1"})
    assert code == 200
    assert body["report"] is not None, "结算后应给出报告"
    st = body["state"]
    assert st["quarter"] == 2, f"结算后应进入第 2 季，实得 {st['quarter']}"
    # 2026-10-16 用户裁定：开局减免 每类 3 名额 × 0.50（现金费折半）生效
    # 2026-10-17 起每季 +subsidy（默认 120）补贴进账面 → 1200−104+120=1216
    subsidy = body["report"]["subsidy"]
    assert abs(st["cash"] - (1096.0 + subsidy)) < TOL, (
        f"1200−104+补贴{subsidy}={1096.0 + subsidy}，实得 {st['cash']}"
    )
    assert len(st["plots"]) == 1, "新建后应有一块地"
    # 2026-10-17 用户拍板：本季投钱投料也算一季工期。
    #   1 级农业工期 1 季 → **当季完工**，所以结算后状态应为 running（不再是在建）。
    #   旧断言是 "constructing"（旧口径要等下季才完工），已按新规则更新。
    assert st["plots"][0]["status"] == "running", (
        f"1 季工期当季完工，应为 running，实得 {st['plots'][0]['status']}"
    )

    # 进入下一季
    code, body = c.post("/api/next_quarter", {"game": "g1", "player": "p1"})
    assert code == 200
    assert body["state"]["phase"] == "collecting"


def test_report_endpoint_returns_last_report():
    c = Client()
    c.new_game("g1")
    c.build("g1", "0,0")
    c.post("/api/end_quarter", {"game": "g1", "player": "p1"})

    code, body = c.get("/api/report", game="g1", player="p1")
    assert code == 200 and body["report"] is not None
    assert body["report"]["quarter"] == 1


def test_construction_completes_over_http():
    """工期推进：第 2 季完工、等级落地。"""
    c = Client()
    c.new_game("g1")
    c.build("g1", "0,0")
    c.post("/api/end_quarter", {"game": "g1", "player": "p1"})
    c.post("/api/next_quarter", {"game": "g1", "player": "p1"})
    c.post("/api/submit", {"game": "g1", "player": "p1",
                           "plan": {"player_id": "p1"}})
    c.post("/api/end_quarter", {"game": "g1", "player": "p1"})

    code, body = c.get("/api/state", game="g1", player="p1")
    p = body["state"]["plots"][0]
    assert p["status"] == "running", f"应已完工，实得 {p['status']}"
    assert p["level"] == 1
    assert p["product"] == "A", "产出建后不可更换"


# ══════════════════════════════════════════════════════════════════
# 三、★ 错误翻译（前端要能按 kind 分支）
# ══════════════════════════════════════════════════════════════════


def test_unknown_route_is_404():
    c = Client()
    code, body = c.get("/api/nope")
    assert code == 404 and body["kind"] == "no_route"
    assert "routes" in body, "报错时把可用端点列出来，方便排查"


def test_wrong_method_is_405():
    c = Client()
    code, body = c.post("/api/games")
    assert code == 405 and body["kind"] == "method_not_allowed"


def test_missing_param_is_400():
    c = Client()
    code, body = c.get("/api/state")          # 没给 game
    assert code == 400 and body["kind"] == "bad_request"
    assert "game" in body["detail"]


def test_unknown_game_is_404_not_auto_created():
    """★ 读一个不存在的局 → 404，**不偷偷开一局新的**。"""
    c = Client()
    code, body = c.get("/api/state", game="nope")
    assert code == 404 and body["kind"] == "not_found"
    # 不该因为这次访问就冒出一局
    assert c.get("/api/games")[1]["games"] == []


def test_invalid_plan_is_400_with_structured_issues():
    """★ 计划不合法 → 400 `plan_rejected`，并带上 §11.3 的结构化五要素。

    ⚠️ 前端要能**不解析字符串**就把数字填进提示框，所以 issues 必须是结构化的。
    """
    c = Client()
    c.new_game("g1")
    code, body = c.post("/api/submit", {
        "game": "g1", "player": "p1",
        "plan": {"player_id": "p1", "purchases": {"ZZZ": 1.0}},
    })
    assert code == 400, body
    assert body["kind"] == "plan_rejected"
    assert body["issues"], "应带上 issues"
    issue = body["issues"][0]
    assert issue["code"] == "unknown_goods"
    for key in ("code", "target", "message", "requested", "available", "shortfall"):
        assert key in issue, f"issues 缺五要素字段 {key}"


def test_build_without_product_is_rejected():
    """农业新建不选产出 → 挡住（§5.1 建造时选定）。"""
    c = Client()
    c.new_game("g1")
    code, body = c.post("/api/submit", {
        "game": "g1", "player": "p1",
        "plan": {"player_id": "p1", "builds": [{
            "plot_id": "0,0", "plot_type": "agriculture", "target_level": 1,
        }]},
    })
    assert code == 400 and body["kind"] == "plan_rejected"
    assert body["issues"][0]["code"] == "build_product_required"


def test_submit_out_of_phase_is_409_conflict():
    """★ 结算后（月报阶段）再提交 → 409 `conflict`，**不是** 400。

    两者要分开：`Conflict` 是"时机不对"，`plan_rejected` 是"计划不对"，
    前端要做的事完全不同。
    """
    c = Client()
    c.new_game("g1")
    c.post("/api/submit", {"game": "g1", "player": "p1", "plan": {"player_id": "p1"}})
    c.post("/api/end_quarter", {"game": "g1", "player": "p1"})

    code, body = c.post("/api/submit", {
        "game": "g1", "player": "p1", "plan": {"player_id": "p1"},
    })
    assert code == 409 and body["kind"] == "conflict", body


def test_duplicate_new_game_is_rejected():
    """★ 重开同名局 → 拒绝，**不覆盖**已有存档。"""
    c = Client()
    c.new_game("g1")
    c.build("g1", "0,0")
    c.post("/api/end_quarter", {"game": "g1", "player": "p1"})

    before = c.get("/api/state", game="g1")[1]["state"]
    code, body = c.new_game("g1")
    assert code == 400, f"重名应被拒绝，实得 {code} {body}"

    # ★ 原局必须**完好** —— 报价被拒不能把存档弄坏
    after = c.get("/api/state", game="g1")[1]["state"]
    assert after["quarter"] == before["quarter"], "原局的季度被改了"
    assert len(after["plots"]) == 1, "原局的地块没了"
    assert abs(after["cash"] - before["cash"]) < TOL, "原局的现金变了"


def test_plots_appear_at_settlement_not_submission():
    """★ 地块是在**结算第 6 步**造出来的，不是提交时。

    ⚠️ 这条钉住一个容易误解的点：提交计划只是"记下意图"，
       第 2 步**不改状态**（不扣钱、不建地）。所以提交后 `/state`
       里仍然没有那块地，要等结算。
    """
    c = Client()
    c.new_game("g1")
    c.build("g1", "0,0")

    submitted = c.get("/api/state", game="g1")[1]["state"]
    assert submitted["plots"] == [], (
        "提交阶段不该出现地块（第 2 步只记录意图，不改状态）"
    )

    c.post("/api/end_quarter", {"game": "g1", "player": "p1"})
    settled = c.get("/api/state", game="g1")[1]["state"]
    assert len(settled["plots"]) == 1, "结算后应出现地块"


def test_bad_game_id_is_rejected():
    """非法 game_id（含路径穿越）→ 挡住，不落到文件名上。"""
    c = Client()
    code, body = c.post("/api/new_game", {"game_id": "../../evil"})
    assert code == 400, body


def test_broken_json_body_is_400():
    """请求体不是合法 JSON → 400（由 `_read_body` 抛 BadRequest）。"""
    from server.app import BadRequest, _error_payload

    code, body = _error_payload(BadRequest("请求体不是合法 JSON：xxx"))
    assert code == 400 and body["kind"] == "bad_request"


# ══════════════════════════════════════════════════════════════════
# 四、★ 只转发：服务层不许自己判规则
# ══════════════════════════════════════════════════════════════════


def test_plan_from_dict_is_pure_shape_conversion():
    """`plan_from_dict` **只做形状转换**，不做合法性判断。

    ⚠️ 非法商品 `ZZZ` 在这里要**原样放行**（转成 Plan 就好），
       由 `engine.validate` 去拒 —— 规则不许散到服务层。
    """
    p = plan_from_dict({
        "player_id": "p1",
        "purchases": {"ZZZ": 5.0},
        "food": {"F": 1.0},              # 也不能吃，同样放行
        "plot_plans": {"0,0": {"u": 0.5}},
        "builds": [{"plot_id": "1,1", "plot_type": "service", "target_level": 1}],
    })
    assert p.purchases == {"ZZZ": 5.0}, "不该在服务层拦商品"
    assert p.food == {"F": 1.0}
    assert p.plot_plans["0,0"].u == 0.5
    assert p.builds[0].plot_type.value == "service"


def test_plan_from_dict_rejects_unknown_plot_type():
    """未知地块类型 → 400（这是**形状**问题，不是规则问题）。"""
    from server.app import BadRequest

    try:
        plan_from_dict({"player_id": "p1", "builds": [
            {"plot_id": "0,0", "plot_type": "spaceship", "target_level": 1}]})
    except BadRequest as e:
        assert "spaceship" in str(e)
    else:
        raise AssertionError("未知地块类型应报 BadRequest")


def test_can_settle_comes_from_engine_not_server():
    """`can_settle` 由引擎给（`report.build_state`），服务层不自己判。"""
    c = Client()
    c.new_game("g1")
    # 没提交计划 → 引擎给出 blocker
    st = c.get("/api/state", game="g1")[1]["state"]
    assert st["can_settle"] is False
    assert st["settle_blockers"] == ["尚未提交计划"]


# ══════════════════════════════════════════════════════════════════
# 五、存档与活跃表
# ══════════════════════════════════════════════════════════════════


def test_games_list_shows_saved_game():
    c = Client()
    c.new_game("g1")
    games = c.get("/api/games")[1]["games"]
    assert len(games) == 1
    assert games[0]["game_id"] == "g1"
    assert games[0]["quarter"] == 1
    assert games[0]["active"] is True


def test_dropping_from_memory_reloads_from_storage():
    """★ 从活跃表丢掉后再取，应从**存档**恢复，且账目一致。"""
    c = Client()
    c.new_game("g1")
    c.build("g1", "0,0")
    c.post("/api/end_quarter", {"game": "g1", "player": "p1"})
    before = c.get("/api/state", game="g1")[1]["state"]

    c.registry.drop_from_memory("g1")
    after = c.get("/api/state", game="g1")[1]["state"]

    assert after["quarter"] == before["quarter"]
    assert abs(after["cash"] - before["cash"]) < TOL
    assert len(after["plots"]) == len(before["plots"])
    assert after["report"] is not None, "★ 读档后上季报告必须还在"


def test_registry_reuses_same_session_and_lock():
    """★ 同一个 game_id 必须复用**同一个 session**（锁才是同一把）。

    每次请求都新建 session = 每局一把新锁 = 那把锁形同虚设（§1.3.1）。
    """
    c = Client()
    c.new_game("g1")
    s1 = c.registry.get("g1")
    s2 = c.registry.get("g1")
    assert s1 is s2, "同一局必须复用同一个 GameSession"
    assert s1.lock is s2.lock, "锁必须是同一把"


# ══════════════════════════════════════════════════════════════════
# 命令行汇总
# ══════════════════════════════════════════════════════════════════


def _run_all() -> int:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    mod = sys.modules[__name__]
    names = sorted(n for n in dir(mod) if n.startswith("test_"))
    ok = fail = 0
    print("=" * 68)
    print("HTTP 层对账（只转发）")
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
