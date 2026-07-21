import { useEffect, useState } from "react";
import { adminFetch, getJSON } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";

type Handoff = {
  handoff_id: string;
  session_id: string;
  intent?: string | null;
  reasons?: string[];
  created_at?: string;
  payload?: { user_input?: string; reply?: string } | null;
};

function errMsg(e: unknown): string {
  const msg = String((e as { message?: string })?.message || "");
  if (msg.includes("401") || msg.includes("403"))
    return "无权访问（服务端已启用 ADMIN_TOKEN）。浏览器控制台执行 localStorage.setItem('admin_token','你的令牌') 后点刷新。";
  return "加载失败：" + (msg || "服务不可用，请确认后端已启动");
}

export function SeatView({ sessionId }: { sessionId: string }) {
  const [list, setList] = useState<Handoff[]>([]);
  const [mode, setMode] = useState<string>("auto");
  const [err, setErr] = useState<string | null>(null);

  async function load() {
    try {
      setErr(null);
      setList(await getJSON<Handoff[]>("/api/handoffs"));
    } catch (e) {
      setErr(errMsg(e));
    }
  }
  useEffect(() => { load(); /* eslint-disable-next-line */ }, []);

  async function resolve(id: string) {
    await adminFetch("/api/handoffs/" + id + "/resolve", { method: "POST" });
    load();
  }
  async function toggle() {
    const r = await (await adminFetch("/api/session/" + sessionId + "/takeover", { method: "POST" })).json();
    setMode(r.mode);
  }

  return (
    <div className="h-full overflow-y-auto p-6">
      <div className="mx-auto flex max-w-4xl flex-col gap-5">
        <Card className="flex items-center gap-3 p-4">
          <Button variant="secondary" size="sm" onClick={toggle}>切换本会话 人工/自动</Button>
          <span className="text-sm text-muted-foreground">
            当前：{mode === "manual" ? "人工接管中（Agent 已暂停）" : "自动（AI 回复）"}
          </span>
        </Card>

        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold">待接管会话</h2>
          <Button variant="secondary" size="sm" onClick={load}>刷新</Button>
        </div>

        {err ? (
          <div className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">{err}</div>
        ) : list.length === 0 ? (
          <div className="text-sm text-muted-foreground">暂无待接管会话</div>
        ) : (
          <div className="flex flex-col gap-3">
            {list.map((h) => (
              <Card key={h.handoff_id} className="flex flex-col gap-1 p-4">
                <div className="flex items-center gap-2 text-sm font-medium">
                  会话 {h.session_id}
                  {h.intent ? <Badge variant="outline">{h.intent}</Badge> : null}
                  <span className="text-xs text-muted-foreground">{h.created_at}</span>
                </div>
                <div className="text-sm text-destructive">原因：{(h.reasons || []).join("、")}</div>
                <div className="text-sm text-muted-foreground">用户：{h.payload?.user_input || ""}</div>
                <div className="text-sm text-muted-foreground">Agent：{h.payload?.reply || ""}</div>
                <div><Button size="sm" className="mt-1" onClick={() => resolve(h.handoff_id)}>标记已解决</Button></div>
              </Card>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
