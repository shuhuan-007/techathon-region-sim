# region-sim/ —— 代码目录导航

> 一句话：**`engine/` 决定事实，`server/` 只转发，`web/` 只显示，`ai/` 只解释。**
> 依赖方向单向：`web → server → engine → config`，**`engine/` 永远不 import `server/`**。

## 10 秒看懂

| 目录 | 是什么 | 关键文件 |
|---|---|---|
| `engine/` | **确定性结算引擎**（纯函数、无 IO）。九阶段季度流水线、市场、建设、人口、政策、终局 | `pipeline.py`（主流程）· `market.py`（弹性求价）· `validate.py`（规则校验）· `report.py`（财报） |
| `server/` | **HTTP 服务层**，零 Web 框架（标准库 `http.server`），自己不计算，只调 engine | `app.py`（18 个 `/api` 端点）· `session.py`（三状态机 + 串行锁）· `ai_host.py`（AI 秘书） |
| `ai/` | **AI 层**：只读工具 + 起草编译器。**无提交权**，边界由 `assert_import_boundary()` 钉死 | `tools.py`（只读三工具 + 边界自检）· `plan_compile.py`（validate+preview 双闸） |
| `storage/` | 存档读写：JSON 原子写（tmp + fsync + replace） | `json_file.py` · `base.py` |
| `web/` | 前端：原生 HTML/CSS/JS，**零计算**（数字全来自后端） | `index.html` · `js/panel.js` · `js/map.js` · `js/city-modal.js` |
| `config/` | **全部数值**都在这里，代码零硬编码 | `config.json` |
| `tests/` | 29 个测试文件 / 612 个用例 | `test_m1.py`（结算锚点）· `test_pipeline.py` · `test_ai_layer.py` |
| `tools/` | 开发工具（**不参与运行时**）：冒烟、存档生成、演示脚本 | `smoke_http.py` · `make_demo_save.py` · `demo_playthrough.py` · `_offline/`（文档转换） |
| `saves/` | 运行产生的存档（**不进 git**） | — |
| `web/assets/plots/` | 21 个 3D 模型（**不进 git**，见下） | `README.md` 说明取回方式 |

## 跑起来

```bash
cd region-sim
python server/app.py          # → http://127.0.0.1:8000/
python -m pytest -q           # → 611 passed
```

Windows 双击 `start.bat` 也行（自动挑解释器、起服务、开浏览器）。

## 3D 模型（21 个 .glb / 533 MB，不在 git 里）

从这里下载，解压到 `web/assets/plots/` 即可：

<https://github.com/shuhuan-007/techathon-region-sim/releases/tag/v1.0-3d-models>

**不下载也能玩** —— 缺模型时前端退化为 2D 格子，规则与数值不变。

## 两个「刻意缺失」

| 缺什么 | 后果 | 怎么办 |
|---|---|---|
| `web/assets/plots/*.glb` | 3D 视角空，退 2D | 从上面 Release 下载 |
| `.env`（AI key） | AI 秘书/叙事不可用 | 没有也能玩；`--offline` 显式关闭 AI |
