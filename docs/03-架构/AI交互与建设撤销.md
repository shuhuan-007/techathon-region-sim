# AI 交互与建设撤销（设计契约，2026-10-18）

> 用户反馈：
> 3. 建设计划无法取消，需要让 AI 助手取消；
> 4. AI 要能回答机制问题（可查规则）；玩家让它规划时只需说“做了什么 + 为什么”；
>    自己填不要改动玩家已经手动填的，至少要先问。
>
> 本文只定**接口与行为**，不写具体实现；实现按此拆给前后端子代理。

---

## 一、原则

1. **AI 仍然只起草**：不提交、不结算、不写存档。
2. **玩家手填优先**：AI 的结果默认**不覆盖**非空本地草稿；前端必须弹确认。
3. **机制问答与规划分流**：问题要解释 → 查规则原文回答；问题要动作 → 出计划。
4. **撤销是一条“编辑”**，不是整份重填：只删指定建造，保留玩家已有的其它安排。

---

## 二、HTTP 契约

`POST /api/ai/advise` body 在现有 `{game, player, question, draft, preview_plan}` 上增加：

```json
{
  "current_plan": { "player_id": "p1", "plot_plans": {}, "purchases": {}, "food": {},
                    "builds": [ ... ], "contracts": [] },
  "mode": "auto | answer | plan"
}
```

- `current_plan`：前端**当前表单里的草稿**（未提交也算）。可空/省略。
- `mode`：
  - `answer`：只回答机制，不产出计划；
  - `plan`：按当前草稿起草/编辑；
  - `auto`（默认）：后端按问题关键词判（见 §四）。

响应仍为 `{ok, advice}`；`advice` 新增/沿用：

- `suggested_plan`：编译后的完整 Plan（可能来自 current_plan + 编辑）；
- `plan_notes`：引擎校正/编辑说明；
- `plan_preview`：真跑试算摘要；
- `plan_action`：`"created" | "edited" | "removed" | "none"`（给前端提示用，可空）。

---

## 三、AI 输出的编辑语法（draft=true）

模型仍可回完整 Plan 形状；**另支持两种轻量编辑**，避免“取消一条建造却重填整份”：

```json
{
  "why": "把 0,0 的建造撤销，其余安排不动。",
  "remove_builds": ["0,0"]
}
```

```json
{
  "why": "在 4,6 补一块农业 A，吃 5,6 的邻接。",
  "builds": [
    {"plot_id": "4,6", "plot_type": "agriculture", "target_level": 1, "product": "A"}
  ]
}
```

规则：

- 有 `remove_builds` → 从 `current_plan.builds` 删除这些 `plot_id`；
- 有 `builds` → **追加/替换**到 `current_plan.builds`（同 `plot_id` 以新指令为准）；
- 有 `plot_plans` → 只更新列出的地块，**未列出的保持 current_plan 原值**（编辑模式不默认全开工）；
- 有 `purchases` / `food` → 整体替换这两块；未出现则保持原值；
- 都没有 → 按旧行为：整份 Plan 起草（空表单时）。
- `why` 只用于聊天摘要，不参与规则。

编译器 `compile_draft(..., current_plan=None)`：
- `current_plan` 非空时先 deep copy 作为 base，再按上面应用编辑；
- 仍走“规整 → validate → preview → 缩量”；
- **不得**因为 `current_plan` 里有些地块没写 `plot_plans` 就默认全开工（编辑模式尊重玩家原样）。

---

## 四、意图分流（auto）

后端用轻量关键词判，不额外调模型：

- **机制问答**（`mode=answer`）：含 `怎么算/为什么/是什么/机制/规则/解释/原理/公式/多少/区别/能不能`，且**不含**强动作词（`建/造/取消/撤销/删/安排/起草/填/开工/停工/买/卖/改`）。
- **规划/编辑**（`mode=plan`）：含强动作词，或用户明确说“帮我规划/填一下/取消”。
- 其余：`mode=answer`（先解释，避免强行填表）。

`mode=answer` 时：
- 不进入 `compile_draft`；模型回自由文本；
- 发给模型的 user message 增加 `[规则摘录]`：按问题关键词从 `rules_digest` 取 1–3 节原文（粮食/口粮→3.3、邻接→3.2、价格→4.1、建造→5.1/5.2、效率→3.1、税→7.1、合同→7.2、开局→5.3、AI 边界→11.4）。
- 摘要直接是模型回答；不带 `suggested_plan`。

---

## 五、前端行为

1. **请求**：`/api/ai/advise` 固定带 `current_plan: publicPlan()`；`mode` 由前端按同一关键词规则给（或省略让后端判）。
2. **应用计划**：
   - `planEmpty()` 为真 → 直接 `applySuggestedPlan`；
   - 非空 → `confirm("你已有草稿；应用 AI 计划会替换/编辑它，继续？")`，确认才应用；
   - AI 编辑（`remove_builds`）编译后的完整计划仍走同一入口，确认文案写“应用后只保留 AI 返回的版本”。
3. **建设撤销 UI**：
   - “本季安排”面板增加“已排建设”列表：
     `地块 | 类型 | 目标等级 | 产物 | [撤销]`
   - 撤销 = `plan.builds.splice(i,1)` → `renderAll()` + `schedulePreview()`；
   - 不提交、不动 `STATE.plots`。
4. **聊天摘要**：
   - `plan` 模式：只显示“做了什么（N 条建造 / 撤销 X / 开工 N 块）+ 为什么（`why` 或 plan_notes）”，不要贴 JSON。
   - `answer` 模式：正常文字回答，可带规则节号。

---

## 六、验收

- 问“居民粮食怎么算？”→ 回答含 F_基准/配额公式与当前数字，不生成计划。
- 问“帮我建一块农业”且表单为空 → 落表并只解释做了什么/为什么。
- 表单已有手填建造 → AI 规划不自动覆盖，弹确认；取消则表单不变。
- 表单有 2 条建造，对 AI 说“取消 0,0 那条” → 只删 0,0，另一条保留，预览刷新。
- UI 撤销按钮：点一下删一条，地图/预览同步。
- 后端测试覆盖：current_plan 合并、remove_builds、未列出的 plot_plans 不被默认改写、机制问答走 answer 分支。


---

## 七、落地状态（2026-10-18）

- **前端**：`renderBuilds()` 逐条撤销（`splice`→`renderAll`→`schedulePreview`）；AI 请求带 `current_plan`＋`mode`；有本地草稿先 `window.confirm`，取消不落表；`plan_action/why` 直显。
- **后端**：`ai/plan_edit.py`（`merge_edits`/`extract_why`/`plan_action_of`）；`ai_host.advise(current_plan, mode)`；`remove_builds` 只删指定项；`protected_build_ids` 保证玩家草稿里的建造不被缩量删掉；编辑模式 `auto_place=False`，不偷偷搬玩家选的格。
- **明确撤销**：问题里“取消 9,9”这类带 plot_id 的指令走确定性意图解析，不依赖模型；命中才生成 `remove_builds`，其余保留。
- **机制问答**：`mode=answer` 时按关键词取规则原文；纯公式题（粮食/邻接/价格等）直接回规则原文，不再让模型用“当前人口=0”代替规则。
- **验证**：`test_plan_edit` 17/0、`test_ai_host` 39/0、`test_plan_compile` 22/0，全量 23 个测试文件 rc=0；8312 真机结算到第 9 季月报、「下一季」可点。
- **已知边界**：预算不足时优先保现有生产/保玩家已有建造，可能把 AI 新建造撤掉并在 `plan_notes` 说明；编辑模式不自动搬格。
