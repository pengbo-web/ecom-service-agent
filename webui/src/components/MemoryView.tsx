import { useEffect, useState } from "react";
import { getMemory, consolidateMemory, type MemorySnapshot } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card } from "@/components/ui/card";
import { RefreshCw, Brain } from "lucide-react";

const CAT_LABEL: Record<string, string> = {
  identity: "身份", preference: "偏好", behavior: "行为", issue: "问题", other: "其他",
};

export function MemoryView({ sessionId }: { sessionId: string }) {
  const [snap, setSnap] = useState<MemorySnapshot | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function load() {
    setErr(null);
    try { setSnap(await getMemory()); }
    catch (e) { setErr(String(e)); }
  }
  useEffect(() => { load(); /* eslint-disable-next-line */ }, []);

  async function consolidateNow() {
    setBusy(true); setErr(null);
    try { await consolidateMemory(sessionId); await load(); }
    catch (e) { setErr(String(e)); }
    finally { setBusy(false); }
  }

  return (
    <div className="h-full overflow-y-auto p-6">
      <div className="mx-auto flex max-w-3xl flex-col gap-4">
        <div className="flex items-center gap-2">
          <Brain className="h-5 w-5 text-primary" />
          <h2 className="text-lg font-semibold">长期记忆</h2>
          {snap && (
            <Badge variant={snap.curation ? "primary" : "outline"}>
              策展{snap.curation ? "已开启" : "未开启"}
            </Badge>
          )}
          <span className="ml-auto flex items-center gap-2">
            <Button variant="outline" size="sm" onClick={load}>
              <RefreshCw className="h-4 w-4" /> 刷新
            </Button>
            <Button size="sm" disabled={busy} onClick={consolidateNow}>
              {busy ? "巩固中…" : "从当前会话巩固一次"}
            </Button>
          </span>
        </div>

        <p className="text-xs text-muted-foreground">
          这里是跨会话持久化的用户画像（存于 <code>{snap?.user_id ? `memory/${snap.user_id}.json` : "服务端"}</code>）。
          生产环境会在会话空闲后自动巩固；也可点右上角手动把当前聊天巩固进来。
        </p>

        {err && <div className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">加载失败：{err}</div>}

        {snap && (
          <>
            <div>
              <div className="mb-2 text-sm font-medium">记忆事实 · {snap.count} 条</div>
              {snap.count === 0 ? (
                <div className="text-sm text-muted-foreground">
                  暂无长期记忆。去「聊天」多说几句偏好/身份（如「我是钻石会员，偏好红色运动鞋」），
                  然后回来点「从当前会话巩固一次」。
                </div>
              ) : (
                <div className="flex flex-col gap-2">
                  {snap.facts.map((f, i) => (
                    <Card key={i} className="flex items-center gap-3 p-3 text-sm">
                      <Badge variant="outline" className="shrink-0">{CAT_LABEL[f.category] || f.category}</Badge>
                      <span className="flex-1">{f.content}</span>
                      <span className="shrink-0 text-xs text-muted-foreground">{(f.created_at || "").slice(0, 10)}</span>
                    </Card>
                  ))}
                </div>
              )}
            </div>

            {snap.interaction_summaries.length > 0 && (
              <div>
                <div className="mb-2 text-sm font-medium">最近交互摘要</div>
                <div className="flex flex-col gap-1">
                  {snap.interaction_summaries.slice().reverse().map((s, i) => (
                    <div key={i} className="flex gap-2 text-xs text-muted-foreground">
                      <span className="shrink-0">{(s.timestamp || "").slice(0, 16).replace("T", " ")}</span>
                      <span>{s.summary}</span>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
