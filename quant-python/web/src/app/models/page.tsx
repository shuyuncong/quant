"use client";

import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { CheckCircle2, Pencil, Plus, Trash2, XCircle } from "lucide-react";

interface ModelItem {
  id: number;
  name: string;
  base_url: string;
  model: string;
  api_key: string;
  env_key: string;
  proxy: string;
  enabled: boolean;
  vision_supported: boolean;
  env_present?: boolean;
  created_at: string;
  updated_at: string;
}

interface ModelForm {
  name: string;
  base_url: string;
  model: string;
  api_key: string;
  env_key: string;
  proxy: string;
  enabled: boolean;
  vision_supported: boolean;
}

const EMPTY_FORM: ModelForm = {
  name: "",
  base_url: "",
  model: "",
  api_key: "",
  env_key: "",
  proxy: "",
  enabled: true,
  vision_supported: true,
};

export default function ModelsPage() {
  const [models, setModels] = useState<ModelItem[]>([]);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [editingId, setEditingId] = useState<number | null>(null);
  const [form, setForm] = useState<ModelForm>(EMPTY_FORM);
  const [testingId, setTestingId] = useState<number | null>(null);

  const load = useCallback(async () => {
    try {
      const response = await fetch("/api/models");
      if (!response.ok) throw new Error("加载模型失败");
      const data = (await response.json()) as { models: ModelItem[] };
      setModels(data.models);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "加载模型失败");
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
  }, [load]);

  const openAdd = () => {
    setEditingId(null);
    setForm(EMPTY_FORM);
    setDialogOpen(true);
  };

  const openEdit = (model: ModelItem) => {
    setEditingId(model.id);
    setForm({
      name: model.name,
      base_url: model.base_url,
      model: model.model,
      api_key: model.api_key === "****" ? "****" : "",
      env_key: model.env_key,
      proxy: model.proxy,
      enabled: model.enabled,
      vision_supported: model.vision_supported,
    });
    setDialogOpen(true);
  };

  const save = async () => {
    if (!form.name.trim() || !form.base_url.trim() || !form.model.trim()) {
      toast.error("名称、Base URL、模型名必填");
      return;
    }
    const body = {
      ...form,
      api_key: form.api_key === "" ? "" : form.api_key,
    };
    try {
      const url = editingId ? `/api/models/${editingId}` : "/api/models";
      const response = await fetch(url, {
        method: editingId ? "PUT" : "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = (await response.json().catch(() => ({}))) as { error?: string };
      if (!response.ok) throw new Error(data.error || "保存失败");
      toast.success(editingId ? "模型已更新" : "模型已创建");
      setDialogOpen(false);
      void load();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "保存失败");
    }
  };

  const remove = async (model: ModelItem) => {
    if (!window.confirm(`确认删除模型 ${model.name}？`)) return;
    try {
      const response = await fetch(`/api/models/${model.id}`, { method: "DELETE" });
      if (!response.ok) throw new Error("删除失败");
      toast.success("模型已删除");
      void load();
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "删除失败");
    }
  };

  const test = async (model: ModelItem) => {
    setTestingId(model.id);
    try {
      const response = await fetch(`/api/models/${model.id}/test`, { method: "POST" });
      const data = (await response.json()) as { ok?: boolean; detail?: string };
      if (data.ok) {
        toast.success(`连通性正常：${data.detail}`);
      } else {
        toast.error(`连通性失败：${data.detail ?? "未知错误"}`);
      }
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "测试失败");
    } finally {
      setTestingId(null);
    }
  };

  return (
    <div className="flex max-w-4xl flex-col gap-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold">模型配置</h1>
          <p className="text-sm text-muted-foreground">
            支持 1~N 个 OpenAI 兼容接口（/chat/completions）。启用且配置了 Key 的模型用于 AI 解读与图片识别。
          </p>
        </div>
        <Button onClick={openAdd}><Plus className="size-4" /> 新增模型</Button>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>模型列表</CardTitle>
          <CardDescription>API Key 只在保存时写入，回显一律脱敏；设置了环境变量名时优先读环境变量。</CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>名称</TableHead>
                <TableHead>Base URL</TableHead>
                <TableHead>模型</TableHead>
                <TableHead>状态</TableHead>
                <TableHead>视觉</TableHead>
                <TableHead>Key</TableHead>
                <TableHead className="w-44">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {models.map((model) => (
                <TableRow key={model.id}>
                  <TableCell className="font-medium">{model.name}</TableCell>
                  <TableCell className="max-w-56 truncate font-mono text-xs">{model.base_url}</TableCell>
                  <TableCell className="font-mono text-xs">{model.model}</TableCell>
                  <TableCell>
                    {model.enabled ? (
                      <Badge><CheckCircle2 className="size-3" /> 启用</Badge>
                    ) : (
                      <Badge variant="outline"><XCircle className="size-3" /> 停用</Badge>
                    )}
                  </TableCell>
                  <TableCell>{model.vision_supported ? "支持" : "-"}</TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {model.env_present ? `环境变量 ${model.env_key}` : model.api_key ? "已配置" : "未配置"}
                  </TableCell>
                  <TableCell>
                    <div className="flex gap-1">
                      <Button variant="outline" size="sm" onClick={() => void test(model)} disabled={testingId === model.id}>
                        {testingId === model.id ? "测试中" : "测试"}
                      </Button>
                      <Button variant="ghost" size="sm" onClick={() => openEdit(model)}>
                        <Pencil className="size-3.5" />
                      </Button>
                      <Button variant="ghost" size="sm" onClick={() => void remove(model)}>
                        <Trash2 className="size-3.5" />
                      </Button>
                    </div>
                  </TableCell>
                </TableRow>
              ))}
              {models.length === 0 && (
                <TableRow>
                  <TableCell colSpan={7} className="text-center text-muted-foreground">
                    暂无模型，点击右上角新增
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <Dialog open={dialogOpen} onOpenChange={setDialogOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{editingId ? "编辑模型" : "新增模型"}</DialogTitle>
            <DialogDescription>OpenAI 兼容接口配置，支持任意 base_url + model + api_key。</DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-3">
            <div className="flex flex-col gap-1.5">
              <Label>名称</Label>
              <Input value={form.name} onChange={(event) => setForm((prev) => ({ ...prev, name: event.target.value }))} placeholder="例如：本地 DeepSeek" />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label>Base URL</Label>
              <Input value={form.base_url} onChange={(event) => setForm((prev) => ({ ...prev, base_url: event.target.value }))} placeholder="https://api.deepseek.com/v1" />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label>模型名</Label>
              <Input value={form.model} onChange={(event) => setForm((prev) => ({ ...prev, model: event.target.value }))} placeholder="deepseek-chat" />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label>API Key（保存后不回显；填 **** 表示保持不变）</Label>
              <Input type="password" value={form.api_key} onChange={(event) => setForm((prev) => ({ ...prev, api_key: event.target.value }))} placeholder="sk-..." />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label>环境变量名（可选，存在时优先于页面 Key）</Label>
              <Input value={form.env_key} onChange={(event) => setForm((prev) => ({ ...prev, env_key: event.target.value }))} placeholder="DEEPSEEK_API_KEY" />
            </div>
            <div className="flex flex-col gap-1.5">
              <Label>代理地址（可选，通过代理访问模型接口）</Label>
              <Input value={form.proxy} onChange={(event) => setForm((prev) => ({ ...prev, proxy: event.target.value }))} placeholder="http://127.0.0.1:7890" />
            </div>
            <div className="flex items-center gap-6">
              <div className="flex items-center gap-2">
                <Switch checked={form.enabled} onCheckedChange={(value) => setForm((prev) => ({ ...prev, enabled: value }))} />
                <Label>启用</Label>
              </div>
              <div className="flex items-center gap-2">
                <Switch checked={form.vision_supported} onCheckedChange={(value) => setForm((prev) => ({ ...prev, vision_supported: value }))} />
                <Label>支持图片识别</Label>
              </div>
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDialogOpen(false)}>取消</Button>
            <Button onClick={() => void save()}>保存</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
