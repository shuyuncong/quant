import { beforeEach, expect, it, vi } from "vitest";
vi.mock("@/lib/db", () => ({ createModel: vi.fn(), listModels: vi.fn(), getModel: vi.fn(), updateModel: vi.fn(), deleteModel: vi.fn() }));
import { createModel, getModel, listModels, updateModel } from "@/lib/db";
import { POST, GET } from "./route";
import { PUT } from "./[id]/route";
const request = (body: unknown, method = "POST") => new Request("http://localhost/api/models", {
  method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
});
beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(createModel).mockResolvedValue(7);
  vi.mocked(getModel).mockResolvedValue({ id: 7, base_url: "https://models.test/v1", protocol: "chat_completions" } as never);
});
it("persists protocol on create and returns it without exposing the key", async () => {
  const response = await POST(request({ name: "astra", base_url: "https://models.test/v1", model: "gpt-6-astra", protocol: "responses" }));
  expect(response.status).toBe(201);
  expect(createModel).toHaveBeenCalledWith(expect.objectContaining({ protocol: "responses" }));
  vi.mocked(listModels).mockResolvedValue([{ id: 7, protocol: "responses", api_key: "secret" } as never]);
  expect((await (await GET()).json()).models[0]).toMatchObject({ protocol: "responses", api_key: "****" });
});
it("defaults old clients to Chat Completions", async () => {
  await POST(request({ name: "old", base_url: "https://models.test/v1", model: "old" }));
  expect(createModel).toHaveBeenCalledWith(expect.objectContaining({ protocol: "chat_completions" }));
});
it("supports a protocol-only edit without overwriting the API key", async () => {
  const response = await PUT(request({ protocol: "responses", api_key: "****" }, "PUT"), { params: Promise.resolve({ id: "7" }) });
  expect(response.status).toBe(200);
  expect(updateModel).toHaveBeenCalledWith(7, { protocol: "responses" });
});
it("accepts reasoning effort and rejects unsupported values", async () => {
  const context = { params: Promise.resolve({ id: "7" }) };
  expect((await PUT(request({ reasoning_effort: "xhigh" }, "PUT"), context)).status).toBe(200);
  expect(updateModel).toHaveBeenCalledWith(7, { reasoning_effort: "xhigh" });
  vi.mocked(updateModel).mockClear();
  expect((await PUT(request({ reasoning_effort: "unknown" }, "PUT"), context)).status).toBe(422);
  expect(updateModel).not.toHaveBeenCalled();
});
it.each(["unknown", "", 42, {}, null])("rejects unsupported protocols %j", async (protocol) => {
  expect((await POST(request({ name: "x", base_url: "https://models.test/v1", model: "x", protocol }))).status).toBe(422);
  expect((await PUT(request({ protocol }, "PUT"), { params: Promise.resolve({ id: "7" }) })).status).toBe(422);
  expect(createModel).not.toHaveBeenCalled();
  expect(updateModel).not.toHaveBeenCalled();
});
