import { useEffect, useRef, useState } from "react";
import { adminFetch, getJSON } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";

type Summary = { pass_rate?: number; avg_process_score?: number; avg_result_score?: number; total_tokens?: number };
type ReflowCase = { id: string; turns?: string[]; expected_intent?: string; expected_requires_human?: boolean; expected_tools?: string[] };
/** 回流的丢弃披露:少给了东西必须说出来(与 anomaly_scope / degraded 同一条纪律)。 */
type ReflowStats = {
  kept: number; dropped_no_assertion: number;
  dropped_samples?: string[]; note?: string;
};
type Diff = { metric: string; baseline: number; current: number; delta: number; regressed: boolean };
type EvalStatus = {
  status: string;
  error?: string | null;
  result?: { summary: Summary; regression: { regressed: boolean; diffs: Diff[] } | null } | null;
};

const pct = (x?: number) => ((x || 0) * 100).toFixed(0) + "%";

export function EvalView() {
  const [reflow, setReflow] = useState<
    { count: number; cases: ReflowCase[]; stats?: ReflowStats | null } | null>(null);
  const [baseline, setBaseline] = useState<Summary | null>(null);
  const [status, setStatus] = useState<EvalStatus | null>(null);
  const timer = useRef<number | null>(null);

  async function loadBaseline() {
    try { setBaseline(await getJSON<Summary>("/api/eval/baseline")); } catch { /* ignore */ }
  }
  async function poll() {
    try {
      const s = await getJSON<EvalStatus>("/api/eval/status");
      setStatus(s);
      if (s.status === "running") {
        timer.current = window.setTimeout(poll, 3000);
      } else if (s.status === "done") {
        loadBaseline();
      }
    } catch { /* ignore */ }
  }
  useEffect(() => {
    loadBaseline();
    poll();
    return () => { if (timer.current) window.clearTimeout(timer.current); };
    // eslint-disable-next-line
  }, []);

  async function doReflow() {
    setReflow(null);
    const r = await (await adminFetch("/api/reflow", { method: "POST" })).json();
    setReflow(r);
  }
  async function runEval() {
    if (!window.confirm("跑评估会真调大模型、约 1-2 分钟、消耗 token（约 6 万）。确认开始？")) return;
    await adminFetch("/api/eval/run", { method: "POST" });
    poll();
  }

  const running = status?.status === "running";

  return (
    <div className="h-full overflow-y-auto p-6">
      <div className="mx-auto flex max-w-4xl flex-col gap-6">
        {/* 回流 */}
        <div>
          <div className="mb-2 flex items-center justify-between">
            <h2 className="text-lg font-semibold">回流：线上问题 → 候选用例</h2>
            <Button variant="secondary" size="sm" onClick={doReflow}>回流一次</Button>
          </div>
          {reflow === null ? (
            <div className="text-sm text-muted-foreground">点「回流一次」从线上 Trace 生成候选用例（免费、秒回）。</div>
          ) : reflow.count === 0 ? (
            <div className="text-sm text-muted-foreground">暂无问题 Trace（先去聊几句会触发拦截/转人工的消息）。</div>
          ) : (
            <div className="flex flex-col gap-2">
              <div className="text-sm">共回流 <b>{reflow.count}</b> 条候选用例：</div>
              {/* 两句必须说在人采纳之前:
                  ① 期望是**线上实际发生的行为**,不是已验证的正确答案。实测有一条
                     "你们几点上班"的意图被记成 return_request——照抄进回归集就把这个
                     误判固化成了标准答案,而回归集正是 skill 转正门禁的比较基准。
                  ② 丢掉了几条也要说。不说的话,"共回流 N 条"读起来像"线上就这么点问题"。 */}
              <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-2
                              text-[11px] text-amber-700 dark:text-amber-400"
                   data-testid="reflow-caveat">
                ⚠️ 这里的「期望」取自**线上实际发生的行为**，不是已验证的正确答案
                （例如意图可能本身就是一次误判）。采纳进回归集前请逐条人工核对——
                回归集是转正门禁的比较基准，错的期望会把错误固化成标准答案。
                {reflow.stats && reflow.stats.dropped_no_assertion > 0 && (
                  <div className="mt-1" data-testid="reflow-dropped">
                    另有 <b>{reflow.stats.dropped_no_assertion}</b> 条已丢弃：
                    去掉不可复现的期望后一条断言都不剩
                    {reflow.stats.dropped_samples?.length
                      ? `（如「${reflow.stats.dropped_samples.slice(0, 3).join("」「")}」）`
                      : ""}
                    。多为仅因「同一问题重复N次未解决」这种会话级判定升级的轮次，
                    单轮用例复现不了它。
                  </div>
                )}
              </div>
              {reflow.cases.map((c) => (
                <Card key={c.id} className="p-3 text-sm">
                  <div className="font-medium">{c.id}</div>
                  <div className="text-muted-foreground">输入：{(c.turns || []).join(" / ")}</div>
                  <div className="text-muted-foreground">
                    期望：{[
                      c.expected_intent ? "意图=" + c.expected_intent : "",
                      c.expected_requires_human ? "需转人工" : "",
                      c.expected_tools ? "工具=" + c.expected_tools.join(",") : "",
                    ].filter(Boolean).join(" · ") || "(仅固化输入)"}
                  </div>
                </Card>
              ))}
            </div>
          )}
        </div>

        {/* 评估回归门禁 */}
        <div>
          <h2 className="mb-2 text-lg font-semibold">评估回归门禁</h2>
          <Card className="flex flex-col gap-3 p-4">
            <div className="text-sm text-muted-foreground">
              {baseline && Object.keys(baseline).length ? (
                <>基线：通过率 <b>{pct(baseline.pass_rate)}</b> · 过程 {pct(baseline.avg_process_score)} · 结果 {pct(baseline.avg_result_score)} · token {baseline.total_tokens || 0}</>
              ) : (
                <>基线：尚未建立（跑一次评估后自动展示）</>
              )}
            </div>
            <div className="flex items-center gap-2">
              <Button size="sm" onClick={runEval} disabled={running}>▶ 一键跑评估</Button>
              <span className="text-xs text-destructive">⚠️ 会真调大模型、约 1-2 分钟、消耗 token</span>
            </div>
          </Card>

          {running && <div className="mt-3 text-sm text-muted-foreground">⏳ 评估进行中…（在沙箱重跑测试集，请稍候）</div>}
          {status?.status === "error" && <div className="mt-3 text-sm text-destructive">❌ 评估出错：{status.error}</div>}
          {status?.status === "done" && status.result && (
            <div className="mt-3 flex flex-col gap-2">
              <Card className="p-3 text-sm">
                <b>本次结果</b>：通过率 {pct(status.result.summary.pass_rate)} · 过程 {pct(status.result.summary.avg_process_score)} · 结果 {pct(status.result.summary.avg_result_score)} · token {status.result.summary.total_tokens || 0}
              </Card>
              {status.result.regression ? (
                <Card className="p-3 text-sm">
                  <b>回归门禁</b>：{status.result.regression.regressed
                    ? <span className="text-destructive">❌ 检测到质量回退</span>
                    : <span className="text-emerald-600">✅ 未见回退</span>}
                  <div className="mt-1">
                    {status.result.regression.diffs.map((d) => (
                      <div key={d.metric}>
                        {d.metric}: {d.baseline.toFixed(2)} → {d.current.toFixed(2)} (Δ {d.delta >= 0 ? "+" : ""}{d.delta.toFixed(2)}) {d.regressed ? "❌" : "✅"}
                      </div>
                    ))}
                  </div>
                </Card>
              ) : (
                <div className="text-sm text-muted-foreground">（无基线可比，本次结果可作为首个基线参考）</div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
