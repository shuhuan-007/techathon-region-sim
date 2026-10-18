# 第三方资源说明

**项目**：Region Sim · 区域发展模拟器
**团队**：建设特区（王子铖、贾乐萱、李佳怡）
**赛事**：Techathon 2026
**文档口径**：本文件所有条目均**逐项核对仓库实物**后书写（文件头注释、目录清单、`.gitignore`、`import` 语句、`grep` 外链）。凡未能从仓库内确证的，标注「（待现场说明）」，不作推测。

> 一句话结论：本项目**运行时零第三方 Python 包、零外部 CDN、零外部数据集**；全部外部依赖只有三样——**three.js r140（本地 vendor）**、**阿里云 DashScope `qwen-flash` 在线 API**、**影眸科技 Hyper3D 平台（仅建模阶段使用，产出为团队自有资产）**。

---

## 一、总览表

| # | 资源 | 版本 | 用途 | 许可证 / 授权 | 引入位置 | 是否改动 |
|---|---|---|---|---|---|---|
| 1 | three.js（`three.min.js`） | **r140** | 3D 城市渲染运行时 | **MIT** | `region-sim/web/js/vendor/three.min.js`（623,549 B）→ `web/index.html` L256 | **否**（原样 vendor） |
| 2 | GLTFLoader（`GLTFLoader.js`） | **three.js r140** `examples/js` UMD | 加载 21 个地块 `.glb` | **MIT**（随 three.js 分发） | `web/js/vendor/GLTFLoader.js`（106,366 B）→ `index.html` L257 | **否**（原样 vendor） |
| 3 | OrbitControls（`OrbitControls.js`） | 上游 **three@0.140.0** | 3D 视图缩放 / 旋转 / 平移 | **MIT** | `web/js/vendor/OrbitControls.js`（26,277 B）→ `index.html` L258 | **是**：ESM→UMD 封装转换（见 §三.3） |
| 4 | 阿里云 DashScope · 通义千问 | 模型 **`qwen-flash`** | AI 秘书起草计划、财报/新闻叙事 | DashScope 服务条款（**团队自购，按量付费**） | `server/llm_client.py` L46/L50 → 调用方 `server/ai_host.py`、`ai/narrate.py` | 否（仅 API 调用，无本地权重） |
| 5 | 影眸科技 Hyper3D 平台 | — | 3D 地块模型的生产平台 | 平台服务条款 | 离线建模阶段，产出落 `web/assets/plots/` | 产出即团队资产（见 §四） |
| 6 | 21 个地块 `.glb` | — | 地块 3D 模型 | **团队原创（100%）** | `web/assets/plots/*.glb`（533.1 MB，不入 git） | **是**：队员李佳怡自建 |
| 7 | 15 个降级 SVG | — | `.glb` 不可用时的 2D 降级图 | **团队原创** | `web/assets/icons/*.svg` | **是**：自 `prototype-v0.html` 抽出 |
| 8 | 系统字体栈 | 随操作系统 | 全部文字排版 | 随 OS 授权 | `web/css/*.css`，**无 `@font-face`** | 否 |
| 9 | Python 标准库（CPython） | 3.11+（`zoneinfo` 可用） | 服务层 / 结算引擎 / 存储 | **PSF License** | 全仓 `engine/`、`server/`、`ai/`、`storage/` | 否 |
| 10 | python-docx | — | **仅离线**把规则 Word 文档转纯文本 | **MIT** | `tools/docx_to_text.py`（36 行） | 否（开发辅助，**非运行时**） |
| 11 | 团队自购 API Key | — | DashScope 鉴权 | 团队资产 | `region-sim/.env`（**已 gitignore，绝不入仓**） | 否 |

---

## 二、AI 服务

### 2.1 服务商与接入方式

| 项 | 实测值 | 证据位置 |
|---|---|---|
| 服务商 | **阿里云 DashScope**（通义千问 Qwen） | `server/llm_client.py` L43-46 |
| 模型名 | **`qwen-flash`** | `.env` → `AI_MODEL=qwen-flash`；代码默认 `DEFAULT_MODEL = "qwen-flash"`（L50） |
| 接入方式 | **OpenAI 兼容** `chat/completions`（compatible-mode） | `DEFAULT_BASE_URL`（L46） |
| 端点 | `https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions` | 同上（可由 `AI_BASE_URL` 覆盖） |
| 超时 | 代码兜底 **300 s**；`.env` 实际配置 **90 s** | `DEFAULT_TIMEOUT = 300.0`（L55）；`.env` → `AI_TIMEOUT=90` |
| 配置优先级 | 真实环境变量 > `region-sim/.env` > 代码 `DEFAULT_*` 常量 | L62-67 注释；`.env` 加载器为纯标准库实现（免 `python-dotenv`） |
| HTTP 客户端 | **标准库 `urllib.request`**（非第三方） | L31-32、L268-277 |

### 2.2 调用链

```
web/js/api.js ──POST/GET──► server/app.py
                                ├─► server/ai_host.py   (L60 import llm_client；L247 注入 client)
                                │       └─► server/llm_client.py ──HTTPS──► DashScope
                                └─► ai/narrate.py       (L176 惰性 import llm_client)
```

- **唯一 HTTP 出口**：`server/llm_client.py` 是全仓唯一对外发请求的模块（`urllib.request.urlopen`，L277）。
- **惰性导入**：`ai/` 层不启动时依赖 `server/`，用函数内 `from server import llm_client`（`ai/narrate.py` L176）打破循环依赖，并有 `assert_import_boundary` 静态自检守护该分层（`ai/tools.py` L90-132）。

### 2.3 密钥与合规

| 事项 | 事实 |
|---|---|
| Key 来源 | **团队自购**（按量付费的商用 API Key），非借用、非赛事提供 |
| 是否入仓 | **否**。`region-sim/.gitignore` L12 `.env`、仓库根 `.gitignore` L18 `.env` 双重排除 |
| 是否写进本文档 | **否**。本文档只记录用途与端点，**不记录任何明文密钥** |
| 失败行为 | 任何失败（代理未起 / 超时 / 非 200 / 返回不可解析）**一律降级为 `LLMResult(ok=False, error=...)`，绝不抛穿、绝不编造回答**（`llm_client.py` L13-19） |
| 重试策略 | 明确**不重试、不并发压测**；仅一个「同参数重试一次」的递归闸，绝不无限（L252、L363） |
| 数据外发 | 仅发送当前局面的**文本摘要**（经体积闸裁剪，§七.7）；不上传代码、模型、`saves/` 存档 |

