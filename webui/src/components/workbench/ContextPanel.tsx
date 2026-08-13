import { useEffect, useState } from "react";
import { Card } from "@/components/ui/card";
import { avatarGradient, initials, relativeTime, statusMeta, TONE_CLASS } from "./parts";
import { adminCustomerOrders, type MyOrder, type WbConversation } from "@/lib/api";

// 订单状态 → 色调。已完成/进行中/异常三档,让坐席扫一眼就看出"这个客户手上
// 有没有正在出问题的单"。退款/取消归入异常档——买家找上来多半就是为了它。
const ORDER_TONE: Record<string, string> = {
  refunding: "bg-destructive/10 text-destructive",
  refunded: "bg-destructive/10 text-destructive",
  cancelled: "bg-muted text-muted-foreground",
  unpaid: "bg-amber-500/10 text-amber-700 dark:text-amber-400",
  pending: "bg-sky-500/10 text-sky-700 dark:text-sky-400",
  shipped: "bg-sky-500/10 text-sky-700 dark:text-sky-400",
  delivered: "bg-emerald-500/10 text-emerald-700 dark:text-emerald-400",
};

export function ContextPanel({ conv }: { conv: WbConversation | null }) {
  const userId = conv?.user_id || "";
  const [orders, setOrders] = useState<MyOrder[] | null>(null);
  const [ordersErr, setOrdersErr] = useState("");

  // 切客户就重拉。**必须先清空**:残留上一个客户的订单会让坐席对着 A 的单
  // 跟 B 说话,这是比看不到订单严重得多的错误。
  useEffect(() => {
    if (!userId) { setOrders(null); setOrdersErr(""); return; }
    let alive = true;
    setOrders(null);
    setOrdersErr("");
    adminCustomerOrders(userId)
      .then((r) => {
        if (!alive) return;
        setOrders(r.orders || []);
        // 空列表有两种含义,后端已经分开下发,这里如实透出——把"订单服务连不上"
        // 显示成"该客户暂无订单",坐席会据此对买家说"您名下没有订单"。
        if (r.degraded) setOrdersErr(r.degraded);
      })
      .catch((e) => { if (alive) { setOrders([]); setOrdersErr(String(e)); } });
    return () => { alive = false; };
  }, [userId]);

  if (!conv) return (
    <div className="hidden h-full min-h-0 items-center justify-center border-l bg-card/40 p-6 text-center text-xs text-muted-foreground lg:flex">
      选择左侧会话查看客户信息
    </div>
  );
  const sm = statusMeta(conv);
  return (
    // min-h-0:网格 item 必须允许缩到内容高度以下,否则这一列的长内容
    // 会把整个网格顶开(overflow-y-auto 只在它自己拿到确定高度时才生效)
    <div className="hidden h-full min-h-0 flex-col gap-4 overflow-y-auto border-l bg-card/40 p-4 lg:flex">
      <div className="flex flex-col items-center gap-2 pt-2">
        <span className="flex h-14 w-14 items-center justify-center rounded-full text-lg font-semibold text-white shadow-sm ring-2 ring-background"
          style={{ backgroundImage: avatarGradient(conv.user_id) }}>
          {initials(conv.user_id)}
        </span>
        <span className="text-sm font-semibold">客户 {conv.user_id}</span>
        <span className={`rounded px-2 py-0.5 text-[11px] ${TONE_CLASS[sm.tone]}`}>{sm.label}</span>
      </div>
      <Card className="flex flex-col gap-2 p-3 text-xs">
        <Row k="会话 ID" v={conv.conversation_id} mono />
        <Row k="状态" v={conv.status === "open" ? "进行中" : "已结束"} />
        <Row k="对话轮次" v={String(conv.turns)} />
        <Row k="最后活跃" v={relativeTime(conv.last_active)} />
        <Row k="创建于" v={relativeTime(conv.created_at)} />
        <Row k="接待模式" v={conv.manual ? "人工" : "AI"} />
      </Card>
      {/* 客户订单:坐席台上最该有的一块。买家开口第一句几乎总是关于某一笔单,
          看不到就只能反问"您的订单号是多少"——把 AI 已经知道的事重新问一遍人。 */}
      <div data-testid="customer-orders">
        <div className="mb-1.5 flex items-center gap-2">
          <span className="text-xs font-semibold">该客户订单</span>
          {orders && <span className="text-[11px] text-muted-foreground">{orders.length} 笔</span>}
        </div>
        {ordersErr && (
          <div className="mb-1.5 rounded border border-destructive/40 bg-destructive/10
                          px-2 py-1 text-[11px] text-destructive">
            ⚠️ {ordersErr}。答复买家前请先核实。
          </div>
        )}
        {orders === null && !ordersErr && (
          <div className="text-[11px] text-muted-foreground">加载中…</div>
        )}
        {orders && orders.length === 0 && !ordersErr && (
          <div className="text-[11px] text-muted-foreground">该客户名下暂无订单</div>
        )}
        <div className="flex flex-col gap-1.5">
          {(orders || []).slice(0, 8).map((o) => (
            <Card key={o.order_id} className="p-2 text-[11px]" data-testid={`cust-order-${o.order_id}`}>
              <div className="flex items-center gap-1.5">
                <span className="font-mono">{o.order_id}</span>
                <span className={`ml-auto shrink-0 rounded px-1.5 py-0.5 ${
                  ORDER_TONE[o.status] || "bg-secondary text-muted-foreground"}`}>
                  {o.status_label}
                </span>
              </div>
              <div className="mt-0.5 flex items-center gap-1.5 text-muted-foreground">
                <span className="truncate" title={(o.items || []).map((i) => i.name).join("、")}>
                  {(o.items || []).map((i) => i.name).join("、") || "—"}
                </span>
                <span className="ml-auto shrink-0">¥{o.total}</span>
              </div>
              {o.created_at && (
                <div className="mt-0.5 text-muted-foreground">{o.created_at}</div>
              )}
            </Card>
          ))}
        </div>
        {orders && orders.length > 8 && (
          <div className="mt-1 text-[11px] text-muted-foreground">
            仅显示最近 8 笔（共 {orders.length} 笔）
          </div>
        )}
      </div>

      <p className="text-[11px] leading-relaxed text-muted-foreground">
        提示:AI 会先接待并可查订单/物流/退款/议价;需要人工时在中栏「转人工接管」后回复,客户端即时可见。
      </p>
    </div>
  );
}

function Row({ k, v, mono }: { k: string; v: string; mono?: boolean }) {
  return (
    <div className="flex items-center justify-between gap-2">
      <span className="text-muted-foreground">{k}</span>
      <span className={`truncate ${mono ? "font-mono text-[11px]" : ""}`} title={v}>{v}</span>
    </div>
  );
}
