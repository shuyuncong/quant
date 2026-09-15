import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/db", () => ({
  listModels: vi.fn(),
  reorderModels: vi.fn(),
}));

import { listModels, reorderModels } from "@/lib/db";
import { PUT } from "./route";

const listModelsMock = vi.mocked(listModels);
const reorderModelsMock = vi.mocked(reorderModels);

describe("PUT /api/models/reorder", () => {
  beforeEach(() => {
    listModelsMock.mockReset();
    reorderModelsMock.mockReset();
    listModelsMock.mockResolvedValue([
      { id: 1, name: "a", priority: 1 } as never,
      { id: 2, name: "b", priority: 0 } as never,
      { id: 3, name: "c", priority: 2 } as never,
    ]);
  });

  it("按 ids 顺序重排全部模型", async () => {
    const response = await PUT(
      new Request("http://localhost/api/models/reorder", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids: [3, 1, 2] }),
      })
    );
    expect(response.status).toBe(200);
    expect(reorderModelsMock).toHaveBeenCalledWith([3, 1, 2]);
  });

  it("拒绝非整数或空 ids", async () => {
    const response = await PUT(
      new Request("http://localhost/api/models/reorder", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids: [1, "x"] }),
      })
    );
    expect(response.status).toBe(422);
    expect(reorderModelsMock).not.toHaveBeenCalled();
  });

  it("拒绝缺模型的部分排序", async () => {
    const response = await PUT(
      new Request("http://localhost/api/models/reorder", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids: [1, 2] }),
      })
    );
    expect(response.status).toBe(422);
    expect(reorderModelsMock).not.toHaveBeenCalled();
  });

  it("拒绝包含不存在模型 id", async () => {
    const response = await PUT(
      new Request("http://localhost/api/models/reorder", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids: [1, 2, 99] }),
      })
    );
    expect(response.status).toBe(422);
    expect(reorderModelsMock).not.toHaveBeenCalled();
  });

  it("拒绝重复 id", async () => {
    const response = await PUT(
      new Request("http://localhost/api/models/reorder", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ids: [1, 1, 2, 3] }),
      })
    );
    expect(response.status).toBe(422);
    expect(reorderModelsMock).not.toHaveBeenCalled();
  });

  it("body 非法时返回 400 且不触碰数据库", async () => {
    const response = await PUT(new Request("http://localhost/api/models/reorder", { method: "PUT" }));
    expect(response.status).toBe(400);
    expect(reorderModelsMock).not.toHaveBeenCalled();
  });
});