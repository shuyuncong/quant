/**
 * npm run dev 的启动器：
 * 1. 先确保 DB 中继（scripts/db-tunnel.mjs）在 127.0.0.1:15432 上运行，
 *    否则 Next 的 instrumentation 钩子连库会 ECONNREFUSED；
 * 2. 再启动 next dev。
 *
 * 直接 spawn next 的 bin 脚本，Ctrl+C 时父子进程同在一个控制台组，
 * 行为与直接 `next dev` 一致。
 */
import { spawn } from "node:child_process";
import net from "node:net";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { createRelay, relayConfig } from "./db-tunnel.mjs";

const RELAY_PORT = relayConfig().listenPort;

function portOpen(port) {
  return new Promise((resolve) => {
    const socket = net.connect({ host: "127.0.0.1", port });
    socket.setTimeout(1500);
    socket.once("connect", () => {
      socket.destroy();
      resolve(true);
    });
    socket.once("timeout", () => {
      socket.destroy();
      resolve(false);
    });
    socket.once("error", () => resolve(false));
  });
}

function spawnNext() {
  const root = dirname(dirname(fileURLToPath(import.meta.url))); // web/
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

async function start() {
  const isOpen = await portOpen(RELAY_PORT);
  if (isOpen) {
    console.log(`[db-tunnel] relay already listening on 127.0.0.1:${RELAY_PORT}`);
    spawnNext();
    return;
  }
  const { server, listen } = createRelay();
  await listen();
  console.log(`[db-tunnel] relay listening on 127.0.0.1:${RELAY_PORT}`);
  spawnNext();
  for (const signal of ["SIGINT", "SIGTERM"]) {
    process.on(signal, () => server.close(() => process.exit(0)));
  }
}

void start();
