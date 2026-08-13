import { ScrollArea } from "@/components/ui/scroll-area";
import { avatarGradient, initials, relativeTime, statusMeta, TONE_CLASS } from "./parts";
import type { WbConversation } from "@/lib/api";

type Filter = "all" | "manual" | "open";

export function ConversationList({ items, selected, onSelect, filter, onFilter, query, onQuery, unreadIds }: {
  items: WbConversation[]; selected?: string; onSelect: (id: string) => void;
  filter: Filter; onFilter: (f: Filter) => void;
  query: string; onQuery: (q: string) => void;
  unreadIds: Set<string>;
}) {
  const q = query.trim().toLowerCase();
  const shown = items.filter((c) => {
    const passTab = filter === "all" ? true : filter === "manual" ? c.manual : c.status === "open";
    const passQ = !q || (c.user_id || "").toLowerCase().includes(q) || (c.preview || "").toLowerCase().includes(q);
    return passTab && passQ;
  });
  const tabs: { key: Filter; label: string }[] = [
    { key: "all", label: "全部" }, { key: "open", label: "进行中" }, { key: "manual", label: "人工中" },
  ];

  return (
    // min-h-0:同 MessageThread,网格 item 必须允许缩到内容高度以下
    <div className="flex h-full min-h-0 flex-col border-r bg-card/40">
      {/* 搜索 */}
      <div className="border-b p-2">
        <div className="flex items-center gap-2 rounded-md border bg-background px-2.5 py-1.5">
          <svg className="h-3.5 w-3.5 shrink-0 text-muted-foreground" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <circle cx="11" cy="11" r="7" /><path d="m21 21-4.3-4.3" />
          </svg>
          <input value={query} onChange={(e) => onQuery(e.target.value)}
            placeholder="搜索买家昵称 / ID / 消息"
            className="h-5 w-full bg-transparent text-xs outline-none placeholder:text-muted-foreground" />
          {query && <button onClick={() => onQuery("")} className="text-muted-foreground hover:text-foreground">✕</button>}
        </div>
      </div>
      {/* 过滤标签 */}
      <div className="flex items-center gap-1 border-b px-2 py-1.5">
        {tabs.map((t) => (
          <button key={t.key} onClick={() => onFilter(t.key)}
            className={`rounded-full px-2.5 py-1 text-xs transition ${
              filter === t.key ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-secondary"}`}>
            {t.label}
          </button>
        ))}
        <span className="ml-auto pr-1 text-[11px] text-muted-foreground">{shown.length}</span>
      </div>

      <ScrollArea className="min-h-0 flex-1">
        {shown.length === 0 ? (
          <div className="p-6 text-center text-xs text-muted-foreground">没有匹配的会话</div>
        ) : (
          <ul className="flex flex-col">
            {shown.map((c) => {
              const sm = statusMeta(c);
              const active = c.conversation_id === selected;
              const unread = unreadIds.has(c.conversation_id) && !active;
              const label = "客户 " + c.user_id;
              return (
                <li key={c.conversation_id} className="relative">
                  {active && <span className="absolute inset-y-0 left-0 w-[3px] rounded-r bg-primary" />}
                  <button onClick={() => onSelect(c.conversation_id)}
                    className={`flex w-full items-center gap-2.5 px-3 py-2.5 text-left transition ${
                      active ? "bg-primary/10" : "hover:bg-secondary/60"}`}>
                    <span className="relative shrink-0">
                      <span className="flex h-10 w-10 items-center justify-center rounded-full text-xs font-semibold text-white shadow-sm ring-2 ring-background"
                        style={{ backgroundImage: avatarGradient(c.user_id) }}>
                        {initials(c.user_id)}
                      </span>
                      {unread && <span className="absolute -right-0.5 -top-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-red-500 px-1 text-[10px] font-medium text-white ring-2 ring-background">
                        {Math.min(99, Math.max(1, c.turns))}
                      </span>}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="flex items-center gap-2">
                        <span className={`truncate text-[13px] ${unread ? "font-semibold" : "font-medium"}`}>{label}</span>
                        <span className="ml-auto shrink-0 text-[11px] text-muted-foreground">{relativeTime(c.last_active)}</span>
                      </span>
                      <span className="mt-0.5 flex items-center gap-1.5">
                        <span className={`truncate text-xs ${unread ? "text-foreground" : "text-muted-foreground"}`}>
                          {c.preview || "（暂无消息）"}
                        </span>
                        <span className={`ml-auto shrink-0 rounded px-1.5 py-0.5 text-[10px] ${TONE_CLASS[sm.tone]}`}>
                          {sm.label}
                        </span>
                      </span>
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </ScrollArea>
    </div>
  );
}
