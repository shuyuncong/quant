import { describe, expect, it } from "vitest";
import { shouldAutoInterpret } from "../jobs";

describe("shouldAutoInterpret", () => {
  it("always interprets user-triggered kinds", () => {
    expect(shouldAutoInterpret("analyze", undefined)).toBe(true);
    expect(shouldAutoInterpret("scan", { completed_round: false, new_events: 0 })).toBe(true);
    expect(shouldAutoInterpret("monitor-once", { new_events: 0 })).toBe(true);
  });

  it("interprets daily-scan only after the full round completes", () => {
    expect(shouldAutoInterpret("daily-scan", { completed_round: true, candidate_count: 3 })).toBe(true);
    expect(shouldAutoInterpret("daily-scan", { completed_round: false, candidate_count: 3 })).toBe(false);
    expect(shouldAutoInterpret("daily-scan", { completed_round: true, candidate_count: 0 })).toBe(false);
    expect(shouldAutoInterpret("daily-scan", undefined)).toBe(false);
  });

  it("keeps monitor-cycle interpretation gated on new events", () => {
    expect(shouldAutoInterpret("monitor-cycle", { new_events: 1 })).toBe(true);
    expect(shouldAutoInterpret("monitor-cycle", { new_events: 0 })).toBe(false);
  });
});
