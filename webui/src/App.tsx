import { useState } from "react";
import { AppShell, type View } from "@/components/AppShell";
import { ChatView } from "@/components/ChatView";
import { DashboardView } from "@/components/DashboardView";
import { SeatView } from "@/components/SeatView";
import { EvalView } from "@/components/EvalView";
import { MemoryView } from "@/components/MemoryView";
import { adminFetch, getSessionId, getUserId, setUserId } from "@/lib/api";

export default function App() {
  const [view, setView] = useState<View>(
    typeof location !== "undefined" && location.pathname === "/dashboard" ? "dash" : "chat"
  );
  const [resetKey, setResetKey] = useState(0);
  const [userId, setUid] = useState<string>(getUserId());
  const sessionId = getSessionId();

  function onUserId(uid: string) {
    const clean = uid.trim() || "default";
    setUserId(clean);
    setUid(clean);
  }

  async function onReset() {
    await adminFetch("/api/session/reset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: sessionId }),
    });
    setResetKey((k) => k + 1);
    setView("chat");
  }

  return (
    <AppShell view={view} onView={setView} onReset={onReset}>
      {view === "chat" && <ChatView key={resetKey + userId} sessionId={sessionId} userId={userId} onUserId={onUserId} />}
      {view === "dash" && <DashboardView sessionId={sessionId} />}
      {view === "seat" && <SeatView sessionId={sessionId} />}
      {view === "eval" && <EvalView />}
      {view === "mem" && <MemoryView sessionId={sessionId} userId={userId} />}
    </AppShell>
  );
}
