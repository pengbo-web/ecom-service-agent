import { Badge } from "@/components/ui/badge";

const INTENT_CN: Record<string, string> = {
  order_query: "订单查询", return_request: "退换货", product_consult: "商品咨询",
  complaint: "投诉", after_sale: "售后", promotion: "优惠活动", account: "账户",
  greeting: "打招呼", other: "其他",
};
export type Meta = { intent: string; confidence: number; requires_human: boolean; follow_up_question?: string | null };

export function MetadataChips({ meta }: { meta: Meta }) {
  return (
    <div className="mt-1.5 flex flex-wrap gap-1.5">
      <Badge variant="primary">意图: {INTENT_CN[meta.intent] || meta.intent}</Badge>
      <Badge variant="default">置信度: {(meta.confidence * 100).toFixed(0)}%</Badge>
      <Badge variant={meta.requires_human ? "destructive" : "default"}>转人工: {meta.requires_human ? "是" : "否"}</Badge>
    </div>
  );
}
