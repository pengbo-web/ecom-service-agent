import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { cn } from "@/lib/utils";

export function MessageBubble({ role, children }: { role: "user" | "assistant"; children: string }) {
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
          </div>
        )}
      </div>
    </div>
  );
}
