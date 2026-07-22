import { useEffect, useRef, useState } from "react";
import { useChatStream } from "@/hooks/useChatStream";
import type { SSEEvent } from "@/lib/sse";
import { MessageBubble } from "@/components/MessageBubble";
import { AgentActivity } from "@/components/AgentActivity";
import { MetadataChips, type Meta } from "@/components/MetadataChips";
import { Composer } from "@/components/Composer";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { consolidateMemory, getHistory, type ConsolidateResult } from "@/lib/api";

type Turn = { id: number; userText: string; activity: SSEEvent[]; reply?: string; meta?: Meta; handoff?: string[] };

export function ChatView({ sessionId, userId, onUserId }: {
  sessionId: string; userId: string; onUserId: (uid: string) => void;
}) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [mem, setMem] = useState<ConsolidateResult | null>(null);
  const [memBusy, setMemBusy] = useState(false);
  const [memErr, setMemErr] = useState<string | null>(null);
  const [uidDraft, setUidDraft] = useState(userId);
  const idRef = useRef(0);
  const cur = useRef<number>(-1);

  // 挂载/切会话时拉取已落盘历史,重启或刷新后聊天记录不再空白
  useEffect(() => {
    let cancelled = false;
    getHistory(sessionId).then((bubbles) => {
      if (cancelled) return;
      const restored: Turn[] = [];
      for (const b of bubbles) {
        if (b.role === "user") restored.push({ id: ++idRef.current, userText: b.content, activity: [] });
        else if (restored.length) restored[restored.length - 1].reply = b.content;
      }
      setTurns(restored);
    });
    return () => { cancelled = true; };
  }, [sessionId]);

  async function onConsolidate() {
    setMemBusy(true); setMemErr(null);
    try { setMem(await consolidateMemory(sessionId, userId)); }
    catch (e) { setMemErr(String(e)); }
    finally { setMemBusy(false); }
  }

  const patch = (fn: (t: Turn) => Turn) =>
    setTurns((ts) => ts.map((t) => (t.id === cur.current ? fn(t) : t)));

  const { send, streaming } = useChatStream({
    sessionId,
    userId,
    onEvent: (e) => {
      if (["thought", "tool_call", "tool_result", "guard"].includes(e.type)) patch((t) => ({ ...t, activity: [...t.activity, e] }));
      else if (e.type === "reply") patch((t) => ({ ...t, reply: e.content }));
      else if (e.type === "metadata") patch((t) => ({ ...t, meta: { intent: e.intent, confidence: e.confidence, requires_human: e.requires_human, follow_up_question: e.follow_up_question } }));
      else if (e.type === "handoff") patch((t) => ({ ...t, handoff: e.reasons || [] }));
      else if (e.type === "error") patch((t) => ({ ...t, reply: "⚠️ 出错了：" + e.message }));
    },
  });

  function onSend(text: string) {
    const id = ++idRef.current;
    cur.current = id;
    setTurns((ts) => [...ts, { id, userText: text, activity: [] }]);
    send(text);
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-2 border-b bg-card/50 px-6 py-1.5">
        <span className="text-xs text-muted-foreground">用户</span>
        <input
          className="h-7 w-32 rounded border bg-background px-2 text-xs"
          value={uidDraft}
          placeholder="用户ID"
          title="用户身份:长期记忆按此隔离(一人一档)。改完点「切换」或按回车。"
          onChange={(e) => setUidDraft(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && uidDraft.trim() && uidDraft !== userId) onUserId(uidDraft); }}
        />
        <Button variant="secondary" size="sm" className="h-7 text-xs"
                disabled={!uidDraft.trim() || uidDraft === userId}
                onClick={() => onUserId(uidDraft)}>
          切换
        </Button>
        <span className="text-xs text-muted-foreground">当前:<b>{userId}</b> · 会话 {sessionId}</span>
        <Button variant="outline" size="sm" className="ml-auto h-7 text-xs"
                disabled={memBusy} onClick={onConsolidate}>
          {memBusy ? "巩固中…" : "结束会话·巩固记忆"}
        </Button>
      </div>
      {memErr && <div className="border-b bg-destructive/10 px-6 py-2 text-xs text-destructive">巩固失败：{memErr}</div>}
      {mem && (
        <div className="border-b bg-secondary/40 px-6 py-3">
          <div className="mb-2 flex items-center gap-2 text-xs">
            <span className="font-semibold">长期记忆（策展{mem.curation ? "已开启" : "未开启"}）· {mem.count} 条</span>
            <button className="ml-auto text-muted-foreground hover:text-foreground" onClick={() => setMem(null)}>收起</button>
          </div>
          {mem.count === 0
            ? <div className="text-xs text-muted-foreground">暂无可长期记忆的事实（多聊几句偏好/身份再试）。</div>
            : <ul className="flex flex-col gap-1">
                {mem.facts.map((f, i) => (
                  <li key={i} className="flex items-center gap-2 text-xs">
                    <Badge variant="outline" className="shrink-0">{f.category}</Badge>
                    <span>{f.content}</span>
                    <span className="ml-auto shrink-0 text-muted-foreground">{f.created_at.slice(0, 10)}</span>
                  </li>
                ))}
              </ul>}
        </div>
      )}
      <ScrollArea className="flex-1">
        <div className="mx-auto flex max-w-3xl flex-col gap-4 p-6">
          {turns.length === 0 && <div className="mt-20 text-center text-muted-foreground">你好，我是小夕 😊 有什么可以帮你？</div>}
          {turns.map((t) => (
            <div key={t.id} className="flex flex-col gap-1">
              <MessageBubble role="user">{t.userText}</MessageBubble>
              <AgentActivity events={t.activity} defaultOpen={!t.reply} />
              {t.handoff && <div className="rounded-md bg-accent/15 px-3 py-2 text-sm text-accent">🎧 已转人工，原因：{t.handoff.join("、")}</div>}
              {t.reply && <MessageBubble role="assistant">{t.reply}</MessageBubble>}
              {t.meta && <MetadataChips meta={t.meta} />}
            </div>
          ))}
        </div>
      </ScrollArea>
      <div className="mx-auto w-full max-w-3xl">
        <Composer disabled={streaming} onSend={onSend} />
      </div>
    </div>
  );
}
