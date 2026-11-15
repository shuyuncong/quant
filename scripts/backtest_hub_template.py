"""HTML template for the unified backtest hub (no external dependencies)."""

HTML_TEMPLATE = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{
  --bg:#0a0e14; --panel:#111821; --panel2:#0d141d; --fg:#e6edf5; --muted:#8b9bb0;
  --border:#263244; --c1:#5b9cf5; --c2:#39b98a; --c3:#e0a23c; --c4:#c678dd;
  --c5:#56b6c2; --c6:#e06c75; --good:#39b98a; --bad:#e06c75; --accent:#5b9cf5;
}
@media (prefers-color-scheme: light){
  :root{--bg:#f5f7fb;--panel:#fff;--panel2:#f9fbfe;--fg:#17202b;--muted:#5c6b7c;
  --border:#dbe3ec;--c1:#2f6fd0;--c2:#16835f;--c3:#b9780e;--c4:#8e44ad;
  --c5:#0f7f8b;--c6:#b23a45;--good:#16835f;--bad:#b23a45;--accent:#2f6fd0;}
}
*{box-sizing:border-box}
[hidden]{display:none!important}
html{background:var(--bg)}
body{margin:0;background:var(--bg);color:var(--fg);font-family:system-ui,-apple-system,"Segoe UI","PingFang SC","Noto Sans SC",sans-serif;line-height:1.55;-webkit-font-smoothing:antialiased}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
.wrap{max-width:1240px;margin:0 auto;padding:0 22px}
header.top{padding:30px 0 16px;border-bottom:1px solid var(--border);background:linear-gradient(180deg,color-mix(in srgb,var(--accent) 7%,transparent),transparent 72%)}
.eyebrow{font-size:11px;letter-spacing:.16em;text-transform:uppercase;color:var(--muted);font-weight:700}
h1{font-size:30px;line-height:1.15;margin:7px 0 6px;letter-spacing:-.025em}
.lede{margin:0;color:var(--muted);font-size:14px;max-width:900px}
.meta-line{margin-top:10px;font-size:12px;color:var(--muted);display:flex;flex-wrap:wrap;gap:6px 18px}
.meta-line b{color:var(--fg);font-weight:600}
.back{display:inline-block;font-size:13px;margin-bottom:14px;color:var(--muted)}
.back:hover{color:var(--fg)}
nav.tabs,nav.subtabs{display:flex;gap:8px;overflow-x:auto;padding:14px 0 4px;scrollbar-width:thin}
nav.tabs{border-bottom:1px solid var(--border);position:sticky;top:0;z-index:20;background:color-mix(in srgb,var(--bg) 92%,transparent);backdrop-filter:blur(8px);margin-bottom:8px}
nav.subtabs{padding-top:8px}
nav button{appearance:none;border:1px solid transparent;background:transparent;color:var(--muted);padding:7px 12px;border-radius:999px;font-size:13px;font-weight:600;cursor:pointer;white-space:nowrap}
nav button:hover{color:var(--fg);background:var(--panel2)}
nav button.active{color:var(--fg);background:var(--panel);border-color:var(--border);box-shadow:0 1px 0 rgba(0,0,0,.08)}
main{padding:14px 0 4px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:16px;padding:18px 18px 16px;margin:16px 0;box-shadow:0 1px 2px rgba(0,0,0,.10)}
.card.hero{padding:22px 22px 18px}
.card h2{font-size:23px;line-height:1.25;margin:4px 0 6px;letter-spacing:-.02em}
.card h3{font-size:15px;margin:0 0 12px;letter-spacing:.01em}
.kicker{font-size:11px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--muted)}
.sub{color:var(--muted);font-size:13px;margin:0 0 8px}
.note{color:var(--muted);font-size:12.5px;margin:10px 0 0}
.card-head{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:10px}
.card-head h3{margin:0}
.pill,.badge{display:inline-block;border:1px solid var(--border);border-radius:999px;padding:2px 8px;font-size:11px;color:var(--muted);background:var(--panel2);vertical-align:1px}
.badge{margin-left:5px;padding:1px 6px;font-size:10px}
.ghost{appearance:none;border:1px solid var(--border);background:var(--panel2);color:var(--muted);border-radius:8px;padding:5px 9px;font-size:12px;cursor:pointer}
.ghost:hover{color:var(--fg)}
.chart-wrap{position:relative;margin:2px 0 4px}
#chart{width:100%;min-height:260px}
#chart svg{display:block;width:100%;height:auto;overflow:visible}
.legend{display:flex;flex-wrap:wrap;gap:8px 16px;padding:8px 2px 2px;font-size:12px;color:var(--muted)}
.legend-item{display:inline-flex;align-items:center;gap:7px;white-space:nowrap}
.legend-item b{color:var(--fg);font-weight:600;font-variant-numeric:tabular-nums}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;flex:0 0 auto;vertical-align:1px}
.c1{background:var(--c1)}.c2{background:var(--c2)}.c3{background:var(--c3)}
.c4{background:var(--c4)}.c5{background:var(--c5)}.c6{background:var(--c6)}
.chart-tip{display:none;position:absolute;z-index:30;pointer-events:none;background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:8px 10px;font-size:12px;box-shadow:0 8px 26px rgba(0,0,0,.28);min-width:170px}
.tip-date{color:var(--muted);font-size:11px;margin-bottom:5px;letter-spacing:.02em}
.tip-row{display:flex;justify-content:space-between;gap:14px;padding:2px 0;white-space:nowrap}
.tip-row span{color:var(--muted)}.tip-row b{color:var(--fg);font-variant-numeric:tabular-nums;font-weight:600}
.table-wrap{overflow-x:auto;border:1px solid var(--border);border-radius:10px}
table{width:100%;border-collapse:collapse;font-size:12.5px;min-width:720px}
th,td{padding:9px 10px;border-bottom:1px solid var(--border);text-align:right;white-space:nowrap}
th{color:var(--muted);font-size:11px;font-weight:700;letter-spacing:.03em;text-transform:uppercase;background:var(--panel2)}
th:first-child,td:first-child{text-align:left}
tbody tr:last-child td{border-bottom:0}
tbody tr:hover td{background:color-mix(in srgb,var(--accent) 5%,transparent)}
.num{font-variant-numeric:tabular-nums}
.pos{color:var(--good)}.neg{color:var(--bad)}.flat{color:var(--muted)}
.desc-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:10px}
.desc-card{background:var(--panel2);border:1px solid var(--border);border-radius:12px;padding:12px 13px}
.desc-head{display:flex;align-items:center;gap:7px;flex-wrap:wrap;margin-bottom:5px;font-size:13px}
.desc-head strong{font-weight:650}
.desc-card p{margin:0;color:var(--muted);font-size:12.5px}
.links{display:flex;flex-wrap:wrap;gap:8px}
.links a{display:inline-block;border:1px solid var(--border);border-radius:8px;padding:5px 9px;font-size:12px;background:var(--panel2)}
.links a:hover{background:var(--panel)}
.catalog-tools{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:12px}
.catalog-tools input,.catalog-tools select{background:var(--panel2);border:1px solid var(--border);color:var(--fg);border-radius:8px;padding:7px 10px;font-size:13px;min-width:180px}
.catalog-tools input{flex:1;max-width:380px}
.catalog-count{color:var(--muted);font-size:12px}
.catalog-table td:first-child{width:100px}
.catalog-table td:nth-child(2){text-align:left;max-width:360px;overflow:hidden;text-overflow:ellipsis}
.catalog-table .file-links{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}
.catalog-table .file-links a{font-size:11px;color:var(--muted)}
.catalog-table .file-links a:hover{color:var(--fg)}
.empty{color:var(--muted);padding:18px 0;font-size:13px}
footer{padding:24px 0 38px;color:var(--muted);font-size:11.5px;border-top:1px solid var(--border);margin-top:22px}
@media(max-width:760px){
  .wrap{padding:0 14px} h1{font-size:25px} .card{padding:14px;border-radius:13px}
  .card h2{font-size:20px} .lede{font-size:13px} nav button{font-size:12px;padding:6px 10px}
  table{font-size:11.5px} th,td{padding:7px 7px}
}
</style>
</head>
<body>
<header class="top"><div class="wrap">
  <a id="backLink" class="back" href="../index.html" hidden>← 返回回测中心</a>
  <div class="eyebrow">BACKTEST HUB · 2020–2026 · 20 万初始</div>
  <h1 id="pageTitle">回测中心</h1>
  <p class="lede" id="pageLede">按策略类别分页签；每个对比页签含 2020–2026 净值、核心指标、年度收益和策略说明。</p>
  <div class="meta-line">
    <span>区间 <b>2020-01-02 → 2026-10-30</b></span>
    <span>初始资金 <b>20 万</b></span>
    <span>年化 <b>按 252 个交易日折算</b></span>
    <span>2026 <b>YTD</b></span>
  </div>
