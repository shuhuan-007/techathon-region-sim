/* APP.cityTicker —— 数据飞字层（city-modal CSS overlay）
   ─────────────────────────────────────────────────────────────
   职责：city-modal 打开期间，把 STATE 的快照（季度/现金/幸福度/人口/
   地块数）以 CSS keyframes 驱动的"飞字"横穿/画弧/盘旋弹窗。
   只读 STATE、零侵入：不写入、不修改、不算乘法。
   ───────────────────────────────────────────────────────────── */
window.APP = window.APP || {};
(function (APP) {

  /* 内部状态 */
  let _spawnTimer = 0;
  let _container = null;

  /* 数据源（主代理已定格，照搬）——只在 spawn 一条时调一次。
     引擎字段缺了那条就跳过，不报错。 */
  function _read(){
    if (typeof STATE === 'undefined' || !STATE) return [];
    const plots = (STATE.plots || []);
    const running = plots.filter(p => p && p.status === 'running').length;
    const cons   = plots.filter(p => p && p.status === 'constructing').length;
    /* STATE.happiness 是 0-1 小数；population_count 是标量（report.py:272-277
       始终有值，与 plan 无关，引擎保留小数，按显示惯例取 1 位）；cash 直接取整。
       旧键 STATE.population.N 不存在（N 不是 PopulationResult 子键），已废弃。 */
    const happy = (STATE.happiness != null) ? Math.round(STATE.happiness * 100) : null;
    const pop   = (STATE.population_count != null)
                  ? Number(STATE.population_count).toFixed(1) : null;
    const cash  = (STATE.cash != null) ? Math.round(STATE.cash) : null;
    const q     = STATE.quarter;
    const items = [];
    if (q      != null) items.push({icon:"📅", text:`Q${q}`});
    if (cash   != null) items.push({icon:"💰", text:`${cash}`});
    if (happy  != null) items.push({icon:"😊", text:`${happy}%`});
    if (pop    != null) items.push({icon:"👥", text:pop});
    if (plots.length) items.push({icon:"🏗️", text:`${running}+${cons}在建`});
    if (plots.length) items.push({icon:"🏘️", text:`${plots.length}块`});
    return items;
  }

  /* 容器：不预置在 HTML，JS 懒创建 + append 到 #city-modal。 */
  function _ensureContainer(){
    if (_container && document.body.contains(_container)) return _container;
    let c = document.getElementById('city-ticker');
    if (!c){
      const modal = document.getElementById('city-modal');
      if (!modal) return null;
      c = document.createElement('div');
      c.id = 'city-ticker';
      modal.appendChild(c);
    }
    _container = c;
    return c;
  }

  /* 随机挑路线 class：line / arc / circle。
     line 最常见，arc 其次，circle 罕见（保持视觉多样但不过度花哨）。 */
  function _pickRoute(){
    const r = Math.random();
    if (r < 0.50) return 'fly-line';
    if (r < 0.85) return 'fly-arc';
    return 'fly-circle';
  }

  /* spawn 一条飞字。 */
  function _spawn(){
    const c = _ensureContainer();
    if (!c) return;
    /* 并发上限：多于 8 条就跳过这次。 */
    if (c.children.length > 8) return;

    const items = _read();
    if (!items.length) return;  /* 没数据就不放 */

    /* 按权重随机：直接均匀抽（item 用尽允许重复）。 */
    const it = items[Math.floor(Math.random() * items.length)];

    const el = document.createElement('div');
    const routeClass = _pickRoute();
    el.className = 'fly ' + routeClass;
    el.textContent = `${it.icon} ${it.text}`;

    /* 起始 Y：10%-70% 容器高。
       注意：fly-circle 通过 CSS `left:50%; top:50% !important` 强制居中，
       inline top 会被 circle 路线忽略（这是预期）。X 由 keyframes 全程控制。 */
    const h = c.clientHeight || 750;  /* modal 高度 ≈ canvas 750 */
    const yTop = h * (0.10 + Math.random() * 0.60);
    el.style.top = yTop + 'px';

    el.addEventListener('animationend', () => { el.remove(); });
    c.appendChild(el);
  }

  /* 下一次 spawn 间隔：2000-4000ms 指数抖动（保守均匀即可）。 */
  function _nextDelay(){
    return 2000 + Math.random() * 2000;
  }

  function _tick(){
    _spawn();
    _spawnTimer = setTimeout(_tick, _nextDelay());
  }

  /* 公开 API ------------------------------------------------------ */
  function start(){
    stop();  /* 幂等：先清一次 */
    const c = _ensureContainer();
    if (!c){
      console.warn('[cityTicker] #city-modal 不存在，无法挂载 ticker');
      return;
    }
    /* 先跑一次读，空就 warn 但依然启动（每 4s 重试一次空转 spawn）。 */
    const items = _read();
    if (!items.length){
      console.warn('[cityTicker] _read() 返回空：STATE 未就绪或字段全缺（继续启动，下一次 spawn 会重试）');
    }
    /* 立即 spawn 一条，后续 2-4s 抖动节拍。 */
    _spawn();
    _spawnTimer = setTimeout(_tick, _nextDelay());
  }

  function stop(){
    if (_spawnTimer){
      clearTimeout(_spawnTimer);
      _spawnTimer = 0;
    }
    const c = document.getElementById('city-ticker');
    if (c) c.innerHTML = '';
  }

  APP.cityTicker = { start, stop };
})(window.APP);
