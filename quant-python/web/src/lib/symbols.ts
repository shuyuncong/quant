/** Normalize an A-share symbol into `000001.SZ` style (mirrors Python normalize_ts_code). */
export function normalizeSymbol(symbol: string): string {
  const raw = String(symbol ?? "").trim().toUpperCase();
  if (!raw) return "";
  if (raw.includes(".")) {
    const [code, exchange] = raw.split(".", 2);
    if (["SH", "SZ", "BJ"].includes(exchange)) return `${code}.${exchange}`;
  }
  if (/^(SH|SZ|BJ)/.test(raw) && raw.length > 2) {
    return `${raw.slice(2)}.${raw.slice(0, 2)}`;
  }
  if (/^\d{6}$/.test(raw)) {
    if (raw.startsWith("6")) return `${raw}.SH`;
    if (/^[489]/.test(raw)) return `${raw}.BJ`;
    return `${raw}.SZ`;
  }
  return raw;
}