</div></header>
<div class="wrap">
  <nav id="categoryTabs" class="tabs" aria-label="策略分类"></nav>
  <nav id="comparisonTabs" class="subtabs" aria-label="对比回测"></nav>
  <main>
    <section id="comparisonView"></section>
    <section id="catalogView" hidden></section>
  </main>
  <footer>研究产物，不构成投资建议。曲线为回测净值，非实盘；不同策略的股票池、执行时点和成本口径不同。生成时间：__GENERATED__。</footer>
</div>
<script>
const DATA = __DATA__;
const $ = (id) => document.getElementById(id);
const state = { category: null, comparison: null, log: false, current: null, daily: false };

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
}
function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue("--" + name).trim() || "#5b9cf5";
}
function fmtPct(v) {
  if (v === null || v === undefined || !Number.isFinite(Number(v))) return "—";
  const x = Number(v) * 100;
  return (x > 0 ? "+" : "") + x.toFixed(1) + "%";
}
function fmtNum(v) {
  if (v === null || v === undefined || !Number.isFinite(Number(v))) return "—";
  return Number(v).toFixed(2);
}
function fmtMoney(v) {
  if (v === null || v === undefined || !Number.isFinite(Number(v))) return "—";
  const x = Number(v);
  if (Math.abs(x) >= 1e8) return (x / 1e8).toFixed(2).replace(/\.00$/, "") + "亿";
  if (Math.abs(x) >= 1e4) return (x / 1e4).toFixed(1).replace(/\.0$/, "") + "万";
  return Math.round(x).toLocaleString("zh-CN");
}
function fmtInt(v) {
  if (v === null || v === undefined || !Number.isFinite(Number(v))) return "—";
  return Math.round(Number(v)).toLocaleString("zh-CN");
}
function valueClass(v) {
  if (v === null || v === undefined || !Number.isFinite(Number(v))) return "flat";
  return Number(v) > 0 ? "pos" : Number(v) < 0 ? "neg" : "flat";
}
function dateNum(s) { return Date.parse(s + "T00:00:00Z"); }
function categoryName(id) {
  const found = (DATA.categories || []).find((c) => c.id === id);
  return found ? found.name : "其他";
}

