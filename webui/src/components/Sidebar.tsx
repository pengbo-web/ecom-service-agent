import * as React from "react";
import { MessageSquare, LayoutDashboard, RotateCcw, ExternalLink } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";

export type View = "chat" | "dash";

export function Sidebar({ view, onView, onReset }: { view: View; onView: (v: View) => void; onReset: () => void }) {
  const item = (v: View, icon: React.ReactNode, label: string) => (
    <button
      onClick={() => onView(v)}
      className={cn(
        "flex w-full items-center gap-2 rounded-md px-3 py-2 text-sm transition-colors",
        view === v ? "bg-primary text-primary-foreground" : "hover:bg-secondary text-foreground"
      )}
    >
      {icon} {label}
    </button>
  );
  return (
    <aside className="flex w-60 shrink-0 flex-col gap-2 border-r bg-card p-3">
      <div className="flex items-center gap-2 px-2 py-3">
        <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary text-primary-foreground font-bold">夕</div>
        <div>
          <div className="text-sm font-semibold leading-tight">并夕夕 · 小夕</div>
          <div className="text-xs text-muted-foreground">智能客服</div>
        </div>
      </div>
      <nav className="flex flex-col gap-1">
        {item("chat", <MessageSquare className="h-4 w-4" />, "聊天")}
        {item("dash", <LayoutDashboard className="h-4 w-4" />, "看板")}
      </nav>
      <div className="mt-auto flex flex-col gap-1">
        <Button variant="ghost" size="sm" className="justify-start" onClick={onReset}>
          <RotateCcw className="h-4 w-4" /> 重置对话
        </Button>
        <a href="/legacy" className="flex items-center gap-2 rounded-md px-3 py-2 text-sm text-muted-foreground hover:bg-secondary">
          <ExternalLink className="h-4 w-4" /> 更多（坐席/评估）
        </a>
      </div>
    </aside>
  );
}
