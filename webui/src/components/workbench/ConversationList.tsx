import { ScrollArea } from "@/components/ui/scroll-area";
import { avatarColor, initials, relativeTime, statusMeta, TONE_CLASS } from "./parts";
import type { WbConversation } from "@/lib/api";

type Filter = "all" | "manual" | "open";

export function ConversationList({ items, selected, onSelect, filter, onFilter }: {
  items: WbConversation[]; selected?: string; onSelect: (id: string) => void;
  filter: Filter; onFilter: (f: Filter) => void;
}) {
  const shown = items.filter((c) =>
    filter === "all" ? true : filter === "manual" ? c.manual : c.status === "open");
  const tabs: { key: Filter; label: string }[] = [
    { key: "all", label: "全部" }, { key: "open", label: "进行中" }, { key: "manual", label: "人工中" },
  ];
  return (
    <div className="flex h-full flex-col border-r bg-card/40">
      <div className="flex items-center gap-1 border-b px-3 py-2">
        {tabs.map((t) => (
          <button key={t.key} onClick={() => onFilter(t.key)}
            className={`rounded-full px-3 py-1 text-xs transition ${
              filter === t.key ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-secondary"}`}>
            {t.label}
          </button>
        ))}
        <span className="ml-auto text-xs text-muted-foreground">{shown.length} 路会话</span>
      </div>
      <ScrollArea className="flex-1">
        {shown.length === 0 ? (
          <div className="p-6 text-center text-xs text-muted-foreground">暂无会话</div>
        ) : (
          <ul className="flex flex-col">
            {shown.map((c) => {
              const sm = statusMeta(c);
              const active = c.conversation_id === selected;
              return (
                <li key={c.conversation_id}>
                  <button onClick={() => onSelect(c.conversation_id)}
                    className={`flex w-full items-start gap-3 border-b px-3 py-3 text-left transition ${
                      active ? "bg-secondary" : "hover:bg-secondary/50"}`}>
                    <span className="mt-0.5 flex h-9 w-9 shrink-0 items-center justify-center rounded-full text-xs font-medium text-white"
                      style={{ background: avatarColor(c.user_id) }}>
                      {initials(c.user_id)}
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="flex items-center gap-2">
                        <span className="truncate text-sm font-medium">客户 {c.user_id}</span>
                        <span className="ml-auto shrink-0 text-[11px] text-muted-foreground">{relativeTime(c.created_at)}</span>
                      </span>
                      <span className="mt-0.5 block truncate text-xs text-muted-foreground">
                        {c.preview || "（暂无消息）"}
                      </span>
                      <span className={`mt-1 inline-block rounded px-1.5 py-0.5 text-[11px] ${TONE_CLASS[sm.tone]}`}>
                        {sm.label}
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
