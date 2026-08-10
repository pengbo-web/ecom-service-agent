import { useState } from "react";
import { cn } from "@/lib/utils";

/** 商品缩略图:图加载失败(或压根没有)时退到一个**有设计的**占位,而不是空白。
 *
 * 改造前的写法是 `onError` 里 `style.display = "none"`——图挂了就留一个空灰块,
 * 看上去像页面没渲染完。本项目的商品数据里 `images` 指向 `/imgs/products/*.jpg`,
 * 而 hmdp 的前端资源目录下**根本没有 products 这个子目录**,所以在当前数据下
 * 每一张商品图都会走到这条失败分支。
 *
 * 占位不用统一的灰底:那样四件商品长得一模一样,买家扫一眼分不出哪张卡是哪件。
 * 按商品 id 取一个稳定的色相 + 商品名首字,同一件商品在商城、聊天窗内商品卡、
 * 购物车里始终是同一个颜色,起到弱标识作用。
 *
 * 色相用 id 的字符码求和取模,不用随机数——随机会让同一件商品每次渲染换个颜色,
 * 那比灰底更糟。
 */
export function ProductThumb({ src, title, id, className }: {
  src?: string; title?: string; id: string; className?: string;
}) {
  const [failed, setFailed] = useState(false);
  const showImg = !!src && !failed;

  const hue = Array.from(id).reduce((a, c) => a + c.charCodeAt(0), 0) % 360;
  // 首字优先取中文/字母,跳过前导空白与符号;取不到就用一个购物图标兜底。
  const initial = (title || "").trim().replace(/^[^\p{L}\p{N}]+/u, "").charAt(0);

  return (
    <div className={cn("relative flex items-center justify-center overflow-hidden", className)}
         style={showImg ? undefined : {
           background: `linear-gradient(135deg, hsl(${hue} 55% 92%), hsl(${(hue + 40) % 360} 55% 84%))`,
         }}>
      {showImg ? (
        <img src={src} alt={title || ""} loading="lazy"
             className="h-full w-full object-cover"
             onError={() => setFailed(true)} />
      ) : (
        <span aria-hidden="true" className="select-none text-3xl font-semibold"
              style={{ color: `hsl(${hue} 45% 32%)` }}>
          {initial || "🛍️"}
        </span>
      )}
    </div>
  );
}
