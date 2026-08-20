import { describe, expect, it } from "vitest";
import { nowIso } from "../time";

describe("nowIso", () => {
  it("returns Beijing wall-clock format", () => {
    expect(nowIso()).toMatch(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/);
  });
});

describe.skipIf(process.env.RUN_SUPABASE_TESTS !== "true")("PostgreSQL operation logs", () => {
  it("is covered by npm run db:verify", () => {
    expect(process.env.DATABASE_URL).toBeTruthy();
  });
});
