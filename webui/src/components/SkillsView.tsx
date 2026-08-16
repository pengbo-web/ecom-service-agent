import { useEffect, useState } from "react";
import { countLabel } from "@/lib/count";
import { distillSkillFromDoc, getSkillsOverview, uploadSkillBundle,
  promoteSkill, rejectSkill, rollbackSkill,
  type SkillCandidate, type SkillDistillResult, type SkillUploadResult,
  type SkillsOverview } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { SkillContentViewer } from "@/components/skills/SkillContentViewer";
import { RotateCcw } from "lucide-react";

// 风险档 → 展示文案与配色。null 表示"判不了",必须按需人工复核呈现,
// 绝不能渲染成低危/可自动上线(否则界面会误导操作者放行未经判定的候选)。
const RISK_LABEL: Record<string, { text: string; cls: string }> = {
  high: { text: "高危 · 需人工确认", cls: "bg-red-500/15 text-red-600 dark:text-red-400" },
  medium: { text: "中 · 过门禁后监控", cls: "bg-amber-500/15 text-amber-600 dark:text-amber-400" },
  low: { text: "低 · 可灰度自动上线", cls: "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400" },
};
const RISK_UNKNOWN = { text: "未判定 · 需人工复核", cls: "bg-secondary text-muted-foreground" };

/** 门禁就绪度:这个候选交给**无人值守的看门狗**会怎样。
 *
 * 为什么必须显示:实测 7 个候选里 6 个的评测集用例数是 0。它们不是"排队等看门狗
 * 处理",而是**每一轮 `--start-all` 都会打同一行 gate_unavailable、永远如此**——
 * 新建 skill 走 gate_then_watch,那条路要求先过离线门禁,而没有任何机制会为新
 * skill 产用例。运维看着这个待审列表,会以为自动化在推进;实际只有人在这里点
 * force 才动得了。这句话不显示出来,那个误解就没有出口。
 *
 * 措辞刻意把"门禁评不了"和"候选不达标"分开:后者该改候选或驳回,前者候选可能
 * 一点问题都没有(实测 invoice-issuance 校验全过、引用的工具真实存在)。
 */
function GateReadiness({ c }: { c: SkillCandidate }) {
  if (c.gate_evaluable === null || c.gate_cases === null) return null;
  if (c.gate_evaluable === false) {
    return (
      <div className="mt-1 text-[11px] text-amber-700 dark:text-amber-400"
           data-testid={`gate-unavailable-${c.name}`}>
        ⚠️ 离线门禁评不了这个候选（评测集 0 条用例点名它）——候选本身未被否证，
        但自动化不会上线它，只能在此人工放行
      </div>
    );
  }
  if (c.gate_underpowered) {
    return (
      <div className="mt-1 text-[11px] text-amber-700 dark:text-amber-400"
           data-testid={`gate-underpowered-${c.name}`}>
        ⚠️ 门禁仅 {c.gate_cases} 条用例，结论以运行噪声为主，证据强度不足
        <SyntheticNote c={c} />
      </div>
    );
  }
  return (
    <div className="mt-1 text-[11px] text-muted-foreground"
         data-testid={`gate-ready-${c.name}`}>
      门禁用例 {c.gate_cases} 条
      <SyntheticNote c={c} />
    </div>
  );
}

/** 这些用例里有多少是**机器造的、没有人看过一眼的**。
 *
 * 自动合成解开了"新 skill 永远转不了正"的死结,代价是引入了一种新的骗法:
 * 一个「门禁用例 5 条」的候选,如果那 5 条全是从真实会话自动合成的,它与 5 条
 * 人工用例在证据强度上完全不是一回事——而界面上长得一模一样。
 *
 * 所以这一行是**必须**的,不是锦上添花:它是操作者判断"这个绿灯值多少钱"的
 * 唯一依据。旧字段缺失(后端未升级)时返回 null,不猜、不显示。
 */
function SyntheticNote({ c }: { c: SkillCandidate }) {
  const n = c.gate_synthetic_cases;
  if (n === null || n === undefined || n <= 0) return null;
  const human = c.gate_human_cases ?? 0;
  return (
    <div className="mt-0.5 text-amber-700 dark:text-amber-400"
         data-testid={`gate-synthetic-${c.name}`}>
      其中 {n} 条自动合成 · 未经人工审核
      {human > 0 ? `（人工 ${human} 条）` : "（无任何人工用例）"}
    </div>
  );
}

