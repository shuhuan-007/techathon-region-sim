"""存档接口 —— **引擎只认这个协议，不知道背后是文件还是数据库**。

依据《项目架构-总》§八（存档）与《总文件架构》。

═══════════════════════════════════════════════════════════════════
为什么要有这一层
═══════════════════════════════════════════════════════════════════

第一期实现就是"一个 JSON 文件一局"。那为什么不直接在 `server/` 里写文件？

因为**引擎与存储必须解耦**：

  · 引擎是**纯函数**（输入 state → 输出 state），它不该知道"文件"这回事
  · 换存储（内存 / 文件 / 以后的数据库）**不该动引擎一行**
  · 测试要能用一个**不碰磁盘**的实现跑起来

所以：`engine` 与 `server` 只依赖本文件定义的 `Storage` **协议**，
具体实现（`json_file` / `memory`）可以随时替换。

═══════════════════════════════════════════════════════════════════
四个方法，就这么多
═══════════════════════════════════════════════════════════════════

    save(game_id, state)     存
    load(game_id) -> state   取（不存在时**明确报错**，不返回空局）
    exists(game_id) -> bool  在不在
    delete(game_id)          删

⚠️ **`load()` 对不存在的局必须报错**，不能"返回一个开局状态" ——
   那会把"我存档丢了"伪装成"这是一局新游戏"，是最难查的一类 bug。

═══════════════════════════════════════════════════════════════════
★ 存档里到底存了什么
═══════════════════════════════════════════════════════════════════

    {
      "schema_version": 1,          ← 以后改格式靠它做迁移
      "game_id": "g1",
      "saved_at": "2026-10-16T...", ← 给人看的，引擎不读
      "quarter": 7,                 ← 冗余出来方便列表页显示，**不是权威**
      "state": { world, players },  ← GameState.to_dict()
      "report": { p1: {...} }       ← 上一季财报（见下）
    }

⚠️ **为什么要单独存 `report`**：`GameState.to_dict()` 里**没有**上一季财报
   （`state.report` / `state.raw_reports` 都不在里面）。
   不单独存的话，**读档后"上季报告"就没了** —— 玩家会以为程序把账吞了。
   这里存的是 `state.report`（已经是纯 dict，可直接 JSON）。

   `raw_reports`（对象形态）**不存**：它是结算过程中的中间产物，
   只有"刚结算完"那一刻的服务层会用到，读档后由 `report` 字典代替。
"""

from __future__ import annotations

from typing import Optional, Protocol, runtime_checkable

from engine.models import GameState


# ══════════════════════════════════════════════════════════════════
# 存档格式版本
# ══════════════════════════════════════════════════════════════════

#: 存档格式版本。**改动存档结构时必须 +1**，并在 `_migrate()` 里加一条分支。
#: ⚠️ 不要拿它当"游戏版本"用 —— 它只描述**文件长什么样**。
#: v2（2026-10-18）：`GameState.history`（逐季财报历史）随状态进档。
SCHEMA_VERSION = 2


class StorageError(Exception):
    """存档读写的错误（存档不存在 / 格式不对 / 版本不认识）。"""


class GameNotFound(StorageError):
    """指定的局不存在。**不返回空局** —— 见模块头说明。"""


class SaveFormatError(StorageError):
    """存档内容不是本程序认识的格式。"""


# ══════════════════════════════════════════════════════════════════
# 协议
# ══════════════════════════════════════════════════════════════════


@runtime_checkable
class Storage(Protocol):
    """存档后端协议。**引擎与 server 只依赖它。**

    ⚠️ 实现者必须保证 `load(save(x)) == x`（往返一致）。
       这是本协议**唯一**的硬要求，也是最容易出错的地方
       （漏存一个字段，读档后数字就变了）。
    """

    def save(self, game_id: str, state: GameState) -> None:
        """把整局状态写进去。**覆盖**同名局。"""
        ...

    def load(self, game_id: str) -> GameState:
        """读出一局。不存在 → `GameNotFound`。"""
        ...

    def exists(self, game_id: str) -> bool:
        """这一局在不在。"""
        ...

    def delete(self, game_id: str) -> None:
        """删掉一局。不存在时**不报错**（幂等）。"""
        ...


# ══════════════════════════════════════════════════════════════════
# 公共工具（给各实现复用，避免两份 sanitize 逻辑）
# ══════════════════════════════════════════════════════════════════

