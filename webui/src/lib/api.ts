function getBaseToken(): string {
  let b = localStorage.getItem("xiaoxi_base");
  if (!b) { b = Math.random().toString(36).slice(2, 10); localStorage.setItem("xiaoxi_base", b); }
  return b;
}
// 会话归属于用户:session_id 带上 user_id,切换用户即切到该用户自己的会话线程(各自历史/上下文)
export function getSessionId(userId: string): string {
  const safe = (userId || "default").replace(/[^a-zA-Z0-9_-]/g, "_");
  return `${safe}--${getBaseToken()}`;
}
export function getUserId(): string {
  return localStorage.getItem("xiaoxi_uid") || "default";
}
export function setUserId(uid: string): void {
  localStorage.setItem("xiaoxi_uid", uid.trim() || "default");
}
export function adminFetch(url: string, opts: RequestInit = {}) {
  const token = localStorage.getItem("admin_token") || "";
  opts.headers = { ...(opts.headers || {}), ...(token ? { "X-Admin-Token": token } : {}) };
  return fetch(url, opts);
}
export async function getJSON<T>(url: string): Promise<T> {
  const r = await adminFetch(url);
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}

export type LtmFact = { content: string; category: string; created_at: string };
export type ConsolidateResult = { enabled: boolean; curation?: boolean; count: number; facts: LtmFact[] };

export async function consolidateMemory(sessionId: string, userId: string): Promise<ConsolidateResult> {
  const r = await adminFetch(`/api/session/${sessionId}/consolidate?user_id=${encodeURIComponent(userId)}`, { method: "POST" });
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}

export type HistoryTurn = { role: "user" | "assistant"; content: string };
export async function getHistory(sessionId: string): Promise<HistoryTurn[]> {
  const r = await fetch(`/api/session/${sessionId}/history`);
  if (!r.ok) return [];
  return (await r.json()).turns || [];
}

export type MemorySnapshot = {
  user_id: string; curation: boolean; count: number;
  facts: LtmFact[]; interaction_summaries: { summary: string; timestamp: string }[];
};

export function getMemory(userId: string): Promise<MemorySnapshot> {
  return getJSON<MemorySnapshot>(`/api/memory?user_id=${encodeURIComponent(userId)}`);
}
