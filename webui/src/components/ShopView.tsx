import { useEffect, useState } from "react";
import { addToCartApi, getProducts, type Product } from "@/lib/api";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { ProductThumb } from "@/components/ProductThumb";

export function ShopView({ onConsult, onBuy, showCart = false, onCartChanged, buying = false }: {
  onConsult: (itemId: string) => void;
  onBuy: (itemId: string) => void;
  // 下单在途:按钮变灰是给买家的反馈,真正拦住第二次请求的是 App.onBuy 里的 ref
  // (双击下两笔单是走查时实测到的缺陷)。
  buying?: boolean;
  // N5:购物车开关关闭时,商品卡退回改造前的样子(只有「咨询」「立即购买」),
  // 不出现「加入购物车」——这是买家可见语义的一部分,不能只靠后端隐式兜底。
  showCart?: boolean;
  onCartChanged?: () => void;
}) {
  const [items, setItems] = useState<Product[] | null>(null);
  // 商品服务连不上 ≠ 这家店没有商品。两者都是空列表,但一个要显示"故障 + 怎么修",
  // 另一个才是"暂无商品"。把它们合成一句会让一次内网故障在买家眼里变成空店铺。
  const [degraded, setDegraded] = useState<string>("");
  const [q, setQ] = useState("");
  // 每张商品卡各自持有加购的忙态/错误(与 SkillsView 各卡片自管状态同一惯例):
  // 一件商品加购失败不该让别的商品卡也显示"加购中"或残留错误提示。
  const [cartBusy, setCartBusy] = useState<Record<string, boolean>>({});
  const [cartErr, setCartErr] = useState<Record<string, string>>({});

  async function load() {
    setItems(null);
    const r = await getProducts();
    setItems(r.products);
    setDegraded(r.degraded ? (r.reason || "商品服务不可用") : "");
  }
  useEffect(() => { load(); }, []);

  async function onAddToCart(id: string) {
    setCartBusy((prev) => ({ ...prev, [id]: true }));
    setCartErr((prev) => ({ ...prev, [id]: "" }));
    try {
      await addToCartApi(id, 1);
      onCartChanged?.();
    } catch (e) {
      setCartErr((prev) => ({ ...prev, [id]: String(e) }));
    } finally {
      setCartBusy((prev) => ({ ...prev, [id]: false }));
    }
  }

  const shown = (items || []).filter((p) =>
    !q.trim() || (p.title || "").toLowerCase().includes(q.trim().toLowerCase()));

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-3 border-b bg-card/50 px-6 py-2">
        <span className="text-sm font-semibold">商城</span>
        <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="搜索商品"
          className="h-7 w-48 rounded-md border bg-background px-2.5 text-xs outline-none" />
        {/* 加载中不报"0 件商品":那是一个还不知道的事实,写出来就是错的
            (改造前头部与主体会同时显示「0 件商品」和「加载中…」)。 */}
        <span className="ml-auto text-xs text-muted-foreground">
          {items === null ? "加载中…" : `${shown.length} 件商品`}
        </span>
      </div>
      {degraded && (
        <div role="alert" data-testid="shop-degraded"
             className="flex flex-wrap items-center gap-2 border-b border-destructive/40
                        bg-destructive/10 px-6 py-2 text-xs text-destructive">
          <span>⚠️ 商品服务暂时不可用：{degraded}。下面显示的<b>不是</b>真实的在售商品清单。</span>
          <button className="underline underline-offset-2" onClick={load}>重试</button>
        </div>
      )}
      <ScrollArea className="min-h-0 flex-1">
        {items === null ? (
          <div className="p-10 text-center text-sm text-muted-foreground">加载中…</div>
        ) : shown.length === 0 ? (
          <div className="p-10 text-center text-sm text-muted-foreground">
            {degraded
              ? "商品服务连不上，无法展示商品。排查：hmdp 是否在跑、hmdp_base_url 是否正确、进程是否被 HTTP_PROXY 劫持。"
              : q.trim() ? `没有匹配「${q.trim()}」的商品` : "本店暂无在售商品"}
          </div>
        ) : (
          <div className="grid grid-cols-2 gap-4 p-5 md:grid-cols-3 lg:grid-cols-4">
            {shown.map((p) => (
              <Card key={p.id} className="flex flex-col overflow-hidden">
                <ProductThumb id={p.id} src={p.image} title={p.title} className="aspect-square" />
                <div className="flex flex-1 flex-col gap-1 p-3">
                  <div className="line-clamp-2 text-sm font-medium" title={p.title}>{p.title}</div>
                  <div className="flex items-baseline gap-2">
                    <span className="text-base font-semibold text-red-500">¥{p.price}</span>
                    <span className="text-[11px] text-muted-foreground">库存 {p.stock}</span>
                  </div>
                  <div className="mt-auto flex gap-1.5 pt-1">
                    <Button size="sm" variant="outline" className="flex-1" onClick={() => onConsult(p.id)}>咨询</Button>
                    {showCart && (
                      <Button size="sm" variant="outline" className="flex-1" disabled={!!cartBusy[p.id]}
                        onClick={() => onAddToCart(p.id)}>
                        {cartBusy[p.id] ? "加购中…" : "加入购物车"}
                      </Button>
                    )}
                    <Button size="sm" className="flex-1 bg-red-500 hover:bg-red-600" disabled={buying}
                            onClick={() => onBuy(p.id)}>{buying ? "处理中…" : "立即购买"}</Button>
                  </div>
                  {cartErr[p.id] && (
                    <div role="alert" className="pt-1 text-[11px] text-destructive">⚠️ {cartErr[p.id]}</div>
                  )}
                </div>
              </Card>
            ))}
          </div>
        )}
      </ScrollArea>
    </div>
  );
}
