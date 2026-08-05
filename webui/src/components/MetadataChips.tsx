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

// Finding-3:哪个 level 算"不满"、哪个算"激烈",唯一权威来源是后端
// app/agent/understanding.py 的判定阈值(_EMOTION_UNHAPPY_MIN/_EMOTION_ANGRY_MIN,
// 与 query-understanding prompt 共用同一份常量,并在 _parse_emotion 里校验过
// emotion/emotion_level 两者一致)。前端不再自行按 emotion_level 数字比较——
// 那样两处阈值各改各的、注定迟早漂移(本 finding 要修的正是这个)。这里只把
// 后端已经判定并校验过一致性的 emotion 标签映射成中文展示文案,不重新判定。
const EMOTION_TAG: Record<string, string> = { unhappy: "不满", angry: "情绪激烈" };

export function MetadataChips({ meta }: { meta: Meta }) {
  // neutral(含字段缺省)不挂标——中性是绝大多数正常轮次,每条都挂会变噪音;
  // unhappy/angry 都是后端如实判定过的情绪,直接展示,不再额外按强度过滤。
  const emotionLabel = EMOTION_TAG[meta.emotion ?? "neutral"] ?? null;
  return (
    <div className="mt-1.5 flex flex-wrap gap-1.5">
      <Badge variant="primary">意图: {INTENT_CN[meta.intent] || meta.intent}</Badge>
      <Badge variant="default">置信度: {(meta.confidence * 100).toFixed(0)}%</Badge>
      <Badge variant={meta.requires_human ? "destructive" : "default"}>转人工: {meta.requires_human ? "是" : "否"}</Badge>
      {emotionLabel && (
        <Badge variant={meta.emotion === "angry" ? "destructive" : "default"}>{emotionLabel}</Badge>
      )}
    </div>
  );
}