function init() {
  if (DATA.mode === "single") {
    $("backLink").hidden = false;
    $("pageTitle").textContent = "回测详情";
    $("pageLede").textContent = "该对比的独立页面；数据与总览一致，曲线为日频。";
    $("categoryTabs").hidden = true;
    $("comparisonTabs").hidden = true;
    renderComparison(DATA.comparison, true);
    return;
  }
  const meta = document.querySelector(".meta-line");
  if (meta && !meta.querySelector('a[href="overview.html"]')) {
    meta.insertAdjacentHTML("beforeend", '<span><a href="overview.html">← 返回总览</a></span>');
  }
  renderCategoryTabs();
  route();
  window.addEventListener("hashchange", route);
}

function route() {
  const raw = location.hash.replace(/^#/, "");
  if (raw === "catalog") { selectCatalog(); return; }
  const [catId, compId] = raw.split("/");
  const category = (DATA.categories || []).find((c) => c.id === catId);
  selectCategory(category ? category.id : DATA.categories[0].id, compId);
}

function renderCategoryTabs() {
  const nav = $("categoryTabs");
  const items = (DATA.categories || []).map((c) => `<button type="button" data-cat="${esc(c.id)}">${esc(c.name)}</button>`);
  items.push(`<button type="button" data-cat="catalog">全部目录</button>`);
  nav.innerHTML = items.join("");
  nav.querySelectorAll("button").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = btn.getAttribute("data-cat");
      if (id === "catalog") selectCatalog(); else selectCategory(id);
    });
  });
}

function setActiveCategory(id) {
  document.querySelectorAll("#categoryTabs button").forEach((btn) => {
    btn.classList.toggle("active", btn.getAttribute("data-cat") === id);
  });
}

