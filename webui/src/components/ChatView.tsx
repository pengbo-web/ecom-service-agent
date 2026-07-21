import { useRef, useState } from "react";
import { useChatStream } from "@/hooks/useChatStream";
import type { SSEEvent } from "@/lib/sse";
import { MessageBubble } from "@/components/MessageBubble";
import { AgentActivity } from "@/components/AgentActivity";
import { MetadataChips, type Meta } from "@/components/MetadataChips";
import { Composer } from "@/components/Composer";
import { ScrollArea } from "@/components/ui/scroll-area";

type Turn = { id: number; userText: string; activity: SSEEvent[]; reply?: string; meta?: Meta; handoff?: string[] };

export function ChatView({ sessionId }: { sessionId: string }) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const idRef = useRef(0);
  const cur = useRef<number>(-1);

  const patch = (fn: (t: Turn) => Turn) =>
    setTurns((ts) => ts.map((t) => (t.id === cur.current ? fn(t) : t)));

  const { send, streaming } = useChatStream({
    sessionId,
    onEvent: (e) => {
      if (["thought", "tool_call", "tool_result", "guard"].includes(e.type)) patch((t) => ({ ...t, activity: [...t.activity, e] }));
      else if (e.type === "reply") patch((t) => ({ ...t, reply: e.content }));
      else if (e.type === "metadata") patch((t) => ({ ...t, meta: { intent: e.intent, confidence: e.confidence, requires_human: e.requires_human, follow_up_question: e.follow_up_question } }));
      else if (e.type === "handoff") patch((t) => ({ ...t, handoff: e.reasons || [] }));
      else if (e.type === "error") patch((t) => ({ ...t, reply: "⚠️ 出错了：" + e.message }));
    },
  });

  function onSend(text: string, confirm: boolean) {
    const id = ++idRef.current;
    cur.current = id;
    setTurns((ts) => [...ts, { id, userText: text, activity: [] }]);
    send(text, confirm);
  }

  return (
    <div className="flex h-full flex-col">
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
