import { useEffect, useState } from "react";
import { getGrowthDrafts, approveDraft, rejectDraft, getOpportunities,
  type OutreachDraft } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { RotateCcw } from "lucide-react";

const OPPORTUNITY_KINDS: { kind: string; label: string }[] = [
  { kind: "stale_pending_order", label: "下单后久未推进" },
  { kind: "stalled_bargain", label: "议价未成交" },
  { kind: "consulted_no_order", label: "咨询过但没下单" },
];

export function GrowthPanel() {
  const [drafts, setDrafts] = useState<OutreachDraft[] | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  // 每行独立 busy:一条草稿在批准中,不能连带把整份列表都锁死——店主要能
  // 同时继续处理别的草稿(见任务约束:防连点只锁当前这一行)。
  const [busyId, setBusyId] = useState<number | null>(null);
  // 批准/驳回失败的原因按 draft id 记:失败的草稿会退回列表继续显示,原因
  // 只属于这一条,绝不能冒泡成页面级错误条,把其它正常草稿也吓成"出错了"
  // (与 OperationsView 的 err/chatErr 分离同一道理)。
  const [rowErr, setRowErr] = useState<Record<number, string>>({});

  // 商机概览是独立的只读小节,拉取失败不该连累草稿列表的展示与操作。
  const [oppCounts, setOppCounts] = useState<Record<string, number | undefined> | null>(null);
  const [oppErr, setOppErr] = useState("");

  async function load() {
    setBusy(true);
    try {
      const r = await getGrowthDrafts("draft");
      setDrafts(r.drafts);
      setErr("");
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function loadOpportunities() {
    try {
      const results = await Promise.all(OPPORTUNITY_KINDS.map((k) => getOpportunities(k.kind)));
      const counts: Record<string, number | undefined> = {};
      results.forEach((r, i) => { counts[OPPORTUNITY_KINDS[i].kind] = r.count; });
      setOppCounts(counts);
      setOppErr("");
    } catch (e) {
      setOppErr(String(e));
    }
  }

  useEffect(() => {
    load();
    loadOpportunities();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 批准会真的把消息发给一个真实买家,而且发出后无法撤回——点错一下就是
  // 一条骚扰/误导信息落到买家手机上。这是任务里唯一强制要求二次确认的动作。
  async function onApprove(d: OutreachDraft) {
    if (!window.confirm(`确认把这条消息发给买家 ${d.user_id}？发出后无法撤回。`)) return;
    setBusyId(d.id);
    setRowErr((m) => { const n = { ...m }; delete n[d.id]; return n; });
    try {
      const r = await approveDraft(d.id);
      if (r.sent) {
        // 只有真正投递成功才从待审列表摘掉;后端对失败的草稿会原样退回
        // draft 状态,前端必须保留它、把原因亮出来,而不是乐观移除。
        setDrafts((list) => (list || []).filter((x) => x.id !== d.id));
      } else {
        setRowErr((m) => ({ ...m, [d.id]: r.reason }));
      }
    } catch (e) {
      setRowErr((m) => ({ ...m, [d.id]: String(e) }));
    } finally {
      setBusyId(null);
    }
  }

  async function onReject(d: OutreachDraft) {
    if (!window.confirm("确认驳回这条草稿？驳回后不会发送给买家。")) return;
    setBusyId(d.id);
    setRowErr((m) => { const n = { ...m }; delete n[d.id]; return n; });
    try {
      await rejectDraft(d.id);
      setDrafts((list) => (list || []).filter((x) => x.id !== d.id));
    } catch (e) {
      setRowErr((m) => ({ ...m, [d.id]: String(e) }));
    } finally {
      setBusyId(null);
    }
  }

  return (
    <div className="flex flex-col gap-6">
      {/* 商机概览:只读,帮店主判断值不值得主动挖一批新草稿,不落任何写操作 */}
      <section>
        <h3 className="mb-2 text-sm font-semibold">商机概览</h3>
        {oppErr && <div className="mb-2 text-xs text-destructive">商机数据读取失败：{oppErr}</div>}
        <div className="grid grid-cols-3 gap-3">
          {OPPORTUNITY_KINDS.map((k) => (
            <Card key={k.kind} className="p-3">
              <div className="text-xs text-muted-foreground">{k.label}</div>
              <div className="mt-1 text-lg font-semibold">
                {oppCounts ? (oppCounts[k.kind] ?? "—") : "…"}
              </div>
            </Card>
          ))}
        </div>
      </section>

      <section>
        <div className="mb-2 flex items-center justify-between">
          <h3 className="text-sm font-semibold">待审草稿（{drafts?.length ?? 0}）</h3>
          <Button variant="ghost" size="sm" onClick={load} disabled={busy}>
            <RotateCcw className="h-3.5 w-3.5" /> 刷新
          </Button>
        </div>
        {err && <div className="mb-2 text-sm text-destructive">读取失败：{err}</div>}
        {!drafts && busy && <div className="text-sm text-muted-foreground">加载中…</div>}
        <div className="flex flex-col gap-3">
          {(drafts || []).map((d) => {
            // needs_review_reason 非空 = 草稿里含金钱承诺词(免运费/全额退款/返现/
            // 补发优惠券…),生成侧故意不改写或丢弃这类措辞,把"agent 原本想说
            // 什么"如实留下——所以这里必须是全链路最后一道、不可能被忽略的可见性。
            const flagged = !!d.needs_review_reason;
            const rowBusy = busyId === d.id;
            return (
              <Card
                key={d.id}
                className={`p-4 text-sm ${flagged ? "border-2 border-destructive" : ""}`}
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">买家 {d.user_id}</span>
                  {d.order_id && (
                    <span className="text-xs text-muted-foreground">关联订单 {d.order_id}</span>
                  )}
                  <span className="rounded bg-secondary px-1.5 py-0.5 text-[11px] text-muted-foreground">
                    {d.opportunity_type}
                  </span>
                </div>

                {flagged && (
                  <div className="mt-2 rounded-md border border-destructive bg-destructive/10
                                  p-2 text-xs font-semibold text-destructive">
                    ⚠ {d.needs_review_reason}
                  </div>
                )}

                <div className="mt-2 whitespace-pre-wrap rounded-md bg-secondary/40 p-2 text-sm">
                  {d.content}
                </div>
                <div className="mt-1 text-xs text-muted-foreground">来源理由：{d.reason}</div>

                {rowErr[d.id] && (
                  <div className="mt-2 text-xs text-destructive">⚠️ {rowErr[d.id]}</div>
                )}

                <div className="mt-3 flex items-center gap-2">
                  <Button size="sm" disabled={rowBusy} onClick={() => onApprove(d)}>
                    {rowBusy ? "处理中…" : "批准并发送"}
                  </Button>
                  <Button size="sm" variant="outline" disabled={rowBusy} onClick={() => onReject(d)}>
                    驳回
                  </Button>
                </div>
              </Card>
            );
          })}
          {drafts && drafts.length === 0 && (
            <div className="text-sm text-muted-foreground">暂无待审草稿</div>
          )}
        </div>
      </section>
    </div>
  );
}
