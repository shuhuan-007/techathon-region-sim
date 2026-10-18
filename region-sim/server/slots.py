"""多档存档（slot）辅助模块 —— 命名、解析、列出。

═══════════════════════════════════════════════════════════════════
为什么要有 slot
═══════════════════════════════════════════════════════════════════

**一个 slot 就是一份普通存档**，只是它的 `game_id` 长这样：

    <game_id>@<slot_name>

比如 `g1@before_build`、`g1@q7`。`JsonFileStorage` 落到磁盘就是
`<game_id>@<slot_name>.json`。

这样表达的好处是**零新机制**：
  · slot 复用既有 `Storage` 协议（save/load/exists/delete/peek），
    引擎与 `JsonFileStorage` 一行不改；
  · `check_game_id` 只需放行 `@` 一个字符（见 `storage/base.py`，
    `@` 不是路径分隔符，不扩大攻击面）；
  · 列 slot 就是普通的"扫目录 + glob 前缀"，不调任何新接口。

═══════════════════════════════════════════════════════════════════
slot_name 的合法形
═══════════════════════════════════════════════════════════════════

`slot_name` 由**玩家起名**（如 `before_build` / `高点` / `实验一`）：

  · **非空**（空名等于没归档）；
  · **不含 `\\` / `/` / `.` / `@`** —— 前两个是路径分隔符，`.` 会让
    `Path.stem` 吃掉名字尾段，`@` 是 slot 的分隔符本身（再吃一个就
    解析不出唯一的 `(game_id, slot_name)`）。
  · **长度 ≤ 32** —— 给 `game_id` 留够 `_SAFE_ID` 的 64 字符总上限
    （`game_id` 本身最长 64，`@` 占 1，加上 slot 后还得 ≤ 64）。

 ⚠️ 自动季度档（`@q3` / `@q7`）也由本模块命名（`auto_quarter_slot_id`），
    与玩家起的 slot 同一命名空间 —— 玩家起一个名为 `q3` 的 slot 会
    覆盖自动档。这条不视作冲突（自动档只是"上一季末"的快照，玩家
    真要踩它名字说明玩家想要这个位置）。
"""

from __future__ import annotations

#: slot 名合法字符黑名单：路径分隔符 / 点 / slot 分隔符本身。
#:
#: ⚠️ 这里**不用白名单**（如 `[A-Za-z0-9_-]`），因为 slot 名是给人看的、
#:    中文名"高点"完全合理；只挡"会破坏文件路径 / 解析"的字符。
_FORBIDDEN = ('\\', '/', '.', '@')

#: 玩家起的 slot 名长度上限。`_SAFE_ID` 允许 game_id 总长度 64，
#: `@` 占 1，给 slot 名 32 上限可保证 `game_id ≤ 31` 时不撞总长上限；
#: `game_id > 31` 时由 `check_game_id` 在拼好的 slot_id 上把关（报错给人看）。
SLOT_NAME_MAX = 32


def check_slot_name(slot: str) -> str:
    """校验玩家起的 slot 名并原样返回。非法 → `ValueError`。

    规则：非空、不含 `\\` `/` `.` `@`、长度 ≤ SLOT_NAME_MAX。
    """
    if not isinstance(slot, str) or not slot:
        raise ValueError(
            f"slot 名为空：{slot!r}\n"
            f"  → slot 是给人看的存档标签；空名等于没归档。"
        )
    if len(slot) > SLOT_NAME_MAX:
        raise ValueError(
            f"slot 名过长（{len(slot)} 字符，上限 {SLOT_NAME_MAX}）：{slot!r}"
        )
    bad = [c for c in _FORBIDDEN if c in slot]
    if bad:
        raise ValueError(
            f"slot 名含禁用字符 {bad}：{slot!r}\n"
            f"  → '\\\\' '/' 是路径分隔符、'.' 会被 Path.stem 吃掉、"
            f"'@' 是 slot 的分隔符本身。"
        )
    return slot


def make_slot_id(game_id: str, slot: str) -> str:
    """拼出 slot 的派生 game_id：`<game_id>@<slot>`。

    ⚠️ 本函数**不**校验 `game_id` —— 那是 `check_game_id` 的职责
       （最终写盘前 `JsonFileStorage.path_of` 会兜底再查一次），
       这里只校验 `slot` 本身。
    """
    return f"{game_id}@{check_slot_name(slot)}"


def auto_quarter_slot_id(game_id: str, quarter: int) -> str:
    """自动季度档的 slot_id：`f"{game_id}@q{quarter}"`。

    由 `session._resolve_unlocked` 在**结算完**自动落一份
    （quarter 是"结算前"那一季，即 `advance` 已 `+1` 之后的 `quarter - 1`）。
    """
    if not isinstance(quarter, int) or quarter < 0:
        raise ValueError(f"自动季度档的 quarter 非法：{quarter!r}")
    return f"{game_id}@q{quarter}"


def parse_slot_id(slot_id: str) -> tuple[str, str]:
    """把 `game@slot` 拆成 `(game_id, slot_name)`。

    ⚠️ 用 `rsplit("@", 1)`：玩家起的 slot 名**不允许**含 `@`（见
       `check_slot_name`），但 `game_id` 本身理论上可以含 `@`（虽然
       没人这么起）。从最右一刀是唯一无歧义的拆法。
    不带 `@` → `ValueError`（那不是 slot，是普通局）。
    """
    if "@" not in slot_id:
        raise ValueError(f"不是 slot_id（没有 '@'）：{slot_id!r}")
    game_id, _, slot_name = slot_id.rpartition("@")
    if not game_id or not slot_name:
        raise ValueError(f"slot_id 形状不对：{slot_id!r}")
    return game_id, slot_name


def is_slot_of(slot_id: str, game_id: str) -> bool:
    """这个 slot_id 是不是某局的档位。**不把普通局错认成 slot。**"""
    try:
        g, _ = parse_slot_id(slot_id)
    except ValueError:
        return False
    return g == game_id
