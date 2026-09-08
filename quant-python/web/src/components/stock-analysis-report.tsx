"use client";

import { useEffect, useState } from "react";
import { Activity, ArrowDownRight, ArrowUpRight, ChartCandlestick, CircleCheck, CircleHelp, Clock3, Layers3, ShieldAlert, TriangleAlert } from "lucide-react";
import { StockPriceChart } from "@/components/stock-price-chart";
import { Button } from "@/components/ui/button";
import {
  type DataResult, type DataSource, type DataTimeframe, type IndicatorValue,
  confirmationLabel, executionLabel, finiteNumber, formatPrice, formatRatio, formatTime, orderedTimeframes,
  positionRisk, priceChange, primaryTimeframe, signalLabel, structureLabel, timeframeLabel, timeframeSignals,
} from "@/lib/stock-report";

function Section({ id, number, title, aside, children }: { id?: string; number: string; title: string; aside?: React.ReactNode; children: React.ReactNode }) {
  return <section id={id} className="report-panel">
    <div className="report-section-heading"><h3><span>{number}</span>{title}</h3>{aside}</div>
    {children}
  </section>;
}

function StateCard({ label, value, description, tone = "neutral", icon: Icon }: {
  label: string; value: string; description: string; tone?: "neutral" | "positive" | "warning" | "danger";
  icon: typeof Activity;
}) {
  return <div className={`report-state report-state-${tone}`}>
    <div className="flex items-center justify-between gap-2 text-xs text-muted-foreground"><span>{label}</span><Icon className="size-4" aria-hidden="true" /></div>
    <p className="mt-3 text-lg font-semibold tracking-tight">{value}</p>
    <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{description}</p>
  </div>;
}

function Condition({ label, value, detail }: { label: string; value: IndicatorValue | undefined; detail: string }) {
  const known = typeof value === "boolean";
  const Icon = !known ? CircleHelp : value ? CircleCheck : Clock3;
  return <div className="flex items-start gap-3 py-3">
    <Icon className={`mt-0.5 size-4 shrink-0 ${!known ? "text-muted-foreground" : value ? "text-sky-700 dark:text-sky-400" : "text-amber-700 dark:text-amber-400"}`} aria-hidden="true" />
    <div className="min-w-0 flex-1"><div className="flex flex-wrap items-center justify-between gap-2 text-sm"><span>{label}</span><span className="text-xs text-muted-foreground">{!known ? "数据未提供" : value ? "已满足" : "未满足"}</span></div><p className="mt-1 text-xs text-muted-foreground">{detail}</p></div>
  </div>;
}

function TimeframeTable({ timeframes, selected, onSelect }: { timeframes: DataTimeframe[]; selected: string; onSelect: (key: string) => void }) {
  return <div className="overflow-x-auto">
    <table className="report-table min-w-[760px]">
      <caption className="sr-only">多周期结构对照，选择周期可切换上方图表。分数为引擎评分，不是胜率。</caption>
      <thead><tr><th scope="col">周期</th><th scope="col">价格位置</th><th scope="col">中枢 [ZD, ZG]</th><th scope="col">收盘</th><th scope="col">买 / 卖评分</th><th scope="col">最新信号与确认</th><th scope="col">数据截至</th></tr></thead>
      <tbody>{timeframes.map((tf) => {
        const signal = timeframeSignals(tf)[0];
        return <tr key={tf.timeframe} data-active={selected === tf.timeframe}>
          <th scope="row"><button type="button" aria-pressed={selected === tf.timeframe} onClick={() => onSelect(tf.timeframe)} className="report-period-link">{timeframeLabel(tf.timeframe)}</button></th>
          <td><span className={tf.status !== "ok" ? "text-amber-700 dark:text-amber-400" : ""}>{structureLabel(tf)}</span></td>
          <td className="tabular-nums">{tf.latest_center ? `${formatPrice(tf.latest_center.zd)} — ${formatPrice(tf.latest_center.zg)}` : "未提供"}</td>
          <td className="tabular-nums">{formatPrice(tf.latest_price)}</td>
          <td className="tabular-nums">{tf.status === "ok" ? `${formatPrice(tf.buy_score, 0)} / ${formatPrice(tf.sell_score, 0)}` : "—"}</td>
          <td className="max-w-64 whitespace-normal">{signal ? <><span>{signalLabel(signal.signal_type)}</span><span className="mt-1 block text-xs text-muted-foreground">{executionLabel(signal)} · {formatTime(signal.confirmed_at)}</span></> : <span className="text-muted-foreground">{tf.status === "ok" ? "无近期信号记录" : "数据不足"}</span>}</td>
          <td className="text-xs text-muted-foreground">{formatTime(tf.latest_time)}</td>
        </tr>;
      })}</tbody>
    </table>
  </div>;
}

