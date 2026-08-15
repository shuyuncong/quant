import { NextResponse } from "next/server";
import {
  getEffectiveConfig,
  shouldKeepExistingSecret,
  type ConfigSection,
  validateSection,
} from "@/lib/config";
import { setSetting } from "@/lib/db";

const VALID_SECTIONS = new Set(["strategies", "notification"]);

function sectionFrom(raw: string): ConfigSection | null {
  return VALID_SECTIONS.has(raw) ? (raw as ConfigSection) : null;
}

export async function GET(_request: Request, { params }: { params: Promise<{ section: string }> }) {
  const { section: raw } = await params;
  const section = sectionFrom(raw);
  if (!section) {
    return NextResponse.json({ error: "未知配置分区" }, { status: 400 });
  }
  try {
    const effective = await getEffectiveConfig();
    return NextResponse.json({ section, config: effective.config, settings: effective.settings });
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "获取配置失败" },
      { status: 500 }
    );
  }
}

export async function PUT(request: Request, { params }: { params: Promise<{ section: string }> }) {
  const { section: raw } = await params;
  const section = sectionFrom(raw);
  if (!section) {
    return NextResponse.json({ error: "未知配置分区" }, { status: 400 });
  }
  let body: Record<string, unknown>;
  try {
    body = (await request.json()) as Record<string, unknown>;
  } catch {
    return NextResponse.json({ error: "请求体必须是 JSON" }, { status: 400 });
  }
  const { ok, errors, normalized } = validateSection(section, body);
  if (!ok) {
    return NextResponse.json({ error: "配置校验失败", errors }, { status: 422 });
  }
  for (const [key, value] of Object.entries(normalized)) {
    if (shouldKeepExistingSecret(key, value)) {
      continue; // "****" 表示保留数据库中已有密钥
    }
    setSetting(key, value);
  }
  const effective = await getEffectiveConfig(true);
  return NextResponse.json({ ok: true, settings: effective.settings });
}
