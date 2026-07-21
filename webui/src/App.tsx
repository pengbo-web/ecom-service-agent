import { useState } from "react";
import { AppShell, type View } from "@/components/AppShell";
import { ChatView } from "@/components/ChatView";
import { DashboardView } from "@/components/DashboardView";
import { SeatView } from "@/components/SeatView";
import { EvalView } from "@/components/EvalView";
import { adminFetch, getSessionId } from "@/lib/api";

export default function App() {
  const [view, setView] = useState<View>(
    typeof location !== "undefined" && location.pathname === "/dashboard" ? "dash" : "chat"
  );
  const [resetKey, setResetKey] = useState(0);
  const sessionId = getSessionId();

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
      {view === "chat" && <ChatView key={resetKey} sessionId={sessionId} />}
      {view === "dash" && <DashboardView sessionId={sessionId} />}
      {view === "seat" && <SeatView sessionId={sessionId} />}
      {view === "eval" && <EvalView />}
    </AppShell>
  );
}
