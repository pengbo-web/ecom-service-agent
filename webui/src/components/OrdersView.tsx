import { useEffect, useState } from "react";
import { getMyOrders, type MyOrder } from "@/lib/api";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Button } from "@/components/ui/button";
import { RotateCcw, PackageOpen } from "lucide-react";

// "我的订单":用户自助下单后在此查看。数据来自 agent 订单库(list_orders),
// 与客服的 list_user_orders 同源,故 AI 也能查到这些自助单。
export function OrdersView({ onShop }: { onShop: () => void }) {
  const [orders, setOrders] = useState<MyOrder[] | null>(null);

  async function load() { setOrders(await getMyOrders()); }
  useEffect(() => { load(); }, []);

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
                  {o.items.map((it, i) => (
                    <div key={i} className="flex items-center justify-between text-sm">
                      <span className="line-clamp-1">{it.name}</span>
                      <span className="shrink-0 text-muted-foreground">×{it.quantity}</span>
                    </div>
                  ))}
                </div>
                <div className="flex items-center justify-between border-t pt-2 text-xs text-muted-foreground">
                  <span>{o.created_at}</span>
                  <span>实付 <span className="text-base font-semibold text-red-500">¥{o.total}</span></span>
                </div>
              </div>
            ))}
          </div>
        )}
      </ScrollArea>
    </div>
  );
}
