import { useEffect, useRef, useState } from "react";
import { AppShell, type View } from "@/components/AppShell";
import { ChatView } from "@/components/ChatView";
import { DashboardView } from "@/components/DashboardView";
import { WorkbenchView } from "@/components/WorkbenchView";
import { ShopView } from "@/components/ShopView";
import { CartView } from "@/components/CartView";
import { OrdersView } from "@/components/OrdersView";
import { EvalView } from "@/components/EvalView";
import { MemoryView } from "@/components/MemoryView";
import { SkillsView } from "@/components/SkillsView";
import { OperationsView } from "@/components/OperationsView";
import { CollabView } from "@/components/CollabView";
import { KnowledgeView } from "@/components/KnowledgeView";
import { LoginCard } from "@/components/LoginCard";
import { adminFetch, openConversation, getUserId, setUserId, me, clearToken,
  getConfig, getToken, setToken, createUser, login, createOrder, getCart } from "@/lib/api";

export default function App() {
  const [view, setView] = useState<View>(
    typeof location !== "undefined" && location.pathname === "/dashboard" ? "dash" : "chat"
  );
  const [userId, setUid] = useState<string>(getUserId());
  // 会话 ID 由服务端签发:挂载/切用户时 open(同用户已有 open 会话则复用,否则新开)。
  const [sessionId, setSessionId] = useState<string>("");
  // 登录门:启动时用已存 token 尝试恢复登录态,恢复失败(无 token/401)回登录卡片。
  const [authedUser, setAuthedUser] = useState<string | null>(null);
  const [authChecked, setAuthChecked] = useState(false);
  const [resetNonce, setResetNonce] = useState(0);   // 重置对话时自增,强制 ChatView 重挂载(会话ID不变也清屏)
  const [itemId, setItemId] = useState<string>(() =>   // 当前咨询商品:初值来自 ?item=,商城点"咨询"时更新
    typeof location !== "undefined" ? (new URLSearchParams(location.search).get("item") || "") : "");

  // N5:购物车 + 真实未支付态由后端开关 unpaid_flow_enabled 决定是否出现。
  // 关闭时退回改造前的样子——不出现购物车 Tab/加购按钮,也不拉取购物车数据。
  const [showCart, setShowCart] = useState(false);
  useEffect(() => { getConfig().then((cfg) => setShowCart(!!cfg.unpaid_flow_enabled)); }, []);

  const [cartCount, setCartCount] = useState(0);
  async function refreshCartCount() {
    if (!showCart) { setCartCount(0); return; }
    try {
      const items = await getCart();
      setCartCount(items.reduce((s, it) => s + it.quantity, 0));
    } catch {
      // 拉取失败不影响其它页面,徽标先保留上一次已知值
    }
  }
  useEffect(() => { if (authedUser && showCart) refreshCartCount(); }, [authedUser, showCart]);

  useEffect(() => {
    (async () => {
      // demo 一键体验:无 token 且后端开 DEMO_MODE 时,自动登录 demo 用户(id 与 hmdp demo 身份一致),
      // 零操作跳过登录卡片,直接进聊天。失败则回落到正常登录门。
      if (!getToken()) {
        const cfg = await getConfig();
        const qsUser = new URLSearchParams(location.search).get("user");
        const demoId = qsUser || cfg.demo_user_id;   // ?user=xxx 以指定客户进入(多窗口演示并发)
        if (cfg.demo_mode && demoId) {
          try {
            const r = await createUser(demoId, "客户 " + demoId);
            setToken(r.token);
          } catch {
            try { const r = await login(demoId); setToken(r.token); } catch { /* 回落登录门 */ }
          }
        }
      }
      const u = await me();
      if (u) { setAuthedUser(u.user_id); setUid(u.user_id); setUserId(u.user_id); }
      else { clearToken(); }
    })().finally(() => setAuthChecked(true));
  }, []);

  useEffect(() => {
    if (!authedUser) return;
    let alive = true;
    openConversation(userId).then((c) => { if (alive) setSessionId(c.conversation_id); });
    return () => { alive = false; };
  }, [userId, authedUser]);

  function onUserId(uid: string) {
    const clean = uid.trim() || "default";
    setUserId(clean);
    setUid(clean);
    setAuthedUser(clean);   // 与 token 对应的登录用户保持一致,防陈旧值(评审建议)
  }

  const [ordersNonce, setOrdersNonce] = useState(0);   // 下单成功后自增,强制"我的订单"重挂载刷新
  // 「立即购买」的在途保护。**实测缺陷**(走查并发与幂等时抓到):这个函数原来没有
  // 任何在途判断,而 `ShopView` 与 `ProductCard` 里的「立即购买」都是裸
  // `<button onClick>`——**双击就是两笔订单**。而购物车那条路的「去下单」一直有
  // `disabled={busy}`:同一件事,一条路上有、另一条没有。
  //
  // 守在这里(而不是只把按钮变灰)是因为它是三个调用点的唯一收口:商城列表、
  // 聊天里的商品卡、以及未来任何新的入口都走它。按钮变灰是给人看的反馈,
  // 这个 ref 才是真正拦住第二次请求的东西。
  //
  // 用 ref 而不是 state:setState 是异步的,两次快速点击可能在同一帧里都读到旧值。
  const buyingRef = useRef(false);
  const [buying, setBuying] = useState(false);

  async function onBuy(id: string) {
    if (buyingRef.current) return;      // 第二次点击直接丢掉,不发第二个请求
    buyingRef.current = true;
    setBuying(true);
    try {
      const r = await createOrder(id, 1);
      setOrdersNonce((n) => n + 1);
      setView("orders");
      refreshCartCount();   // 后端下单成功后会把该商品的购物车行标记为 converted
      alert(`下单成功！订单号 ${r.order_id}（${r.status_label}），实付 ¥${r.total}`);
    } catch {
      alert("下单失败，请确认已登录、商品仍在售");
    } finally {
      buyingRef.current = false;
      setBuying(false);
    }
  }

  async function onReset() {
    const r = await adminFetch("/api/session/reset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId, user_id: userId }),
    });
    const data = await r.json();
    if (data?.conversation_id) setSessionId(data.conversation_id);
    setResetNonce((n) => n + 1);   // 单一连续会话:会话ID重置后不变,用 nonce 强制 ChatView 重挂载清屏
    setView("chat");
  }

  if (!authChecked) return <div className="p-8 text-sm text-muted-foreground">正在恢复登录…</div>;
  if (!authedUser) {
    return (
      <LoginCard
        onLogin={(uid) => {
          setAuthedUser(uid);
          setUid(uid);
          setUserId(uid);
        }}
      />
    );
  }
  if (!sessionId) return <div className="p-8 text-sm text-muted-foreground">正在建立会话…</div>;

  return (
    <AppShell view={view} onView={setView} onReset={onReset} showCart={showCart} cartCount={cartCount}>
      {view === "shop" && <ShopView onConsult={(id) => { setItemId(id); setView("chat"); }} onBuy={onBuy} buying={buying}
        showCart={showCart} onCartChanged={refreshCartCount} />}
      {view === "chat" && <ChatView key={`${sessionId}:${resetNonce}`} sessionId={sessionId} userId={userId} itemId={itemId} onUserId={onUserId} onConversation={setSessionId} onBuy={onBuy} buying={buying} />}
      {view === "cart" && showCart && <CartView onShop={() => setView("shop")} onCartChanged={refreshCartCount} />}
      {view === "orders" && <OrdersView key={ordersNonce} onShop={() => setView("shop")} />}
      {view === "dash" && <DashboardView sessionId={sessionId} />}
      {view === "seat" && <WorkbenchView />}
      {view === "eval" && <EvalView />}
      {view === "mem" && <MemoryView sessionId={sessionId} userId={userId} />}
      {view === "skills" && <SkillsView />}
      {view === "ops" && <OperationsView />}
      {view === "collab" && <CollabView />}
      {view === "kb" && <KnowledgeView />}
    </AppShell>
  );
}
