"use client";

import { useEffect, useState } from "react";
import { toast } from "sonner";
import { BrainCircuit } from "lucide-react";
import { Button } from "./ui/button";

export function ModelPurposeSettings({
  models,
}: {
  models: { id: number; name: string; enabled: boolean }[];
}) {
  const [values, setValues] = useState({ technical: 0, synthesis: 0 });
  const [ready, setReady] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    const abort = new AbortController();
    void fetch("/api/models/purposes", { signal: abort.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error("加载用途设置失败");
        setValues(await response.json());
        setReady(true);
      })
      .catch((error) => {
        if (!abort.signal.aborted) toast.error(String(error));
      });
    return () => abort.abort();
  }, []);

  async function save() {
    setSaving(true);
    try {
      const response = await fetch("/api/models/purposes", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(values),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error);
      toast.success("模型用途已保存");
    } catch (error) {
      toast.error(String(error));
    } finally {
      setSaving(false);
    }
  }

  const purposeOptions = [
    ["technical", "AI 技术分析"],
    ["synthesis", "综合结论"],
  ] as const;

  return (
    <section className="space-y-3 rounded-xl border bg-card p-4 shadow-xs">
      <div className="flex items-center gap-2">
        <BrainCircuit className="size-4 text-primary" />
        <div>
          <h2 className="text-sm font-semibold">分析模型用途分工</h2>
          <p className="text-xs text-muted-foreground">
            每只股票调用技术分析与综合归纳各一次。首选失败时继续自动使用现有模型降级链。
          </p>
        </div>
      </div>

      <div className="flex flex-wrap items-end gap-4 pt-1">
        {purposeOptions.map(([key, label]) => (
          <label key={key} className="grid gap-1.5 text-xs font-medium">
            <span>{label}</span>
            <select
              className="h-9 min-w-48 rounded-lg border border-input bg-background px-3 text-sm focus-visible:outline-2 focus-visible:outline-ring"
              disabled={!ready || saving}
              value={values[key]}
              onChange={(event) =>
                setValues({ ...values, [key]: Number(event.target.value) })
              }
            >
              <option value={0}>自动（按优先级排序首选）</option>
              {models
                .filter((model) => model.enabled)
                .map((model) => (
                  <option key={model.id} value={model.id}>
                    {model.name}
                  </option>
                ))}
            </select>
          </label>
        ))}

        <Button
          size="sm"
          className="h-9 px-4 text-xs"
          disabled={!ready || saving}
          onClick={() => void save()}
        >
          {saving ? "保存中…" : "保存用途设置"}
        </Button>
      </div>
    </section>
  );
}
