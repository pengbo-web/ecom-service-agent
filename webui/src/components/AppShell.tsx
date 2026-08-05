import * as React from "react";
import { MessageSquare, LayoutDashboard, Headset, FlaskConical, Brain, RotateCcw, ShoppingBag, ShoppingCart, ClipboardList, Sparkles, BarChart3 } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";

export type View = "shop" | "chat" | "cart" | "orders" | "dash" | "seat" | "eval" | "mem" | "skills" | "ops";

const BASE_TABS: { v: View; icon: React.ReactNode; label: string }[] = [
  { v: "shop", icon: <ShoppingBag className="h-4 w-4" />, label: "商城" },
  { v: "chat", icon: <MessageSquare className="h-4 w-4" />, label: "聊天" },
  { v: "orders", icon: <ClipboardList className="h-4 w-4" />, label: "我的订单" },
  { v: "dash", icon: <LayoutDashboard className="h-4 w-4" />, label: "看板" },
  { v: "seat", icon: <Headset className="h-4 w-4" />, label: "工作台" },
  { v: "eval", icon: <FlaskConical className="h-4 w-4" />, label: "评估" },
  { v: "mem", icon: <Brain className="h-4 w-4" />, label: "记忆" },
  { v: "skills", icon: <Sparkles className="h-4 w-4" />, label: "Skill" },
  { v: "ops", icon: <BarChart3 className="h-4 w-4" />, label: "经营" },
];

export function AppShell({ view, onView, onReset, children, showCart = false, cartCount = 0 }: {
  view: View; onView: (v: View) => void; onReset: () => void; children: React.ReactNode;
  // N5:购物车 Tab 只在开关打开时出现,关闭时退回改造前的 Tab 列表(逐字节一致)。
  showCart?: boolean;
  cartCount?: number;
}) {
  const tabs = showCart
    ? [
        ...BASE_TABS.slice(0, 1),
        { v: "cart" as View, icon: <ShoppingCart className="h-4 w-4" />, label: "购物车" },
        ...BASE_TABS.slice(1),
      ]
    : BASE_TABS;

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center gap-3 border-b bg-card px-4 py-2">
        <div className="flex items-center gap-2">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary text-primary-foreground font-bold">夕</div>
          <div className="text-sm font-semibold">并夕夕 · 小夕</div>
          <span className="rounded bg-emerald-500 px-1.5 py-0.5 text-[10px] font-bold text-white" title="构建版本:用户隔离+历史回显">v2·隔离版</span>
        </div>
        <nav className="flex gap-1">
          {tabs.map((t) => (
            <button
              key={t.v}
              onClick={() => onView(t.v)}
              className={cn(
                "relative flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm transition-colors",
                view === t.v ? "bg-primary text-primary-foreground" : "text-foreground hover:bg-secondary"
              )}
            >
              {t.icon} {t.label}
              {t.v === "cart" && cartCount > 0 && (
                <span className="ml-0.5 inline-flex h-4 min-w-4 items-center justify-center rounded-full bg-red-500 px-1 text-[10px] font-bold text-white">
                  {cartCount > 99 ? "99+" : cartCount}
                </span>
              )}
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
