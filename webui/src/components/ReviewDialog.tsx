import { useState } from "react";
import { submitReview, type ReviewableItem } from "@/lib/api";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Star } from "lucide-react";

// 买家评价弹窗:1-5 星 + 文本框。只有已签收订单项才会被传进来(由 OrdersView
// 从 /api/reviewable 的结果里挑,不在这里再判断"能不能评"——服务端才是那条
// 硬约束的唯一事实来源,前端只是按服务端已经算好的可评列表展示入口)。
//
// 重复评价:后端 create_review 命中 UNIQUE 约束会返回 success:false + 明确的
// 中文 reason(而不是 500),这里原样展示 reason,不自己改写措辞——万一以后
// 后端补充了更精确的原因文案,这里不需要跟着改。
export function ReviewDialog({
  item, open, onOpenChange, onSubmitted,
}: {
  item: ReviewableItem | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSubmitted: (item: ReviewableItem) => void;
}) {
  const [rating, setRating] = useState(0);
  const [content, setContent] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  function reset() {
    setRating(0);
    setContent("");
    setErr("");
  }

  function handleOpenChange(next: boolean) {
    if (!next) reset();
    onOpenChange(next);
  }

  async function onSubmit() {
    if (!item || rating < 1 || busy) return;
    setBusy(true);
    setErr("");
    try {
      const r = await submitReview(item.order_id, item.sku, rating, content.trim());
      if (r.success) {
        onSubmitted(item);
        handleOpenChange(false);
      } else {
        // 重复评价等场景:后端给的中文原因原样显示,不能吞掉也不能改写。
        setErr(r.reason || "提交失败,请稍后再试。");
      }
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogContent>
        <DialogTitle>评价{item ? `「${item.name}」` : ""}</DialogTitle>
        <div className="mt-3 flex flex-col gap-3">
          <div className="flex items-center gap-1">
            {[1, 2, 3, 4, 5].map((n) => (
              <button
                key={n}
                type="button"
                data-testid={`star-${n}`}
                aria-label={`${n} 星`}
                aria-pressed={rating >= n}
                onClick={() => setRating(n)}
                className="p-0.5"
              >
                <Star
                  className={`h-6 w-6 ${
                    rating >= n ? "fill-amber-400 text-amber-400" : "text-muted-foreground"
                  }`}
                />
              </button>
            ))}
          </div>
          <textarea
            className="min-h-20 w-full rounded-md border bg-background p-2 text-sm"
            placeholder="说说这次购物体验吧(选填)"
            value={content}
            disabled={busy}
            onChange={(e) => setContent(e.target.value)}
          />
          {err && <div role="alert" className="text-xs text-destructive">⚠️ {err}</div>}
          <div className="flex justify-end gap-2">
            <Button variant="outline" size="sm" onClick={() => handleOpenChange(false)} disabled={busy}>
              取消
            </Button>
            <Button size="sm" onClick={onSubmit} disabled={busy || rating < 1}>
              {busy ? "提交中…" : "提交评价"}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
