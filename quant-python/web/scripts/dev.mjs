/**
 * npm run dev 的启动器：
 * 1. 按 Next.js 的 dotenv 优先级加载环境变量；
 * 2. 确认 DATABASE_URL 只指向本地 Docker PostgreSQL；
 * 3. 再启动 next dev。
 *
 * 直接 spawn next 的 bin 脚本，Ctrl+C 时父子进程同在一个控制台组，
 * 行为与直接 `next dev` 一致。
 */
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import nextEnv from "@next/env";
import { assertLocalDevelopmentDatabase } from "./db-safety.mjs";

const root = dirname(dirname(fileURLToPath(import.meta.url))); // web/
const { loadEnvConfig } = nextEnv;
// 与 `next dev` 使用完全相同的 dotenv 优先级，门禁不会漏掉
// .env.development.local / .env.local / .env.development / .env。
loadEnvConfig(root, true);

function spawnNext() {
  const nextBin = join(root, "node_modules", "next", "dist", "bin", "next");
  const child = spawn(
    process.execPath,
    [nextBin, "dev", "-H", "0.0.0.0", "-p", "3111"],
    { stdio: "inherit" },
  );
  child.on("exit", (code, signal) => {
    if (signal) {
      process.kill(process.pid, signal);
    } else {
      process.exit(code ?? 0);
    }
  });
}

function start() {
  // 开发环境只允许 AGENTS.md 规定的本地 Docker PostgreSQL：loopback:5432/quant。
  // 同时拒绝 URL 查询参数，避免 `?host=...` 覆盖连接目标绕过门禁。
  const databaseUrl = process.env.DATABASE_URL?.trim();
  if (!databaseUrl) {
    console.error("[database] 拒绝启动：开发环境必须设置本地 DATABASE_URL");
    process.exit(1);
  }
  try {
    assertLocalDevelopmentDatabase(databaseUrl);
  } catch (error) {
    console.error(
      `[database] 拒绝启动：${error instanceof Error ? error.message : String(error)}`,
    );
    process.exit(1);
  }
  spawnNext();
}

start();