function selectCategory(id, preferred) {
  if (id === "catalog") { selectCatalog(); return; }
  const category = (DATA.categories || []).find((c) => c.id === id) || DATA.categories[0];
  state.category = category.id;
  setActiveCategory(category.id);
  $("catalogView").hidden = true;
  $("comparisonView").hidden = false;
  $("comparisonTabs").hidden = false;
  const comps = (DATA.comparisons || []).filter((c) => c.category === category.id);
  $("comparisonTabs").innerHTML = comps.map((c) => `<button type="button" data-comp="${esc(c.id)}">${esc(c.title)}</button>`).join("");
  $("comparisonTabs").querySelectorAll("button").forEach((btn) => {
    btn.addEventListener("click", () => selectComparison(btn.getAttribute("data-comp")));
  });
  const chosen = comps.find((c) => c.id === preferred) || comps[0];
  if (chosen) selectComparison(chosen.id);
}

function selectComparison(id) {
  const comp = (DATA.comparisons || []).find((c) => c.id === id);
  if (!comp) return;
  state.comparison = comp.id;
  state.category = comp.category;
  setActiveCategory(comp.category);
  document.querySelectorAll("#comparisonTabs button").forEach((btn) => {
    btn.classList.toggle("active", btn.getAttribute("data-comp") === comp.id);
  });
  renderComparison(comp, false);
  const target = "#" + comp.category + "/" + comp.id;
  if (location.hash !== target) history.replaceState(null, "", target);
}

function selectCatalog() {
  state.category = "catalog";
  setActiveCategory("catalog");
  $("comparisonTabs").hidden = true;
  $("comparisonView").hidden = true;
  $("catalogView").hidden = false;
  renderCatalog();
  if (location.hash !== "#catalog") history.replaceState(null, "", "#catalog");
}

