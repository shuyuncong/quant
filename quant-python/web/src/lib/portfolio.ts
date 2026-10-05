import { createHash } from "node:crypto";
import type { PoolClient } from "pg";
import { getDb } from "./db";
import { nowIso } from "./time";
import { normalizeSymbol } from "./symbols";
import { calculateTrade, decimal, decimalString, validateTradeLot, type TradeInput } from "./trade-math";

async function transaction<T>(run: (client: PoolClient) => Promise<T>): Promise<T> {
  const client = await (await getDb()).connect();
  try {
    await client.query("BEGIN");
    const result = await run(client);
    await client.query("COMMIT");
    return result;
  } catch (error) {
    await client.query("ROLLBACK");
    throw error;
  } finally { client.release(); }
}
async function lockHolding(client: PoolClient, symbol: string) {
  await client.query("SELECT pg_advisory_xact_lock(hashtext($1))", [`portfolio:${symbol}`]);
  return (await client.query("SELECT * FROM quant.holdings WHERE symbol=$1 FOR UPDATE", [symbol])).rows[0];
}
const validateSymbol = (value: string) => {
  const symbol = normalizeSymbol(value);
  if (!/^\d{6}\.(SH|SZ|BJ)$/.test(symbol)) throw new Error("股票代码不合法");
  return symbol;
};

export async function recordHoldingTrade(raw: TradeInput) {
  const input = { ...raw, symbol: validateSymbol(raw.symbol), price: decimalString(decimal(raw.price)), fees: decimalString(decimal(raw.fees)) };
  if (!/^[\w-]{16,100}$/.test(input.request_key)) throw new Error("缺少有效的成交请求编号");
  if (!/^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?$/.test(input.traded_at)) throw new Error("成交时间格式不合法");
  const date = new Date(input.traded_at.replace(" ", "T") + (input.traded_at.length === 16 ? ":00" : "") + "+08:00");
  if (!Number.isFinite(date.getTime()) || date.getTime() > Date.now() + 60_000) throw new Error("成交时间不能在未来");
  input.traded_at = input.traded_at.replace("T", " ") + (input.traded_at.length === 16 ? ":00" : "");
  if (new Date(date.getTime()+8*3600_000).toISOString().slice(0,19).replace("T"," ") !== input.traded_at) throw new Error("成交日期或时间不存在");
  input.note = String(input.note ?? "").trim().slice(0, 1000);
  const requestHash = createHash("sha256").update(JSON.stringify(input)).digest("hex");
  return transaction(async (client) => {
    const current = await lockHolding(client, input.symbol);
    const prior = (await client.query("SELECT * FROM quant.holding_trades WHERE request_key=$1", [input.request_key])).rows[0];
    if (prior) {
      if (prior.request_hash !== requestHash) throw new Error("同一请求编号对应的成交内容已改变，请重新打开表单");
      return { trade: prior, holding: current, reused: true };
    }
    if (!current || Number(current.shares) <= 0) throw new Error("请先添加持仓或登记期初持仓，再执行加减仓");
    const last = (await client.query("SELECT MAX(traded_at) AS latest FROM quant.holding_trades WHERE symbol=$1", [input.symbol])).rows[0]?.latest;
    if (last && input.traded_at < last) throw new Error("成交时间早于已有记录，请先核对历史成交顺序");
    const day = input.traded_at.slice(0, 10);
    const correction = (await client.query("SELECT created_at,after_state FROM quant.holding_events WHERE symbol=$1 AND kind IN ('opening_snapshot','correction') ORDER BY id DESC LIMIT 1", [input.symbol])).rows[0];
    if (correction && day < correction.created_at.slice(0,10)) throw new Error("成交早于最近一次持仓校正日期，请核对登记顺序");
    const sameDay = (await client.query("SELECT COALESCE(SUM(quantity),0) AS bought FROM quant.holding_trades WHERE symbol=$1 AND side='buy' AND traded_at LIKE $2 AND holding_version>$3", [input.symbol, `${day}%`, Number(correction?.after_state?.version??0)])).rows[0];
    const available = Math.max(0, Number(current.shares) - Number(sameDay.bought));
    if (input.side === "sell" && input.quantity > available) throw new Error(`T+1 限制：当前可卖 ${available} 股`);
    validateTradeLot(input.symbol, input.side, input.quantity, available);
    const result = calculateTrade({ ...current, shares: Number(current.shares) }, input);
    const version = Number(current.version ?? 0) + 1;
    const now = nowIso();
    const holding = (await client.query(
      "UPDATE quant.holdings SET shares=$2,cost_price=$3,total_amount=$4,version=$5,updated_at=$6 WHERE symbol=$1 RETURNING *",
      [input.symbol, result.shares, result.cost_price, result.total_amount, version, now],
    )).rows[0];
    const trade = (await client.query(
      `INSERT INTO quant.holding_trades(request_key,request_hash,symbol,side,quantity,price,fees,amount,cash_amount,realized_pnl,traded_at,note,holding_version,created_at)
       VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14) RETURNING *`,
      [input.request_key, requestHash, input.symbol, input.side, input.quantity, input.price, result.fees, result.amount, result.cash_amount, result.realized_pnl, input.traded_at, input.note, version, now],
    )).rows[0];
    await client.query("INSERT INTO quant.holding_events(symbol,kind,before_state,after_state,created_at) VALUES($1,$2,$3,$4,$5)",
      [input.symbol, input.side, JSON.stringify(current), JSON.stringify(holding), now]);
    return { trade, holding, reused: false };
  });
}

