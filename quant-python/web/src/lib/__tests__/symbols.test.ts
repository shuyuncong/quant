import { describe, expect, it } from "vitest";
import { normalizeSymbol } from "../symbols";

describe("normalizeSymbol", () => {
  it("adds SH suffix for 6-prefix codes", () => {
    expect(normalizeSymbol("600036")).toBe("600036.SH");
  });
  it("adds SZ suffix for other 6-digit codes", () => {
    expect(normalizeSymbol("000001")).toBe("000001.SZ");
  });
  it("adds BJ suffix for 4/8/9-prefix codes", () => {
    expect(normalizeSymbol("830799")).toBe("830799.BJ");
  });
  it("keeps existing exchange suffix", () => {
    expect(normalizeSymbol("000001.SZ")).toBe("000001.SZ");
  });
  it("converts exchange prefix forms", () => {
    expect(normalizeSymbol("sz000858")).toBe("000858.SZ");
    expect(normalizeSymbol("SH600519")).toBe("600519.SH");
  });
  it("returns non-code input unchanged", () => {
    expect(normalizeSymbol("abc")).toBe("ABC");
  });
});
