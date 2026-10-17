"""草稿编辑（`ai/plan_edit.py`）—— 纯函数离线测试。

跑法（在 region-sim/ 下）：

    python tests/test_plan_edit.py

依据 `docs/03-架构/AI交互与建设撤销.md` §三（用户反馈第 3 条：
「建设计划无法取消，需要让 AI 助手取消」）。本文件钉死四件事：

    ① `remove_builds` 只删指定的那条建造，**其它一条不许丢**；
    ② `builds` 追加不丢原有、同 plot_id 以新为准（替换）；
    ③ `plot_plans` **只更新列出的地块**，未列出的保持原值
       （编辑模式不默认全开工 —— 那会替玩家做经营决策）；
    ④ `current_plan` 为空 → **原样返回 raw**（旧行为：整份起草）。

另钉 `extract_why` / `plan_action_of` 的判定链，以及「不改调用方对象」。
"""

from __future__ import annotations

import copy
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ai.plan_edit import (  # noqa: E402
    extract_why,
    merge_edits,
    plan_action_of,
    plan_has_content,
)


# ══════════════════════════════════════════════════════════════════
# 夹具
# ══════════════════════════════════════════════════════════════════


def _build(plot_id: str, *, ptype: str = "agriculture", level: int = 1,
           product=None) -> dict:
    """一条建造指令（与 /api/submit 的 plan.builds 同形）。"""
    return {
        "plot_id": plot_id,
        "plot_type": ptype,
        "target_level": level,
        "product": product,
    }


def _plot_plan(plot_id: str, u: float = 1.0, active: bool = True) -> dict:
    return {"plot_id": plot_id, "active": active, "u": u}


def _current(builds=None, plot_plans=None, purchases=None, food=None) -> dict:
    """前端 `publicPlan()` 同形的当前草稿。"""
    return {
        "player_id": "p1",
        "plot_plans": dict(plot_plans or {}),
        "purchases": dict(purchases or {}),
        "food": dict(food or {}),
        "builds": list(builds or []),
        "contracts": [],
    }


# ══════════════════════════════════════════════════════════════════
# 一、撤销：只删指定的那条
# ══════════════════════════════════════════════════════════════════


def test_remove_one_of_two_builds_keeps_the_other():
    """current_plan 2 条建造，`remove_builds` 删 1 条 → 另 1 条保留。"""
    current = _current(builds=[_build("0,0", product="A"),
                               _build("4,6", product="B")])
    merged = merge_edits(
        {"why": "撤销 0,0 那条", "remove_builds": ["0,0"]}, current
    )
    ids = [b["plot_id"] for b in merged["builds"]]
    assert ids == ["4,6"], f"只该剩 4,6，实得 {ids}"
    # ★ 原草稿一个字没动（deepcopy，不是就地改）
    assert len(current["builds"]) == 2, "merge_edits 不许改调用方的 current_plan"


def test_remove_accepts_loose_shapes():
    """`remove_builds` 的几种写法都认：裸串 / dict 项 / 以坐标为键的 dict。"""
    current = _current(builds=[_build("0,0"), _build("0,1"), _build("0,2")])
    for raw_remove in (
        ["0,0"],
        "0,0",
        [{"plot_id": "0,0"}],
        {"0,0": True},
    ):
        merged = merge_edits({"remove_builds": raw_remove}, current)
        ids = [b["plot_id"] for b in merged["builds"]]
        assert ids == ["0,1", "0,2"], (
            f"remove_builds={raw_remove!r} 没删对：{ids}"
        )


def test_remove_unknown_id_is_noop():
    """删一个草稿里没有的坐标 → 什么都别动（不猜、不误删）。"""
    current = _current(builds=[_build("0,0")])
    merged = merge_edits({"remove_builds": ["9,9"]}, current)
    assert [b["plot_id"] for b in merged["builds"]] == ["0,0"]


# ══════════════════════════════════════════════════════════════════
# 二、追加 / 替换
# ══════════════════════════════════════════════════════════════════


def test_append_build_does_not_lose_existing():
    """`builds` 追加 → 原有的一条不丢，新的排在后面。"""
    current = _current(builds=[_build("0,0", product="A")])
    merged = merge_edits({"builds": [_build("4,6", product="B")]}, current)
    ids = [b["plot_id"] for b in merged["builds"]]
    assert ids == ["0,0", "4,6"], f"追加后应保留原有并新增，实得 {ids}"
    assert merged["builds"][1]["product"] == "B"