function renderComparison(comp, daily) {
  state.current = comp;
  state.daily = daily;
  const basisNote = comp.metric_note || "";
  const view = $("comparisonView");
  const detailLink = DATA.mode === "hub"
    ? `<a class="pill" href="./${esc(comp.id)}/index.html">打开独立页面 ↗</a>`
    : "";
  const rows = comp.strategies.map((s) => {
    const m = s.metrics || {};
    const badge = s.basis === "daily" ? `<span class="badge" title="日频口径">日频</span>` : "";
    return `<tr>
      <td><span class="dot ${esc(s.color)}"></span>${esc(s.name)} ${badge}</td>
      <td class="num ${valueClass(m.total_return)}">${fmtPct(m.total_return)}</td>
      <td class="num ${valueClass(m.annualized_return)}">${fmtPct(m.annualized_return)}</td>
      <td class="num neg">${fmtPct(m.max_drawdown)}</td>
      <td class="num">${fmtNum(m.sharpe)}</td>
      <td class="num">${fmtPct(m.win_rate)}</td>
      <td class="num">${fmtNum(m.profit_factor)}</td>
      <td class="num">${fmtInt(m.trades)}</td>
      <td class="num">${fmtMoney(m.final_equity)}</td>
    </tr>`;
  }).join("");
  const years = ["2020", "2021", "2022", "2023", "2024", "2025", "2026"];
  const annualHead = years.map((y) => `<th>${y}${y === "2026" ? " YTD" : ""}</th>`).join("");
  const annualRows = comp.strategies.map((s) => {
    const cells = years.map((y) => `<td class="num ${valueClass(s.annual && s.annual[y])}">${fmtPct(s.annual && s.annual[y])}</td>`).join("");
    return `<tr><td><span class="dot ${esc(s.color)}"></span>${esc(s.name)}</td>${cells}</tr>`;
  }).join("");
  const annualDrawdownRows = comp.strategies.map((s) => {
    const cells = years.map((y) => `<td class="num neg">${fmtPct(s.annual_drawdown && s.annual_drawdown[y])}</td>`).join("");
    return `<tr><td><span class="dot ${esc(s.color)}"></span>${esc(s.name)}</td>${cells}</tr>`;
  }).join("");
  const descs = comp.strategies.map((s) => `
    <div class="desc-card">
      <div class="desc-head"><span class="dot ${esc(s.color)}"></span><strong>${esc(s.name)}</strong>
      <span class="badge">${s.basis === "daily" ? "日频口径" : "交易口径"}</span></div>
      <p>${esc(s.description)}</p>
    </div>`).join("");
  const prefix = DATA.mode === "hub" ? "../" : "../../";
  const local = DATA.mode === "hub" ? `./${comp.id}/` : "./";
  const localLinks = ["curves.csv", "metrics.csv", "annual.csv", "data.json"]
    .map((name) => `<a href="${local}${name}" target="_blank" rel="noopener">${name}</a>`).join("");
  const sourceLinks = (comp.source_files || []).map((f) => `<a href="${prefix}${encodeURI(f)}" target="_blank" rel="noopener">${esc(f.split("/").pop())}</a>`).join("");
  const links = localLinks + sourceLinks;
  view.innerHTML = `
    <section class="card hero">
      <div class="kicker">${esc(categoryName(comp.category))}</div>
      <h2>${esc(comp.title)}</h2>
      <p class="sub">${esc(comp.subtitle)}</p>
      <p class="note">${esc(comp.note)}</p>
      <div style="margin-top:10px">${detailLink}</div>
    </section>
    <section class="card">
      <div class="card-head"><h3>净值曲线 · 2020-01-02 = 20 万</h3>
        <div style="display:flex;gap:8px;align-items:center"><span class="pill">${comp.strategies.length} 条曲线</span>
        <button id="logToggle" class="ghost" type="button">对数坐标</button></div></div>
      <div class="chart-wrap"><div id="chart"></div><div id="chartTip" class="chart-tip"></div></div>
      <div id="legend" class="legend"></div>
      <p class="note">${daily ? "日频曲线。" : "周频抽样曲线；独立页面为日频。"} 2026 截至 10-30；线性坐标下亏损曲线可能被压缩，可切换对数坐标。</p>
    </section>
    <section class="card">
      <h3>核心指标 · 2020–2026</h3>
      <div class="table-wrap"><table>
        <thead><tr><th>策略</th><th>总收益</th><th>年化</th><th>最大回撤</th><th>夏普</th><th>胜率</th><th>盈亏比</th><th>交易数</th><th>期末权益</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>
      <p class="note">${esc(basisNote)} 年化按 252 个交易日折算；2026 为 YTD。</p>
    </section>
    <section class="card">
      <h3>年度收益</h3>
      <div class="table-wrap"><table>
        <thead><tr><th>策略</th>${annualHead}</tr></thead>
        <tbody>${annualRows}</tbody>
      </table></div>
      <p class="note">2020–2025 为完整年度；2026 截至 10-30。</p>
      <h3 style="margin-top:18px">年度最大回撤</h3>
      <div class="table-wrap"><table>
        <thead><tr><th>策略</th>${annualHead}</tr></thead>
        <tbody>${annualDrawdownRows}</tbody>
      </table></div>
      <p class="note">年度回撤以当年年初净值为起点；全期最大回撤见上方核心指标。</p>
    </section>
    <section class="card">
      <h3>策略说明</h3>
      <div class="desc-grid">${descs}</div>
    </section>
    <section class="card">
      <h3>原始数据</h3>
      <div class="links">${links || '<span class="note">没有登记的原始文件。</span>'}</div>
      <p class="note">原始目录：${esc(comp.source_dir)}。页面只做标准化展示，不改变原始回测产物。</p>
    </section>`;
  const toggle = $("logToggle");
  if (toggle) toggle.addEventListener("click", () => { state.log = !state.log; renderChart(state.current, state.daily); });
  renderChart(comp, daily);
}

function niceTicks(min, max, count) {
  const span = max - min;
  if (!(span > 0)) return [min];
  const rough = span / Math.max(2, count - 1);
  const mag = Math.pow(10, Math.floor(Math.log10(rough)));
  const norm = rough / mag;
  const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10) * mag;
  const start = Math.ceil(min / step) * step;
  const ticks = [];
  for (let v = start; v <= max + step * 1e-9; v += step) ticks.push(v);
  return ticks;
}

function logTicks(min, max) {
  const ticks = [];
  const start = Math.floor(Math.log10(min));
  const end = Math.ceil(Math.log10(max));
  for (let exp = start; exp <= end; exp++) {
    for (const mult of [1, 2, 5]) {
      const value = mult * Math.pow(10, exp);
      if (value >= min * 0.999 && value <= max * 1.001) ticks.push(value);
    }
  }
  return ticks;
}

