import { useEffect, useState } from "react";
import { createOrder, getCart, removeFromCartApi, setCartQuantity, type CartItem } from "@/lib/api";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Button } from "@/components/ui/button";
import { ShoppingCart, Minus, Plus, Trash2 } from "lucide-react";
import { ProductThumb } from "@/components/ProductThumb";

// 购物车:只收集意向,**不做结算**——「去下单」走既有自助下单路径
// (POST /api/order),下单成功后后端会把该 sku 的购物车行标记为 converted,
// 所以这里下单成功后重新拉一次列表即可,不需要前端自己再调一次转化。
//
// 每张卡片各自持有 busy/error 状态(与 SkillsView 的上传/提炼卡片同一惯例):
// 一件商品的操作失败不该把别的行也弄得不能点。
export function CartView({ onShop, onCartChanged }: { onShop: () => void; onCartChanged?: () => void }) {
  const [items, setItems] = useState<CartItem[] | null>(null);
  const [listErr, setListErr] = useState("");
  const [busySku, setBusySku] = useState<string | null>(null);
  const [rowErr, setRowErr] = useState<Record<string, string>>({});

  async function load() {
    try {
      setItems(await getCart());
      setListErr("");
      onCartChanged?.();   // 同步 AppShell 购物车 Tab 上的件数徽标
    } catch (e) {
      setListErr(String(e));
    }
  }
  useEffect(() => { load(); }, []);

  function setErr(sku: string, msg: string) {
    setRowErr((prev) => ({ ...prev, [sku]: msg }));
  }
  function clearErr(sku: string) {
    setRowErr((prev) => {
      if (!(sku in prev)) return prev;
      const next = { ...prev };
      delete next[sku];
      return next;
    });
  }

  async function changeQuantity(item: CartItem, delta: number) {
    const next = item.quantity + delta;
    setBusySku(item.sku);
    clearErr(item.sku);
    try {
      // 增/减都走同一个「设置数量」端点(PUT /api/cart/{sku}),不按方向拆成
      // 两条不同路径——唯一的例外是减到 0:那不是"数量",是"移除",走既有
      // 的显式 DELETE,而不是拿 0 去调设置数量接口(后端会拒绝非正数)。
      if (next <= 0) {
        await removeFromCartApi(item.sku);
      } else {
        await setCartQuantity(item.sku, next);
      }
      await load();
    } catch (e) {
      setErr(item.sku, String(e));
    } finally {
      setBusySku(null);
    }
  }

  async function removeItem(item: CartItem) {
    setBusySku(item.sku);
    clearErr(item.sku);
    try {
      await removeFromCartApi(item.sku);
      await load();
    } catch (e) {
      setErr(item.sku, String(e));
    } finally {
      setBusySku(null);
    }
  }

  async function placeOrder(item: CartItem) {
    setBusySku(item.sku);
    clearErr(item.sku);
    try {
      const r = await createOrder(item.sku, item.quantity);
      await load();   // 下单成功后端已把该行标记为 converted,重新拉一次列表即可
      alert(`下单成功！订单号 ${r.order_id}（${r.status_label}），实付 ¥${r.total}`);
    } catch (e) {
      setErr(item.sku, String(e));
    } finally {
      setBusySku(null);
    }
  }

  const totalQty = (items || []).reduce((s, it) => s + it.quantity, 0);
  // 合计只累加**算得出**的行。有商品查不到时不把它当 0 混进总额——那会给出
  // 一个偏低且看不出来的数字;改为把合计标成"不含 N 件"。
  const priced = (items || []).filter((it) => it.subtotal != null);
  const totalAmount = priced.reduce((s, it) => s + (it.subtotal || 0), 0);
  const unpricedCount = (items || []).length - priced.length;

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-3 border-b bg-card/50 px-6 py-2">
        <span className="text-sm font-semibold">购物车</span>
        <span className="text-xs text-muted-foreground">共 {totalQty} 件</span>
        {items && items.length > 0 && (
          <span className="ml-auto text-xs text-muted-foreground" data-testid="cart-total">
            合计 <b className="text-base text-red-500">¥{totalAmount.toFixed(2)}</b>
            {unpricedCount > 0 && (
              <span className="ml-1 text-destructive">（不含 {unpricedCount} 件无法定价的商品）</span>
            )}
          </span>
        )}
      </div>
      <ScrollArea className="min-h-0 flex-1">
        {listErr && (
          <div className="p-4 text-sm text-destructive">读取失败：{listErr}</div>
        )}
        {items === null ? (
          <div className="p-10 text-center text-sm text-muted-foreground">加载中…</div>
        ) : items.length === 0 ? (
          <div className="flex flex-col items-center gap-3 p-16 text-center text-sm text-muted-foreground">
            <ShoppingCart className="h-10 w-10 opacity-40" />
            <div>购物车空空如也，去商城逛逛吧～</div>
            <Button size="sm" onClick={onShop}>去商城</Button>
          </div>
        ) : (
          <div className="mx-auto flex max-w-2xl flex-col gap-3 p-5">
            {items.map((it) => {
              const busy = busySku === it.sku;
              const err = rowErr[it.sku];
              return (
                <div key={it.sku} className="rounded-xl border bg-card p-4 shadow-sm">
                  <div className="flex items-start gap-3">
                    <ProductThumb id={it.sku} src={it.image || undefined}
                                  title={it.title || undefined}
                                  className="h-14 w-14 shrink-0 rounded-lg" />
                    <div className="min-w-0 flex-1">
                      {/* 商品名查不到时退回 sku,但**明说**是查不到,而不是把一个
                          原始 id 当商品名摆着让买家猜。 */}
                      <div className="truncate text-sm font-medium" title={it.title || it.sku}>
                        {it.title || `商品 ${it.sku}`}
                      </div>
                      {it.product_missing ? (
                        <div className="mt-0.5 text-xs text-destructive">
                          商品信息暂时查不到（可能已下架），下单前请先确认
                        </div>
                      ) : (
                        <div className="mt-0.5 flex items-baseline gap-2 text-xs">
                          <span className="text-red-500">单价 ¥{it.price}</span>
                          <span className="text-muted-foreground">
                            小计 <b className="text-foreground">¥{it.subtotal}</b>
                          </span>
                          {typeof it.stock === "number" && (
                            <span className="text-muted-foreground">库存 {it.stock}</span>
                          )}
                        </div>
                      )}
                    </div>
                    <Button variant="ghost" size="sm" disabled={busy}
                      onClick={() => removeItem(it)}>
                      <Trash2 className="h-3.5 w-3.5" /> 移除
                    </Button>
                  </div>
                  <div className="mt-3 flex items-center justify-between">
                    <div className="flex items-center gap-2">
                      <Button variant="outline" size="sm" className="h-7 w-7 p-0" disabled={busy}
                        aria-label="减少数量" onClick={() => changeQuantity(it, -1)}>
                        <Minus className="h-3.5 w-3.5" />
                      </Button>
                      <span className="w-6 text-center text-sm">{it.quantity}</span>
                      <Button variant="outline" size="sm" className="h-7 w-7 p-0" disabled={busy}
                        aria-label="增加数量" onClick={() => changeQuantity(it, 1)}>
                        <Plus className="h-3.5 w-3.5" />
                      </Button>
                    </div>
                    {/* 金额写在按钮上,让买家在**点下去之前**就知道要付多少。
                        改造前按钮只写「去下单」,金额是下完单才弹窗告知的——
                        那是让人闭着眼睛付钱。 */}
                    <Button size="sm" disabled={busy} onClick={() => placeOrder(it)}>
                      {busy ? "处理中…"
                            : it.subtotal != null ? `去下单 ¥${it.subtotal}` : "去下单"}
                    </Button>
                  </div>
                  {err && <div role="alert" className="mt-2 text-xs text-destructive">⚠️ {err}</div>}
                </div>
              );
            })}
          </div>
        )}
      </ScrollArea>
    </div>
  );
}
