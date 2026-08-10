import { type Product } from "@/lib/api";
import { ProductThumb } from "@/components/ProductThumb";

// 会话内商品卡片(对齐千牛/闲鱼:顾客带商品进客服时,对话顶部展示当前咨询商品)。
// onAsk 把快捷问句作为一条用户消息发出去(AI 已绑定当前商品,会接地回答)。
export function ProductCard({ product, onAsk, onBuy }: {
  product: Product; onAsk: (text: string) => void; onBuy?: (itemId: string) => void;
}) {
  const chips = [
    { label: "规格属性", msg: "这个商品有哪些规格和属性？" },
    { label: "有货吗", msg: "现在有货吗，什么时候能发货？" },
    { label: "能便宜吗", msg: "这个能便宜点吗，帮我砍砍价" },
  ];
  return (
    <div className="mx-auto w-full max-w-md overflow-hidden rounded-xl border bg-card shadow-sm">
      <div className="flex gap-3 p-3">
        <ProductThumb id={product.id} src={product.image} title={product.title}
                      className="h-20 w-20 shrink-0 rounded-lg" />
        <div className="min-w-0 flex-1">
          <div className="line-clamp-2 text-sm font-medium leading-snug" title={product.title}>{product.title}</div>
          <div className="mt-1 flex items-baseline gap-2">
            <span className="text-lg font-semibold text-red-500">¥{product.price}</span>
            <span className="text-[11px] text-muted-foreground">库存 {product.stock}</span>
          </div>
        </div>
      </div>
      <div className="flex flex-col gap-1 border-t px-3 py-2 text-[11px]">
        <div className="flex items-center gap-1.5">
          <span className="text-muted-foreground">保障</span>
          <Tag>7天无理由退货</Tag><Tag>极速退款</Tag>
        </div>
        <div className="flex items-center gap-1.5">
          <span className="text-muted-foreground">物流</span>
          <Tag tone="warn">现货</Tag><span className="text-muted-foreground">现在付款，次日发货</span>
        </div>
      </div>
      <div className="flex flex-wrap items-center gap-1.5 border-t px-3 py-2">
        {chips.map((c) => (
          <button key={c.label} onClick={() => onAsk(c.msg)}
            className="rounded-md border border-primary/40 px-2.5 py-1 text-xs text-primary transition hover:bg-primary/10">
            {c.label}
          </button>
        ))}
        {onBuy && (
          <button onClick={() => onBuy(String(product.id))}
            className="ml-auto rounded-md bg-red-500 px-3.5 py-1.5 text-xs font-medium text-white transition hover:bg-red-600">
            立即购买
          </button>
        )}
      </div>
    </div>
  );
}

function Tag({ children, tone }: { children: React.ReactNode; tone?: "warn" }) {
  return (
    <span className={`rounded px-1.5 py-0.5 ${
      tone === "warn" ? "bg-amber-500/15 text-amber-600 dark:text-amber-400"
        : "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400"}`}>
      {children}
    </span>
  );
}
