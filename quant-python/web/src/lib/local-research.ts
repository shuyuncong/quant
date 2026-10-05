export function localResearchAvailable(url = process.env.DATABASE_URL): boolean {
  try {
    const target = new URL(url ?? "");
    return ["postgres:", "postgresql:"].includes(target.protocol)
      && ["localhost", "127.0.0.1", "[::1]"].includes(target.hostname)
      && (target.port || "5432") === "5432" && target.pathname === "/quant"
      && !target.search && !target.hash;
  } catch { return false; }
}
export function assertLocalResearch() {
  if (!localResearchAvailable()) throw new Error("回测仅允许在本地研究环境运行，请连接本地 quant-pg（5432）");
}
