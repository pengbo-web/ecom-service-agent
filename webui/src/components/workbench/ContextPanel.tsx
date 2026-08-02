import { Card } from "@/components/ui/card";
import { avatarGradient, initials, relativeTime, statusMeta, TONE_CLASS } from "./parts";
import type { WbConversation } from "@/lib/api";

export function ContextPanel({ conv }: { conv: WbConversation | null }) {
  if (!conv) return (
    <div className="hidden h-full items-center justify-center border-l bg-card/40 p-6 text-center text-xs text-muted-foreground lg:flex">
      选择左侧会话查看客户信息
    </div>
  );
  const sm = statusMeta(conv);
  return (
    <div className="hidden h-full flex-col gap-4 border-l bg-card/40 p-4 lg:flex">
      <div className="flex flex-col items-center gap-2 pt-2">
        <span className="flex h-14 w-14 items-center justify-center rounded-full text-lg font-semibold text-white shadow-sm ring-2 ring-background"
          style={{ backgroundImage: avatarGradient(conv.user_id) }}>
          {initials(conv.user_id)}
        </span>
        <span className="text-sm font-semibold">客户 {conv.user_id}</span>
        <span className={`rounded px-2 py-0.5 text-[11px] ${TONE_CLASS[sm.tone]}`}>{sm.label}</span>
      </div>
      <Card className="flex flex-col gap-2 p-3 text-xs">
        <Row k="会话 ID" v={conv.conversation_id} mono />
        <Row k="状态" v={conv.status === "open" ? "进行中" : "已结束"} />
        <Row k="对话轮次" v={String(conv.turns)} />
        <Row k="创建于" v={relativeTime(conv.created_at)} />
        <Row k="接待模式" v={conv.manual ? "人工" : "AI"} />
      </Card>
      <p className="text-[11px] leading-relaxed text-muted-foreground">
        提示:AI 会先接待并可查订单/物流/退款/议价;需要人工时在中栏「转人工接管」后回复,客户端即时可见。
      </p>
    </div>
  );
}

function Row({ k, v, mono }: { k: string; v: string; mono?: boolean }) {
  return (
    <div className="flex items-center justify-between gap-2">
      <span className="text-muted-foreground">{k}</span>
      <span className={`truncate ${mono ? "font-mono text-[11px]" : ""}`} title={v}>{v}</span>
    </div>
  );
}
