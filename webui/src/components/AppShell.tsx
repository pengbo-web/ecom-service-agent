import * as React from "react";
import {
  MessageSquare, LayoutDashboard, Headset, FlaskConical, Brain, RotateCcw,
  ShoppingBag, ShoppingCart, ClipboardList, Sparkles, BarChart3, Network,
  Moon, Sun, PanelLeftClose, PanelLeft, BookOpen,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";

export type View = "shop" | "chat" | "cart" | "orders" | "dash" | "seat" | "eval"
  | "mem" | "skills" | "ops" | "collab" | "kb";

type Tab = { v: View; icon: React.ReactNode; label: string; hint: string };

// 按**谁在用**分组,而不是按功能类型。原来九个 tab 平铺在顶栏,买家的"我的订单"
// 和运维的"评估"并排——看不出这个系统同时服务三类角色,而那正是它的架构。
const GROUPS: { title: string; tabs: Tab[] }[] = [
  {
    title: "买家侧",
    tabs: [
      { v: "shop", icon: <ShoppingBag className="h-4 w-4" />, label: "商城", hint: "商品浏览与下单" },
      { v: "chat", icon: <MessageSquare className="h-4 w-4" />, label: "聊天", hint: "买家与客服 Agent 对话" },
      { v: "orders", icon: <ClipboardList className="h-4 w-4" />, label: "我的订单", hint: "买家订单与评价" },
    ],
  },
  {
    title: "坐席侧",
    tabs: [
      { v: "seat", icon: <Headset className="h-4 w-4" />, label: "客服工作台", hint: "人工接管与转人工队列" },
    ],
  },
  {
    title: "经营侧",
    tabs: [
      { v: "ops", icon: <BarChart3 className="h-4 w-4" />, label: "经营控制台", hint: "诊断 / 商机 / 触达审批" },
      { v: "collab", icon: <Network className="h-4 w-4" />, label: "多智能体协作", hint: "协作链时间线与编排规则" },
    ],
  },
  {
    title: "系统侧",
    tabs: [
      { v: "kb", icon: <BookOpen className="h-4 w-4" />, label: "知识库", hint: "政策文档上传与索引状态" },
      { v: "dash", icon: <LayoutDashboard className="h-4 w-4" />, label: "可观测看板", hint: "调用链 / 成本 / 护栏" },
      { v: "skills", icon: <Sparkles className="h-4 w-4" />, label: "Skill 自进化", hint: "候选 / 灰度 / 转正" },
      { v: "mem", icon: <Brain className="h-4 w-4" />, label: "记忆", hint: "长期记忆与固化" },
      { v: "eval", icon: <FlaskConical className="h-4 w-4" />, label: "评估", hint: "回归集与基线对比" },
    ],
  },
];

const THEME_KEY = "xiaoxi_theme";

/** 深色开关。初值取上次选择,没存过则跟随系统——首次打开就该是对的。 */
function useTheme(): [boolean, () => void] {
  const [dark, setDark] = React.useState<boolean>(() => {
    if (typeof window === "undefined") return false;
    const saved = localStorage.getItem(THEME_KEY);
    if (saved) return saved === "dark";
    return !!window.matchMedia?.("(prefers-color-scheme: dark)").matches;
  });
  React.useEffect(() => {
    if (typeof document === "undefined") return;
    document.documentElement.classList.toggle("dark", dark);
    localStorage.setItem(THEME_KEY, dark ? "dark" : "light");
  }, [dark]);
  return [dark, () => setDark((d) => !d)];
}

/** 侧栏折叠态:窄屏默认收起,手动切换后在本次宽度档位内尊重人的选择。
 *
 * 固定 224px 的侧栏在 ~1000px 以下会吃掉三分之一横向空间,把内容区挤到标题
 * 竖排断行——控制台里的表格与时间线本来就横向吃紧,这不是"看着挤"而是读不了。
 * 但也不能做成纯自动:宽屏用户想收起来腾地方看长表格,得允许。
 *
 * 宽度档位变化时清掉手动选择:否则在宽屏展开后缩窗口,侧栏会固执地保持展开,
 * 又回到挤压的老问题——人的那次选择针对的是当时的宽度,不是永久偏好。
 */
function useSidebarCollapsed(): [boolean, () => void] {
  const [manual, setManual] = React.useState<boolean | null>(null);
  const [narrow, setNarrow] = React.useState(false);

  React.useEffect(() => {
    const mq = typeof window !== "undefined" && window.matchMedia
      ? window.matchMedia("(max-width: 1024px)") : null;
    if (!mq) return;
    const sync = () => { setNarrow(mq.matches); setManual(null); };
    setNarrow(mq.matches);
    // Safari <14 只有 addListener;两种都试,拿不到监听能力也不该让组件炸掉。
    mq.addEventListener?.("change", sync);
    return () => mq.removeEventListener?.("change", sync);
  }, []);

  const collapsed = manual ?? narrow;
  return [collapsed, () => setManual(!collapsed)];
}

export function AppShell({ view, onView, onReset, children, showCart = false, cartCount = 0 }: {
  view: View; onView: (v: View) => void; onReset: () => void; children: React.ReactNode;
  // N5:购物车入口只在开关打开时出现,关闭时退回改造前的样子。
  showCart?: boolean;
  cartCount?: number;
}) {
  const [dark, toggleTheme] = useTheme();
  const [collapsed, setCollapsed] = useSidebarCollapsed();

  const groups = React.useMemo(() => GROUPS.map((g) => (
    g.title !== "买家侧" || !showCart ? g : {
      ...g,
      tabs: [
        g.tabs[0],
        { v: "cart" as View, icon: <ShoppingCart className="h-4 w-4" />, label: "购物车", hint: "加购意向,不做结算" },
        ...g.tabs.slice(1),
      ],
    }
  )), [showCart]);

  const current = groups.flatMap((g) => g.tabs).find((t) => t.v === view);

  return (
    <div className="flex h-full">
      <aside className={cn(
        "flex shrink-0 flex-col bg-sidebar text-sidebar-foreground transition-[width] duration-150",
        collapsed ? "w-[4.25rem]" : "w-56",
      )}>
        <div className="flex items-center gap-2 px-3 py-3">
          <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg
                          bg-primary font-bold text-primary-foreground">夕</div>
          {!collapsed && (
            <div className="min-w-0">
              <div className="truncate text-sm font-semibold text-white">并夕夕 · 小夕</div>
              <div className="truncate text-[10px] text-sidebar-muted">企业级智能客服控制台</div>
            </div>
          )}
        </div>

        <nav className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
          {groups.map((g) => (
            <div key={g.title} className="mb-3">
              {/* 折叠时用一条分隔线代替组标题:省掉标题却让四组挤成一列,
                  会让人以为它们是同一类东西——分组本身是要传达的信息。 */}
              {collapsed
                ? <div className="mx-2 mb-1.5 h-px bg-white/10" />
                : <div className="px-2 pb-1 text-[10px] font-medium uppercase tracking-wider text-sidebar-muted">
                    {g.title}
                  </div>}
              <div className="flex flex-col gap-0.5">
                {g.tabs.map((t) => (
                  <button
                    key={t.v}
                    onClick={() => onView(t.v)}
                    title={collapsed ? `${t.label} — ${t.hint}` : t.hint}
                    aria-current={view === t.v ? "page" : undefined}
                    className={cn(
                      "relative flex items-center gap-2.5 rounded-md px-2.5 py-2 text-sm transition-colors",
                      collapsed && "justify-center px-0",
                      view === t.v
                        ? "bg-white/10 text-white"
                        : "text-sidebar-foreground hover:bg-white/5 hover:text-white",
                    )}
                  >
                    {/* 选中态除了背景还加一条左侧色条:纯背景差在投影仪/低对比度
                        屏幕上经常看不出来,而"我现在在哪一页"不该靠猜。 */}
                    {view === t.v && (
                      <span className="absolute left-0 top-1/2 h-5 w-0.5 -translate-y-1/2
                                       rounded-r bg-sidebar-active" />
                    )}
                    <span className="shrink-0">{t.icon}</span>
                    {!collapsed && <span className="truncate">{t.label}</span>}
                    {t.v === "cart" && cartCount > 0 && (
                      <span className={cn(
                        "inline-flex h-4 min-w-4 items-center justify-center rounded-full",
                        "bg-red-500 px-1 text-[10px] font-bold text-white",
                        collapsed ? "absolute right-2 top-1.5" : "ml-auto",
                      )}>
                        {cartCount > 99 ? "99+" : cartCount}
                      </span>
                    )}
                  </button>
                ))}
              </div>
            </div>
          ))}
        </nav>

        <button
          onClick={setCollapsed}
          title={collapsed ? "展开侧栏" : "收起侧栏"}
          className="flex items-center gap-2 border-t border-white/10 px-4 py-2.5
                     text-xs text-sidebar-muted transition-colors hover:text-white"
        >
          {collapsed ? <PanelLeft className="h-4 w-4" /> : <PanelLeftClose className="h-4 w-4" />}
          {!collapsed && "收起"}
        </button>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex items-center gap-3 border-b bg-card px-5 py-2.5">
          <div className="min-w-0">
            <h1 className="truncate text-sm font-semibold">{current?.label || ""}</h1>
            {current?.hint && (
              <p className="truncate text-[11px] text-muted-foreground">{current.hint}</p>
            )}
          </div>
          {/* shrink-0:右侧这组按钮不参与压缩,否则窄屏下 flex 会先挤它们、
              再挤标题,最后两边都断行成竖排。 */}
          <div className="ml-auto flex shrink-0 items-center gap-1.5">
            <span className="rounded bg-emerald-500/15 px-1.5 py-0.5 text-[10px] font-medium
                             text-emerald-700 dark:text-emerald-400"
                  title="构建版本:用户隔离+历史回显">v2 · 隔离版</span>
            <Button variant="ghost" size="sm" onClick={toggleTheme}
                    title={dark ? "切到浅色" : "切到深色"} aria-label="切换主题">
              {dark ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
            </Button>
            <Button variant="ghost" size="sm" onClick={onReset}>
              <RotateCcw className="h-4 w-4" /> 重置对话
            </Button>
          </div>
        </header>
        <main className="min-h-0 flex-1 overflow-hidden">{children}</main>
      </div>
    </div>
  );
}