function renderChart(comp, daily) {
  const host = $("chart");
  if (!host) return;
  const series = (comp.strategies || []).map((s, i) => ({
    ...s,
    pts: daily ? (s.curve || []) : (s.curve_weekly || []),
    stroke: cssVar(s.color)
  })).filter((s) => s.pts && s.pts.length);
  if (!series.length) { host.innerHTML = '<p class="empty">没有曲线数据。</p>'; return; }
  const allDates = [], allValues = [];
  series.forEach((s) => s.pts.forEach((p) => { allDates.push(dateNum(p[0])); allValues.push(Number(p[1])); }));
  let xmin = Math.min(...allDates), xmax = Math.max(...allDates);
  let ymin = Math.min(...allValues), ymax = Math.max(...allValues);
  if (xmax === xmin) xmax = xmin + 1;
  let log = state.log && ymin > 0;
  if (log) {
    ymin = Math.max(1, ymin * 0.94);
    ymax = ymax * 1.06;
  } else {
    const pad = (ymax - ymin) * 0.06 || Math.max(1000, ymax * 0.02);
    ymin = Math.max(0, ymin - pad);
    ymax = ymax + pad;
  }
  if (ymax <= ymin) ymax = ymin + 1;
  const W = 1000, H = 430, ml = 82, mr = 118, mt = 22, mb = 44;
  const pw = W - ml - mr, ph = H - mt - mb;
  const x = (d) => ml + ((dateNum(d) - xmin) / (xmax - xmin)) * pw;
  const yLog = (v) => mt + ph - ((Math.log10(v) - Math.log10(ymin)) / (Math.log10(ymax) - Math.log10(ymin))) * ph;
  const yLin = (v) => mt + ph - ((v - ymin) / (ymax - ymin)) * ph;
  const y = (v) => (log ? yLog(Math.max(v, ymin)) : yLin(v));
  const ticks = log ? logTicks(ymin, ymax) : niceTicks(ymin, ymax, 5);
  const border = cssVar("border"), muted = cssVar("muted"), fg = cssVar("fg");
  let svg = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(comp.title)} 净值曲线">`;
  ticks.forEach((v) => {
    const yy = y(v);
    if (yy < mt - 1 || yy > mt + ph + 1) return;
    svg += `<line x1="${ml}" y1="${yy.toFixed(2)}" x2="${ml + pw}" y2="${yy.toFixed(2)}" stroke="${border}" opacity="0.55"/>`;
    svg += `<text x="${ml - 10}" y="${(yy + 4).toFixed(2)}" text-anchor="end" font-size="12" fill="${muted}">${fmtMoney(v)}</text>`;
  });
  const firstYear = new Date(xmin).getUTCFullYear();
  const lastYear = new Date(xmax).getUTCFullYear();
  for (let year = firstYear; year <= lastYear; year++) {
    let t = Date.UTC(year, 0, 1);
    if (t < xmin) t = xmin;
    if (t > xmax) continue;
    const xx = ml + ((t - xmin) / (xmax - xmin)) * pw;
    svg += `<line x1="${xx.toFixed(2)}" y1="${mt}" x2="${xx.toFixed(2)}" y2="${mt + ph}" stroke="${border}" opacity="0.28"/>`;
    svg += `<text x="${xx.toFixed(2)}" y="${H - 14}" text-anchor="middle" font-size="12" fill="${muted}">${year}</text>`;
  }
  svg += `<line x1="${ml}" y1="${mt + ph}" x2="${ml + pw}" y2="${mt + ph}" stroke="${border}"/>`;
  svg += `<line x1="${ml}" y1="${mt}" x2="${ml}" y2="${mt + ph}" stroke="${border}"/>`;
  series.forEach((s) => {
    const points = s.pts.map((p) => `${x(p[0]).toFixed(2)},${y(p[1]).toFixed(2)}`).join(" ");
    svg += `<polyline points="${points}" fill="none" stroke="${s.stroke}" stroke-width="1.9" stroke-linejoin="round" stroke-linecap="round"/>`;
  });
  const labels = series.map((s) => {
    const last = s.pts[s.pts.length - 1];
    return { name: s.name, value: Number(last[1]), x: x(last[0]), y: y(last[1]), stroke: s.stroke };
  }).sort((a, b) => a.y - b.y);
  let lastLabelY = -999;
  labels.forEach((label) => {
    let ly = Math.max(mt + 10, Math.min(mt + ph - 4, label.y));
    if (ly - lastLabelY < 14) ly = lastLabelY + 14;
    lastLabelY = ly;
    const lx = Math.min(W - 8, label.x + 7);
    svg += `<circle cx="${label.x.toFixed(2)}" cy="${label.y.toFixed(2)}" r="3.2" fill="${label.stroke}"/>`;
    svg += `<text x="${lx.toFixed(2)}" y="${(ly + 4).toFixed(2)}" font-size="11" fill="${fg}">${esc(label.name)} ${fmtMoney(label.value)}</text>`;
  });
  svg += `<g id="hoverGroup" style="display:none"><line id="hoverLine" y1="${mt}" y2="${mt + ph}" stroke="${muted}" stroke-dasharray="3 3"/><g id="hoverDots"></g></g>`;
  svg += `<rect id="hoverRect" x="${ml}" y="${mt}" width="${pw}" height="${ph}" fill="transparent"/>`;
  svg += `</svg>`;
  host.innerHTML = svg;
  const legend = $("legend");
  if (legend) legend.innerHTML = series.map((s) => `<span class="legend-item"><i class="dot ${esc(s.color)}"></i>${esc(s.name)} <b>${fmtMoney(s.pts[s.pts.length - 1][1])}</b></span>`).join("");
  const toggle = $("logToggle");
  if (toggle) {
    toggle.textContent = log ? "线性坐标" : "对数坐标";
    toggle.disabled = !(Math.min(...allValues) > 0);
    toggle.title = Math.min(...allValues) > 0 ? "" : "存在非正净值，无法使用对数坐标";
  }
  attachHover(host, series, { W, H, ml, mr, mt, mb, pw, ph, xmin, xmax, x, y, log });
}

