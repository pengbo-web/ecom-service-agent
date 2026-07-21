import { useEffect, useState } from "react";
import { getJSON } from "@/lib/api";
import { MetricCards, type Metrics } from "@/components/MetricCards";
import { TracesTable, type Trace } from "@/components/TracesTable";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

export function DashboardView({ sessionId }: { sessionId: string }) {
  const [m, setM] = useState<Metrics | null>(null);
  const [traces, setTraces] = useState<Trace[]>([]);
  const [onlyMine, setOnlyMine] = useState(true);

  async function load() {
    setM(await getJSON<Metrics>("/api/metrics"));
    const url = "/api/traces?limit=50" + (onlyMine ? "&session_id=" + encodeURIComponent(sessionId) : "");
    setTraces(await getJSON<Trace[]>(url));
  }
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [onlyMine]);

  return (
    <div className="h-full overflow-y-auto p-6">
      <div className="mx-auto flex max-w-5xl flex-col gap-5">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold">可观测看板</h2>
          <Button variant="secondary" size="sm" onClick={load}>刷新</Button>
        </div>
        {m ? <MetricCards m={m} /> : <div className="text-muted-foreground">加载中…</div>}
        {m && (
          <div>
            <h3 className="mb-2 text-sm font-medium">意图分布</h3>
            <div className="flex flex-wrap gap-2">
              {Object.entries(m.intent_distribution).map(([k, v]) => <Badge key={k} variant="outline">{k}: {v}</Badge>)}
            </div>
          </div>
        )}
        <div>
          <div className="mb-2 flex items-center gap-3">
            <h3 className="text-sm font-medium">最近请求（点行看调用链）</h3>
            <label className="flex items-center gap-1 text-xs text-muted-foreground">
              <input type="checkbox" checked={onlyMine} onChange={(e) => setOnlyMine(e.target.checked)} /> 只看本会话
            </label>
          </div>
          <TracesTable traces={traces} />
        </div>
      </div>
    </div>
  );
}
