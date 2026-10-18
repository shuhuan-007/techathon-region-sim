"""DEMO 剧本：20 季单分支验证 + 存档恢复确定性证明。

背景：
- 引擎是确定性的：同样输入 → 同样输出，分毫不差。
- 本脚本把一个 20 季的"剧本"（每季一份 Plan + 可选存档点）跑一遍，
  沿途抓 JSON 快照，最后渲染成一份路演用 demo.md。
- 同时跑**分支 B 线**：从 A 线 Q12 的存档恢复，用完全相同的 plans 重放 Q13-Q20，
  断言两条线每季 cash / gdp 完全一致 —— 这是"确定性引擎"的现场铁证。

跑法：
    python -m region-sim.tools.demo_playthrough \\
        --server http://127.0.0.1:8000 \\
        --out _tmp-submission/demo-run-{timestamp}
    # 骨架模式（不连真 server，仅验证脚本可启动）：
    python region-sim/tools/demo_playthrough.py --mock --out _tmp-submission/demo-test

输出：
    <out>/demo.md                        ← 主交付物，图文并茂
    <out>/raw/Q{NN}-{state,report,map}.json
    <out>/raw/slots/{slot}.json

当前阶段：**骨架**。
- DemoClient / MockTransport / HttpTransport 三个壳已立，HTTP 真实现留给下一阶段；
- ACT1 / ACT2 / ACT3 的 20 份 Plan 全用空壳（"plots": [], "purchases": {}, "food_rations": {}）；
- 四个核心方法 run_act_a / run_act_b_from_q12 / verify_determinism / write_demo_md
  保留 raise NotImplementedError，由下一阶段填充。
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

# ──────────────────────────────────────────────────────────────────
# 常量：A/B 线的 game/player id，A 线的两个存档点名
# ──────────────────────────────────────────────────────────────────
GAME_A = "demo-a"
GAME_B = "demo-b"
PLAYER = "p1"
# 存档点名（**以任务书为准**，与 docs/04-交接/DEMO剧本-玩法展示版.md 对齐）
SLOT_Q3_TUTORIAL    = "q3-tutorial-done"
SLOT_Q5_FIRST_CRISIS = "q5-first-crisis"
SLOT_Q8_MIDWAY_DOUBT = "q8-midway-doubt"
SLOT_Q12_BEFORE_FINALE = "q12-before-finale"


# ──────────────────────────────────────────────────────────────────
# 一、Transport 抽象：决定"真发 HTTP" 还是"返回 mock"
# ──────────────────────────────────────────────────────────────────
class Transport:
    """HTTP 传输抽象。call() 返回 dict（server 回复的 JSON 对象）。"""

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        raise NotImplementedError


class MockTransport(Transport):
    """骨架阶段：只回显调用，不真正发 HTTP。由 HttpTransport 在下一阶段替换。"""

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        return {"_mock": True, "method": method, "path": path, "body": body}


class HttpTransport(Transport):
    """真 HTTP 实现（stdlib urllib.request）。

    约定：
    - body 非 None 时以 application/json POST/PUT；None 视为 GET；
    - GET 且 body 非 None 时，把 body 当 query string 拼到 URL 后面；
    - 超时默认 300s（20 季跑完可能有几秒到几十秒，留足余量）；
    - HTTP 状态 != 200 抛 RuntimeError，body 作为错误信息一部分。
    """

    def __init__(self, base_url: str, timeout: float = 300.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        method = method.upper()
        url = self.base_url + path
        headers: dict[str, str] = {}
        data: bytes | None = None

        if method == "GET":
            if body:
                qs = urllib.parse.urlencode(body)
                sep = "&" if urllib.parse.urlparse(url).query else "?"
                url = f"{url}{sep}{qs}"
        else:
            if body is not None:
                data = json.dumps(body).encode("utf-8")
                headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, method=method, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status = resp.getcode()
                text = resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            err_text = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"HTTP {e.code} on {method} {path}: {err_text}"
            ) from e

        if status != 200:
            raise RuntimeError(f"HTTP {status} on {method} {path}: {text}")
        return json.loads(text)


# ──────────────────────────────────────────────────────────────────
# 二、DemoClient：对 region-sim HTTP API 的最小封装
# 路径/参数如果有偏差，由下一个阶段按 server/routes.py 里的最终版对表
# ──────────────────────────────────────────────────────────────────
class DemoClient:
    """对 region-sim HTTP API 的最小封装。

    骨架阶段：默认 transport = MockTransport，不接真 server。
    """

    def __init__(self, base_url: str, transport: Transport | None = None):
        self.base_url = base_url.rstrip("/")
        self.transport = transport or MockTransport()

    # ---- 生命周期 ----
    def new_game(self, game_id: str, player_id: str) -> dict:
        return self.transport.call(
            "POST", "/api/new_game",
            {"game_id": game_id, "player_id": player_id},
        )

    # ---- 读取 ----
    def get_state(self, game_id: str, player_id: str) -> dict:
        return self.transport.call(
            "GET", "/api/state",
            {"game": game_id, "player": player_id, "narratives": 0},
        )

    def get_report(self, game_id: str, player_id: str) -> dict:
        return self.transport.call(
            "GET", "/api/report",
            {"game": game_id, "player": player_id},
        )

    def get_map(self, game_id: str, player_id: str) -> dict:
        return self.transport.call(
            "GET", "/api/map",
            {"game": game_id, "player": player_id},
        )

    def get_finale(self, game_id: str, player_id: str) -> dict:
        return self.transport.call(
            "GET", "/api/finale",
            {"game": game_id, "player": player_id},
        )

    # ---- 写入 ----
    def submit_plan(self, game_id: str, player_id: str, plan: dict) -> dict:
        return self.transport.call(
            "POST", "/api/submit",
            {"game": game_id, "player": player_id, "plan": plan},
        )

    def simulate(self, game_id: str, player_id: str, plan: dict) -> dict:
        return self.transport.call(
            "POST", "/api/simulate",
            {"game": game_id, "player": player_id, "plan": plan},
        )

    def end_quarter(self, game_id: str, player_id: str) -> dict:
        return self.transport.call(
            "POST", "/api/end_quarter",
            {"game": game_id, "player": player_id},
        )

    def next_quarter(self, game_id: str, player_id: str) -> dict:
        return self.transport.call(
            "POST", "/api/next_quarter",
            {"game": game_id, "player": player_id},
        )

    # ---- 存档 ----
    def save_slot(self, game_id: str, player_id: str, slot: str) -> dict:
        return self.transport.call(
            "POST", "/api/save_slot",
            {"game": game_id, "player": player_id, "slot": slot},
        )

    def load_slot(self, slot_id: str, as_game_id: str) -> dict:
        """从 slot_id 恢复，挂载成新局 as_game_id。

        注意：服务端 schema 是 {game: "<slot_id>", as: "<name>"}（2026-10-18 实测），
        slot_id 里的 @ 让服务端识别为完整路径。
        """
        return self.transport.call(
            "POST", "/api/load_slot",
            {"game": slot_id, "as": as_game_id},
        )

    def list_slots(self, game_id: str) -> dict:
        return self.transport.call(
            "GET", "/api/slots",
            {"game": game_id},
        )

    # ---- AI ----
    def ai_advise(
        self,
        game_id: str,
        player_id: str,
        question: str,
        draft: bool = True,
        current_plan: dict | None = None,
        mode: str = "auto",
    ) -> dict:
        return self.transport.call(
            "POST", "/api/ai/advise",
            {
                "game": game_id,
                "player": player_id,
                "question": question,
                "draft": draft,
                "current_plan": current_plan,
                "mode": mode,
            },
        )


# ──────────────────────────────────────────────────────────────────
# 三、剧本数据：QuarterScript × 20
# 骨架阶段 plan 用空壳 dict，真实 Plan 由下一阶段按 engine 规则填充
# ──────────────────────────────────────────────────────────────────
@dataclass
class QuarterScript:
    """一季度的剧本：玩家计划 + 是否在结算后存档 + 给 demo.md 用的一段中文叙事。"""
    quarter: int
    plan: dict                           # 提交给 /api/plan 的 Plan 字典
    save_slot_after: str | None = None   # 结算后要存的 slot 名（存在 A 线上）
    narrate: str = ""                    # 给 demo.md 用的中文叙事段


# ──────────────────────────────────────────────────────────────────
# 剧本 · 12 季真实 Plan（任务书 2026-10-18 玩法展示版 v2）
#
# ★ plot_id 命名规则（侦察结论）：
#   · engine/models.py L142 PLOT_ID_SEP = ","；L145 make_plot_id(row,col) = f"{row},{col}"
#   · engine/models.py L590 all_plot_ids 把 10×10 全部 100 格 ID 预生成 —— "0,0"~"9,9"
#   · engine/pipeline.py L1290 新建时 plot_id = order.plot_id（玩家指定，引擎照抄）
#   → Q1 在 (0,0)(1,0)(2,0)(3,0) 摆 4 块农业 ⇒ BuildOrder.plot_id 字面写 "0,0".."3,0"
#   → Q4 industry 摆 (0,1) ⇒ plot_id = "0,1"（Q7 升级时就引用这个 ID）
#
# ★ Plan schema：严格按 server/app.py::plan_from_dict L173-212
#   {"player_id": "p1",
#    "plot_plans": {"<pid>": {"plot_id":..., "active":bool, "u":float}},  # dict
#    "builds":    [{"plot_id","plot_type","target_level","product"?}],   # list
#    "purchases": {"<goods>": float},
#    "food":      {"<goods>": float}}
#   ⚠️ BuildOrder 字段＝{plot_id, plot_type, target_level, product?} —— **无 "route"**。
#      升研究线 = product="研究"（引擎按 _INDUSTRY_KEY_TO_OUTPUT 命中 ind_1_2_research）。
#
# ★ Q4 玩家自建的 industry (0,1) 在 Q7 要升到 L2 research ——
#   为了让 Q7 的 BuildOrder 能引用到这个 ID，定义模块级常量 _Q4_IND_PID。
# ──────────────────────────────────────────────────────────────────

_Q4_IND_PID  = "0,1"   # Q4 玩家手动 build 的 industry G 地块；Q7 升级时引用
_Q4_AGRI_B1  = "4,1"   # Q4 农业 B 第 1 块
_Q4_AGRI_B2  = "5,1"   # Q4 农业 B 第 2 块
_Q11_RES_F1  = "6,0"   # Q11 资源 F 第 1 块（位置任意，拣空角）
_Q11_RES_F2  = "7,0"   # Q11 资源 F 第 2 块


def _p(player_plans: dict[str, dict] | None = None,
       builds: list[dict] | None = None,
       purchases: dict[str, float] | None = None,
       food: dict[str, float] | None = None) -> dict:
    """拼一个 Plan dict（server/app.py::plan_from_dict 的形状）。**不**做合法性判断。"""
    return {
        "player_id": PLAYER,
        "plot_plans": player_plans or {},
        "builds":     builds or [],
        "purchases":  purchases or {},
        "food":       food or {},
    }


# 第一幕 · 起势：Q1-Q3 教学 / Q4 拒绝 AI（剧情最高峰） / Q5 粮食危机。
ACT1: list[QuarterScript] = [
    # ── Q1 第一笔投资 ────────────────────────────────────────────────
    QuarterScript(
        quarter=1,
        plan=_p(
            builds=[
                {"plot_id": "0,0", "plot_type": "agriculture", "target_level": 1, "product": "A"},
                {"plot_id": "1,0", "plot_type": "agriculture", "target_level": 1, "product": "A"},
                {"plot_id": "2,0", "plot_type": "agriculture", "target_level": 1, "product": "A"},
                {"plot_id": "3,0", "plot_type": "agriculture", "target_level": 1, "product": "A"},
            ],
        ),
        narrate="第一笔投资：摆 4 块一级农业（产出 A），位置 (0,0)(1,0)(2,0)(3,0)。",
    ),
    # ── Q2 AI 出场（answer 模式，不起草）───────────────────────────
    QuarterScript(
        quarter=2,
        plan=_p(),   # 全空：玩家只问 AI，不动地块
        narrate="我问 AI：为什么我的 E 不到 1？（answer 模式，不走 AI 起草）",
    ),
    # ── Q3 AI 起草（plan 字段空，让 run_act_a 走草稿合并逻辑填）───
    QuarterScript(
        quarter=3,
        plan=_p(),   # plan 仍空：run_act_a 会调 ai_advise(draft=True) 拿草稿再合并
        save_slot_after=SLOT_Q3_TUTORIAL,
        # narrate 里把 ai_question 写明，run_act_a 从这里读出（不在 QuarterScript 加新字段）
        narrate="让 AI 帮我起草：再扩 2 块农业。ai_question=『帮我起草一份再扩 2 块农业』。"
                "**存档点 ① `q3-tutorial-done`**",
    ),
    # ── Q4 玩家拒绝 AI（剧情最高峰）───────────────────────────────
    QuarterScript(
        quarter=4,
        plan=_p(
            builds=[
                {"plot_id": _Q4_IND_PID, "plot_type": "industry", "target_level": 1, "product": "G"},
                {"plot_id": _Q4_AGRI_B1, "plot_type": "agriculture", "target_level": 1, "product": "B"},
                {"plot_id": _Q4_AGRI_B2, "plot_type": "agriculture", "target_level": 1, "product": "B"},
            ],
            purchases={"H-G": 2.0},   # industry L1 物资
        ),
        narrate="玩家拒绝 AI 建议，走自己的路：(0,1) 摆 1 块工业 G，"
                "(4,1)(5,1) 补 2 块农业 B。剧情戏剧性最高峰。",
    ),
    # ── Q5 粮食危机：不买粮看 H 跌 ────────────────────────────────
    QuarterScript(
        quarter=5,
        plan=_p(),   # 全空：purchases={}，food={} 一颗口粮都不买
        save_slot_after=SLOT_Q5_FIRST_CRISIS,
        narrate="咦，幸福度怎么跌了？（**不买粮看 H 崩**）"
                "**存档点 ② `q5-first-crisis`**",
    ),
]

# 第二幕 · 危机自救 → 押注未来 → 收割：Q6-Q12。
ACT2: list[QuarterScript] = [
    # ── Q6 自救：plot_plans 留空，run_act_a 会用当前已有地块全开 ──
    QuarterScript(
        quarter=6,
        plan=_p(
            purchases={"A": 5.0, "B": 3.0},
        ),
        narrate="原来口粮不买就掉 H，赶紧买 A:5 / B:3。"
                "plot_plans 空 → run_act_a 把当前已有地块全开（runner 的事）。",
    ),
    # ── Q7 押注未来：Q4 那块 G 厂升 L2 research ────────────────────
    #   ★ BuildOrder 字段＝{plot_id, plot_type, target_level, product?}，**无 route 字段**。
    #     升研究线＝build 传 `product="研究"`：引擎按 `_INDUSTRY_KEY_TO_OUTPUT["研究_L2"]=(2,"研究")`
    #     命中 `ind_1_2_research` 工程（route 只是工程表内部标签，**不在 BuildOrder 上携带**）。
    #     见 engine/loader.py L85 + engine/construction.py find_work_for_build。
    QuarterScript(
        quarter=7,
        plan=_p(
            builds=[
                # 升级不是新建：target_level=2 + product="研究"（让引擎命中 ind_1_2_research）
                # plot_id 引用 Q4 的 industry G（_Q4_IND_PID = "0,1"）
                {"plot_id": _Q4_IND_PID,
                 "plot_type": "industry",
                 "target_level": 2,
                 "product": "研究"},
            ],
            purchases={"H-G": 3.0, "A": 5.0, "B": 3.0},
        ),
        narrate="G 厂 8 季工期，赌一把（升 research: product='研究'）。",
    ),
    # ── Q8 中途盘点 ────────────────────────────────────────────────
    QuarterScript(
        quarter=8,
        plan=_p(),   # 全空
        save_slot_after=SLOT_Q8_MIDWAY_DOUBT,
        narrate="现金紧张，研究地块还没回本。"
                "**存档点 ③ `q8-midway-doubt`**",
    ),
    # ── Q9 反弹：口粮链闭合 ───────────────────────────────────────
    QuarterScript(
        quarter=9,
        plan=_p(
            purchases={"A": 5.0, "B": 3.0, "C": 1.0, "D": 0.5},
        ),
        narrate="口粮链闭合，H 上升。",
    ),
    # ── Q10 新产品：G 厂建成（购 H-G 3 是配合工业投产）────────────
    QuarterScript(
        quarter=10,
        plan=_p(
            purchases={"A": 5.0, "B": 3.0, "C": 1.0, "D": 0.5, "H-G": 3.0},
        ),
        narrate="G 厂建成卖高价。",
    ),
    # ── Q11 现金爆炸：补 2 块资源 F ────────────────────────────────
    QuarterScript(
        quarter=11,
        plan=_p(
            builds=[
                {"plot_id": _Q11_RES_F1, "plot_type": "resource", "target_level": 1, "product": "F"},
                {"plot_id": _Q11_RES_F2, "plot_type": "resource", "target_level": 1, "product": "F"},
            ],
            purchases={"A": 5.0, "B": 3.0, "C": 1.0, "D": 0.5, "H-G": 3.0},
        ),
        narrate="现金爆炸：口粮照旧 + 摆 2 块一级资源 F（位置任意，挑了 (6,0)(7,0)）。",
    ),
    # ── Q12 里程碑收官 ────────────────────────────────────────────
    QuarterScript(
        quarter=12,
        plan=_p(
            purchases={"A": 5.0, "B": 3.0, "C": 1.0, "D": 0.5},
        ),
        save_slot_after=SLOT_Q12_BEFORE_FINALE,
        narrate="收个好看尾，进第三幕前存个档。"
                "**存档点 ④ `q12-before-finale`**",
    ),
]

# 第三幕 · 冲刺：Q13-Q20 —— **留给下一棒子代理填**。
# 任务书：B 线会用 A 线 Q13-Q20 跑，本棒只需要保证 AC1+ACT2=12 季完整。
ACT3: list[QuarterScript] = []
SCRIPT: list[QuarterScript] = ACT1 + ACT2 + ACT3
assert len(SCRIPT) == 12, f"剧本应为 ACT1(5)+ACT2(7)=12 季，实际 {len(SCRIPT)} 季"
assert [s.quarter for s in SCRIPT] == list(range(1, 13))


# ──────────────────────────────────────────────────────────────────
# 四、快照与执行器
# ──────────────────────────────────────────────────────────────────
@dataclass
class QuarterSnapshot:
    """一季度结算前后的现场。"""
    quarter: int
    state: dict                          # 结算前抓的 /api/state
    report: dict                         # 结算后抓的 /api/report（本季刚结束的结果）
    map_data: dict                       # 结算前抓的 /api/map
    cash_after: float = 0.0              # report 里抓出来的本季末玩家现金
    gdp_this_quarter: float = 0.0        # report 里抓出来的本季 GDP
    slot_saved: str | None = None        # 本季结束若存档，存到哪个 slot


class PlaythroughRunner:
    """把剧本跑一遍，沿途抓快照。A 线 + B 线双轨。"""

    def __init__(self, client: DemoClient, out_dir: Path):
        self.client = client
        self.out_dir = out_dir
        self.raw_dir = out_dir / "raw"
        self.slots_dir = self.raw_dir / "slots"
        self.snapshots_a: list[QuarterSnapshot] = []
        self.snapshots_b: list[QuarterSnapshot] = []
        self.finale_a: dict = {}
        self.finale_b: dict = {}

    # ---------- 输出目录 ----------

    def setup(self) -> None:
        """创建输出目录。骨架模式下只需要这个。"""
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        self.slots_dir.mkdir(parents=True, exist_ok=True)

    # ---------- 内部小工（写文件） ----------

    def _dump_json(self, path: Path, data: dict) -> None:
        """写一份 JSON 到 raw/，保持 UTF-8 + indent=2。"""
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ---------- A 线 ----------

    @staticmethod
    def _extract_ai_question(narrate: str) -> str | None:
        """从 narrate 文本里抓 `ai_question=『...』` 的问句（任务书约定藏字段的位置）。

        找不到就返回 None。⚠️ 剧本 v2 规定：ai_question 不加 QuarterScript 字段、
        塞在 narrate 字符串里，由 run_act_a 从这里 parse 出来再调 client.ai_advise。
        """
        if not narrate:
            return None
        m = re.search(r"ai_question=『([^』]+)』", narrate)
        return m.group(1).strip() if m else None

    @staticmethod
    def _merge_plan(base: dict, suggested: dict) -> dict:
        """把 AI 草稿叠加到 baseline plan 上（Q3 用）。

        字段级策略（server/app.py::plan_from_dict 的形状）：
          plot_plans / purchases / food —— dict，用 suggested 覆盖 base 的同名键；
          builds                  —— list，suggested 的整段接到 base 后面；
          其它 AI 乱给的字段拒绝合入（防脏写）。

        ⚠️ 这是演示用合并，非通用 patch；只承认 plan_from_dict 认知的 5 个键。
        """
        if not isinstance(suggested, dict):
            return base
        out = {**base, "player_id": base.get("player_id") or PLAYER}
        for key in ("plot_plans", "purchases", "food"):
            merged = dict(base.get(key) or {})
            suggested_val = suggested.get(key)
            if isinstance(suggested_val, dict):
                merged.update(suggested_val)
            out[key] = merged
        builds_out: list = list(base.get("builds") or [])
        if isinstance(suggested.get("builds"), list):
            builds_out.extend(suggested["builds"])
        out["builds"] = builds_out
        return out

    @staticmethod
    def _empty_plan() -> dict:
        """Q13-Q20 摆烂重放用的空 plan（形状对齐 plan_from_dict L173-212）。"""
        return {
            "player_id": PLAYER,
            "plot_plans": {},
            "builds": [],
            "purchases": {},
            "food": {},
        }

    @staticmethod
    def _auto_fill_plot_plans(plan: dict, state_before: dict) -> dict:
        """plot_plans 空 → 把当前已有 non-idle 地块全开（active=True, u=1.0）。

        用于 Q6 等『让 runner 给我全开』的剧本季（narrate 已注明）。
        state_before["plots"] 是 dict[str, plot_dict]。
        """
        plots = (state_before.get("plots")
                 or state_before.get("world", {}).get("plots")
                 or {})
        if not isinstance(plots, dict) or not plots:
            return plan
        if plan.get("plot_plans"):
            return plan
        filled = {
            pid: {"plot_id": pid, "active": True, "u": 1.0}
            for pid in plots.keys()
        }
        merged = dict(plan)
        merged["plot_plans"] = filled
        return merged

    def _run_one_quarter(
        self,
        game_id: str,
        q: int,
        plan: dict,
        save_slot_after: str | None,
        dump: bool,
        is_a: bool,
    ) -> QuarterSnapshot:
        """跑一季的通用流程（A/B 共用，只是落盘与快照列表不同）。

        顺序：get_state（含 narratives=0）→ get_map → submit → end_quarter
              → next_quarter → [save_slot] → 追加 QuarterSnapshot。
        """
        state_before = self.client.get_state(game_id, PLAYER)
        map_before = self.client.get_map(game_id, PLAYER)
        self.client.submit_plan(game_id, PLAYER, plan)
        eq_resp = self.client.end_quarter(game_id, PLAYER)
        report = eq_resp.get("report") or {}
        self.client.next_quarter(game_id, PLAYER)

        slot_id: str | None = None
        if save_slot_after:
            save_resp = self.client.save_slot(game_id, PLAYER, save_slot_after)
            slot_id = save_resp.get("slot_id")
            # 落一份 slot 的元数据给 demo.md 的 slot_listing 用
            try:
                self._dump_json(
                    self.slots_dir / f"{save_slot_after}.json",
                    {
                        "slot_id": slot_id,
                        "game_id": game_id,
                        "slot": save_slot_after,
                        "quarter": q,
                        "cash_after": float(report.get("cash_after") or 0.0),
                        "value_added": float(report.get("value_added") or 0.0),
                    },
                )
            except OSError:
                pass  # 非致命：仅展示用，不挡主流程

        snap = QuarterSnapshot(
            quarter=q,
            state=state_before,
            report=report,
            map_data=map_before,
            cash_after=float(report.get("cash_after") or 0.0),
            gdp_this_quarter=float(report.get("value_added") or 0.0),
            slot_saved=slot_id,
        )
        (self.snapshots_a if is_a else self.snapshots_b).append(snap)

        if dump:
            prefix = f"Q{q:02d}"
            self._dump_json(self.raw_dir / f"{prefix}-state.json", state_before)
            self._dump_json(self.raw_dir / f"{prefix}-report.json", report)
            self._dump_json(self.raw_dir / f"{prefix}-map.json", map_before)
        return snap

    def run_act_a(self) -> None:
        """跑 A 线 Q1-Q20。

        Q1-Q12：按 SCRIPT 剧本走；Q3/Q4/Q6 三季 AI 真调（用户裁定）；
        Q13-Q20：直接摆烂 —— submit 空 plan，存托住 B 线确定性对比的对照组。

        ⚠️ 关键设计：SCRIPT 只 12 季（剧本 v2 收官版），ACT3=[] 是**刻意**。
           A 线 Q13-Q20 必须在本方法里跑，这样 verify_determinism 才能用
           snapshots_a[12:20] vs snapshots_b 两份 8 项对齐。
        """
        state0 = self.client.new_game(GAME_A, PLAYER)
        self._dump_json(self.raw_dir / "Q00-new_game.json", state0)

        # Q1-Q12 剧本季
        for script in SCRIPT:
            q = script.quarter
            ai_summary = ""
            plan_to_submit: dict = dict(script.plan or {})
            plan_to_submit.setdefault("player_id", PLAYER)
            for k in ("plot_plans", "purchases", "food", "builds"):
                plan_to_submit.setdefault(k, {} if k != "builds" else [])

            # ② AI 真调（Q3 起草合并 / Q4 拒 AI / Q6 参考不全采纳）
            if q in (3, 4, 6):
                ai_q = self._extract_ai_question(script.narrate)
                if ai_q:
                    mode = "plan" if q in (3,) else "answer"
                    try:
                        resp = self.client.ai_advise(
                            GAME_A, PLAYER,
                            question=ai_q,
                            draft=(mode == "plan"),
                            current_plan=plan_to_submit,
                            mode=mode,
                        )
                        advice = resp.get("advice") or {}
                        ai_summary = advice.get("summary") or ""
                        suggested = advice.get("suggested_plan")
                        if q == 3 and isinstance(suggested, dict) and suggested:
                            plan_to_submit = self._merge_plan(plan_to_submit, suggested)
                        # q==4 / q==6：剧情设计要玩家"拒 AI" or "参考但不全采纳"，
                        # 保持 plan_to_submit 原样即"玩家自建"。
                    except RuntimeError as e:
                        ai_summary = f"[AI 调用异常：{e}]"
                self._dump_json(
                    self.raw_dir / f"Q{q:02d}-ai.json",
                    {"quarter": q, "mode": mode, "summary": ai_summary},
                )

            # ③ Q6 季【全开已建地块】：plot_plans 空就从 state 抓回来全开
            if not plan_to_submit.get("plot_plans"):
                current_state = self.client.get_state(GAME_A, PLAYER)
                plan_to_submit = self._auto_fill_plot_plans(plan_to_submit, current_state)

            # ④-⑦ 跑本季并落盘（含 save_slot）
            snap = self._run_one_quarter(
                game_id=GAME_A,
                q=q,
                plan=plan_to_submit,
                save_slot_after=script.save_slot_after,
                dump=True,
                is_a=True,
            )
            # narrate 里追加 AI summary 便于 demo.md 直读
            if ai_summary:
                snap.state["_ai_summary"] = ai_summary  # 挂 state 上不改 schema

        # Q13-Q20 摆烂（A 线对照组；B 线用同形状 plan 跑）
        for q in range(13, 21):
            self._run_one_quarter(
                game_id=GAME_A,
                q=q,
                plan=self._empty_plan(),
                save_slot_after=None,
                dump=True,
                is_a=True,
            )

        # ⑧ 终局三张单
        self.finale_a = self.client.get_finale(GAME_A, PLAYER)
        try:
            self._dump_json(self.raw_dir / "finale-a.json", self.finale_a)
        except OSError:
            pass

    # ---------- B 线 ----------

    def run_act_b_from_q12(self) -> None:
        """B 线：从 A 线 Q12 的存档恢复成 demo-b，用**完全相同的空 plan** 重放 Q13-Q20。

        关键设计（剧本 v2 收官）：
        - SCRIPT 只到 Q12，ACT3=[]；意味着 A 线 Q13-Q20 也都是空 plan 摆烂。
        - 所以 B 线 Q13-Q20 同样用 _empty_plan()，与 A 线 [12:20] 数字**分毫不差**
          才是确定性引擎的铁证。
        - slot_id 形如 `demo-a@q12-before-finale`（server/slots.py::make_slot_id）。
        """
        slot_id = f"{GAME_A}@{SLOT_Q12_BEFORE_FINALE}"
        load_resp = self.client.load_slot(slot_id, GAME_B)
        try:
            self._dump_json(self.raw_dir / "B-load-slot.json", load_resp)
        except OSError:
            pass

        for q in range(13, 21):
            self._run_one_quarter(
                game_id=GAME_B,
                q=q,
                plan=self._empty_plan(),
                save_slot_after=None,
                dump=False,      # B 线快照免一盘 JSON，raw/ 里已太多
                is_a=False,
            )

        self.finale_b = self.client.get_finale(GAME_B, PLAYER)
        try:
            self._dump_json(self.raw_dir / "finale-b.json", self.finale_b)
        except OSError:
            pass

    # ---------- 确定性断言 ----------

    def verify_determinism(self) -> list[str]:
        """比对 A/B 两条线 Q13-Q20 的现金 + GDP 是否完全一致。

        数据源：
          · A 线 Q13-Q20 快照 = self.snapshots_a[12:20]（run_act_a 已跑完 20 季）
          · B 线 Q13-Q20 快照 = self.snapshots_b（run_act_b_from_q12 只跑 8 季）

        比对方式：math.isclose(rel_tol=1e-9, abs_tol=1e-9) 判 float 等价。
        （引擎自报是确定性的，理论应严格等；用 isclose 是防止 JSON 序列化中
        出现 0.30000000000000004 之类的浮点尾巴。）

        返回：
            空 list   —— 完全一致（铁证成立）；
            非空 list —— 不一致的季度标识 ["Q14", "Q17"]。
        """
        if len(self.snapshots_a) < 20:
            return [f"Q{q}" for q in range(13, 21)]  # A 线没跑完 20 季，判全错
        if len(self.snapshots_b) < 8:
            return [f"Q{q}" for q in range(13, 21)]  # B 线不足 8 季，判全错

        a_tail = self.snapshots_a[12:20]
        mismatches: list[str] = []
        for offset, (sa, sb) in enumerate(zip(a_tail, self.snapshots_b)):
            q_num = 13 + offset
            cash_ok = math.isclose(sa.cash_after, sb.cash_after,
                                   rel_tol=1e-9, abs_tol=1e-9)
            gdp_ok = math.isclose(sa.gdp_this_quarter, sb.gdp_this_quarter,
                                  rel_tol=1e-9, abs_tol=1e-9)
            if not (cash_ok and gdp_ok):
                mismatches.append(f"Q{q_num}")
        return mismatches

    # ---------- demo.md ----------

    def write_demo_md(self) -> Path:
        """把 snapshots + finale + 分支对比结果渲染成 demo.md 并写盘。"""
        text = render_demo_md(self)
        path = self.out_dir / "demo.md"
        path.write_text(text, encoding="utf-8")
        return path


# ──────────────────────────────────────────────────────────────────
# 五、demo.md 模板 + 渲染函数
# ──────────────────────────────────────────────────────────────────
DEMO_MD_TEMPLATE = """# DEMO · 20 季运行实录

