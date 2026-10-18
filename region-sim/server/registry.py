"""对局注册表 —— `game_id` → `GameSession`，带存档。

《项目架构-总》§八：**每局一把锁、一个会话实例**。
本模块负责"把局找出来 / 建出来 / 存下去"，**不含任何规则**。

═══════════════════════════════════════════════════════════════════
为什么要有"活跃局"这层缓存
═══════════════════════════════════════════════════════════════════

`GameSession` 持有**每局一把锁**（§1.3.1），而锁必须**跨请求复用** ——
每个请求都新建一个 session，就等于每局一把新锁，那把锁形同虚设。

所以：

    game_id ──→ 活跃表里有？ ──有──→ 直接用（锁是同一把）
                    │
                    没有
                    ↓
              从存档读出来 → 放进活跃表 → 用

⚠️ **活跃表与存档是两回事**：
   活跃表是"现在内存里跑着的局"，存档是磁盘上的快照。
   结算完自动落盘（`session._save_unlocked`），所以两者不会长期分叉。
"""

from __future__ import annotations

import threading

from engine.loader import Config
from engine.models import GameState
from server.session import GameSession
from server.slots import (
    auto_quarter_slot_id,
    is_slot_of,
    make_slot_id,
    parse_slot_id,
)
from storage.base import Storage, check_game_id