function StockDetail({ result, source }: { result: DataResult; source: DataSource }) {
  const primary = primaryTimeframe(result);
  const timeframes = orderedTimeframes(result.timeframes);
  const [selectedPeriod, setSelectedPeriod] = useState(primary?.timeframe ?? "");
  const selected = timeframes.find((tf) => tf.timeframe === selectedPeriod) ?? primary;
  const change = primary ? priceChange(primary) : null;
  const daily = timeframes.find((tf) => tf.timeframe === "1d");
  const risk = positionRisk(daily);
  const indicators = selected?.indicators ?? {};
  const market = source.market_context;
  const regime = typeof market?.regime === "string" ? market.regime : null;
  const regimeName = regime ? ({ bull: "牛市", range: "震荡", bear: "熊市", strong: "强势", weak: "弱势" } as Record<string, string>)[regime] ?? regime : "环境未提供";
  const entryAllowed = typeof market?.allows_entries === "boolean" ? market.allows_entries : null;
  const confirmation = confirmationLabel(primary);
  const sourceWarnings = timeframes.filter((tf) => tf.status !== "ok" || tf.indicators.history_complete === false || tf.error || tf.indicators.source_warning || tf.indicators.analysis_warning);
  const selectedSignals = selected ? timeframeSignals(selected) : [];
  return <article className="space-y-4">
    <header className="report-panel report-stock-header">
      <div className="flex flex-wrap items-start justify-between gap-5">
        <div className="min-w-0">
          <div className="mb-2 flex items-center gap-2 text-xs font-medium text-sky-700 dark:text-sky-400"><span className="h-1.5 w-1.5 rounded-full bg-current" />个股研究报告 <span className="font-normal text-muted-foreground">/ 历史分析快照</span></div>
          <h2 className="break-words text-2xl font-semibold tracking-tight">{result.name || result.symbol || "未命名股票"}<span className="ml-3 inline-block font-mono text-sm font-normal text-muted-foreground">{result.symbol}</span></h2>
          <p className="mt-2 text-xs text-muted-foreground">分析于 {formatTime(result.analyzed_at ?? source.analyzed_at)} · 北京时间</p>
        </div>
        <div className="sm:text-right">
          <div className="flex items-baseline gap-3"><span className="text-3xl font-semibold tabular-nums tracking-tight">{formatPrice(primary?.latest_price)}</span><span className={`inline-flex items-center gap-1 text-sm font-medium tabular-nums ${change === null || change === 0 ? "text-muted-foreground" : change > 0 ? "report-up" : "report-down"}`}>{change !== null && change !== 0 && (change > 0 ? <ArrowUpRight className="size-4" aria-hidden="true" /> : <ArrowDownRight className="size-4" aria-hidden="true" />)}{formatRatio(change)}</span></div>
          <p className="mt-1 text-xs text-muted-foreground">{primary ? `${timeframeLabel(primary.timeframe)}收盘 · 较上一根${timeframeLabel(primary.timeframe)} K 线` : "价格未提供"}</p>
          <p className="mt-1 text-xs text-muted-foreground">行情截至 {formatTime(primary?.latest_time ?? null)}</p>
        </div>
      </div>
      <div className="report-summary-line"><ChartCandlestick className="size-4 shrink-0" aria-hidden="true" /><p>
        {primary ? `${timeframeLabel(primary.timeframe)}${structureLabel(primary)}；金叉回踩状态：${confirmation}。` : "该股票没有可用周期数据。"}
        {entryAllowed === false ? " 当前报告的市场门禁不允许开仓。" : " 请结合下方周期结构与风险依据阅读。"}
      </p></div>
    </header>

    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
      <StateCard label="市场环境" value={regimeName} description={entryAllowed === null ? "本报告未提供完整市场门禁信息" : entryAllowed ? "报告门禁允许开仓 · 不代表信号可执行" : "报告门禁不允许开仓"} icon={Activity} tone={entryAllowed === false ? "danger" : "neutral"} />
      <StateCard label={primary ? `${timeframeLabel(primary.timeframe)}结构` : "价格结构"} value={primary ? structureLabel(primary) : "数据不足"} description={primary?.latest_center ? `ZD ${formatPrice(primary.latest_center.zd)} / ZG ${formatPrice(primary.latest_center.zg)}` : "等待有效中枢与价格数据"} icon={Layers3} tone="neutral" />
      <StateCard label={`${primary ? timeframeLabel(primary.timeframe) : ""}金叉回踩状态`} value={confirmation} description="历史确认与本根就绪分别记录，不等同于买入指令" icon={Clock3} tone={confirmation === "本根确认就绪" ? "positive" : confirmation === "等待回踩确认" ? "warning" : "neutral"} />
      <StateCard label="日线位置风险" value={risk === "triggered" ? "已触发风险项" : risk === "clear" ? "已检测项未触发" : "数据不足"} description={risk === "unavailable" ? "长期均线或风险检测必需数据不完整" : "检测范围：高位偏离、高位放量；不代表总体无风险"} icon={ShieldAlert} tone={risk === "triggered" ? "danger" : "neutral"} />
    </div>

    {sourceWarnings.length > 0 && <details className="report-notice" open>
      <summary className="cursor-pointer font-medium">数据提示 · {sourceWarnings.length} 个周期需要留意</summary>
      <ul className="mt-2 space-y-1 text-xs">{sourceWarnings.map((tf) => <li key={tf.timeframe} className="break-words">{timeframeLabel(tf.timeframe)}：{tf.error || String(tf.indicators.analysis_warning || tf.indicators.source_warning || (tf.indicators.history_complete === false ? "历史数据不完整" : "数据不足或分析失败"))}</li>)}</ul>
    </details>}

    <Section number="01" title="价格走势与结构" aside={<span className="text-xs text-muted-foreground">报告内行情 · 非实时更新</span>}>
      {selected ? <>
        <div className="mb-4 flex flex-wrap gap-1.5" role="group" aria-label="图表周期">{timeframes.map((tf) => <button key={tf.timeframe} type="button" className="report-period" aria-pressed={selected.timeframe === tf.timeframe} onClick={() => setSelectedPeriod(tf.timeframe)}>{timeframeLabel(tf.timeframe)}</button>)}</div>
        <StockPriceChart key={selected.timeframe} timeframe={selected} />
      </> : <p className="py-8 text-center text-sm text-muted-foreground">报告未包含周期行情。</p>}
    </Section>

    <Section number="02" title="多周期结构对照" aside={<span className="text-xs text-muted-foreground">评分为引擎原始分数，不是胜率</span>}>
      {timeframes.length ? <TimeframeTable timeframes={timeframes} selected={selectedPeriod} onSelect={setSelectedPeriod} /> : <p className="text-sm text-muted-foreground">暂无周期数据。</p>}
    </Section>

    <div className="grid items-start gap-4 xl:grid-cols-2">
      <Section number="03" title={`${selected ? timeframeLabel(selected.timeframe) : ""}条件与信号`}>
        <div className="divide-y">
          <Condition label="站上 MA60" value={finiteNumber(indicators.ma60) !== null ? indicators.above_ma60 : undefined} detail={`MA60 ${formatPrice(indicators.ma60)} · 仅反映当前价格位置`} />
          <Condition label="金叉后回踩触碰" value={indicators.golden_cross_pullback_touched} detail="引擎定义的金叉 K 线回踩条件" />
          <Condition label="本根入场就绪条件" value={indicators.golden_cross_entry_ready} detail={`${confirmationLabel(selected)} · 首次确认：${formatTime(typeof indicators.golden_cross_first_confirmation_time === "string" ? indicators.golden_cross_first_confirmation_time : null)}。本根未就绪不抹去历史确认。`} />
        </div>
        <div className="mt-3 rounded-lg bg-muted/40 p-3">
          <h4 className="text-sm font-medium">近期信号记录</h4>
          {selectedSignals.length ? <ul className="mt-3 space-y-3">{selectedSignals.map((signal, index) => <li key={index} className="text-sm">
            <div className="flex flex-wrap justify-between gap-2"><span>{signalLabel(signal.signal_type)}</span><span className="report-tag">{executionLabel(signal)}</span></div>
            <p className="mt-1 text-xs text-muted-foreground">确认 {formatTime(signal.confirmed_at)} · 价格 {formatPrice(signal.price)}</p>
            <p className="mt-1 text-xs text-muted-foreground">结构发生 {formatTime(signal.structure_time)}</p>
          </li>)}</ul> : <p className="mt-2 text-xs text-muted-foreground">{selected?.status === "ok" ? "该周期无近期信号记录。" : "该周期数据不足，无法判断近期信号。"}</p>}
        </div>
      </Section>
      <Section number="04" title="风险观察 · 日线">
        <div className={`mb-4 flex items-start gap-3 rounded-lg p-3 ${risk === "triggered" ? "bg-rose-50 text-rose-800 dark:bg-rose-950/30 dark:text-rose-300" : "bg-muted/40"}`}>
          <TriangleAlert className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
          <div><p className="text-sm font-medium">{risk === "triggered" ? "已有位置风险信号" : risk === "clear" ? "未触发已定义的位置风险项" : "风险检测数据不足"}</p><p className="mt-1 text-xs leading-relaxed">{risk === "unavailable" ? "需要完整日线、长期均线及相关指标，不能把缺失数据当成风险解除。" : "范围限于高位偏离与高位放量，不覆盖所有持仓和市场风险。"}</p></div>
        </div>
        <dl className="report-metrics">
          <div><dt>长期均线</dt><dd>{formatPrice(daily?.indicators.ma_long)}</dd></div>
          <div><dt>距长期均线（绝对偏离）</dt><dd>{formatRatio(daily?.indicators.distance_to_ma_long)}</dd></div>
          <div><dt>近期涨幅（配置窗口）</dt><dd>{formatRatio(daily?.indicators.recent_return)}</dd></div>
          <div><dt>成交量比值</dt><dd>{formatPrice(daily?.indicators.volume_ratio)} 倍</dd></div>
        </dl>
        {Array.isArray(daily?.indicators.position_risk_flags) && daily.indicators.position_risk_flags.length > 0 && <ul className="mt-4 space-y-2 text-sm">{daily.indicators.position_risk_flags.map((flag) => <li key={flag} className="flex gap-2"><span aria-hidden="true">·</span>{flag}</li>)}</ul>}
        <p className="mt-4 border-t pt-3 text-xs leading-relaxed text-muted-foreground">各周期数据截至时间可能不同；市场环境描述分析时状态，不能代表未来持有期。策略启用状态沿用报告记录。</p>
      </Section>
    </div>

    {selected && <details className="report-panel">
      <summary className="cursor-pointer text-sm font-medium">{timeframeLabel(selected.timeframe)}数据明细 <span className="ml-2 text-xs font-normal text-muted-foreground">OHLCV / MACD · {selected.bars.length} 根</span></summary>
      <div className="mt-4 max-h-80 overflow-auto"><table className="report-table min-w-[740px]">
        <caption className="sr-only">当前周期 K 线原始数值</caption>
        <thead><tr>{["时间（北京时间）", "开", "高", "低", "收", "成交量（原始值）", "DIF", "DEA", "HIST"].map((label) => <th scope="col" key={label}>{label}</th>)}</tr></thead>
        <tbody>{selected.bars.map((bar, index) => <tr key={index}><th scope="row" className="!font-normal">{formatTime(bar.datetime)}</th>{(["open", "high", "low", "close", "volume", "dif", "dea", "hist"] as const).map((key) => <td key={key} className="tabular-nums">{formatPrice(bar[key], ["dif", "dea", "hist"].includes(key) ? 4 : 2)}</td>)}</tr>)}</tbody>
      </table></div>
    </details>}
  </article>;
}

