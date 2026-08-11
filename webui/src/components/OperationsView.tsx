import { useEffect, useRef, useState } from "react";
import { getSellerOverview, sellerChat,
  type SellerOverview, type SellerChatReply, type EmotionDistribution,
  type ReviewInsights, type SkillQuality } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { RotateCcw, Send } from "lucide-react";
import { GrowthPanel } from "@/components/operations/GrowthPanel";
import { ShopProfilePanel } from "@/components/operations/ShopProfilePanel";

type ChatMsg = { role: "user" | "assistant"; text: string; agent?: string };

const WINDOW_OPTIONS = [7, 14, 30];

function pct(x: number, digits = 1): string {
  return (x * 100).toFixed(digits) + "%";
}

// 跨线幅度越大颜色越重:超出告警线一半以上标红,刚跨线标橙,理论上不会出现
// 但兜个底(未跨线不该出现在异常清单里,出现了也不误判为安全色)。
// 异常明细里的键值经常混着分数(0~1,要按指标卡同款格式转百分比)和嵌套结构
// (如退款原因是 {reason, count}[] 的数组)——直接 `${k}:${v}` 拼接会把对象
// 字符串化成 [object Object]。这里逐值判断:能安全转成一行文本的转,转不出来
// 的（未知形状的对象）直接省略这一项，绝不能让 [object Object] 露出去。
function formatDetailValue(key: string, value: unknown): string | null {
  if (value === null || value === undefined) return null;
  if (Array.isArray(value)) {
    const parts = value
      .map((item) => {
        if (item && typeof item === "object" && "reason" in item && "count" in item) {
          const it = item as { reason: unknown; count: unknown };
          return `${it.reason}×${it.count}`;
        }
        return null;
      })
      .filter((s): s is string => s !== null);
    return parts.length > 0 ? parts.join("、") : null;
  }
  if (typeof value === "object") return null; // 未知形状的对象,无法安全渲染成文本,宁可省略
  if (typeof value === "number") {
    // rate/ratio 字段是后端给的小数分数,和指标卡用同一套 pct() 格式化,不能原样吐 17 位小数
    if (/rate|ratio/i.test(key)) return pct(value);
    return String(value);
  }
  return String(value);
}

function anomalyTone(value: number, threshold: number): string {
  if (threshold <= 0) return "text-destructive";
  const over = (value - threshold) / threshold;
  if (over >= 0.5) return "text-destructive";
  if (over > 0) return "text-amber-600 dark:text-amber-400";
  return "text-muted-foreground";
}

/** 异常扫描的口径与盲区。
 *
 * 为什么必须显示:这一区的告警按**近况**判(服务健康默认近 1 天),而上面的
 * 「服务质量」「评价」几张卡是按经营窗(默认 7 天)算的。两个数会不一样,而且
 * 应该不一样——"7 天里坏过、今天已经好了"是正常状态,不是看板自相矛盾。不把
 * 两个窗口标出来,店主看到「工具失败率 78%」却「当前无跨线异常」只会得出
 * 「这看板不准」这一个结论。
 *
 * 实测教训见 settings.anomaly_service_window_days:曾经两边共用 7 天窗,于是
 * 一个已经修好的工具连续报警 7 天、每轮扫描一条,协作链页面被同一条假警报
 * 刷满,而当天 9/9 健康完全被淹掉。
 */
export function AnomalyScopeNote({ scope }: { scope?: SellerOverview["anomaly_scope"] }) {
  if (!scope) return null;
  const insufficient = scope.service_insufficient || [];
  return (
    <div className="mt-1 flex flex-col gap-0.5 text-[11px] text-muted-foreground"
         data-testid="anomaly-scope">
      <div>
        统计口径：服务健康（工具失败率／转人工率／情绪）按<b>近 {scope.service_window_days} 天</b>判；
        退款率与差评率按<b>近 {scope.window_days} 天</b>判
        <span className="ml-1">
          （退款、评价有天然滞后，缩窗会把它们压成 0；而一个工具是不是坏的只有"现在"这一个时态）
        </span>
      </div>
      {insufficient.length > 0 && (
        // 「没报警」和「没数据所以报不了警」是两件事。近窗样本不足的 skill 在
        // 这里如实列出,而不是消失成一句"当前无跨线异常"。
        <div>
          近窗样本不足、<b>本轮无法判定</b>的 skill：
          {insufficient.map((s) => `${s.skill_name}（${s.total}/${s.min_samples}）`).join("、")}
        </div>
      )}
      {(scope.products_truncated || scope.reviews_truncated) && (
        <div>
          扫描有截断：
          {scope.products_truncated && `商品只看了前 ${scope.products_examined} 名`}
          {scope.products_truncated && scope.reviews_truncated && "；"}
          {scope.reviews_truncated && `评价只看了 ${scope.reviews_examined} 条`}
          —— 长尾未进入阈值判断
        </div>
      )}
    </div>
  );
}