def test_same_plot_id_replaces_in_place():
    """同 plot_id → **以新指令为准**（替换，不是重复追加）。"""
    current = _current(builds=[_build("0,0", product="A"),
                               _build("0,1", product="C")])
    merged = merge_edits(
        {"builds": [_build("0,0", ptype="industry", product="G")]}, current
    )
    builds = merged["builds"]
    assert len(builds) == 2, f"同 plot_id 不该多出一条：{builds}"
    by_id = {b["plot_id"]: b for b in builds}
    assert by_id["0,0"]["plot_type"] == "industry"
    assert by_id["0,0"]["product"] == "G"
    assert by_id["0,1"]["product"] == "C", "别的建造不许被连累"


def test_remove_then_append_same_reply():
    """同一条回包里既撤又建：先删、再追加（顺序稳定）。"""
    current = _current(builds=[_build("0,0"), _build("0,1")])
    merged = merge_edits(
        {"remove_builds": ["0,0"], "builds": [_build("4,6")]}, current
    )
    assert [b["plot_id"] for b in merged["builds"]] == ["0,1", "4,6"]


# ══════════════════════════════════════════════════════════════════
# 三、plot_plans：只更新列出的，未列出的保持
# ══════════════════════════════════════════════════════════════════


def test_plot_plans_only_updates_listed_plots():
    """模型只写了 1 块地的开工 → 其它地块**保持 players 原值**。"""
    current = _current(
        builds=[],
        plot_plans={
            "5,3": _plot_plan("5,3", u=1.0),
            "6,4": _plot_plan("6,4", u=0.5),
            "5,4": _plot_plan("5,4", u=0.0, active=False),
        },
    )
    merged = merge_edits(
        {"why": "5,3 这季不开工", "plot_plans": {"5,3": _plot_plan("5,3", u=0.0,
                                                              active=False)}},
        current,
    )
    pp = merged["plot_plans"]
    assert pp["5,3"]["u"] == 0.0, "列出的地块要被更新"
    assert pp["6,4"]["u"] == 0.5, "没列出的地块必须保持原值（不许默认全开工）"
    assert pp["5,4"]["active"] is False, "没列出的地块连 active 也不许改"
    assert "4,5" not in pp, "不许凭空补出草稿里没有的地块"


def test_plot_plans_untouched_when_reply_has_no_plot_plans():
    """回包里没有 plot_plans（只撤建造）→ 开工安排原样保留。"""
    current = _current(
        builds=[_build("0,0")],
        plot_plans={"5,3": _plot_plan("5,3", u=0.25)},
    )
    merged = merge_edits({"remove_builds": ["0,0"]}, current)
    assert merged["plot_plans"] == {"5,3": _plot_plan("5,3", u=0.25)}


# ══════════════════════════════════════════════════════════════════
# 四、purchases / food：出现即整体替换
# ══════════════════════════════════════════════════════════════════


def test_purchases_and_food_replace_whole_block():
    current = _current(purchases={"G": 3}, food={"A": 2})
    merged = merge_edits({"purchases": {"H-A": 1}, "food": {"A": 1.5}}, current)
    assert merged["purchases"] == {"H-A": 1}, "purchases 应整体替换"
    assert merged["food"] == {"A": 1.5}, "food 应整体替换"


def test_absent_purchases_and_food_keep_original():
    current = _current(purchases={"G": 3}, food={"A": 2})
    merged = merge_edits({"remove_builds": []}, current)
    assert merged["purchases"] == {"G": 3}
    assert merged["food"] == {"A": 2}


# ══════════════════════════════════════════════════════════════════
# 五、空 current_plan：旧行为（原样返回）
# ══════════════════════════════════════════════════════════════════


def test_empty_current_plan_returns_raw_unchanged():
    """没有 current_plan / 空草稿 → 原样返回 raw（走旧路径）。"""
    raw = {"player_id": "p1", "builds": [_build("0,0")],
           "plot_plans": {}, "purchases": {}, "food": {}}
    for empty in (None, {}, {"player_id": "p1"},
                  _current(builds=[], plot_plans={})):
        merged = merge_edits(raw, empty)
        assert merged == raw, f"current_plan={empty!r} 时应原样返回 raw"
    # 原样 = 同一个对象（compile_draft 拿到的就是模型给的那份）
    assert merge_edits(raw, None) is raw