function attachHover(host, series, geo) {
  const svg = host.querySelector("svg");
  if (!svg) return;
  const rect = svg.querySelector("#hoverRect");
  const group = svg.querySelector("#hoverGroup");
  const line = svg.querySelector("#hoverLine");
  const dots = svg.querySelector("#hoverDots");
  const tip = $("chartTip");
  if (!rect || !group || !line || !dots || !tip) return;
  const base = series.reduce((a, b) => (a.pts.length >= b.pts.length ? a : b));
  const baseDates = base.pts.map((p) => p[0]);
  const maps = series.map((s) => new Map(s.pts.map((p) => [p[0], Number(p[1])])));
  function hide() { group.style.display = "none"; tip.style.display = "none"; }
  function move(event) {
    const svgRect = svg.getBoundingClientRect();
    const px = ((event.clientX - svgRect.left) / svgRect.width) * geo.W;
    if (px < geo.ml || px > geo.ml + geo.pw) { hide(); return; }
    const t = geo.xmin + ((px - geo.ml) / geo.pw) * (geo.xmax - geo.xmin);
    let lo = 0, hi = baseDates.length - 1;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (dateNum(baseDates[mid]) < t) lo = mid + 1; else hi = mid;
    }
    let idx = lo;
    if (idx > 0 && Math.abs(dateNum(baseDates[idx - 1]) - t) < Math.abs(dateNum(baseDates[idx]) - t)) idx--;
    const date = baseDates[idx];
    const xx = geo.x(date);
    line.setAttribute("x1", xx); line.setAttribute("x2", xx);
    let dotHtml = "";
    const rows = series.map((s, i) => {
      const value = maps[i].get(date);
      if (value === undefined) return "";
      dotHtml += `<circle cx="${xx.toFixed(2)}" cy="${geo.y(value).toFixed(2)}" r="3" fill="${s.stroke}"/>`;
      return `<div class="tip-row"><span><i class="dot ${esc(s.color)}"></i>${esc(s.name)}</span><b>${fmtMoney(value)}</b></div>`;
    }).join("");
    dots.innerHTML = dotHtml;
    group.style.display = "";
    tip.innerHTML = `<div class="tip-date">${esc(date)}</div>${rows}`;
    tip.style.display = "block";
    const hostRect = host.getBoundingClientRect();
    let left = event.clientX - hostRect.left + 14;
    let top = event.clientY - hostRect.top + 12;
    if (left + 250 > hostRect.width) left -= 265;
    if (top + tip.offsetHeight + 18 > hostRect.height) top = hostRect.height - tip.offsetHeight - 12;
    tip.style.left = Math.max(8, left) + "px";
    tip.style.top = Math.max(8, top) + "px";
  }
  rect.addEventListener("mousemove", move);
  rect.addEventListener("mouseleave", hide);
}

