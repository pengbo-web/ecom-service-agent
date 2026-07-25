import { useState } from "react";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { login, createUser, setToken } from "@/lib/api";

export function LoginCard({ onLogin }: { onLogin: (uid: string) => void }) {
  const [uid, setUid] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [notFound, setNotFound] = useState(false);

  async function doLogin() {
    const clean = uid.trim();
    if (!clean) return;
    setBusy(true); setErr(null); setNotFound(false);
    try {
      const r = await login(clean);
      setToken(r.token);
      onLogin(r.user_id);
    } catch (e: any) {
      if (e?.status === 404) setNotFound(true);
      else setErr("登录失败，请稍后重试");
    } finally {
      setBusy(false);
    }
  }

  async function doCreate() {
    const clean = uid.trim();
    if (!clean) return;
    setBusy(true); setErr(null);
    try {
      const r = await createUser(clean);
      setToken(r.token);
      onLogin(r.user_id);
    } catch (e: any) {
      setErr(e?.status === 409 ? "用户已存在，请直接登录" : "创建失败，请稍后重试");
      setNotFound(false);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex h-full items-center justify-center bg-background">
      <Card className="w-80">
        <CardHeader>
          <div className="flex items-center gap-2">
            <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-primary text-primary-foreground font-bold">夕</div>
            <div className="text-sm font-semibold">登录小夕</div>
          </div>
        </CardHeader>
        <CardContent className="flex flex-col gap-3">
          <input
            className="h-9 w-full rounded border bg-background px-3 text-sm"
            placeholder="用户ID"
            value={uid}
            onChange={(e) => { setUid(e.target.value); setNotFound(false); setErr(null); }}
            onKeyDown={(e) => { if (e.key === "Enter" && !busy) doLogin(); }}
            autoFocus
          />
          <Button disabled={busy || !uid.trim()} onClick={doLogin}>
            {busy ? "登录中…" : "登录"}
          </Button>
          {notFound && (
            <div className="flex flex-col gap-2 rounded-md bg-secondary/40 px-3 py-2 text-xs">
              <span className="text-muted-foreground">用户不存在</span>
              <Button variant="secondary" size="sm" disabled={busy} onClick={doCreate}>
                创建并登录
              </Button>
            </div>
          )}
          {err && <div className="text-xs text-destructive">{err}</div>}
        </CardContent>
      </Card>
    </div>
  );
}
