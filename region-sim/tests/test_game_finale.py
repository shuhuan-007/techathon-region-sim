"""「20 季终局」回归 —— 引擎 + 服务端骨架。

跑法（在 region-sim/ 下）：

    python tests/test_game_finale.py

或标准 unittest runner：

    python -m unittest tests.test_game_finale -v

依据任务书（子代理 A）：
  · `advance` 把每玩家 QuarterReport dict **追加**到 `state.history[player_id]`
    （按季升序，唯一来源，preview 与 advance 共用）。
  · 第 20 季结算后 `state.world.quarter == 21`，
    `is_game_over(21, 20) is True`，`_resolve_unlocked` 会把
    `state.world.phase` 切到 `WorldPhase.FINISHED`、`session.phase`
    切到 `SessionPhase.FINISHED`（"finished"）。
  · FINISHED 下 `submit / end_quarter / start_next_quarter` 全部抛
    `server.session.Conflict`；`/api/state` 与 `/api/map` 依旧 200。
  · `GameState.history` 进存档（v2 `SCHEMA_VERSION`），v1 旧档没有
    `history` 键 → `state.history == {}`（**不造数**）。
  · `session.finale_overview(player_id)` 只读聚合，不缺季时
    `quarters_completed == total_quarters` 且 `missing_quarters == []`；
    手工删一行 → 该季进 `missing_quarters` 且 `history_complete is False`。

⚠️ **20 季不真跑**：把 `state.world.total_quarters` 临时改小（=2），
    在「迷你局」上走 2 季结算到 `quarter == 3 > 2` 即终局——
    逻辑路径与 20 季完全一致，但跑测试不必真烧 20 次 `advance`。
    `total_quarters` 是 `Config.new_world()` 普通字段，改它不是改规则，
    而是测「引擎与服务层对 `is_game_over` 的反应」。
"""

from __future__ import annotations

import sys
import unittest
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import pipeline
from engine.clock import is_game_over
from engine.loader import Config, load_config
from engine.models import (
    GameState,
    Plan,
    PlotPlan,
    PlotType,
    SessionPhase,
    WorldPhase,
)
from server.app import App, dispatch
from server.registry import GameRegistry
from server.session import Conflict, GameSession
from storage import MemoryStorage
from storage.base import SCHEMA_VERSION, pack, unpack
from tests.test_m1 import make_state, mature, plain_plan
from tests.test_pipeline import cfg_with_example_elasticity

TOL = 1e-9


def _make_session(*, total_quarters: int = 20) -> tuple[GameSession, str]:
    """造一个 Resource L1 地块的单人局会话。`total_quarters` 可调小用于迷你局。"""
    cfg = cfg_with_example_elasticity()
    cfg.total_quarters = total_quarters
    plot = mature("0,0", PlotType.RESOURCE, 1)
    st = make_state(cfg, "p1", plot, cash=1000.0)
    st.world.total_quarters = total_quarters
    session = GameSession(state=st, cfg=cfg, player_ids=["p1"])
    return session, "p1"


class FinaleHistoryAppendTest(unittest.TestCase):
    """① `advance` 后 `state.history[player_id]` 追加 1 行，且追加项的
    `quarter` 与 `rep.to_dict()["quarter"]` 一致、含 `value_added`。"""

    def test_advance_appends_history_row(self) -> None:
        session, pid = _make_session()
        st = session.state
        self.assertEqual(st.history.get(pid, []), [])

        plan = plain_plan(pid, "0,0", u=1.0)
        rep = pipeline.advance(st, {pid: plan}, session.cfg)

        expected = rep.raw_reports[pid].to_dict() if hasattr(rep, "raw_reports") else None
        # advance 返回的是 state，rep dict 要从 raw_reports 拿
        rep_dict = st.raw_reports[pid].to_dict()

        history = st.history.get(pid, [])
        self.assertEqual(len(history), 1, "一次 advance 应恰好追加 1 行")
        row = history[0]
        self.assertIsInstance(row, dict)
        self.assertIn("quarter", row)
        self.assertIn("value_added", row)
        self.assertEqual(row["quarter"], rep_dict["quarter"])
        self.assertEqual(row["value_added"], rep_dict["value_added"])

    def test_advance_appends_per_quarter_ascending(self) -> None:
        """连跑 3 季 → 3 行，且 quarter 严格升序 1/2/3。"""
        session, pid = _make_session()
        st = session.state
        for expected_q in (1, 2, 3):
            pipeline.advance(st, {pid: plain_plan(pid, "0,0", u=1.0)}, session.cfg)
            rows = st.history.get(pid, [])
            self.assertEqual(len(rows), expected_q)
            self.assertEqual(rows[-1]["quarter"], expected_q)


