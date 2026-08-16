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
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Separator } from "@/components/ui/separator";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { RefreshCw } from "lucide-react";

interface NoteRow {
  id: number;
  job_id: number | null;
  symbol: string;
  content: string;
  model: string;
  result_path: string | null;
  created_at: string;
  job_kind: string | null;
}

const KIND_LABEL: Record<string, string> = {
  analyze: "个股分析",
  scan: "日线扫描",
  "daily-scan": "每日扫描",
  "monitor-once": "盘中监控",
  "monitor-cycle": "定时监控",
  "test-notify": "测试通知",
  "dispatch-outbox": "补投队列",
};

function fileName(resultPath: string | null): string {
  if (!resultPath) return "-";
  return resultPath.split(/[\\/]/).pop() ?? resultPath;
}

export default function InterpretationsPage() {
  const [notes, setNotes] = useState<NoteRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<NoteRow | null>(null);

  const loadNotes = useCallback(async () => {
    try {
      const response = await fetch("/api/notes");
      if (!response.ok) throw new Error("加载 AI 解读失败");
      const data = (await response.json()) as { notes: NoteRow[] };
      setNotes(data.notes);
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "加载 AI 解读失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void loadNotes();
    const timer = setInterval(() => {
      void loadNotes();
    }, 5000);
    return () => clearInterval(timer);
  }, [loadNotes]);

  return (
    <div className="flex flex-col gap-6">
      <Card>
        <CardHeader className="flex-row items-center justify-between space-y-0">
          <div>
            <CardTitle>AI 解读</CardTitle>
            <CardDescription>
              分析任务生成结果后自动调用已配置的模型生成解读，解读结果保存在此处，点击可查看全文。
            </CardDescription>
          </div>
          <Button variant="outline" size="sm" onClick={() => void loadNotes()} disabled={loading}>
            <RefreshCw className="size-4" />
            刷新
          </Button>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>ID</TableHead>
                <TableHead>报告</TableHead>
                <TableHead>类型</TableHead>
                <TableHead>模型</TableHead>
                <TableHead>时间</TableHead>
                <TableHead>摘要</TableHead>
                <TableHead className="w-24">操作</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {notes.map((note) => (
                <TableRow key={note.id}>
                  <TableCell className="font-mono text-xs">#{note.id}</TableCell>
                  <TableCell className="font-mono text-xs">{fileName(note.result_path)}</TableCell>
                  <TableCell>
                    <Badge variant="outline">
                      {note.job_kind ? (KIND_LABEL[note.job_kind] ?? note.job_kind) : "-"}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">{note.model || "-"}</TableCell>
                  <TableCell className="text-xs text-muted-foreground">{note.created_at}</TableCell>
                  <TableCell className="max-w-80 truncate text-xs text-muted-foreground">
                    {note.content.replace(/\s+/g, " ").slice(0, 120)}
                  </TableCell>
                  <TableCell>
                    <Button variant="outline" size="sm" onClick={() => setSelected(note)}>
                      查看
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
              {notes.length === 0 && (
                <TableRow>
                  <TableCell colSpan={7} className="text-center text-muted-foreground">
                    {loading
                      ? "加载中..."
                      : "暂无 AI 解读，任务生成结果后会自动生成，也可在结果页手动生成。"}
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <Dialog open={selected !== null} onOpenChange={(open) => !open && setSelected(null)}>
        <DialogContent className="max-h-[85vh] max-w-3xl overflow-y-auto">
          <DialogHeader>
            <DialogTitle>AI 解读 #{selected?.id}</DialogTitle>
            <DialogDescription>
              {selected?.model ? "模型：" + selected.model : "模型：-"}
              {selected?.created_at ? " · " + selected.created_at : ""}
              {selected?.result_path ? " · " + fileName(selected.result_path) : ""}
            </DialogDescription>
          </DialogHeader>
          <Separator />
          <div className="whitespace-pre-wrap text-sm leading-relaxed">{selected?.content}</div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
