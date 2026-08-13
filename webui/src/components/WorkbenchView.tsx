import { useEffect, useMemo, useRef, useState } from "react";
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
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<string | undefined>();
  const [turns, setTurns] = useState<WbTurn[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const selRef = useRef<string | undefined>(undefined);
  selRef.current = selected;

  // 未读追踪:seen[会话]=坐席上次看到的用户消息数;首次加载把现有全部标为已读,
  // 之后 turns 增长(客户来新消息)或新会话出现即算未读。ref 存储,不触发额外渲染。
  const seen = useRef<Record<string, number>>({});
  const inited = useRef(false);

  async function refreshList() {
    try {
      setErr(null);
      const list = await adminListConversations();
      if (!inited.current) {
        const s: Record<string, number> = {};
        list.forEach((c) => { s[c.conversation_id] = c.turns; });
        seen.current = s; inited.current = true;
      }
      // 当前打开的会话保持已读(边看边来的消息不计未读)
      const sel = selRef.current;
      if (sel) { const c = list.find((x) => x.conversation_id === sel); if (c) seen.current[sel] = c.turns; }
      setConvs(list);
    } catch (e: any) { setErr(String(e?.message || e)); }
  }
  async function refreshThread(sid: string) {
    try { setTurns(await adminGetMessages(sid)); } catch { /* 静默,下一轮重试 */ }
  }

  useEffect(() => {
    refreshList();
    const t = setInterval(refreshList, 3000);
    return () => clearInterval(t);
  }, []);

  useEffect(() => {
    if (!selected) { setTurns([]); return; }
    const c = convs.find((x) => x.conversation_id === selected);
    if (c) seen.current[selected] = c.turns;   // 打开即标记已读
    refreshThread(selected);
    const t = setInterval(() => { if (selRef.current) refreshThread(selRef.current); }, 3000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected]);

  const unreadIds = useMemo(
    () => new Set(convs.filter((c) => c.turns > (seen.current[c.conversation_id] ?? 0)).map((c) => c.conversation_id)),
    [convs],
  );

  const selConv = convs.find((c) => c.conversation_id === selected) || null;

  async function onToggleManual() {
    if (!selected) return;
    await adminTakeover(selected);
    await refreshList();
  }

  return (
    // `grid-rows-[minmax(0,1fr)]` + 每列 `min-h-0` 是这个面板能用的前提,不是样式偏好。
    //
    // **实测缺陷**:会话消息一多,中间那列(MessageThread)会**撑破整个网格**——
    // 容器 663px,它长到 3113px;输入框被推到 top=3101(视口只有 720),买家看不见,
    // 而 `AppShell` 的 `<main>` 是 `overflow-hidden`,所以页面也滚不动。
    // 结果就是:消息刷屏之后**既看不到输入框、也滚不动**,这一路会话等于废了。
    //
    // 原因是 CSS 的默认行为:grid/flex item 的**自动最小尺寸等于内容尺寸**,
    // 所以子元素写了 `h-full` 也不肯缩到容器高度以下,行轨道跟着被内容顶开。
    // 必须两处都给:行轨道 `minmax(0,1fr)` 不让它超过容器,item `min-h-0` 允许它缩。
    // 只给其中一处仍然会溢出(实测)。
    //
    // 三列都要给,不能只修中间那一列:左右两列各自也有长列表,今天没炸只是因为
    // 它们内部先有了滚动条——那是运气,不是设计。
    <div className="grid h-full grid-rows-[minmax(0,1fr)] grid-cols-[288px_1fr] lg:grid-cols-[288px_1fr_260px]">
      <ConversationList items={convs} selected={selected} onSelect={setSelected}
        filter={filter} onFilter={setFilter} query={query} onQuery={setQuery} unreadIds={unreadIds} />
      {selConv ? (
        <MessageThread sessionId={selConv.conversation_id} userId={selConv.user_id}
          manual={selConv.manual} turns={turns}
          onAfterReply={setTurns} onToggleManual={onToggleManual} />
      ) : (
        <div className="flex min-h-0 items-center justify-center text-sm text-muted-foreground">
          {err ? <span className="text-destructive">加载失败:{err}</span> : "从左侧选择一路会话开始接待"}
        </div>
      )}
      <ContextPanel conv={selConv} />
    </div>
  );
}
