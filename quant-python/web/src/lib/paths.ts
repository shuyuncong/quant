import path from "node:path";

export const webRoot = process.cwd();
export const signalSystemDir = path.join(webRoot, "..", "signal_system");
export const bridgeScript = path.join(signalSystemDir, "web_bridge.py");
export const configYaml = path.join(signalSystemDir, "config", "config.yaml");
export const webDataDir = path.join(webRoot, "data");
