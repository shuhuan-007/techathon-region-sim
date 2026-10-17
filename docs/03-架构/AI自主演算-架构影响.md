# AI 自主演算 · 架构影响

> 你拍板：**允许 AI 自主演算**，机制复杂不要求玩家掌握；**玩家是掌舵人，AI 是全权秘书**；
> **最后填数据也可以 AI 代劳**。
>
> 这条改变了一个架构细节，但**改动很小、收益很大**。这份说清它。

---

## 一、先确认你的理解（基本正确）

> "在玩家的执行过程中，没有数据变化，只有 AI 对 AI 能看到的数据给他解释，
> 并且帮他起草事实可执行的操作。最后结算的时候统一执行？"

**是的，完全正确。** 而且《规则基线V7》里已经写了同一条：

| 基线原文 | 位置 |
|---|---|
| **程序决定事实，AI 只负责解释。** | §0 |
| 财报解读 / 新闻 / **合同起草** / 政策说明 = AI | §0 |
| **AI 不得改规则、造补贴、改价格、定处罚。** | §0 |
| **AI 只解释和提醒，不自行判罚。** | §7 |

**所以"AI 起草操作"本来就在设计里**（契约起草就是），只是之前没说清**它能算到什么程度**。

---

## 二、你这条拍板带来的架构增量：**What-If 引擎**

### 2.1 问题

**如果 AI 能自主演算，它必须能"算但不改"。**

玩家问："我在这儿建个服务会怎样？"
AI 要能回答，**但不能真的把服务建上去**。

**所以引擎需要两个入口**：

```python
# 真结算：改状态
advance(state, plans) -> GameState

# 试算：不改状态，只给报告
preview(state, plans) -> QuarterReport
```

### 2.2 为什么这几乎是免费的

**因为 `advance()` 已经是纯函数。**

```python
def preview(state: GameState, plans: dict[str, Plan]) -> QuarterReport:
    """试算：不改真实状态，只返回报告。AI 和 UI 都用它。"""
    shadow = deepcopy(state)          # 影子状态
    result = advance(shadow, plans)   # 跑一遍真引擎
    return result.report              # 只要报告
```

**成本**：一次 `deepcopy`。
**你的状态有多大**：3×4 = 12 个地块 + 几个玩家。**深拷贝开销可以忽略。**

### 2.3 唯一的硬约束 ⚠️

> **`preview` 必须和 `advance` 共用同一份代码。绝不允许各写一份公式。**

**原因**：如果 AI 用一套算法演算、结算用另一套，**AI 说的数会和真实结算不一致**。
玩家按 AI 说的做，结果钱不对——**这是最伤信任的 bug**。

**做法**：**一份实现，两个包装**。

```python
def _run(state, plans, *, commit: bool) -> GameState:
    """唯一实现。commit=False 时 caller 丢弃结果即可。"""
    ...全部十步...

def advance(state, plans):
    return _run(state, plans, commit=True)

def preview(state, plans):
    shadow = deepcopy(state)
    return _run(shadow, plans, commit=False).report
```

> **绝不出现两个 `compute_xxx()` 函数。** 这是代码审查的红线。

### 2.4 顺手白拿的三个能力

| 能力 | 说明 |
|---|---|
| **AI 回答"如果…会怎样"** | 正是你要的自主演算 |
| **UI 悬停提示不用后端改动** | 前端也能调 `preview` 显示"建了之后会怎样" |
| **测试可对拍** | `test_preview_equals_advance`：同一输入，试算报告的**最终状态必须等于**真结算的状态 |

---

## 三、"AI 代劳填数据"意味着什么

你说**最后填数据也可以 AI 代劳**（AI 直接生成 `Plan`）。这是 `Plan`=Command 的自然延伸：

```
玩家说：我想搞农业，但不太懂怎么配
   ↓
AI 生成 Plan（或 Plan 草稿）
   ↓
玩家 看一眼 / 直接提交
```

