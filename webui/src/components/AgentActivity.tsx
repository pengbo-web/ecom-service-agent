import { useEffect, useState } from "react";
import { Brain, Wrench, ClipboardList, ShieldAlert, ChevronDown, ChevronRight, Route, GitBranch, CheckCircle2, Sparkles, BookOpen, Zap } from "lucide-react";
import type { SSEEvent } from "@/lib/sse";

// 领域路由标签(H1.0:售前/售中/售后)
const ROUTE_LABEL: Record<string, string> = {
  presale: "售前", midsale: "售中", aftersale: "售后",
};
// 功能选择器动作标签(H1.2:LLM ReAct 选择器每步选下一个功能 Agent)
const ROLE_LABEL: Record<string, string> = {
  evaluate: "评估", redraft: "重写", polish: "润色", done: "结束",
};

export function AgentActivity({ events, defaultOpen = false }: { events: SSEEvent[]; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  // 跟随 defaultOpen 变化：流式中展开，收到回复后自动折叠（用户仍可手动切换）
  useEffect(() => { setOpen(defaultOpen); }, [defaultOpen]);
  if (!events.length) return null;
  return (
    <div className="my-1.5 rounded-lg border bg-secondary/50 text-xs">
      <button className="flex w-full items-center gap-1 px-3 py-2 text-muted-foreground" onClick={() => setOpen((o) => !o)}>
        {open ? <ChevronDown className="h-3.5 w-3.5" /> : <ChevronRight className="h-3.5 w-3.5" />}
        🧠 Agent 思考过程（{events.length}）
      </button>
      {open && (
        <div className="flex flex-col gap-1 px-3 pb-2">
          {events.map((e, i) => (
            <div key={i} className="flex items-start gap-1.5">
              {e.type === "thought" && <><Brain className="mt-0.5 h-3.5 w-3.5 text-primary" /><span>{e.content}</span></>}
              {e.type === "tool_call" && <><Wrench className="mt-0.5 h-3.5 w-3.5 text-accent" /><span className="font-mono">{e.name}({JSON.stringify(e.args)})</span></>}
              {e.type === "tool_result" && <><ClipboardList className="mt-0.5 h-3.5 w-3.5 text-muted-foreground" /><span className="font-mono text-muted-foreground">{String(e.content).slice(0, 200)}</span></>}
              {e.type === "guard" && <><ShieldAlert className="mt-0.5 h-3.5 w-3.5 text-destructive" /><span>护栏[{e.stage}/{e.action}] {e.reason}</span></>}
              {e.type === "route" && <><Route className="mt-0.5 h-3.5 w-3.5 text-primary" /><span>路由 → <b>{ROUTE_LABEL[String(e.key)] ?? e.key}</b> Agent{e.intent ? <span className="text-muted-foreground">（{e.intent} · {e.need_kb ? "需检索" : "免检索"}）</span> : null}</span></>}
              {e.type === "select" && <><GitBranch className="mt-0.5 h-3.5 w-3.5 text-accent" /><span>选择器 → <b>{ROLE_LABEL[String(e.next)] ?? e.next}</b><span className="text-muted-foreground">（{e.reason}）</span></span></>}
              {e.type === "evaluate" && <><CheckCircle2 className={`mt-0.5 h-3.5 w-3.5 ${e.ok ? "text-primary" : "text-destructive"}`} /><span>评估{e.ok ? "通过：接地/准确/合规/完整" : "不通过 → 将依据工具真实结果重写"}</span></>}
              {e.type === "polish" && <><Sparkles className="mt-0.5 h-3.5 w-3.5 text-accent" /><span>润色完成（小夕语气，事实原样保留）</span></>}
              {e.type === "recall" && e.skipped && <><BookOpen className="mt-0.5 h-3.5 w-3.5 text-muted-foreground" /><span className="text-muted-foreground">预召回 → 跳过（{e.reason}，无需检索知识库）</span></>}
              {e.type === "recall" && !e.skipped && <><BookOpen className="mt-0.5 h-3.5 w-3.5 text-primary" /><span>预召回<b>[{e.backend === "aperag" ? "ApeRAG" : "本地索引"}]</b>{e.query ? `（查询：${e.query}）` : ""} → 平台知识：{[...new Set((e.hits as { doc: string; section: string }[] ?? []).map(h => `${h.doc}/${h.section}`))].join("、")}</span></>}
              {e.type === "faq_cache" && <><Zap className="mt-0.5 h-3.5 w-3.5 text-accent" /><span>FAQ 秒答（命中：{e.matched}，相似度 {Number(e.score).toFixed(2)}，零 LLM）</span></>}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
