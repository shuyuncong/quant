import { expect, it, vi } from "vitest";
import { createModel, getModel, listModels, updateModel, type DbClient } from "../db";

it("reads saved protocols and defaults profiles without a setting", async () => {
  const query = vi.fn().mockResolvedValue({ rows: [
    { id: 1, protocol_json: '"responses"', reasoning_json: '"xhigh"' }, { id: 2 }, { id: 3, protocol_json: "invalid" },
  ] });
  const models = await listModels({ query } as unknown as DbClient);
  expect(models.map(model => model.protocol)).toEqual(["responses", "chat_completions", "chat_completions"]);
  expect(models.map(model => model.reasoning_effort)).toEqual(["xhigh", "default", "default"]);
  query.mockResolvedValue({ rows: [{ id: 1, protocol_json: '"responses"' }] });
  expect((await getModel(1, { query } as unknown as DbClient))?.protocol).toBe("responses");
});
it("a reasoning-only update persists the selected effort", async () => {
  const query = vi.fn().mockResolvedValue({ rows: [] });
  await updateModel(9, { reasoning_effort: "xhigh" }, { query } as unknown as DbClient);
  expect(query.mock.calls[1][1]).toEqual(["models.reasoning.9", '"xhigh"', expect.any(String)]);
});
it("saves a newly assigned model id and protocol on the supplied transaction", async () => {
  const query = vi.fn().mockResolvedValueOnce({ rows: [{ max: 3 }] })
    .mockResolvedValueOnce({ rows: [{ id: 9 }] }).mockResolvedValue({ rows: [] });
  expect(await createModel({ name: "astra", model: "gpt-6-astra", base_url: "https://models.test/v1", protocol: "responses" }, { query } as unknown as DbClient)).toBe(9);
  expect(query.mock.calls[2][1]).toEqual(["models.protocol.9", '"responses"', expect.any(String)]);
});
it("a protocol-only update writes the setting and timestamp without invalid SQL", async () => {
  const query = vi.fn().mockResolvedValue({ rows: [] });
  await updateModel(9, { protocol: "responses" }, { query } as unknown as DbClient);
  expect(query.mock.calls[0][0]).toContain("SET updated_at = $1");
  expect(query.mock.calls[0][1]).toEqual([expect.any(String), 9]);
  expect(query.mock.calls[1][1]).toEqual(["models.protocol.9", '"responses"', expect.any(String)]);
});