---

## 三、前端库（`web/js/vendor/`）

### 3.1 目录清单（实测 4 个文件）

| 文件 | 大小 | 版本 | 许可证 | 改动 |
|---|---|---|---|---|
| `three.min.js` | 623,549 B | **r140** | **MIT**（`SPDX-License-Identifier: MIT`，`Copyright 2010-2022 Three.js Authors`） | 未改动 |
| `GLTFLoader.js` | 106,366 B | three.js **r140** `examples/js` UMD | **MIT**（随 three.js 分发） | 未改动 |
| `OrbitControls.js` | 26,277 B | 上游 three@**0.140.0** | **MIT** | **有改动**（见 3.3） |
| `README.md` | 682 B | — | 团队自撰说明 | — |

**版本确证方式**：`three.min.js` 第 4 行起 UMD 头内有 `const e="140"`（即 `REVISION = "140"`），并非凭文件名推断。

### 3.2 引入方式与来源说明

- `index.html` **L256-258** 依次加载三个 vendor 脚本，**必须在 `map.js` 之前**；`<link>` 只加载本地 `css/app.css`、`css/tiles.css`、`css/finale.css`（L7-9）。
- `vendor/README.md` 自述：文件**从 jsdelivr / unpkg 下载**，下载日 **2026-10-17**，目的是*「避免运行时依赖外网 CDN」*。
- **为什么是 r140 而不是 r160**（README 原文理由）：r150+ 官方废弃了 UMD 的 `examples/js/`，**r140 是最后一个 UMD 完整的版本**；本项目**零构建、零 ESM、零打包器**，必须用 UMD 全局脚本。

### 3.3 唯一改动项：`OrbitControls.js`

| 项 | 说明 |
|---|---|
| 改动性质 | **模块封装转换**：上游只提供 ESM 版（`examples/jsm/`），本项目无构建链，故手工转为 UMD |
| 改动范围 | 仅**外壳**：从全局 `window.THREE` 解构依赖（`Vector2/Vector3/Quaternion/Spherical/MOUSE/TOUCH/EventDispatcher`），导出为全局对象 |
| 未改动部分 | **相机控制算法本体**（阻尼、极角/方位角限位、缩放、平移、触摸）逐行保留上游实现 |
| 是否留痕 | **是**。文件头 L1-4 有团队自撰说明：`Converted from ESM to UMD on 2026-10-17 … Original jsdelivr three@0.140.0/examples/jsm/controls/OrbitControls.js` |
| 许可证义务 | 上游 MIT，保留上游版权与许可声明；转换后的衍生文件**同样以 MIT 对外** |

> ⚠️ **如实说明**：`vendor/README.md` 的「文件」清单只列了 `three.min.js` 与 `GLTFLoader.js` 两项，**未列 `OrbitControls.js`**——该文件晚于 README 加入，README 未同步更新。此处以文件实物与文件头注释为准。

---

## 四、3D 建模资产

### 4.1 数量与体积（实测，非引用文档）

| 项 | 实测值 |
|---|---|
| `.glb` 文件数 | **21 个** |
| 总体积 | **533.1 MB** |
| 目录 | `region-sim/web/assets/plots/` |
| 是否入 git | **否**。`region-sim/.gitignore` 末行 `web/assets/plots/*.glb` 排除（体积过大，故**仅本地/交付介质随附**） |

### 4.2 来源与权属

| 项 | 说明 |
|---|---|
| 制作人 | 队员 **李佳怡** |
| 制作方式 | **提示词生图 → 影眸科技（Hyper3D）平台从零建构**，非下载、非购买、非素材库拼装 |
| 权属 | **100% 团队原创产出**（平台仅为生产工具，同「用 Photoshop 画的图仍属作者」） |
| 关联申报 | 本参赛**已报名影眸科技专项赛道** |
| 溯源依据 | 《原创声明》：「本项目地块建模由队员李佳怡**从提示词生图开始、于影眸科技平台从零建构**——建模本身也属团队原创产出」 |

### 4.3 21 个文件如何被使用

| 分组 | 数量 | 键名（= 文件名主干，中文契约） |
|---|---|---|
| 农业 1/2/3 级 | 3 | `农业1` `农业2` `农业3` |
| 资源 1/2/3 级 | 3 | `资源1` `资源2` `资源3` |
| 服务 1/2/3 级 | 3 | `服务1` `服务2` `服务3` |
| 工业 1 级 | 1 | `工业1` |
| 工业 2 级（四条机械路线） | 4 | `工业农业2` `工业工业2` `工业资源2` `工业服务2` |
| 工业 3 级（四路线 + 研究线 I） | 5 | `工业农业3` `工业工业3` `工业资源3` `工业服务业3` `工业高技术3` |
| 研究（仅 1 级） | 1 | `研究` |
| **契约小计** | **20** | 由 `engine/assets.py::asset_key()` 查表 |
| 「在建」状态专用模型 | 1 | `在建`（`web/js/city-modal.js` L669：`isConstructing ? '在建' : cell.asset`） |
| **合计** | **21** | — |

**两个刻意的「不修正」**（美术侧笔误，改动会断契约，故原样保留）：`工业服务2`（二级少一个「业」字）、`工业服务业3`（三级多一个「业」字）。**这是有意为之，不是缺陷。**

### 4.4 文档与实物的一处偏差（如实披露）

`web/assets/plots/README.md` 自述为「**20 个** `.glb`，约 **326 MB**」，与实测「**21 个**，**533.1 MB**」不一致。原因：`在建.glb`（39.8 MB）为后续追加交付的状态模型，README 未同步更新。**以目录实物为准。**

### 4.5 缺失资产的降级设计（团队自研，非第三方）

`asset_key()` 对 `research` level 2/3 及非法组合返回 `None`；`.glb` 若 404 / 超时 / 解析失败，前端**静默改画同名 SVG 降级图**，不弹错、不阻塞其它格（`web/js/map.js` S5 3D 层）。**因此 3D 资源缺失永远不会变成死路。**

---

## 五、字体、图标等静态资源

### 5.1 字体：**零外部字体**

| 检查项 | 结果 |
|---|---|
| `@font-face` | **0 处**（`css/app.css`、`css/tiles.css`、`css/finale.css` 全无） |
| Webfont 文件（`.woff/.woff2/.ttf/.otf`） | **0 个**（`web/` 下无字体目录） |
| CDN 字体（Google Fonts / Typekit / 阿里图标库） | **0 处** |
| 实际用字 | **纯系统字体栈**：正文 `font-family: inherit`；等宽处 `ui-monospace, Menlo, Consolas, monospace`（`finale.css` L146） |

