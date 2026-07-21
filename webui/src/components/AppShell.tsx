import * as React from "react";
import { MessageSquare, LayoutDashboard, Headset, FlaskConical, RotateCcw } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";

export type View = "chat" | "dash" | "seat" | "eval";

const TABS: { v: View; icon: React.ReactNode; label: string }[] = [
  { v: "chat", icon: <MessageSquare className="h-4 w-4" />, label: "聊天" },
  { v: "dash", icon: <LayoutDashboard className="h-4 w-4" />, label: "看板" },
  { v: "seat", icon: <Headset className="h-4 w-4" />, label: "坐席" },
  { v: "eval", icon: <FlaskConical className="h-4 w-4" />, label: "评估" },
];

export function AppShell({ view, onView, onReset, children }: {
  view: View; onView: (v: View) => void; onReset: () => void; children: React.ReactNode;
}) {
  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center gap-3 border-b bg-card px-4 py-2">
        <div className="flex items-center gap-2">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary text-primary-foreground font-bold">夕</div>
          <div className="text-sm font-semibold">并夕夕 · 小夕</div>
        </div>
        <nav className="flex gap-1">
          {TABS.map((t) => (
            <button
              key={t.v}
              onClick={() => onView(t.v)}
              className={cn(
                "flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm transition-colors",
                view === t.v ? "bg-primary text-primary-foreground" : "text-foreground hover:bg-secondary"
              )}
            >
              {t.icon} {t.label}
            </button>
          ))}
        </nav>
        <Button variant="ghost" size="sm" className="ml-auto" onClick={onReset}>
          <RotateCcw className="h-4 w-4" /> 重置对话
        </Button>
      </header>
      <main className="min-h-0 flex-1 overflow-hidden">{children}</main>
    </div>
  );
}
