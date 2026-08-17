import path from "node:path";
import { describe, expect, it } from "vitest";
import { resolvePathWithin } from "../paths";

describe("resolvePathWithin", () => {
  it("accepts files below the configured directory", () => {
    const base = path.resolve("output");
    expect(resolvePathWithin(base, "analysis.json")).toBe(path.join(base, "analysis.json"));
  });

  it("rejects parent traversal and sibling-prefix paths", () => {
    const base = path.resolve("output");
    expect(resolvePathWithin(base, "../output-archive/secret.json")).toBeNull();
    expect(resolvePathWithin(base, "..")).toBeNull();
  });
});