export function StockAnalysisReport({ source }: { source: DataSource }) {
  const [index, setIndex] = useState(0);
  const result = source.results[index] ?? source.results[0];
  return <div className="space-y-4">
    {source.results.length > 1 && <label className="flex flex-wrap items-center gap-3 text-sm">报告内股票
      <select aria-label="报告内股票" className="report-select max-w-full" value={index} onChange={(e) => setIndex(Number(e.target.value))}>{source.results.map((stock, i) => <option key={i} value={i}>{stock.name || stock.symbol} · {stock.symbol}</option>)}</select>
      <span className="text-xs text-muted-foreground">共 {source.results.length} 只</span>
    </label>}
    {source.errors.length > 0 && <div role="status" className="report-notice"><p className="font-medium">报告包含 {source.errors.length} 条数据错误</p><ul className="mt-2 space-y-1 text-xs">{source.errors.map((error, i) => <li key={i} className="break-words">{String(error.symbol ?? "")} {String(error.error ?? "数据获取失败")}</li>)}</ul></div>}
    {result ? <StockDetail key={`${index}:${result.symbol}`} result={result} source={source} /> : <div className="report-panel py-12 text-center"><ChartCandlestick className="mx-auto mb-3 size-8 text-muted-foreground" aria-hidden="true" /><p className="font-medium">该任务没有个股结构化报告</p><p className="mt-2 text-sm text-muted-foreground">扫描候选或旧格式结果可通过“数据源”查看。</p></div>}
  </div>;
}

