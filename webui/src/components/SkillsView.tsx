import { useEffect, useState } from "react";
import { distillSkillFromDoc, getSkillsOverview, uploadSkillBundle,
  type SkillDistillResult, type SkillUploadResult, type SkillsOverview } from "@/lib/api";
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
  const [upResult, setUpResult] = useState<SkillUploadResult | null>(null);
  // 上传失败要单独展示在上传卡片里:复用顶部那个 err 会渲染成"读取失败",
  // 指向的是总览拉取失败,把人引到完全错误的方向。
  const [upErr, setUpErr] = useState("");
  // 上传与提炼各自一个 busy:共用一个会让"提炼中"把上传卡片也渲染成「上传中…」
  // (反之亦然),操作者会以为自己触发了一个根本没发生的动作。
  const [upBusy, setUpBusy] = useState(false);
  const [dsBusy, setDsBusy] = useState(false);
  // 刷新失败时下方仍是上一次成功拉取的旧数据。操作者正是靠这个面板判断某个
  // high 风险候选要不要处理,所以必须显式标出"这是过期数据",不能默默照常渲染。
  const [stale, setStale] = useState(false);
  const [doc, setDoc] = useState("");
  const [dsResult, setDsResult] = useState<SkillDistillResult | null>(null);
  // 蒸馏失败要单独展示在蒸馏卡片里:复用顶部那个 err 会渲染成"读取失败",
  // 指向的是总览拉取失败,把人引到完全错误的方向(与上传卡片的 upErr 同一道理)。
  const [dsErr, setDsErr] = useState("");

  async function onDistill() {
    if (!doc.trim()) return;
    // 这一步会真调大模型、花钱,必须先让人确认(与评估页同口径)
    if (!window.confirm("提炼会真调大模型、消耗 token。确认开始？")) return;
    setDsBusy(true);
    setDsResult(null);
    setDsErr("");
    try {
      const result = await distillSkillFromDoc(doc);
      setDsResult(result);
      if (result.created) await load();
    } catch (e) {
      setDsErr(`提炼请求失败（网络、鉴权或体积超限）：${String(e)}`);
    } finally {
      setDsBusy(false);
    }
  }

  async function onPickDocFile(file: File | null) {
    if (!file) return;
    setDoc(await file.text());
  }

  async function onPickBundle(file: File | null, input?: HTMLInputElement) {
    if (!file) return;
    setUpBusy(true);
    setUpResult(null);
    setUpErr("");
    try {
      const result = await uploadSkillBundle(file);
      setUpResult(result);
      if (result.accepted) await load();   // 候选列表刷新出新条目
    } catch (e) {
      // 校验不通过走的是 200 + accepted:false,能进这里的是网络/鉴权/体积超限
      setUpErr(`上传请求失败（网络、鉴权或体积超限）：${String(e)}`);
    } finally {
      setUpBusy(false);
      // 清空 input:否则改好文件后再选**同名**文件不会触发 onChange,界面像没反应
      if (input) input.value = "";
    }
  }

  async function load() {
    try { setData(await getSkillsOverview()); setErr(""); setStale(false); }
    catch (e) { setErr(String(e)); setStale(true); }
  }

  // 手动刷新要顺带清掉上一轮的上传/提炼结论:否则一条来自上次上传的红色
  // 「未通过」会一直挂在那里,被误读成本次动作的结果。
  function onRefresh() {
    setUpResult(null);
    setUpErr("");
    setDsResult(null);
    setDsErr("");
    load();
  }

  useEffect(() => { load(); }, []);

  return (
    <div className="h-full overflow-y-auto p-6">
      <div className="mx-auto flex max-w-4xl flex-col gap-6">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <h2 className="text-lg font-semibold">Skill 管理</h2>
            {stale && data && (
              <span className="rounded bg-amber-500/15 px-1.5 py-0.5 text-[11px]
                               text-amber-700 dark:text-amber-400">数据已过期</span>
            )}
          </div>
          <Button variant="ghost" size="sm" onClick={onRefresh}>
            <RotateCcw className="h-3.5 w-3.5" /> 刷新
          </Button>
        </div>
        {err && <div className="text-sm text-destructive">读取失败：{err}</div>}
        {/* 刷新失败但下方仍有旧数据:必须明说这是过期快照。操作者靠这个面板
            决定高危候选要不要处理,拿旧数据当现状会直接做错决定。 */}
        {stale && data && (
          <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-2
                          text-xs text-amber-700 dark:text-amber-400">
            ⚠️ 以下内容是<b>上次成功刷新时的旧数据</b>（本次刷新失败，最新状态未知）。
            请勿据此判断候选或灰度的当前状况，先排除上面的读取失败再操作。
          </div>
        )}

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

        {/* 上传技能包 */}
        <section>
          <h3 className="mb-2 text-sm font-semibold">上传技能包</h3>
          <Card className="flex flex-col gap-2 p-4 text-sm">
            <div className="text-muted-foreground">
              技能是一个<b>目录</b>（<code>SKILL.md</code> + 可选的 <code>references/</code> 参考资料），
              所以用 <b>.zip</b> 上传；只有一份说明时也可直接选 <code>.md</code>。
              上传只会落到<b>待审候选</b>，并跑与自动生成候选相同的校验与风险分级，<b>不会直接上线</b>。
            </div>
            <input type="file" accept=".zip,.md,.markdown,.txt" disabled={upBusy}
              onChange={(e) => onPickBundle(e.target.files?.[0] || null, e.currentTarget)}
              className="text-xs" />
            {upBusy && <div className="text-xs text-muted-foreground">上传中…</div>}
            {upErr && <div className="text-xs text-destructive">⚠️ {upErr}</div>}
            {upResult && (upResult.accepted ? (
              <div className="text-xs text-emerald-600 dark:text-emerald-400">
                ✅ 已收为候选 <b>{upResult.name}</b>
                {upResult.replaced ? "（覆盖了同名旧候选）" : ""} · 风险 {upResult.risk} ·
                放行 {upResult.policy}
                {upResult.files.length > 0 && <> · 附带 {upResult.files.length} 份资料</>}
              </div>
            ) : (
              <div className="text-xs text-destructive">
                ❌ 未通过：{upResult.errors.join("；")}
              </div>
            ))}
          </Card>
        </section>

        {/* 上传 SOP/资料 → 提炼技能 */}
        <section>
          <h3 className="mb-2 text-sm font-semibold">上传客服 SOP / 产品资料 → 提炼技能</h3>
          <Card className="flex flex-col gap-2 p-4 text-sm">
            <div className="text-muted-foreground">
              把服务规则或产品说明贴进来（或选文件），由大模型提炼成步骤化技能。
              产物同样<b>只落待审候选</b>并带风险档；碰钱/承诺类会被判高危、强制人工确认。
            </div>
            <input type="file" accept=".md,.markdown,.txt" disabled={dsBusy}
              onChange={(e) => onPickDocFile(e.target.files?.[0] || null)}
              className="text-xs" />
            <textarea value={doc} onChange={(e) => setDoc(e.target.value)} disabled={dsBusy}
              rows={6} placeholder="粘贴客服 SOP 或产品资料正文…"
              className="w-full rounded-md border bg-background p-2 text-xs outline-none" />
            <div className="flex items-center gap-2">
              <Button size="sm" onClick={onDistill} disabled={dsBusy || !doc.trim()}>
                ▶ 提炼成候选技能
              </Button>
              <span className="text-[11px] text-destructive">⚠️ 会真调大模型、消耗 token</span>
            </div>
            {dsBusy && <div className="text-xs text-muted-foreground">提炼中…</div>}
            {dsErr && <div className="text-xs text-destructive">⚠️ {dsErr}</div>}
            {dsResult?.truncated && (
              <div className="text-xs text-amber-600 dark:text-amber-400">
                ⚠️ 资料超过 12000 字，仅前 12000 字参与了蒸馏，建议拆分后分次蒸馏
              </div>
            )}
            {dsResult && (dsResult.created ? (
              <div className="text-xs text-emerald-600 dark:text-emerald-400">
                ✅ 已提炼出候选 <b>{dsResult.name}</b> · 风险 {dsResult.risk} · 放行 {dsResult.policy}
              </div>
            ) : (
              <div className="text-xs text-destructive">❌ {dsResult.errors.join("；")}</div>
            ))}
          </Card>
        </section>
      </div>
    </div>
  );
}
