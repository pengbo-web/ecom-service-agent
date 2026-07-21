import { useState } from "react";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { adminFetch } from "@/lib/api";

export type Trace = { trace_id: string; started_at: number; session_id?: string; intent?: string; status: string; latency_ms: number; prompt_tokens?: number; completion_tokens?: number };

export function TracesTable({ traces }: { traces: Trace[] }) {
  const [detail, setDetail] = useState<any | null>(null);
  async function open(id: string) {
    const t = await (await adminFetch("/api/traces/" + id)).json();
    setDetail(t);
  }
  return (
    <>
      <div className="overflow-x-auto rounded-lg border">
        <table className="w-full text-sm">
          <thead className="bg-secondary/60 text-muted-foreground">
            <tr>{["时间(相对)", "会话", "意图", "状态", "延迟(ms)", "tokens"].map((h) => <th key={h} className="px-3 py-2 text-left font-medium">{h}</th>)}</tr>
          </thead>
          <tbody>
            {traces.length === 0 && <tr><td colSpan={6} className="px-3 py-6 text-center text-muted-foreground">暂无记录</td></tr>}
            {traces.map((t) => (
              <tr key={t.trace_id} className="cursor-pointer border-t hover:bg-secondary/40" onClick={() => open(t.trace_id)}>
                <td className="px-3 py-2">{t.started_at.toFixed(0)}</td>
                <td className="px-3 py-2" title={t.session_id}>{(t.session_id || "-").slice(0, 12)}</td>
                <td className="px-3 py-2">{t.intent || "-"}</td>
                <td className={"px-3 py-2 " + (t.status === "ok" ? "text-green-600" : "text-destructive")}>{t.status}</td>
                <td className="px-3 py-2">{t.latency_ms.toFixed(0)}</td>
                <td className="px-3 py-2">{(t.prompt_tokens || 0)}+{(t.completion_tokens || 0)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <Dialog open={!!detail} onOpenChange={(o) => !o && setDetail(null)}>
        <DialogContent>
          <DialogTitle>调用链 {detail?.trace_id}</DialogTitle>
          <pre className="mt-3 max-h-[60vh] overflow-auto rounded-md bg-secondary p-3 text-xs">
{detail && `用户: ${detail.user_input}\n意图: ${detail.intent} 状态: ${detail.status}\n\n` +
  (detail?.spans || []).map((s: any) => `[${s.kind}] ${s.name}  ${s.latency_ms.toFixed(0)}ms` +
    (s.success == null ? "" : s.success ? " ✅" : " ❌") +
    (s.prompt_tokens ? `  tok:${s.prompt_tokens}+${s.completion_tokens}` : "")).join("\n")}
          </pre>
        </DialogContent>
      </Dialog>
    </>
  );
}