class GameRegistry:
    """管理所有对局。**线程安全**（内部一把表锁）。

    ⚠️ 表锁只保护"活跃表"这个 dict，**不保护对局本身** ——
       对局的并发由 `GameSession.lock` 负责（每局一把）。
       两把锁的分工不要混。
    """

    def __init__(self, cfg: Config, storage: Storage) -> None:
        self.cfg = cfg
        self.storage = storage
        self._sessions: dict[str, GameSession] = {}
        self._lock = threading.RLock()

    # ── 建局 ──────────────────────────────────────────────

    def create(self, game_id: str, player_id: str = "p1") -> GameSession:
        """开一局新的：开局世界 + 开局玩家（**零地块**，§5.3 地图全是可建空地）。

        ⚠️ 已存在的 `game_id` **拒绝覆盖** —— 默默覆盖会毁掉别人的存档。
        """
        check_game_id(game_id)
        with self._lock:
            if self.storage.exists(game_id):
                raise ValueError(
                    f"对局 {game_id!r} 已存在 —— 不覆盖。\n"
                    f"  → 想重开请换一个 game_id，或先显式删除存档。"
                )
            state = GameState(
                world=self.cfg.new_world(),
                players={player_id: self.cfg.new_player(player_id)},
            )
            session = GameSession(
                state=state, cfg=self.cfg,
                storage=self.storage, game_id=game_id,
            )
            # ★ 2026-10-18 S1：新开局在 Q1 发一次季初补贴（early 档，默认 1200）。
            #   幂等来自"创建路径**只走这里一次**"；加载路径（下面的 get()）
            #   特意**不调** —— 旧档现金已含历次补贴，不补发。
            session.credit_opening_subsidy()
            # 开局就落一次盘，这样"我建过这局"这件事立刻可见
            session.save_now()
            self._sessions[game_id] = session
            return session

    # ── 取局 ──────────────────────────────────────────────

    def get(self, game_id: str) -> GameSession:
        """取出一个会话。活跃表里没有就从存档读出来。

        存档也没有 → `GameNotFound`（**不偷偷建一局新的**）。
        """
        check_game_id(game_id)
        with self._lock:
            cached = self._sessions.get(game_id)
            if cached is not None:
                return cached

            state = self.storage.load(game_id)      # 不存在会抛 GameNotFound
            session = GameSession(
                state=state, cfg=self.cfg,
                storage=self.storage, game_id=game_id,
            )
            self._sessions[game_id] = session
            return session

    # ── 列表 / 删除 ───────────────────────────────────────

    def list_games(self) -> list[dict]:
        """全部存档的元数据（排序，确定性）。**只读元数据，不还原状态。**"""
        out: list[dict] = []
        for game_id in sorted(_list(self.storage)):
            try:
                info = self.storage.peek(game_id)
            except Exception:                       # noqa: BLE001
                # 单个存档坏了不该让整个列表页崩 —— 标记出来即可
                info = {"game_id": game_id, "error": "存档损坏，读不出元数据"}
            info["active"] = game_id in self._sessions
            out.append(info)
        return out

    def delete(self, game_id: str) -> None:
        """删掉一局（活跃表 + 存档）。"""
        check_game_id(game_id)
        with self._lock:
            self._sessions.pop(game_id, None)
            self.storage.delete(game_id)

    def drop_from_memory(self, game_id: str) -> None:
        """只从活跃表移除（存档保留）。用来测"读档"路径。"""
        with self._lock:
            self._sessions.pop(game_id, None)

    # ── 多档存档（slot） ─────────────────────────────────

    def save_slot(self, game_id: str, slot: str,
                  player_id: str = "p1") -> str:
        """把 `game_id` 当前状态**另存一份**为 `<game_id>@<slot>`。

        返回拼好的 slot_id（写盘的就是这个键）。

        ⚠️ 这是对当前**活跃状态**做快照 —— 若该局的 `GameSession`
           还在内存里，就用内存里的（已含未落盘的最新状态）；
           若不在，`get()` 会先从存档加载一份再快照。
           两种情况**都在该局的锁内**读，避免"读一半结算一半"。

        ⚠️ **不**改原局任何字段：slot 是只读快照，不是分支。
        """
        check_game_id(game_id)               # 原局 id 也走安全闸
        sess = self.get(game_id)             # 懒加载、挂锁
        slot_id = make_slot_id(game_id, slot)
        with sess.lock:
            self.storage.save(slot_id, sess.state)
        return slot_id

    def load_slot_as(self, slot_id: str, as_game_id: str) -> GameSession:
        """从 `slot_id` 恢复，**挂载成新局** `as_game_id`。

        三条语义（一次说清，别埋 bug）：

          ① **原 slot 档不动** —— slot_id 对应的存档文件**不删不改**，
             以后还能再 `load_slot_as` 同一份。
          ② `as_game_id` 与 slot_id 是**两局** —— 前者是"现在内存里
             跑着的局"（也会走 `_save_unlocked` 落盘到自己的 json），
             后者只是"曾经的一份快照"。
          ③ `GameState` 本身**没有 `game_id` 字段**（见 engine/models.py
             807 行起，只是 world/players/contracts/report/history），
             所以挂载时**不需要给 state 改名**；只需把 `GameSession` 的
             `game_id` 设成 `as_game_id`，后续 `_save_unlocked` 就会
             写到 `as_game_id` 的文件里。

        ⚠️ `as_game_id` 已在活跃表 → 幂等返回现有那局（不覆盖）。
        ⚠️ `as_game_id` 在存档里**已有同名的普通局** → 报错不覆盖
           （这是 `create()` 已有的保护语义，load_slot_as 也沿用）。
        """
        # 防止把 load 结果又盖回 slot 本身；也防止 as_game_id 含 @ 弄出
        # "slot 的 slot"（虽 check_game_id 允许 @，业务层拒绝更直观）。
        if "@" in as_game_id:
            raise ValueError(
                f"as_game_id 不能含 '@'（slot 不能作为新局 id）：{as_game_id!r}\n"
                f"  → 想给 slot 再起一档，请先起一个普通名作为新局，"
                f"   再对该新局调 save_slot。"
            )
        if "@" not in slot_id:
            raise ValueError(
                f"slot_id 形状不对（必须含 '@'）：{slot_id!r}"
            )
        check_game_id(as_game_id)
        check_game_id(slot_id)

        with self._lock:
            cached = self._sessions.get(as_game_id)
            if cached is not None:
                return cached                   # 幂等：不覆盖活跃局
            if self.storage.exists(as_game_id):
                raise ValueError(
                    f"as_game_id {as_game_id!r} 已有存档 —— 拒绝覆盖。\n"
                    f"  → 想重开一个 => 换个 as_game_id；想重现 slot => "
                    f"先 delete 那个 as_game_id。"
                )
            state = self.storage.load(slot_id)  # 不存在会抛 GameNotFound
            session = GameSession(
                state=state, cfg=self.cfg,
                storage=self.storage, game_id=as_game_id,
            )
            # ★ 立刻落一份 as_game_id 自己的存档 —— 避免"load 出 slot 后
            #   内存里有、磁盘上没有"，下次重启又找不到。
            session.save_now()
            self._sessions[as_game_id] = session
            return session

    def list_slots(self, game_id: str) -> list[dict]:
        """列出某局名下全部 slot 的元数据（**只读元数据**，**不还原状态**）。

        形状与 `list_games` 每行一致（`peek()` 给的字段），再加两个
        slot 专属键：
          · `slot`     —— slot 名（`@` 后那一段）；
          · `is_auto`  —— 是不是 `<game_id>@q<N>` 形（session 自动季度档）。

        ⚠️ 只能过滤 `JsonFileStorage` 的目录；其它 `Storage` 实现
           （`MemoryStorage` 等）走 `list_games()` 全量再过滤 ——
           两个分支结果形状相同。
        """
        check_game_id(game_id)
        out: list[dict] = []
        for fid in _list(self.storage):          # 现有全量列出（带兜底）
            if not is_slot_of(fid, game_id):
                continue
            _, slot_name = parse_slot_id(fid)
            try:
                meta = self.storage.peek(fid)
            except Exception:                    # noqa: BLE001
                meta = {"game_id": fid, "error": "存档损坏，读不出元数据"}
            meta["slot"] = slot_name
            meta["is_auto"] = (
                slot_name.startswith("q") and slot_name[1:].isdigit()
            )
            meta["active"] = False               # slot 永远是"被恢复的一方"
            out.append(meta)
        return out


def _list(storage: Storage) -> list[str]:
    """取全部局 ID。`Storage` 协议没要求 `list_games`，所以这里兜一下。"""
    lister = getattr(storage, "list_games", None)
    if lister is None:
        raise TypeError(
            f"{type(storage).__name__} 没有实现 list_games()，无法列出对局。\n"
            f"  → 列表页需要它；`JsonFileStorage` 与 `MemoryStorage` 都有。"
        )
    return lister()
