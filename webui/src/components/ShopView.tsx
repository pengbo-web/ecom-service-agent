import { useEffect, useState } from "react";
import { getProducts, type Product } from "@/lib/api";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";

export function ShopView({ onConsult, onBuy }: { onConsult: (itemId: string) => void; onBuy: (itemId: string) => void }) {
  const [items, setItems] = useState<Product[] | null>(null);
  const [q, setQ] = useState("");

  useEffect(() => { getProducts().then(setItems); }, []);

  const shown = (items || []).filter((p) =>
    !q.trim() || (p.title || "").toLowerCase().includes(q.trim().toLowerCase()));

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-3 border-b bg-card/50 px-6 py-2">
        <span className="text-sm font-semibold">商城</span>
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="搜索商品"
          className="h-7 w-48 rounded-md border bg-background px-2.5 text-xs outline-none" />
        <span className="ml-auto text-xs text-muted-foreground">{shown.length} 件商品</span>
      </div>
      <ScrollArea className="min-h-0 flex-1">
        {items === null ? (
          <div className="p-10 text-center text-sm text-muted-foreground">加载中…</div>
        ) : shown.length === 0 ? (
          <div className="p-10 text-center text-sm text-muted-foreground">暂无商品(确认 hmdp 后端在跑)</div>
        ) : (
          <div className="grid grid-cols-2 gap-4 p-5 md:grid-cols-3 lg:grid-cols-4">
            {shown.map((p) => (
              <Card key={p.id} className="flex flex-col overflow-hidden">
                <div className="flex aspect-square items-center justify-center bg-secondary/40 text-3xl">
                  {p.image
                    ? <img src={p.image} alt={p.title} className="h-full w-full object-cover"
                        onError={(e) => { (e.target as HTMLImageElement).style.display = "none"; }} />
                    : "🛍️"}
                </div>
                <div className="flex flex-1 flex-col gap-1 p-3">
                  <div className="line-clamp-2 text-sm font-medium" title={p.title}>{p.title}</div>
                  <div className="flex items-baseline gap-2">
                    <span className="text-base font-semibold text-red-500">¥{p.price}</span>
                    <span className="text-[11px] text-muted-foreground">库存 {p.stock}</span>
                  </div>
                  <div className="mt-auto flex gap-1.5 pt-1">
                    <Button size="sm" variant="outline" className="flex-1" onClick={() => onConsult(p.id)}>咨询</Button>
                    <Button size="sm" className="flex-1 bg-red-500 hover:bg-red-600" onClick={() => onBuy(p.id)}>立即购买</Button>
                  </div>
                </div>
              </Card>
            ))}
          </div>
        )}
      </ScrollArea>
    </div>
  );
}