> 生成时间：{timestamp}
> 引擎：region-sim · 确定性季度结算
> 本局：A 线 = demo-a · B 线 = demo-b（从 Q12 存档恢复重放）

---

## 第一幕 · 起势（Q1-Q5）

{narrative_act1}

| 季 | 现金 | 本季 GDP | H | 备注 |
|---|---|---|---|---|
{table_act1}

**存档点 ①：`demo-a@q5-first-base`**

## 第二幕 · 转型（Q6-Q12）

{narrative_act2}

| 季 | 现金 | 本季 GDP | H | 备注 |
|---|---|---|---|---|
{table_act2}

**存档点 ②：`demo-a@q12-research-mid`**

## 第三幕 · 冲刺（Q13-Q20）

{narrative_act3}

| 季 | 现金 | 本季 GDP | H | 备注 |
|---|---|---|---|---|
{table_act3}

## 终局结算

{finale}

---

## 存档恢复确定性验证（Q13-Q20）

**操作**：B 线 `demo-b` 从 A 线 `demo-a@q12-research-mid` 恢复，用完全相同的 plans 跑到 Q20。

**断言**：B 线每季 cash / gdp 与 A 线对应季度完全一致。

| 季 | A 线现金 | B 线现金 | A 线 GDP | B 线 GDP | 一致？ |
|---|---|---|---|---|---|
{table_branch_compare}

