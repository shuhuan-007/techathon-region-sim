# web/assets/plots/ —— glB 地块模型该放的地方

> 契约见 [`docs/03-架构/glB地块资产接口.md`](../../../docs/03-架构/glB地块资产接口.md)。
> 引擎侧唯一实现：`engine/report.py`（`ASSETS_DIR = "assets/plots"`、`ASSETS_EXT = ".glb"`）；测试锚点：`tests/test_assets.py`。

## 这里放什么

**glB 地块模型文件**（`<key>.glb`）。把 `.glb` 文件丢进这个目录就生效，引擎与前端都不用再改——manifest 声明的是路径约定，不是目录清单。

## 命名规则

```
<类型短码>_l<等级>.glb
```

- **类型短码**：`agri` / `resource` / `industry` / `service` / `research`
- **等级**：`1` / `2` / `3`
- 全部小写；**文件名 = 键名 + `.glb`**。

## 共 15 个键

`agri_l1/l2/l3` · `resource_l1/l2/l3` · `industry_l1/l2/l3` · `service_l1/l2/l3` · `research_l1/l2/l3`
（同级不同产出共用一张模型；`research_l2/l3` 是预留键。）

## 文件不存在时怎么办

**前端回退到现有 2D 格子**：模型文件缺失（fetch 404 等）时，保持该格 2D 渲染不动——不弹错、不阻塞其它格、不留死路。当前前端只挂 `data-asset` 属性、不加载任何模型（未引 three.js）。

> ⚠️ 目录名**已统一为 `plots`**（以引擎 `ASSETS_DIR` 为准）；旧的 `web/assets/tiles/` 已移除。
