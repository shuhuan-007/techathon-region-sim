/* APP.citySky —— "天上报表层"：city-modal 打开期间，在弹窗顶部居中挂一份
   完整的经营报表，视觉上像"一份挂在天上的文件"。
   ─────────────────────────────────────────────────────────────
   与 cityTicker 的分工（用户 2026-10-18 v4 拍板）
   · cityTicker:小漂字,从 STATE 拿三五条值,动画横穿弹窗。语义=装饰/氛围。
   · citySky   :完整报表文档。优先尝试 fetch /api/report 拿**上季结算**的
     report JSON（包含期初/期末现金、增值、支出拆解、价格、效率、队伍等
     长字段）;失败或 4xx/5xx 时退到 STATE 公共字段（cash/quarter/happiness/
     population_count/plots.length）。语义=可读档案。
   ─────────────────────────────────────────────────────────────
   UI 分段（有序）
     1) 头部标题："第 {quarter} 季 · 经营报表"
     2) 现金截面：期初/期末/本季增值/营收
     3) 支出拆解：总支出 + 工资/运营/建造/采购/退税/补贴
     4) 队伍   ：人口·劳均·幸福·饱食（人口读 report.population.population,
                 劳均读 population_level_avg 或 avg_level,幸福/饱食读 food.*）
     5) 地块效率：report.efficiency 数组,最多 3 行,多余用"…"
     6) 市场价  ：report.prices 中 H-A / H-F / H-G / H-J
     7) 交易   ：report.bought / sold / bought_breakdown(只读 key,不画表)
   ─────────────────────────────────────────────────────────────
   数据源
     · fetch('/api/report?game=<game>&player=p1').then(r.ok?r.json():null)
       game 从 location.search 取;失败 / 4xx / 5xx → _report=null,render
       走 fallback 分支(STATE)。
     · 一次性 fetch 并缓存到 _report,反复 open 不重复 fetch(_fetchPromise
       去重)。refresh() 只重画,不重新拉。
   ───────────────────────────────────────────────────────────── */
