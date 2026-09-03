/**
 * Local TCP relay for the Supabase PostgreSQL session pooler.
 *
 * A controlled server-side maintenance command may point PROD_DATABASE_URL at
 * 127.0.0.1:15432. `npm run dev` never starts this relay.
 * Direct connections to the AWS pooler are firewalled at the TLS layer from
 * the dev workstation, so traffic must egress through the local Clash proxy:
 *
 *   pg client -> 127.0.0.1:15432 -> HTTP CONNECT via 127.0.0.1:7890
 *              -> aws-0-ap-southeast-1.pooler.supabase.com:5432
 *
 * Run standalone only: npm run db:tunnel
 * `npm run dev` deliberately never imports or starts this relay.
 *
 * Env overrides:
 *   RELAY_LISTEN_PORT  (default 15432)
 *   RELAY_PROXY        host:port of the HTTP proxy (default 127.0.0.1:7890)
 *   RELAY_TARGET       host:port of the PostgreSQL server (required; no production default)
 */
import net from "node:net";

function envHostPort(name, fallback) {
  const raw = process.env[name]?.trim();
  if (raw) {
    const idx = raw.lastIndexOf(":");
    if (idx > 0) return { host: raw.slice(0, idx), port: Number(raw.slice(idx + 1)) };
    throw new Error(`${name} must be host:port (received ${raw})`);
  }
  const [host, port] = fallback.split(":");
  return { host, port: Number(port) };
}

export function relayConfig() {
  // 开发机不得直连生产库：不提供 Supabase 默认 target。
  // 必须显式设置 RELAY_TARGET 才会建立转发（且只用于受控的服务器侧迁移/回滚场景）。
  if (!process.env.RELAY_TARGET) {
    throw new Error(
      "RELAY_TARGET is required (开发机禁止直连生产库；db-tunnel 仅用于受控服务器侧迁移场景)",
    );
  }
  return {
    listenPort: Number(process.env.RELAY_LISTEN_PORT ?? 15432),
    proxy: envHostPort("RELAY_PROXY", "127.0.0.1:7890"),
    target: envHostPort("RELAY_TARGET", ""),
  };
}

async function openTunnel(proxy, target, log) {
  const upstream = net.connect(proxy);
  await new Promise((resolve, reject) => {
    upstream.setTimeout(15_000);
    upstream.once("timeout", () => reject(new Error("proxy timeout")));
    upstream.once("error", reject);
    upstream.once("connect", resolve);
  });
  upstream.setTimeout(0);

  const authority = `${target.host}:${target.port}`;
  log(`[db-tunnel] connecting ${authority} via ${proxy.host}:${proxy.port}`);
  await new Promise((resolve, reject) => {
    upstream.write(
      `CONNECT ${authority} HTTP/1.1\r\nHost: ${authority}\r\nProxy-Connection: keep-alive\r\n\r\n`,
    );
    let buf = Buffer.alloc(0);
    const onData = (chunk) => {
      buf = Buffer.concat([buf, chunk]);
      const headEnd = buf.indexOf("\r\n\r\n");
      if (headEnd < 0) return;
      upstream.removeListener("data", onData);
      const head = buf.subarray(0, headEnd).toString();
      const rest = buf.subarray(headEnd + 4);
      if (!/ 200 /.test(head)) {
        reject(new Error(`proxy refused CONNECT: ${head.split("\r\n")[0]}`));
        return;
      }
      if (rest.length > 0) upstream.unshift(rest);
      resolve();
    };
    upstream.on("data", onData);
    upstream.once("error", reject);
    upstream.setTimeout(15_000, () => {
      upstream.removeListener("data", onData);
      reject(new Error("proxy CONNECT timeout"));
    });
  });
  upstream.setTimeout(0);
  log(`[db-tunnel] tunnel established: ${authority} via ${proxy.host}:${proxy.port}`);
  return upstream;
}

function pump(src, dst) {
  return new Promise((resolve) => {
    src.on("data", (chunk) => dst.write(chunk));
    src.on("end", () => {
      try {
        dst.end();
      } catch {
        /* dst already destroyed */
      }
    });
    src.on("error", () => dst.destroy());
    src.on("close", resolve);
  });
}

/**
 * Creates the relay server. Call `server.listen(port)` (or the returned
 * `ready` promise) to start it.
 */
export function createRelay(cfg = relayConfig(), log = console.log) {
  const { listenPort, proxy, target } = cfg;
  const server = net.createServer((client) => {
    client.on("error", () => client.destroy());
    const tunnel = openTunnel(proxy, target, log);
    tunnel
      .then((socket) => {
        socket.on("error", () => client.destroy());
        Promise.all([pump(client, socket), pump(socket, client)]).catch(() => undefined);
      })
      .catch((error) => {
        log(`[db-tunnel] ${error.message}`);
        client.destroy();
      });
  });
  server.on("error", (error) => {
    if (error.code === "EADDRINUSE") {
      log(`[db-tunnel] port ${listenPort} already in use (relay already running?)`);
    } else {
      log(`[db-tunnel] error: ${error.message}`);
    }
  });
  return { server, listen: () => new Promise((r) => server.listen(listenPort, "127.0.0.1", r)) };
}

if (process.argv[1] && import.meta.url === new URL(`file://${process.argv[1].replace(/\\/g, "/")}`).href) {
  const { server, listen } = createRelay();
  await listen();
  console.log(`[db-tunnel] relay listening on 127.0.0.1:${relayConfig().listenPort}`);
  process.on("SIGINT", () => server.close(() => process.exit(0)));
  process.on("SIGTERM", () => server.close(() => process.exit(0)));
}