// N2:情绪分布卡——三档计数 + 激烈(angry)占比。是否"有情绪问题"由 anomaly.py
// 按阈值判定(见上面「跨线异常」区),这里只如实摆出统计口径,不下结论。
export function EmotionDistributionCard({ emotion, windowDays }:
  { emotion?: EmotionDistribution; windowDays: number }) {
  if (!emotion || emotion.total === 0) {
    // 注意:措辞故意避开"近 N 天"这个精确串——它与关键指标卡的窗口标注
    // (windowLabel = `近 ${window_days} 天`)共用同一个正则会被页面级
    // findAllByText 计入同一批次,破坏那边"恰好 5 张卡各一个窗口标注"的断言。
    return (
      <Card className="p-3 text-sm text-muted-foreground" data-testid="emotion-empty">
        过去 {windowDays} 天暂无会话
      </Card>
    );
  }
  return (
    <Card className="p-3 text-sm" data-testid="emotion-distribution">
      <div className="grid grid-cols-3 gap-3">
        <div>
          <div className="text-xs text-muted-foreground">平静</div>
          <div className="mt-1 text-lg font-semibold">{emotion.counts.neutral}</div>
        </div>
        <div>
          <div className="text-xs text-muted-foreground">不满</div>
          <div className="mt-1 text-lg font-semibold">{emotion.counts.unhappy}</div>
        </div>
        <div>
          <div className="text-xs text-muted-foreground">激烈</div>
          <div className="mt-1 text-lg font-semibold">{emotion.counts.angry}</div>
        </div>
      </div>
      <div className="mt-2 text-xs text-muted-foreground">
        激烈占比 {pct(emotion.angry_rate)} · 统计窗口 {windowDays} 天
      </div>
    </Card>
  );
}

/** 按 skill 的服务质量表(成功率 / 工具失败率 / 转人工率)。
 *
 * 这份数据后端一直在返回(`quality.skills`),但前端此前只用了同一响应里的
 * `emotion`,把它整段丢掉了——而它恰恰是这一页最该显示的东西:「跨线异常」区
 * 只列**跨了线**的,不跨线的分布一个都看不到。
 *
 * 补它还有一个更硬的理由:告警的判定窗已经收到近 1 天(见 AnomalyScopeNote),
 * 那么"某个 skill 前几天坏过、今天已经好了"就只能靠这张按经营窗(默认 7 天)
 * 统计的表来看。没有它,缩窗就等于用"少报假警"换"看不见历史",那不算修好。
 *
 * 只摆口径,不下结论:是否算异常由 anomaly.py 按阈值判(见「跨线异常」区),
 * 这里的着色只是让偏高的数字更容易被眼睛抓到,不代表告警。
 */
