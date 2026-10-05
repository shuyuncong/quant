import { describe,it,expect } from "vitest";
import { localResearchAvailable } from "../local-research";
describe("research environment gate",()=>{
  it("accepts only the local quant database and rejects tunnel and host overrides",()=>{
    expect(localResearchAvailable("postgresql://quant:secret@127.0.0.1:5432/quant")).toBe(true);
    for(const url of ["postgresql://q:s@127.0.0.1:15432/quant","postgresql://q:s@db.example:5432/quant","postgresql://q:s@localhost/quant?host=db.example","postgresql://q:s@localhost/other"]){expect(localResearchAvailable(url)).toBe(false);}
  });
});
