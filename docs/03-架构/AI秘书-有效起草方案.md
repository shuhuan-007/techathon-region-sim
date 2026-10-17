# AI 秘书 · 有效起草方案（2026-10-18）

> 症状：**AI 助手无法生成有效的方案填入游戏**。
> 用户原话：「这让 AI 秘书完全不成立。」
>
> 本文只解决一件事：**让 AI 起草的计划，能真正通过引擎校验、并在结算时跑通**。
> AI 的权限边界不变：**只起草、不提交、无 commit**（《规则基线》§11.4）。

---

## 一、复现（不是猜）

用真实存档 `region-sim/saves/1.json`（第 4 季、5 块运行地块、现金 416）真调一次
`qwen-flash`，`draft=True`：

- 模型回了 3 条 `builds`，地块是 **`4,5` / `5,4` / `6,3` —— 全是玩家自己已有的 1 级地块**；
- 模型**完全没写 `plot_plans`**（按引擎语义 = 5 块地本季全部不开工）；
- 引擎校验回 3 条 `build_not_an_upgrade`，`suggested_plan = null`，前端拿不到任何计划。

另用一个「10 条工业建造」的计划验证到第二个洞：
**`validate` 放行，`submit` 成功，点结算才报「资金不足」**。

## 二、根因（四条，按严重度）

1. **AI 看不到「能往哪建」**：局面里只有玩家自己的地块，没有空地清单。
   模型只能拿自己已有的地块去猜，于是造出「在已建成地块上新建 1 级」。
2. **AI 不知道「漏写 plot_plans = 不开工」**：提示词只解释 `plot_plans` 是调开工率，
   没说「要开工必须写进来」。模型经常整块省略，计划即使合法也是空转。
3. **`validate` 不查建造的钱**：`_check_cash` 只算 `purchases`。
   建造现金费 + 工程物资（如农业新建要 `H-A×1`）完全不查 →
   计划过校验、提交成功、结算才爆。提示词还写着「现金够不够不用你算」，
   把模型往这个坑里推。
4. **没有「编译器/修复层」**：模型输出直接进 `validate`，一处写错整份作废。
   而模型的错误是**可规整的**（已有地块当建造、产物写错、坐标越界、超预算）。

## 三、方案（四层）

### 1. 引擎校验硬化（`engine/validate.py`）

让「校验通过」尽量等价于「结算能跑」：

- 建造坐标必须在地图内、不能重复下指令；
- 农业/工业的 `product` 必须是该类型/该等级的真实路线；
- `_check_cash` 把**建造现金费（含开局减免）+ 工程物资（按 P0 估）+ 口粮缺口**算进去；
- 仍然只是**粗校验**，最终判据仍是结算第 5 步；但足以挡住「10 条工业」这类明显超支。

### 2. AI 可行动作快照（`ai/plan_context.py`）

把模型真正需要、且引擎已经知道的事实压成一小块：

- `[空地]`：地图上还没被任何玩家占的格子（截断到 N 个，说明总数）；
- `[可建]`：类型 / 现金 / 工程物资 / 按上季价的材料成本 / 折后现金 / 可选产物；
- `[可开工]`：自己每块 RUNNING 地块的 id、类型、等级、产物；在建的单独列；
- `[人口/口粮]`：岗位、人口、基准档/优秀档各商品需求；
- `[现金]` 与「先保证总支出 ≤ 现金」的硬提示。

### 3. 起草编译器（`ai/plan_compile.py`）—— 本方案的核心

模型仍然输出 Plan 形状的 JSON；**不直接信它**，先过编译器：

1. **规整**：类型中文/英文归一；产物大写；坐标格式；数字转 float；未知商品/口粮剔除。
2. **语义归位**：
   - `builds` 指向自己已建成的同级地块 → 改成「本季开工」（`plot_plans[u=1]`），并记一条 note；
   - 指向在建地块 → 丢弃并记 note；
   - 指向空地 → 保留建造；`target_level>1` 且是空地 → 压回 1 级并记 note；
   - 重复坐标 → 只留第一条。
3. **补默认**：所有 RUNNING 地块默认 `u=1`（模型显式给了 u 的以模型为准）；
   模型没给口粮时，按基准档需求补一份（可被下一步缩掉）。
4. **判决**：先 `validate_plan`；不过就返回 issues（不硬造）。
5. **试算**：`pipeline.preview` 在副本上真跑。资金不足 → **确定性缩量修复**：
   先按比例降 u（1 → 0.75 → 0.5 → 0.25 → 0），再逐条移除建造，最后降口粮；
   每步重跑 preview，直到跑通或到下限。
6. **出口**：只有「validate 过 + preview 跑通」才返回 `plan`；
   否则 `plan=None` + `issues`，绝不把坏计划交给前端。
   返回 `notes` 逐条说明「引擎替你改了什么」，前端必须显示。

### 4. 前端显示（`web/js/panel.js`）

- 草案落进表单时，除了「N 条建造」，再显示：
  - `plan_notes`（引擎校正说明，最多 5 条）；
  - `plan_preview`（本季扣款 / 结算后现金）。