export function ServiceQualityTable({ skills, windowDays }:
  { skills?: SkillQuality[]; windowDays: number }) {
  const rows = skills || [];
  if (rows.length === 0) {
    return (
      <Card className="p-3 text-sm text-muted-foreground" data-testid="service-quality-empty">
        过去 {windowDays} 天没有 skill 执行记录
      </Card>
    );
  }
  return (
    <Card className="p-3 text-sm" data-testid="service-quality-table">
      <div className="overflow-x-auto">
        <table className="w-full min-w-[420px] text-left">
          <thead>
            <tr className="text-xs text-muted-foreground">
              <th className="pb-1 font-normal">Skill</th>
              <th className="pb-1 text-right font-normal">执行次数</th>
              <th className="pb-1 text-right font-normal">成功率</th>
              <th className="pb-1 text-right font-normal">工具失败率</th>
              <th className="pb-1 text-right font-normal">转人工率</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((s) => (
              <tr key={s.skill_name} className="border-t" data-testid="service-quality-row">
                <td className="py-1 font-medium">{s.skill_name}</td>
                <td className="py-1 text-right tabular-nums">{s.total}</td>
                <td className="py-1 text-right tabular-nums">{pct(s.success_rate)}</td>
                {/* 着色只是为了让偏高的数字更容易被抓到,判异常仍归确定性阈值 */}
                <td className={`py-1 text-right tabular-nums ${
                  s.tool_error_rate >= 0.3 ? "text-destructive"
                    : s.tool_error_rate > 0 ? "text-amber-600 dark:text-amber-400" : ""}`}>
                  {pct(s.tool_error_rate)}
                </td>
                <td className="py-1 text-right tabular-nums">{pct(s.human_rate)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="mt-2 text-[11px] text-muted-foreground">
        统计窗口 {windowDays} 天。<b>这里的数字是历史分布，不是告警</b>——
        是否跨线由下面「跨线异常」区按确定性阈值判定，且服务健康只看近况
        （所以一个前几天坏过、今天已恢复的 skill 会在这张表里偏高、却不该报警）。
        {rows.some((s) => s.other > 0) && (
          <span className="ml-1">
            另有 {rows.reduce((n, s) => n + s.other, 0)} 次执行的 outcome
            不属于成功／工具失败／转人工三类，已计入执行次数但不进任何比率。
          </span>
        )}
      </div>
    </Card>
  );
}

// N4:评价洞察卡——均分/差评率 + 差评 top 商品与其关键词。`reviews` 目前是
// 可选字段(后端刚补上,老响应体没有这个键时不该崩,走空态)。bad_terms 抽不出
// 就是空数组(词表匹配,宁可留空也不编造),前端据此显示"暂无高频关键词"。
export function ReviewInsightsCard({ reviews, windowDays }:
  { reviews?: ReviewInsights; windowDays: number }) {
  if (!reviews || reviews.total === 0) {
    return (
      <Card className="p-3 text-sm text-muted-foreground" data-testid="reviews-empty">
        过去 {windowDays} 天暂无评价
      </Card>
    );
  }
  return (
    <Card className="p-3 text-sm" data-testid="reviews-card">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
        <div>
          <div className="text-xs text-muted-foreground">评价均分</div>
          <div className="mt-1 text-lg font-semibold">{reviews.avg_rating.toFixed(1)}</div>
        </div>
        <div>
          <div className="text-xs text-muted-foreground">差评率</div>
          <div className="mt-1 text-lg font-semibold">{pct(reviews.bad_rate)}</div>
        </div>
        <div>
          <div className="text-xs text-muted-foreground">评价总数</div>
          <div className="mt-1 text-lg font-semibold">{reviews.total}</div>
        </div>
      </div>
      {reviews.products.length > 0 && (
        <div className="mt-3 flex flex-col gap-2 border-t pt-2">
          <div className="text-xs font-medium text-muted-foreground">差评 top 商品</div>
          {reviews.products.map((p) => (
            <div key={p.sku} className="flex flex-col gap-0.5 text-xs" data-testid={`review-product-${p.sku}`}>
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium text-foreground">{p.name}</span>
                <span className="text-muted-foreground">
                  均分 {p.avg_rating.toFixed(1)} · 差评 {p.bad_count} 条
                </span>
              </div>
              <div className="text-muted-foreground">
                {p.bad_terms.length > 0 ? `高频词：${p.bad_terms.join("、")}` : "暂无高频关键词"}
              </div>
            </div>
          ))}
        </div>
      )}
      <div className="mt-2 text-xs text-muted-foreground">统计窗口 {windowDays} 天</div>
    </Card>
  );
}

export function OperationsView() {
  const [data, setData] = useState<SellerOverview | null>(null);
  const [err, setErr] = useState("");
  // 刷新失败时下方仍是上一次成功拉取的旧数据。店主正是靠这个面板判断某项
  // 异常要不要处理,所以必须显式标出"这是过期数据",不能默默照常渲染
  // (与 SkillsView 同口径:stale 与 data 分开存,stale 为 true 时旧 data 保留不清空)。
  const [stale, setStale] = useState(false);
  const [busy, setBusy] = useState(false);
  const [windowDays, setWindowDays] = useState(7);
  // 请求序号:窗口快速切换/连续刷新时,慢的旧响应不能覆盖快的新响应——
  // 只有仍是"最新一次发起的请求"时,其结果才允许写入 state。
  const reqIdRef = useRef(0);

  // 对话请求独立一套 busy/err:复用页面级 err 会把"参谋这轮答不上来"渲染成
  // "经营数据读取失败",把店主引到完全错误的方向(与 SkillsView 的
  // upErr/dsErr 同一道理)。
  const [chatBusy, setChatBusy] = useState(false);
  const [chatErr, setChatErr] = useState("");
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [draft, setDraft] = useState("");
  // 会话内复用同一个 session_id,不用每轮重生成(参谋侧要能看到"这轮"之前的上下文)。
  const sessionIdRef = useRef(`seller-${Date.now()}`);

  // 「经营诊断」是原有的只读总览+参谋对话;「商机与触达」是 M14 新增的
  // 草稿审批子区,两者拉的数据/操作完全不相关,分 tab 避免把批准/驳回这类
  // 会真实触达买家的按钮和纯只读的经营看板混在同一屏,增加误触风险。
  const [tab, setTab] = useState<"diag" | "growth" | "profile">("diag");

  async function load(days: number) {
    const reqId = ++reqIdRef.current;
    setBusy(true);
    try {
      const d = await getSellerOverview(days);
      if (reqId !== reqIdRef.current) return; // 更新的请求已发出,这次结果作废
      setData(d);
      setErr("");
      setStale(false);
    } catch (e) {
      if (reqId !== reqIdRef.current) return; // 同上:过期请求的失败也不该覆盖当前状态
      // 旧 data 原样保留,只标 stale——绝不能让这次失败悄悄清空或替换成空壳数据。
      setErr(String(e));
      setStale(true);
    } finally {
      if (reqId === reqIdRef.current) setBusy(false);
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
          {tab === "diag" && (
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
          )}
        </div>

        {/* 子页签:「经营诊断」是原有的只读看板+参谋对话,「商机与触达」是
            M14 新增的草稿审批区——后者的批准按钮会真实触达买家,分开成两页
            避免和纯只读的诊断面板混在一屏、增加误触到批准按钮的概率。 */}
        <div className="flex items-center gap-2 border-b pb-2">
          {([
            { key: "diag" as const, label: "经营诊断" },
            { key: "growth" as const, label: "商机与触达" },
            { key: "profile" as const, label: "店铺语气" },
          ]).map((t) => (
            <button
              key={t.key}
              onClick={() => setTab(t.key)}
              className={`rounded px-3 py-1.5 text-sm transition-colors ${
                tab === t.key
                  ? "bg-primary text-primary-foreground"
                  : "bg-secondary text-muted-foreground hover:bg-muted"
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>

        {tab === "growth" && <GrowthPanel />}

        {tab === "profile" && <ShopProfilePanel />}

        {tab === "diag" && (
        <>
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

        {/* 关键指标:每张卡片各自标出自己的统计窗口——卡片一旦被单独截图/挪用/
            滚动出上下文,脱离窗口的数字对店主就没有意义,不能只靠一个共享的
            大标题一次性交代。 */}
        <section>
          <h3 className="mb-2 text-sm font-semibold">关键指标</h3>
          {!data && busy && <div className="text-sm text-muted-foreground">加载中…</div>}
          {!data && !busy && err && <div className="text-sm text-muted-foreground">暂无数据</div>}
          {metrics.length > 0 && (
            <div className={`grid grid-cols-2 gap-3 sm:grid-cols-3 md:grid-cols-5 ${stale ? "opacity-60" : ""}`}>
              {metrics.map((m) => (
                <Card key={m.label} className="p-3" data-testid="metric-card">
                  <div className="text-xs text-muted-foreground">{m.label}</div>
                  <div className="mt-1 text-lg font-semibold">{m.value}</div>
                  {windowLabel && (
                    <div className="mt-1 text-[10px] text-muted-foreground">{windowLabel}</div>
                  )}
                </Card>
              ))}
            </div>
          )}
          {overview?.data_scope && (
            // 数据源口径。**不是免责声明,是防一类具体的错误结论**:实测买家侧有
            // 12 笔订单(经 hmdp 渠道),而这些数字只覆盖 agent 订单库里的 2 笔。
            // 参谋据此得出过「仅 2 笔订单但 119 次客服对话,对话量远超订单量」这样
            // 的"经营异常"——那是渠道口径差异,不是经营事实。店主看不到口径,
            // 只会把 2 当成全店成交。
            <div className="mt-2 rounded-md border border-amber-500/40 bg-amber-500/10 p-2
                            text-[11px] text-amber-700 dark:text-amber-400"
                 data-testid="overview-data-scope">
              {overview.data_scope}
            </div>
          )}
        </section>

        {/* 服务质量:按 skill 的成功率/工具失败率/转人工率。「跨线异常」区只列
            跨了线的,这张表是不跨线也看得见的分布——而且告警的判定窗已收到近况,
            "前几天坏过、今天已恢复"只能靠这张表看到。 */}
        <section>
          <h3 className="mb-2 text-sm font-semibold">服务质量（按 Skill）</h3>
          <div className={stale ? "opacity-60" : ""}>
            {!data && busy && <div className="text-sm text-muted-foreground">加载中…</div>}
            {data && (
              <ServiceQualityTable skills={data.quality?.skills} windowDays={windowDays} />
            )}
          </div>
        </section>

        {/* 情绪分布:三档计数 + 激烈占比,如实摆出统计口径,是否告警看下面的跨线异常区 */}
        <section>
          <h3 className="mb-2 text-sm font-semibold">情绪分布</h3>
          <div className={stale ? "opacity-60" : ""}>
            {!data && busy && <div className="text-sm text-muted-foreground">加载中…</div>}
            {data && (
              <EmotionDistributionCard emotion={data.quality?.emotion} windowDays={windowDays} />
            )}
          </div>
        </section>

        {/* 评价:均分/差评率 + 差评 top 商品与其关键词,如实摆出统计口径,
            是否告警看下面的跨线异常区(bad_review_rate_high)。 */}
        <section>
          <h3 className="mb-2 text-sm font-semibold">评价</h3>
          <div className={stale ? "opacity-60" : ""}>
            {!data && busy && <div className="text-sm text-muted-foreground">加载中…</div>}
            {data && <ReviewInsightsCard reviews={data.reviews} windowDays={windowDays} />}
          </div>
        </section>

        {/* 异常清单:每条都并排给出当前值与告警线,以及跨线幅度对应的颜色 */}
        <section>
          <h3 className="mb-2 text-sm font-semibold">跨线异常（{data?.anomalies.length ?? 0}）</h3>
          <div className={`flex flex-col gap-2 ${stale ? "opacity-60" : ""}`}>
            {!data && busy && <div className="text-sm text-muted-foreground">加载中…</div>}
            {(data?.anomalies || []).map((a, i) => (
              <Card key={`${a.kind}-${a.subject}-${i}`} className="p-3 text-sm" data-testid="anomaly-row">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">{a.subject_name || a.subject}</span>
                  <span className={anomalyTone(a.value, a.threshold)}>
                    当前 {pct(a.value, 2)} · 告警线 {pct(a.threshold, 2)}
                  </span>
                </div>
                {a.detail && Object.keys(a.detail).length > 0 && (() => {
                  const parts = Object.entries(a.detail)
                    .map(([k, v]) => {
                      const rendered = formatDetailValue(k, v);
                      return rendered === null ? null : `${k}:${rendered}`;
                    })
                    .filter((s): s is string => s !== null);
                  return parts.length > 0 ? (
                    <div className="mt-1 text-xs text-muted-foreground">
                      {parts.join(" · ")}
                    </div>
                  ) : null;
                })()}
              </Card>
            ))}
            {data && data.anomalies.length === 0 && (
              <div className="text-sm text-muted-foreground">当前无跨线异常</div>
            )}
            {data && <AnomalyScopeNote scope={data.anomaly_scope} />}
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
        </>
        )}
      </div>
    </div>
  );
}
