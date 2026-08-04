// 会话 ID 由服务端签发(conversation_id = "c-" + uuid4().hex[:16]),前端不再自造。
// 挂载/切用户时调用 openConversation:同用户已有 open 会话则复用,否则服务端新开一个。

// ---- 登录态:token 存取 + 统一携带 ----
const TOKEN_KEY = "xiaoxi_token";
export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) || "";
}
export function setToken(t: string): void {
  localStorage.setItem(TOKEN_KEY, t);
}
export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY);
}
export function authHeaders(): Record<string, string> {
  const t = getToken();
  return t ? { Authorization: `Bearer ${t}` } : {};
}

export type LoginResult = { user_id: string; name: string; token: string; expires_in: number };
export async function login(userId: string): Promise<LoginResult> {
  const r = await fetch("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user_id: userId }),
  });
  if (!r.ok) throw Object.assign(new Error("login"), { status: r.status });
  return r.json();
}
export async function createUser(userId: string, name?: string): Promise<LoginResult> {
  const r = await fetch("/api/users", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user_id: userId, name: name || userId }),
  });
  if (!r.ok) throw Object.assign(new Error("create"), { status: r.status });
  return r.json();
}
export async function me(): Promise<{ user_id: string; name: string } | null> {
  const r = await fetch("/api/auth/me", { headers: authHeaders() });
  return r.ok ? r.json() : null;
}

// 商城:hmdp 商品列表(供商品卡渲染 + "咨询"带 item 进聊天)
export type Product = { id: string; title: string; price: number; stock: number; image: string; description: string };
export async function getProducts(keyword = ""): Promise<Product[]> {
  try {
    const r = await fetch(`/api/products?keyword=${encodeURIComponent(keyword)}`);
    return r.ok ? (await r.json()).products as Product[] : [];
  } catch {
    return [];
  }
}
export async function getProduct(itemId: string): Promise<Product | null> {
  try {
    const r = await fetch(`/api/product/${encodeURIComponent(itemId)}`);
    return r.ok ? ((await r.json()).product as Product | null) : null;
  } catch {
    return null;
  }
}

// ---- 自助下单:商品卡/商城点『立即购买』→ 建单;"我的订单"页拉列表 ----
export type OrderItem = { name: string; sku: string; quantity: number; price: number };
export type MyOrder = {
  order_id: string; status: string; status_label: string;
  items: OrderItem[]; total: number; created_at: string; shipping_address: string;
};
export async function createOrder(itemId: string, quantity = 1): Promise<{ success: boolean; order_id: string; status_label: string; total: number }> {
  const r = await fetch("/api/order", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ item_id: itemId, quantity }),
  });
  if (!r.ok) throw Object.assign(new Error("order"), { status: r.status });
  return r.json();
}
export async function getMyOrders(): Promise<MyOrder[]> {
  const r = await fetch("/api/orders", { headers: authHeaders() });
  return r.ok ? (await r.json()).orders as MyOrder[] : [];
}

// demo 一键体验:后端开 DEMO_MODE 时,前端自动登录 demo_user_id、跳过登录卡片。
export async function getConfig(): Promise<{ demo_mode: boolean; demo_user_id: string }> {
  try {
    const r = await fetch("/api/config");
    return r.ok ? r.json() : { demo_mode: false, demo_user_id: "" };
  } catch {
    return { demo_mode: false, demo_user_id: "" };
  }
}

export async function openConversation(userId: string): Promise<{ conversation_id: string }> {
  const r = await fetch("/api/conversation/open", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ user_id: userId }),
  });
  return r.json();
}

export type ConversationMeta = { conversation_id: string; status: string; created_at: string; close_reason?: string | null };
export async function listConversations(userId: string): Promise<ConversationMeta[]> {
  const r = await fetch(`/api/conversations?user_id=${encodeURIComponent(userId)}`, { headers: authHeaders() });
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
  opts.headers = { ...(opts.headers || {}), ...authHeaders(), ...(token ? { "X-Admin-Token": token } : {}) };
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
  const r = await fetch(`/api/session/${sessionId}/history`, { headers: authHeaders() });
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

// ---- 客服工作台(坐席侧)----
export type WbConversation = {
  conversation_id: string; user_id: string; status: string;
  created_at: string; last_active: string; manual: boolean; preview: string; turns: number;
};
export type WbTurn = { role: "user" | "assistant"; content: string };

export async function adminListConversations(limit = 50): Promise<WbConversation[]> {
  const r = await adminFetch(`/api/admin/conversations?limit=${limit}`);
  if (!r.ok) throw new Error("HTTP " + r.status);
  return (await r.json()).conversations as WbConversation[];
}
export async function adminGetMessages(sessionId: string): Promise<WbTurn[]> {
  const r = await adminFetch(`/api/admin/session/${sessionId}/messages`);
  if (!r.ok) throw new Error("HTTP " + r.status);
  return (await r.json()).turns as WbTurn[];
}
export async function adminReply(sessionId: string, text: string): Promise<WbTurn[]> {
  const r = await adminFetch(`/api/admin/session/${sessionId}/reply`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
  if (!r.ok) throw new Error("HTTP " + r.status);
  return (await r.json()).turns as WbTurn[];
}
export async function adminTakeover(sessionId: string): Promise<{ mode: string }> {
  const r = await adminFetch(`/api/session/${sessionId}/takeover`, { method: "POST" });
  return r.json();
}

// ---- Skill 管理(自进化状态总览)----
export type SkillCatalogEntry = { name: string; description: string };

export type SkillCandidate = {
  name: string; path: string; valid: boolean;
  unknown_tools: string[]; errors: string[]; is_improvement: boolean;
  // risk/policy 为 null 表示"判不了"(候选文件读不出等),必须按"需人工复核"处理,不是低危
  risk: string | null; policy: string | null;
};

export type SkillCanary = {
  skill_name: string; candidate_path: string; percent: number;
  risk: string | null; policy: string | null; status: string;
  started_at: string; finished_at: string | null;
};

export type SkillsOverview = {
  live: SkillCatalogEntry[];
  candidates: SkillCandidate[];
  traces: Record<string, Record<string, number>>;
  traces_window: { limit: number; note: string };
  canaries: SkillCanary[];
};

export function getSkillsOverview(): Promise<SkillsOverview> {
  return getJSON<SkillsOverview>("/api/admin/skills");
}