### 5.2 图标：15 个 SVG，全部团队自绘

| 项 | 说明 |
|---|---|
| 文件数 | **15 个** `.svg`（实测；`icons/README.md` 自述「共 14 个」，系计数笔误，目录实物为 15） |
| 来源 | **团队原创**：手工抽自团队自己的原型 `region-sim/web/prototype-v0.html` 第 698 行 `const ART = {...}` 常量 |
| 处理 | 按 key 拆出模板字符串，补 `<?xml version…?>` 头与自封闭 `<svg>` 根（`viewBox="0 0 100 64"`） |
| 是否用第三方图标库 | **否**。全仓 grep 无 FontAwesome / Material Icons / iconfont / Iconfont CDN 任何痕迹 |
| 改动上游原型 | **否**（`prototype-v0.html` 本身未被修改） |

清单：`农业1-3`、`工业1-3`、`服务1-3`、`资源1-3`、`荒地`、`装饰`、`锁定`。工业细分 8 个目录 + `工业高技术3` + `研究` 无专属 SVG，按通用 `工业2/工业3` 兜底（详见 `icons/README.md` §二）。

### 5.3 纹理与贴图

| 项 | 结果 |
|---|---|
| 外部贴图 / HDRI / 素材包 | **未使用**；所有视觉由 `.glb` 自带材质 + CSS 渐变/纯色实现 |

---

## 六、未使用的资源（明确声明）

| 声明 | 核查方式与结论 |
|---|---|
| **未使用任何外部数据集** | 全仓无 `.csv/.parquet/.sqlite` 数据文件、无 Kaggle/统计局/World Bank 等数据引用；**全部数值来自团队自写规则文档与 `config/config.json`（28,744 B）**。人口、产值、价格、效率等参数均为团队依规则基线设定 |
| **未使用任何付费中间件 / 数据库 / 消息队列** | 存储层为自研 `storage/`（`json_file.py` 落盘 + `memory.py` 内存），**无 Redis / PostgreSQL / MongoDB / Kafka** |
| **未使用任何前端框架或构建工具** | 无 React/Vue/Svelte、无 webpack/Vite/Rollup、**零 `node_modules`**；原生 HTML/CSS/JS，直接以 `<script>` 引入 |
| **未使用前端第三方工具库** | 无 jQuery、无 Lodash、无 D3、无 Chart.js；图表为手写 SVG/Canvas |
| **未使用外部 CDN** | 见 §6.1 |
| **未使用任何第三方模拟经营项目代码** | 引擎 / 编译 / 校验 / 结算均为团队依规则文档自底向上实现，无 fork、无复制架构 |
| **未使用他人美术素材** | 无素材站下载、无 AI 图库二次分发；3D 与 SVG 均为团队产出 |

### 6.1 运行时外部依赖核查（`grep https://`）

对 `web/index.html` 与 `web/js/*.js`（排除 `vendor/`）全量搜索 `https?://`，**命中仅 1 处**：

| 位置 | 内容 | 是否网络请求 |
|---|---|---|
| `web/js/finale.js` L134 | `document.createElementNS("http://www.w3.org/2000/svg", tag)` | **否**——这是 SVG 的 **XML 命名空间标识符**，是 W3C 规范字符串，**不产生任何网络请求** |

> **结论：运行时零外网依赖（唯一例外是 §二 的 DashScope AI API）。**
> 三个 vendor 脚本、三个 CSS、九个业务 JS **全部为 `/static/…` 本地路径**，断网亦可完整启动与游玩（AI 功能自动降级，不影响结算与 3D 渲染）。

---

## 七、我们在开源 / 工具之上的自有改进

> 赛方要求「可用开源项目作基础，但**必须体现自己的改进**」。以下每条都指明**改进了什么**、以及**为什么不是套壳**。

### 7.1 确定性纯函数结算引擎（不是调库，是自建账本机器）

`engine/` 下 20 个模块、约 **6,000 行**实现单季结算，核心 `engine/pipeline.py::_run_body` 按「**第 0 步 → 第 9 步**」定序推进（政策抽取 → 完工 → 生产 → 服务收入 → 采购 → 工程投入 → 销售 → 合同交付 → 现金流 → 报告）。
**改进在哪**：结算被设计成**确定性的账本机器**（《规则基线》§0）——随机源是自研 `engine/rng.py`（`DEFAULT_SEED = 20261016` 显式播种），**同一 `state + plans` 必得同一份 `report`**，可回归、可复盘、可测试。
**为什么不是套壳**：没有任何开源模拟经营框架被 fork 或包装；规则来自团队自写文档，代码自底向上实现。

### 7.2 AI 双闸校验：`validate` + `preview` 双绿才放行

AI 起草的计划**不直接生效**：必须先过 `tool_validate`（合法性，`engine/validate.py`）**再**过 `tool_preview`（只读试算，`pipeline.preview`），**两闸全绿才返回 plan，任一不过即 `plan_rejected` 并附问题清单**（`server/ai_host.py` L325-328、L568-569）。
**改进在哪**：把「模型输出」夹在**两道确定性闸门之间**——模型无权判定自己合法，判定权 100% 在引擎。
**为什么不是套壳**：这是围绕本项目引擎契约设计的拒绝通道，不是任何现成框架的能力。

### 7.3 「AI 不代提交」的硬边界（架构级，非提示词级）

**AI 无 commit 权限**：最后一脚永远由玩家走 `/api/submit`（`server/ai_host.py` L40 明写 `§11.4：AI 无 commit`；system prompt L79 强制「提交永远由玩家走 /api/submit，你无 commit，也不得声称执行过任何操作」；`server/app.py` L311 同口径）。
**改进在哪**：边界不靠「请模型自觉」，而是**静态可执行的自检**——`ai/tools.py` L90-132 用导入黑名单 `assert_import_boundary()` 在测试期就检测 `ai/` 层是否摸到 commit 入口，并有专门测试 `tests/test_ai_host.py` L427 守着「边界没被放宽」。
**为什么不是套壳**：这是为本项目分层架构自建的编译期约束，开源 SDK 不提供。

### 7.4 零计算前端：前端不造公式（红线）