**结论**：{branch_verdict}

---

## 存档文件

```text
{slot_listing}
```

## 原始数据

见 `./raw/`：
- `Q{{NN}}-{{state,report,map}}.json`
- `slots/{{slot}}.json`
"""


def render_demo_md(runner: PlaythroughRunner) -> str:
    """把 PlaythroughRunner 抓到的快照渲染成 demo.md 文本。

    由下一阶段实现 —— 要做的小事：
      1. 从 runner.snapshots_a / snapshots_b / finale_a 拿数字；
      2. 把 ACT1/ACT2/ACT3 的 narrate 串成段落；
      3. 每幕的 table 行通过 `| Q{n} | {cash} | {gdp} | {h} | {备注} |` 拼出；
      4. table_branch_compare 走 verify_determinism 的结果画 √ / ×；
      5. slot_listing 列出 runner.slots_dir 下所有 JSON 文件名；
      6. 返回 DEMO_MD_TEMPLATE.format(...)。
    """
    # TODO: 由下一阶段实现
    raise NotImplementedError("由下一阶段实现")


# ──────────────────────────────────────────────────────────────────
# 六、入口
# ──────────────────────────────────────────────────────────────────
def main() -> int:
    p = argparse.ArgumentParser(description="跑 20 季 DEMO + 存档恢复确定性验证")
    p.add_argument("--server", default="http://127.0.0.1:8000",
                   help="region-sim HTTP server base URL（mock 模式下不连）")
    p.add_argument("--out", required=True,
                   help="输出目录，demo.md 和 raw/ 都写在这里")
    p.add_argument("--mock", action="store_true",
                   help="骨架模式：不连真 server，仅验证脚本可启动 + 目录已创建")
    args = p.parse_args()

    out_dir = Path(args.out)
    transport: Transport = (
        MockTransport() if args.mock else HttpTransport(args.server)
    )
    client = DemoClient(args.server, transport=transport)
    runner = PlaythroughRunner(client, out_dir)
    runner.setup()

    print(f"[DEMO] out_dir = {out_dir}")
    print(f"[DEMO] server  = {args.server}")
    print(f"[DEMO] mock    = {args.mock}")

    if args.mock:
        print("[DEMO] 骨架模式：仅验证脚本结构可导入、可启动")
        print(f"[DEMO] raw_dir   = {runner.raw_dir}")
        print(f"[DEMO] slots_dir = {runner.slots_dir}")
        print(f"[DEMO] 剧本 20 季已加载：Q1~Q20（A 线）；B 线待 A 线跑完后从 Q12 恢复")
        return 0

    # 真跑（骨架阶段会撞 NotImplementedError —— 由下一阶段点亮）
    runner.run_act_a()
    runner.run_act_b_from_q12()
    inconsistent = runner.verify_determinism()
    md_path = runner.write_demo_md()
    print(f"[DEMO] demo.md 写入:{md_path}")
    print(f"[DEMO] 分支一致性：{'通过' if not inconsistent else f'失败：{inconsistent}'}")
    return 0 if not inconsistent else 1


if __name__ == "__main__":
    sys.exit(main())
