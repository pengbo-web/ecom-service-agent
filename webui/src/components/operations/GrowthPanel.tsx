import { useEffect, useState } from "react";
import { getGrowthDrafts, approveDraft, rejectDraft, getOpportunities, getOpportunityKinds,
  getOutreachStats, type OpportunityKind, type OutreachDraft, type OutreachStats } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { RotateCcw } from "lucide-react";

// 与 OperationsView 同款格式化:后端给的是 0~1 的小数分数,统一转成 1 位小数
// 的百分比字符串。两处各自定义而不是共享一份导出——GrowthPanel 是独立卡片,
// 没有必要为一个三行函数在两个组件之间建耦合。
function pct(x: number | undefined, digits = 1): string {
  return ((x ?? 0) * 100).toFixed(digits) + "%";
}

type SentWarning = { id: number; user_id: string; reason: string };

// 「来源理由」是整段店铺诊断原文,同一批扫描生成的多张草稿会一字不差地
// 重复这一整段——不能丢数据(理由本身是有效的每条草稿信息),但默认展开会
// 让店主对着 8 张一模一样的长段落反复下滑。所以按行折叠、可展开,阈值内的
// 短文本(未跨行/不长)直接原样显示,不额外加交互。
const REASON_PREVIEW_LEN = 40;

