import { describe, expect, it } from "vitest";
import { shouldAutoInterpret } from "../jobs";

describe("shouldAutoInterpret", () => {
  it("interprets the kinds that do not produce an AI conclusion themselves", () => {
    expect(shouldAutoInterpret("scan", { completed_round: false, new_events: 0 })).toBe(true);
  });

  it("does not re-interpret kinds whose own pipeline already wrote a note and pushed", () => {
    // analyze / monitor-once / monitor-cycle 走五页签批次：自己写 note 并推送。
    // 再起子任务解读就是同一份内容读第二遍、再推一条一模一样的消息。
    expect(shouldAutoInterpret("analyze", undefined)).toBe(false);
    expect(shouldAutoInterpret("monitor-once", { new_events: 0 })).toBe(false);
    expect(shouldAutoInterpret("monitor-once", { new_events: 5 })).toBe(false);
    expect(shouldAutoInterpret("monitor-cycle", { new_events: 1 })).toBe(false);
  });

  it("interprets daily-scan only after the full round completes", () => {
    expect(shouldAutoInterpret("daily-scan", { completed_round: true, candidate_count: 3 })).toBe(true);
    expect(shouldAutoInterpret("daily-scan", { completed_round: false, candidate_count: 3 })).toBe(false);
    expect(shouldAutoInterpret("daily-scan", { completed_round: true, candidate_count: 0 })).toBe(false);
    expect(shouldAutoInterpret("daily-scan", undefined)).toBe(false);
  });

  it("never auto-interprets the yearline research scan", () => {
    expect(
      shouldAutoInterpret("scan", { pool_type: "yearline_pullback", candidate_count: 3 })
    ).toBe(false);
    // 其他池(或不带 pool_type)的扫描保持原有行为
    expect(shouldAutoInterpret("scan", { pool_type: "macd_zero_axis" })).toBe(true);
    expect(shouldAutoInterpret("scan", undefined)).toBe(true);
    expect(
      shouldAutoInterpret("daily-scan", { pool_type: "yearline_pullback", completed_round: true, candidate_count: 3 })
    ).toBe(false);
  });

  it("never auto-interprets the macd_divergence research scan", () => {
    expect(
      shouldAutoInterpret("scan", { pool_type: "macd_divergence", candidate_count: 3 })
    ).toBe(false);
    expect(
      shouldAutoInterpret("daily-scan", {
        pool_type: "macd_divergence",
        completed_round: true,
        candidate_count: 3,
      })
    ).toBe(false);
    expect(shouldAutoInterpret("scan", { pool_type: "macd_zero_axis" })).toBe(true);
  });
});
