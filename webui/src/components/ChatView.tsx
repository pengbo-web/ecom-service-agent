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
import {
  consolidateMemory, getHistory, listConversations,
  login, createUser, setToken, clearToken, setUserId,
  type ConsolidateResult, type ConversationMeta, type HistoryTurn,
} from "@/lib/api";

type Turn = { id: number; userText: string; activity: SSEEvent[]; reply?: string; meta?: Meta; handoff?: string[] };

export function ChatView({ sessionId, userId, onUserId, onConversation }: {
  sessionId: string; userId: string; onUserId: (uid: string) => void;
  onConversation: (conversationId: string) => void;
}) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [mem, setMem] = useState<ConsolidateResult | null>(null);
  const [memBusy, setMemBusy] = useState(false);
  const [memErr, setMemErr] = useState<string | null>(null);
  const [uidDraft, setUidDraft] = useState(userId);
  const [switching, setSwitching] = useState(false);
  const [switchErr, setSwitchErr] = useState<string | null>(null);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [convList, setConvList] = useState<ConversationMeta[] | null>(null);
  const [pastConv, setPastConv] = useState<string | null>(null);
  const [pastBubbles, setPastBubbles] = useState<HistoryTurn[] | null>(null);
  const idRef = useRef(0);
  const cur = useRef<number>(-1);

  // 拉取已落盘历史:挂载/切用户/重置/翻篇时刷新聊天区。
  // 唯一例外是流中的 rotated 换发——此时新会话历史为空,重拉会 setTurns([])
  // 把正在流式接收的当前轮清掉,后续 reply 事件 patch 不到轮次,回复当场
  // 消失(评审 Major)。用 rotatedTo 标记该来源,跳过这一次重拉。
  const rotatedTo = useRef<string | null>(null);
  useEffect(() => {
    if (rotatedTo.current === sessionId) {
      rotatedTo.current = null;
      return;                      // rotated 换发:保住当前轮,不重拉
    }
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
    try {
      // 巩固=把对话沉淀进长期记忆,不结束会话:不翻篇、不清屏,当前会话继续聊。
      setMem(await consolidateMemory(sessionId, userId));
    }
    catch (e) { setMemErr(String(e)); }
    finally { setMemBusy(false); }
  }

  const patch = (fn: (t: Turn) => Turn) =>
    setTurns((ts) => ts.map((t) => (t.id === cur.current ? fn(t) : t)));

  const { send, streaming } = useChatStream({
    sessionId,
    userId,
    onEvent: (e) => {
      // 首帧可能缺席(人工接管/限流/成本上限三个短路分支无此事件):只在收到且 rotated 时才换发,不等待不依赖
      if (e.type === "conversation") {
        if (e.status === "rotated") {
          rotatedTo.current = e.conversation_id;   // 标记来源:历史重拉跳过这一次,保住当前轮
          onConversation(e.conversation_id);
        }
      }
      else if (["thought", "tool_call", "tool_result", "guard", "route", "select", "evaluate", "polish"].includes(e.type)) patch((t) => ({ ...t, activity: [...t.activity, e] }));
      else if (e.type === "reply") patch((t) => ({ ...t, reply: e.content }));
      else if (e.type === "metadata") patch((t) => ({ ...t, meta: { intent: e.intent, confidence: e.confidence, requires_human: e.requires_human, follow_up_question: e.follow_up_question } }));
      else if (e.type === "handoff") patch((t) => ({ ...t, handoff: e.reasons || [] }));
      else if (e.type === "error") {
        if (e.status === 401) { clearToken(); location.reload(); return; }
        patch((t) => ({ ...t, reply: "⚠️ 出错了：" + e.message }));
      }
    },
  });

  // 「切换」= 登出 + 登录别的用户;不存在则确认后创建并登录。成功后同步展示名缓存并触发上层重开会话。
  async function onSwitchUser() {
    const target = uidDraft.trim();
    if (!target || target === userId || switching) return;
    setSwitching(true);
    setSwitchErr(null);
    try {
      const r = await login(target);
      setToken(r.token);
      setUserId(target);
      onUserId(target);
    } catch (e: any) {
      if (e?.status === 404) {
        if (window.confirm(`用户「${target}」不存在，是否创建并登录？`)) {
          try {
            const r = await createUser(target);
            setToken(r.token);
            setUserId(target);
            onUserId(target);
          } catch {
            setSwitchErr("创建失败，请稍后重试");
          }
        }
      } else {
        setSwitchErr("切换失败，请稍后重试");
      }
    } finally {
      setSwitching(false);
    }
  }

  function onSend(text: string) {
    const id = ++idRef.current;
    cur.current = id;
    setTurns((ts) => [...ts, { id, userText: text, activity: [] }]);
    send(text);
  }

  async function toggleHistory() {
    const next = !historyOpen;
    setHistoryOpen(next);
    if (next) {
      setPastConv(null); setPastBubbles(null);
      setConvList(await listConversations(userId));
    }
  }

  async function openPastConv(conversationId: string) {
    setPastConv(conversationId);
    setPastBubbles(await getHistory(conversationId));
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
          onKeyDown={(e) => { if (e.key === "Enter" && uidDraft.trim() && uidDraft !== userId) onSwitchUser(); }}
        />
        <Button variant="secondary" size="sm" className="h-7 text-xs"
                disabled={!uidDraft.trim() || uidDraft === userId || switching}
                onClick={onSwitchUser}>
          {switching ? "切换中…" : "切换"}
        </Button>
        {switchErr && <span className="text-xs text-destructive">{switchErr}</span>}
        <span className="text-xs text-muted-foreground">当前:<b>{userId}</b> · 会话 {sessionId}</span>
        <Button variant="ghost" size="sm" className="ml-auto h-7 text-xs" onClick={toggleHistory}>
          {historyOpen ? "收起历史" : "历史会话"}
        </Button>
        <Button variant="outline" size="sm" className="h-7 text-xs"
                title="把当前对话沉淀进长期记忆,不结束会话,可继续聊。结束会话请用「重置对话」。"
                disabled={memBusy} onClick={onConsolidate}>
          {memBusy ? "巩固中…" : "巩固记忆"}
        </Button>
      </div>
      {historyOpen && (
        <div className="max-h-64 overflow-auto border-b bg-secondary/30 px-6 py-3">
          {pastConv ? (
            <div>
              <button className="mb-2 text-xs text-muted-foreground hover:text-foreground" onClick={() => { setPastConv(null); setPastBubbles(null); }}>
                ← 返回列表
              </button>
              <div className="text-xs text-muted-foreground mb-2">只读回看：{pastConv}</div>
              {pastBubbles === null ? (
                <div className="text-xs text-muted-foreground">加载中…</div>
              ) : pastBubbles.length === 0 ? (
                <div className="text-xs text-muted-foreground">该会话暂无消息记录。</div>
              ) : (
                <div className="flex flex-col gap-2">
                  {pastBubbles.map((b, i) => (
                    <MessageBubble key={i} role={b.role}>{b.content}</MessageBubble>
                  ))}
                </div>
              )}
            </div>
          ) : convList === null ? (
            <div className="text-xs text-muted-foreground">加载中…</div>
          ) : convList.length === 0 ? (
            <div className="text-xs text-muted-foreground">暂无历史会话。</div>
          ) : (
            <ul className="flex flex-col gap-1">
              {convList.map((c) => (
                <li key={c.conversation_id}>
                  <button
                    className="flex w-full items-center gap-2 rounded px-2 py-1 text-left text-xs hover:bg-secondary"
                    onClick={() => openPastConv(c.conversation_id)}
                  >
                    <span className="font-mono">{c.conversation_id}</span>
                    <span className="text-muted-foreground">{c.created_at}</span>
                    <Badge variant={c.status === "open" ? "default" : "outline"} className="shrink-0">
                      {c.status === "open" ? "进行中" : "已结束"}
                    </Badge>
                    {c.close_reason && <span className="text-muted-foreground">({c.close_reason})</span>}
                    {c.conversation_id === sessionId && <span className="ml-auto text-muted-foreground">当前</span>}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
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
