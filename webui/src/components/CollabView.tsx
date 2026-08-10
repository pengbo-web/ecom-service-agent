import { useEffect, useState } from "react";
import { RotateCcw, AlertTriangle, ArrowRight, Radio, Workflow, Inbox } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import {
  getCollabChains, getCollabTimeline, getCollabRouting, getCollabHealth,
  retryFailedEvent, getCollabInbox, ackCollabEvent,
  type CollabChain, type CollabTimeline, type CollabRouting, type CollabHealth,
  type CollabEvent, type CollabInbox,
} from "@/lib/api";

// 事件四态。**skipped 必须与 failed 分开**:它是消费闸拦下的**正常**结果
// (营销静默期就走这条),不是错误。把"刻意没做"渲染成红色会让运营去修一个
// 根本不存在的故障,而真正需要人处理的 failed 反而被淹没在同色噪声里。
const STATUS: Record<string, { label: string; cls: string; note: string }> = {
  pending: {
    label: "待认领", cls: "bg-amber-500/10 text-amber-700 dark:text-amber-400",
    note: "已入队,等 worker 下一轮认领",
  },
  processing: {
    label: "处理中", cls: "bg-sky-500/10 text-sky-700 dark:text-sky-400",
    note: "已被认领,正在处理",
  },
  done: {
    label: "已完成", cls: "bg-emerald-500/10 text-emerald-700 dark:text-emerald-400",
    note: "已处理完毕",
  },
  skipped: {
    label: "已跳过", cls: "bg-slate-500/10 text-slate-600 dark:text-slate-400",
    note: "消费闸拦下,这是刻意不做,不是故障",
  },
  failed: {
    label: "失败", cls: "bg-destructive/10 text-destructive",
    note: "处理失败。系统刻意不自动重试(坏事件会无限循环),需人工决定",
  },
};

function statusOf(s: string) {
  return STATUS[s] || { label: s, cls: "bg-muted text-muted-foreground", note: "" };
}

// 优先级只有三档(见 routing.py:连续值会让每次新增订阅都要纠结填 37 还是 42)。
function priorityBadge(p: number): { label: string; cls: string } | null {
  if (p >= 10) return { label: "紧急", cls: "bg-destructive/10 text-destructive" };
  if (p <= -10) return { label: "低", cls: "bg-muted text-muted-foreground" };
  return null;   // 普通档不加徽章:每条都挂一个"普通"等于没有信息,只是噪声
}

// 后端时间戳是 "YYYY-MM-DD HH:MM:SS",Safari 不认这种带空格的格式。
// created_at 与 consumed_at 同源(都来自 Database._now(),同一个本地时钟),
// 所以两者相减是可信的;但**绝不拿浏览器的 now 去减服务端时间**——两边时钟
// 不一致会算出负数或夸张数值(与 collab/health 把 stale_seconds 放在服务端算
// 是同一条原则)。
function parseTs(s: string | null): number | null {
  if (!s) return null;
  const t = Date.parse(s.replace(" ", "T"));
  return Number.isNaN(t) ? null : t;
}

// 注意这是**排队等待**时长,不是处理耗时:consumed_at 写在事件被认领的那一刻
// (status → processing),不是处理完成时。标成"耗时"会让人以为参谋跑了 12 秒,
// 而实际上那 12 秒是它躺在队列里等 worker 轮询。排队时长恰好是优先级排序真正
// 影响的那个量,所以它值得单独摆出来。
function waitLabel(ev: CollabEvent): string {
  const a = parseTs(ev.created_at);
  const b = parseTs(ev.consumed_at);
  if (a === null || b === null) return "";
  const sec = Math.max(0, Math.round((b - a) / 1000));
  return sec < 60 ? `等待 ${sec}s` : `等待 ${Math.round(sec / 60)}min`;
}

function agentLabel(routing: CollabRouting | null, key: string): string {
  return routing?.agents.find((a) => a.key === key)?.label || key || "—";
}

