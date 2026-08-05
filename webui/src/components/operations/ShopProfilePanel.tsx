import { useEffect, useState } from "react";
import { getShopProfile, putShopProfile, type ShopProfile } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { RotateCcw } from "lucide-react";

const EMPTY: ShopProfile = { shop_name: "", tone: "", banned_words: "" };

/**
 * 店铺人格设定:店主自定义客服的语气/称呼/禁语。
 *
 * 与 SkillsView / OperationsView 同一惯例:读取/保存各自独立的 busy/err 状态,
 * 不共享——否则"保存中"会把这张卡片渲染成"读取失败"之类的错误方向。
 *
 * 语气文本的安全边界不在前端:这里的字数上限只是提前拦截,真正兜底的围栏与
 * "安全规则拼在店主文本之后"发生在后端(app/config/shop_profile.py、
 * app/prompts/agents.py::build_profile_prompt)。前端唯一要讲清楚的是——
 * 保存成功不代表历史对话被改写,只是**下一轮**对话开始生效。
 */
export function ShopProfilePanel() {
  const [profile, setProfile] = useState<ShopProfile>(EMPTY);
  const [defaultTone, setDefaultTone] = useState("");
  const [maxChars, setMaxChars] = useState(600);

  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");

  const [saveBusy, setSaveBusy] = useState(false);
  const [saveErr, setSaveErr] = useState("");
  const [saveMsg, setSaveMsg] = useState("");

  async function load() {
    setBusy(true);
    setErr("");
    try {
      const r = await getShopProfile();
      setProfile(r.profile);
      setDefaultTone(r.default_tone);
      setMaxChars(r.max_tone_chars);
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    load();
  }, []);

  const toneLen = profile.tone.length;
  const overLimit = toneLen > maxChars;

  async function onSave() {
    if (overLimit || saveBusy) return;
    setSaveBusy(true);
    setSaveErr("");
    setSaveMsg("");
    try {
      await putShopProfile(profile);
      setSaveMsg("已保存，下一轮对话开始生效（不会改写已发生的历史对话）。");
    } catch (e) {
      setSaveErr(`保存失败：${String(e)}`);
    } finally {
      setSaveBusy(false);
    }
  }

  // 只把默认语气填回文本框,不直接提交——店主还可以在此基础上再改,
  // 也可以看清楚"默认到底是什么"之后再决定按不按保存。
  function onRestoreDefault() {
    setSaveMsg("");
    setSaveErr("");
    setProfile((p) => ({ ...p, tone: defaultTone }));
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold">店铺语气设定</h3>
        <Button variant="ghost" size="sm" onClick={load} disabled={busy}>
          <RotateCcw className="h-3.5 w-3.5" /> 刷新
        </Button>
      </div>
      <div className="text-xs text-muted-foreground">
        这里设定的语气与称呼会拼进客服每一轮的系统提示，<b>只影响说话方式</b>——
        不代客下单、不许编造数字等安全规则不受影响，任何语气设定都无法豁免。
      </div>

      {err && <div className="text-sm text-destructive">读取失败：{err}</div>}

      <Card className="flex flex-col gap-3 p-4 text-sm">
        <label className="flex flex-col gap-1">
          <span className="text-xs text-muted-foreground">店铺名</span>
          <input
            className="h-8 rounded border bg-background px-2 text-sm"
            value={profile.shop_name}
            disabled={busy}
            onChange={(e) => setProfile((p) => ({ ...p, shop_name: e.target.value }))}
          />
        </label>

        <label className="flex flex-col gap-1">
          <div className="flex items-center justify-between">
            <span className="text-xs text-muted-foreground">语气设定</span>
            <span className={`text-xs ${overLimit ? "text-destructive" : "text-muted-foreground"}`}>
              已用 {toneLen} / {maxChars} 字
            </span>
          </div>
          <textarea
            className="min-h-[120px] rounded border bg-background px-2 py-1.5 text-sm"
            value={profile.tone}
            disabled={busy}
            placeholder="留空 = 使用默认语气"
            onChange={(e) => setProfile((p) => ({ ...p, tone: e.target.value }))}
          />
          {overLimit && (
            <div className="text-xs text-destructive">
              语气设定过长（{toneLen} 字），上限 {maxChars} 字，请精简后再保存。
            </div>
          )}
        </label>

        <label className="flex flex-col gap-1">
          <span className="text-xs text-muted-foreground">禁止使用的措辞（可选，逗号分隔）</span>
          <input
            className="h-8 rounded border bg-background px-2 text-sm"
            value={profile.banned_words}
            disabled={busy}
            onChange={(e) => setProfile((p) => ({ ...p, banned_words: e.target.value }))}
          />
        </label>

        <div className="flex items-center gap-2">
          <Button size="sm" disabled={overLimit || saveBusy || busy} onClick={onSave}>
            {saveBusy ? "保存中…" : "保存"}
          </Button>
          <Button variant="outline" size="sm" disabled={busy} onClick={onRestoreDefault}>
            恢复默认语气
          </Button>
        </div>

        {saveErr && <div className="text-xs text-destructive">⚠️ {saveErr}</div>}
        {saveMsg && (
          <div className="text-xs text-emerald-600 dark:text-emerald-400">✅ {saveMsg}</div>
        )}
      </Card>
    </div>
  );
}
