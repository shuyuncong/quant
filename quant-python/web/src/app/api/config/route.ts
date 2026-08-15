import { NextResponse } from "next/server";
import { getEffectiveConfig } from "@/lib/config";

export async function GET() {
  try {
    const effective = await getEffectiveConfig();
    return NextResponse.json(effective);
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "获取配置失败" },
      { status: 500 }
    );
  }
}