def test_plan_has_content_detects_draft():
    """`plan_has_content` 是"编辑模式 vs 旧行为"的分叉点。"""
    assert plan_has_content(_current(builds=[_build("0,0")])) is True
    assert plan_has_content(_current(plot_plans={"5,3": _plot_plan("5,3")})) is True
    assert plan_has_content(_current(purchases={"G": 1})) is True
    assert plan_has_content(_current(food={"A": 1})) is True
    assert plan_has_content(_current()) is False
    assert plan_has_content({"player_id": "p1"}) is False
    assert plan_has_content(None) is False


# ══════════════════════════════════════════════════════════════════
# 六、why / plan_action
# ══════════════════════════════════════════════════════════════════


def test_extract_why_takes_why_or_rationale():
    assert extract_why({"why": "  撤销 0,0  "}) == "撤销 0,0"
    assert extract_why({"rationale": "贴 5,5 吃邻接"}) == "贴 5,5 吃邻接"
    assert extract_why({"why": "", "rationale": "理由"}) == "理由"
    assert extract_why({"why": 123}) == ""
    assert extract_why({}) == ""
    assert extract_why(None) == ""
    # why 优先于 rationale
    assert extract_why({"why": "A", "rationale": "B"}) == "A"


def test_plan_action_removed_only_when_it_hits():
    current = _current(builds=[_build("0,0"), _build("0,1")])
    assert plan_action_of({"remove_builds": ["0,0"]}, current) == "removed"
    # 删了不存在的坐标、又没有别的动作 → none（不报"撤销了"）
    assert plan_action_of({"remove_builds": ["9,9"]}, current) == "none"


def test_plan_action_edited_created_none():
    current = _current(builds=[_build("0,0")])
    assert plan_action_of({"builds": [_build("4,6")]}, current) == "edited"
    assert plan_action_of({"plot_plans": {"0,0": _plot_plan("0,0")}},
                          current) == "edited"
    # 空草稿 + 有动作 → created
    assert plan_action_of({"builds": [_build("0,0")]}, None) == "created"
    assert plan_action_of({"remove_builds": ["0,0"]}, {}) == "created"
    # 什么都没做 → none
    assert plan_action_of({}, current) == "none"
    assert plan_action_of({"why": "只是解释"}, current) == "none"
    assert plan_action_of(None, current) == "none"


# ══════════════════════════════════════════════════════════════════
# 七、只读 + 不串味
# ══════════════════════════════════════════════════════════════════


def test_merge_does_not_mutate_inputs_and_drops_control_keys():
    """不改入参；返回里不带 why/remove_builds（那是聊天指令，不是 Plan 字段）。"""
    current = _current(builds=[_build("0,0")],
                       plot_plans={"5,3": _plot_plan("5,3")})
    snapshot = copy.deepcopy(current)
    merged = merge_edits(
        {"why": "撤销 0,0", "remove_builds": ["0,0"],
         "builds": [_build("4,6")]},
        current,
    )
    assert current == snapshot, "★ merge_edits 改了调用方的 current_plan"
    assert "why" not in merged, f"控制字段 why 不该进 Plan：{sorted(merged)}"
    assert "remove_builds" not in merged
    # 编辑后仍是完整 Plan 形状（编译器要读这四个键）
    for key in ("builds", "plot_plans", "purchases", "food"):
        assert key in merged, f"合并结果缺 {key}"


def test_merge_returns_full_plan_shape():
    """合并结果的四个键都在，且 purchases/food 默认是 dict（编译器要 get）。"""
    current = _current(builds=[_build("0,0")])
    merged = merge_edits({"remove_builds": ["0,0"]}, current)
    assert merged["builds"] == []
    assert isinstance(merged["plot_plans"], dict)
    assert isinstance(merged["purchases"], dict)
    assert isinstance(merged["food"], dict)


# ══════════════════════════════════════════════════════════════════
# runner（与 tests/test_ai_host.py 同写法）
# ══════════════════════════════════════════════════════════════════


def _run_all() -> int:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    mod = sys.modules[__name__]
    names = sorted(n for n in dir(mod) if n.startswith("test_"))
    ok = fail = 0
    print("=" * 68)
    print("草稿编辑 ai/plan_edit.py（纯函数，离线）")
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
