import { useEffect, useRef, useState } from "react";
import { getSellerOverview, sellerChat,
  type SellerOverview, type SellerChatReply } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { RotateCcw, Send } from "lucide-react";

// 模块级缓存(不是 state):App.tsx 按 view 条件渲染各 Tab,离开"经营"页会把
// OperationsView 整个卸载,切回来是全新挂载,组件内 state 会清零。若只靠
// state,刷新失败这类"旧数据+过期标记"的场景在离开再回来后就直接消失
// (界面变回一片空白,店主会误读成"系统从没采集过数据",而不是"这次没
// 刷新到最新值")。把上一次成功拉取的快照存在组件外,新挂载先拿它垫底。
let cachedOverview: SellerOverview | null = null;
let cachedWindowDays = 7;

type ChatMsg = { role: "user" | "assistant"; text: string; agent?: string };

const WINDOW_OPTIONS = [7, 14, 30];

function pct(x: number, digits = 1): string {
  return (x * 100).toFixed(digits) + "%";
}

// 跨线幅度越大颜色越重:超出告警线一半以上标红,刚跨线标橙,理论上不会出现
// 但兜个底(未跨线不该出现在异常清单里,出现了也不误判为安全色)。
function anomalyTone(value: number, threshold: number): string {
  if (threshold <= 0) return "text-destructive";
  const over = (value - threshold) / threshold;
  if (over >= 0.5) return "text-destructive";
  if (over > 0) return "text-amber-600 dark:text-amber-400";
  return "text-muted-foreground";
}

