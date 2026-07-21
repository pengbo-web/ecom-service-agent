import { useState } from "react";
import { Send } from "lucide-react";
import { Button } from "@/components/ui/button";

export function Composer({ disabled, onSend }: { disabled: boolean; onSend: (t: string, confirm: boolean) => void }) {
  const [text, setText] = useState("");
  const [confirm, setConfirm] = useState(false);
  function submit() {
    const t = text.trim();
    if (!t || disabled) return;
    onSend(t, confirm);
    setText("");
    setConfirm(false);   // 确认为一次性,用后复位
  }
  return (
    <div className="border-t bg-card p-3">
      <div className="flex items-end gap-2">
        <textarea
          className="flex-1 resize-none rounded-md border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-ring"
          rows={1} placeholder="试试：我的订单还没发货，怎么回事？"
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); } }}
        />
        <Button disabled={disabled} onClick={submit}><Send className="h-4 w-4" /> 发送</Button>
      </div>
      <label className="mt-2 flex items-center gap-1.5 text-xs text-muted-foreground">
        <input type="checkbox" checked={confirm} onChange={(e) => setConfirm(e.target.checked)} />
        确认执行敏感操作（退款 / 成交）——机器人请你确认后，勾选再发送
      </label>
    </div>
  );
}
