import { useState } from "react";
import { MessageBubble } from "@/components/MessageBubble";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Button } from "@/components/ui/button";
import { adminReply, type WbTurn } from "@/lib/api";

export function MessageThread({ sessionId, userId, manual, turns, onAfterReply, onToggleManual }: {
  sessionId: string; userId: string; manual: boolean; turns: WbTurn[];
  onAfterReply: (turns: WbTurn[]) => void; onToggleManual: () => void;
}) {
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);

  async function send() {
    const text = draft.trim();
    if (!text || busy) return;
    setBusy(true);
    try {
      const next = await adminReply(sessionId, text);
      setDraft("");
      onAfterReply(next);
    } finally { setBusy(false); }
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-3 border-b px-4 py-2.5">
        <span className="text-sm font-semibold">客户 {userId}</span>
        <span className="font-mono text-[11px] text-muted-foreground">{sessionId}</span>
        <Button size="sm" variant={manual ? "default" : "secondary"} className="ml-auto h-7 text-xs"
          onClick={onToggleManual}>
          {manual ? "● 人工接管中(点击交还 AI)" : "转人工接管"}
        </Button>
      </div>

      <ScrollArea className="min-h-0 flex-1">
        <div className="mx-auto flex max-w-2xl flex-col gap-3 p-5">
          {turns.length === 0 ? (
            <div className="mt-16 text-center text-sm text-muted-foreground">该会话暂无消息</div>
          ) : turns.map((t, i) => (
            <MessageBubble key={i} role={t.role}>{t.content}</MessageBubble>
          ))}
        </div>
      </ScrollArea>

      <div className="border-t p-3">
        {!manual && (
          <div className="mb-2 rounded-md bg-amber-500/10 px-3 py-1.5 text-xs text-amber-600 dark:text-amber-400">
            当前为 AI 自动接待。点右上「转人工接管」后即可以人工身份回复。
          </div>
        )}
        <div className="flex items-end gap-2">
          <textarea
            className="min-h-[44px] flex-1 resize-none rounded-md border bg-background px-3 py-2 text-sm"
            placeholder={manual ? "输入人工回复,Enter 发送…" : "接管后可回复"}
            disabled={!manual || busy}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }}
          />
          <Button size="sm" disabled={!manual || busy || !draft.trim()} onClick={send}>
            {busy ? "发送中…" : "发送"}
          </Button>
        </div>
      </div>
    </div>
  );
}
