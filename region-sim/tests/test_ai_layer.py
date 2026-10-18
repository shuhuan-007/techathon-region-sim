"""ai/ 层测试 —— 白名单自检、三只只读工具、规则检索。

跑法（在 region-sim/ 下）：

    python tests/test_ai_layer.py

依据《规则基线》§11.4（权限阶梯）：AI 可解释 → 可试算 → 可起草
→ 玩家确认 → 提交；**AI 无 commit**。
本文件只测 ai/ 暴露的五件事（见 ai/__init__.py 的 __all__）与
ai/rules_digest.py 的检索，不改 ai/ 下任何实现。
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.loader import Config, load_config
from engine.models import Plan, PlotPlan, PlotType
from engine import validate as engine_validate

from ai import (
    AisImportBoundaryError,
    assert_import_boundary,
    tool_draft_plan,
    tool_preview,
    tool_validate,
)
from ai import rules_digest
from tests.test_m1 import make_state, mature, plain_plan

TOL = 1e-6


@pytest.fixture(autouse=True)
def _rebaseline_ai_boundary():
    """整库连跑时，别的测试文件可能已经 import 了 server.* / storage ——
    按 ai/tools.py 的口径，那些属于"进程里早就有的"，**不算 ai/ 的账**。
    每个用例开始时把基线重置为当前进程快照，等价于本文件隔离单跑。
    """
    from ai import tools as _tools

    saved = _tools._LOADED_AT_AI_IMPORT
    _tools._LOADED_AT_AI_IMPORT = frozenset(sys.modules)
    try:
        yield
    finally:
        _tools._LOADED_AT_AI_IMPORT = saved


def cfg_with_example_elasticity() -> Config:
    """★ 注入**算例**弹性（与 tests/test_pipeline.py 同口径），只为让
    pipeline.preview 能跑通求价一步；不是裁定、不是默认值。"""
    cfg = load_config()
    for curve in cfg.market.values():
        if curve.Es is None:
            curve.Es = 0.5
        if curve.Ed is None:
            curve.Ed = 0.5
    return cfg


# ══════════════════════════════════════════════════════════════════
# 一、★ 白名单自检真的会拦住 advance / 黑名单模块（最重要）
# ══════════════════════════════════════════════════════════════════
#
# 实现口径（以 ai/tools.py 为准，测它真实的行为）：
#   `assert_import_boundary()` 查两样东西——
#     ① sys.modules 里是否已加载黑名单模块（server.session / storage …）；
#     ② ai.* 命名空间下的模块是否持有了**真·** engine.pipeline.advance
#        （`is` 比较函数对象，防误判同名局部变量）。
#   命中 → 抛 AisImportBoundaryError。
#
# 因此「假装 import 了 engine.pipeline.advance」的构造方式是以实现为准的：
# 往 sys.modules 塞一个 ai.* 假模块、让它持有真的 advance；以及直接往
# sys.modules 塞黑名单模块。两种都必须在调用自检时被拦住。


def test_self_check_passes_in_clean_state():
    """干净状态下自检不应误报（基线：白名单模块自己不算违规）。"""
    assert_import_boundary()  # 不抛即通过


def test_self_check_catches_fake_advance_holder():
    """情形 A：某个 ai.* 模块（假装）from-import 了真的 advance。

    模拟 `from engine.pipeline import advance` 的**既成事实**：
    sys.modules 里多一个 ai.* 模块、且它持有的 advance 与
    engine.pipeline.advance 是同一个函数对象 —— 自检必须抛错。
    """
    import types

    from engine import pipeline as real_pipeline
    import ai.tools  # noqa: F401  （确保 real_advance 已可解析）

    fake_name = "ai._test_fake_module_holding_advance"
    fake = types.ModuleType(fake_name)
    fake.advance = real_pipeline.advance  # 真的指到 engine.pipeline.advance
    sys.modules[fake_name] = fake
    try:
        raised = False
        try:
            assert_import_boundary()
        except AisImportBoundaryError as exc:
            raised = True
            assert "advance" in str(exc), "报错信息应点名 advance"
        assert raised, (
            "★ 缺陷：ai.* 模块持有了真·advance，自检却没有拦下"
        )
    finally:
        sys.modules.pop(fake_name, None)
    # 清理后自检应恢复通过（确认是塞进去的假模块触发了拦截）
    assert_import_boundary()


def test_self_check_catches_blacklist_module_loaded():
    """情形 B：黑名单模块（server.session / storage）出现在 sys.modules。

    ai/tools.py 自己会 import `server.session`？不会 —— 但本测试文件
    没 import server 任何东西；塞一个假的 storage 模块进 sys.modules，
    自检必须抛 AisImportBoundaryError。
    """
    import types

    fake_name = "storage"  # 黑名单按模块整名匹配（含子模块）
    saved = sys.modules.get(fake_name, "__absent__")
    tools_mod = sys.modules["ai.tools"]
    baseline = tools_mod._LOADED_AT_AI_IMPORT
    sys.modules[fake_name] = types.ModuleType(fake_name)
    try:
        # 整库连跑时 storage 可能已被别的测试文件 import（＝"进程里早就有的"），
        # 本用例要验的是「**新出现**的黑名单模块必须被拦下」，
        # 所以把它从基线里摘掉，模拟它确实是刚刚才加载的。
        tools_mod._LOADED_AT_AI_IMPORT = baseline - {fake_name}
        raised = False
        try:
            assert_import_boundary()
        except AisImportBoundaryError as exc:
            raised = True
            assert "storage" in str(exc)
        assert raised, "★ 缺陷：黑名单模块 storage 已加载，自检却没有拦下"
    finally:
        tools_mod._LOADED_AT_AI_IMPORT = baseline
        if saved == "__absent__":
            sys.modules.pop(fake_name, None)
        else:
            sys.modules[fake_name] = saved
    assert_import_boundary()


def test_self_check_does_not_blame_pre_existing_modules():
    """★ 防回归：**进程里早就有的**黑名单模块，不算 ai/ 的账。

    ⚠️ 这条是 2026-10-17 凌晨补的，起因是自检的一个真缺陷：
       `assert_import_boundary()` 最初扫的是**全进程** `sys.modules`，
       而注释写的是"只查 ai/ 自己直接加载的" —— **实现与注释不符**。
       后果是真会误伤：正常跑着的服务器进程里，`server.app` 早已 import 了
       `storage` 与 `server.session`，于是**任何一次自检都报"ai/ 越界"**，
       而 ai/ 一个 commit 入口都没碰过。安全机制动不动误报，
       下一步就是有人把整个自检删掉 —— 那才是最坏的结局。

    正确口径（本测试钉住）：**只有 ai/ 加载之后新出现的黑名单模块才算账。**
    """
    import types

    fake_name = "storage"
    saved = sys.modules.get(fake_name, "__absent__")
    tools_mod = sys.modules["ai.tools"]
    baseline = tools_mod._LOADED_AT_AI_IMPORT
    sys.modules[fake_name] = types.ModuleType(fake_name)
    try:
        # 把这个模块塞进"ai 加载之前就已存在"的基线，
        # 模拟"进程启动时它就在"这一情形。
        tools_mod._LOADED_AT_AI_IMPORT = baseline | {fake_name}
        try:
            assert_import_boundary()
        except AisImportBoundaryError as exc:
            raise AssertionError(
                "★ 缺陷回归：进程里**早就存在**的黑名单模块被算成了 ai/ 的账"
                f"（这正是会误伤服务器进程的那个 bug）：{exc}"
            ) from None
    finally:
        tools_mod._LOADED_AT_AI_IMPORT = baseline
        if saved == "__absent__":
            sys.modules.pop(fake_name, None)
        else:
            sys.modules[fake_name] = saved

    assert_import_boundary()


# ══════════════════════════════════════════════════════════════════
# 二、tool_preview 不改状态（只读试算）
# ══════════════════════════════════════════════════════════════════


def test_tool_preview_does_not_mutate_state():
    """调用 tool_preview 前后，state.to_dict() 必须完全相等。"""
    cfg = cfg_with_example_elasticity()
    plot = mature("0,0", PlotType.RESOURCE, 1)
    state = make_state(cfg, "p1", plot)
    plan = plain_plan("p1", "0,0", u=1.0)

    before = copy.deepcopy(state.to_dict())
    out = tool_preview(state, {"p1": plan}, cfg)
    after = state.to_dict()

    assert before == after, "★ tool_preview 必须一个字都不改"
    assert state.world.quarter == 1, "★ 试算不能推进时钟"
    assert isinstance(out, dict), "返回 QuarterReport.to_dict() 的 dict 形状"
    assert "cash_after" in out or "players" in out or "quarter" in out, (
        f"试算报告形状可疑，keys={sorted(out)[:10]}"
    )


# ══════════════════════════════════════════════════════════════════
# 三、tool_validate 只读 —— 不改 plan、不改 player
# ══════════════════════════════════════════════════════════════════


def test_tool_validate_is_readonly_and_matches_engine():
    """tool_validate 前后 plan.to_dict() / player.to_dict() 完全相等；
    且返回与直接调 engine.validate.validate_plan 一致（逐条比对）。"""
    cfg = load_config()
    plot = mature("0,0", PlotType.RESOURCE, 1)
    state = make_state(cfg, "p1", plot)
    player = state.players["p1"]
    plan = plain_plan("p1", "0,0", u=1.0)

    plan_before = copy.deepcopy(plan.to_dict())
    player_before = copy.deepcopy(player.to_dict())

    issues = tool_validate(plan, player, cfg, "p1")

    assert plan.to_dict() == plan_before, "★ tool_validate 改了 plan"
    assert player.to_dict() == player_before, "★ tool_validate 改了 player"

    # 与引擎直调结果一致（AI 与玩家同一入口，models.py 顶部口径）
    direct = [i.to_dict() for i in
              engine_validate.validate_plan(plan, player, cfg, "p1")]
    assert issues == direct
    assert issues == [], (
        f"成熟地块 u=1 现金 1000 的合法计划不应被拒，却得到：{issues}"
    )


def test_tool_validate_reports_bad_plan_as_issue_list():
    """非法计划（买了配置里不存在的商品）→ 返回问题清单，不是异常。"""
    cfg = load_config()
    plot = mature("0,0", PlotType.RESOURCE, 1)
    state = make_state(cfg, "p1", plot)
    player = state.players["p1"]
    bad = Plan(
        player_id="p1",
        plot_plans={"0,0": PlotPlan(plot_id="0,0", active=True, u=1.0)},
        purchases={"不存在的商品": 3.0},
    )
    plan_before = copy.deepcopy(bad.to_dict())
    issues = tool_validate(bad, player, cfg, "p1")
    assert bad.to_dict() == plan_before, "★ 非法计划也不能被 tool_validate 改动"
    assert isinstance(issues, list) and len(issues) > 0, (
        "非法计划应至少产出一条 issue"
    )
    # 五要素落点：每条 issue 至少带 code/target/message
    for it in issues:
        assert "code" in it and "target" in it and "message" in it, (
            f"issue 缺五要素字段：{it}"
        )


# ══════════════════════════════════════════════════════════════════
# 四、tool_draft_plan —— 好计划过得去，坏输入只回问题清单
# ══════════════════════════════════════════════════════════════════


def test_tool_draft_plan_produces_valid_plan():
    """run_all=True：把玩家全部 RUNNING 地块按 u 排进计划，产物必须
    能过 engine.validate.validate_plan；且起草不改 player。"""
    cfg = load_config()
    plot = mature("0,0", PlotType.RESOURCE, 1)
    state = make_state(cfg, "p1", plot)
    player = state.players["p1"]
    player_before = copy.deepcopy(player.to_dict())

    out = tool_draft_plan("p1", player=player, cfg=cfg, run_all=True, u=1.0)

    assert player.to_dict() == player_before, "★ 起草不得改玩家状态"
    assert out["ok"] is True, (
        f"成熟地块 u=1 现金 1000 应能起草成功，得到：{out['issues']}"
    )
    assert out["plan"] is not None and isinstance(out["plan"], dict)
    assert out["issues"] == []
    # next_step 必须指向 /api/submit（AI 无 commit 的落点）
    assert "/api/submit" in out["next_step"]

    # 产出的 dict 形状能重建为 Plan 并再次过 validate（判决权在 validate）
    plan_dict = out["plan"]
    assert plan_dict["player_id"] == "p1"
    assert "0,0" in plan_dict["plot_plans"], "run_all=True 应排入 RUNNING 地块"
    assert plan_dict["plot_plans"]["0,0"]["active"] is True
    assert abs(plan_dict["plot_plans"]["0,0"]["u"] - 1.0) < TOL


def test_tool_draft_plan_bad_input_returns_issue_list_not_bad_plan():
    """故意构造过不了的输入：买一种不存在的商品（采购必被拒）。
    tool_draft_plan 必须 ok=False、plan=None、issues 非空 ——
    「返回问题清单，而不是吐出一个坏计划」（任务书原话）。"""
    cfg = load_config()
    plot = mature("0,0", PlotType.RESOURCE, 1)
    state = make_state(cfg, "p1", plot)
    player = state.players["p1"]
    player_before = copy.deepcopy(player.to_dict())

    out = tool_draft_plan(
        "p1", player=player, cfg=cfg,
        run_all=True, u=1.0,
        purchases={"不存在的商品": 3.0},
    )

    assert player.to_dict() == player_before
    assert out["ok"] is False, "有非法采购的计划不应被标成 ok"
    assert out["plan"] is None, "★ 过不了 validate 就不许吐出 plan（坏计划）"
    assert isinstance(out["issues"], list) and len(out["issues"]) > 0, (
        "必须返回问题清单"
    )
    for it in out["issues"]:
        assert "code" in it and "message" in it

    # 交叉核对：问题清单应与 validate 直说的一致（起草不私改判决）
    cross = [i.to_dict() for i in engine_validate.validate_plan(
        Plan(
            player_id="p1",
            plot_plans={"0,0": PlotPlan(plot_id="0,0", active=True, u=1.0)},
            purchases={"不存在的商品": 3.0},
        ),
        player, cfg, "p1",
    )]
    assert out["issues"] == cross, (
        "tool_draft_plan 的问题清单必须等于 engine.validate 的判决"
    )


def test_tool_draft_plan_without_player_says_so():
    """拿不到玩家状态 → 明说拿不到（不编数字），而不是硬起草。"""
    out = tool_draft_plan("ghost", player=None, cfg=load_config())
    assert out["ok"] is False and out["plan"] is None
    assert len(out["issues"]) == 1
    assert out["issues"][0]["code"] == "no_player_state"


# ══════════════════════════════════════════════════════════════════
# 五、rules_digest —— 基线检索：7.2 是合同那一节
# ══════════════════════════════════════════════════════════════════


def test_rule_section_7_2_is_contract():
    """rule_section('7.2') 必须取到合同一节：返回文本含『7.2』与『合同』。"""
    sec = rules_digest.rule_section("7.2")
    assert sec["key"] == "7.2", f"节号应原样回显，得到 key={sec['key']!r}"
    assert "7.2" in sec["text"], "节文本应含节号自身"
    assert "合同" in sec["text"], (
        f"7.2 按基线是合同一节；标题为 {sec['title']!r}"
    )
    assert "合同" in sec["title"], "标题本身应点名合同"


def test_rule_sections_catalog_nonempty():
    """rule_sections() 目录非空，且能找到 key=='7.2' 的条目。"""
    sections = rules_digest.rule_sections()
    assert len(sections) > 50, f"实测曾扫出 95 节，现在只有 {len(sections)} 节？"
    keys = [s["key"] for s in sections]
    assert "7.2" in keys, "目录里应含 7.2 节"


def test_rule_section_not_found_is_explicit():
    """找不到的节 → 抛 RuleNotFoundError（明说，不猜）。"""
    try:
        rules_digest.rule_section("99.99_绝不存在的节号")
    except rules_digest.RuleNotFoundError:
        pass
    else:
        raise AssertionError("★ 找不到的节应抛 RuleNotFoundError，却返回了内容")


def test_digest_is_excerpt_not_fabrication():
    """digest 是**原文截取**：summary 必须是 rule_section 全文的前缀。"""
    d = rules_digest.digest("7.2")
    full = rules_digest.rule_section("7.2")["text"]
    assert full.startswith(d["summary"]), (
        "★ digest 的 summary 必须是基线原文的前缀（不许 AI 改写）"
    )
    assert d["key"] == "7.2"


# ══════════════════════════════════════════════════════════════════
# runner（与 tests/test_pipeline.py 同写法）
# ══════════════════════════════════════════════════════════════════


def _run_all() -> int:
    import io

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    mod = sys.modules[__name__]
    names = sorted(n for n in dir(mod) if n.startswith("test_"))
    ok = fail = 0
    print("=" * 68)
    print("ai/ 层测试")
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
