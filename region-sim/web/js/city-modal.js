/* APP.cityModal —— 全屏 3D 城市预览 modal（备用方案/死胡同退路）
   ─────────────────────────────────────────────────────────────
   与 APP.map3d 完全独立的第二份 three.js 实例：自己的 canvas /
   scene / camera / renderer，互不共享,避免互相污染。

   设计（2026-10-17，用户拍板的死胡同退路）
   ─────────────────────────────────────────────────────────────
   · 用户在地图 panel 点「🏙️ 3D 城市」按钮 → 全屏 modal 打开,
     中间 900×600 canvas 显示 10×10 网格上的 glb 模型 + 一块深绿底盘。
   · **只读**——没有 OrbitControls、没有点击/拖拽/滚轮事件,
     相机位置一次性写死,就只渲染一帧静态图。
   · 世界坐标系：
       x 向右 = 地图 col 方向,
       y 向上,
       z 朝地图 row 方向（顶视时 row 向屏幕下 ≈ z 朝屏幕外下）。
     cell (row,col) 中心放在 (col+0.5, 0, row+0.5) —— 与主线 map3d
     的"先摆后绕 -π/2"约定不一致是有意的：这里我们直接把模型当
     顶视放、相机斜俯拍,不需要把 glb 翻 90°。
     ⚠️ 但 glb 作者建模时 +y 是"上",本 modal 相机在 (5,12,5)
     斜向下看 (5,0,5),所以模型只要 y 朝上即可,**不需要**绕 x 转。
   · scale：同主线——Box3 实测水平跨距,缩到 cell 尺寸 0.9。
   · constructing 灰化:同主线 MeshBasicMaterial 0x888888。
   · glb 加载：自己一份 _gltfCache（不共享主线）,加载逻辑复制
     主线带 15s timeout 防悬挂,但 warn 前缀改成 [cityModal]。
   ───────────────────────────────────────────────────────────── */
