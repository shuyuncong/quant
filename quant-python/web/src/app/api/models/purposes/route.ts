import { NextResponse } from "next/server";
import { getDb, getSetting, listModels, setSetting } from "@/lib/db";
const purposes = ["technical", "synthesis"] as const;
export async function GET() {
  return NextResponse.json(Object.fromEntries(await Promise.all(purposes.map(async purpose => [purpose, Number(await getSetting(`models.purpose.${purpose}`)) || 0]))));
}
export async function PUT(request: Request) {
  const body = await request.json().catch(() => null);
  if (!body || typeof body !== "object") return NextResponse.json({error:"请求体必须是 JSON"},{status:400});
  const models = await listModels();
  for (const purpose of purposes) {
    const id = body[purpose];
    if (!Number.isInteger(id) || (id !== 0 && !models.some(model=>model.id===id&&model.enabled))) return NextResponse.json({error:"请选择已启用的模型或自动降级链"},{status:422});
  }
  const client = await (await getDb()).connect();
  try {
    await client.query("BEGIN");
    for (const purpose of purposes) await setSetting(`models.purpose.${purpose}`,body[purpose],client);
    await client.query("COMMIT");
  } catch(error) {await client.query("ROLLBACK");throw error;} finally {client.release();}
  return NextResponse.json({ok:true});
}
