import { useEffect, useState } from "react";
import { getSkillsOverview, type SkillsOverview } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { RotateCcw } from "lucide-react";

// 风险档 → 展示文案与配色。null 表示"判不了",必须按需人工复核呈现,
// 绝不能渲染成低危/可自动上线(否则界面会误导操作者放行未经判定的候选)。
const RISK_LABEL: Record<string, { text: string; cls: string }> = {
  high: { text: "高危 · 需人工确认", cls: "bg-red-500/15 text-red-600 dark:text-red-400" },
  medium: { text: "中 · 过门禁后监控", cls: "bg-amber-500/15 text-amber-600 dark:text-amber-400" },
  low: { text: "低 · 可灰度自动上线", cls: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400" },
};
const RISK_UNKNOWN = { text: "未判定 · 需人工复核", cls: "bg-secondary text-muted-foreground" };

function RiskBadge({ risk }: { risk: string | null }) {
  const r = (risk && RISK_LABEL[risk]) || RISK_UNKNOWN;
  return <span className={`rounded px-1.5 py-0.5 text-[11px] ${r.cls}`}>{r.text}</span>;
}

function rate(counts: Record<string, number>): string {
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  if (!total) return "—";
  return (((counts.success || 0) / total) * 100).toFixed(0) + "%";
}

export function SkillsView() {
  const [data, setData] = useState<SkillsOverview | null>(null);
  const [err, setErr] = useState<string>("");

  async function load() {
    try { setData(await getSkillsOverview()); setErr(""); }
    catch (e) { setErr(String(e)); }
  }
  useEffect(() => { load(); }, []);

  return (
    <div className="h-full overflow-y-auto p-6">
      <div className="mx-auto flex max-w-4xl flex-col gap-6">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold">Skill 管理</h2>
          <Button variant="ghost" size="sm" onClick={load}>
            <RotateCcw className="h-3.5 w-3.5" /> 刷新
          </Button>
        </div>
        {err && <div className="text-sm text-destructive">读取失败：{err}</div>}

        {/* 现行技能 */}
        <section>
          <h3 className="mb-2 text-sm font-semibold">现行技能（{data?.live.length ?? 0}）</h3>
          <div className="flex flex-col gap-2">
            {(data?.live || []).map((s) => {
              const counts = data?.traces[s.name];
              return (
                <Card key={s.name} className="p-3">
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-medium">{s.name}</span>
                    {counts && (
                      <span className="text-xs text-muted-foreground">
                        实战成功率 {rate(counts)}（{Object.entries(counts)
                          .map(([k, v]) => `${k}:${v}`).join(" · ")}）
                      </span>
                    )}
                  </div>
                  <div className="mt-1 line-clamp-2 text-xs text-muted-foreground">{s.description}</div>
                </Card>
              );
            })}
            {data && data.live.length === 0 && (
              <div className="text-sm text-muted-foreground">暂无技能</div>
            )}
          </div>
          {data && (
            <div className="mt-1 text-[11px] text-muted-foreground">
              成绩取样：最近 {data.traces_window.limit} 条轨迹 —— {data.traces_window.note}
            </div>
          )}
        </section>

        {/* 待审候选 */}
        <section>
          <h3 className="mb-2 text-sm font-semibold">待审候选（{data?.candidates.length ?? 0}）</h3>
          <div className="flex flex-col gap-2">
            {(data?.candidates || []).map((c) => (
              <Card key={c.name} className="p-3">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">{c.name}</span>
                  <span className="rounded bg-secondary px-1.5 py-0.5 text-[11px] text-muted-foreground">
                    {c.is_improvement ? "改进" : "新建"}
                  </span>
                  <RiskBadge risk={c.risk} />
                  {c.valid
                    ? <span className="text-[11px] text-emerald-600 dark:text-emerald-400">✅ 校验通过</span>
                    : <span className="text-[11px] text-destructive">❌ 校验未过</span>}
                </div>
                {!c.valid && (
                  <div className="mt-1 text-xs text-destructive">{c.errors.join("；")}</div>
                )}
                <div className="mt-1 font-mono text-[11px] text-muted-foreground">{c.path}</div>
              </Card>
            ))}
            {data && data.candidates.length === 0 && (
              <div className="text-sm text-muted-foreground">
                暂无候选（跑离线自进化或从下方上传）
              </div>
            )}
          </div>
        </section>

        {/* 活跃灰度 */}
        <section>
          <h3 className="mb-2 text-sm font-semibold">活跃灰度（{data?.canaries.length ?? 0}）</h3>
          <div className="flex flex-col gap-2">
            {(data?.canaries || []).map((c) => (
              <Card key={c.skill_name} className="p-3 text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium">{c.skill_name}</span>
                  <span className="rounded bg-primary/15 px-1.5 py-0.5 text-[11px] text-primary">
                    分流 {c.percent}%
                  </span>
                  <RiskBadge risk={c.risk} />
                </div>
                <div className="mt-1 text-xs text-muted-foreground">
                  放行方式 {c.policy || "—"} · 开始于 {c.started_at}
                </div>
              </Card>
            ))}
            {data && data.canaries.length === 0 && (
              <div className="text-sm text-muted-foreground">当前没有灰度在跑</div>
            )}
          </div>
        </section>
      </div>
    </div>
  );
}
