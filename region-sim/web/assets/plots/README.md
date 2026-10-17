# web/assets/plots/ —— glB 地块模型放置目录（中文 20 键契约）

> 契约变化 2026-10-17：旧 15 键英文小写契约（`agri_l1` 等）**作废**。
> 现行契约直接用建模目录的**中文目录名**作键 —— 文件名 = 键名 + `.glb`。
> 引擎侧唯一实现：`engine/report.py`（`ASSETS_DIR = "assets/plots"`、`ASSETS_EXT = ".glb"`）；
> 查表：`engine/assets.py:asset_key()`；测试锚点：`tests/test_assets.py`。

## 这里放什么
20 个 `.glb` 文件，已交付（约 326 MB，不入 git，见 `.gitignore` 例外）：

- 简单三类（`<前缀><level>`，3 类×3 级 = 9 个）：
  `农业1.glb` `农业2.glb` `农业3.glb`
  `资源1.glb` `资源2.glb` `资源3.glb`
  `服务1.glb` `服务2.glb` `服务3.glb`
- 工业 1 级：`工业1.glb`
- 工业 2 级（四条机械路线）：`工业农业2.glb` `工业工业2.glb` `工业资源2.glb` `工业服务2.glb`
- 工业 3 级（四条机械路线 + 研究线 I）：
  `工业农业3.glb` `工业工业3.glb` `工业资源3.glb` `工业服务业3.glb` `工业高技术3.glb`
- 研究只有 1 级：`研究.glb`

合计 20 个。⚠️ 美术笔误要保留：`工业服务2`（二级少"业"）与 `工业服务业3`（三级多"业"）**不"修正"**。

## 引擎怎么查
`engine/assets.py:asset_key(plot_type, level, product)` 返回中文目录名（即本目录里的文件名主干）；
`engine/report.py` 把它挂到 `/api/map` 响应的 `cell.asset` 与 `assets` manifest（`{file, path, exists}`）。
research level 2/3 与非法组合返回 `null` → 前端回退 2D 渲染，不留死路。

## 前端怎么用
1. 从 `/api/map` 拿 `cell.asset`（中文键）。
2. 拼 URL：`/static/assets/plots/${encodeURIComponent(key)}.glb`。
3. three.js GLTFLoader 加载；失败（404 等）静默保持 2D cell 渲染（见 `web/js/map.js` 的 S5 3D 层）。

## 文件不存在时
保持 2D 渲染不弹错；不阻塞其它格。
