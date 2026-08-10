import { useEffect, useRef, useState } from "react";
import { RotateCcw, Upload, Trash2, FileText, AlertTriangle } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import {
  getKbDocuments, uploadKbDocument, deleteKbDocument,
  type KbDocument, type KbDocumentList,
} from "@/lib/api";

// 知识库文档管理:代替去 ApeRAG 自己的页面上传。检索侧不变(仍走 aperag_search
// 读同一个 collection),这里只把"写"这一半搬进来。
//
// 已在真服务上验证:API 写入的文档与 ApeRAG UI 上传的文档落在同一 collection、
// 走同一条索引流水线、被同一次检索并排召回——不存在"两套东西"。

// 索引状态用**上游原值**,不翻译成自己一套词(多一层就多一处会漂移的口径)。
// 终态是 ACTIVE 而不是 COMPLETE——项目里此前两处写成 COMPLETE,导致轮询永远
// 等不到终态、恒定报"索引未完成",而索引其实早就建好了。
const INDEX_STATUS: Record<string, { label: string; cls: string; note: string }> = {
  PENDING: { label: "排队中", cls: "bg-amber-500/10 text-amber-700 dark:text-amber-400",
             note: "已确认入库,等待建索引" },
  CREATING: { label: "建索引中", cls: "bg-sky-500/10 text-sky-700 dark:text-sky-400",
              note: "正在切分与向量化,完成后才会被召回" },
  ACTIVE: { label: "已生效", cls: "bg-emerald-500/10 text-emerald-700 dark:text-emerald-400",
            note: "索引已就绪,客服检索时可以召回这份文档" },
  DELETING: { label: "删除中", cls: "bg-slate-500/10 text-slate-600 dark:text-slate-400",
              note: "正在移除索引" },
  FAILED: { label: "建索引失败", cls: "bg-destructive/10 text-destructive",
            note: "这份文档**不会被召回**。多为格式无法解析,建议换成 md/txt 重传" },
};

function statusOf(s?: string | null) {
  if (!s) return { label: "—", cls: "bg-muted text-muted-foreground", note: "上游未给出状态" };
  return INDEX_STATUS[s] || { label: s, cls: "bg-muted text-muted-foreground", note: "" };
}