export async function correctHolding(input: { symbol: string; name?: string; shares?: number; cost_price?: number; total_amount?: number }) {
  const symbol = validateSymbol(input.symbol);
  const shares = Number(input.shares ?? 0);
  if (!Number.isSafeInteger(shares) || shares < 0) throw new Error("持仓数量必须是非负整数");
  const cost = decimal(input.cost_price ?? 0);
  if (shares > 0 && cost <= 0n) throw new Error("有持仓时成本价必须大于 0");
  const amount = input.total_amount && input.total_amount > 0 ? decimal(input.total_amount) : cost * BigInt(shares);
  return transaction(async (client) => {
    const previous = await lockHolding(client, symbol);
    const now = nowIso();
    const holding = (await client.query(
      `INSERT INTO quant.holdings(symbol,name,shares,cost_price,total_amount,version,created_at,updated_at) VALUES($1,$2,$3,$4,$5,1,$6,$6)
       ON CONFLICT(symbol) DO UPDATE SET name=EXCLUDED.name,shares=EXCLUDED.shares,cost_price=EXCLUDED.cost_price,total_amount=EXCLUDED.total_amount,version=quant.holdings.version+1,updated_at=EXCLUDED.updated_at RETURNING *`,
      [symbol, input.name ?? previous?.name ?? "", shares, decimalString(cost), decimalString(shares ? amount : 0n), now],
    )).rows[0];
    await client.query("INSERT INTO quant.holding_events(symbol,kind,before_state,after_state,created_at) VALUES($1,$2,$3,$4,$5)",
      [symbol, previous ? "correction" : "opening_snapshot", JSON.stringify(previous ?? {}), JSON.stringify(holding), now]);
    return holding;
  });
}

export async function listHoldingTrades(symbol?: string, limit = 50) {
  return (await (await getDb()).query(
    `SELECT id,symbol,side,quantity,price,fees,amount,cash_amount,realized_pnl,traded_at,note,holding_version
     FROM quant.holding_trades ${symbol ? "WHERE symbol=$2" : ""} ORDER BY traded_at DESC,id DESC LIMIT $1`,
    symbol ? [Math.min(limit, 500), validateSymbol(symbol)] : [Math.min(limit, 500)],
  )).rows;
}

export async function portfolioSnapshot() {
  return transaction(async (client) => {
    await client.query("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ");
    const holdings = (await client.query("SELECT * FROM quant.holdings WHERE shares>0 ORDER BY symbol")).rows.map(row => ({ ...row, shares: Number(row.shares), cost_price: Number(row.cost_price), total_amount: Number(row.total_amount), version: Number(row.version) }));
    const capital = (await client.query("SELECT value FROM quant.settings WHERE key='holdings.total_capital'")).rows[0];
    const trades = (await client.query(`SELECT * FROM (SELECT id,symbol,side,quantity,price,fees,amount,realized_pnl,traded_at,
      ROW_NUMBER() OVER(PARTITION BY symbol ORDER BY traded_at DESC,id DESC) AS rn FROM quant.holding_trades) t WHERE rn<=50 OR traded_at LIKE $1 ORDER BY symbol,traded_at DESC`, [`${nowIso().slice(0,10)}%`])).rows;
    return { holdings, trades, total_capital: capital ? Number(JSON.parse(capital.value)) || 0 : 0,
      captured_at: nowIso(), history_complete: false, history_note: "历史成交可能不完整；每股最多最近 50 笔。期初持仓不代表已知买入日期。" };
  });
}
