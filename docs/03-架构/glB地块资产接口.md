# glB 地块资产接口

> **状态：契约已冻结，3D 渲染未做。** 本文把"每个地块该用哪个模型"定成契约，
> 等 glB 模型进来时**只须把文件放进目录**，引擎与前端都不用再改。
>
> 唯一实现：`region-sim/engine/report.py` 模块头的「glB 地块资产契约」一节
> （常量 `_TYPE_CODE` / `_PRODUCT_IN_KEY`、`asset_key()` / `asset_manifest()`）。
> 测试锚点：`region-sim/tests/test_assets.py`。

---

## 一、资产键（asset key）命名契约

**一块地用哪个模型，由一个字符串键唯一决定：**

```
<类型短码>_l<等级>[ _<产出短码> ]
```

- **类型短码**：`agri` / `resource` / `industry` / `service` / `research`
- **等级**：`1` / `2` / `3`
- **产出短码**：**只在该类型同级需要区分模型时才进键**。
  当前 15 张美术图是"5 类型 × 3 级"各一张，**同级产出共用一张**
  （农业 A/B/C/D 同图、工业同级各路线同图），所以**产出目前不进键**。
- 全部小写、只含 `[a-z0-9_]`、形状稳定可预测。

**键由引擎给出**（`/api/map` 的 cell `asset` 字段），前端**不许自己拼键**
——拼不出才允许回退（见 §四）。

### 15 个键（当前全集）

| 类型 | 键 |
|---|---|
| 农业 | `agri_l1` `agri_l2` `agri_l3` |
| 资源 | `resource_l1` `resource_l2` `resource_l3` |
| 工业 | `industry_l1` `industry_l2` `industry_l3` |
| 服务 | `service_l1` `service_l2` `service_l3` |
| 研究 | `research_l1` `research_l2` `research_l3` |

> 配方层研究只有 1 级（见《地块总表》§二），但**美术层按 3 级备图**，
> 所以 `research_l2` / `research_l3` 是**预留键**——manifest 照给，模型可后补。

### 29 组合 → 15 键

《地块总表》§二 的 29 个"类型×等级×产出"组合（农业 12、资源 3、工业 10、服务 3、研究 1）
全部映射到上表 15 键中的 **13 个**（`research_l2/l3` 预留）。
同级不同产出落同一键，正是"15 张图够用"的原因。

> **扩展点**：将来若某类型同级要按产出拆模型（例：工业 2 级区分机械造型），
> 在引擎 `report.py` 的 `_PRODUCT_IN_KEY` 登记 `{PlotType.INDUSTRY: {"H-A": "machine_agri", …}}`，
> 键即变长为 `industry_l2_machine_agri`，旧键废弃。键集合不再保持 15，
> 须同步更新本表与 `test_assets.py` 的 `EXPECTED_15`。

---

## 二、目录与文件名约定

```
region-sim/web/assets/plots/<key>.glb
```

- **文件名 = 键名 + `.glb`**，一个键一个文件。
- 静态路径前缀 `assets/plots/`、扩展名 `.glb` 是引擎常量
  （`ASSETS_DIR` / `ASSETS_EXT`），换格式只改这两处常量与目录里的文件。
- 前端经 `/static/assets/plots/<key>.glb` 取文件（`server/app.py` 的静态路由已覆盖 web/ 全目录，无需改动）。

---

## 三、manifest 形状（`/api/map` 响应）

`GET /api/map` 的响应在 `map` 子对象里**新增两块**：

```jsonc
{
  "ok": true,
  "map": {
    "rows": 10, "cols": 10,
    "cells": [
      {
        "plot_id": "0,0",
        "owner": "p1",
        "mine": true,
        "asset": "agri_l1"      // ★ 新增：有地块 → 键；空地 → null
      },
      {
        "plot_id": "0,1",
        "owner": null,
        "mine": false,
        "asset": null           // 空地/无模型 → null（前端的回退信号）
      }
      // … 共 100 格
    ],
    "assets": {                 // ★ 新增：键 → 文件信息（manifest）
      "agri_l1":   { "file": "agri_l1.glb",   "path": "assets/plots/agri_l1.glb" },
      "agri_l2":   { "file": "agri_l2.glb",   "path": "assets/plots/agri_l2.glb" }
      // … 恰好 15 条，覆盖全部键
    }
  }
}
```

- manifest **覆盖全部 15 键**，不论文件是否已存在——它声明的是**路径约定**，不是目录清单。
- 前端流程：`cell.asset` → 查 `map.assets[key].path` → 拼 `/static/` 前缀取模型。

---

## 四、缺模型时的回退（不留死路）

**两处都表示"没有模型可用"，前端一律回退到现有 2D 渲染：**

1. **`cell.asset == null`**（空地或未来的未定类型）——cell DOM 上**没有 `data-asset` 属性**，
   维持现有 2D 格子。
2. **键存在但文件缺失** —— 前端 `fetch` / Loader 加载 `/static/assets/plots/<key>.glb` 失败
   （404 等）时，**保持该格的 2D 渲染不动**，不弹错、不阻塞其它格。

当前前端**只挂 `data-asset` 属性、不加载任何模型**（最小改动，
不引 three.js、不写 3D 逻辑）。回退路径由上面两条约定预留，
将来接 3D 时按"拿不到文件 → 留下 2D 格"实现即可。

---

## 五、接 glB 时可能需要补充的字段（建议，**未实现**）

模型进来后大概率需要的额外信息，**现在不定、留作清单**：

- **旋转 / 朝向**：地块模型是否随朝向旋转（如道路、邻接方向）。可加 `asset_transform: {rot_y}`。
- **比例 / 偏移**：模型与格子的缩放、落地偏移。可加 `scale` / `offset`。
- **状态变体**：在建（半透明/脚手架）、停工（暗色）、不属于我（锁定色）是**换模型还是加材质罩层**——
  契约暂以"同一键一个文件"为准，状态用罩层表达。
- **产出细节层**：若以后同级产出要视觉区分（农田种 A vs 种 D），是**换贴图**还是**叠加小模型**
  ——决定 `_PRODUCT_IN_KEY` 是否启用。

这些字段进来时**加在 manifest 的对应键条目里**（向后兼容：旧条目没有就当默认），
不要改 `cell.asset` 的取值，键契约不动。
