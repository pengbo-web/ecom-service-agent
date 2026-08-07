import { useState } from "react";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { adminFetch } from "@/lib/api";

export type Trace = { trace_id: string; started_at: number; session_id?: string; intent?: string; status: string; latency_ms: number; prompt_tokens?: number; completion_tokens?: number };
type Span = {
  span_id?: string;
  kind: string;
  name: string;
  latency_ms?: number;
  success?: boolean | null;
  prompt_tokens?: number;
  completion_tokens?: number;
  // W1:stage 嵌套树的父指针。旧后端/旧数据没有这个键时按顶层处理，
  // 不强求存在——纯新增字段，不破坏对老响应的兼容。
  parent_span_id?: string | null;
};
type Detail = { trace_id?: string; user_input?: string; intent?: string; status?: string; spans?: Span[]; error?: string };

// 安全/失败信号单独给一套醒目配色，不能淹没在一长串普通步骤里：
// workflow_guard = 守卫拦下了一次被跳过的工作流步骤(安全动作)；
// degrade = 模型客户端切到了备选/降级路径(失败信号)。配色沿用 SkillsView
// 的 RiskBadge 同一套语义(红=需要注意的拦截，橙=已发生的降级)。
const HIGHLIGHT_KIND: Record<string, { label: string; cls: string }> = {
  workflow_guard: { label: "⛔ 工作流拦截", cls: "bg-red-500/15 text-red-600 dark:text-red-400" },
  degrade: { label: "⚠️ 降级", cls: "bg-amber-500/15 text-amber-600 dark:text-amber-400" },
};

function childrenOf(spans: Span[], parentId: string | null): Span[] {
  return spans.filter((s) => (s.parent_span_id ?? null) === parentId);
}

// stage 是唯一有真实耗时区间的嵌套阶段，其它都是挂在某个 stage 下的零时长
// 标记——给 stage 一个更显眼的字重，读起来一眼能分出"框"和"点"。
function SpanRow({ span, depth, spans }: { span: Span; depth: number; spans: Span[] }) {
  const highlight = HIGHLIGHT_KIND[span.kind];
  const isStage = span.kind === "stage";
  const kids = span.span_id ? childrenOf(spans, span.span_id) : [];
  return (
    <>
      <div className="flex flex-wrap items-baseline gap-1.5 py-0.5" style={{ paddingLeft: depth * 14 }}>
        {highlight ? (
          <span className={`rounded px-1.5 py-0.5 text-[11px] font-medium ${highlight.cls}`}>{highlight.label}</span>
        ) : (
          <span className="text-muted-foreground">[{span.kind}]</span>
        )}
        <span className={isStage ? "font-medium" : ""}>{span.name}</span>
        <span className="text-muted-foreground">{(span.latency_ms ?? 0).toFixed(0)}ms</span>
        {span.success != null && <span>{span.success ? "✅" : "❌"}</span>}
        {!!span.prompt_tokens && (
          <span className="text-muted-foreground">tok:{span.prompt_tokens}+{span.completion_tokens ?? 0}</span>
        )}
      </div>
      {kids.map((k, i) => <SpanRow key={k.span_id ?? `${depth}-${i}`} span={k} depth={depth + 1} spans={spans} />)}
    </>
  );
}

// 有 parent_span_id 才能重建嵌套；没有该键的旧数据/旧后端一律按顶层平铺，
// 退化成从前那种扁平列表，不会因为缺字段而报错或抛异常。
function SpanTree({ spans }: { spans: Span[] }) {
  const roots = childrenOf(spans, null);
  return <>{roots.map((s, i) => <SpanRow key={s.span_id ?? `root-${i}`} span={s} depth={0} spans={spans} />)}</>;
}

export function TracesTable({ traces }: { traces: Trace[] }) {
  const [detail, setDetail] = useState<Detail | null>(null);
  const [loading, setLoading] = useState(false);
  async function open(id: string) {
    setLoading(true);
    setDetail(null);
    try {
      const t: Detail = await (await adminFetch("/api/traces/" + id)).json();
      setDetail(t);
    } catch {
      setDetail({ error: "加载调用链失败" });
    } finally {
      setLoading(false);
    }
  }
  return (
    <>
      <div className="overflow-x-auto rounded-lg border">
        <table className="w-full text-sm">
          <thead className="bg-secondary/60 text-muted-foreground">
            <tr>{["时间戳", "会话", "意图", "状态", "延迟(ms)", "tokens"].map((h) => <th key={h} className="px-3 py-2 text-left font-medium">{h}</th>)}</tr>
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
      <Dialog open={!!detail || loading} onOpenChange={(o) => !o && setDetail(null)}>
        <DialogContent>
          <DialogTitle>调用链 {detail?.trace_id}</DialogTitle>
          {loading && <div className="mt-3 text-sm text-muted-foreground">加载中…</div>}
          {!loading && detail?.error && (
            <div className="mt-3 text-sm text-destructive">⚠️ {detail.error}</div>
          )}
          {!loading && detail && !detail.error && (
            <div className="mt-3 max-h-[60vh] overflow-auto rounded-md bg-secondary p-3 text-xs">
              <div className="mb-2">
                用户: {detail.user_input ?? "-"}
                <br />
                意图: {detail.intent ?? "-"} 状态: {detail.status ?? "-"}
              </div>
              <SpanTree spans={detail.spans || []} />
            </div>
          )}
        </DialogContent>
      </Dialog>
    </>
  );
}