/** 这个 skill 的失败里,有多少**根本不该算在它头上**。
 *
 * 光看成功率会得出完全错误的结论:实测 track-order 的 success 15 / tool_error 38
 * 读起来是"成功率 28%,这 skill 很烂",而 41 条失败里 37 条是**归属校验正确地
 * 拦住了跨用户访问**——安全机制在按设计工作,不是 skill 缺知识。少了这一行,
 * 运营看着成功率会去改一份一个字都没写错的流程文档。
 *
 * `undetermined` 照常显示,不因为"不好看"而藏起来:它大起来说明判据不够用了。
 */
const ATTR_LABEL: Record<string, { text: string; cls: string }> = {
  knowledge_gap: { text: "缺知识", cls: "text-red-600 dark:text-red-400" },
  capability_limit: { text: "权限/依赖边界", cls: "text-muted-foreground" },
  evaluation_noise: { text: "非有效证据", cls: "text-muted-foreground" },
  undetermined: { text: "判不出", cls: "text-amber-700 dark:text-amber-400" },
};

function FailureAttribution({ counts, name }:
  { counts?: Record<string, number>; name: string }) {
  if (!counts) return null;
  const total = Object.values(counts).reduce((a, b) => a + b, 0);
  if (!total) return null;
  const gap = counts.knowledge_gap || 0;
  return (
    <div className="mt-1 text-[11px]" data-testid={`failure-attr-${name}`}>
      <span className="text-muted-foreground">{total} 次失败归因：</span>
      {["knowledge_gap", "capability_limit", "evaluation_noise", "undetermined"]
        .filter((k) => counts[k])
        .map((k) => (
          <span key={k} className={`ml-1.5 ${ATTR_LABEL[k].cls}`}>
            {ATTR_LABEL[k].text} {counts[k]}
          </span>
        ))}
      {gap === 0 && (
        <span className="ml-1.5 text-emerald-700 dark:text-emerald-400">
          · 无一条指向 skill 本身，改流程文档解决不了这些失败
        </span>
      )}
    </div>
  );
}

/** 这个成功率是**谁打出来的**。
 *
 * 实测 976 轮轨迹里真实买家只有 62 轮，其余是测试固定用户、压测、评测沙箱、
 * 人工走查 —— 而在 `source` 字段落地之前，表里没有任何东西能把它们分开。
 * 一个不带口径的「实战成功率 28%」，读的人无从判断它讲的是线上还是压测，
 * 而看门狗恰恰拿同一批数据做自动回滚判定。
 *
 * 真实流量占比越低，这个数字越不该被当作线上指标读 —— 所以低占比时给警示色。
 * 后端没给这个字段（老版本）时整行不显示，不猜。
 */
function TrafficScope({ all, liveMap, name }:
  { all: Record<string, number>;
    liveMap?: Record<string, Record<string, number>>; name: string }) {
  // 判据是**整张表在不在**，不是这个 skill 的条目在不在。
  // 二者混为一谈会让"这个 skill 真实流量为 0"被当成"后端没给字段"而整行不显示——
  // 而那恰恰是最该显示的一种情况(成功率完全由压测/走查构成)。
  if (!liveMap) return null;
  const total = Object.values(all).reduce((a, b) => a + b, 0);
  const liveTotal = Object.values(liveMap[name] || {}).reduce((a, b) => a + b, 0);
  if (!total) return null;
  const risky = liveTotal === 0 || liveTotal / total < 0.5;
  return (
    <span className={`ml-1.5 ${risky ? "text-amber-700 dark:text-amber-400" : ""}`}
          data-testid={`traffic-scope-${name}`}>
      {liveTotal === 0
        ? "· 其中真实流量 0 轮，这个数字不能当线上指标读"
        : `· 其中真实流量 ${liveTotal} 轮`}
    </span>
  );
}

/** 全库轨迹的来源构成。放在成绩取样那一行，说明整页数字的底子是什么。 */
const SOURCE_LABEL: Record<string, string> = {
  live: "真实", loadtest: "压测", eval: "评测",
  simulated: "模拟", dev: "走查", unknown: "未标注",
};

