"""Reference-style overview page template for the backtest hub."""

OVERVIEW_TEMPLATE = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  *{box-sizing:border-box}
  html{color-scheme:dark light}
  :root{
    --bg:#080b10; --bg2:#0c1119; --panel:#101722; --panel2:#141d2a; --line:#223044;
    --text:#e8eef8; --muted:#8ea0b8; --soft:#5f7189; --grid:rgba(140,165,195,.14);
    --c1:#60a5fa; --c2:#fbbf24; --c3:#a78bfa; --c4:#34d399; --c5:#fb7185; --c6:#22d3ee;
    --quality:#f472b6; --good:#34d399; --bad:#fb7185; --shadow:0 18px 50px rgba(0,0,0,.28);
  }
  @media (prefers-color-scheme: light){
    :root{
      --bg:#f6f8fb; --bg2:#eef2f7; --panel:#ffffff; --panel2:#f8fafc; --line:#dbe4ef;
      --text:#0f172a; --muted:#5b6b82; --soft:#7b8aa0; --grid:rgba(30,50,80,.12);
      --quality:#be185d; --shadow:0 18px 40px rgba(15,23,42,.08);
    }
  }
  html,body{margin:0;min-height:100%;background:
    radial-gradient(1100px 500px at 15% -10%, rgba(96,165,250,.12), transparent 60%),
    radial-gradient(900px 420px at 90% 0%, rgba(34,211,238,.09), transparent 55%),
    var(--bg);
    color:var(--text);font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,"Noto Sans SC",sans-serif;
    -webkit-font-smoothing:antialiased}
  .wrap{max-width:1200px;margin:0 auto;padding:0 24px}
  header{padding:46px 0 10px}
  .eyebrow{font-size:11px;letter-spacing:.18em;text-transform:uppercase;color:var(--soft);font-weight:650}
  h1{font-size:clamp(30px,4.5vw,52px);line-height:1.06;letter-spacing:-.035em;margin:12px 0 12px;font-weight:720}
  .sub{max-width:860px;color:var(--muted);font-size:15px;line-height:1.75;margin:0}
  .sub strong{color:var(--text);font-weight:620}
  .pills{display:flex;flex-wrap:wrap;gap:8px;margin-top:18px;align-items:center}
  .pill{font-size:12px;color:var(--muted);border:1px solid var(--line);background:var(--panel);border-radius:999px;padding:7px 11px;text-decoration:none}
  .pill b{color:var(--text);font-weight:620}
  .pill.link{color:var(--text);border-color:color-mix(in srgb,var(--c1) 45%,var(--line));font-weight:650}
  .cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:26px 0 16px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:17px 17px 15px;box-shadow:var(--shadow);min-width:0}
  .quality-alert{display:grid;grid-template-columns:1.35fr 1fr 1fr;gap:0;background:linear-gradient(120deg,rgba(244,114,182,.10),rgba(148,163,184,.055) 55%,transparent);border:1px solid rgba(244,114,182,.30);border-radius:16px;padding:16px 18px;margin:0 0 16px;box-shadow:var(--shadow)}
  .qa-copy{padding-right:18px}
  .qa-kicker{font-size:10px;letter-spacing:.12em;text-transform:uppercase;color:var(--quality);font-weight:700;margin-bottom:7px}
  .qa-copy p{margin:0;color:var(--muted);font-size:12px;line-height:1.7}
  .qa-copy b{color:var(--text);font-weight:680}
  .qa-stat{border-left:1px solid color-mix(in srgb,var(--line) 80%,transparent);padding:0 0 0 16px;margin-left:16px;display:flex;flex-direction:column;justify-content:center;min-width:0}
  .qa-stat span{font-size:10.5px;color:var(--soft);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .qa-stat b{font-size:25px;line-height:1.2;letter-spacing:-.03em;font-variant-numeric:tabular-nums;color:var(--quality);margin:3px 0 4px}
  .qa-stat small{font-size:10.5px;color:var(--muted);font-variant-numeric:tabular-nums;line-height:1.55}
  .qa-stat.pit b{color:var(--muted)}
  .card.q-buy{border-color:rgba(34,211,238,.45)}
  .card.q-ma{border-color:rgba(251,113,133,.42)}
  .card.q-etf{border-color:rgba(52,211,153,.38)}
  .card.q-quality{border-color:rgba(244,114,182,.38)}
  .card-label{display:flex;align-items:center;gap:7px;color:var(--muted);font-size:12px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .swatch{width:9px;height:9px;border-radius:50%;display:inline-block;flex:none;box-shadow:0 0 0 3px color-mix(in srgb, currentColor 10%, transparent)}
  .card-big{font-size:30px;line-height:1.15;font-weight:700;letter-spacing:-.03em;margin:10px 0 8px;font-variant-numeric:tabular-nums}
  .card-meta{font-size:11.5px;color:var(--muted);line-height:1.7;font-variant-numeric:tabular-nums}
  .chart-card{background:var(--panel);border:1px solid var(--line);border-radius:20px;padding:20px;box-shadow:var(--shadow);margin:16px 0}
  .chart-head{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap}
  .chart-title{font-size:19px;font-weight:680;letter-spacing:-.01em;margin:0}
  .chart-sub{font-size:12px;color:var(--muted);margin:5px 0 0;font-variant-numeric:tabular-nums}
  .controls{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
  .seg{display:inline-flex;align-items:center;background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:3px;gap:2px}
  .seg button{border:0;background:transparent;color:var(--muted);font:inherit;font-size:11.5px;font-weight:600;padding:6px 9px;border-radius:7px;cursor:pointer;white-space:nowrap}
  .seg button:hover{color:var(--text)}
  .seg button.active{background:var(--text);color:var(--bg)}
  .ghost{border:1px solid var(--line);background:var(--panel2);color:var(--muted);border-radius:9px;padding:7px 10px;font:inherit;font-size:11.5px;font-weight:600;cursor:pointer}
  .ghost:hover{color:var(--text);border-color:var(--soft)}
  .legend{display:flex;flex-wrap:wrap;gap:6px;margin:16px 0 2px}
  .legend-item{border:1px solid var(--line);background:var(--panel2);border-radius:9px;padding:6px 8px;display:inline-flex;align-items:center;gap:7px;cursor:pointer;font:inherit;font-size:11.5px;color:var(--muted);transition:opacity .15s,border-color .15s}
  .legend-item:hover{border-color:var(--soft);color:var(--text)}
  .legend-item.off{opacity:.32}
  .legend-item .legend-val{margin-left:2px;color:var(--text);font-variant-numeric:tabular-nums;font-weight:650}
  .legend-item .line{width:14px;height:0;border-top:2px solid currentColor;display:inline-block;flex:none}
  .canvas-wrap{position:relative;height:450px;margin-top:4px}
  canvas{display:block;width:100%;height:100%;outline:none;cursor:crosshair}
  .tooltip{position:absolute;pointer-events:none;min-width:190px;max-width:280px;background:color-mix(in srgb, var(--panel2) 94%, transparent);border:1px solid var(--line);border-radius:11px;padding:9px 10px;box-shadow:0 12px 30px rgba(0,0,0,.28);opacity:0;transform:translateY(3px);transition:opacity .1s,transform .1s;backdrop-filter:blur(8px);z-index:3}
  .tooltip.show{opacity:1;transform:translateY(0)}
  .tt-date{font-size:10.5px;color:var(--muted);letter-spacing:.02em;margin-bottom:5px}
  .tt-row{display:flex;align-items:center;gap:6px;font-size:11px;line-height:1.7;white-space:nowrap}
  .tt-row .dot{width:6px;height:6px;border-radius:50%;flex:none}
  .tt-row .nm{color:var(--muted);overflow:hidden;text-overflow:ellipsis;max-width:112px}
  .tt-row .vv{margin-left:auto;font-variant-numeric:tabular-nums;color:var(--text);font-weight:650}
  .tt-row .pp{width:47px;text-align:right;font-variant-numeric:tabular-nums;color:var(--muted);font-size:10.5px}
  .chart-foot{display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;color:var(--soft);font-size:11px;margin-top:7px;line-height:1.6}
  .grid2{display:grid;grid-template-columns:1.1fr .9fr;gap:12px;margin:16px 0}
  .table-card{background:var(--panel);border:1px solid var(--line);border-radius:18px;padding:17px;box-shadow:var(--shadow);min-width:0}
  .table-head{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin-bottom:8px}
  .table-head h2{font-size:16px;margin:0;letter-spacing:-.01em}
  .table-head span{font-size:11px;color:var(--soft)}
  .table-scroll{overflow-x:auto;margin:0 -4px;padding:0 4px}
  table{width:100%;border-collapse:collapse;font-size:12.5px;font-variant-numeric:tabular-nums}
  th{color:var(--soft);font-size:10.5px;font-weight:650;letter-spacing:.02em;text-align:right;padding:9px 7px;border-bottom:1px solid var(--line);white-space:nowrap}
  th:first-child,td:first-child{text-align:left}
  td{text-align:right;padding:9px 7px;border-bottom:1px solid color-mix(in srgb,var(--line) 65%,transparent);white-space:nowrap;color:var(--text)}
  tbody tr:last-child td,tbody tr:last-child th{border-bottom:0}
  tbody tr:hover{background:color-mix(in srgb,var(--text) 3.5%,transparent)}
  .quality-row{background:color-mix(in srgb,#f472b6 7%,transparent)}
  .quality-row.pit-row{background:color-mix(in srgb,#94a3b8 6%,transparent)}
  .neg-row{background:color-mix(in srgb,#fb7185 5%,transparent)}
  .pos{color:var(--good)} .neg{color:var(--bad)}
  td .swatch{vertical-align:-1px;margin-right:6px}
  .tag{font-size:9px;color:var(--soft);border:1px solid var(--line);border-radius:4px;padding:1px 4px;margin-left:4px;vertical-align:1px}
  .annual-card table{font-size:11px}
  .annual-card th,.annual-card td{padding:7px 4px}
  .notes{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:16px 0 36px}
  .note{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:14px 15px;color:var(--muted);font-size:11.5px;line-height:1.75}
  .note b{color:var(--text);font-weight:650}
  .note .n{font-size:10px;letter-spacing:.14em;color:var(--soft);font-weight:700;margin-bottom:6px}
  footer{border-top:1px solid var(--line);padding:18px 0 40px;color:var(--soft);font-size:11px;line-height:1.7}
  footer .wrap{display:flex;justify-content:space-between;gap:14px;flex-wrap:wrap}
  code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:10.5px;color:var(--muted)}
  @media (max-width:1000px){.grid2{grid-template-columns:1fr}.notes{grid-template-columns:1fr}.quality-alert{grid-template-columns:1fr 1fr}.qa-copy{grid-column:1/-1;padding-right:0}}
  @media (max-width:760px){.cards{grid-template-columns:repeat(2,minmax(0,1fr))}.card-big{font-size:25px}.canvas-wrap{height:370px}header{padding-top:32px}.quality-alert{grid-template-columns:1fr}.qa-stat{border-left:0;border-top:1px solid color-mix(in srgb,var(--line) 80%,transparent);margin:12px 0 0;padding:12px 0 0}}
  @media (max-width:520px){.wrap{padding:0 14px}.cards{grid-template-columns:1fr 1fr}.card{padding:13px}.card-big{font-size:22px}.card-meta{font-size:10.5px}.chart-card{padding:14px}.chart-head{gap:10px}.canvas-wrap{height:330px}.legend-item{font-size:10.5px;padding:5px 7px}.chart-foot{font-size:10px}}
</style>
</head>
<body>
<header class="wrap">
  <div class="eyebrow">Quant research · 2026-11-15</div>
  <h1>回测总览：首板、ETF 与小市值，谁赢了？</h1>
  <p class="sub">同一区间、同一套指标：<strong>2020-01-02 → 2026-10-30</strong>，<strong>8 条代表曲线</strong>。所有资金曲线按 <strong>20 万初始资金</strong>回测，图中以区间起点 = 100 展示；2026 为 YTD。首板策略是日线机械复现，ETF 和小市值是各自研究口径，不能只看收益排名。</p>
  <div class="pills">__PILLS__<a class="pill link" href="index.html">分类对比 →</a><a class="pill link" href="index.html#catalog">全部目录 →</a></div>
</header>

<main class="wrap">
  <section class="cards">__CARDS__</section>

  <section class="quality-alert">
    <div class="qa-copy">
      <div class="qa-kicker">质量 A/B baseline · 同一规则，两套池子</div>
      <p>当前中证 1000 快照做出 <b>__SNAP_TOTAL__</b>，换成 PIT 全 A（含退市股）只剩 <b>__PIT_TOTAL__</b>。__ALERT_COPY__</p>
    </div>
    <div class="qa-stat">
      <span>当前中证 1000 快照</span>
      <b>__SNAP_TOTAL__</b>
      <small>年化 __SNAP_ANN__ · 回撤 __SNAP_MDD__ · Sharpe __SNAP_SHARPE__</small>
    </div>
    <div class="qa-stat pit">
      <span>PIT 全 A · 含退市股</span>
      <b>__PIT_TOTAL__</b>
      <small>年化 __PIT_ANN__ · 回撤 __PIT_MDD__ · Sharpe __PIT_SHARPE__</small>
    </div>
  </section>

  <section class="chart-card">
    <div class="chart-head">
      <div>
        <h2 class="chart-title">累计净值</h2>
        <p class="chart-sub" id="range-label">2020-01-02 → 2026-10-30 · 起点 = 100</p>
      </div>
      <div class="controls">
        <div class="seg" role="group" aria-label="区间">
          <button data-range="all" class="active">全部</button>
          <button data-range="5y">近 5 年</button>
          <button data-range="3y">近 3 年</button>
          <button data-range="1y">近 1 年</button>
        </div>
        <div class="seg" role="group" aria-label="纵轴">
          <button data-scale="log" class="active">对数</button>
          <button data-scale="linear">线性</button>
        </div>
        <button class="ghost" id="reset">重置</button>
      </div>
    </div>
    <div class="legend" id="legend"></div>
    <div class="canvas-wrap" id="canvas-wrap">
      <canvas id="chart" role="img" aria-label="八条策略累计净值曲线"></canvas>
      <div class="tooltip" id="tooltip"></div>
    </div>
    <div class="chart-foot">
      <span>点击图例可隐藏/显示曲线 · 鼠标悬停查看当日净值与区间收益 · 纵轴默认对数</span>
      <span>区间起点归一为 100</span>
    </div>
  </section>

  <section class="grid2">
    <div class="table-card">
      <div class="table-head"><h2>区间指标</h2><span>2020-01-02 → 2026-10-30 · 20 万初始</span></div>
      <div class="table-scroll">
        <table>
          <thead><tr><th>策略</th><th>总收益</th><th>年化</th><th>最大回撤</th><th>Sharpe</th><th>胜率</th><th>盈亏比</th><th>交易</th><th>期末</th></tr></thead>
          <tbody>__METRICS_ROWS__</tbody>
        </table>
      </div>
    </div>
    <div class="table-card annual-card">
      <div class="table-head"><h2>年度收益</h2><span>2020–2025 完整年度 · 2026 YTD</span></div>
      <div class="table-scroll">
        <table>
          <thead><tr>__ANNUAL_HEAD__</tr></thead>
          <tbody>__ANNUAL_ROWS__</tbody>
        </table>
      </div>
    </div>
  </section>

  <section class="notes">
    __NOTES__
  </section>
</main>

<footer>
  <div class="wrap">
    <span>数据：<code>docs/backtest-results/</code> 下首板、ETF、小市值、PIT 与开盘缺口回测 · 生成 __GENERATED__</span>
    <span>Sharpe 未扣无风险利率；年化按 252 个交易日折算；研究展示，不构成投资建议。</span>
  </div>
</footer>

<script>
const DATA = __DATA__;
const canvas = document.getElementById('chart');
const ctx = canvas.getContext('2d');
const tooltip = document.getElementById('tooltip');
const rangeLabel = document.getElementById('range-label');
const legend = document.getElementById('legend');
const state = { range: 'all', log: true, hidden: new Set(), hover: null };
const YEARS = ['2020', '2021', '2022', '2023', '2024', '2025', '2026'];

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue('--' + name).trim();
}
function fmtPct(v) {
  if (v === null || v === undefined || !Number.isFinite(Number(v))) return '—';
  const x = Number(v) * 100;
  return (x > 0 ? '+' : '') + x.toFixed(1) + '%';
}
function fmtNum(v) {
  if (v === null || v === undefined || !Number.isFinite(Number(v))) return '—';
  return Number(v).toFixed(2);
}
function fmtIndex(v) {
  if (v === null || v === undefined || !Number.isFinite(Number(v))) return '—';
  return Number(v).toFixed(v >= 1000 ? 0 : 1);
}
function fmtDate(d) { return d; }
function dateNum(s) { return Date.parse(s + 'T00:00:00Z'); }
function valueClass(v) {
  if (v === null || v === undefined || !Number.isFinite(Number(v))) return '';
  return Number(v) > 0 ? 'pos' : Number(v) < 0 ? 'neg' : '';
}
function startIndex() {
  const years = { all: null, '5y': 5, '3y': 3, '1y': 1 }[state.range];
  if (!years) return 0;
  const end = new Date(DATA.dates[DATA.dates.length - 1] + 'T00:00:00Z');
  const threshold = new Date(end);
  threshold.setUTCFullYear(end.getUTCFullYear() - years);
  const t = threshold.getTime();
  const idx = DATA.dates.findIndex((d) => dateNum(d) >= t);
  return Math.max(0, idx);
}
function rebase(values, start, end) {
  let first = null;
  for (let i = start; i <= end; i++) {
    const v = values[i];
    if (v !== null && v !== undefined && Number.isFinite(Number(v))) { first = Number(v); break; }
  }
  if (!first) return values.map(() => null);
  return values.map((v) => (v === null || v === undefined || !Number.isFinite(Number(v)) ? null : Number(v) / first * 100));
}
function visibleSeries() {
  return DATA.series.filter((s) => !state.hidden.has(s.id)).map((s) => {
    const vals = rebase(s.values, startIndex(), DATA.dates.length - 1);
    return { ...s, vals };
  });
}
function niceTicks(min, max, count) {
  const span = max - min;
  if (!(span > 0)) return [min];
  const rough = span / Math.max(2, count - 1);
  const mag = Math.pow(10, Math.floor(Math.log10(rough)));
  const norm = rough / mag;
  const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10) * mag;
  const start = Math.ceil(min / step) * step;
  const out = [];
  for (let v = start; v <= max + step * 1e-9; v += step) out.push(v);
  return out;
}
function logTicks(min, max) {
  const out = [];
  const a = Math.floor(Math.log10(min));
  const b = Math.ceil(Math.log10(max));
  for (let e = a; e <= b; e++) {
    for (const m of [1, 2, 5]) {
      const v = m * Math.pow(10, e);
      if (v >= min * 0.999 && v <= max * 1.001) out.push(v);
    }
  }
  return out;
}
function resizeCanvas() {
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const w = Math.max(320, rect.width);
  const h = Math.max(260, rect.height);
  canvas.width = Math.round(w * dpr);
  canvas.height = Math.round(h * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { w, h };
}
function draw() {
  const start = startIndex();
  const end = DATA.dates.length - 1;
  const allSeries = DATA.series.map((s) => ({ ...s, vals: rebase(s.values, start, end) }));
  const series = allSeries.filter((s) => !state.hidden.has(s.id));
  const { w, h } = resizeCanvas();
  const muted = cssVar('muted'), soft = cssVar('soft'), grid = cssVar('grid');
  const ml = 58, mr = 22, mt = 18, mb = 28;
  const pw = w - ml - mr, ph = h - mt - mb;
  const values = [];
  series.forEach((s) => s.vals.forEach((v, i) => { if (i >= start && i <= end && v !== null) values.push(v); }));
  if (!values.length) {
    ctx.clearRect(0, 0, w, h);
    rangeLabel.textContent = DATA.dates[start] + ' → ' + DATA.dates[end] + ' · 起点 = 100';
    renderLegend(allSeries, start, end);
    return;
  }
  let min = Math.min(...values), max = Math.max(...values);
  if (!(max > min)) { max = min + 1; }
  let useLog = state.log && min > 0;
  if (useLog) {
    min = Math.max(1, min * 0.94);
    max = max * 1.06;
  } else {
    const pad = (max - min) * 0.06 || 1;
    min = Math.max(0, min - pad);
    max = max + pad;
  }
  const x = (i) => ml + (end === start ? 0 : (i - start) / (end - start) * pw);
  const y = (v) => {
    if (useLog) {
      const lo = Math.log10(min), hi = Math.log10(max);
      return mt + ph - (Math.log10(Math.max(v, min)) - lo) / (hi - lo) * ph;
    }
    return mt + ph - (v - min) / (max - min) * ph;
  };
  ctx.clearRect(0, 0, w, h);
  const ticks = useLog ? logTicks(min, max) : niceTicks(min, max, 5);
  ctx.font = '11px ui-sans-serif,system-ui,-apple-system,sans-serif';
  ctx.textBaseline = 'middle';
  ticks.forEach((v) => {
    const yy = y(v);
    if (yy < mt - 1 || yy > mt + ph + 1) return;
    ctx.strokeStyle = grid;
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(ml, yy); ctx.lineTo(ml + pw, yy); ctx.stroke();
    ctx.fillStyle = muted;
    ctx.textAlign = 'right';
    ctx.fillText(fmtIndex(v), ml - 9, yy);
  });
  const firstYear = new Date(dateNum(DATA.dates[start])).getUTCFullYear();
  const lastYear = new Date(dateNum(DATA.dates[end])).getUTCFullYear();
  ctx.textAlign = 'center';
  for (let year = firstYear; year <= lastYear; year++) {
    let t = Date.UTC(year, 0, 1);
    if (t < dateNum(DATA.dates[start])) t = dateNum(DATA.dates[start]);
    if (t > dateNum(DATA.dates[end])) continue;
    let idx = DATA.dates.findIndex((d) => dateNum(d) >= t);
    if (idx < 0) idx = end;
    const xx = x(idx);
    ctx.strokeStyle = grid;
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(xx, mt); ctx.lineTo(xx, mt + ph); ctx.stroke();
    ctx.fillStyle = soft;
    ctx.fillText(String(year), xx, h - 12);
  }
  ctx.strokeStyle = cssVar('line');
  ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(ml, mt + ph); ctx.lineTo(ml + pw, mt + ph); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(ml, mt); ctx.lineTo(ml, mt + ph); ctx.stroke();
  series.forEach((s) => {
    ctx.strokeStyle = s.color;
    ctx.lineWidth = 1.8;
    ctx.lineJoin = 'round';
    ctx.lineCap = 'round';
    ctx.beginPath();
    let started = false;
    for (let i = start; i <= end; i++) {
      const v = s.vals[i];
      if (v === null || v === undefined) { started = false; continue; }
      const xx = x(i), yy = y(v);
      if (!started) { ctx.moveTo(xx, yy); started = true; } else ctx.lineTo(xx, yy);
    }
    ctx.stroke();
  });
  if (state.hover !== null && state.hover >= start && state.hover <= end) {
    const hx = x(state.hover);
    ctx.strokeStyle = muted;
    ctx.lineWidth = 1;
    ctx.setLineDash([3, 3]);
    ctx.beginPath(); ctx.moveTo(hx, mt); ctx.lineTo(hx, mt + ph); ctx.stroke();
    ctx.setLineDash([]);
    series.forEach((s) => {
      const v = s.vals[state.hover];
      if (v === null || v === undefined) return;
      ctx.fillStyle = s.color;
      ctx.beginPath(); ctx.arc(hx, y(v), 3.2, 0, Math.PI * 2); ctx.fill();
    });
  }
  rangeLabel.textContent = DATA.dates[start] + ' → ' + DATA.dates[end] + ' · 起点 = 100';
  renderLegend(allSeries, start, end);
}
function renderLegend(series, start, end) {
  legend.innerHTML = series.map((s) => {
    const last = s.vals[end];
    const off = state.hidden.has(s.id) ? ' off' : '';
    return `<button type="button" class="legend-item${off}" data-id="${escAttr(s.id)}"><span class="line" style="color:${s.color}"></span>${escHtml(s.name)}<span class="legend-val">${fmtIndex(last)}</span></button>`;
  }).join('');
  legend.querySelectorAll('.legend-item').forEach((btn) => {
    btn.addEventListener('click', () => {
      const id = btn.getAttribute('data-id');
      if (state.hidden.has(id)) state.hidden.delete(id); else state.hidden.add(id);
      draw();
    });
  });
}
function escHtml(s) { return String(s).replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function escAttr(s) { return escHtml(s); }
function showTooltip(e) {
  const start = startIndex(), end = DATA.dates.length - 1;
  const rect = canvas.getBoundingClientRect();
  const ml = 58, mr = 22, pw = rect.width - ml - mr;
  const mx = e.clientX - rect.left;
  if (mx < ml || mx > ml + pw) { hideTooltip(); return; }
  const idx = Math.max(start, Math.min(end, Math.round(start + (mx - ml) / pw * (end - start))));
  state.hover = idx;
  draw();
  const rows = visibleSeries().map((s) => {
    const v = s.vals[idx];
    if (v === null || v === undefined) return '';
    const base = s.vals[start];
    const ret = base ? v / base - 1 : null;
    return `<div class="tt-row"><span class="dot" style="background:${s.color}"></span><span class="nm">${escHtml(s.name)}</span><span class="vv">${fmtIndex(v)}</span><span class="pp">${fmtPct(ret)}</span></div>`;
  }).join('');
  tooltip.innerHTML = `<div class="tt-date">${DATA.dates[idx]}</div>${rows}`;
  tooltip.classList.add('show');
  let left = e.clientX - rect.left + 14;
  let top = e.clientY - rect.top + 12;
  if (left + 240 > rect.width) left -= 260;
  if (top + tooltip.offsetHeight + 18 > rect.height) top = rect.height - tooltip.offsetHeight - 12;
  tooltip.style.left = Math.max(8, left) + 'px';
  tooltip.style.top = Math.max(8, top) + 'px';
}
function hideTooltip() {
  state.hover = null;
  tooltip.classList.remove('show');
  draw();
}
function setRange(range) {
  state.range = range;
  document.querySelectorAll('[data-range]').forEach((b) => b.classList.toggle('active', b.getAttribute('data-range') === range));
  draw();
}
function setScale(scale) {
  state.log = scale === 'log';
  document.querySelectorAll('[data-scale]').forEach((b) => b.classList.toggle('active', b.getAttribute('data-scale') === scale));
  draw();
}
document.querySelectorAll('[data-range]').forEach((b) => b.addEventListener('click', () => setRange(b.getAttribute('data-range'))));
document.querySelectorAll('[data-scale]').forEach((b) => b.addEventListener('click', () => setScale(b.getAttribute('data-scale'))));
document.getElementById('reset').addEventListener('click', () => {
  state.hidden.clear();
  state.range = 'all';
  state.log = true;
  document.querySelectorAll('[data-range]').forEach((b) => b.classList.toggle('active', b.getAttribute('data-range') === 'all'));
  document.querySelectorAll('[data-scale]').forEach((b) => b.classList.toggle('active', b.getAttribute('data-scale') === 'log'));
  draw();
});
canvas.addEventListener('mousemove', showTooltip);
canvas.addEventListener('mouseleave', hideTooltip);
window.addEventListener('resize', draw);
draw();
</script>
</body>
</html>
'''
