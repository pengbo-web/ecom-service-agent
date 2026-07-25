// 会话 ID 由服务端签发(conversation_id = "c-" + uuid4().hex[:16]),前端不再自造。
// 挂载/切用户时调用 openConversation:同用户已有 open 会话则复用,否则服务端新开一个。
export async function openConversation(userId: string): Promise<{ conversation_id: string }> {
  const r = await fetch("/api/conversation/open", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user_id: userId }),
  });
  return r.json();
}

export type ConversationMeta = { conversation_id: string; status: string; created_at: string; close_reason?: string | null };
export async function listConversations(userId: string): Promise<ConversationMeta[]> {
  const r = await fetch(`/api/conversations?user_id=${encodeURIComponent(userId)}`);
  return (await r.json()).conversations;
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