/** 事件 payload 里挑几个有信息量的键渲染成一行,不整段吐 JSON。 */
function payloadLine(payload: Record<string, unknown>): string {
  const keys = ["kind", "subject", "subject_name", "drafted", "outcome", "draft_id", "user_id"];
  return keys
    .filter((k) => payload?.[k] !== undefined && payload?.[k] !== null && payload?.[k] !== "")
    .map((k) => `${k}=${String(payload[k])}`)
    .join(" · ");
}

/** 这条事件是不是「跑过了,但产出是降级的」。
 *
 * 实测撞到过:一轮 worker 里 20 条事件全部 `done: 20, failed: 0`,而其中**每一次
 * LLM 调用都失败了**——参谋走了"归因不可用 → 降级为纯统计"分支,发出的诊断
 * 只剩数字和告警线。事件确实处理完了,所以标 done 没错;但时间线上它与一次
 * 真实的成功分析**长得一模一样**,运维看到 20 个绿点,以为参谋分析了 20 次。
 *
 * `degraded` 本来就在 `insight.diagnosis` 的 payload 里(路由表还靠它决定
 * 不转营销),只是没人把它显示出来。
 */
function isDegraded(payload: Record<string, unknown>): boolean {
  return payload?.degraded === true;
}

export function CollabView() {
  const [tab, setTab] = useState<"chains" | "inbox" | "rules">("chains");

  const [inbox, setInbox] = useState<CollabInbox | null>(null);
  const [inboxErr, setInboxErr] = useState("");

  const [chains, setChains] = useState<CollabChain[] | null>(null);
  const [chainsErr, setChainsErr] = useState("");
  const [selected, setSelected] = useState<string>("");
  const [timeline, setTimeline] = useState<CollabTimeline | null>(null);
  const [tlErr, setTlErr] = useState("");
  const [tlBusy, setTlBusy] = useState(false);

  const [routing, setRouting] = useState<CollabRouting | null>(null);
  const [routingErr, setRoutingErr] = useState("");

  // 健康各自独立 err:它读取失败不该连累链列表,反过来也一样(与 GrowthPanel 同口径)。
  const [health, setHealth] = useState<CollabHealth | null>(null);
  const [healthErr, setHealthErr] = useState("");

  async function loadChains() {
    try {
      const r = await getCollabChains();
      setChains(r.chains);
      setChainsErr("");
      // 首次加载自动选中最近一条:一个需要先点一下才显示任何东西的时间线,
      // 大多数人只会看到空白面板然后离开。
      if (!selected && r.chains.length > 0) void openChain(r.chains[0].correlation_id);
    } catch (e) {
      setChainsErr(String(e));
    }
  }

  async function openChain(cid: string) {
    setSelected(cid);
    setTlBusy(true);
    try {
      const t = await getCollabTimeline(cid);
      // 形状校验:渲染时要 .map 这两个数组,任何缺字段的响应(旧服务端、代理
      // 返回的 200 错误页)都会在渲染期抛 TypeError 把整页带崩。
      if (!t || !Array.isArray(t.events) || !Array.isArray(t.shared)) {
        setTimeline(null);
        setTlErr("响应格式不正确(服务端版本可能不匹配)");
        return;
      }
      setTimeline(t);
      setTlErr("");
    } catch (e) {
      setTimeline(null);
      setTlErr(String(e));
    } finally {
      setTlBusy(false);
    }
  }

  async function loadRouting() {
    try {
      setRouting(await getCollabRouting());
      setRoutingErr("");
    } catch (e) {
      setRoutingErr(String(e));
    }
  }

  async function loadInbox() {
    try {
      const r = await getCollabInbox();
      if (!r || !Array.isArray(r.events)) {
        setInbox(null);
        setInboxErr("响应格式不正确(服务端版本可能不匹配)");
        return;
      }
      setInbox(r);
      setInboxErr("");
    } catch (e) {
      setInbox(null);
      setInboxErr(String(e));
    }
  }

  async function onAck(id: number) {
    try {
      await ackCollabEvent(id);
      await loadInbox();
    } catch (e) {
      setInboxErr(String(e));
    }
  }

  async function loadHealth() {
    try {
      const h = await getCollabHealth();
      if (!h || !h.worker || !Array.isArray(h.failed)) {
        setHealth(null);
        setHealthErr("响应格式不正确(服务端版本可能不匹配)");
        return;
      }
      setHealth(h);
      setHealthErr("");
    } catch (e) {
      setHealth(null);
      setHealthErr(String(e));
    }
  }

  async function onRetry(id: number) {
    try {
      await retryFailedEvent(id);
      await Promise.all([loadHealth(), loadChains()]);
      if (selected) await openChain(selected);
    } catch (e) {
      setHealthErr(String(e));
    }
  }

  useEffect(() => {
    loadChains();
    loadRouting();
    loadHealth();
    loadInbox();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 事件按 id 升序渲染(后端给的是 DESC):时间线是"先发生的在上面",倒序
  // 会把因果关系读反——看起来像营销先起草、参谋才诊断。
  const events = [...(timeline?.events || [])].sort((a, b) => a.id - b.id);

  return (
    <div className="h-full overflow-y-auto p-6">
      <div className="mx-auto flex max-w-6xl flex-col gap-5">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <h2 className="text-lg font-semibold">多智能体协作</h2>
            <p className="mt-0.5 text-xs text-muted-foreground">
              事件驱动 + 隐式编排：发布方只宣布「发生了什么」，收件人由路由表决定。
              三个 Expert 之间没有任何一方在指挥另一方。
            </p>
          </div>
          <Button variant="ghost" size="sm"
                  onClick={() => { loadChains(); loadHealth(); loadRouting(); }}>
            <RotateCcw className="h-3.5 w-3.5" /> 刷新
          </Button>
        </div>

        {/* 健康条常驻在两个页签之上:worker 停摆时,下面的链列表会安静地停止
            增长而没有任何症状——买家链路一切正常。那正是最危险的失效形态。 */}
        <HealthStrip health={health} err={healthErr} onRetry={onRetry} />

        <div className="flex items-center gap-2 border-b pb-2">
          {([
            { key: "chains" as const, label: "协作链", icon: <Radio className="h-3.5 w-3.5" /> },
            {
              key: "inbox" as const, icon: <Inbox className="h-3.5 w-3.5" />,
              label: `人工待办${inbox && inbox.pending > 0 ? `（${inbox.pending}）` : ""}`,
            },
            { key: "rules" as const, label: "编排规则", icon: <Workflow className="h-3.5 w-3.5" /> },
          ]).map((t) => (
            <button key={t.key} onClick={() => setTab(t.key)}
              className={`flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm transition-colors ${
                tab === t.key ? "bg-primary text-primary-foreground"
                              : "bg-secondary text-muted-foreground hover:bg-muted"}`}>
              {t.icon} {t.label}
            </button>
          ))}
        </div>

        {tab === "chains" && (
          <div className="grid gap-4 lg:grid-cols-[minmax(0,20rem)_minmax(0,1fr)]">
            {/* 链列表 */}
            <section className="flex flex-col gap-2">
              <h3 className="text-sm font-semibold">最近协作链（{chains?.length ?? 0}）</h3>
              {chainsErr && <div className="text-sm text-destructive">读取失败：{chainsErr}</div>}
              {!chains && !chainsErr && <div className="text-sm text-muted-foreground">加载中…</div>}
              {chains && chains.length === 0 && (
                <Card className="p-3 text-xs text-muted-foreground">
                  暂无协作链。触发方式：跑一次协作 worker
                  <code className="mx-1 rounded bg-muted px-1">python -m app.scripts.agent_collab</code>
                  ，或在买家会话里触发一次转人工。
                </Card>
              )}
              <div className="flex flex-col gap-1.5">
                {(chains || []).map((c) => {
                  const active = c.correlation_id === selected;
                  return (
                    <button key={c.correlation_id} onClick={() => openChain(c.correlation_id)}
                      data-testid={`chain-${c.correlation_id}`}
                      className={`rounded-lg border p-2.5 text-left transition-colors ${
                        active ? "border-primary bg-primary/5" : "bg-card hover:bg-secondary/60"}`}>
                      <div className="flex items-center gap-2">
                        <span className="font-mono text-xs">{c.correlation_id}</span>
                        {c.failed > 0 && (
                          <span className="rounded bg-destructive/10 px-1.5 py-0.5 text-[10px] text-destructive">
                            {c.failed} 失败
                          </span>
                        )}
                        {c.pending > 0 && (
                          <span className="rounded bg-amber-500/10 px-1.5 py-0.5 text-[10px] text-amber-700 dark:text-amber-400">
                            {c.pending} 待处理
                          </span>
                        )}
                        {/* 这里**没有**降级徽章:降级诊断不产生任何总线事件
                            (路由无目标 → 不插行),按链统计只会恒为 0。
                            降级看上方健康条的 degraded 段。 */}
                      </div>
                      <div className="mt-1 flex flex-wrap items-center gap-1 text-[11px] text-muted-foreground">
                        {c.agents.map((a, i) => (
                          <span key={`${a}-${i}`} className="flex items-center gap-1">
                            {i > 0 && <ArrowRight className="h-3 w-3" />}
                            {agentLabel(routing, a)}
                          </span>
                        ))}
                      </div>
                      <div className="mt-1 text-[11px] text-muted-foreground">
                        {c.events} 个事件 · {c.started_at}
                      </div>
                    </button>
                  );
                })}
              </div>
            </section>

            {/* 时间线 */}
            <section className="flex flex-col gap-3">
              <h3 className="text-sm font-semibold">
                时间线{selected && <span className="ml-1 font-mono text-xs font-normal text-muted-foreground">{selected}</span>}
              </h3>
              {tlErr && <div className="text-sm text-destructive">读取失败：{tlErr}</div>}
              {tlBusy && <div className="text-sm text-muted-foreground">加载中…</div>}
              {!selected && !tlBusy && (
                <div className="text-sm text-muted-foreground">左侧选一条协作链查看它的完整时间线</div>
              )}

              {events.length > 0 && (
                <div className="flex flex-col" data-testid="timeline">
                  {events.map((ev, i) => {
                    const st = statusOf(ev.status);
                    const pr = priorityBadge(ev.priority);
                    const wait = waitLabel(ev);
                    const facts = payloadLine(ev.payload || {});
                    return (
                      <div key={ev.id} className="flex gap-3" data-testid={`event-${ev.id}`}>
                        {/* 竖线 + 节点:最后一条不画向下的连线 */}
                        <div className="flex flex-col items-center">
                          <div className={`mt-3 h-2.5 w-2.5 shrink-0 rounded-full ${
                            ev.status === "failed" ? "bg-destructive"
                            : ev.status === "done" ? "bg-emerald-500"
                            : ev.status === "skipped" ? "bg-slate-400"
                            : "bg-amber-500"}`} />
                          {i < events.length - 1 && <div className="w-px flex-1 bg-border" />}
                        </div>
                        <Card className="mb-2 flex-1 p-3">
                          <div className="flex flex-wrap items-center gap-2 text-sm">
                            <span className="font-medium">{agentLabel(routing, ev.source_agent)}</span>
                            <ArrowRight className="h-3.5 w-3.5 text-muted-foreground" />
                            <span className="font-medium">{agentLabel(routing, ev.target_agent)}</span>
                            <code className="rounded bg-muted px-1.5 py-0.5 text-[11px]">{ev.event_type}</code>
                            <span className={`rounded px-1.5 py-0.5 text-[11px] ${st.cls}`}
                                  title={st.note}>{st.label}</span>
                            {pr && (
                              <span className={`rounded px-1.5 py-0.5 text-[11px] ${pr.cls}`}>
                                优先级 {pr.label}
                              </span>
                            )}
                            {/* 「已完成」+「降级」是两件必须同时看到的事:事件处理完了,
                                但产出是空的。只显示前者会让 20 个绿点被读成
                                20 次真实分析。 */}
                            {isDegraded(ev.payload || {}) && (
                              <span className="rounded bg-amber-500/10 px-1.5 py-0.5 text-[11px]
                                               text-amber-700 dark:text-amber-400"
                                    title="归因不可用(模型调用失败/超时),本条只剩数字与告警线,不会转给营销">
                                降级产出
                              </span>
                            )}
                          </div>
                          {facts && (
                            <div className="mt-1 text-xs text-muted-foreground">{facts}</div>
                          )}
                          <div className="mt-1 text-[11px] text-muted-foreground">
                            {ev.created_at}{wait && ` · ${wait}`}
                          </div>
                          {/* 跳过与失败都要给出"这意味着什么",否则一个灰色徽章
                              等于没说——尤其 skipped 很容易被误读成出错。 */}
                          {(ev.status === "skipped" || ev.status === "failed") && (
                            <div className={`mt-1 text-[11px] ${
                              ev.status === "failed" ? "text-destructive" : "text-muted-foreground"}`}>
                              {st.note}
                            </div>
                          )}
                        </Card>
                      </div>
                    );
                  })}
                </div>
              )}

              {/* 共享上下文:这条链往共享池里写了什么。它是参谋的结论真正流到
                  客服侧的载体,不摆出来的话"跨 Agent 共享记忆"就只是一句宣称。 */}
              {timeline && timeline.shared.length > 0 && (
                <div data-testid="chain-shared">
                  <h4 className="mb-1.5 text-sm font-semibold">该链写入的共享上下文</h4>
                  <div className="flex flex-col gap-1.5">
                    {timeline.shared.map((s) => (
                      <Card key={s.key} className="p-2.5 text-xs">
                        <div className="flex flex-wrap items-center gap-2">
                          <code className="rounded bg-muted px-1.5 py-0.5">{s.key}</code>
                          <span className="text-muted-foreground">
                            由 {agentLabel(routing, s.source_agent)} 写入 · {s.updated_at}
                          </span>
                        </div>
                        <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap break-all
                                        rounded bg-secondary/40 p-2 text-[11px] leading-relaxed">
                          {typeof s.value === "string" ? s.value : JSON.stringify(s.value, null, 2)}
                        </pre>
                      </Card>
                    ))}
                  </div>
                </div>
              )}
              {timeline && timeline.shared.length === 0 && events.length > 0 && (
                <div className="text-xs text-muted-foreground">该链未写入共享上下文</div>
              )}
            </section>
          </div>
        )}

        {tab === "inbox" && (
          <InboxPanel inbox={inbox} err={inboxErr} routing={routing} onAck={onAck} />
        )}

        {tab === "rules" && (
          <RulesPanel routing={routing} err={routingErr} />
        )}
      </div>
    </div>
  );
}

/** 协作健康:worker 心跳 + LLM 预算 + 失败事件。 */
function HealthStrip({ health, err, onRetry }:
  { health: CollabHealth | null; err: string; onRetry: (id: number) => void }) {
  if (err) return <Card className="p-3 text-sm text-destructive">协作健康读取失败：{err}</Card>;
  if (!health) return null;
  const w = health.worker;
  const b = health.budget;
  return (
    <div className="flex flex-col gap-2" data-testid="collab-health-strip">
      <Card className={`flex flex-wrap items-center gap-x-4 gap-y-1 p-3 text-sm ${
        w.healthy ? "" : "border-destructive"}`}>
        <span className="flex items-center gap-1.5">
          <span className={`h-2 w-2 rounded-full ${w.healthy ? "bg-emerald-500" : "bg-destructive"}`} />
          <span className="font-medium">协作 worker</span>
        </span>
        {w.healthy ? (
          <span className="text-muted-foreground">{w.stale_seconds ?? 0} 秒前跑完一轮</span>
        ) : w.last_success_at ? (
          // 跑过但停了 ≠ 从未跑过。前者是"部署了但挂了",后者多半是"压根没起
          // worker",要做的事完全不同,提示必须分开。
          <span className="text-destructive">
            已停摆：上次跑完 {w.last_success_at}（超 {w.threshold_seconds}s 判异常）。
            买家链路不受影响，但异常扫描、归因、起草、跟进都不会发生。
          </span>
        ) : (
          <span className="text-destructive">
            从未运行。启动：
            <code className="mx-1 rounded bg-muted px-1">python -m app.scripts.agent_collab --loop</code>
          </span>
        )}
        {b?.enabled && (
          <span className="text-muted-foreground" title={b.note}>
            · LLM 预算 {b.spent}/{b.limit}
            <span className="ml-1 text-[11px]">（{b.scope}，{b.note}）</span>
          </span>
        )}
        {w.last_error && (
          <span className="w-full text-xs text-muted-foreground">
            最近报错（{w.last_error_at}）：{w.last_error}
          </span>
        )}
      </Card>

      {/* 归因降级:事件状态是 done、失败数是 0,但诊断里没有真正的归因。
          全降级是一个**明确的故障态**——不摆出来的话,一次完全无效的运行和
          一次健康的运行在界面上一模一样。 */}
      {health.degraded && health.degraded.diagnoses > 0 && health.degraded.degraded > 0 && (
        <Card className={`p-3 text-sm ${health.degraded.all_degraded ? "border-destructive" : ""}`}
              data-testid="degraded-strip">
          <div className={health.degraded.all_degraded ? "text-destructive" : "text-amber-700 dark:text-amber-400"}>
            {health.degraded.all_degraded ? "⚠ " : ""}
            最近 {health.degraded.diagnoses} 条诊断里有 <b>{health.degraded.degraded}</b> 条是
            <b>降级</b>的（{(health.degraded.rate * 100).toFixed(0)}%）
            {health.degraded.all_degraded && "——本轮归因全部不可用"}
          </div>
          <div className="mt-1 text-[11px] text-muted-foreground">
            {health.degraded.note}。事件状态仍是「已完成」、失败数仍是 0，
            所以<b>只看协作链颜色看不出这件事</b>。
          </div>
        </Card>
      )}

      {health.failed_count > 0 && (
        <Card className="border-destructive p-3">
          <div className="mb-2 flex items-center gap-1.5 text-sm text-destructive">
            <AlertTriangle className="h-4 w-4" />
            {health.failed_count} 条协作事件处理失败，停在队列里等人决定（系统不自动重试，避免坏事件无限循环）
          </div>
          <div className="flex flex-col gap-1">
            {health.failed.map((e) => (
              <div key={e.id} className="flex flex-wrap items-center gap-2 text-xs"
                   data-testid={`strip-failed-${e.id}`}>
                <code className="rounded bg-muted px-1.5 py-0.5">{e.event_type}</code>
                <span className="text-muted-foreground">→ {e.target_agent}</span>
                <span className="font-mono text-muted-foreground">{e.correlation_id}</span>
                <span className="text-muted-foreground">{e.created_at}</span>
                <Button variant="ghost" size="sm" className="ml-auto" onClick={() => onRetry(e.id)}>
                  放回队列重试
                </Button>
              </div>
            ))}
          </div>
        </Card>
      )}
    </div>
  );
}

/** 人工闸待办:投给 `human` 却没有任何消费方的事件。
 *
 * 这个面板存在的理由与失败事件那张卡一样:路由表声明"这类事件投给人工",
 * 而 worker 只消费 analyst 与 growth——在此之前 human 的事件写进去就再没有
 * 出口,实测积压 325 条。"留给人工"事实上是"留给没人"。
 */
function InboxPanel({ inbox, err, routing, onAck }: {
  inbox: CollabInbox | null; err: string; routing: CollabRouting | null;
  onAck: (id: number) => void;
}) {
  if (err) return <div className="text-sm text-destructive">读取失败：{err}</div>;
  if (!inbox) return <div className="text-sm text-muted-foreground">加载中…</div>;

  return (
    <div className="flex flex-col gap-3" data-testid="collab-inbox">
      <p className="text-xs text-muted-foreground">
        这些事件按路由表被投给<b>人工闸</b>，系统不会自动处理它们（
        <code className="rounded bg-muted px-1">action.drafts_ready</code> 要人审批、
        <code className="rounded bg-muted px-1">result.outreach_*</code> 要人过目）。
        处理完点「已确认」把它从待办里划掉——确认<b>只改这条事件的状态</b>，
        不代表批准或发送任何东西，草稿的批准仍在「经营控制台 → 商机与触达」。
      </p>

      {inbox.events.length === 0 && (
        <div className="text-sm text-muted-foreground">没有待办事件</div>
      )}

      <div className="flex flex-col gap-1.5">
        {inbox.events.map((ev) => {
          const pr = priorityBadge(ev.priority);
          const facts = payloadLine(ev.payload || {});
          return (
            <Card key={ev.id} className="flex flex-wrap items-center gap-2 p-2.5 text-xs"
                  data-testid={`inbox-${ev.id}`}>
              <code className="rounded bg-muted px-1.5 py-0.5">{ev.event_type}</code>
              <span className="text-muted-foreground">
                来自 {agentLabel(routing, ev.source_agent)}
              </span>
              {pr && (
                <span className={`rounded px-1.5 py-0.5 ${pr.cls}`}>优先级 {pr.label}</span>
              )}
              {facts && <span className="text-muted-foreground">{facts}</span>}
              <span className="font-mono text-muted-foreground">{ev.correlation_id}</span>
              <span className="text-muted-foreground">{ev.created_at}</span>
              <Button variant="ghost" size="sm" className="ml-auto"
                      onClick={() => onAck(ev.id)}>
                已确认
              </Button>
            </Card>
          );
        })}
      </div>

      {inbox.pending > inbox.events.length && (
        <div className="text-xs text-muted-foreground">
          仅显示前 {inbox.events.length} 条（共 {inbox.pending} 条待办）
        </div>
      )}
    </div>
  );
}

/** 编排规则:Agent 名册 + 订阅表 + 消费闸。全部来自后端唯一口径。 */
function RulesPanel({ routing, err }: { routing: CollabRouting | null; err: string }) {
  if (err) return <div className="text-sm text-destructive">路由表读取失败：{err}</div>;
  if (!routing) return <div className="text-sm text-muted-foreground">加载中…</div>;

  const SIDE: Record<string, { label: string; cls: string }> = {
    buyer: { label: "买家侧", cls: "bg-primary/10 text-primary" },
    seller: { label: "商家侧", cls: "bg-accent/15 text-accent" },
    human: { label: "人工", cls: "bg-destructive/10 text-destructive" },
  };

  // 按事件类型分组:同一条事件可能同时唤醒多个 Agent(这正是路由表相对
  // "发布方直接指定 target" 的关键区别),分组渲染才看得出这一点。
  const grouped = routing.subscriptions.reduce<Record<string, typeof routing.subscriptions>>(
    (acc, s) => { (acc[s.event_type] ||= []).push(s); return acc; }, {});

  return (
    <div className="flex flex-col gap-5">
      <section>
        <h3 className="mb-2 text-sm font-semibold">Agent 名册</h3>
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
          {routing.agents.map((a) => {
            const side = SIDE[a.side] || { label: a.side, cls: "bg-muted text-muted-foreground" };
            return (
              <Card key={a.key} className="p-3" data-testid={`agent-${a.key}`}>
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium">{a.label}</span>
                  <span className={`rounded px-1.5 py-0.5 text-[10px] ${side.cls}`}>{side.label}</span>
                </div>
                <code className="mt-1 block text-[11px] text-muted-foreground">{a.key}</code>
                <div className="mt-1 text-xs text-muted-foreground">{a.desc}</div>
              </Card>
            );
          })}
        </div>
      </section>

      <section>
        <h3 className="mb-1 text-sm font-semibold">事件订阅表</h3>
        <p className="mb-2 text-xs text-muted-foreground">
          发布方只宣布事件类型，收件人由这张表决定。<b>路由是调度，不是授权</b>——
          往表里加一条订阅只决定谁被唤醒，不会让任何 Agent 多出一分权限
          （能力边界由工具子集、consent 门、人工审批闸各自强制）。
        </p>
        <div className="flex flex-col gap-2">
          {Object.entries(grouped).map(([ev, subs]) => (
            <Card key={ev} className="p-3" data-testid={`sub-${ev}`}>
              <code className="rounded bg-muted px-1.5 py-0.5 text-xs">{ev}</code>
              <div className="mt-2 flex flex-col gap-1.5">
                {subs.map((s, i) => (
                  <div key={`${s.target}-${i}`} className="flex flex-wrap items-center gap-2 text-xs">
                    <ArrowRight className="h-3 w-3 text-muted-foreground" />
                    <span className="font-medium">
                      {routing.agents.find((a) => a.key === s.target)?.label || s.target}
                    </span>
                    {s.conditional && (
                      <span className="rounded bg-amber-500/10 px-1.5 py-0.5 text-[10px] text-amber-700 dark:text-amber-400">
                        有条件
                      </span>
                    )}
                    {/* 普通档不显示徽章(每条都挂"普通"只是噪声);动态优先级
                        如实标出来,不能挑一个档位糊弄——同一个 signal.anomaly,
                        转人工是紧急、例行扫描是普通,摆一个"普通"会误导。 */}
                    {(s.priority_dynamic || s.priority !== 0) && (
                      <span className="rounded bg-secondary px-1.5 py-0.5 text-[10px] text-muted-foreground">
                        {s.priority_label}
                      </span>
                    )}
                    <span className="text-muted-foreground">{s.reason}</span>
                  </div>
                ))}
              </div>
            </Card>
          ))}
        </div>
      </section>

      <section>
        <h3 className="mb-1 text-sm font-semibold">消费闸</h3>
        <p className="mb-2 text-xs text-muted-foreground">
          与订阅条件是两回事：订阅条件在<b>发布</b>那一刻判（纯内存，"这类事件该不该给它"），
          消费闸在<b>消费</b>那一刻判（可读库，"此刻该不该动手"）。状态在发布与消费之间会变，
          所以状态判断必须放在消费侧。两者失败方向也相反——订阅 fail-closed（判不清就不唤醒），
          闸 fail-open（判不清就别拦已经该做的事）。
        </p>
        <div className="flex flex-col gap-2">
          {routing.gates.map((g) => (
            <Card key={g.target} className="p-3 text-xs" data-testid={`gate-${g.target}`}>
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium">
                  {routing.agents.find((a) => a.key === g.target)?.label || g.target}
                </span>
                <code className="rounded bg-muted px-1.5 py-0.5">{g.name}</code>
              </div>
              <div className="mt-1 text-muted-foreground">{g.reason}</div>
            </Card>
          ))}
          {routing.gates.length === 0 && (
            <div className="text-xs text-muted-foreground">当前没有登记消费闸（所有事件一律放行）</div>
          )}
        </div>
      </section>
    </div>
  );
}