**改进在哪**：所有经济数值（产值、效率、价格、GDP）由后端接口下发后**直接渲染**，前端不重算；`web/js/map.js` L95 把这条写成红线——「`efficiency=null`，**前端不造公式**」。
**如实披露一处例外**：`panel.js` L445-453 有一处**显示兜底**，在后端尚未下发时按「与后端同一公式」补齐，注释明写「公式不自创」；该兜底**只用于显示，不参与结算、不回写状态**。

### 7.5 21 个自建 3D 资产 + 中文键契约

**改进在哪**：不用通用模型库，而是**为每个地块状态定制建模**（20 个契约键 + 1 个「在建」状态模型，533.1 MB），并以「中文目录名 = 查表键」的契约挂到 `engine/assets.py::asset_key()`，由 `/api/map` 下发 `cell.asset`。
**为什么不是套壳**：模型是本团队从提示词生图起在影眸 Hyper3D 平台逐件构建的原创资产，非购买、非下载。

### 7.6 3D 失败自动降级：不留死路

`.glb` 404 / 超时 / 解析失败 → 前端静默改画同名 2D SVG（§4.5），不弹错、不阻塞其它格；`asset_key()` 对无建模组合返回 `None` 亦走同一降级。
**改进在哪**：把「资源缺失」当**正常态**而非异常态设计，保证演示现场弱网/换机也不白屏。

### 7.7 AI 上下文体积闸：确定性三档降级

拼给模型的局面文本设**字符上限**，超限时按三档降级（`server/ai_host.py` L695、L1685-1690）。关键设计：**每一档都是「砍次要项」，而不是「砍上限」**——绝不为了让模型读得下而篡改数值。
**改进在哪**：在「上下文有限」这一通用约束下，给出**不损失数值保真度**的裁剪策略，并用确定性规则（非模型）执行。

### 7.8 9 阶段流水线 + 611 个单测的工程化

**改进在哪**：`tests/` 下 **29 个测试文件、611 个用例**（`pytest --collect-only` 实测），覆盖结算、校验、AI 契约、存储、HTTP 端点、资产契约、终局报告；测试**全部离线**（`tests/test_llm_client.py` 用 monkeypatch 替换 `urlopen`，**绝不真发网络**），可在无网、无 Key 环境完整回归。
**为什么不是套壳**：测试是对**自有规则**的断言（如「`tool_preview` 不得改动 state」「AI 边界未被放宽」），不是对第三方库的复测。

> 附：整库连跑实测 **611 passed / 0 failed**（此前 10 条集中在 `tests/test_ai_layer.py` 的导入顺序自检，`ai.tools` 的 `assert_import_boundary` 在整库连跑时命中已被其它测试加载的模块），**与第三方资源无关、不影响功能**，属已知测试编排问题。

---

## 八、许可证合规小结

| 义务 | 履行情况 |
|---|---|
| MIT（three.js 全家桶） | 保留上游版权与许可声明；`three.min.js` 头部 `SPDX-License-Identifier: MIT` 原样未删 |
| MIT 衍生（`OrbitControls.js`） | 文件头**主动标注**转换事实、上游版本与来源 URL；衍生文件同样以 MIT 提供 |
| 商用 API（DashScope） | 团队自购、按量付费，遵守服务条款；密钥不入仓、不入文档 |
| 团队自有资产（21 `.glb` + 15 SVG） | 明确声明 100% 团队原创；已报名影眸科技专项赛道 |
| 数据集 | 未使用任何外部数据集，无第三方数据许可义务 |

---

**备注**：21 个 `.glb` 因 533.1 MB 体积不入 git，已打成 502 MB 压缩包**公开挂在 GitHub Release** ——
<https://github.com/shuhuan-007/techathon-region-sim/releases/tag/v1.0-3d-models>
下载解压到 `region-sim/web/assets/plots/` 即可看到 3D；不下载也能跑（自动退化 2D，数值不变）。其余资源的详细出处与许可证原文，我们可在演示日当场提供。

# 技术文档导读

> **给评委的 5 分钟路线图**：这个仓库是什么、从哪进、每个目录管什么、一个季度怎么算出来、质量靠什么兜住。
> 本文件只做**导航**，不复制规则原文；规则的唯一权威是 [`docs/01-规范/规则基线.md`](../docs/01-规范/规则基线.md)。

| 项 | 值（本轮实测，非估算） |
|---|---|
| 代码根 | `region-sim/` |
| 语言 / 运行时 | Python 3.12（实测 3.12.14）+ 原生 JS（**无构建步骤**） |
| 第三方运行时依赖 | **0**：HTTP 用标准库 `http.server`，LLM 走原生 HTTP；pytest 只用于测试 |
| 引擎模块 | **20** 个 `.py`（`region-sim/engine/`） |
| 测试 | **29** 个 `test_*.py` / **612** 个 `def test_` 函数 / pytest 收集 **611** 项 |
| HTTP 端点 | **18** 个 `/api/*` + 2 条静态路由 |
| 文档 | `docs/` **24** 份（`99-历史版本/` 为归档，不计入） |
| 一局长度 | **20 季**（`config/config.json` → `world.total_quarters`） |

---

## 九、5 分钟上手

### 9.1 起服务（真实默认 `127.0.0.1:8000`）

```powershell
cd D:\开发特区\region-sim
python server/app.py
```

输出会直接打印地址；浏览器打开 **http://127.0.0.1:8000/** 即可玩。

> **端口是代码里的真实默认值**：`server/app.py:860` → `--port` 缺省 `8000`，`--host` 缺省 `127.0.0.1`。
> 换端口加参数，别改代码：`python server/app.py --port 8300`。

两种启动方式都支持（`server/app.py:82-83` 做了 `sys.path` 自举，直接跑脚本不会 `ModuleNotFoundError`）：

```powershell
python server/app.py            # 直接跑脚本
python -m server.app            # 当模块跑
```

### 9.2 打开前端

**必须走服务**，不要双击 `web/index.html` —— 页面上每个数字都来自 `/api/*`，`file://` 下取不到。
静态文件由同一条服务提供：`GET /` 返回 `web/index.html`，`GET /static/<路径>` 返回 `web/` 下的资源。

### 9.3 跑测试（在工作目录 `region-sim/` 下）

```powershell
cd D:\开发特区\region-sim
python -m pytest tests -q                 # 全量
python -m pytest tests/test_m1.py -q      # 只跑 M1 锚点（18 项，0.1 秒）
python -m pytest tests -q -k storage      # 按名字筛
```

### 9.4 一条命令证明"装得起来"（真起 socket 真发 HTTP）

