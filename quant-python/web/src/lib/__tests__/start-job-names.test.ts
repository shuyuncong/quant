import { beforeEach, describe, expect, it, vi } from "vitest";
const mocks=vi.hoisted(()=>({analysis:vi.fn(),scan:vi.fn()}));
vi.mock("../analysis-service",()=>({startAnalysisBatch:mocks.analysis}));
vi.mock("../scan-service",()=>({startScanBatch:mocks.scan}));
import { startJob } from "../jobs";
describe("unified job routing",()=>{
  beforeEach(()=>{vi.resetAllMocks();mocks.analysis.mockResolvedValue(42);mocks.scan.mockResolvedValue(43);});
  it("routes manual and scheduled analysis through the same portfolio snapshot pipeline",async()=>{
    for(const kind of ["analyze","monitor-once","monitor-cycle"] as const){
      const input={symbols:["600036"],notify:false};
      await expect(startJob(kind,input)).resolves.toBe(42);
      expect(mocks.analysis).toHaveBeenCalledWith(kind,input);
    }
    expect(mocks.scan).not.toHaveBeenCalled();
  });
  it("preserves scope and scheduled identity when preparing a job",async()=>{
    const input={scope:{holdings:true,watchlist:false,pools:[],symbols:[]},existing_job_id:9};
    await startJob("monitor-cycle",input);
    expect(mocks.analysis).toHaveBeenCalledWith("monitor-cycle",input);
  });
  it("routes daily and manual scans through the same resumable service",async()=>{
    for(const kind of ["scan","daily-scan"] as const){
      await expect(startJob(kind,{scan_kind:"all"})).resolves.toBe(43);
      expect(mocks.scan).toHaveBeenCalledWith(kind,{scan_kind:"all"});
    }
  });
});
