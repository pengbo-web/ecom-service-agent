import { useEffect, useState } from "react";
import { getGrowthDrafts, approveDraft, rejectDraft, getOpportunities, getOpportunityKinds,
  getOutreachStats, getFollowups, type OpportunityKind, type OutreachDraft, type OutreachStats,
  type OutreachFollowup, type GrowthOpportunity,
  getCollabHealth, retryFailedEvent, type CollabHealth } from "@/lib/api";
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
  // 跨类型合并后按 priority_score 取前几条:光有"每类几个"的计数,店主还是不知道
  // 该先跟谁。排序与理由都由后端算好(确定性打分,见 app/agent/tools/priority.py),
  // 前端只负责展示——不在这里复算分数,否则又是一份会漂移的副本。
  const [oppTop, setOppTop] = useState<GrowthOpportunity[] | null>(null);

  // 「触达效果」卡:发送时记基线、到期按订单状态推进判定的转化率(N3)。
  // 同样独立成自己的 busy/error,拉取失败不该连累草稿列表或商机概览。
  const [outreachStats, setOutreachStats] = useState<OutreachStats | null>(null);
  const [outreachErr, setOutreachErr] = useState("");

  // 「跟进链」卡(N7:持续沟通=序列自动推进,不是自动发送)。只读展示,
  // 同样独立成自己的 busy/error——它读取失败不该连累草稿列表/商机概览/
  // 触达效果这三张已有卡片,反过来也一样。
  const [followups, setFollowups] = useState<OutreachFollowup[] | null>(null);
  const [followupsErr, setFollowupsErr] = useState("");
  const [followupsBusy, setFollowupsBusy] = useState(false);

  // 「协作健康」卡:失败事件 + worker 心跳。这张卡的存在本身就是要点——系统
  // 刻意不自动重试失败事件,那它就必须在界面上有一个位置,否则"留给人工决定"
  // 等于留给没人。同样独立 busy/error,不连累其它卡片。
  const [health, setHealth] = useState<CollabHealth | null>(null);
  const [healthErr, setHealthErr] = useState("");
  const [healthBusy, setHealthBusy] = useState(false);

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
      // 每一类内部后端已按优先级排好;跨类型比较要重新排一次(分数口径统一,
      // 都是同一个打分函数算的,可直接比)。没有 priority_score 的行(打分被
      // 关掉或降级)按 0 处理排在最后,不会因此消失。
      const merged = results
        .flatMap((r) => r.opportunities || [])
        .sort((a, b) => Number(b.priority_score ?? 0) - Number(a.priority_score ?? 0))
        .slice(0, 5);
      setOppKinds(kinds);
      setOppCounts(counts);
      setOppTop(merged);
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

  async function loadFollowups() {
    setFollowupsBusy(true);
    try {
      const r = await getFollowups();
      setFollowups(r.followups);
      setFollowupsErr("");
    } catch (e) {
      setFollowupsErr(String(e));
    } finally {
      setFollowupsBusy(false);
    }
  }

  async function loadHealth() {
    setHealthBusy(true);
    try {
      const h = await getCollabHealth();
      // 形状校验不是洁癖:这张卡片渲染时会读 h.worker.* 与 h.failed.map,
      // 任何缺字段的响应(旧版服务端、代理返回的 200 错误页、灰度期新旧端点
      // 并存)都会在渲染期抛 TypeError,**把整个增长面板带崩**——而那个面板
      // 上就是人工审批闸。一个健康卡片绝不该有能力搞垮它所监控的页面。
      // 校验不过就当读取失败处理:少一张卡,而不是少一整页。
      if (!h || typeof h !== "object" || !h.worker || !Array.isArray(h.failed)) {
        setHealth(null);
        setHealthErr("响应格式不正确(服务端版本可能不匹配)");
        return;
      }
      setHealth(h);
      setHealthErr("");
    } catch (e) {
      setHealth(null);
      setHealthErr(String(e));
    } finally {
      setHealthBusy(false);
    }
  }

  async function onRetry(eventId: number) {
    try {
      await retryFailedEvent(eventId);
      await loadHealth();          // 重试后立刻刷新,让计数当场变化
    } catch (e) {
      setHealthErr(String(e));
    }
  }

  useEffect(() => {
    load();
    loadOpportunities();
    loadOutreachStats();
    loadFollowups();
    loadHealth();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 跟进链状态 → 中文;done 没有 stop_reason(达到步数上限是正常走完,不是
  // 被什么原因拦下),终止原因只在 stopped 时才有意义。
  const FOLLOWUP_STATUS_LABELS: Record<string, string> = {
    active: "进行中", done: "已完成", stopped: "已终止",
  };

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
  //
  // N6:草稿带 offer.coupon_code 时,批准还会真的发放一张优惠券,同样碰钱、
  // 同样不可撤销——必须把"会发券"这件事摆在人点下去**之前**,而不是等发完
  // 才在结果里告知,否则这道人工闸就形同虚设。
  async function onApprove(d: OutreachDraft) {
    const couponCode = (d.offer?.coupon_code as string | undefined) || "";
    const confirmText = couponCode
      ? `确认把这条消息发给买家 ${d.user_id}？并发放优惠券 ${couponCode}，发出后无法撤回。`
      : `确认把这条消息发给买家 ${d.user_id}？发出后无法撤回。`;
    if (!window.confirm(confirmText)) return;
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
      {/* 协作健康:放在最前面是刻意的——失败事件与 worker 停摆都是"系统在悄悄
          少干活"的信号,埋在页面底部等于没有。健康时只占一行浅色提示,不抢
          注意力;异常时才变红并展开列表。 */}
      <section data-testid="collab-health">
        <div className="mb-2 flex items-center justify-between">
          <h3 className="text-sm font-semibold">协作健康</h3>
          <Button variant="ghost" size="sm" onClick={loadHealth} disabled={healthBusy}>
            <RotateCcw className="h-3.5 w-3.5" /> 刷新
          </Button>
        </div>
        {healthErr && <div className="mb-2 text-sm text-destructive">读取失败：{healthErr}</div>}
        {health && (
          <div className="flex flex-col gap-2">
            <Card className={`p-3 text-sm ${health.worker.healthy ? "" : "border-destructive"}`}>
              {health.worker.healthy ? (
                <span className="text-muted-foreground">
                  协作 worker 正常，{health.worker.stale_seconds ?? 0} 秒前跑完一轮
                </span>
              ) : health.worker.last_success_at ? (
                // 跑过但停了:部署了 worker 但它挂了/卡住了
                <span className="text-destructive">
                  协作 worker 已停摆：上次跑完是 {health.worker.last_success_at}
                  （超过 {health.worker.threshold_seconds} 秒判为异常）。
                  期间买家链路不受影响，但异常扫描、归因、起草、跟进都不会发生。
                </span>
              ) : (
                // 从未跑过:多半是压根没起 worker,与"挂了"是两回事,提示要分开
                <span className="text-destructive">
                  协作 worker 从未运行过。请启动：python -m app.scripts.agent_collab --loop
                </span>
              )}
              {health.worker.last_error && (
                <div className="mt-1 text-xs text-muted-foreground">
                  最近一次报错（{health.worker.last_error_at}）：{health.worker.last_error}
                </div>
              )}
            </Card>

            {health.failed_count > 0 && (
              <Card className="border-destructive p-3">
                <div className="mb-2 text-sm text-destructive">
                  {health.failed_count} 条协作事件处理失败，已停在队列里等人决定
                  （系统不会自动重试，避免坏事件无限循环）
                </div>
                <div className="flex flex-col gap-1">
                  {health.failed.map((e) => (
                    <div key={e.id}
                         className="flex flex-wrap items-center gap-2 text-xs"
                         data-testid={`failed-event-${e.id}`}>
                      <span className="rounded bg-muted px-1.5 py-0.5">{e.event_type}</span>
                      <span className="text-muted-foreground">→ {e.target_agent}</span>
                      <span className="text-muted-foreground">链 {e.correlation_id}</span>
                      <span className="text-muted-foreground">{e.created_at}</span>
                      <Button variant="ghost" size="sm" className="ml-auto"
                              onClick={() => onRetry(e.id)}>
                        放回队列重试
                      </Button>
                    </div>
                  ))}
                </div>
              </Card>
            )}
          </div>
        )}
      </section>

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

        {/* 「先跟谁」:计数回答不了这个问题,排序才能。分数与理由都来自后端的
            确定性打分(滞留时长/订单金额/该类历史转化率),所以这里把 reason
            原样显示出来——店主质疑排序时能当场对着三项事实核对,而不是只看到
            一个无从追问的数字。 */}
        {oppTop && oppTop.length > 0 && (
          <div className="mt-3" data-testid="opp-top">
            <div className="mb-1 text-xs text-muted-foreground">最该先跟的（按优先级）</div>
            <div className="flex flex-col gap-1">
              {oppTop.map((o, i) => (
                <Card key={`${o.kind}-${o.user_id}-${o.order_id ?? i}`}
                      className="flex flex-wrap items-center gap-2 p-2 text-xs">
                  <span className="text-muted-foreground">{i + 1}.</span>
                  <span className="font-medium">买家 {String(o.user_id ?? "—")}</span>
                  <span className="rounded bg-muted px-1.5 py-0.5">
                    {String(o.situation_label ?? o.kind ?? "")}
                  </span>
                  {o.order_id ? (
                    <span className="text-muted-foreground">{String(o.order_id)}</span>
                  ) : null}
                  <span className="ml-auto text-muted-foreground">
                    {String(o.priority_reason ?? "")}
                  </span>
                </Card>
              ))}
            </div>
          </div>
        )}
      </section>

      {/* 跟进链(N7):"持续沟通"指序列到期自动推进,不是自动发送——每一步
          仍产一条待审草稿。终止的链必须带着中文原因常驻显示,店主要看懂
          "为什么不再跟了",而不是这条链悄悄从列表里消失。 */}
      <section>
        <div className="mb-2 flex items-center justify-between">
          <h3 className="text-sm font-semibold">跟进链（{followups?.length ?? 0}）</h3>
          <Button variant="ghost" size="sm" onClick={loadFollowups} disabled={followupsBusy}>
            <RotateCcw className="h-3.5 w-3.5" /> 刷新
          </Button>
        </div>
        {followupsErr && (
          <div className="mb-2 text-sm text-destructive">跟进链读取失败：{followupsErr}</div>
        )}
        {!followups && followupsBusy && <div className="text-sm text-muted-foreground">加载中…</div>}
        <div className="flex flex-col gap-2" data-testid="followups-list">
          {(followups || []).map((f) => (
            <Card key={f.id} className="p-3 text-sm" data-testid={`followup-${f.id}`}>
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium">买家 {f.user_id}</span>
                <span className="rounded bg-secondary px-1.5 py-0.5 text-[11px] text-muted-foreground">
                  {f.kind_label || f.kind}
                </span>
                <span className="text-xs text-muted-foreground">
                  第 {f.step} / 共 {f.max_steps} 步
                </span>
                <span
                  className={`rounded px-1.5 py-0.5 text-[11px] ${
                    f.status === "stopped"
                      ? "bg-destructive/10 text-destructive"
                      : f.status === "done"
                      ? "bg-secondary text-muted-foreground"
                      : "bg-primary/10 text-primary"
                  }`}
                >
                  {FOLLOWUP_STATUS_LABELS[f.status] || f.status}
                </span>
              </div>
              {f.status === "active" && (
                <div className="mt-1 text-xs text-muted-foreground">
                  下次触达时间：{f.next_touch_at}
                </div>
              )}
              {f.status === "stopped" && (
                <div className="mt-1 text-xs text-destructive">
                  终止原因：{f.stop_reason_label || f.stop_reason || "未知"}
                </div>
              )}
            </Card>
          ))}
          {followups && followups.length === 0 && (
            <div className="text-sm text-muted-foreground">暂无跟进链</div>
          )}
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
            // N6:offer.coupon_code 非空 = 批准这条草稿会真的发放一张优惠券。
            // 文案(discount)由后端(growth_drafts 端点)同源给出,不在前端
            // 另存一份券码/文案表——券码不在后端已知券里(模型编的)时
            // coupon_discount 是空字符串,提示这不是一张真实的券。
            const couponCode = (d.offer?.coupon_code as string | undefined) || "";
            const couponUnknown = !!couponCode && !d.coupon_discount;
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

                {couponCode && (
                  <div
                    data-testid={`coupon-${d.id}`}
                    className="mt-2 rounded-md border border-primary bg-primary/10
                              p-2 text-xs font-semibold text-primary"
                  >
                    🎁 附带优惠券 {couponCode}
                    {d.coupon_discount ? ` · ${d.coupon_discount}` : ""}
                    {couponUnknown && "（非本店已知券码,批准后可能发放失败）"}
                  </div>
                )}

                {flagged && (
                  <div className="mt-2 rounded-md border border-destructive bg-destructive/10
                                  p-2 text-xs font-semibold text-destructive">
                    ⚠ {d.needs_review_reason}
                  </div>
                )}

                {d.deliverable === false && (
                  // 与上面那张券的提示同一条原则:把"批准之后会发生什么"在按钮
                  // 按下**之前**摆出来。这一条批下去必定失败,而且重试永远失败
                  // ——实测走查时就是批完才发现,营销那次 LLM 起草和一次人工审批
                  // 都白花了。不隐藏批准按钮:店主可能有别的处置(比如先想办法
                  // 让买家来咨询),该由人决定,界面只负责说清楚。
                  <div data-testid={`undeliverable-${d.id}`}
                       className="mt-2 rounded-md border border-amber-500/50 bg-amber-500/10
                                  p-2 text-xs font-semibold text-amber-700 dark:text-amber-400">
                    ⚠ {d.undeliverable_reason || "该买家当前无法投递"}
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