function sizeText(n?: number | null): string {
  if (n === null || n === undefined) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

//: 前端也列一份允许类型,只为**提前拦住明显不对的文件**并给出人话提示。
//: 真正的把关在后端(见 _KB_ALLOWED_EXT)——这个页面能改客服的政策依据,
//: 传错一份文档全店客服的口径当场就变了,不能只靠前端。
const ACCEPT = ".md,.markdown,.txt,.pdf,.docx";

export function KnowledgeView() {
  const [data, setData] = useState<KbDocumentList | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  // 上传独立一套 busy/err:上传失败不该让整页看起来像读取失败(与 SkillsView
  // 的 upErr/dsErr 同口径)。
  const [upBusy, setUpBusy] = useState(false);
  const [upErr, setUpErr] = useState("");
  const [upOk, setUpOk] = useState("");
  const [dragOver, setDragOver] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  // 删除按行记 busy/err,一行失败不影响其它行
  const [rowBusy, setRowBusy] = useState<Set<string>>(new Set());
  const [rowErr, setRowErr] = useState<Record<string, string>>({});

  // 索引是异步的(实测 PENDING → CREATING → ACTIVE 约 15 秒)。有文档还没到终态
  // 时自动轮询,到齐了就停——不做无条件定时刷新,那会在没有任何变化时一直打后端。
  const pollRef = useRef<number | null>(null);

  async function load() {
    setBusy(true);
    try {
      const d = await getKbDocuments();
      setData(d);
      setErr("");
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => { load(); }, []);

  useEffect(() => {
    const pending = (data?.documents || []).some(
      (d) => d.vector_index_status === "PENDING" || d.vector_index_status === "CREATING");
    if (pollRef.current) { window.clearTimeout(pollRef.current); pollRef.current = null; }
    if (pending) pollRef.current = window.setTimeout(load, 5000);
    return () => { if (pollRef.current) window.clearTimeout(pollRef.current); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data]);

  async function onPick(files: FileList | null) {
    const f = files?.[0];
    if (!f || upBusy) return;
    setUpBusy(true); setUpErr(""); setUpOk("");
    try {
      const r = await uploadKbDocument(f);
      if (r.success) {
        setUpOk(`${r.name || f.name} 已入库。${r.note || ""}`);
        await load();
      } else {
        // 上传成功但确认失败:文档停在 UPLOADED,永远不会被检索到,而且**不出现
        // 在列表里**(实测)。必须把这件事说清,否则店主以为传上去了。
        setUpErr(r.reason || "上传失败");
      }
    } catch (e) {
      setUpErr(String(e));
    } finally {
      setUpBusy(false);
      if (fileRef.current) fileRef.current.value = "";   // 允许重复选同一个文件
    }
  }

  async function onDelete(d: KbDocument) {
    // 删除是不可逆的,而且影响面比看起来大:删掉一份政策文档之后,客服对该类
    // 问题的回答就失去依据、退化成模型常识。所以这里要二次确认。
    if (!window.confirm(
      `确认从知识库删除《${d.name}》？\n\n删除后客服回答该类问题将失去政策依据（退化为模型常识）。此操作不可撤销。`
    )) return;
    setRowBusy((p) => new Set(p).add(d.id));
    setRowErr((m) => { const n = { ...m }; delete n[d.id]; return n; });
    try {
      await deleteKbDocument(d.id);
      await load();
    } catch (e) {
      setRowErr((m) => ({ ...m, [d.id]: String(e) }));
    } finally {
      setRowBusy((p) => { const n = new Set(p); n.delete(d.id); return n; });
    }
  }

  const docs = data?.documents || [];
  const failed = docs.filter((d) => d.vector_index_status === "FAILED");

  return (
    <div className="h-full overflow-y-auto p-6">
      <div className="mx-auto flex max-w-4xl flex-col gap-5">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h2 className="text-lg font-semibold">知识库</h2>
            <p className="mt-0.5 text-xs text-muted-foreground">
              客服回答政策类问题的依据。这里上传的文档与在 ApeRAG 页面上传<b>完全等效</b>——
              同一个 collection、同一条索引流水线、同一次检索召回。
            </p>
          </div>
          <Button variant="ghost" size="sm" onClick={load} disabled={busy}>
            <RotateCcw className="h-3.5 w-3.5" /> 刷新
          </Button>
        </div>

        {/* 写到哪里去:collection 与地址必须摆出来。同一套前端可能连不同环境的
            知识库,而"我刚把政策传到哪个库了"不该靠猜。 */}
        {data && (
          <div className="text-[11px] text-muted-foreground">
            目标：<code className="rounded bg-muted px-1">{data.base_url}</code>
            {" · collection "}
            <code className="rounded bg-muted px-1">{data.collection_id}</code>
          </div>
        )}

        {err && (
          <Card className="border-destructive p-3 text-sm text-destructive" data-testid="kb-error">
            读取失败：{err}
          </Card>
        )}

        {/* 上传区 */}
        <section>
          <div
            data-testid="kb-dropzone"
            onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
            onDragLeave={() => setDragOver(false)}
            onDrop={(e) => { e.preventDefault(); setDragOver(false); onPick(e.dataTransfer.files); }}
            className={`flex flex-col items-center gap-2 rounded-xl border-2 border-dashed p-6 text-center transition-colors ${
              dragOver ? "border-primary bg-primary/5" : "bg-card"}`}
          >
            <Upload className="h-6 w-6 text-muted-foreground" />
            <div className="text-sm">
              把文档拖到这里，或
              <button type="button" className="ml-1 text-primary underline underline-offset-2"
                      onClick={() => fileRef.current?.click()} disabled={upBusy}>
                选择文件
              </button>
            </div>
            <div className="text-[11px] text-muted-foreground">
              支持 md / markdown / txt / pdf / docx，单份上限 10 MB。
              上传后索引<b>异步创建</b>（约 15 秒），状态变为「已生效」后才会被召回。
            </div>
            <input ref={fileRef} type="file" accept={ACCEPT} className="hidden"
                   onChange={(e) => onPick(e.target.files)} />
            {upBusy && <div className="text-xs text-muted-foreground">上传中…</div>}
          </div>

          {upErr && (
            <div role="alert" className="mt-2 rounded-md border border-destructive bg-destructive/10
                                         p-2 text-xs text-destructive" data-testid="kb-upload-error">
              ⚠️ {upErr}
            </div>
          )}
          {upOk && (
            <div className="mt-2 rounded-md border border-emerald-500/40 bg-emerald-500/10
                            p-2 text-xs text-emerald-700 dark:text-emerald-400"
                 data-testid="kb-upload-ok">
              ✅ {upOk}
            </div>
          )}
        </section>

        {/* 建索引失败的文档要单独提出来:它们在列表里长得和别的一样,但**不会被
            召回**——店主会以为政策已经生效了。 */}
        {failed.length > 0 && (
          <Card className="border-destructive p-3" data-testid="kb-failed-banner">
            <div className="flex items-center gap-1.5 text-sm text-destructive">
              <AlertTriangle className="h-4 w-4" />
              {failed.length} 份文档建索引失败，<b>不会被客服召回</b>
            </div>
            <div className="mt-1 text-[11px] text-muted-foreground">
              多为格式无法解析。建议转成 md / txt 后重新上传，或删除后换一份。
            </div>
          </Card>
        )}

        {/* 文档列表 */}
        <section>
          <h3 className="mb-2 text-sm font-semibold">文档（{docs.length}）</h3>
          {!data && busy && <div className="text-sm text-muted-foreground">加载中…</div>}
          {data && docs.length === 0 && (
            <Card className="p-4 text-sm text-muted-foreground">
              知识库还是空的。上传政策文档（退换货、运费、发票、价保…）之后，
              客服回答这类问题才有依据，而不是靠模型常识。
            </Card>
          )}
          <div className="flex flex-col gap-2">
            {docs.map((d) => {
              const st = statusOf(d.vector_index_status);
              const rBusy = rowBusy.has(d.id);
              return (
                <Card key={d.id} className="p-3" data-testid={`kb-doc-${d.id}`}>
                  <div className="flex flex-wrap items-center gap-2">
                    <FileText className="h-4 w-4 shrink-0 text-muted-foreground" />
                    <span className="min-w-0 flex-1 truncate text-sm font-medium" title={d.name}>
                      {d.name}
                    </span>
                    <span className={`rounded px-1.5 py-0.5 text-[11px] ${st.cls}`} title={st.note}>
                      {st.label}
                    </span>
                    <Button variant="ghost" size="sm" disabled={rBusy}
                            onClick={() => onDelete(d)}>
                      <Trash2 className="h-3.5 w-3.5" /> {rBusy ? "删除中…" : "删除"}
                    </Button>
                  </div>
                  <div className="mt-1 flex flex-wrap gap-x-3 text-[11px] text-muted-foreground">
                    {sizeText(d.size) && <span>{sizeText(d.size)}</span>}
                    {d.created && <span>{d.created}</span>}
                    <span title="ApeRAG 的原始状态值，未做翻译">
                      vector_index_status={d.vector_index_status || "—"}
                    </span>
                  </div>
                  {st.note && d.vector_index_status !== "ACTIVE" && (
                    <div className={`mt-1 text-[11px] ${
                      d.vector_index_status === "FAILED" ? "text-destructive" : "text-muted-foreground"}`}>
                      {st.note}
                    </div>
                  )}
                  {rowErr[d.id] && (
                    <div className="mt-1 text-[11px] text-destructive">⚠️ {rowErr[d.id]}</div>
                  )}
                </Card>
              );
            })}
          </div>
        </section>
      </div>
    </div>
  );
}
