"""存档层 —— 引擎只认 `Storage` 协议，不知道背后是文件还是内存。

    from storage import JsonFileStorage, MemoryStorage, Storage

第一期用 `JsonFileStorage`（一个 JSON 文件一局）；
测试用 `MemoryStorage`（不碰磁盘）。
"""

from storage.base import (
    SCHEMA_VERSION,
    GameNotFound,
    SaveFormatError,
    Storage,
    StorageError,
    check_game_id,
)
from storage.json_file import JsonFileStorage
from storage.memory import MemoryStorage

__all__ = [
    "Storage",
    "StorageError",
    "GameNotFound",
    "SaveFormatError",
    "SCHEMA_VERSION",
    "check_game_id",
    "JsonFileStorage",
    "MemoryStorage",
]
