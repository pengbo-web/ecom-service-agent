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

/** 坐席工作队列的一行 = **一个买家在等**，不是一次升级判定。
 *
 * 实测:这一页此前直接渲染 `/api/handoffs` 的原始升级记录,于是 50 条待办实际只来自
 * 7 个会话——其中一个占了 37 条。坐席看到 50 以为有 50 个人在等,而 resolve 是逐条的,
 * 要点 37 次才能清掉一个买家,处理完一条同一个买家立刻又冒出来。
 *
 * 这一页的标题本来就写着「待接管会话」——界面自己说的单位就是会话。 */
type HandoffSession = {
  session_id: string;
  latest: Handoff;
  waiting_since: string;
  escalations: number;
  handoff_ids: string[];
};

function errMsg(e: unknown): string {
  const msg = String((e as { message?: string })?.message || "");
  if (msg.includes("401") || msg.includes("403"))
    return "无权访问（服务端已启用 ADMIN_TOKEN）。浏览器控制台执行 localStorage.setItem('admin_token','你的令牌') 后点刷新。";
  return "加载失败：" + (msg || "服务不可用，请确认后端已启动");
}

export function SeatView({ sessionId }: { sessionId: string }) {
  const [list, setList] = useState<HandoffSession[]>([]);
  const [totals, setTotals] = useState<{ waiting_buyers: number; escalations_total: number }>(
    { waiting_buyers: 0, escalations_total: 0 });
  const [mode, setMode] = useState<string>("auto");
  const [err, setErr] = useState<string | null>(null);

  async function load() {
    try {
      setErr(null);
      // 用**会话**队列,不是原始升级记录。/api/handoffs 仍然保留,那是审计视图。
      const d = await getJSON<{ sessions: HandoffSession[]; waiting_buyers: number;
                               escalations_total: number }>("/api/handoffs/sessions");
      setList(d.sessions || []);
      setTotals({ waiting_buyers: d.waiting_buyers || 0,
                  escalations_total: d.escalations_total || 0 });
    } catch (e) {
      setErr(errMsg(e));
    }
  }
  useEffect(() => { load(); /* eslint-disable-next-line */ }, []);

  async function resolveSession(sid: string) {
    // 一次清掉这个买家的全部待处理升级:逐条 resolve 会让一个买家需要点 37 次(实测),
    // 而中间任何一次遗漏都会让这个会话重新出现在队列里。
    await adminFetch("/api/handoffs/session/" + encodeURIComponent(sid) + "/resolve",
                     { method: "POST" });
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
          <h2 className="text-lg font-semibold">
            待接管会话
            {totals.waiting_buyers > 0 && (
              <span className="ml-2 text-sm font-normal text-muted-foreground">
                {totals.waiting_buyers} 位买家在等
                {totals.escalations_total > totals.waiting_buyers && (
                  // 两个数分开报:一个是排班依据(多少人在等),一个是质量信号
                  // (累计升级次数)。混成一个数正是这次要修的那个错。
                  <>（累计升级 {totals.escalations_total} 次）</>
                )}
              </span>
            )}
          </h2>
          <Button variant="secondary" size="sm" onClick={load}>刷新</Button>
        </div>

        {err ? (
          <div className="rounded-md bg-destructive/10 px-3 py-2 text-sm text-destructive">{err}</div>
        ) : list.length === 0 ? (
          <div className="text-sm text-muted-foreground">暂无待接管会话</div>
        ) : (
          <div className="flex flex-col gap-3">
            {list.map((g) => (
              <Card key={g.session_id} className="flex flex-col gap-1 p-4"
                    data-testid={`handoff-session-${g.session_id}`}>
                <div className="flex flex-wrap items-center gap-2 text-sm font-medium">
                  会话 {g.session_id}
                  {g.latest?.intent ? <Badge variant="outline">{g.latest.intent}</Badge> : null}
                  {/* 排队看的是**最早**那次升级,不是最近一次——等最久的该先处理 */}
                  <span className="text-xs text-muted-foreground">等待自 {g.waiting_since}</span>
                  {g.escalations > 1 && (
                    // 反复升级本身就是优先级信号:同一个买家被打回来 N 次
                    <Badge variant="outline">升级 {g.escalations} 次</Badge>
                  )}
                </div>
                <div className="text-sm text-destructive">
                  原因：{(g.latest?.reasons || []).join("、")}
                </div>
                <div className="text-sm text-muted-foreground">用户：{g.latest?.payload?.user_input || ""}</div>
                <div className="text-sm text-muted-foreground">Agent：{g.latest?.payload?.reply || ""}</div>
                <div>
                  <Button size="sm" className="mt-1" onClick={() => resolveSession(g.session_id)}>
                    标记已解决{g.escalations > 1 ? `（${g.escalations} 条）` : ""}
                  </Button>
                </div>
              </Card>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