- 校验失败时仍显示 issues，并明确「这次没填进去」。

## 四、验收（必须全过）

- [ ] `save 1.json` + 真模型 `qwen-flash`：起草 → `suggested_plan` 非空 → `submit` 成功 →
      `end_quarter` 成功，不出现「资金不足 / build_not_an_upgrade」。
- [ ] 离线假模型覆盖六类坏输出：已有地块当建造 / 全省略 plot_plans / 超预算 /
      越界坐标 / 非法产物 / 未知商品 —— 全部要么修复成有效计划，要么明确失败。
- [ ] 返回的 `suggested_plan` 必须同时过 `validate_plan` 与 `pipeline.preview`。
- [ ] `engine.validate` 对「10 条工业」在提交前就报 `insufficient_cash_estimate`。
- [ ] 现有 `tests/test_ai_host.py`、`test_ai_layer.py`、`test_validate.py`、
      `test_pipeline.py`、`test_m1.py` 全部不回归。
- [ ] AI 仍然**没有 commit**：`advise` 后 `session.plans` 为空、`phase` 不变。

## 五、明确不做

- 不让 AI 自动提交（§11.4 权限阶梯不动）。
- 不替玩家做「长期最优」决策；编译器只保证**合法 + 能结算**，不保证最优。
- 不新增规则；所有数值仍来自引擎/config。

---

## 六、落地结果（2026-10-18）

### 改了哪些文件

| 文件 | 作用 |
|---|---|
| `region-sim/ai/plan_compile.py`（新） | 起草编译器：规整 → 语义归位 → 补默认 → validate → preview → 确定性缩量；只产出 Plan 数据，不提交 |
| `region-sim/ai/plan_context.py`（新） | 可行动作快照：空地 / 可建工程（含材料与折后价）/ 可开工 / 人口口粮 |
| `region-sim/server/ai_host.py` | 接入快照与编译器；`draft` 出口改为 `suggested_plan` + `plan_notes` + `plan_preview`；`state_for(..., with_narratives=False)` 避免起草时白等新闻叙事 |
| `region-sim/engine/validate.py` | 校验硬化：坐标/越界/同图重复/真实产出路线/工程表 dry-run；现金粗估加入建造、物资、口粮 |
| `region-sim/ai/tools.py` | import 白名单补 `engine.construction`（只读查表） |
| `region-sim/web/js/panel.js` | 草案落表时显示「引擎校正 N 处」+ 逐条 notes + 「试算：本季扣款 X，结算后约 Y」 |
| `tests/test_plan_compile.py`（新） | 编译器 12 条 |
| `tests/test_plan_context.py`（新） | 快照 9 条 |
| `tests/test_validate_builds.py`（新） | 校验硬化 20 条 |
| `tests/test_ai_draft_e2e.py`（新） | 端到端 4 条（含 `advise → submit → end_quarter` 整条 HTTP 路由） |

### 验收结果

- **全量测试**：`region-sim/tests/` 下 21 个测试文件全部 `rc=0`（新增 15+9+20+4 条，既有 test_validate 46、test_ai_host 32、test_pipeline 28、test_m1 16、test_server 25 等全绿）。
- **真模型 · save 1.json（第 4 季、5 块运行地块、现金 416）**：
  模型仍把 `4,5 / 5,4 / 6,3` 写成建造、且漏写 `plot_plans`；
  编译器改成「5 块地全部开工 + 基准口粮」，`plan_notes` 2 条；
  validate `[]`、preview 扣款 366.63 / 结算后 413.60；
  `submit` OK、`end_quarter` OK。
- **真模型 · 开局 g1（0 地块、现金 1200）**：
  模型建了 2 块农业（`0,0` / `0,1`），但顺手把这两块写进了 `plot_plans`；
  编译器丢弃这两条「尚不存在的地块」、保留 2 条建造；
  validate `[]`、preview 扣款 208 / 结算后 1112；
  `submit` OK、`end_quarter` OK、地图上真的出现 2 块 1 级农业。
- **HTTP 路由**：`POST /api/ai/advise`（draft=true）→ `POST /api/submit` → `POST /api/end_quarter` 全 200，见 `tests/test_ai_draft_e2e.py::test_http_route_draft_submit_settle`。

### 已知边界（不影响本次验收）

1. `validate` 的口粮粗估按 P0 全额算、不折抵自产，可能偏保守；编译器在 validate 报现金不足时会先降口粮、再降开工、再移建造，所以 AI 草案仍能跑通；**玩家手工填的计划**可能被保守估法多提醒一次。
2. `buildable_products()`（前端下拉框契约）仍不含工业二级「研究」线；校验已接受 `product="研究"`。要不要把研究线放进 UI 是产品决定。
3. 「建后不可更换产物」目前由**编译器**兜住 AI 草案（农业升级保持原作物、工业 2→3 沿当前路线）；`construction.start_work()` 本身仍会照单覆盖，玩家手工计划要等后续规则口径统一再堵。
4. `research` 地块没有工程行（`construction.works` 14 行不含 research），校验现在提前拒绝「research 0→1」；若以后要允许建研究地块，需先补工程表。

