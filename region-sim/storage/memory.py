"""内存存档 —— 给测试用，**不碰磁盘**。

它存在的意义不是"能跑"，而是让测试**快且干净**：

  · 不写文件 → 测试跑在内存里，几百个用例也不慢
  · 不留产物 → 跑完磁盘上什么都没有，不会有残留存档互相污染
  · 可注入失败 → `fail_on_save` / `fail_on_load` 用来测服务层的错误分支

⚠️ 它**不是**"临时存档"：进程一退，里面就没了。
   真存档请用 `storage/json_file.py`。
"""

from __future__ import annotations

from copy import deepcopy

from engine.models import GameState
from storage.base import GameNotFound, check_game_id, pack, unpack


class MemoryStorage:
    """把存档放在一个 dict 里。接口与 `JsonFileStorage` **完全一致**。"""

    def __init__(self) -> None:
        self._store: dict[str, dict] = {}
        # 测试钩子：设成 True 时对应操作抛错，用来验服务层的错误分支
        self.fail_on_save = False
        self.fail_on_load = False

    def save(self, game_id: str, state: GameState, saved_at: str | None = None) -> None:
        if self.fail_on_save:
            raise OSError("（测试注入的写失败）")
        check_game_id(game_id)
        # ★ 深拷贝：否则"存档"与"内存里的活状态"是同一个对象，
        #   后面结算改了 state，存档也跟着变 —— 那样就测不出读档问题了。
        self._store[game_id] = deepcopy(pack(game_id, state, saved_at or "memory"))

    def load(self, game_id: str) -> GameState:
        if self.fail_on_load:
            raise OSError("（测试注入的读失败）")
        check_game_id(game_id)
        if game_id not in self._store:
            raise GameNotFound(f"存档不存在：{game_id!r}")
        return unpack(deepcopy(self._store[game_id]))

    def exists(self, game_id: str) -> bool:
        return game_id in self._store

    def delete(self, game_id: str) -> None:
        self._store.pop(game_id, None)

    def list_games(self) -> list[str]:
        return sorted(self._store)

    def peek(self, game_id: str) -> dict:
        if game_id not in self._store:
            raise GameNotFound(f"存档不存在：{game_id!r}")
        raw = self._store[game_id]
        return {
            "game_id": raw.get("game_id", game_id),
            "schema_version": raw.get("schema_version"),
            "saved_at": raw.get("saved_at"),
            "quarter": raw.get("quarter"),
            "total_quarters": raw.get("total_quarters"),
        }