**架构上的三个要求**：

| # | 要求 | 为什么 |
|---|---|---|
| 1 | **`Plan` 必须完全可序列化** | AI 生成的是数据，不是函数调用 |
| 2 | **AI 生成的 Plan 走同一条校验** | `engine.validate.plan()` —— **AI 没有特权** |
| 3 | **AI 用 `preview` 验证自己的 Plan** | 生成 → 试算 → 看结果合理 → 再提交 |

**第 2 条最重要**：**AI 生成的计划必须和玩家手填的走同一个校验入口。**
否则 AI 就能绕过规则（比如用完现金），这等于"AI 改了事实"。

> **一句话**：**AI 可以有个"手"，但那只手也必须过安检。**

---

## 四、架构上的分层（修正版）

**之前**：

```
engine 决定事实
ai     只解释事实        ← 太窄了
web    只显示事实
```

**现在**：

```
┌────────────────────────────────────────────┐
│ engine/     决定事实                         │
│   advance()   ← 真结算，改状态                │
│   preview()   ← 试算，不改状态 ★ 新增         │
│   validate()  ← 校验，AI 和玩家共用           │
└────────────────────────────────────────────┘
              ↑ 只读调用          ↑ 只读调用
┌──────────────────────────┐  ┌──────────────┐
│ ai/    解释 + 演算 + 起草  │  │ web/  显示    │
│   只能通过 preview/validate │  │  + 试算提示   │
└──────────────────────────┘  └──────────────┘
```

**AI 的权限重新表述**：

| 之前 | 现在 |
|---|---|
| AI 只解释 | **AI 可解释、可试算、可起草** |
| — | **但 AI 不能 commit**（只能生成 Plan，由玩家/系统提交） |

> **关键区分**：**"能算" ≠ "能改"。**
> AI 可以跑一百次 `preview`，但**永远调不到 `advance`**。
> 这一条用代码强制：**`ai/` 不 import `advance`，只 import `preview`。**

---

## 五、这样安全吗（风险评估）

**你的判断是"玩家是掌舵人，AI 是全权秘书"。这个定位是好的**，但有两个风险要知道：

| 风险 | 说明 | 缓解 |
|---|---|---|
| **AI 算错** | 它自己推演出错误结论 | ✅ **用 `preview` 而不是让它"心算"** —— 数字来自引擎，不是 LLM |
| **AI 越权** | 生成一个不合法的 Plan | ✅ **共用一个 `validate`** |
| **玩家不理解** | 全权秘书 → 玩家不知道发生了什么 | ✅ **月报要给出"为什么"**（`ledger` 流水 + AI 解读） |

**第 1 条是关键**：

> **"允许 AI 自主演算"的正确实现，是让 AI 调 `preview`，而不是让语言模型自己算数。**
>
> **LLM 做算术不可靠。** 但 LLM **会调用工具**。
> 所以：**AI 负责"决定试算什么"，引擎负责"算出结果"。**

**这样既满足"AI 自主演算"，又保证数字永远对。**

---

## 六、落地要点

### 6.1 引擎要新增的

```python
# engine/pipeline.py
def _run(state, plans, *, commit: bool) -> GameState: ...
def advance(state, plans) -> GameState: ...
def preview(state, plans) -> QuarterReport: ...      # ★ 新增
```

### 6.2 AI 层要有的能力

```python
# ai/tools.py —— AI 可调用的工具（只读 + 起草）
def tool_preview(state, plans) -> Report        # 试算
def tool_validate(state, pid, plan) -> list[str]  # 校验，返回问题列表
def tool_draft_plan(state, pid, intent: str) -> Plan  # 起草
# ★ 没有 tool_commit —— AI 无权提交
```

