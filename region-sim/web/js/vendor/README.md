# Vendored three.js

本目录存放从 jsdelivr/unpkg 下载的 three.js UMD build，避免运行时依赖外网 CDN。

## 文件
- `three.min.js`: three.js r140 UMD, from jsdelivr/unpkg, downloaded 2026-10-17
- `GLTFLoader.js`: three.js r140 examples/js/loaders/GLTFLoader.js UMD, from jsdelivr/unpkg, 2026-10-17

## 加载方式
在 index.html 中 `<script src="js/vendor/three.min.js"></script>` 然后 `<script src="js/vendor/GLTFLoader.js"></script>`
（须在 map.js 之前加载）

## 为什么 r140 而不是 r160
r150+ 后 three.js 官方废弃了 UMD examples（examples/js/），r140 是最后一个 UMD 完整的版本。
本项目零构建零 ESM，必须用 UMD。