import re

_SAFE_ID = re.compile(r"^[A-Za-z0-9_@-]{1,64}$")


def check_game_id(game_id: str) -> str:
    """校验 `game_id` 并原样返回。

    ★ **这不是洁癖，是安全边界。** `game_id` 最终会变成一个文件名：

        JsonFileStorage("saves").load("../../../../etc/passwd")

    如果不校验，`game_id` 就成了**任意文件读取**（写更危险）的入口。
    所以只允许 `[A-Za-z0-9_@-]`、长度 1–64。

    ⚠️ **@ 的取舍（2026-10-18 多档存档）**：`@` 用于 slot 命名
       （`<game_id>@<slot_name>`，见 `server/slots.py`）。它**不是路径
       分隔符**（在 POSIX / Windows 都是普通文件名字符）、无法表达 `..`
       或 `/`，因此**不扩大攻击面**；这是把"多档"表示成"派生 game_id"
       的关键。中文 / 空格 / 点 / 斜杠**仍一律拒绝** ——
       转义规则在不同文件系统上不一样，拒绝才是可预测的。
    """
    if not isinstance(game_id, str) or not _SAFE_ID.match(game_id):
        raise ValueError(
            f"非法的 game_id：{game_id!r}\n"
            f"  → 只允许字母/数字/下划线/连字符/@，长度 1–64。\n"
            f"  ⚠️ game_id 会变成文件名，不校验等于开了任意文件读写。"
        )
    return game_id


def pack(game_id: str, state: GameState, saved_at: str) -> dict:
    """把一局打包成**可 JSON 序列化**的 dict（各实现共用）。

    `saved_at` 由调用方给（本模块**不取当前时间** —— 引擎与存储都保持
    确定性，取时间的责任留给服务层）。
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "game_id": game_id,
        "saved_at": saved_at,
        "quarter": state.world.quarter,       # 冗余，方便列表页显示
        "total_quarters": state.world.total_quarters,
        "state": state.to_dict(),
        # ★ 上一季财报单独存 —— `GameState.to_dict()` 里没有它（见模块头）
        "report": dict(state.report or {}),
        # ★ v2：逐季财报历史（终局曲线/20 行指标表的唯一来源）。
        #   与 `report` 同层：GameState.to_dict() 含 history，但这里仍**单列**
        #   一份顶层快照，便于不还原 GameState 也能直读整条时间线（peek 等）。
        "history": {
            pid: [dict(r) for r in rows]
            for pid, rows in (state.history or {}).items()
        },
    }


def unpack(data: dict) -> GameState:
    """把打包好的 dict 还原成 `GameState`（各实现共用）。"""
    if not isinstance(data, dict) or "state" not in data:
        raise SaveFormatError(
            "存档内容不是本程序认识的格式：缺少 `state` 字段。\n"
            f"  实得顶层键：{sorted(data) if isinstance(data, dict) else type(data).__name__}"
        )

    version = data.get("schema_version")
    if version is None:
        raise SaveFormatError(
            "存档没有 `schema_version` —— 无法判断格式。\n"
            "  ⚠️ 不猜版本：猜错会安静地读出一局错的账。"
        )
    if version > SCHEMA_VERSION:
        raise SaveFormatError(
            f"存档版本 {version} 比本程序认识的 {SCHEMA_VERSION} 还新。\n"
            f"  → 请升级程序，**不要**用旧程序打开新存档（会丢字段）。"
        )
    # 旧版本迁移的挂点：以后在此按 version 补字段
    # if version < 2: data = _migrate_1_to_2(data)

    state = GameState.from_dict(data["state"])
    # ★ 把上一季财报放回去 —— 不还原的话读档后"上季报告"是空的
    state.report = dict(data.get("report") or {})
    # ★ v2：把逐季财报历史放回去。**不造数**：v1 老档没有顶层 "history" 键、
    #   且 state["state"] 里也没内嵌 → `data.get("history")` 为 None → 按 `{}` 处理。
    #   （`GameState.from_dict` 在内嵌 history 时也已吃进；这里以顶层键为准覆盖一次，
    #     与 `report` 字段同一口径：顶层键才是权威来源。）
    state.history = {
        pid: [dict(r) for r in rows]
        for pid, rows in (data.get("history") or {}).items()
    }
    return state
