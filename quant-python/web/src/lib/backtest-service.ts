import fs from "node:fs/promises";
import path from "node:path";
import { randomUUID } from "node:crypto";
import { createJob, getDb, getJob, updateJob } from "./db";
import { freezeEngineConfig } from "./config";
import type { PoolClient } from "pg";
import { runBridge } from "./bridge";
import { signalSystemDir, resolvePathWithin } from "./paths";
import { assertLocalResearch, localResearchAvailable } from "./local-research";
import { STRATEGIES } from "./analysis-types";
import { normalizeSymbol } from "./symbols";
import { nowIso } from "./time";
const active = new Set<number>();
export function validateBacktestOptions(input: Record<string, unknown>) {
  const start = String(input.start ?? ""), end = String(input.end ?? "");
  if (!/^\d{4}-\d{2}-\d{2}$/.test(start) || !/^\d{4}-\d{2}-\d{2}$/.test(end) || !Number.isFinite(Date.parse(start)) || !Number.isFinite(Date.parse(end)) || start>=end) throw new Error("请选择有效的起止日期");
  if (end>nowIso().slice(0,10)) throw new Error("结束日期不能在未来");
  const initial_cash = Number(input.initial_cash ?? 100000), max_positions = Number(input.max_positions ?? 4), position_size_pct = Number(input.position_size_pct ?? .25);
  if (!Number.isFinite(initial_cash)||initial_cash<1000||initial_cash>1e10) throw new Error("初始资金应为 1000 至 100 亿");
  if (!Number.isInteger(max_positions)||max_positions<1||max_positions>100) throw new Error("最大持仓数量应为 1-100");
  if (!Number.isFinite(position_size_pct)||position_size_pct<=0||position_size_pct>1) throw new Error("单笔仓位比例应为 0-100%");
  const symbols = Array.isArray(input.symbols) ? [...new Set(input.symbols.map(value=>normalizeSymbol(String(value))))] : [];
  if (symbols.some(symbol=>!/^\d{6}\.(SH|SZ|BJ)$/.test(symbol))) throw new Error("股票代码不合法");
  const strategies = Array.isArray(input.strategies) ? input.strategies.map(String) : STRATEGIES.map(row=>row.id);
  if (!strategies.length||strategies.some(id=>!STRATEGIES.some(row=>row.id===id))) throw new Error("请选择有效策略");
  return { start,end,initial_cash,max_positions,position_size_pct,symbols,strategies };
}
export async function startBacktest(input: Record<string,unknown>) {
  assertLocalResearch();
  const options = { ...validateBacktestOptions(input), output_dir: path.join(signalSystemDir,"output","backtests",randomUUID()) };
  const overrides = await freezeEngineConfig();
  const id = await createJob("backtest",{ options,overrides });
  void executeBacktest(id).catch(console.error);
  return id;
}
export async function executeBacktest(id: number) {
  assertLocalResearch();
  if (active.has(id)) return;
  active.add(id);
  let client: PoolClient | undefined;
  try {
    client = await (await getDb()).connect();
    if (!(await client.query("SELECT pg_try_advisory_lock(920001) AS acquired")).rows[0].acquired) return;
    const job = await getJob(id); if(!job) return;
    const payload = JSON.parse(job.payload);
    await updateJob(id,{status:"running",started_at:job.started_at??nowIso(),error:null});
    const outcome = await runBridge("research-backtest",payload,{env:{QUANT_RESEARCH_LOCAL:"1"}});
    if(!outcome.ok) throw new Error(outcome.error??"回测失败");
    const result=(outcome.data as {report:{output_file:string}}).report;
    await updateJob(id,{status:"success",result_path:result.output_file,finished_at:nowIso()});
  } catch(error) { await updateJob(id,{status:"failed",error:error instanceof Error?error.message:String(error),finished_at:nowIso()}); }
  finally {if (client) {await client.query("SELECT pg_advisory_unlock(920001)").catch(()=>undefined);client.release();}active.delete(id);}
}
export async function resumeBacktests() {
  if(!localResearchAvailable()) return;
  const jobs=(await(await getDb()).query("SELECT id FROM quant.jobs WHERE kind='backtest' AND status IN ('pending','running') ORDER BY id")).rows;
  for(const job of jobs) void executeBacktest(Number(job.id)).catch(console.error);
}
export async function readBacktestFile(file:string) {
  const resolved=resolvePathWithin(path.join(signalSystemDir,"output","backtests"),file);
  if(!resolved) throw new Error("回测文件路径无效");
  return JSON.parse(await fs.readFile(resolved,"utf8"));
}
