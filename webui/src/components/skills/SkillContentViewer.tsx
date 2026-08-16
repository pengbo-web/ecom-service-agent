import { useState } from "react";
import { getSkillContent, type SkillContent } from "@/lib/api";
import { ChevronDown, ChevronRight } from "lucide-react";

/** 点开看这份技能到底写了什么。
 *
 * **为什么必须有。** 这个页面此前只显示 skill 的名字与 description —— 而
 * description 只是 frontmatter 里的一句话,真正决定客服说什么的是正文。
 * 待审候选那一栏尤其荒唐:操作者要在**没看过内容**的前提下点「转正上线」,
 * 而那个按钮会立刻把这份正文推给线上会话。风险档、校验结论、门禁用例数全都
 * 齐了,唯独缺了"它到底写了什么"。
 *
 * 三条取舍:
 *
 * 1. **按需拉取,不随总览一起下发。** 技能正文可以有几千字,7 个现行 + 6 个候选
 *    全塞进 `/api/admin/skills` 会让这个页面每次刷新都拖一大坨没人看的文本。
 * 2. **原样显示,不渲染 Markdown。** 这份文本是**喂给模型的指令**,不是给人看的
 *    文章。渲染成 Markdown 会把 `## 步骤` 变成大标题、把 `\n\n` 折叠掉——
 *    而模型看到的恰恰是那些原始字符。审的必须是模型看到的那一份。
 * 3. **披露字段照实显示。** 正文被截断、有文件读不出、有符号链接逃出技能目录,
 *    这些情况下界面上少了一段内容,与"正文本来就这么长"看起来完全一样。
 */
export function SkillContentViewer(
  { name, variant }: { name: string; variant: "live" | "candidate" }
) {
  const [open, setOpen] = useState(false);
  const [data, setData] = useState<SkillContent | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  async function toggle() {
    if (open) { setOpen(false); return; }
    setOpen(true);
    if (data || busy) return;      // 已经拉过就不再请求(正文不会自己变)
    setBusy(true);
    setErr("");
    try {
      setData(await getSkillContent(name, variant));
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const key = `${variant}-${name}`;
  return (
    <div className="mt-2">
      <button
        type="button"
        onClick={toggle}
        aria-expanded={open}
        className="flex items-center gap-1 text-xs text-muted-foreground
                   hover:text-foreground"
        data-testid={`view-skill-${key}`}
      >
        {open ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
        {open ? "收起正文" : "查看正文"}
      </button>

      {open && (
        <div className="mt-1" data-testid={`skill-body-${key}`}>
          {busy && <div className="text-xs text-muted-foreground">读取中…</div>}
          {err && (
            <div role="alert" className="text-xs text-destructive"
                 data-testid={`skill-body-err-${key}`}>⚠️ {err}</div>
          )}
          {data && (
            <>
              <div className="mb-1 flex flex-wrap gap-x-3 text-[11px] text-muted-foreground">
                <span>v{data.version}</span>
                <span>指纹 {data.fingerprint.slice(0, 8)}</span>
                <span className="font-mono">{data.path}</span>
              </div>

              <Disclosures data={data} testKey={key} />

              {/* 定高 + 自己滚动:一份长正文不该把整页顶开,让下面的候选都看不见 */}
              <pre className="max-h-96 overflow-auto rounded-md border bg-muted/40 p-2
                              text-[11px] leading-relaxed whitespace-pre-wrap break-words">
                {data.content}
              </pre>

              {data.files.length > 0 && (
                <div className="mt-2">
                  <div className="text-[11px] text-muted-foreground">
                    附带资料 {data.files.length} 份（会随转正一起上线，并被
                    <code className="mx-1">read_skill_file</code>灌进模型上下文）：
                    {data.files.join("、")}
                  </div>
                  <pre className="mt-1 max-h-64 overflow-auto rounded-md border
                                  bg-muted/40 p-2 text-[11px] leading-relaxed
                                  whitespace-pre-wrap break-words">
                    {data.attachment_text}
                  </pre>
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}

/** 正文没读全 / 有东西在审核面之外 —— 这些必须显示。
 *
 * 少显示了一段正文,与"正文本来就那么长",在界面上看起来完全一样。而这里正是
 * 操作者据以决定要不要放行的地方。
 */
function Disclosures({ data, testKey }: { data: SkillContent; testKey: string }) {
  const notes: string[] = [];
  if (data.truncated) notes.push("有文件超长被截断，下面显示的不是全部内容");
  if (data.over_cap) notes.push("总量触顶，仍有内容没有显示出来");
  if (data.unreadable.length) notes.push(`读不出（非 UTF-8 或损坏）：${data.unreadable.join("、")}`);
  if (data.escaped.length) {
    notes.push(`有路径经符号链接逃出技能目录：${data.escaped.join("、")}`
      + "——模型读不到它，但转正时 copytree 会解引用把内容复制上线");
  }
  if (!notes.length) return null;
  return (
    <div className="mb-1 rounded border border-amber-500/40 bg-amber-500/10 p-1.5
                    text-[11px] text-amber-700 dark:text-amber-400"
         data-testid={`skill-body-disclosure-${testKey}`}>
      {notes.map((n) => <div key={n}>⚠️ {n}</div>)}
    </div>
  );
}