export function GrowthPanel() {
  const [drafts, setDrafts] = useState<OutreachDraft[] | null>(null);
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  // 每行独立 busy:用 Set 记录正在处理中的 draft id 集合,而不是单个标量。
  // 单一标量在同时批准两行时会互相覆盖——后完成的那次把标量清空,会连带
  // 把还没完成的那一行按钮重新点亮,等于放行了一次仍在途中的批准请求
  // (见任务约束:防连点必须按行隔离,不能相互覆盖)。
  const [busyIds, setBusyIds] = useState<Set<number>>(new Set());
  // 「来源理由」默认折叠、按 id 记哪些卡片被手动展开过——与 busyIds 同一套
  // Set 记录法,保证互不干扰(展开一张不影响其它卡片的折叠状态)。
  const [expandedReasons, setExpandedReasons] = useState<Set<number>>(new Set());
  // 批准/驳回失败的原因按 draft id 记:失败的草稿会退回列表继续显示,原因
  // 只属于这一条,绝不能冒泡成页面级错误条,把其它正常草稿也吓成"出错了"
  // (与 OperationsView 的 err/chatErr 分离同一道理)。
  const [rowErr, setRowErr] = useState<Record<number, string>>({});
  // 投递成功但账本(标记已发送)失败的情况:消息已经真实发到买家手上,
  // 草稿必须从待审队列摘掉(不可能再退回去重新批准一次),但绝不能让它
  // 看起来像一次干净的成功——后端把 reason 写成明确要求人工核查,这条
  // 警示必须常驻显示,不能随草稿一起从界面上消失,也不能跟普通成功撞脸。
  const [sentWarnings, setSentWarnings] = useState<SentWarning[]>([]);

  // 商机概览是独立的只读小节,拉取失败不该连累草稿列表的展示与操作。
  // 商机类型全集(kind + 中文标签)本身也来自后端,不在这里另存一份——
  // 后端新增一个 kind,这张卡片零改动就能多出一格。
  const [oppKinds, setOppKinds] = useState<OpportunityKind[] | null>(null);
  const [oppCounts, setOppCounts] = useState<Record<string, number | undefined> | null>(null);
  const [oppErr, setOppErr] = useState("");

  // 「触达效果」卡:发送时记基线、到期按订单状态推进判定的转化率(N3)。
  // 同样独立成自己的 busy/error,拉取失败不该连累草稿列表或商机概览。
  const [outreachStats, setOutreachStats] = useState<OutreachStats | null>(null);
  const [outreachErr, setOutreachErr] = useState("");

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
      const { kinds } = await getOpportunityKinds();
      const results = await Promise.all(kinds.map((k) => getOpportunities(k.kind)));
      const counts: Record<string, number | undefined> = {};
      results.forEach((r, i) => { counts[kinds[i].kind] = r.count; });
      setOppKinds(kinds);
      setOppCounts(counts);
      setOppErr("");
    } catch (e) {
      setOppErr(String(e));
    }
  }

  async function loadOutreachStats() {
    try {
      setOutreachStats(await getOutreachStats());
      setOutreachErr("");
    } catch (e) {
      setOutreachErr(String(e));
    }
  }

  useEffect(() => {
    load();
    loadOpportunities();
    loadOutreachStats();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function setRowBusy(id: number, isBusy: boolean) {
    setBusyIds((prev) => {
      const next = new Set(prev);
      if (isBusy) next.add(id); else next.delete(id);
      return next;
    });
  }

  function toggleReason(id: number) {
    setExpandedReasons((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  }

  // 批准会真的把消息发给一个真实买家,而且发出后无法撤回——点错一下就是
  // 一条骚扰/误导信息落到买家手机上。这是任务里唯一强制要求二次确认的动作。
  async function onApprove(d: OutreachDraft) {
    if (!window.confirm(`确认把这条消息发给买家 ${d.user_id}？发出后无法撤回。`)) return;
    setRowBusy(d.id, true);
    setRowErr((m) => { const n = { ...m }; delete n[d.id]; return n; });
    try {
      const r = await approveDraft(d.id);
      if (r.sent) {
        // 消息已经真实投递给买家,不管账本(success)记没记成功,这条草稿
        // 都不再处于"待审"——它不可能被撤回,也不该被重新批准一次。
        setDrafts((list) => (list || []).filter((x) => x.id !== d.id));
        if (!r.success) {
          // 投递成功但账本(标记已发送)失败:后端 reason 明确要求人工
          // 核查,这不是一次普通成功,必须留下一条显眼、不随草稿消失的警示。
          setSentWarnings((list) => [...list, { id: d.id, user_id: d.user_id, reason: r.reason }]);
        }
      } else {
        // 没有真正投递(不论 success 是 true 还是 false,例如"已被处理过"
        // 或"投递失败已退回待审"):草稿留在待审队列里,把原因亮出来,
        // 而不是乐观移除。
        setRowErr((m) => ({ ...m, [d.id]: r.reason }));
      }
    } catch (e) {
      setRowErr((m) => ({ ...m, [d.id]: String(e) }));
    } finally {
      setRowBusy(d.id, false);
    }
  }

  // 驳回不会给买家发送任何东西,后果也不是不可逆的——顶多是少发一条本该
  // 发的消息,店主可以再让增长 agent 重新生成一条草稿去补救。这与批准
  // "发出去就收不回"的性质完全不同,所以驳回不再要求二次确认,只有批准
  // 需要。
  async function onReject(d: OutreachDraft) {
    setRowBusy(d.id, true);
    setRowErr((m) => { const n = { ...m }; delete n[d.id]; return n; });
    try {
      await rejectDraft(d.id);
      setDrafts((list) => (list || []).filter((x) => x.id !== d.id));
    } catch (e) {
      setRowErr((m) => ({ ...m, [d.id]: String(e) }));
    } finally {
      setRowBusy(d.id, false);
    }
  }

  return (
    <div className="flex flex-col gap-6">
      {/* 触达效果:发出去的消息到底有没有让买家往前走(N3)。转化率没有测量
          窗口和"转化"定义就是一句空话——这两条口径必须钉在界面上,不能只靠
          店主自己脑补,否则这张卡片比不展示更容易误导人。 */}
      <section>
        <h3 className="mb-2 text-sm font-semibold">触达效果</h3>
        {outreachErr && (
          <div className="mb-2 text-xs text-destructive">触达效果读取失败：{outreachErr}</div>
        )}
        <Card className="p-3" data-testid="outreach-stats">
          <div className="grid grid-cols-3 gap-3">
            <div>
              <div className="text-xs text-muted-foreground">已发送</div>
              <div className="mt-1 text-lg font-semibold">{outreachStats?.sent ?? "—"}</div>
            </div>
            <div>
              <div className="text-xs text-muted-foreground">已转化</div>
              <div className="mt-1 text-lg font-semibold">{outreachStats?.converted ?? "—"}</div>
            </div>
            <div>
              <div className="text-xs text-muted-foreground">转化率</div>
              <div className="mt-1 text-lg font-semibold">{pct(outreachStats?.conversion_rate)}</div>
            </div>
          </div>
          <div className="mt-2 text-[11px] text-muted-foreground">
            统计窗口：近 {outreachStats?.window_days ?? "—"} 天发出的消息 ·
            归因窗口：发出满 {outreachStats?.window_hours ?? "—"} 小时后才判定 ·
            口径：只认目标订单状态<b>向前推进</b>
            （待支付→待发货→已发货→已签收），退款等其它变化不算转化
          </div>
        </Card>
      </section>

      {/* 商机概览:只读,帮店主判断值不值得主动挖一批新草稿,不落任何写操作 */}
      <section>
        <h3 className="mb-2 text-sm font-semibold">商机概览</h3>
        {oppErr && <div className="mb-2 text-xs text-destructive">商机数据读取失败：{oppErr}</div>}
        <div className="grid grid-cols-3 gap-3">
          {(oppKinds || []).map((k) => (
            <Card key={k.kind} className="p-3">
              {/* 标签完全来自后端;缺失时兜底显示原始 kind 而不是空白 */}
              <div className="text-xs text-muted-foreground">{k.label || k.kind}</div>
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

        {sentWarnings.length > 0 && (
          <div className="mb-3 flex flex-col gap-2">
            {sentWarnings.map((w) => (
              <div
                key={w.id}
                role="alert"
                className="rounded-md border-2 border-destructive bg-destructive/10
                          p-3 text-sm font-semibold text-destructive"
              >
                ⚠ 消息已发给买家 {w.user_id}，但{w.reason}
              </div>
            ))}
          </div>
        )}

        {!drafts && busy && <div className="text-sm text-muted-foreground">加载中…</div>}
        <div className="flex flex-col gap-3">
          {(drafts || []).map((d) => {
            // needs_review_reason 非空 = 草稿里含金钱承诺词(免运费/全额退款/返现/
            // 补发优惠券…),生成侧故意不改写或丢弃这类措辞,把"agent 原本想说
            // 什么"如实留下——所以这里必须是全链路最后一道、不可能被忽略的可见性。
            const flagged = !!d.needs_review_reason;
            const rowBusy = busyIds.has(d.id);
            return (
              <Card
                key={d.id}
                data-testid={`draft-${d.id}`}
                className={`p-4 text-sm ${flagged ? "border-2 border-destructive" : ""}`}
              >
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">买家 {d.user_id}</span>
                  {d.order_id && (
                    <span className="text-xs text-muted-foreground">关联订单 {d.order_id}</span>
                  )}
                  <span className="rounded bg-secondary px-1.5 py-0.5 text-[11px] text-muted-foreground">
                    {/* 中文标签由后端(growth_drafts 端点)同源给出;字段缺失
                        时兜底显示原始 kind,而不是自己另存一份映射表去猜 */}
                    {d.opportunity_label || d.opportunity_type}
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
                {(() => {
                  const isLong = d.reason.length > REASON_PREVIEW_LEN || d.reason.includes("\n");
                  const isExpanded = expandedReasons.has(d.id);
                  const showFull = isExpanded || !isLong;
                  const preview = d.reason.split("\n")[0].slice(0, REASON_PREVIEW_LEN);
                  return (
                    <div className="mt-1 text-xs text-muted-foreground">
                      来源理由：{showFull ? d.reason : `${preview}…`}
                      {isLong && (
                        <button
                          type="button"
                          className="ml-1 text-primary underline underline-offset-2"
                          onClick={() => toggleReason(d.id)}
                          data-testid={`reason-toggle-${d.id}`}
                        >
                          {isExpanded ? "收起" : "展开"}
                        </button>
                      )}
                    </div>
                  );
                })()}

                {rowErr[d.id] && (
                  <div className="mt-2 text-xs text-destructive">⚠️ {rowErr[d.id]}</div>
                )}

                <div className="mt-3 flex items-center gap-2">
                  <Button
                    size="sm"
                    disabled={rowBusy}
                    onClick={() => onApprove(d)}
                    data-testid={`approve-${d.id}`}
                  >
                    {rowBusy ? "处理中…" : "批准并发送"}
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={rowBusy}
                    onClick={() => onReject(d)}
                    data-testid={`reject-${d.id}`}
                  >
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
