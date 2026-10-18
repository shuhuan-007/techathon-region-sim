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


def _list(storage: Storage) -> list[str]:
    """取全部局 ID。`Storage` 协议没要求 `list_games`，所以这里兜一下。"""
    lister = getattr(storage, "list_games", None)
    if lister is None:
        raise TypeError(
            f"{type(storage).__name__} 没有实现 list_games()，无法列出对局。\n"
            f"  → 列表页需要它；`JsonFileStorage` 与 `MemoryStorage` 都有。"
        )
    return lister()
