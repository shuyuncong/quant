import { getDb, createJob, updateJob } from "./db";
import { startJob } from "./jobs";
import type { ScheduleRow } from "./types";
import { shanghaiDate, shanghaiHhmm, nowIso } from "./time";
export interface DueSlot { key: string; at: string; missed: boolean }
export function dueSlots(row: ScheduleRow, calendar: { is_trading_day: boolean; is_trading_session: boolean }, now: Date): DueSlot[] {
  if (!row.enabled || (row.trading_days_only && !calendar.is_trading_day)) return [];
  const day = shanghaiDate(now), hhmm = shanghaiHhmm(now);
  if (row.kind === "daily_scan") return hhmm >= row.time ? [{ key: `${row.id}:${day}`, at: `${day} ${row.time}:00`, missed: false }] : [];
  const minute = now.getHours()*60+now.getMinutes();
  if (row.kind === "monitor_fixed") return row.fixed_times.filter(time => time <= hhmm).map(time => {
    const [h,m] = time.split(":").map(Number);
    return { key: `${row.id}:${day}:${time}`, at: `${day} ${time}:00`, missed: minute-h*60-m>5 };
  });
  if (!calendar.is_trading_session) return [];
  const seconds = minute*60+now.getSeconds();
  const slot = Math.floor(seconds / Math.max(60, row.interval_seconds));
  return [{ key: `${row.id}:${day}:${slot}`, at: nowIso(now), missed: false }];
}
let ticking = false;
export async function runScheduleTick(rows: ScheduleRow[], calendar: { is_trading_day: boolean; is_trading_session: boolean }, now: Date) {
  if (ticking) return;
  ticking = true;
  try {
    const db = await getDb();
    // A restart between claiming a slot and freezing its inputs is recoverable.
    const prepared = (await db.query("SELECT id,kind,payload FROM quant.jobs WHERE status='pending' AND payload::jsonb->>'prepare_pending'='true' ORDER BY id")).rows;
    for (const job of prepared) {
      try { await startJob(job.kind, { ...JSON.parse(job.payload), prepare_pending: false, existing_job_id: Number(job.id) }); }
      catch (error) { await updateJob(Number(job.id), { status: "failed", error: String(error), finished_at: nowIso() }); }
    }
    for (const row of rows) {
      for (const slot of dueSlots(row, calendar, now)) {
        if (row.kind === "monitor_cycle") await db.query("UPDATE quant.schedule_runs SET status='merged',detail='后续间隔触发已合并' WHERE schedule_kind=$1 AND status='pending' AND job_id IS NULL AND run_key<>$2", [row.kind, slot.key]);
        await db.query(`INSERT INTO quant.schedule_runs(run_key,schedule_kind,scheduled_at,status,detail,created_at)
          VALUES($1,$2,$3,$4,$5,$6) ON CONFLICT(run_key) DO NOTHING`, [slot.key, row.kind, slot.at, slot.missed ? "missed" : "pending", slot.missed ? "超过 5 分钟迟到窗口" : "", nowIso()]);
      }
    }
    const pending = (await db.query("SELECT * FROM quant.schedule_runs WHERE status IN ('pending','running') ORDER BY scheduled_at")).rows;
    for (const run of pending) {
      if (run.job_id) {
        const job = (await db.query("SELECT status FROM quant.jobs WHERE id=$1", [run.job_id])).rows[0];
        if (job?.status === "success" || job?.status === "failed") await db.query("UPDATE quant.schedule_runs SET status=$2 WHERE run_key=$1", [run.run_key, job.status]);
        continue;
      }
      const row = rows.find(row => run.run_key.startsWith(`${row.id}:`));
      if (!row?.enabled) continue;
      if (String(run.scheduled_at).slice(0, 10) !== shanghaiDate(now)) {
        await db.query("UPDATE quant.schedule_runs SET status='missed',detail='已超过计划交易日' WHERE run_key=$1", [run.run_key]); continue;
      }
      const kind = row.kind === "daily_scan" ? "daily-scan" : "monitor-cycle";
      const active = (await db.query("SELECT id FROM quant.jobs WHERE status IN ('pending','running') AND kind=ANY($1::text[]) LIMIT 1", [row.kind === "daily_scan" ? ["daily-scan","scan"] : ["analyze","monitor-cycle","monitor-once"]])).rows.length > 0;
      if (active) continue;
      if (row.kind === "monitor_cycle" && !calendar.is_trading_session) continue;
      if (row.kind === "monitor_fixed" && now.getTime()-new Date(run.scheduled_at).getTime()>300_000) {
        await db.query("UPDATE quant.schedule_runs SET status='missed',detail='运行队列延迟超过 5 分钟' WHERE run_key=$1", [run.run_key]); continue;
      }
      const client = await db.connect();
      let jobId: number;
      try {
        await client.query("BEGIN");
        const locked = (await client.query("SELECT job_id FROM quant.schedule_runs WHERE run_key=$1 FOR UPDATE", [run.run_key])).rows[0];
        if (locked.job_id) { await client.query("ROLLBACK"); continue; }
        jobId = await createJob(kind, { schedule_run_key: run.run_key, prepare_pending: true, scope: row.scope, notify: true }, client);
        await client.query("UPDATE quant.schedule_runs SET job_id=$2,status='running' WHERE run_key=$1", [run.run_key, jobId]);
        await client.query("COMMIT");
      } catch (error) { await client.query("ROLLBACK"); throw error; }
      finally { client.release(); }
      try { await startJob(kind, { existing_job_id: jobId, schedule_run_key: run.run_key, scope: row.scope, notify: true }); }
      catch (error) { await updateJob(jobId, { status: "failed", error: String(error), finished_at: nowIso() }); }
    }
  } finally { ticking = false; }
}