/** Parent keys this loader by task/retry so stale requests cannot replace another task. */
export function StockReportLoader({ jobId, onRetry }: { jobId: number; onRetry: () => void }) {
  const [source, setSource] = useState<DataSource | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const abort = new AbortController();
    let active = true;
    void (async () => {
      try {
        const response = await fetch(`/api/jobs/${jobId}/data`, { signal: abort.signal });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "读取报告失败");
        if (!Array.isArray(data.results)) throw new Error("报告格式不完整，请通过数据源查看");
        if (active) setSource(data as DataSource);
      } catch (err) {
        if (active && !abort.signal.aborted) setError(err instanceof Error ? err.message : "读取报告失败");
      }
    })();
    return () => { active = false; abort.abort(); };
  }, [jobId]);
  if (error) return <div role="alert" className="report-panel"><p className="font-medium">报告暂时无法读取</p><p className="my-3 break-words text-sm text-muted-foreground">{error}</p><Button variant="outline" size="sm" onClick={onRetry}>重新读取</Button></div>;
  if (!source) return <div className="report-panel space-y-4" role="status"><p className="text-sm text-muted-foreground">正在读取个股报告…</p><div className="h-16 animate-pulse rounded bg-muted" /><div className="h-60 animate-pulse rounded bg-muted/50" /></div>;
  return <StockAnalysisReport source={source} />;
}
