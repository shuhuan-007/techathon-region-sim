"""JSON 文件存档 —— 第一期实现：**一个文件一局**。

依据《项目架构-总》§八。接口见 `storage/base.py`。

═══════════════════════════════════════════════════════════════════
文件布局
═══════════════════════════════════════════════════════════════════

    <root>/g1.json          一局一个文件
    <root>/g2.json
    ...

文件名 = `<game_id>.json`。`game_id` 经 `check_game_id()` 校验
（防路径穿越，见 `base.py`）。

═══════════════════════════════════════════════════════════════════
★ 原子写：为什么不能直接 open(path, "w")
═══════════════════════════════════════════════════════════════════

直接写会有一个**真实的丢档窗口**：

    打开文件 → 清空 → 写一半 → ⚡断电/崩溃 → 文件是**半截 JSON**

结果不只是"这一季没了"，而是**整个存档读不出来**（JSON 解析失败），
玩家几十季的进度一起没。

所以走"写临时文件 + 原子替换"：

    ① 写 `<name>.json.tmp`
    ② `os.replace(tmp, 目标)`   ← 同一文件系统内**原子**操作

`os.replace` 在 Windows 与 POSIX 上都是原子的（要么旧文件、要么新文件，
不会出现半截）。Windows 上 `os.replace` 允许覆盖已存在的目标，
所以这里不需要先删。

⚠️ 临时文件必须与目标**在同一个目录**，否则跨文件系统时 `os.replace`
   可能退化成"复制+删除"，就又不原子了。

═══════════════════════════════════════════════════════════════════
时间戳
═══════════════════════════════════════════════════════════════════

`saved_at` 用**服务层传进来的**时间（默认取当前时间）。
存储层不参与任何计算，时间戳只是给人看的元数据。
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from engine.models import GameState
from storage.base import (
    GameNotFound,
    SaveFormatError,
    check_game_id,
    pack,
    unpack,
)


class JsonFileStorage:
    """把每局存成一个 JSON 文件。

    参数
    ----
    root : 存档目录。不存在时**自动创建**。
    indent : 写文件时的缩进。默认 `2`（人能读，便于对账）；
             `None` = 最紧凑。
    """

    def __init__(self, root: str | Path, indent: int | None = 2) -> None:
        self.root = Path(root)
        self.indent = indent
        self.root.mkdir(parents=True, exist_ok=True)

    # ── 路径 ──────────────────────────────────────────────

    def path_of(self, game_id: str) -> Path:
        """这一局的文件路径。**先校验 game_id**（防路径穿越）。"""
        return self.root / f"{check_game_id(game_id)}.json"

    # ── 协议实现 ──────────────────────────────────────────

    def save(self, game_id: str, state: GameState, saved_at: str | None = None) -> None:
        """写档。**原子**（临时文件 + `os.replace`，见模块头）。"""
        target = self.path_of(game_id)
        payload = pack(game_id, state, saved_at or _now())

        # 临时文件与目标**同目录** —— 跨文件系统时 os.replace 会失去原子性
        tmp = target.with_suffix(target.suffix + ".tmp")
        try:
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                json.dump(payload, f, ensure_ascii=False, indent=self.indent)
                f.flush()
                os.fsync(f.fileno())      # ★ 落盘再替换，别让断电吃掉
            os.replace(tmp, target)       # ★ 原子替换
        except Exception:
            # 失败时清掉临时文件，别在存档目录里留垃圾
            tmp.unlink(missing_ok=True)
            raise

    def load(self, game_id: str) -> GameState:
        """读档。不存在 → `GameNotFound`（**不返回空局**）。"""
        target = self.path_of(game_id)
        if not target.exists():
            raise GameNotFound(
                f"存档不存在：{game_id!r}（找的是 {target}）\n"
                f"  ⚠️ 不返回空局 —— 那会把『存档丢了』伪装成『这是一局新游戏』。"
            )

        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise SaveFormatError(
                f"存档 {game_id!r} 不是合法 JSON：{e}\n"
                f"  文件：{target}\n"
                f"  ⚠️ 可能是上一次写入中途崩溃留下的半截文件。"
            ) from None

        return unpack(raw)

    def exists(self, game_id: str) -> bool:
        return self.path_of(game_id).exists()

    def delete(self, game_id: str) -> None:
        """删除。**幂等**（不存在也不报错）。"""
        self.path_of(game_id).unlink(missing_ok=True)

    # ── 列表页要用 ────────────────────────────────────────

    def list_games(self) -> list[str]:
        """全部局 ID（**排序**，保证确定性）。

        ⚠️ 只读每个文件的**头部元数据**，不解析 `state` ——
           几十季的存档可能很大，列表页不该把它整个读进内存。
        """
        out: list[str] = []
        for p in sorted(self.root.glob("*.json")):
            out.append(p.stem)
        return out

    def peek(self, game_id: str) -> dict:
        """只取元数据（不还原 `GameState`），供列表页显示。"""
        target = self.path_of(game_id)
        if not target.exists():
            raise GameNotFound(f"存档不存在：{game_id!r}")
        raw = json.loads(target.read_text(encoding="utf-8"))
        return {
            "game_id": raw.get("game_id", game_id),
            "schema_version": raw.get("schema_version"),
            "saved_at": raw.get("saved_at"),
            "quarter": raw.get("quarter"),
            "total_quarters": raw.get("total_quarters"),
        }


def _now() -> str:
    """本地时间的 ISO 字符串（带时区），只作元数据。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")
