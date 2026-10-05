"use client";
import { STRATEGIES, type AnalysisScope } from "@/lib/analysis-types";
export function AnalysisScopePicker({ value, onChange }: { value: AnalysisScope; onChange: (value: AnalysisScope) => void }) {
  return <fieldset className="flex flex-wrap items-center gap-x-4 gap-y-2 text-xs"><legend className="mb-2 text-sm font-medium">监控范围</legend>
    <label className="flex items-center gap-1.5"><input type="checkbox" checked={value.holdings} onChange={event => onChange({ ...value, holdings: event.target.checked })} />我的持仓</label>
    <label className="flex items-center gap-1.5"><input type="checkbox" checked={value.watchlist} onChange={event => onChange({ ...value, watchlist: event.target.checked })} />自定义股票池</label>
    {STRATEGIES.map(strategy => <label key={strategy.id} className="flex items-center gap-1.5"><input type="checkbox" checked={value.pools.includes(strategy.id)} onChange={event => onChange({ ...value, pools: event.target.checked ? [...value.pools, strategy.id] : value.pools.filter(id => id !== strategy.id) })} />{strategy.name}池</label>)}
  </fieldset>;
}