> ✅ **2026-10-17 对齐现状（已落地，不改上图结构）**：
> - `ai/tools.py` 三工具已实装（`tool_preview`/`tool_validate`/`tool_draft_plan`），
>   与上图一致；外加了**调用时自检** `assert_import_boundary()`（查 `sys.modules`
>   实际加载，黑名单＝`server.session`/`server.registry`/`storage`/`advance`），
>   把「AI 无 commit」从注释变成了代码。
> - **叙事这一路与上图不同**：不是 LLM 心算新闻，而是 `ai/narrate.py`
>   （`narrate_news`/`narrate_report`，提示词在 `ai/prompts/news.md` 与 `report.md`）
>   把 `engine/news.py` 的事实包递给模型、校验返回。**默认离线闸**：
>   不设环境变量 `NARRATE_OFFLINE=0` 就**不真调模型**（防离线测试误连网关）。
> - **生成时机**：结算后、下季行动前、**按季缓存**（同季一次）；
>   `end_quarter`/`next_quarter` 响应**不生成**（`with_narratives=False`），
>   由 `GET /api/state`（`session.state_for`）触发。
> - HTTP 侧：`server/llm_client.py`（网关薄客户端，任何失败回 `ok=False` 不抛）
>   ＋ `server/ai_host.py::advise` ＋路由 `POST /api/ai/advise`
>   （请求体 `{game, player, question?}`，可带 `preview_plan` 先试算）。

**服务层接口命名（✅ 2026-10-16 采纳队友建议）**：

```
get_rules       查询规则和版本
get_state       查询玩家状态
validate_plan   校验方案，不改变状态      ← 对应上面的 tool_validate
simulate_plan   试算方案，不改变状态      ← 对应上面的 tool_preview
submit_plan     玩家确认后正式提交        ← 服务端 submit，AI 无权调用
```

- 试算结果必须**带规则版本与状态版本**；**正式执行前重新校验**，防止执行过期方案。
- **架构不变**：仍是"`preview` 与 `advance` 共用一份实现，**AI 永远调不到 `advance`**"（§八）。
- 详见 [`队友交付-合并记录.md`](../04-交接/队友交付-合并记录.md) §二-2 与《规则基线》§11.4。

### 6.3 测试要加的

```python
def test_preview_equals_advance():
    """试算的最终状态必须等于真结算的状态。"""
    s1, s2 = deepcopy(state), deepcopy(state)
    r = preview(s1, plans)
    final = advance(s2, plans)
    assert r.final_cash == final.players["p1"].cash
    assert s1 == state          # ★ 试算不改原状态
```

**这个测试是整个"AI 自主演算"的安全网。**

---

## 七、修正后的完整图景

```
玩家（掌舵人）
   │  说意图
   ↓
AI 秘书
   │  ① 读 report（引擎算好的事实）
   │  ② 调 preview 试算"如果…会怎样"   ← 数字来自引擎
   │  ③ 起草 Plan
   │  ④ 调 validate 自检
   ↓
玩家确认（或直接提交）
   ↓
服务端 submit → 收齐 → ★ 屏障
   ↓
engine.advance()  ← 唯一能改状态的地方
   ↓
月报 + ledger 流水 → AI 解读 → 玩家看
```

**整局中 `advance()` 只被调用一次（每人每季）。其他所有演算都是 `preview`。**

---

## 八、一句话总结

> **你的理解是对的**：行动阶段不改数据，AI 解释 + 起草，结算时统一执行。
>
> **你这条"允许 AI 自主演算"带来的唯一架构增量**：
> **引擎加一个 `preview()`** —— 和 `advance()` **共用同一份实现**，只是不改状态。
>
> **两条铁律**：
> 1. **`preview` 和 `advance` 必须共用代码**（否则 AI 说的数和结算不一致）
> 2. **AI 能调 `preview` 和 `validate`，永远调不到 `advance`**（能算 ≠ 能改）
>
> **"AI 自主演算"的正确实现 = AI 决定试算什么，引擎负责算出结果。**
> **别让 LLM 心算——它做算术不可靠，但它会调工具。**