function TrafficSources({ sources }: { sources?: Record<string, number> }) {
  if (!sources || !Object.keys(sources).length) return null;
  const total = Object.values(sources).reduce((a, b) => a + b, 0);
  const live = sources.live || 0;
  return (
    <span data-testid="trace-sources">
      ｜来源：{Object.entries(sources)
        .sort((a, b) => b[1] - a[1])
        .map(([k, v]) => `${SOURCE_LABEL[k] || k} ${v}`)
        .join(" · ")}
      {live === 0 && total > 0 && (
        <span className="ml-1 text-amber-700 dark:text-amber-400">
          （尚无标记为真实流量的轨迹，看门狗的自动转正/回滚因此不会动作）
        </span>
      )}
    </span>
  );
}

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
  // 转正/驳回/回滚:**按 skill 名分别记 busy 与结果**。用单个标量会让同时操作
  // 两个候选时互相覆盖——后完成的那次把标量清空,会连带把还在途中的那一行按钮
  // 重新点亮,等于放行一次仍在进行的转正(与 GrowthPanel 的 busyIds 同一道理)。
  const [actBusy, setActBusy] = useState<Set<string>>(new Set());
  const [actErr, setActErr] = useState<Record<string, string>>({});
  const [actOk, setActOk] = useState<Record<string, string>>({});
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

  function setRowBusy(name: string, busy: boolean) {
    setActBusy((prev) => {
      const next = new Set(prev);
      if (busy) next.add(name); else next.delete(name);
      return next;
    });
  }

  /** 跑一次行操作,把成败按 skill 名记在该行,不冒泡成页面级错误。 */
  async function runAct(name: string, fn: () => Promise<string>) {
    setRowBusy(name, true);
    setActErr((m) => { const n = { ...m }; delete n[name]; return n; });
    setActOk((m) => { const n = { ...m }; delete n[name]; return n; });
    try {
      const msg = await fn();
      setActOk((m) => ({ ...m, [name]: msg }));
      await load();
    } catch (e) {
      setActErr((m) => ({ ...m, [name]: String(e) }));
    } finally {
      setRowBusy(name, false);
    }
  }

  // 转正会把这份正文**立刻**装到线上,客服下一轮就按它说话——必须二次确认。
  // 高危档要在确认文案里说清风险来源,而不是只说"确认吗"。
  async function onPromote(c: SkillCandidate) {
    const isHigh = c.risk === "high" || c.policy === "manual";
    const tip = isHigh
      ? `【高危候选】${c.name}\n\n风险档：${c.risk ?? "未判定"}（放行策略：${c.policy ?? "未知"}）\n`
        + "高危通常意味着正文里含动钱/承诺类指令，转正后客服会立刻按它执行。\n\n"
      : `${c.name}\n\n`;
    // 门禁默认不跑(会真跑两轮评测、一两分钟且花钱),所以必须把"未经门禁"这件事
    // 摆在人点下去之前,而不是让后端拒绝之后再解释。
    // 门禁"评不了"和"图快不跑"是两回事,确认框里必须分开说:前者人工放行是**唯一**
    // 通路(补用例之前,自动化永远不会上线它),后者只是本次为了不让人干等。
    // 把两种情形说成同一句"本次不跑门禁",会让人以为等看门狗跑就行——而它不会。
    const gateNote = c.gate_evaluable === false
      ? "该候选的离线门禁**评不了**（评测集里 0 条用例点名它）：候选本身未被否证，"
        + "但在补上用例之前，无人值守的自动化永远不会上线它——人工放行是唯一通路。\n"
      : "注意：为避免长时间等待，本次**不跑评测门禁**（门禁会真跑两轮评测、耗时数分钟并消耗 token）。\n";
    if (!window.confirm(
      tip + "转正将立即上线这份技能正文。\n"
      + gateNote
      + "确认在未经门禁的前提下放行？"
    )) return;
    await runAct(c.name, async () => {
      // force=true 才能在未跑门禁时放行(后端对 gate=None 是 fail-closed 的)。
      // 这不是"绕过安全检查":校验、风险判档、备份全都照走,force 只放行评测门禁,
      // 而操作者刚刚在确认框里明确接受了这一点。
      let r = await promoteSkill(c.name, { force: true });

      // 事实一致性双锚点拦下来了:候选相对**生产基线**丢了硬事实(政策数字、
      // 工具引用)。这不是"候选质量不行"那种笼统结论——它能精确说出丢了哪几条,
      // 所以把那几条摆给操作者看,由人判断是不是有意删的。
      //
      // 这里刻意**不复用** force:那个开关的含义是"我知道没跑门禁",而这一次
      // 要征求的是另一个知情同意——"我知道我在删掉这几条硬事实"。
      const fc = r.fact_check;
      if (!r.promoted && fc && !fc.ok) {
        const lost = fc.lost_since_baseline.join("、");
        const thisRound = fc.lost_since_previous.length
          ? `\n本轮删除：${fc.lost_since_previous.join("、")}` : "";
        const earlier = fc.lost_before_this_round.length
          ? `\n更早的轮次已丢失（不是这份候选造成的）：${fc.lost_before_this_round.join("、")}`
          : "";
        if (!window.confirm(
          `${c.name} 相对生产基线丢失了硬事实：\n\n${lost}${thisRound}${earlier}\n\n`
          + `基线来源：${fc.baseline_source ?? "未知"}\n\n`
          + "这类内容（退货天数、金额、工具引用）客服会照着它对用户做承诺。\n"
          + "确认这些是**有意删除**，仍然上线？"
        )) return "已取消（未上线）";
        r = await promoteSkill(c.name, { force: true, allowFactLoss: true });
      }

      if (!r.promoted) return `未上线：${r.reason ?? "原因未知"}`;
      return `已上线${r.risk ? `（风险档 ${r.risk}）` : ""}`
        + `${r.backup ? `，旧版本已备份到 ${r.backup}` : ""}`;
    });
  }

  async function onReject(c: SkillCandidate) {
    // 驳回不上线任何东西,后果也可逆(候选归档进 _rejected/,不删),所以不做
    // 二次确认——与 GrowthPanel 里"批准要确认、驳回不用"同一条取舍。
    await runAct(c.name, async () => {
      const r = await rejectSkill(c.name);
      return `已驳回，候选归档到 ${r.archived_to}`;
    });
  }

  async function onRollback(name: string) {
    if (!window.confirm(
      `${name}\n\n回滚会把线上技能恢复到最近一次备份（即上一次转正前的版本）。\n确认回滚？`
    )) return;
    await runAct(name, async () => {
      await rollbackSkill(name);
      return "已回滚到上一版";
    });
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
          <h3 className="mb-2 text-sm font-semibold">现行技能（{countLabel(data?.live.length)}）</h3>
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
                        <TrafficScope all={counts}
                                      liveMap={data?.traces_live}
                                      name={s.name} />
                      </span>
                    )}
                  </div>
                  <FailureAttribution counts={data?.failure_attribution?.[s.name]}
                                      name={s.name} />
                  <div className="mt-1 line-clamp-2 text-xs text-muted-foreground">{s.description}</div>
                  <SkillContentViewer name={s.name} variant="live" />
                  {/* 回滚是转正的对偶动作:没有它,「一键转正」就是一个**没有退路**
                      的按钮——而技能正文直接决定客服说什么,上线后发现不对必须能
                      立刻退回去,不该要求运营去登服务器。 */}
                  <div className="mt-2 flex items-center gap-2">
                    <Button size="sm" variant="ghost" disabled={actBusy.has(s.name)}
                            onClick={() => onRollback(s.name)}
                            data-testid={`rollback-${s.name}`}>
                      {actBusy.has(s.name) ? "处理中…" : "回滚到上一版"}
                    </Button>
                  </div>
                  {actErr[s.name] && (
                    <div role="alert" className="mt-2 text-xs text-destructive"
                         data-testid={`act-err-${s.name}`}>⚠️ {actErr[s.name]}</div>
                  )}
                  {actOk[s.name] && (
                    <div className="mt-2 text-xs text-emerald-700 dark:text-emerald-400"
                         data-testid={`act-ok-${s.name}`}>✅ {actOk[s.name]}</div>
                  )}
                </Card>
              );
            })}
            {data && data.live.length === 0 && (
              <div className="text-sm text-muted-foreground">暂无技能</div>
            )}
          </div>
          {data && (
            <div className="mt-1 text-[11px] text-muted-foreground">
              {/* limit=0 表示后端不再截窗口(全时段聚合)。写死"最近 N 条"会在
                  N=0 时渲染成「最近 0 条轨迹」——一句正好相反的话。 */}
              成绩取样：{data.traces_window.limit > 0
                ? `最近 ${data.traces_window.limit} 条轨迹`
                : "全部轨迹"} —— {data.traces_window.note}
              <TrafficSources sources={data.trace_sources} />
            </div>
          )}
        </section>

        {/* 待审候选 */}
        <section>
          <h3 className="mb-2 text-sm font-semibold">待审候选（{countLabel(data?.candidates.length)}）</h3>
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
                <GateReadiness c={c} />
                <div className="mt-1 font-mono text-[11px] text-muted-foreground">{c.path}</div>

                {/* 候选这一栏比现行技能更需要它:下面那个「转正上线」会立刻把这份
                    正文推给线上会话,而在此之前页面上关于内容只有一句 description。
                    风险档、校验结论、门禁用例数全都齐了,唯独缺"它到底写了什么"。 */}
                <SkillContentViewer name={c.name} variant="candidate" />

                {/* 转正/驳回:改造前这两个动作只能登进服务器敲 CLI,7 步自进化闭环
                    因此断在最后一环。校验未过的候选不给转正按钮——转正会当场被
                    后端校验拦下,给一个必然失败的按钮只是让人白点一次。 */}
                <div className="mt-3 flex flex-wrap items-center gap-2">
                  {c.valid && (
                    <Button size="sm" disabled={actBusy.has(c.name)}
                            onClick={() => onPromote(c)}
                            data-testid={`promote-${c.name}`}>
                      {actBusy.has(c.name) ? "处理中…" : "转正上线"}
                    </Button>
                  )}
                  <Button size="sm" variant="outline" disabled={actBusy.has(c.name)}
                          onClick={() => onReject(c)}
                          data-testid={`reject-${c.name}`}>
                    驳回
                  </Button>
                  {c.policy && (
                    <span className="text-[11px] text-muted-foreground">
                      放行策略：{c.policy}
                    </span>
                  )}
                </div>
                {actErr[c.name] && (
                  <div role="alert" className="mt-2 rounded-md border border-destructive
                                               bg-destructive/10 p-2 text-xs text-destructive"
                       data-testid={`act-err-${c.name}`}>
                    ⚠️ {actErr[c.name]}
                  </div>
                )}
                {actOk[c.name] && (
                  <div className="mt-2 text-xs text-emerald-700 dark:text-emerald-400"
                       data-testid={`act-ok-${c.name}`}>
                    ✅ {actOk[c.name]}
                  </div>
                )}
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
          <h3 className="mb-2 text-sm font-semibold">活跃灰度（{countLabel(data?.canaries.length)}）</h3>
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
                {dsResult.attempts === 2 && (
                  <span className="ml-1 text-muted-foreground">
                    （首次产物引用了本店没有的工具，已自动修正后重新生成）
                  </span>
                )}
              </div>
            ) : (
              // 失败要**可行动**。改造前后端只回一句三选一的「frontmatter 不全 /
              // 工具名不实 / 名字非法」,店主既不知道是哪一种也不知道该改什么;
              // 而实测真因往往只是资料里写了一个本店没有的工具名。
              <div className="text-xs" data-testid="distill-error">
                <div className="text-destructive">❌ {dsResult.errors.join("；")}</div>
                {(dsResult.unknown_tools?.length ?? 0) > 0
                  && (dsResult.available_tools?.length ?? 0) > 0 && (
                  <details className="mt-1">
                    <summary className="cursor-pointer text-muted-foreground">
                      查看本店可用工具（{dsResult.available_tools!.length} 个）
                    </summary>
                    <div className="mt-1 flex flex-wrap gap-1">
                      {dsResult.available_tools!.map((t) => (
                        <code key={t} className="rounded bg-muted px-1 py-0.5 text-[10px]">{t}</code>
                      ))}
                    </div>
                  </details>
                )}
                {dsResult.attempts === 2 && (
                  <div className="mt-1 text-muted-foreground">
                    已自动把错误原因回喂模型重试过一次，仍未通过——请按上面的提示改一下资料再试。
                  </div>
                )}
              </div>
            ))}
          </Card>
        </section>
      </div>
    </div>
  );
}