export function OperationsView() {
  const [data, setData] = useState<SellerOverview | null>(cachedOverview);
  const [err, setErr] = useState("");
  // 刷新失败时下方仍是上一次成功拉取的旧数据。店主正是靠这个面板判断某项
  // 异常要不要处理,所以必须显式标出"这是过期数据",不能默默照常渲染
  // (与 SkillsView 同口径:stale 与 data 分开存,stale 为 true 时旧 data 保留不清空)。
  const [stale, setStale] = useState(false);
  const [busy, setBusy] = useState(false);
  const [windowDays, setWindowDays] = useState(cachedWindowDays);

  // 对话请求独立一套 busy/err:复用页面级 err 会把"参谋这轮答不上来"渲染成
  // "经营数据读取失败",把店主引到完全错误的方向(与 SkillsView 的
  // upErr/dsErr 同一道理)。
  const [chatBusy, setChatBusy] = useState(false);
  const [chatErr, setChatErr] = useState("");
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [draft, setDraft] = useState("");
  // 会话内复用同一个 session_id,不用每轮重生成(参谋侧要能看到"这轮"之前的上下文)。
  const sessionIdRef = useRef(`seller-${Date.now()}`);

  async function load(days: number) {
    setBusy(true);
    try {
      const d = await getSellerOverview(days);
      cachedOverview = d;
      cachedWindowDays = days;
      setData(d);
      setErr("");
      setStale(false);
    } catch (e) {
      // 旧 data 原样保留,只标 stale——绝不能让这次失败悄悄清空或替换成空壳数据。
      setErr(String(e));
      setStale(true);
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    load(windowDays);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [windowDays]);

  async function onSend() {
    const text = draft.trim();
    if (!text || chatBusy) return;
    setMessages((m) => [...m, { role: "user", text }]);
    setDraft("");
    setChatBusy(true);
    setChatErr("");
    try {
      const r: SellerChatReply = await sellerChat(sessionIdRef.current, text);
      setMessages((m) => [...m, { role: "assistant", text: r.reply, agent: r.agent }]);
    } catch (e) {
      setChatErr(`参谋请求失败（网络或鉴权）：${String(e)}`);
    } finally {
      setChatBusy(false);
    }
  }

  const overview = data?.overview;
  const windowLabel = overview ? `近 ${overview.window_days} 天` : "";
  const metrics = overview ? [
    { label: "订单量", value: String(overview.orders) },
    { label: "GMV", value: `¥${overview.gmv.toLocaleString()}` },
    { label: "客单价", value: `¥${overview.avg_order_value}` },
    { label: "退款率", value: pct(overview.refund_rate) },
    { label: "咨询会话数", value: String(overview.conversations) },
  ] : [];

  return (
    <div className="h-full overflow-y-auto p-6">
      <div className="mx-auto flex max-w-4xl flex-col gap-6">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <h2 className="text-lg font-semibold">经营控制台</h2>
            {stale && data && (
              <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-[11px]
                               text-amber-700 dark:text-amber-400">数据已过期</span>
            )}
          </div>
          <div className="flex items-center gap-2">
            {WINDOW_OPTIONS.map((d) => (
              <button
                key={d}
                onClick={() => setWindowDays(d)}
                className={`rounded px-2 py-1 text-xs transition-colors ${
                  d === windowDays
                    ? "bg-primary text-primary-foreground"
                    : "bg-secondary text-muted-foreground hover:bg-muted"
                }`}
              >
                {d} 天
              </button>
            ))}
            <Button variant="ghost" size="sm" onClick={() => load(windowDays)}>
              <RotateCcw className="h-3.5 w-3.5" /> 刷新
            </Button>
          </div>
        </div>

        {err && <div className="text-sm text-destructive">读取失败：{err}</div>}
        {/* 刷新失败但下方仍有旧数据:必须明说这是过期快照。店主靠这个面板决定
            某个异常要不要处理,拿旧数据当现状会直接做错决定。 */}
        {stale && data && (
          <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-2
                          text-xs text-amber-700 dark:text-amber-400">
            ⚠️ 以下内容是<b>上次成功刷新时的旧数据</b>（本次刷新失败，最新状态未知）。
            请勿据此判断当前是否需要处理，先排除上面的读取失败再操作。
          </div>
        )}

        {/* 关键指标:每张卡片都挂在同一个统计窗口下——脱离窗口的数字对店主没有意义 */}
        <section>
          <h3 className="mb-2 text-sm font-semibold">
            关键指标{windowLabel && `（${windowLabel}）`}
          </h3>
          {!data && busy && <div className="text-sm text-muted-foreground">加载中…</div>}
          {!data && !busy && err && <div className="text-sm text-muted-foreground">暂无数据</div>}
          {metrics.length > 0 && (
            <div className={`grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-5 ${stale ? "opacity-60" : ""}`}>
              {metrics.map((m) => (
                <Card key={m.label} className="p-3">
                  <div className="text-xs text-muted-foreground">{m.label}</div>
                  <div className="mt-1 text-lg font-semibold">{m.value}</div>
                </Card>
              ))}
            </div>
          )}
        </section>

        {/* 异常清单:每条都并排给出当前值与告警线,以及跨线幅度对应的颜色 */}
        <section>
          <h3 className="mb-2 text-sm font-semibold">跨线异常（{data?.anomalies.length ?? 0}）</h3>
          <div className={`flex flex-col gap-2 ${stale ? "opacity-60" : ""}`}>
            {!data && busy && <div className="text-sm text-muted-foreground">加载中…</div>}
            {(data?.anomalies || []).map((a, i) => (
              <Card key={`${a.kind}-${a.subject}-${i}`} className="p-3 text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">{a.subject_name || a.subject}</span>
                  <span className={anomalyTone(a.value, a.threshold)}>
                    当前 {pct(a.value, 2)} · 告警线 {pct(a.threshold, 2)}
                  </span>
                </div>
                {a.detail && Object.keys(a.detail).length > 0 && (
                  <div className="mt-1 text-xs text-muted-foreground">
                    {Object.entries(a.detail).map(([k, v]) => `${k}:${v}`).join(" · ")}
                  </div>
                )}
              </Card>
            ))}
            {data && data.anomalies.length === 0 && (
              <div className="text-sm text-muted-foreground">当前无跨线异常</div>
            )}
          </div>
        </section>

        {/* 参谋对话:每轮回复都标出本轮由哪个 Agent 回答 */}
        <section>
          <h3 className="mb-2 text-sm font-semibold">问参谋</h3>
          <Card className="flex flex-col gap-2 p-4 text-sm">
            <div className="flex flex-col gap-2">
              {messages.length === 0 && (
                <div className="text-xs text-muted-foreground">
                  可以问参谋，比如"退款率为什么升高"。
                </div>
              )}
              {messages.map((m, i) => (
                <div key={i} className={`flex flex-col gap-0.5 ${m.role === "user" ? "items-end" : "items-start"}`}>
                  {m.role === "assistant" && m.agent && (
                    <span className="rounded bg-secondary px-1.5 py-0.5 text-[11px] text-muted-foreground">
                      {m.agent}
                    </span>
                  )}
                  <span className={`max-w-[85%] whitespace-pre-wrap rounded-md px-3 py-1.5 text-sm ${
                    m.role === "user" ? "bg-primary text-primary-foreground" : "bg-secondary"
                  }`}>
                    {m.text}
                  </span>
                </div>
              ))}
            </div>
            <div className="flex items-center gap-2">
              <input
                className="h-8 flex-1 rounded border bg-background px-2 text-xs"
                value={draft}
                placeholder="向参谋提问经营情况…"
                disabled={chatBusy}
                onChange={(e) => setDraft(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter") onSend(); }}
              />
              <Button size="sm" disabled={chatBusy || !draft.trim()} onClick={onSend}>
                <Send className="h-3.5 w-3.5" /> {chatBusy ? "提问中…" : "发送"}
              </Button>
            </div>
            {chatErr && <div className="text-xs text-destructive">⚠️ {chatErr}</div>}
          </Card>
        </section>
      </div>
    </div>
  );
}
