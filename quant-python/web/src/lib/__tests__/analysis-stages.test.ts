import {beforeEach,describe,expect,it,vi} from "vitest";
const mocks=vi.hoisted(()=>({query:vi.fn(),release:vi.fn(),chat:vi.fn()}));
vi.mock('../db',()=>({getDb:async()=>({connect:async()=>({query:mocks.query,release:mocks.release})})}));
vi.mock('../llm',()=>({chatWithFallback:mocks.chat,buildInterpretationContext:()=>'{"indicators":{}}'}));
import {runAnalysisStages} from '../analysis-service';
import type {AnalysisDocument} from '../analysis-types';
function document():AnalysisDocument{return {schema_version:2,symbol:'600036.SH',name:'test',as_of:'2026-09-14 15:20:00',snapshot_hash:'frozen',revision:1,report:{},portfolio:{},strategies:['macd_zero_axis','yearline_pullback','macd_divergence'].map(id=>({strategy_id:id,status:'ok',name:id,version:'1',as_of:null,reference_price:null,exit_rule:'fixed',buy:false,sell:null,buy_conditions:[],sell_conditions:[],warnings:[],parameters:{}})) as AnalysisDocument['strategies'],technical:{status:'failed',content:''},synthesis:{status:'success',content:'old synthesis'}};}
describe('analysis stage recovery',()=>{
  beforeEach(()=>{vi.resetAllMocks();mocks.chat.mockResolvedValue({content:'fresh',model:{name:'stub'}});});
  it('recomputes synthesis after technical recovery using the original snapshot',async()=>{
    const doc=document();
    mocks.query.mockImplementation(async(sql:string)=>sql.includes('pg_try')?{rows:[{acquired:true}]}:sql.startsWith('SELECT *')?{rows:[{document:doc}]}:{rows:[]});
    await runAnalysisStages(7);
    expect(mocks.chat).toHaveBeenCalledTimes(2);
    expect(mocks.chat.mock.calls[1][1].purpose).toBe('synthesis');
    expect(JSON.parse(mocks.chat.mock.calls[1][0][1].content).technical.content).toBe('fresh');
    expect(doc.snapshot_hash).toBe('frozen');expect(doc.revision).toBe(2);
    expect(mocks.release).toHaveBeenCalledOnce();
  });
  it('retains data errors without spending LLM calls',async()=>{
    const doc=document();doc.strategies.forEach(item=>item.status='error');
    mocks.query.mockImplementation(async(sql:string)=>sql.includes('pg_try')?{rows:[{acquired:true}]}:sql.startsWith('SELECT *')?{rows:[{document:doc}]}:{rows:[]});
    await runAnalysisStages(8);expect(mocks.chat).not.toHaveBeenCalled();expect(doc.synthesis.status).toBe('failed');
    expect(mocks.query.mock.calls.find(([sql])=>sql.startsWith('UPDATE'))?.[1][2]).toBe('partial_failed');
  });
  it('does not run a second worker when another process owns the record',async()=>{
    mocks.query.mockResolvedValue({rows:[{acquired:false}]});await runAnalysisStages(9);expect(mocks.chat).not.toHaveBeenCalled();expect(mocks.release).toHaveBeenCalledOnce();
  });
});
