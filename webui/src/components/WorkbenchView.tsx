import { useEffect, useRef, useState } from "react";
import { ConversationList } from "./workbench/ConversationList";
import { MessageThread } from "./workbench/MessageThread";
import { ContextPanel } from "./workbench/ContextPanel";
import {
  adminListConversations, adminGetMessages, adminTakeover,
  type WbConversation, type WbTurn,
} from "@/lib/api";

type Filter = "all" | "manual" | "open";

export function WorkbenchView() {
  const [convs, setConvs] = useState<WbConversation[]>([]);
  const [filter, setFilter] = useState<Filter>("all");
  const [selected, setSelected] = useState<string | undefined>();
  const [turns, setTurns] = useState<WbTurn[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const selRef = useRef<string | undefined>(undefined);
  selRef.current = selected;

  async function refreshList() {
    try { setErr(null); setConvs(await adminListConversations()); }
    catch (e: any) { setErr(String(e?.message || e)); }
  }
  async function refreshThread(sid: string) {
    try { setTurns(await adminGetMessages(sid)); } catch { /* 静默,下一轮重试 */ }
  }

  // 列表每 3s 刷新
  useEffect(() => {
    refreshList();
    const t = setInterval(refreshList, 3000);
    return () => clearInterval(t);
  }, []);

  // 选中会话:立即拉一次 + 每 3s 刷新消息(收到 AI/客户新消息)
  useEffect(() => {
    if (!selected) { setTurns([]); return; }
    refreshThread(selected);
    const t = setInterval(() => { if (selRef.current) refreshThread(selRef.current); }, 3000);
    return () => clearInterval(t);
  }, [selected]);

  const selConv = convs.find((c) => c.conversation_id === selected) || null;

  async function onToggleManual() {
    if (!selected) return;
    await adminTakeover(selected);
    await refreshList();
  }

  return (
    <div className="grid h-full grid-cols-[300px_1fr] lg:grid-cols-[300px_1fr_260px]">
      <ConversationList items={convs} selected={selected} onSelect={setSelected}
        filter={filter} onFilter={setFilter} />
      {selConv ? (
        <MessageThread sessionId={selConv.conversation_id} userId={selConv.user_id}
          manual={selConv.manual} turns={turns}
          onAfterReply={setTurns} onToggleManual={onToggleManual} />
      ) : (
        <div className="flex items-center justify-center text-sm text-muted-foreground">
          {err ? <span className="text-destructive">加载失败:{err}</span> : "从左侧选择一路会话开始接待"}
        </div>
      )}
      <ContextPanel conv={selConv} />
    </div>
  );
}
