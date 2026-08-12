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
  const [err, setErr] = useState<string | null>(null);
  // 统计窗口。默认 24 小时而不是全部历史:全历史口径会让**已经修好的问题
  // 永远显示为红色**——修完之后新调用全成功,累计值却被几百条旧失败压着。
  // 「全部」保留为一个选项(排查长期趋势时要用),但不该是默认看到的那一个。
  const [windowHours, setWindowHours] = useState<number>(24);

  async function load() {
    try {
      setErr(null);
      setM(await getJSON<Metrics>(`/api/metrics?window_hours=${windowHours}`));
      // 表格必须跟卡片同一个窗口:上面那段注释讲的"全历史口径会让已经修好的问题
      // 永远显示为红色",对这张表同样成立,而且更容易骗人——排查时选「近 1 小时」,
      // 点开表格里某一行看调用链,拿到的却是 40 小时前的那次(走查实测)。
      const url = `/api/traces?limit=50&window_hours=${windowHours}`
        + (onlyMine ? "&session_id=" + encodeURIComponent(sessionId) : "");
      setTraces(await getJSON<Trace[]>(url));
    } catch (e: any) {
      const msg = String(e?.message || "");
      setErr(
        msg.includes("401") || msg.includes("403")
          ? "无权访问看板（服务端已启用 ADMIN_TOKEN）。在浏览器控制台执行 localStorage.setItem('admin_token','你的令牌') 后点「刷新」。"
          : "加载失败：" + (msg || "服务不可用，请确认后端已启动")
      );
    }
  }
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [onlyMine, windowHours]);

  const WINDOWS: { label: string; hours: number }[] = [
    { label: "近 1 小时", hours: 1 },
    { label: "近 24 小时", hours: 24 },
    { label: "近 7 天", hours: 24 * 7 },
    { label: "全部", hours: 0 },
  ];

  return (
    <div className="h-full overflow-y-auto p-6">
      <div className="mx-auto flex max-w-5xl flex-col gap-5">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="text-lg font-semibold">可观测看板</h2>
          <div className="flex items-center gap-1.5">
            {WINDOWS.map((w) => (
              <button
                key={w.hours}
                onClick={() => setWindowHours(w.hours)}
                className={`rounded px-2 py-1 text-xs transition-colors ${
                  w.hours === windowHours
                    ? "bg-primary text-primary-foreground"
                    : "bg-secondary text-muted-foreground hover:bg-muted"}`}
              >
                {w.label}
              </button>
            ))}
            <Button variant="secondary" size="sm" onClick={load}>刷新</Button>
          </div>
        </div>
        {err ? (
          <div className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">{err}</div>
        ) : m ? (
          <MetricCards m={m} />
        ) : (
          <div className="text-muted-foreground">加载中…</div>
        )}
        {m && (
          <div>
            <h3 className="mb-2 text-sm font-medium">意图分布</h3>
            <div className="flex flex-wrap gap-2">
              {/* `|| {}`:字段缺失时 Object.entries(undefined) 会抛错,把整页打成白屏
                  ——而看板正是排故障时要看的那一页(与 MetricCards 的格式化兜底同一条)。 */}
              {Object.entries(m.intent_distribution || {}).map(([k, v]) => <Badge key={k} variant="outline">{k}: {v}</Badge>)}
            </div>
          </div>
        )}
        <div>
          <div className="mb-2 flex items-center gap-3">
            <h3 className="text-sm font-medium">最近请求（点行看调用链）</h3>
            <label className="flex items-center gap-1 text-xs text-muted-foreground">
              <input type="checkbox" checked={onlyMine} onChange={(e) => setOnlyMine(e.target.checked)} /> 只看本会话
            </label>
            {/* 截断要说出来。窗口对齐之后仍有一处对不上:选「近 7 天」时卡片 145 条、
                表格 50 行——那是 limit=50 截的。表格封顶本身正常,但标题只写「最近请求」
                时,运维照旧会觉得数字算错了。少给了东西就要说,与 anomaly_scope /
                reflow 的丢弃披露同一条纪律。
                只看本会话时卡片是全量口径、表格是单会话,两者本就不该相等,故不显示。 */}
            {!onlyMine && m && traces.length >= 50 && traces.length < m.total_traces && (
              <span className="text-xs text-amber-700 dark:text-amber-400"
                    data-testid="traces-truncated">
                仅显示最近 {traces.length} 条，窗口内共 {m.total_traces} 条
              </span>
            )}
          </div>
          <TracesTable traces={traces} />
        </div>
      </div>
    </div>
  );
}