class FinaleTransitionTest(unittest.TestCase):
    """② 打到「第 20 季」（迷你局用 total_quarters=2 模拟）结算后：
    `state.world.quarter` 推进、`is_game_over` 为 True、
    `_resolve_unlocked` 把 `state.world.phase` 与 `session.phase` 都切到 FINISHED。"""

    def test_resolve_marks_finished_at_total_quarters(self) -> None:
        session, pid = _make_session(total_quarters=2)
        st = session.state

        # Q1 → 结算（quarter 1→2，未到终局，phase 应仍在 REPORT）
        session.submit(pid, Plan(player_id=pid, plot_plans={
            "0,0": PlotPlan(plot_id="0,0", active=True, u=1.0),
        }))
        session.end_quarter(pid)
        self.assertEqual(st.world.quarter, 2)
        self.assertIs(session.phase, SessionPhase.REPORT)
        self.assertIs(st.world.phase, WorldPhase.RUNNING)
        self.assertFalse(is_game_over(st.world.quarter, st.world.total_quarters))

        # 月报看完进入下一季（Q2）
        session.start_next_quarter()
        self.assertIs(session.phase, SessionPhase.COLLECTING)

        # Q2 → 结算（quarter 2→3，**到终局**）
        session.submit(pid, Plan(player_id=pid, plot_plans={
            "0,0": PlotPlan(plot_id="0,0", active=True, u=1.0),
        }))
        session.end_quarter(pid)
        self.assertEqual(st.world.quarter, 3)
        self.assertTrue(is_game_over(st.world.quarter, st.world.total_quarters))
        self.assertEqual(session.phase.value, "finished")
        self.assertIs(session.phase, SessionPhase.FINISHED)
        self.assertIs(st.world.phase, WorldPhase.FINISHED)


class FinaleReadOnlyGuardTest(unittest.TestCase):
    """③ FINISHED 后写路径全部关闸，读路径仍通：
    `submit/end_quarter/start_next_quarter` 全部抛 `Conflict`；
    `/api/state` 与 `/api/map` 依旧 200，且 state 里
    `phase == "finished"`、`game_over is True`、`world_phase == "finished"`。"""

    def _advance_to_finished(self) -> tuple[App, GameRegistry, str, str]:
        """经 **App 真路径**（非引擎夹具）把一局迷你局推到 FINISHED。

        ⚠️ `/api/new_game` 出来的玩家**没有任何地块**——plan 里写 `0,0`
        会被 `validate.require_valid` 按 `unknown_plot` 拒掉（inherit 测试
        之前的写法踩过这个）。所以这里沿 `tests.test_m1.make_state` 的口径，
        在 new_game 后把 `mature("0,0", RESOURCE, 1)` 注入玩家，并
        把 `last_happiness` 设为 0.8（与 make_state 相同：E_幸福 = 1.00）。
        """
        cfg = cfg_with_example_elasticity()
        cfg.total_quarters = 2
        registry = GameRegistry(cfg, MemoryStorage())
        app = App(cfg, registry)
        gid = "g1"
        pid = "p1"
        code, body = dispatch(app, "POST", "/api/new_game", {},
                              {"game_id": gid, "player_id": pid})
        self.assertEqual(code, 200, body)
        session = registry.get(gid)

        # 注入一块成熟地块 & 与 make_state 同口径的开局面
        plot = mature("0,0", PlotType.RESOURCE, 1)
        session.state.players[pid].plots[plot.plot_id] = plot
        session.state.world.phase = WorldPhase.RUNNING
        session.state.players[pid].last_happiness = 0.8
        session.state.world.total_quarters = 2

        # advance 到终局
        for _ in range(2):
            code_s, _ = dispatch(app, "POST", "/api/submit", {},
                                 {"game": gid, "player": pid, "plan": {
                                     "player_id": pid,
                                     "plot_plans": {"0,0": {"active": True, "u": 1.0}},
                                 }})
            self.assertEqual(code_s, 200)
            code_e, _ = dispatch(app, "POST", "/api/end_quarter", {},
                                 {"game": gid, "player": pid})
            self.assertEqual(code_e, 200)
            # Q1 结算完是 REPORT，要 start_next；Q2 结算完是 FINISHED，不要 start_next
            if session.phase is SessionPhase.REPORT:
                dispatch(app, "POST", "/api/next_quarter", {},
                         {"game": gid, "player": pid})
        return app, registry, gid, pid

    def test_write_paths_raise_conflict(self) -> None:
        app, registry, gid, pid = self._advance_to_finished()
        session = registry.get(gid)
        self.assertIs(session.phase, SessionPhase.FINISHED)

        with self.assertRaises(Conflict):
            session.submit(pid, Plan(player_id=pid))
        with self.assertRaises(Conflict):
            session.end_quarter(pid)
        with self.assertRaises(Conflict):
            session.start_next_quarter()

    def test_read_paths_still_200(self) -> None:
        app, registry, gid, pid = self._advance_to_finished()

        code, body = dispatch(app, "GET", "/api/state",
                              {"game": gid, "player": pid}, {})
        self.assertEqual(code, 200, body)
        self.assertTrue(body["ok"])
        st = body["state"]
        self.assertEqual(st["phase"], "finished")
        self.assertIs(st["game_over"], True)
        self.assertEqual(st["world_phase"], "finished")

        code, body = dispatch(app, "GET", "/api/map",
                              {"game": gid, "player": pid}, {})
        self.assertEqual(code, 200, body)
        self.assertTrue(body["ok"])
        self.assertIn("map", body)

    def test_finale_endpoint_returns_overview(self) -> None:
        app, registry, gid, pid = self._advance_to_finished()
        code, body = dispatch(app, "GET", "/api/finale",
                              {"game": gid, "player": pid}, {})
        self.assertEqual(code, 200, body)
        self.assertTrue(body["ok"])
        fin = body["finale"]
        self.assertEqual(fin["scope"], "player")
        self.assertEqual(fin["player_id"], pid)
        self.assertIs(fin["game_over"], True)
        self.assertEqual(fin["quarter"], 3)
        self.assertEqual(fin["total_quarters"], 2)


