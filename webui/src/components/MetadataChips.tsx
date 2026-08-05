import { Badge } from "@/components/ui/badge";

const INTENT_CN: Record<string, string> = {
  order_query: "订单查询", return_request: "退换货", product_consult: "商品咨询",
  complaint: "投诉", after_sale: "售后", promotion: "优惠活动", account: "账户",
  greeting: "打招呼", other: "其他", human_request: "转人工请求", fast_path: "快速应答",
};
export type Meta = {
  intent: string; confidence: number; requires_human: boolean; follow_up_question?: string | null;
  // N2:情绪信号(SSE metadata 帧新增字段,老会话/未注入 QU 时缺省 neutral/0)
  emotion?: string; emotion_level?: number;
};

export function MetadataChips({ meta }: { meta: Meta }) {
  const level = meta.emotion_level ?? 0;
  // 中性(level < 2)不挂标——每条回复都挂会让标签变噪音,只在真的有情绪时才现身。
  const emotionLabel = level >= 3 ? "情绪激烈" : level >= 2 ? "不满" : null;
  return (
    <div className="mt-1.5 flex flex-wrap gap-1.5">
      <Badge variant="primary">意图: {INTENT_CN[meta.intent] || meta.intent}</Badge>
      <Badge variant="default">置信度: {(meta.confidence * 100).toFixed(0)}%</Badge>
      <Badge variant={meta.requires_human ? "destructive" : "default"}>转人工: {meta.requires_human ? "是" : "否"}</Badge>
      {emotionLabel && (
        <Badge variant={level >= 3 ? "destructive" : "default"}>{emotionLabel}</Badge>
      )}
    </div>
  );
}
