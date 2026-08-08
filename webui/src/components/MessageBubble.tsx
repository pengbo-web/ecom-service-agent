import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { cn } from "@/lib/utils";

export function MessageBubble({ role, children, streaming }: {
  role: "user" | "assistant"; children: string;
  // E1(回复流式化):这条买家可见回复是否还在逐块接收中——渲染一个跟随
  // 内容末尾的打字光标,让"正在生成"这件事对买家可见(不是等 46 秒后一次性
  // 出字)。终帧(reply)到达后 ChatView 会把这个 prop 置回 false/undefined。
  streaming?: boolean;
}) {
  const isUser = role === "user";
  return (
    <div className={cn("flex", isUser ? "justify-end" : "justify-start")}>
      <div className={cn(
        "max-w-[78%] rounded-2xl px-4 py-2.5 text-sm leading-relaxed",
        isUser ? "bg-primary text-primary-foreground" : "border bg-card"
      )}>
        {isUser ? (
          <span className="whitespace-pre-wrap">{children}</span>
        ) : (
          <div className="prose prose-sm max-w-none prose-p:my-1">
            <ReactMarkdown remarkPlugins={[remarkGfm]}>{children}</ReactMarkdown>
            {streaming && (
              <span
                data-testid="reply-streaming-cursor"
                className="ml-0.5 inline-block h-3.5 w-1.5 animate-pulse bg-current align-middle"
                aria-hidden="true"
              />
            )}
          </div>
        )}
      </div>
    </div>
  );
}