```powershell
cd D:\开发特区\region-sim
python tools/smoke_http.py
```

它会起一个随机空闲端口的真服务 → 开新局 → 下建设指令 → 结算一季 → 读状态 → 读存档 → 关服务，每步断言；
**不碰你的 `saves/`**（用临时目录，跑完即删）。

### 9.5 参数与环境变量

| 名称 | 默认 | 说明 |
|---|---|---|
| `--host` | `127.0.0.1` | 只听本机 |
| `--port` | `8000` | 缺省端口（**不是 8080 / 3000**） |
| `--saves` | `saves` | 存档目录，相对 `region-sim/` |
| `--offline` | 关 | 置 `NARRATE_OFFLINE=1`，**不真调叙事模型**（纯离线调试用） |
| `.env` | 不入库 | `AI_API_KEY` / `AI_BASE_URL` / `AI_MODEL` / `AI_TIMEOUT`；被 `.gitignore` 忽略，**仓库里没有密钥** |

> 不带 `--offline` 时服务默认**在线**（`server/app.py:866-871`）：财报/新闻会真调模型。
> 模型没起来是**常态不是异常**：`/api/ai/advise` 返回 `200 + ok:false`，不影响其它端点。

---

## 十、仓库目录地图

```
D:\开发特区\
├── README.md                     243 行 · 门面：怎么读、五条红线、M1 锚点、文档权威顺序
├── 项目现状与下一步.md             1151 行 · 活体快照：现在什么状态 / 已定死 / 未定 / 下一步写什么
├── docs/                         24 份文档（逐份一句话见 §十五）
│   ├── 01-规范/                  规则唯一权威（2 份：规则基线、地块总表）
│   ├── 02-走查/                  按执行顺序走一遍（1 份）
│   ├── 03-架构/                  架构与技术决策（10 份）
│   ├── 04-交接/                  给队友 / 给演示（7 份）
│   ├── 05-原始资料/              数据源与前端原型原件（4 份）
│   └── 99-历史版本/              归档目录（不计入 24 份，本导读不展开）
├── region-sim/                   ★ 程序主体
│   ├── engine/                   20 个 py：全部业务逻辑，纯函数、确定性
│   ├── server/                   7 个 py：HTTP 只转发 + 会话三状态机 + 每局一把锁
│   ├── ai/                       8 个 py + prompts/：只解释、只读、**无 commit**
│   ├── storage/                  4 个 py：存档协议 + JSON 实现 + 内存实现
│   ├── web/                      前端：index.html + css/(3) + js/(9) + js/vendor/(4)
│   ├── tests/                    29 个 test_*.py（612 个 test 函数）
│   ├── tools/                    11 个辅助脚本（造档 / 冒烟 / 平衡实验 / 文本提取）
│   ├── config/config.json        28,744 B：所有数值域（见 §11.4）
│   ├── saves/ · saves-live/      跑出来的存档（`.gitignore` 内，不入库）
│   └── .env                      AI 网关密钥（`.gitignore` 内，**不入库**）
└── 提交材料/                      路演 PPT 与本导读
```

**`engine/` 20 个模块按职责分三组**（名字即职责，不必逐个点开）：

| 组 | 文件 | 一句话 |
|---|---|---|
| 模型与底座（6） | `models.py` · `loader.py` · `rng.py` · `clock.py` · `assets.py` · `naming.py` | 状态对象 / 配置加载 / 确定性随机 / 世界时钟 / 地块资产键 / 地块起名 |
| 规则计算（8） | `production.py` · `market.py` · `efficiency.py` · `population.py` · `settlement.py` · `finance.py` · `construction.py` · `policies.py` | 产量 / 供求求价 / 效率 E / 人口粮食链 / 幸福度 / 税与 GDP / 工程与工期 / 政策 |
| 编排与输出（5） | `pipeline.py` · `report.py` · `finale.py` · `news.py` · `validate.py` | 季度流水线 / `/state` 报告 / 20 季终局 / 新闻骨架 / 计划校验 |

**`server/` 7 个**：`app.py`（路由表 + HTTP 壳）· `session.py`（三状态机 + 每局一把 `RLock`）· `registry.py`（`game_id → GameSession`）· `ai_host.py`（把局面递给 AI）· `llm_client.py`（OpenAI 兼容薄客户端）· `slots.py`（多档存档）· `__init__.py`。

**`ai/` 8 个**：`tools.py`（试算/校验/起草三工具，全只读 + import 边界自检）· `narrate.py`（**唯一模型调用口**）· `plan_context.py`（能给模型看的动作快照）· `plan_compile.py`（把模型输出规整成能跑的计划）· `plan_edit.py`（草稿轻量编辑）· `strategy.py`（确定性策略算数）· `rules_digest.py`（规则基线摘要检索）· `__init__.py`。

**`storage/` 4 个**：`base.py`（协议）· `json_file.py`（一文件一局）· `memory.py`（测试用，不碰盘）· `__init__.py`。

**`web/`**：`index.html`；`css/`＝`app.css` `tiles.css` `finale.css`；`js/`＝`state` `api` `map` `finale` `panel` `main` `city-modal` `city-modal-sky` `city-modal-ticker`；`js/vendor/`＝`three.min.js` `GLTFLoader.js` `OrbitControls.js`（第三方，原样入库）。

---

## 十一、架构与依赖红线

### 11.1 五层，单向依赖

```
┌───────────────────────────────────────────────┐
│ ① web/      前端：只发操作、只显示，★ 零计算      │
└───────────────────────────────────────────────┘
                  ↕ HTTP / JSON
┌───────────────────────────────────────────────┐
│ ② server/   服务层：三状态机、收齐 Plan 调 advance │
│             ★ 不放任何规则，只转发                │
└───────────────────────────────────────────────┘
                  ↕ 函数调用
┌───────────────────────────────────────────────┐
│ ③ engine/   引擎：advance(state, plans) 纯函数   │
│             ★ 不 import server、不碰文件、不用随机 │
└───────────────────────────────────────────────┘
                  ↕ 读
┌───────────────────────────────────────────────┐
│ ④ config/   config.json（数据）                 │
└───────────────────────────────────────────────┘
                  ↕ Storage 协议（可替换）
┌───────────────────────────────────────────────┐
│ ⑤ storage/  第一期：JSON 文件；测试：内存        │
└───────────────────────────────────────────────┘
```

**依赖方向 `web → server → engine → config`，engine 绝不 import server**（`docs/03-架构/项目架构-总.md:59`）。

