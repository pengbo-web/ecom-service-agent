import { useEffect, useState } from "react";
import { getMyOrders, getReviewableItems, payOrder, type MyOrder, type ReviewableItem } from "@/lib/api";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Button } from "@/components/ui/button";
import { ReviewDialog } from "@/components/ReviewDialog";
import { RotateCcw, PackageOpen } from "lucide-react";

// 用 "order_id::sku" 作为一个订单项的判重键,与后端 reviews 表的
// UNIQUE(order_id, sku) 同一套颗粒度——一单里的每个 sku 各自独立可评。
function reviewKey(orderId: string, sku: string): string {
  return `${orderId}::${sku}`;
}

// "我的订单":用户自助下单后在此查看。数据来自 agent 订单库(list_orders),
// 与客服的 list_user_orders 同源,故 AI 也能查到这些自助单。
export function OrdersView({ onShop }: { onShop: () => void }) {
  const [orders, setOrders] = useState<MyOrder[] | null>(null);
  // 可评价的 (order_id, sku) 集合,来自 /api/reviewable(只含已签收且未评过的
  // 订单项)。已评过的组合不在其中,据此判断某个商品该显示「评价」还是
  // 「已评价」——不在前端自己猜测"是否已签收/已评过",服务端才是唯一事实来源。
  const [reviewable, setReviewable] = useState<Set<string> | null>(null);
  const [reviewItem, setReviewItem] = useState<ReviewableItem | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  // 「去支付」各自持有忙态/错误(与评价按钮同一惯例:一笔订单支付失败
  // 不该影响别的订单继续可点)。
  const [payBusy, setPayBusy] = useState<string | null>(null);
  const [payErr, setPayErr] = useState<Record<string, string>>({});

  async function load() {
    setOrders(await getMyOrders());
    try {
      const items = await getReviewableItems();
      setReviewable(new Set(items.map((it) => reviewKey(it.order_id, it.sku))));
    } catch {
      // 可评价列表拉取失败不阻塞订单列表本身的展示,只是「评价」按钮先不出现。
      setReviewable(new Set());
    }
  }
  useEffect(() => { load(); }, []);

  async function onPay(orderId: string) {
    setPayBusy(orderId);
    setPayErr((prev) => ({ ...prev, [orderId]: "" }));
    try {
      await payOrder(orderId);
      await load();   // 刷新:状态由"待支付"变为"待发货"
    } catch (e) {
      setPayErr((prev) => ({ ...prev, [orderId]: String(e) }));
    } finally {
      setPayBusy(null);
    }
  }

  function openReview(orderId: string, sku: string, name: string) {
    setReviewItem({ order_id: orderId, sku, name, delivered_at: null });
    setDialogOpen(true);
  }

  // 提交成功后立刻把这一项从「可评价」集合里摘掉,按钮马上变「已评价」,
  // 不必等下一次整页刷新——这正是"提交成功后按钮变已评价"这条体验要求。
  function onReviewed(item: ReviewableItem) {
    setReviewable((prev) => {
      const next = new Set(prev);
      next.delete(reviewKey(item.order_id, item.sku));
      return next;
    });
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-3 border-b bg-card/50 px-6 py-2">
        <span className="text-sm font-semibold">我的订单</span>
        <span className="text-xs text-muted-foreground">{orders?.length ?? 0} 笔</span>
        <Button variant="ghost" size="sm" className="ml-auto" onClick={load}>
          <RotateCcw className="h-3.5 w-3.5" /> 刷新
        </Button>
      </div>
      <ScrollArea className="min-h-0 flex-1">
        {orders === null ? (
          <div className="p-10 text-center text-sm text-muted-foreground">加载中…</div>
        ) : orders.length === 0 ? (
          <div className="flex flex-col items-center gap-3 p-16 text-center text-sm text-muted-foreground">
            <PackageOpen className="h-10 w-10 opacity-40" />
            <div>还没有订单，去商城逛逛吧～</div>
            <Button size="sm" onClick={onShop}>去商城</Button>
          </div>
        ) : (
          <div className="mx-auto flex max-w-2xl flex-col gap-3 p-5">
            {orders.map((o) => (
              <div key={o.order_id} className="rounded-xl border bg-card p-4 shadow-sm">
                <div className="flex items-center justify-between border-b pb-2">
                  <span className="font-mono text-xs text-muted-foreground">{o.order_id}</span>
                  <span className="rounded-full bg-amber-500/15 px-2 py-0.5 text-xs font-medium text-amber-600 dark:text-amber-400">
                    {o.status_label}
                  </span>
                </div>
                <div className="flex flex-col gap-1.5 py-2">
                  {o.items.map((it, i) => {
                    // 只有已签收订单才可能出现评价入口——status 是原始状态值
                    // (非展示用的 status_label),与后端"只有 delivered 可评"
                    // 这条硬约束保持同一判据。reviewable 还没加载完(null)时
                    // 先不渲染任何评价态,避免"已评价"先闪一下再变"评价"。
                    const canShowReview = reviewable !== null && o.status === "delivered" && !!it.sku;
                    const isReviewable = canShowReview && reviewable.has(reviewKey(o.order_id, it.sku));
                    return (
                      <div key={i} className="flex items-center justify-between text-sm">
                        <span className="line-clamp-1">{it.name}</span>
                        <div className="flex shrink-0 items-center gap-2">
                          <span className="text-muted-foreground">×{it.quantity}</span>
                          {canShowReview && (
                            isReviewable ? (
                              <Button
                                size="sm"
                                variant="outline"
                                className="h-6 px-2 text-[11px]"
                                onClick={() => openReview(o.order_id, it.sku, it.name)}
                              >
                                评价
                              </Button>
                            ) : (
                              <span className="rounded bg-secondary px-1.5 py-0.5 text-[11px] text-muted-foreground">
                                已评价
                              </span>
                            )
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>
                <div className="flex items-center justify-between border-t pt-2 text-xs text-muted-foreground">
                  <span>{o.created_at}</span>
                  <div className="flex items-center gap-2">
                    <span>实付 <span className="text-base font-semibold text-red-500">¥{o.total}</span></span>
                    {o.status === "unpaid" && (
                      <Button size="sm" className="h-7 bg-red-500 px-3 text-xs hover:bg-red-600"
                        disabled={payBusy === o.order_id}
                        onClick={() => onPay(o.order_id)}>
                        {payBusy === o.order_id ? "支付中…" : "去支付"}
                      </Button>
                    )}
                  </div>
                </div>
                {payErr[o.order_id] && (
                  <div role="alert" className="pt-1.5 text-[11px] text-destructive">⚠️ {payErr[o.order_id]}</div>
                )}
              </div>
            ))}
          </div>
        )}
      </ScrollArea>
      <ReviewDialog
        item={reviewItem}
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        onSubmitted={onReviewed}
      />
    </div>
  );
}
