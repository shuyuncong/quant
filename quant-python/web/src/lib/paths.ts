import path from "node:path";

export const webRoot = process.cwd();
export const signalSystemDir = path.join(webRoot, "..", "signal_system");
export const bridgeScript = path.join(signalSystemDir, "web_bridge.py");
export const configYaml = path.join(signalSystemDir, "config", "config.yaml");
export const webDataDir = path.join(webRoot, "data");

export function resolvePathWithin(baseDir: string, requestedPath: string): string | null {
  const base = path.resolve(baseDir);
  const full = path.resolve(base, requestedPath);
  const relative = path.relative(base, full);
  if (!relative || relative === ".." || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
    return null;
  }
  return full;
}
