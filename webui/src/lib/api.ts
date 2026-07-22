export function getSessionId(): string {
  let s = localStorage.getItem("xiaoxi_sid");
  if (!s) { s = "web-" + Math.random().toString(36).slice(2, 10); localStorage.setItem("xiaoxi_sid", s); }
  return s;
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

export async function consolidateMemory(sessionId: string): Promise<ConsolidateResult> {
  const r = await adminFetch(`/api/session/${sessionId}/consolidate`, { method: "POST" });
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}

export type MemorySnapshot = {
  user_id: string; curation: boolean; count: number;
  facts: LtmFact[]; interaction_summaries: { summary: string; timestamp: string }[];
};

export function getMemory(): Promise<MemorySnapshot> {
  return getJSON<MemorySnapshot>("/api/memory");
}