function renderCatalog() {
  const view = $("catalogView");
  if (!view) return;
  const categories = Array.from(new Set((DATA.catalog || []).map((r) => r.category))).sort();
  view.innerHTML = `
    <section class="card hero">
      <div class="kicker">ALL BACKTESTS</div>
      <h2>全部回测目录</h2>
      <p class="sub">共 ${DATA.catalog.length} 个结果目录；有标准化曲线和指标的进入上面的分类页签，其余保留原始文件与独立详情页。</p>
      <p class="note">“详情”是每个目录的独立 HTML；“对比页”是该目录作为富对比数据源时的完整页面。文件链接直接打开原始产物。</p>
    </section>
    <section class="card">
      <div class="catalog-tools">
        <input id="catalogSearch" type="search" placeholder="搜索目录、类别或文件…" autocomplete="off">
        <select id="catalogCategory"><option value="">全部类别</option>${categories.map((c) => `<option value="${esc(c)}">${esc(c)}</option>`).join("")}</select>
        <span class="catalog-count" id="catalogCount"></span>
      </div>
      <div class="table-wrap"><table class="catalog-table">
        <thead><tr><th>日期</th><th>目录</th><th>类别</th><th>原始文件</th><th>页面</th></tr></thead>
        <tbody id="catalogBody"></tbody>
      </table></div>
    </section>`;
  const search = $("catalogSearch"), category = $("catalogCategory");
  const draw = () => {
    const q = (search.value || "").trim().toLowerCase();
    const cat = category.value;
    const rows = (DATA.catalog || []).filter((r) => {
      const haystack = (r.dir + " " + r.category + " " + (r.files || []).map((f) => f.label).join(" ")).toLowerCase();
      return (!q || haystack.includes(q)) && (!cat || r.category === cat);
    });
    $("catalogBody").innerHTML = rows.map((r) => {
      const files = (r.files || []).slice(0, 5).map((f) => `<a href="../${encodeURI(f.path)}" target="_blank" rel="noopener">${esc(f.label)}</a>`).join("");
      const rich = r.rich_id ? `<a href="./${esc(r.rich_id)}/index.html">对比页</a>` : "";
      return `<tr>
        <td class="num">${esc(r.date)}</td>
        <td title="${esc(r.dir)}"><a href="${esc(r.page)}">${esc(r.dir)}</a></td>
        <td>${esc(r.category)}</td>
        <td><div class="file-links">${files || '<span class="note">—</span>'}</div></td>
        <td><div class="file-links"><a href="${esc(r.page)}">详情</a>${rich}</div></td>
      </tr>`;
    }).join("");
    $("catalogCount").textContent = `${rows.length} / ${DATA.catalog.length} 个目录`;
  };
  search.addEventListener("input", draw);
  category.addEventListener("change", draw);
  draw();
}

init();
</script>
</body>
</html>
'''

MINI_TEMPLATE = r'''<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--bg:#0a0e14;--panel:#111821;--fg:#e6edf5;--muted:#8b9bb0;--border:#263244;--accent:#5b9cf5}
@media (prefers-color-scheme: light){:root{--bg:#f5f7fb;--panel:#fff;--fg:#17202b;--muted:#5c6b7c;--border:#dbe3ec;--accent:#2f6fd0}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font-family:system-ui,-apple-system,"Segoe UI","PingFang SC",sans-serif;line-height:1.55}
.wrap{max-width:900px;margin:0 auto;padding:34px 20px 50px}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
.back{font-size:13px;color:var(--muted);display:inline-block;margin-bottom:18px}
.eyebrow{font-size:11px;letter-spacing:.15em;text-transform:uppercase;color:var(--muted);font-weight:700}
h1{font-size:26px;margin:6px 0 4px;letter-spacing:-.02em;word-break:break-all}
.sub{color:var(--muted);font-size:13px;margin:0 0 18px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:18px;margin:14px 0}
h2{font-size:15px;margin:0 0 10px}
ul{margin:0;padding-left:18px}li{margin:5px 0}
.note{color:var(--muted);font-size:12.5px}
.tag{display:inline-block;border:1px solid var(--border);border-radius:999px;padding:2px 8px;font-size:11px;color:var(--muted);margin-right:6px}
</style>
</head>
<body><main class="wrap">
<a class="back" href="../../index.html">← 返回回测中心</a>
<div class="eyebrow">CATALOG DETAIL</div>
<h1>__TITLE__</h1>
<p class="sub"><span class="tag">__CATEGORY__</span><span class="tag">__DATE__</span></p>
<div class="card"><h2>原始文件</h2><ul>__FILES__</ul></div>
__RICH__
<p class="note">该目录没有标准化 2020-2026 曲线；这里保留原始产物链接。若它是富对比数据源，上方“对比页”会展示完整曲线、指标和策略说明。</p>
</main></body></html>
'''
