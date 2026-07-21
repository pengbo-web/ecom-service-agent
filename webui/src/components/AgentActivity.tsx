import { useEffect, useState } from "react";
import { Brain, Wrench, ClipboardList, ShieldAlert, ChevronDown, ChevronRight } from "lucide-react";
import type { SSEEvent } from "@/lib/sse";

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
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
