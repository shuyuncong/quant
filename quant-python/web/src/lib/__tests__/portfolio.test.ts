import {beforeEach,afterEach,describe,expect,it,vi} from 'vitest';
import {createHash} from 'node:crypto';
const mocks=vi.hoisted(()=>({query:vi.fn(),release:vi.fn()}));
vi.mock('../db',()=>({getDb:async()=>({connect:async()=>({query:mocks.query,release:mocks.release})})}));
import {recordHoldingTrade} from '../portfolio';
const input={symbol:'600036.SH',side:'sell' as const,quantity:100,price:'10',fees:'0',traded_at:'2026-10-05 10:00:00',request_key:'test-request-0000001'};
describe('portfolio transaction boundary',()=>{
  beforeEach(()=>{vi.resetAllMocks();vi.useFakeTimers();vi.setSystemTime(new Date('2026-10-05T12:00:00+08:00'));});
  afterEach(()=>vi.useRealTimers());
  it('rejects nonexistent dates before opening a transaction',async()=>{await expect(recordHoldingTrade({...input,traded_at:'2026-02-30 10:00:00'})).rejects.toThrow('不存在');expect(mocks.query).not.toHaveBeenCalled();});
  it('rolls back a T+1 violation without changing holdings or creating a trade',async()=>{
    mocks.query.mockImplementation(async(sql:string)=>({rows:sql.includes('SELECT * FROM quant.holdings')?[{symbol:input.symbol,shares:100,cost_price:'10',total_amount:'1000',version:1}]:sql.includes('SUM(quantity)')?[{bought:100}]:[]}));
    await expect(recordHoldingTrade(input)).rejects.toThrow('T+1');
    expect(mocks.query.mock.calls.some(([sql])=>sql.startsWith('UPDATE')||sql.startsWith('INSERT'))).toBe(false);
    expect(mocks.query).toHaveBeenCalledWith('ROLLBACK');expect(mocks.release).toHaveBeenCalledOnce();
  });
  it('reuses a confirmed request after the position was fully closed',async()=>{
    const request_hash=createHash('sha256').update(JSON.stringify({...input,price:'10.00000000',fees:'0.00000000',note:''})).digest('hex');
    mocks.query.mockImplementation(async(sql:string)=>({rows:sql.includes('SELECT * FROM quant.holdings')?[{shares:0}]:sql.includes('request_key=$1')?[{id:12,request_hash}]:[]}));
    const result=await recordHoldingTrade(input);expect(result.reused).toBe(true);expect(result.trade.id).toBe(12);
    expect(mocks.query.mock.calls.some(([sql])=>sql.startsWith('UPDATE')||sql.startsWith('INSERT'))).toBe(false);
  });
});