window.APP = window.APP || {};
(function (APP) {
  /* 内部状态 */
  let _canvas = null;          // <canvas id="city-canvas">
  let _renderer = null;        // THREE.WebGLRenderer
  let _scene = null;           // THREE.Scene
  let _camera = null;          // THREE.OrthographicCamera
  let _plotGroup = null;       // 每次 _renderCity 清空重摆
  let _ground = null;          // 底盘 mesh,一次建好常留
  let _enabled = true;
  let _failedUrls = {};

  const _gltfCache = {};       // name → Promise<GLTF>（独立缓存）
  let _gltfLoader = null;

  /* canvas 与相机的固定尺寸（900×600 / 父代拍板） */
  const CANVAS_W = 900, CANVAS_H = 600;

  function _open(){
    const m = document.getElementById('city-modal');
    if (!m) return;
    m.classList.remove('hidden');
    _ensureRenderer();
    _renderCity();
  }

  function _close(){
    const m = document.getElementById('city-modal');
    if (m) m.classList.add('hidden');
    // 不 destroy gltf cache——下次打开还能复用
    if (_plotGroup) _plotGroup.clear();
  }

  function _ensureRenderer(){
    if (_renderer || !_enabled) return;
    if (typeof THREE === 'undefined' || !THREE.GLTFLoader){
      console.warn('[cityModal] THREE 或 GLTFLoader 未加载 — modal 关闭');
      _enabled = false; return;
    }
    _canvas = document.getElementById('city-canvas');
    if (!_canvas){ _enabled = false; return; }
    try {
      _renderer = new THREE.WebGLRenderer({
        canvas: _canvas, antialias: false, alpha: false,
        powerPreference: 'low-power',
      });
      _renderer.setPixelRatio(1);
      _renderer.setSize(CANVAS_W, CANVAS_H, /*updateStyle*/false);
      _renderer.setClearColor(0x0a0f14, 1);
    } catch(e){
      console.warn('[cityModal] WebGLRenderer 构造失败:', e);
      _enabled = false; return;
    }
    _scene = new THREE.Scene();
    /* 光照：与主线 map3d 同款（Ambient 1.0 + 两盏方向光）*/
    _scene.add(new THREE.AmbientLight(0xffffff, 1.0));
    const dirA = new THREE.DirectionalLight(0xffffff, 0.6);
    dirA.position.set(1, 1, 1.5);
    _scene.add(dirA);
    const dirB = new THREE.DirectionalLight(0xffffff, 0.4);
    dirB.position.set(-1, -1, 1);
    _scene.add(dirB);

    /* 正交相机俯视 —— 父代拍板：
       position (5, 12, 5) → lookAt (5, 0, 5),
       Ortho frustum -6..6 / -4..4(对应 900×600 比例 3:2)。 */
    _camera = new THREE.OrthographicCamera(-6, 6, 4, -4, 0.1, 100);
    _camera.position.set(5, 12, 5);
    _camera.up.set(0, 1, 0);
    _camera.lookAt(5, 0, 5);
    _camera.updateProjectionMatrix();

    /* 底盘：BoxGeometry(11, 0.3, 11),top 面在 y=0,深绿色 */
    const groundGeo = new THREE.BoxGeometry(11, 0.3, 11);
    const groundMat = new THREE.MeshLambertMaterial({ color: 0x2a4a2a });
    _ground = new THREE.Mesh(groundGeo, groundMat);
    _ground.position.set(5, -0.15, 5);
    _scene.add(_ground);

    _plotGroup = new THREE.Group();
    _scene.add(_plotGroup);
    _gltfLoader = new THREE.GLTFLoader();
  }

  /* 异步加载一份 glb（独立缓存 + 15s timeout 防悬挂）。
     与主线 _loadGlb 同一份代码,只是 warn 前缀不同。 */
  function _loadGlb(name){
    if (_gltfCache[name]) return _gltfCache[name];
    const url = '/static/assets/plots/' + encodeURIComponent(name) + '.glb';
    _gltfCache[name] = new Promise((resolve, reject) => {
      let settled = false;
      const timer = setTimeout(() => {
        if (settled) return;
        settled = true;
        if (!_failedUrls[url]){
          console.warn('[cityModal] glb 加载超时(15s)', url);
          _failedUrls[url] = true;
        }
        reject(new Error('glb load timeout: ' + url));
      }, 15000);
      _gltfLoader.load(url,
        (gltf) => {
          if (settled) return;
          settled = true;
          clearTimeout(timer);
          resolve(gltf);
        },
        undefined,
        (err) => {
          if (settled) return;
          settled = true;
          clearTimeout(timer);
          if (!_failedUrls[url]){
            console.warn('[cityModal] glb 加载失败', url, err && err.message ? err.message : err);
            _failedUrls[url] = true;
          }
          reject(err);
        });
    });
    return _gltfCache[name];
  }

  /* 把 gltf 实例摆到 (x, 0, z) —— glb 必须 y 朝上,相机已经配好俯视角度。
     scale = 0.9 / max(size.x, size.z)（让 glb 最宽处恰好 0.9 个 cell 单位,
     cell 中心间距 1.0,模型间不重叠,再加 1.1 倍水平跨距更宽限）。
     asGray=true 时换共享灰色 Basic 材质。 */
  function _placeGlb(gltf, x, z, asGray){
    const inst = gltf.scene.clone(true);
    if (asGray){
      const gray = new THREE.MeshBasicMaterial({ color: 0x888888 });
      inst.traverse(o => { if (o.isMesh) o.material = gray; });
    }
    /* identity 下量 bbox */
    const bbox = new THREE.Box3().setFromObject(inst);
    const size = new THREE.Vector3();
    bbox.getSize(size);
    const center = new THREE.Vector3();
    bbox.getCenter(center);
    const horizSpan = Math.max(size.x, size.z) || 1;
    /* 0.9 / 1.0：cell 物理宽 1.0,模型占 0.9。父代说"1.1 倍水平跨距摆"
       意思是 cell 中心间距 1.1 —— 我们的底盘已经按 11 设计(10 × 1.1),
       所以这里 scale 按 cell=1.0 算即可,摆放时 x/z 用 ×1.1 间隔。 */
    const scale = 0.9 / horizSpan;
    inst.position.set(-center.x, -center.y, -center.z);
    const wrapper = new THREE.Group();
    wrapper.add(inst);
    wrapper.scale.setScalar(scale);
    /* 模型几何中心贴地：bbox 高度尺寸 × scale 后,把 wrapper 抬高
       半个模型高度 —— 但因为 center.y 已被移到原点,此时 glb 的
       几何中心在原点,要让底面在 y=0,需要把 wrapper 抬高 size.y*scale/2。 */
    wrapper.position.set(x, size.y * scale / 2, z);
    _plotGroup.add(wrapper);
  }

  /* 主渲染：清空 → 摆全部 plot → 渲染 */
  async function _renderCity(){
    if (!_enabled || !_renderer) return;
    if (typeof STATE === 'undefined' || typeof MAP === 'undefined'){
      console.warn('[cityModal] STATE/MAP 未全局可用');
      return;
    }
    if (!STATE || !MAP){ return; }
    /* 清空 */
    while (_plotGroup.children.length){
      _plotGroup.remove(_plotGroup.children[0]);
    }

    /* cell 索引 */
    const cellById = {};
    (MAP.cells || []).forEach(c => { if (c && c.plot_id) cellById[c.plot_id] = c; });

    /* 副标题：第 N 季 · X 块地块 */
    const subtitleEl = document.getElementById('cm-subtitle');
    if (subtitleEl){
      const q = STATE.quarter !== undefined ? STATE.quarter : '—';
      const n = (STATE.plots || []).length;
      subtitleEl.textContent = `第 ${q} 季 · ${n} 块地块`;
    }

    /* cell 中心间距 1.1（父代拍板的"加宽不重叠"）。
       cell (row,col) → 世界 (col*1.1 + 0.05, 0, row*1.1 + 0.05),
       范围 [0.05, 9.95],中心 5 正好对齐底盘中心 (5,·,5)。 */
    const SPACING = 1.1;
    const OFFSET = 0.05;

    const tasks = [];
    (STATE.plots || []).forEach(p => {
      const pid = p.plot_id;
      const cell = cellById[pid];
      if (!cell || !cell.asset) return;
      const key = cell.asset;
      const asGray = (p.status === 'constructing') || (cell.status === 'constructing');
      /* plot_id 是 "r,c" 字符串 */
      const parts = pid.split(',');
      const row = parseInt(parts[0], 10);
      const col = parseInt(parts[1], 10);
      if (isNaN(row) || isNaN(col)) return;
      const x = col * SPACING + OFFSET;
      const z = row * SPACING + OFFSET;
      tasks.push(
        _loadGlb(key).then(gltf => {
          _placeGlb(gltf, x, z, asGray);
          _renderFrame();
        }).catch(() => {/* 已 warn,静默 */})
      );
    });

    /* 即便一个 plot 都没有,也把底盘 render 出来 */
    _renderFrame();
    if (tasks.length){
      await Promise.allSettled(tasks);
    }
    _renderFrame();
  }

  function _renderFrame(){
    if (_renderer && _scene && _camera){
      _renderer.render(_scene, _camera);
    }
  }

  function _bind(){
    const btn = document.getElementById('btn-city-view');
    if (btn) btn.onclick = _open;
    const closeBtn = document.getElementById('btn-city-close');
    if (closeBtn) closeBtn.onclick = _close;
    document.addEventListener('keydown', e => {
      if (e.key === 'Escape') _close();
    });
    /* 点击 modal_batch 背景也关闭（除了 canvas box） */
    const m = document.getElementById('city-modal');
    if (m){
      m.addEventListener('click', (e) => {
        if (e.target === m) _close();
      });
    }
  }

  if (document.readyState === 'loading'){
    document.addEventListener('DOMContentLoaded', _bind);
  } else {
    _bind();
  }

  APP.cityModal = { open: _open, close: _close };
})(window.APP);