### 11.2 为什么这样分（三条理由，都是可验证的收益）

| 红线 | 收益（不是口号） |
|---|---|
| engine 不依赖 HTTP | 引擎能脱离 Web 直接跑 —— **611 项测试 3.7 秒跑完，一个 socket 都不用起** |
| 一份实现两个包装 | `pipeline.py::_run` 是唯一实现，`advance`（真结算改状态）与 `preview`（试算改副本）都调它 —— **AI 报的数与真实结算不可能不一致** |
| 服务层零规则 | 路由表就是 `app.py` 里一个 18 行的 dict；换框架只改这一个文件，引擎与服务层一行不动 |

### 11.3 边界是**代码**保证的，不是注释

- `ai/tools.py::assert_import_boundary()`：AI 层若摸到 `server.*` / `storage.*` 等 commit 入口就抛 `AisImportBoundaryError` —— 把「AI 无 commit」从注释变成自检；对应 `tests/test_ai_layer.py` 14 项。
- `pipeline.py` 只有 `_run` 会改状态；`_run` 失败即回滚（先撤政策挂载，再抛错），守「不产生部分状态变更」。
- 防"两套公式"：`preview` 与 `advance` 共用 `_run_body`，不存在第二份算式。

### 11.4 配置与代码分离

所有数值只在 `region-sim/config/config.json`（28,744 B）里，代码只读不算写。**19 个顶层数据域 + 3 组 `_comment` 内嵌注释（写明每条数值的规则出处）**：

| 数据域 | 承载什么 |
|---|---|
| `meta` | `config_version` / `rules_baseline` / `note`（配置对应哪版规则） |
| `world` | 地图行列、**总季数 20**、开局现金 1200 |
| `goods` | 15 个商品编码：`A B C D F G H-A H-F H-G H-J I` 与 `+` 升级档 |
| `recipes` | 农业 / 资源 / 工业 / 服务 / 研究 五类配方 |
| `wages` | 分行业工资（农业/资源/工业·一般/机械/高技术/服务） |
| `efficiency` | `adjacency` 邻接加成 / `happiness` 幸福度 / `ramp_up` 磨合 / `research_bonus` 研究加成 |
| `service_income` | 服务 L1/L2/L3 每邻收入（12 / 20 / 28） |
| `population` | `r`、等级乘数斜率、基数篮、品级乘数 |
| `food` | 基准口粮 / 优质口粮 / 可替代商品 |
| `tax` | `wage_tax_rate`（工资税 10%） |
| `production` | 经营费固定/可变占比 |
| `trade` | 买价来源、卖价来源、扣款时点、首季无价兜底 |
| `concurrency` | `per_game_lock` / `lock_scope` / `serialize_submit`（并发口径写进配置） |
| `construction` | `works` 工程表（每类地块每级的现金费、物资、工期） |
| `opening` | 开局减免：`discount_quota` 3 个名额 × `discount_rate` 0.50 |
| `economy` | 季度补贴与早期补贴 |
| `policies` | 政策模式与倍率 |
| `plot_names` | 地块命名库（UI 便利层） |

> **为什么重要**：改平衡只改 JSON，不碰 Python —— 所以 `tests/test_m1.py` 能用「甲 42 / 乙 142 / 合计 184」把配置与引擎一起钉死；
> 数值一动，锚点先红。

---

## 十二、一个季度是怎么算完的

入口只有一个：`engine/pipeline.py::_run(state, plans, cfg)`（第 205 行），它套住 `_run_body`（第 265 行）。
**九步顺序**来自《规则基线》§6，代码里另有第 0 步与几个半步（细到能对着行号读）：

| 步 | 人话 | 代码位置 |
|---:|---|---|
| 0 | **抽政策**：每季随机抽 1 项（25%），只挂内存不当真 | `pipeline.py:220` |
| 1 | **期初更新**：工期推进、到期完工转运行、公告 | `pipeline.py:297` |
| 2 | **玩家提交计划**：只校验，**一分钱不扣** | `server/session.py:501`（在服务层，不在流水线里） |
| 3 | **算效率 E**：邻接 × 幸福度 × 磨合 × 研究（四分量**相乘**） | `pipeline.py:303` |
| 4 | **生产**：产量 = `e·u·E`，投入 = `input·u·E`，劳动 = `d·u`（**劳动不乘 E**） | `pipeline.py:335` |
| 4.5 | **买入汇总与求价**：`B_t` = 原料+物资+口粮，`X_t` = 产出−合同交付 | `pipeline.py:440` |
| 5 | **一次性扣款 ★**：物资（按**上季价**）+ 工资 + 经营费；**钱只在这里动一次** | `pipeline.py:561` |
| 6 | **产品分配与工程投入**：合同履约；本季产品投工程 | `pipeline.py:645` |
| 7 | **市场结算**：产出按 `P_t` **全部卖出**（不用申报）+ 服务收入入账 | `pipeline.py:651` |
| 7.5 | **粮食结算**：扣 A/B/C/D → 得本季幸福度 `H` | `pipeline.py:679` |
| 8 | **税收与报告**：工资税 10% 记回流收入，生成本季报告 | `pipeline.py:712` |
| 9 | **结转**：`quarter += 1`，保存本季 `H` | `pipeline.py:769` |

**半步不是补丁，是顺序铁律的落点**（写错任何一条，数字对不上且极难查）：

| 半步 | 为什么必须在这个位置 |
|---|---|
| 5.5 合同转账 / 5.6 合同货交付 | 必须等第 5 步扣款成功才动钱；货要在第 6 步工程投入**之前**到位 |
| 5.9 磨合推进 | 生产已用本季 E 算完，此时 +1 不会反改本季产量；本季新开工的地块保持 0 |
| 7.7 季末清仓 | **已取消** —— 没有仓库，产出即供给，没有库存可清 |

**六条顺序铁律**（`pipeline.py:57-64`）：① 幸福度用**上季** `H` ② 粮食在结算时扣 ③ 买入按上季价、卖出按本季价
④ 劳动不乘 E ⑤ 本季产出不能倒填本季已开始的生产 ⑥ 粮食结算必须在市场结算**之后**。

**阶段状态机**（`server/session.py`，`SessionPhase`）：
`COLLECTING`（收计划）→ `RESOLVING`（结算）→ `REPORT`（月报）→ 回 `COLLECTING`；到 20 季切 `FINISHED`（终局只读）。
同一局**串行**：每局一把 `RLock`，`ThreadingHTTPServer` 的并发只到"不同局可并行"这一层。

