"use client";

import { useId, useState, type PointerEvent } from "react";
import {
  type DataBar, type DataTimeframe, executionLabel, formatPrice, formatTime,
  signalLabel, timeframeSignals, timestamp,
} from "@/lib/stock-report";

export function validCandle(bar: DataBar): boolean {
  const { open, high, low, close } = bar;
  return [open, high, low, close].every((value) => value !== null && Number.isFinite(value) && value > 0)
    && high! >= Math.max(open!, close!) && low! <= Math.min(open!, close!) && high! >= low!;
}

const WIDTH = 960;
const LEFT = 68;
const RIGHT = 880;
const PRICE_TOP = 30;
const PRICE_BOTTOM = 245;
const VOLUME_TOP = 278;
const VOLUME_BOTTOM = 332;
const MACD_BASE = 408;
const MACD_BOTTOM = 438;
const DATE_Y = 464;
const VIEW_HEIGHT = 480;

export function StockPriceChart({ timeframe: tf }: { timeframe: DataTimeframe }) {
  const titleId = useId();
  const [selected, setSelected] = useState<number | null>(null);
  const [showCenter, setShowCenter] = useState(true);
  const bars = tf.bars;
  const valid = bars.filter(validCandle);
  if (!valid.length) {
    return <div className="report-chart-empty" role="status">暂无可绘制的完整 K 线。可在数据明细中查看已有记录。</div>;
  }
  const center = showCenter ? tf.latest_center : null;
  const prices = valid.flatMap((bar) => [bar.low!, bar.high!]);
  if (center) prices.push(center.zd, center.zg);
  const min = Math.min(...prices);
  const max = Math.max(...prices);
  const padding = Math.max((max - min) * 0.12, max * 0.005, 0.01);
  const lo = min - padding;
  const hi = max + padding;
  const y = (value: number) => PRICE_BOTTOM - (value - lo) / (hi - lo) * (PRICE_BOTTOM - PRICE_TOP);
  const step = (RIGHT - LEFT) / bars.length;
  const x = (index: number) => LEFT + (index + 0.5) * step;
  const candleWidth = Math.min(12, step * 0.64);
  const activeIndex = Math.min(selected ?? bars.length - 1, bars.length - 1);
  const active = bars[activeIndex];
  const volumes = bars.map((bar) => bar.volume).filter((value): value is number => value !== null && value >= 0);
  const volumeMax = Math.max(...volumes, 1);
  const macdValues = bars.flatMap((bar) => [bar.dif, bar.dea, bar.hist]).filter((value): value is number => value !== null);
  const macdMax = Math.max(...macdValues.map(Math.abs), 0.000001);
  const macdY = (value: number) => MACD_BASE - value / macdMax * 28;
  const macdPath = (key: "dif" | "dea") => {
    let connected = false;
    return bars.map((bar, index) => {
      const value = bar[key];
      if (value === null) { connected = false; return ""; }
      const command = connected ? "L" : "M";
      connected = true;
      return `${command}${x(index)},${macdY(value)}`;
    }).join(" ");
  };
  const selectPointer = (event: PointerEvent<SVGSVGElement>) => {
    const bounds = event.currentTarget.getBoundingClientRect();
    const local = (event.clientX - bounds.left) / bounds.width * WIDTH;
    setSelected(Math.max(0, Math.min(bars.length - 1, Math.floor((local - LEFT) / step))));
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3 text-xs">
        <div className="flex flex-wrap items-center gap-4">
          <label className="inline-flex cursor-pointer items-center gap-2">
            <input type="checkbox" checked={showCenter} onChange={(event) => setShowCenter(event.target.checked)} disabled={!tf.latest_center} className="accent-sky-700" />
            最新中枢参考带
          </label>
          <span className="text-muted-foreground">成交量 / MACD 同屏</span>
        </div>
        <span className="text-muted-foreground">红涨 · 绿跌 · B 买向 / S 卖向 / W 观察或状态未知</span>
      </div>
      <div className="report-chart-readout" aria-live="polite" aria-atomic="true">
        <span className="font-medium text-foreground">{formatTime(active.datetime)}</span>
        <span>开 {formatPrice(active.open)}</span><span>高 {formatPrice(active.high)}</span>
        <span>低 {formatPrice(active.low)}</span><span>收 {formatPrice(active.close)}</span>
        <span>量 {formatPrice(active.volume, 0)}（原始值）</span>
        <span>DIF {formatPrice(active.dif, 4)}</span><span>DEA {formatPrice(active.dea, 4)}</span><span>HIST {formatPrice(active.hist, 4)}</span>
      </div>
      <div className="overflow-x-auto rounded-lg border border-border/60">
        <svg viewBox={`0 0 ${WIDTH} ${VIEW_HEIGHT}`} role="img" aria-labelledby={titleId} className="block w-full min-w-[560px] text-muted-foreground" onPointerMove={selectPointer} onPointerDown={selectPointer}>
          <title id={titleId}>K 线、成交量与 MACD 同屏展示。使用下方选点滑块或展开数据明细查看数值。</title>
          {[0, 1, 2, 3, 4].map((index) => {
            const price = lo + (hi - lo) * index / 4;
            return <g key={index}><line x1={LEFT} x2={RIGHT} y1={y(price)} y2={y(price)} stroke="currentColor" opacity="0.13" strokeDasharray="3 4" /><text x={LEFT - 12} y={y(price) + 4} fill="currentColor" fontSize="12" textAnchor="end">{formatPrice(price)}</text></g>;
          })}
          {center && <g className="text-sky-700 dark:text-sky-400">
            <rect x={LEFT} y={y(center.zg)} width={RIGHT - LEFT} height={Math.max(y(center.zd) - y(center.zg), 1)} fill="currentColor" opacity="0.09" />
            {[{ label: "ZG", value: center.zg }, { label: "ZD", value: center.zd }].map(({ label, value }) => <g key={label}>
              <line x1={LEFT} x2={RIGHT} y1={y(value)} y2={y(value)} stroke="currentColor" strokeDasharray="5 4" opacity="0.65" />
              <text x={RIGHT + 6} y={y(value) + (label === "ZG" ? -5 : 13)} fill="currentColor" fontSize="11">{label} {formatPrice(value)}</text>
            </g>)}
          </g>}
          {bars.map((bar, index) => validCandle(bar) && <g key={index} className={bar.close! >= bar.open! ? "report-up" : "report-down"}>
            <line x1={x(index)} x2={x(index)} y1={y(bar.high!)} y2={y(bar.low!)} stroke="currentColor" strokeWidth="1.2" />
            <rect x={x(index) - candleWidth / 2} y={Math.min(y(bar.open!), y(bar.close!))} width={candleWidth} height={Math.max(Math.abs(y(bar.open!) - y(bar.close!)), 1.5)} fill="currentColor" />
          </g>)}
          {timeframeSignals(tf).filter((signal) => signal.side === "buy" || signal.side === "sell").map((signal, index) => {
            const time = timestamp(signal.confirmed_at);
            const barIndex = time === null ? -1 : bars.findIndex((bar) => timestamp(bar.datetime) === time);
            if (barIndex < 0 || !validCandle(bars[barIndex])) return null;
            const buy = signal.side === "buy";
            const watch = signal.execution_mode !== "enabled" || signal.actionable !== true;
            const py = buy ? y(bars[barIndex].low!) + 14 : y(bars[barIndex].high!) - 10;
            return <text key={index} x={x(barIndex)} y={py} textAnchor="middle" fontSize="11" fontWeight="700" className={watch ? "text-amber-700 dark:text-amber-400" : buy ? "report-up" : "report-down"} fill="currentColor">
              <title>{signalLabel(signal.signal_type)} · {executionLabel(signal)} · 记录确认时间 {formatTime(signal.confirmed_at)}</title>{watch ? "W" : buy ? "B" : "S"}
            </text>;
          })}
          <line x1={LEFT} x2={RIGHT} y1="258" y2="258" stroke="currentColor" opacity="0.16" />
          <text x={LEFT} y="272" fill="currentColor" fontSize="11">VOL · 报告原始量</text>
          {bars.map((bar, index) => bar.volume !== null && bar.volume >= 0 && <rect key={index} x={x(index) - candleWidth / 2} y={VOLUME_BOTTOM - bar.volume / volumeMax * (VOLUME_BOTTOM - VOLUME_TOP)} height={Math.max(bar.volume / volumeMax * (VOLUME_BOTTOM - VOLUME_TOP), 1)} width={candleWidth} className={validCandle(bar) ? bar.close! >= bar.open! ? "report-up" : "report-down" : "text-muted-foreground"} fill="currentColor" opacity="0.65" />)}
          {!volumes.length && <text x="480" y="310" textAnchor="middle" fill="currentColor" fontSize="13">成交量数据未提供</text>}
          <line x1={LEFT} x2={RIGHT} y1="344" y2="344" stroke="currentColor" opacity="0.16" />
          <text x={LEFT} y="358" fill="currentColor" fontSize="11">MACD · DIF / DEA / HIST</text>
          <line x1={LEFT} x2={RIGHT} y1={MACD_BASE} y2={MACD_BASE} stroke="currentColor" opacity="0.2" />
          {bars.map((bar, index) => bar.hist !== null && <rect key={index} x={x(index) - candleWidth / 2} y={Math.min(macdY(bar.hist), MACD_BASE)} height={Math.max(Math.abs(macdY(bar.hist) - MACD_BASE), 1)} width={candleWidth} className={bar.hist >= 0 ? "report-up" : "report-down"} fill="currentColor" opacity="0.4" />)}
          <path d={macdPath("dif")} fill="none" stroke="currentColor" className="text-sky-700 dark:text-sky-400" strokeWidth="1.5" />
          <path d={macdPath("dea")} fill="none" stroke="currentColor" className="text-amber-700 dark:text-amber-400" strokeWidth="1.5" strokeDasharray="4 2" />
          {!macdValues.length && <text x="480" y="400" textAnchor="middle" fill="currentColor" fontSize="13">MACD 数据未提供</text>}
          <line x1={x(activeIndex)} x2={x(activeIndex)} y1={PRICE_TOP} y2={MACD_BOTTOM} stroke="currentColor" strokeDasharray="3 3" opacity="0.5" />
          {[0, Math.floor((bars.length - 1) / 2), bars.length - 1].filter((value, index, values) => values.indexOf(value) === index).map((index) => <text key={index} x={x(index)} y={DATE_Y} textAnchor={index === 0 ? "start" : index === bars.length - 1 ? "end" : "middle"} fill="currentColor" fontSize="12">{formatTime(bars[index].datetime).slice(0, 10)}</text>)}
        </svg>
      </div>
      <label className="flex items-center gap-3 text-xs text-muted-foreground">
        <span className="shrink-0">逐根查看</span>
        <input aria-label="选择 K 线" type="range" min="0" max={bars.length - 1} value={activeIndex} onChange={(event) => setSelected(Number(event.target.value))} aria-valuetext={formatTime(active.datetime)} className="min-w-0 flex-1 accent-sky-700" />
        <span className="tabular-nums">{activeIndex + 1} / {bars.length}</span>
      </label>
      <p className="text-xs leading-relaxed text-muted-foreground">
        展示报告内 {bars.length} 根 K 线{tf.history_bar_count !== null ? ` · 引擎分析 ${tf.history_bar_count} 根` : ""}。信号按记录确认时间定位，B/S 仅表示事件方向；观察信号标为 W，范围外记录见下方明细。{tf.latest_center ? ` 蓝带为最新中枢的当前价格参考，并非历史逐时中枢；结构区间 ${formatTime(tf.latest_center.start_time)} 至 ${formatTime(tf.latest_center.end_time)}。` : " 当前周期未提供有效中枢。"} DIF 为蓝色实线，DEA 为琥珀色虚线。
      </p>
    </div>
  );
}
