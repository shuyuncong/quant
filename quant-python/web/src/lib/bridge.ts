import { spawn } from "node:child_process";
import { bridgeScript, signalSystemDir } from "./paths";

export interface BridgeOutcome {
  ok: boolean;
  data?: unknown;
  error?: string;
  code: number;
}

export interface BridgeOptions {
  timeoutMs?: number;
  env?: Record<string, string>;
  /** Cancels the bridge process; the promise still settles exactly once. */
  signal?: AbortSignal;
}

/** Run one bridge command; payload is passed via stdin so secrets never appear on the command line. */
export function runBridge(
  command: string,
  payload: unknown = {},
  options: BridgeOptions = {}
): Promise<BridgeOutcome> {
  const { promise, resolve } = Promise.withResolvers<BridgeOutcome>();
  if (options.signal?.aborted) {
    resolve({ ok: false, error: "桥接进程已取消", code: 1 });
    return promise;
  }
  let child;
  try {
    const pythonBin = process.env.PYTHON_BIN?.trim() || "python";
    child = spawn(/* turbopackIgnore: true */ pythonBin, ["-B", bridgeScript, command, "--payload", "-"], {
      cwd: signalSystemDir,
      windowsHide: true,
      env: { ...process.env, PYTHONIOENCODING: "utf-8", ...options.env },
      stdio: ["pipe", "pipe", "pipe"],
    });
  } catch (error) {
    resolve({ ok: false, error: String(error), code: 1 });
    return promise;
  }

  let stdout = "";
  let stderr = "";
  let settled = false;
  let timer: NodeJS.Timeout | undefined;

  /** Kill Python without caring whether it already exited. */
  const kill = () => {
    try {
      child.kill();
    } catch {
      /* already exited */
    }
  };

  if (options.timeoutMs && options.timeoutMs > 0) {
    timer = setTimeout(kill, options.timeoutMs);
  }

  // Cancellation kills Python; the close handler settles once the process has actually exited.
  const abort = () => kill();
  options.signal?.addEventListener("abort", abort, { once: true });

  const settle = (outcome: BridgeOutcome) => {
    if (settled) return;
    settled = true;
    clearTimeout(timer);
    options.signal?.removeEventListener("abort", abort);
    resolve(outcome);
  };

  child.stdout.on("data", (chunk: Buffer) => {
    stdout += chunk.toString("utf8");
  });
  child.stderr.on("data", (chunk: Buffer) => {
    stderr += chunk.toString("utf8");
  });
  child.on("error", (error) => {
    settle({ ok: false, error: String(error), code: 1 });
  });
  child.on("close", (code, signal) => {
    const exitCode = code ?? (signal ? 1 : 0);
    type BridgeJson = { ok?: boolean; data?: unknown; error?: string; code?: number };
    let parsed: BridgeJson | null = null;
    try {
      parsed = stdout.trim() ? (JSON.parse(stdout.trim()) as BridgeJson) : null;
    } catch {
      parsed = null;
    }
    if (parsed && typeof parsed.ok === "boolean") {
      settle({
        ok: parsed.ok,
        data: parsed.data,
        error: parsed.error,
        code: parsed.code ?? exitCode,
      });
    } else {
      settle({
        ok: false,
        error: stderr.trim() || `桥接进程退出码 ${exitCode}`,
        code: exitCode,
      });
    }
  });

  child.stdin.on("error", () => {
    /* stdin closed before write finished; close handler reports outcome */
  });
  try {
    child.stdin.write(JSON.stringify(payload));
    child.stdin.end();
  } catch {
    /* ignore write errors */
  }
  return promise;
}