class FinaleStorageRoundtripTest(unittest.TestCase):
    """④ pack/unpack 往返：`history` 完整保真（v2）；
    v1 旧档（无 history 键）→ `state.history == {}`，不造数。"""

    def test_pack_unpack_preserves_history(self) -> None:
        self.assertEqual(SCHEMA_VERSION, 2, "本任务把存档版本推到 v2")
        session, pid = _make_session()
        st = session.state
        for _ in range(3):
            pipeline.advance(st, {pid: plain_plan(pid, "0,0", u=1.0)}, session.cfg)
        self.assertEqual(len(st.history[pid]), 3)

        blob = pack("g1", st, "2026-10-18T00:00:00+08:00")
        self.assertEqual(blob["schema_version"], 2)
        self.assertIn("history", blob)
        self.assertIn(pid, blob["history"])
        self.assertEqual(len(blob["history"][pid]), 3)

        st2 = unpack(deepcopy(blob))
        self.assertEqual(st2.history.keys(), {pid})
        rows = st2.history[pid]
        self.assertEqual(len(rows), 3)
        self.assertEqual([r["quarter"] for r in rows], [1, 2, 3])
        # 行内容逐字段一致（不是同一对象、但深度相等）
        self.assertEqual(rows, st.history[pid])
        self.assertIsNot(rows, st.history[pid])

    def test_unpack_legacy_v1_without_history_gives_empty(self) -> None:
        """给一个**无 history 键**的 v1 dict：unpack 不报错且 history == {}。"""
        session, pid = _make_session()
        st = session.state
        pipeline.advance(st, {pid: plain_plan(pid, "0,0", u=1.0)}, session.cfg)

        blob = pack("g1", st, "2026-10-18T00:00:00+08:00")
        # 手工造一个 v1：去掉顶层 history，再把版本号写回 1
        legacy = deepcopy(blob)
        legacy["schema_version"] = 1
        legacy.pop("history", None)
        # 同时把 state 里内嵌的 history 也抹掉（v1 的 GameState.to_dict 没有它）
        legacy["state"].pop("history", None)

        st2 = unpack(legacy)
        self.assertEqual(st2.history, {}, "v1 旧档不应被造数")


class FinaleOverviewTest(unittest.TestCase):
    """⑤ `finale_overview`：完整时 quarters_completed == total_quarters、
    `missing_quarters == []`；手工删一行 → 进 `missing_quarters` 且
    `history_complete is False`。"""

    def _play_full_mini_game(self, total: int = 2) -> tuple[GameSession, str]:
        session, pid = _make_session(total_quarters=total)
        for _ in range(total):
            session.submit(pid, Plan(player_id=pid, plot_plans={
                "0,0": PlotPlan(plot_id="0,0", active=True, u=1.0),
            }))
            session.end_quarter(pid)
            if session.phase is SessionPhase.REPORT:
                session.start_next_quarter()
        return session, pid

    def test_full_mini_game_overview(self) -> None:
        total = 2
        session, pid = self._play_full_mini_game(total=total)
        fin = session.finale_overview(pid)
        self.assertIs(fin["game_over"], True)
        self.assertEqual(fin["quarters_completed"], total)
        self.assertEqual(fin["total_quarters"], total)
        self.assertEqual(fin["missing_quarters"], [])
        self.assertIs(fin["history_complete"], True)
        self.assertEqual(len(fin["history"]), total)
        self.assertEqual([r["quarter"] for r in fin["history"]], [1, 2])
        self.assertIsNotNone(fin["final_report"])
        self.assertEqual(fin["final_report"]["quarter"], total)

    def test_manual_delete_enters_missing_and_breaks_complete(self) -> None:
        total = 3
        session, pid = self._play_full_mini_game(total=total)
        # 手工删掉第 2 季那一行（模拟数据缺失）
        rows = session.state.history[pid]
        before = len(rows)
        session.state.history[pid] = [r for r in rows if r.get("quarter") != 2]
        self.assertEqual(len(session.state.history[pid]), before - 1)

        fin = session.finale_overview(pid)
        self.assertEqual(fin["quarters_completed"], total - 1)
        self.assertIn(2, fin["missing_quarters"])
        self.assertNotIn(1, fin["missing_quarters"])
        self.assertNotIn(3, fin["missing_quarters"])
        self.assertIs(fin["history_complete"], False)


if __name__ == "__main__":
    unittest.main()