window.APP = window.APP || {};
(function (APP) {

  let _$sky = null;
  let _opened = false;
  let _report = null;         // 一次成功 fetch 后凕 save
  let _fetchPromise = null;   // 去重

  function _findGame(){
    try {
      return new URLSearchParams(location.search).get('game') || 'default';
    } catch(_){ return 'default'; }
  }

  async function _fetchReport(){
    if (_fetchPromise) return _fetchPromise;
    const game = encodeURIComponent(_findGame());
    _fetchPromise = fetch('/api/report?game=' + game + '&player=p1')
      .then(r => r.ok ? r.json() : null)
      .then(j => (j && j.ok && j.report) ? j.report : null)
      .catch(() => null);
    return _fetchPromise;
  }

  /* 把 [[label,val],[...]] 序列化成 <section><table>;
     缺啥跳过啥(val == null / '')。*/
  function _h(title, rows){
    const rs = rows
      .filter(r => r && r[1] != null && r[1] !== '')
      .map(([k, v]) => '<tr><td class="k">' + k + '</td><td class="v">' + v + '</td></tr>')
      .join('');
    if (!rs) return '';   // 一段全空就不发
    return '<section class="sky-sec"><div class="sky-hd">' + title + '</div><table>' + rs + '</table></section>';
  }

  function _fmtSec(n){ return (n != null && isFinite(n)) ? (Math.round(n * 100) / 100).toFixed(2) : null; }
  function _fmtInt(n){ return (n != null && isFinite(n)) ? Math.round(n) : null; }
  function _fmtPct01(x){ return (x != null && isFinite(x)) ? Math.round(x * 100) + '%' : null; }

  function _render(){
    if (!_$sky) return;
    const parts = [];
    const r = _report;

    /* 标题 quarter：优先 report,否则 STATE */
    let q = null;
    if (r && r.quarter != null) q = r.quarter;
    else if (typeof STATE !== 'undefined' && STATE && STATE.quarter != null) q = STATE.quarter;
    parts.push('<div class="sky-title">第 ' + (q != null ? q : '—') + ' 季 · 经营报表</div>');

    if (r){
      /* 1) 现金截面 */
      parts.push(_h('现金截面', [
        ['期初现金',  _fmtSec(r.cash_before)],
        ['期末现金',  _fmtSec(r.cash_after)],
        ['本季增值',  _fmtSec(r.value_added)],
        ['营收',      _fmtSec(r.sales_revenue)],
      ]));

      /* 2) 支出 */
      parts.push(_h('支出拆解', [
        ['总支出',    _fmtSec(r.total_deduction)],
        ['工资',      _fmtSec(r.wages)],
        ['运营',      _fmtSec(r.operating)],
        ['建造',      _fmtSec(r.construction_cash)],
        ['采购',      _fmtSec(r.materials_cost)],
        ['退税',      _fmtSec(r.wage_tax_refund)],
        ['补贴',      _fmtSec(r.subsidy)],
      ]));

      /* 3) 队伍 */
      if (r.population || r.food){
        const pop = r.population || {};
        const food = r.food || {};
        const popTotal = pop.population != null ? pop.population : null;
        const popLvl   = (pop.population_level_avg != null) ? pop.population_level_avg
                        : (pop.avg_level != null) ? pop.avg_level : null;
        parts.push(_h('队伍', [
          ['人口',     popTotal != null ? Number(popTotal).toFixed(1) : null],
          ['劳均等级', popLvl   != null ? _fmtSec(popLvl) : null],
          ['幸福度',   food.happiness != null ? _fmtPct01(food.happiness) : null],
          ['饱食基准', food.baseline_food != null ? _fmtSec(food.baseline_food) : null],
        ]));
      }

      /* 4) 地块效率:最多 3 行,多余用"…" */
      if (Array.isArray(r.efficiency) && r.efficiency.length){
        const eff = r.efficiency.slice(0, 3).map(e => {
          const lbl = (e && (e.plot_type || e.plot_id)) || '?';
          const eff_v = (e && e.efficiency != null) ? _fmtSec(e.efficiency) : '—';
          const prod = (e && e.product) ? (' → ' + e.product) : '';
          return [lbl, eff_v + prod];
        });
        if (r.efficiency.length > 3) eff.push(['…', r.efficiency.length + ' 条']);
        parts.push(_h('地块效率', eff));
      }

      /* 5) 市场价 */
      if (r.prices){
        const pr = r.prices;
        parts.push(_h('市场价',
          ['H-A', 'H-F', 'H-G', 'H-J']
            .filter(k => pr[k] != null)
            .map(k => [k, _fmtSec(pr[k])])
        ));
      }

      /* 6) 交易 */
      if (r.bought || r.sold || r.bought_breakdown){
        const rows = [];
        if (r.bought != null) rows.push(['买入额', _fmtSec(r.bought)]);
        if (r.sold   != null) rows.push(['卖出额', _fmtSec(r.sold)]);
        if (r.bought_breakdown && typeof r.bought_breakdown === 'object'){
          const keys = Object.keys(r.bought_breakdown);
          if (keys.length) rows.push(['买入品类', keys.join(' · ')]);
        }
        parts.push(_h('交易', rows));
      }
    } else {
      /* fallback:STATE 公共字段 */
      if (typeof STATE !== 'undefined' && STATE){
        const fallback = [];
        if (STATE.cash != null)         fallback.push(['现金', _fmtSec(STATE.cash)]);
        if (STATE.population_count != null)
                                        fallback.push(['人口 N', Number(STATE.population_count).toFixed(1)]);
        if (STATE.happiness != null)    fallback.push(['幸福度', _fmtPct01(STATE.happiness)]);
        const plots = (STATE.plots || []);
        if (plots.length)               fallback.push(['地块', plots.length + ' 块']);
        parts.push(_h('快照(实时 STATE)', fallback));
        parts.push('<div class="sky-note">数据来自实时 STATE —— 完整报表需结算后由 /api/report 给出。</div>');
      } else {
        parts.push('<div class="sky-note">STATE 未就绪。</div>');
      }
    }

    _$sky.innerHTML = parts.join('');
  }

  function _open(){
    if (_opened) return;
    _opened = true;
    const modal = document.getElementById('city-modal');
    if (!modal) return;
    if (!_$sky){
      _$sky = document.createElement('div');
      _$sky.id = 'city-sky';
    }
    modal.appendChild(_$sky);
    /* 先画 STATE fallback,再 fetch report 重画(只在还没拿到过时) */
    _render();
    if (_report == null){
      _fetchReport().then(r => { _report = r; if (_opened) _render(); });
    }
  }

  function _close(){
    _opened = false;
    if (_$sky && _$sky.parentNode) _$sky.parentNode.removeChild(_$sky);
  }

  function _refresh(){ if (_opened) _render(); }

  APP.citySky = { open: _open, close: _close, refresh: _refresh };
})(window.APP);