---

## 十三、HTTP 接口清单

路由表在 `server/app.py:681-700`（`ROUTES` dict）——**真实 18 个 `/api/*`**，逐条只做转发：

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/games` | 存档列表（含季数、是否终局） |
| POST | `/api/new_game` | 开一局新的（现金 1200、1/20 季、零地块） |
| GET | `/api/state` | 引擎算好的状态视图（**默认不生成叙事**，秒回） |
| GET | `/api/narratives` | 本季新闻 + 财报解读（后台单独生成，按季缓存） |
| GET | `/api/map` | 整张 10×10 地块图 |
| GET | `/api/report` | 上一季完整财报 |
| GET | `/api/build_options` | 可建产物 / 路线选项（由工程表派生） |
| POST | `/api/submit` | 提交计划（**只记不扣**，校验不过不入库） |
| POST | `/api/simulate` | ★ 只读试算（AI 与玩家共用同一份 `_run`） |
| POST | `/api/validate` | ★ 只校验（返回结构化 `issues`） |
| GET | `/api/rules` | ★ 规则基线原文（可 `?section=`，给 AI 读） |
| POST | `/api/ai/advise` | AI 建议面板；模型不可用＝`200 + ok:false` |
| POST | `/api/end_quarter` | 结算这一季（第 5 步扣款在这里发生） |
| POST | `/api/next_quarter` | 看完月报进入下一季 |
| GET | `/api/finale` | 终局总览 + 终局叙事（20 季总结） |
| POST | `/api/save_slot` | 把当前局另存为 `game@slot` |
| POST | `/api/load_slot` | 从 slot 挂载成新局（原档不动） |
| GET | `/api/slots` | 列出该局名下全部 slot 元数据 |

**另有 2 条非 API 路由**：`GET /` → `web/index.html`；`GET /static/<路径>` → `web/` 下静态文件（前端脚本就用 `/static/js/*.js`）。

**统一错误形状**（前端不用解析字符串）：

```json
{ "ok": false, "error": "计划被拒绝", "kind": "plan_rejected",
  "detail": "...", "issues": [ { "code": "...", "target": "...", "message": "..." } ] }
```

`kind` 机器可读：`conflict` / `plan_rejected` / `not_found` / `no_route` / `method_not_allowed` / `internal`。
`dispatch()` 收网所有异常翻成 JSON，**不让服务端 500 空响应**；抽成纯函数后 `tests/test_server.py` 不用起 socket 就能测路由。

---

## 十四、测试与质量闸

### 14.1 规模

| 指标 | 值 |
|---|---|
| 测试文件 | **29** 个（`region-sim/tests/test_*.py`） |
| `def test_` 函数 | **612** 个 |
| pytest 实际收集 | **611** 项（少 1：`test_pipeline.py` 里 `test_oversell_raises` 定义了两次，后一个覆盖前一个） |
| 全量耗时 | **3.7 秒**（纯函数，不起 socket、不连网） |
| 本轮全量结果 | **611 通过 / 0 失败**（17 s）；干净克隆下 `test_assets` 按设计 skip（详见 §14.4） |
| 单文件隔离 | `python -m pytest tests/test_ai_layer.py -q` → **14/14 通过** |

### 14.2 锚点测试：M1 账本钉死

`tests/test_m1.py`（18 项，0.1 秒）把**唯一一套验收数字**写进断言，任何一处算错立刻红：

| 项目 | 区域甲（F） | 区域乙（G） |
|---|---:|---:|
| 生产总支出 | 66 | 183 |
| 结算价 `P_t` | 17.76 | 55.481481 |
| 期末现金 | 1008.64 | 1098.907407 |
| 增加值 | **42** | **142** |

**增加值合计 184**（浮动容差 `1e-6` 比对，不写 `==`）。配套静态成本四点 `G 183 · H-A 364 · I 542 · H-A+ 920`。
> 这就是"锚点"的意思：**对不上就是实现有问题**，反馈闭环省掉大量调试。（口径出处：`README.md` §六、《规则基线》§8.1）

### 14.3 测试按层分布（不是一堆散测）

| 层 | 代表文件 | 钉住什么 |
|---|---|---|
| 引擎计算 | `test_production` `test_market` `test_finance` `test_efficiency` `test_population` `test_settlement` | 公式与顺序 |
| 编排 | `test_pipeline.py`（32）· `test_validate.py`（49）· `test_validate_builds.py`（26） | 九步顺序、计划校验、可建性 |
| 服务契约 | `test_server.py`（30）· `test_ai_endpoints.py`（12）· `test_contracts.py` | 路由、错误翻译、只读端点 |
| 存储 | `test_storage.py`（27） | JSON / 内存两实现同协议 |
| AI 层 | `test_ai_layer.py`（14）· `test_ai_host.py`（59）· `test_plan_compile.py`（34） | 无 commit、边界自检、起草编译器 |
| 终局 | `test_finale_report*.py` · `test_game_finale.py` | 20 季结算与终局报告 |

### 14.4 一条曾被修掉的测试红灯（如实留档）

全量跑曾出现 **10 failed**（全在 `tests/test_ai_layer.py`）；**已修**：该文件加 autouse 夹具按「ai/ 载入后新增模块」重设基线，整库现在 **611/611 全绿**，单跑仍 14/14。
根因：`assert_import_boundary()` 检查的是**当前进程** `sys.modules` —— 全量跑时其它测试已经把 `server.registry` / `storage` 加载进同一进程，自检把它们误判为"AI 层引入的 commit 入口"。
**这是测试隔离缺陷，不是产品缺陷**（AI 层实际只 import `engine`）。**已修**：`tests/test_ai_layer.py` 加了 autouse 夹具，每个用例开始把基线重置为当前 `sys.modules` 快照（口径即「ai/ 载入之后新出现的才记账」），整库连跑 611/611 全绿，单跑该文件仍 14/14。生产代码 `ai/tools.py` **一行未改**。

---

## 十五、文档索引（`docs/` 24 份，逐份一句话）

**先读这三份**：① [`规则基线.md`](../docs/01-规范/规则基线.md)（规则唯一权威）→ ② [`项目架构-总.md`](../docs/03-架构/项目架构-总.md)（怎么搭的）→ ③ [`已实现规则-流水账.md`](../docs/02-走查/已实现规则-流水账.md)（按顺序走一遍）。
**评委只想看演示**：本文件 → [`演示操作稿.md`](../docs/04-交接/演示操作稿.md)。

### `01-规范/`（2）—— 唯一权威

| 文档 | 一句话 |
|---|---|
| `规则基线.md`（1398 行） | 规则唯一权威 V7：程序要算的一切 + 配置口径；与代码冲突时以它为准 |
| `地块总表.md`（266 行） | 有哪些地块：类型 / 等级 / 产物 / 成本 / 工期，一张表说清 |

### `02-走查/`（1）

| 文档 | 一句话 |
|---|---|
| `已实现规则-流水账.md`（592 行） | 一个区域从开局到结算，程序每步到底算什么 —— 对着代码读的流水账 |

### `03-架构/`（10）

| 文档 | 一句话 |
|---|---|
| `项目架构-总.md`（464 行） | **架构合并定稿**：五层、状态机、技术选型、红线 |
| `总文件架构.md`（374 行） | 理论上的完整目标形态（含尚未实现的部分） |
| `核心变量与计算方案.md`（194 行） | 真正在变的几个量：是什么 / 被什么决定 / 影响什么 |
| `游戏架构参考.md`（338 行） | 外部架构模式参考与取舍理由 |
| `AI自主演算-架构影响.md`（214 行） | 允许 AI 演算后的边界：唯一实现、两个包装、AI 无 commit |
| `AI秘书-有效起草方案.md`（109 行） | AI 起草为何曾失效 + `plan_context` / `plan_compile` 解法 |
| `AI交互与建设撤销.md`（102 行） | AI 交互与建设撤销的设计契约 |
| `结算与叙事解耦.md`（65 行） | 把叙事从结算里摘出去，新增 `/api/narratives` |
| `经济体检.md`（172 行） | 历史实验档（已加注，不作权威） |
| `glB地块资产接口.md`（103 行） | 每个地块用哪个 `.glb` 的契约（**已冻结，3D 渲染未做**） |

### `04-交接/`（7）

| 文档 | 一句话 |
|---|---|
| `前端设计规范.md`（391 行） | 前端布局与交互规范（草案 v0.3） |
| `界面要显示的变量清单.md`（224 行） | 屏幕上该出现哪些数（前端据此画） |
| `演示操作稿.md`（73 行） | **演示五步**：说什么 / 看到什么（实测数字）/ 注意什么 |
| `DEMO剧本-玩法展示版.md`（185 行） | 中期存档 + 一集 DEMO 的剧本设计稿 |
| `规则细化交接书.md`（569 行） | 给规则细化同学的任务书 |
| `规则裁定合并说明-v1.1.md`（265 行） | 队友 v1.1 交付的逐条裁定与不可再生数据（**不可再删**） |
| `队友交付-合并记录.md`（276 行） | 队友文件的三步处理：存原件 / 逐条判定 / 同步基线 |

### `05-原始资料/`（4）

| 文档 | 一句话 |
|---|---|
| `配方v3.docx`（150 行） | 数值数据源（配方原件） |
| `队友弹性参数.docx`（115 行） | 15 个商品的弹性参数原件 |
| `游戏设计前端.docx`（145 行） | 前端设计需求原件 |
| `前端原型-初版.html`（1220 行） | 前端原型初版 —— **参考，不是规格书** |

> 归档：`docs/99-历史版本/` 只放追溯件（前端原型参考 + 归档说明），不计入上面 24 份。

---

## 十六、已知边界与后续（不粉饰）

### 16.1 明确不做（不是没做完）

| 边界 | 实情 |
|---|---|
| **多人联机** | 黑客松期间**不做**，功能上只做单人；但代码**按多人建模**（`players` 字典、`player_id` 索引、`plans` 字典收齐再结算）。单→多的全部改动面＝三处 |
| 系统贷款 | 无。仅玩家间借贷，利率协商，还不上进失信状态 |
| 调平衡 | 不追。先把账对上（锚点 42 / 142 / 184 优先于"好玩"） |
| 零级地块 / 人口固定 100 / 幸福度分档 | 均不采用（分别改为：开局分类减免名额 / `N = L×2` / `H ∈ [0,1]` 连续插值） |

### 16.2 资产与仓库边界

| 项 | 实情 |
|---|---|
| **3D 资产不入 git** | `region-sim/web/assets/plots/*.glb`（20 个）与顶层 `建模/`、`*.rar`、`*.zip` 全在 `.gitignore` 内。**克隆下来没有模型**，前端自动回退 2D 格子 —— 契约见 `docs/03-架构/glB地块资产接口.md` |
| 存档不入库 | `saves/` `saves-live/` 均被忽略；仓库里的 `saves/*.json` 只是本地快照 |
| 密钥不入库 | `region-sim/.env` 被忽略，仓库内无明文 key |
| 无仓库机制 | 产出即供给，没有库存、没有"申报卖出"、没有季末清仓；居民口粮只能从 `purchases` 买入 |
| 合同 | 已实现（玩家对玩家、双方确认才生效），但**单人局机制上不可达** —— 这是设计，不是 bug |

### 16.3 待办与已知瑕疵

| # | 事项 | 状态 |
|---|---|---|
| 1 | ~~`test_ai_layer` 全量跑 10 红~~ | **已修**（测试侧 autouse 夹具重设基线，生产代码未动）；整库 611/611 全绿 |
| 2 | `test_pipeline.py` 里 `test_oversell_raises` 重复定义一次 | 已知，导致 612 定义 / 611 收集 |
| 3 | 工资税回流去向（D4） | 单人期先直接回流玩家；多人时再引入公共账户 |
| 4 | 浏览器渲染未做视觉验证（本机浏览器守护进程起不来） | 如实记录，未声称已验证 |
| 5 | 终局后的读写口径 | `FINISHED` 只读：无 submit / end / next 入口 |
| 6 | 并发 | 每局一把锁串行；**单进程**内安全，多进程不共享锁 —— 单机演示够用 |

---

## 十七、附：三分钟验收路径（建议照做）

```powershell
# 1) 起服务（默认 8000）
cd D:\开发特区\region-sim
python server/app.py

# 2) 浏览器打开 http://127.0.0.1:8000/ → 开新局 → 建几块地 → 推进一季 → 看月报

# 3) 另开一个终端：跑锚点与冒烟
cd D:\开发特区\region-sim
python -m pytest tests/test_m1.py -q
python tools/smoke_http.py
```

预期：第 2 步看到现金 **1200.0**、季度 **1 / 20**、10×10 空地；第 3 步 `18 passed`、冒烟脚本全绿退出。
