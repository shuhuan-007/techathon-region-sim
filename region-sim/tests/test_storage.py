"""存档对账 —— 往返一致、原子写、防路径穿越。

跑法（在 region-sim/ 下）：

    python tests/test_storage.py

依据《项目架构-总》§八。接口在 `storage/base.py`，实现见
`storage/json_file.py`（第一期）与 `storage/memory.py`（测试用）。

★ 最重要的一条是 `test_roundtrip_preserves_report`：
  **读档后"上季报告"必须还在** —— `GameState.to_dict()` 里没有它，
  不单独存的话读档后财报就消失了。
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine import pipeline
from engine.loader import load_config
from engine.models import (
    GameState,
    Plan,
    PlayerState,
    Plot,
    PlotPlan,
    PlotStatus,
    PlotType,
    WorldPhase,
)
from storage import (
    SCHEMA_VERSION,
    GameNotFound,
    JsonFileStorage,
    MemoryStorage,
    SaveFormatError,
    Storage,
    StorageError,
    check_game_id,
)

TOL = 1e-9


# ══════════════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════════════


def fresh_state():
    """一个开局状态（没结算过）。"""
    cfg = load_config()
    plot = Plot(plot_id="0,0", plot_type=PlotType.RESOURCE, level=1,
                status=PlotStatus.RUNNING, running_quarters=99)
    player = PlayerState(player_id="p1", cash=1000.0, plots={"0,0": plot})
    player.last_happiness = 0.8
    world = cfg.new_world()
    world.phase = WorldPhase.RUNNING
    return cfg, GameState(world=world, players={"p1": player})


def played_state():
    """一个**结算过一季**的状态（有 price_history、有 report）。"""
    cfg, st = fresh_state()
    plan = Plan(player_id="p1", plot_plans={"0,0": PlotPlan(plot_id="0,0", u=1.0)})
    pipeline.advance(st, {"p1": plan}, cfg)
    return cfg, st


class tmp_storage:
    """上下文管理器：一个临时目录里的 `JsonFileStorage`。"""

    def __enter__(self):
        self._dir = tempfile.TemporaryDirectory()
        self.root = Path(self._dir.name) / "saves"
        return JsonFileStorage(self.root)

    def __exit__(self, *exc):
        self._dir.cleanup()
        return False


# ══════════════════════════════════════════════════════════════════
# 一、★ 往返一致（本协议唯一的硬要求）
# ══════════════════════════════════════════════════════════════════


def test_memory_roundtrip():
    """内存存档往返一致。"""
    cfg, st = played_state()
    s = MemoryStorage()
    s.save("g1", st)
    assert s.load("g1").to_dict() == st.to_dict()


def test_json_roundtrip():
    """JSON 存档往返一致。"""
    cfg, st = played_state()
    with tmp_storage() as s:
        s.save("g1", st)
        assert s.load("g1").to_dict() == st.to_dict()


def test_roundtrip_preserves_report():
    """★★ **读档后"上季报告"必须还在**。

    ⚠️ `GameState.to_dict()` 里**没有** `report` / `raw_reports`。
       如果存档只写 `state.to_dict()`，读档后 `state.report` 就是空的 ——
       玩家点"📋 上季报告"会看到一片空白，以为程序把账吞了。
    """
    cfg, st = played_state()
    assert st.report, "夹具本身应该有上一季报告"
    original = json.loads(json.dumps(st.report))   # 转成纯 JSON 形态再比

    for s in (MemoryStorage(), None):
        if s is None:
            with tmp_storage() as js:
                js.save("g1", st)
                back = js.load("g1")
        else:
            s.save("g1", st)
            back = s.load("g1")

        assert back.report == original, (
            "★ 读档后 report 丢了或有变 —— 上季报告会显示不出来"
        )
        assert "p1" in back.report, "report 要按 player_id 索引（红线 1）"


def test_roundtrip_preserves_price_history():
    """`price_history` 必须一起存 —— 它是"上季价"，买入记账要用。"""
    cfg, st = played_state()
    assert st.world.price_history, "结算过一季就该有价格历史"

    with tmp_storage() as s:
        s.save("g1", st)
        back = s.load("g1")
        assert back.world.price_history == st.world.price_history
        # 上季价 = 参考价之外的真实成交价（能查出"上季价"这件事本身）
        assert abs(back.world.last_price("F") - st.world.last_price("F")) < TOL


def test_roundtrip_preserves_plots_and_cash():
    """地块（含产线/状态/磨合）与现金一字不差。"""
    cfg, st = played_state()
    with tmp_storage() as s:
        s.save("g1", st)
        back = s.load("g1")

        p0, p1 = st.players["p1"], back.players["p1"]
        assert abs(p0.cash - p1.cash) < TOL
        assert abs(p0.last_happiness - p1.last_happiness) < TOL
        assert set(p0.plots) == set(p1.plots)
        for pid in p0.plots:
            assert p0.plots[pid].to_dict() == p1.plots[pid].to_dict()


def test_roundtrip_survives_second_quarter():
    """★ 读档后**接着往下跑**，结果与不存档一路跑到底完全一致。

    这条比"字段相等"更强：它证明存档没有丢掉任何**会影响后续计算**的东西。
    """
    cfg = fresh_state()[0]

    # 甲：跑两季，中途存档再读回
    _, st_a = fresh_state()
    plan = lambda: Plan(player_id="p1",
                        plot_plans={"0,0": PlotPlan(plot_id="0,0", u=1.0)})
    pipeline.advance(st_a, {"p1": plan()}, cfg)

    with tmp_storage() as s:
        s.save("g1", st_a)
        st_a = s.load("g1")

    pipeline.advance(st_a, {"p1": plan()}, cfg)

    # 乙：一口气跑两季，不存档
    _, st_b = fresh_state()
    pipeline.advance(st_b, {"p1": plan()}, cfg)
    pipeline.advance(st_b, {"p1": plan()}, cfg)

    assert st_a.world.quarter == st_b.world.quarter
    assert abs(st_a.players["p1"].cash - st_b.players["p1"].cash) < TOL, (
        f"存档往返后接着跑，现金 {st_a.players['p1'].cash} "
        f"≠ 不存档跑到同一季的 {st_b.players['p1'].cash}"
    )
    assert st_a.world.price_history == st_b.world.price_history


# ══════════════════════════════════════════════════════════════════
# 二、★ 不存在的局必须报错（不返回空局）
# ══════════════════════════════════════════════════════════════════


def test_load_missing_raises_not_empty_game():
    """★ 读一个不存在的局 → `GameNotFound`，**绝不返回空局**。

    ⚠️ 返回空局会把"存档丢了"伪装成"这是一局新游戏" ——
       玩家会以为进度被重置，而日志里什么都没有。这是最难查的一类 bug。
    """
    with tmp_storage() as s:
        try:
            s.load("nope")
        except GameNotFound:
            pass
        else:
            raise AssertionError("★ 不存在的局必须抛 GameNotFound，不能返回空局")

    m = MemoryStorage()
    try:
        m.load("nope")
    except GameNotFound:
        pass
    else:
        raise AssertionError("MemoryStorage 同理")


def test_exists_agrees_with_load():
    """`exists()` 与 `load()` 的结论必须一致。"""
    cfg, st = played_state()
    with tmp_storage() as s:
        assert s.exists("g1") is False
        s.save("g1", st)
        assert s.exists("g1") is True
        s.load("g1")          # 不抛错
        s.delete("g1")
        assert s.exists("g1") is False


# ══════════════════════════════════════════════════════════════════
# 三、★ game_id 校验（防路径穿越）
# ══════════════════════════════════════════════════════════════════


def test_game_id_rejects_path_traversal():
    """★★ `game_id` 会变成**文件名**，所以必须校验。

    不校验的话，`load("../../secret")` 就是任意文件读取。
    """
    for bad in ("../evil", "..\\evil", "a/b", "a\\b", ".", "..", "",
                "g1.json", "a b", "存档", "a" * 65):
        try:
            check_game_id(bad)
        except ValueError:
            continue
        raise AssertionError(f"★ 非法 game_id 应被拒绝：{bad!r}")


def test_game_id_accepts_safe_ids():
    """正常 ID 要放行（字母/数字/下划线/连字符）。"""
    for good in ("g1", "game_1", "game-1", "P1", "a" * 64, "0"):
        assert check_game_id(good) == good


def test_storage_rejects_bad_game_id_end_to_end():
    """`save` / `load` 也走校验 —— 不能只在 `check_game_id` 里挡。"""
    cfg, st = played_state()
    with tmp_storage() as s:
        for method in (lambda: s.save("../evil", st), lambda: s.load("../evil")):
            try:
                method()
            except ValueError:
                continue
            raise AssertionError("★ 非法 game_id 必须在 save/load 就被拒绝")


def test_traversal_cannot_escape_root():
    """★ 端到端：确认"往上级目录写"这件事**真的做不到**。"""
    cfg, st = played_state()
    with tmp_storage() as s:
        try:
            s.save("../../../pwned", st)
        except ValueError:
            pass
        else:
            raise AssertionError("★ 路径穿越没被挡住")
        # 上级目录里不该出现任何东西
        assert not (s.root.parent / "pwned.json").exists()


# ══════════════════════════════════════════════════════════════════
# 四、存档格式错误
# ══════════════════════════════════════════════════════════════════


def test_corrupt_json_raises_clear_error():
    """半截 JSON（写到一半崩溃）→ 明确报错，不是崩在 json 解析里。"""
    cfg, st = played_state()
    with tmp_storage() as s:
        s.save("g1", st)
        s.path_of("g1").write_text('{"state": {"world":', encoding="utf-8")
        try:
            s.load("g1")
        except SaveFormatError as e:
            assert "JSON" in str(e)
        else:
            raise AssertionError("半截 JSON 应抛 SaveFormatError")


def test_missing_schema_version_is_rejected():
    """★ 没有 `schema_version` → 拒绝，**不猜版本**。"""
    cfg, st = played_state()
    with tmp_storage() as s:
        s.save("g1", st)
        raw = json.loads(s.path_of("g1").read_text(encoding="utf-8"))
        del raw["schema_version"]
        s.path_of("g1").write_text(json.dumps(raw), encoding="utf-8")
        try:
            s.load("g1")
        except SaveFormatError as e:
            assert "schema_version" in str(e)
        else:
            raise AssertionError("缺 schema_version 应抛 SaveFormatError")


def test_future_schema_version_is_rejected():
    """存档版本比程序新 → 拒绝（用旧程序开新档会丢字段）。"""
    cfg, st = played_state()
    with tmp_storage() as s:
        s.save("g1", st)
        raw = json.loads(s.path_of("g1").read_text(encoding="utf-8"))
        raw["schema_version"] = SCHEMA_VERSION + 1
        s.path_of("g1").write_text(json.dumps(raw), encoding="utf-8")
        try:
            s.load("g1")
        except SaveFormatError as e:
            assert "还新" in str(e) or "版本" in str(e)
        else:
            raise AssertionError("未来版本应抛 SaveFormatError")


def test_garbage_payload_is_rejected():
    """合法 JSON 但不是存档（缺 `state`）→ 明确报错。"""
    cfg, st = played_state()
    with tmp_storage() as s:
        s.path_of("g1").write_text('{"hello": 1}', encoding="utf-8")
        try:
            s.load("g1")
        except SaveFormatError as e:
            assert "state" in str(e)
        else:
            raise AssertionError("非存档内容应抛 SaveFormatError")


# ══════════════════════════════════════════════════════════════════
# 五、★ 原子写
# ══════════════════════════════════════════════════════════════════


def test_save_leaves_no_tmp_file():
    """写完后**不留临时文件**（否则存档目录会积垃圾）。"""
    cfg, st = played_state()
    with tmp_storage() as s:
        s.save("g1", st)
        leftovers = list(s.root.glob("*.tmp"))
        assert leftovers == [], f"不该留下临时文件：{leftovers}"


def test_overwrite_is_atomic_and_old_survives_failure():
    """★ 覆盖写失败时，**旧存档仍然可用**。

    这正是"临时文件 + 原子替换"要保的东西：
    直接 `open(path, "w")` 会先把旧档清空，一旦随后失败，
    新旧两版**都没了**。

    这里用"临时文件写不进去"来模拟失败（把 tmp 路径占成目录）。
    """
    cfg, st = played_state()
    with tmp_storage() as s:
        s.save("g1", st)
        good = s.load("g1")

        # 把 tmp 路径变成一个目录 → 写临时文件必然失败
        tmp = s.path_of("g1").with_suffix(".json.tmp")
        tmp.mkdir()

        try:
            s.save("g1", st)
        except Exception:
            pass
        else:
            raise AssertionError("写不进去时应该抛错")

        # ★ 关键：旧存档还在、还能读
        assert s.exists("g1"), "★ 写失败不该把旧存档弄丢"
        assert s.load("g1").to_dict() == good.to_dict(), "★ 旧存档内容必须完好"


def test_save_overwrites_previous():
    """再次 save 覆盖旧的（同一局推进一季后存回）。"""
    cfg, st = played_state()
    with tmp_storage() as s:
        s.save("g1", st)
        q1 = s.load("g1").world.quarter

        st2 = s.load("g1")
        pipeline.advance(st2, {"p1": Plan(player_id="p1", plot_plans={
            "0,0": PlotPlan(plot_id="0,0", u=1.0)})}, cfg)
        s.save("g1", st2)

        back = s.load("g1")
        assert back.world.quarter == q1 + 1
        assert back.world.quarter == st2.world.quarter


# ══════════════════════════════════════════════════════════════════
# 六、列表与删除
# ══════════════════════════════════════════════════════════════════


def test_list_games_sorted():
    """列表**排序**（确定性）。"""
    cfg, st = played_state()
    with tmp_storage() as s:
        for gid in ("g3", "g1", "g2"):
            s.save(gid, st)
        assert s.list_games() == ["g1", "g2", "g3"]


def test_delete_is_idempotent():
    """删除**幂等**：删两次不报错。"""
    cfg, st = played_state()
    with tmp_storage() as s:
        s.save("g1", st)
        s.delete("g1")
        s.delete("g1")          # 第二次不该抛
        assert s.list_games() == []


def test_peek_returns_metadata_without_state():
    """`peek()` 只给元数据（列表页用），不含整个 state。"""
    cfg, st = played_state()
    with tmp_storage() as s:
        s.save("g1", st)
        info = s.peek("g1")
        assert set(info) == {"game_id", "schema_version", "saved_at",
                             "quarter", "total_quarters"}
        assert info["game_id"] == "g1"
        assert info["quarter"] == st.world.quarter
        assert info["schema_version"] == SCHEMA_VERSION


# ══════════════════════════════════════════════════════════════════
# 七、内存实现的两个特性
# ══════════════════════════════════════════════════════════════════


def test_memory_storage_deep_copies():
    """★ 内存存档要**深拷贝**：存完之后改活状态，存档不该跟着变。

    不深拷贝的话，"存档"与"活状态"是同一个对象，
    测试就测不出任何读档问题（永远"一致"）。
    """
    cfg, st = played_state()
    s = MemoryStorage()
    s.save("g1", st)
    before = s.load("g1").players["p1"].cash

    st.players["p1"].cash += 9999          # 改活状态
    assert abs(s.load("g1").players["p1"].cash - before) < TOL, (
        "★ 存档被活状态污染了 —— 说明没有深拷贝"
    )


def test_memory_storage_failure_hooks():
    """注入失败钩子可用（服务层要拿它测错误分支）。"""
    cfg, st = played_state()
    s = MemoryStorage()
    s.fail_on_save = True
    try:
        s.save("g1", st)
    except OSError:
        pass
    else:
        raise AssertionError("fail_on_save 应生效")

    s.fail_on_save = False
    s.save("g1", st)
    s.fail_on_load = True
    try:
        s.load("g1")
    except OSError:
        pass
    else:
        raise AssertionError("fail_on_load 应生效")


# ══════════════════════════════════════════════════════════════════
# 八、协议一致性
# ══════════════════════════════════════════════════════════════════


def test_both_implementations_satisfy_protocol():
    """两个实现都满足 `Storage` 协议（接口可替换）。"""
    cfg, st = played_state()
    with tmp_storage() as js:
        assert isinstance(js, Storage), "JsonFileStorage 应满足 Storage 协议"
    assert isinstance(MemoryStorage(), Storage), "MemoryStorage 应满足 Storage 协议"


def test_error_hierarchy():
    """错误类型关系：`GameNotFound` / `SaveFormatError` 都是 `StorageError`。

    调用方可以只 catch `StorageError` 兜住全部存档问题。
    """
    assert issubclass(GameNotFound, StorageError)
    assert issubclass(SaveFormatError, StorageError)


def test_root_directory_is_created():
    """存档目录不存在时**自动创建**（少一个手工步骤）。"""
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "deep" / "nested" / "saves"
        assert not root.exists()
        JsonFileStorage(root)
        assert root.exists()


# ══════════════════════════════════════════════════════════════════
# 命令行汇总
# ══════════════════════════════════════════════════════════════════


def _run_all() -> int:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    mod = sys.modules[__name__]
    names = sorted(n for n in dir(mod) if n.startswith("test_"))
    ok = fail = 0
    print("=" * 68)
    print("存档对账")
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
