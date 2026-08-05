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

// ---- 评价(N4:买家评已签收订单;一单一 sku 只能评一次)----
export type ReviewableItem = { order_id: string; sku: string; name: string; delivered_at: string | null };

/** 当前买家可评价的已签收订单项(已评过的不会出现在这里)。 */
export async function getReviewableItems(): Promise<ReviewableItem[]> {
  const r = await fetch("/api/reviewable", { headers: authHeaders() });
  if (!r.ok) throw new Error(`加载可评价订单失败 (${r.status})`);
  return (await r.json()).items as ReviewableItem[];
}

export type SubmitReviewResult = { success: boolean; review_id?: number; reason?: string };

/** 提交评价。重复评价时后端返回 success:false + reason(明确中文提示,不是 500)。 */
export async function submitReview(
  orderId: string, sku: string, rating: number, content: string
): Promise<SubmitReviewResult> {
  const r = await fetch("/api/review", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ order_id: orderId, sku, rating, content }),
  });
  if (!r.ok) {
    let detail = "";
    try { detail = (await r.json()).detail || ""; } catch { /* 忽略非 JSON 响应体 */ }
    throw new Error(detail || `提交评价失败 (${r.status})`);
  }
  return r.json();
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

export type SkillUploadResult = {
  accepted: boolean; name: string; replaced: boolean;
  risk: string | null; policy: string | null;
  files: string[]; errors: string[]; unknown_tools: string[];
};

/** 上传技能包(.zip)或单个 SKILL.md 作为候选(后端只写 _candidates 并跑同一套校验)。 */
export async function uploadSkillBundle(file: File): Promise<SkillUploadResult> {
  const form = new FormData();
  form.append("file", file);
  // 注意:不要手动设 Content-Type,交给浏览器带上 multipart 边界
  const r = await adminFetch("/api/admin/skills/upload", { method: "POST", body: form });
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}

export type SkillDistillResult = {
  created: boolean; name: string | null;
  risk: string | null; policy: string | null; errors: string[];
  // 资料超过 MAX_DOC_CHARS(12000)时为 true:尾部没有真正参与蒸馏,前端须提示操作者
  truncated: boolean;
};

/** 上传客服 SOP/产品资料,让后端 LLM 提炼成候选技能(会花钱,调用方需先确认)。 */
export async function distillSkillFromDoc(docText: string): Promise<SkillDistillResult> {
  const r = await adminFetch("/api/admin/skills/distill", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ doc_text: docText }),
  });
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}

// ---- 经营控制台(参谋侧:只读经营总览 + 异常清单 + 对话)----
export type SellerAnomaly = {
  kind: string; subject: string; subject_name?: string;
  value: number; threshold: number; detail?: Record<string, unknown>;
};

// N2:情绪分布(neutral/unhappy/angry 计数 + 激烈占比),来自
// shop_analytics.service_quality 的 emotion 段。`quality` 目前是可选字段——
// 后端 /api/seller/overview 尚未把 service_quality() 并进响应体(该端点在
// app/api/app.py,超出本任务允许改动的文件范围),故先按"可能缺失"处理,
// 缺失时卡片走空态,不假装有数据。
export type EmotionDistribution = {
  window_days: number; total: number;
  counts: { neutral: number; unhappy: number; angry: number };
  angry_rate: number;
};

// N4:评价洞察(全店均分/差评率 + 差评 top 商品与其关键词)。词表匹配抽出的
// bad_terms 可能为空(抽不出就留空,不编造),前端须按空数组处理。
export type ReviewProduct = {
  sku: string; name: string; avg_rating: number; bad_count: number; bad_terms: string[];
};
export type ReviewInsights = {
  success: boolean; window_days: number; avg_rating: number; total: number;
  bad_rate: number; products: ReviewProduct[];
};

export type SellerOverview = {
  overview: {
    success: boolean; window_days: number; orders: number; gmv: number;
    avg_order_value: number; refunds: number; refund_rate: number;
    cancels: number; cancel_rate: number; conversations: number;
    orders_per_conversation: number;
  };
  products: { success: boolean; window_days: number; products: Array<{
    sku: string; name: string; orders: number; revenue: number;
    refunds: number; refund_rate: number; stock: number | null;
    refund_reasons: Array<{ reason: string; count: number }>;
  }> };
  anomalies: SellerAnomaly[];
  quality?: { success: boolean; window_days: number; emotion: EmotionDistribution };
  reviews?: ReviewInsights;
};

export type SellerChatReply = {
  success: boolean; reply: string; agent: string; agent_key: string; session_id: string;
};

export async function getSellerOverview(windowDays = 7): Promise<SellerOverview> {
  const r = await adminFetch(`/api/seller/overview?window_days=${windowDays}`);
  if (!r.ok) throw new Error(`加载经营数据失败 (${r.status})`);
  return r.json();
}

export async function sellerChat(sessionId: string, message: string): Promise<SellerChatReply> {
  const r = await adminFetch("/api/seller/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId, message }),
  });
  if (!r.ok) throw new Error(`参谋暂时不可用 (${r.status})`);
  return r.json();
}

// ---- 增长子区(商机与草稿审批):后端主动生成的触达草稿,批准即真实发给买家 ----
export type OutreachDraft = {
  id: number; opportunity_type: string; opportunity_label?: string;
  user_id: string; order_id: string;
  content: string; offer: Record<string, unknown>; reason: string;
  correlation_id: string; status: string; needs_review_reason: string;
  created_by: string; reviewed_by: string | null; created_at: string;
};

export async function getGrowthDrafts(status = "draft"): Promise<{ drafts: OutreachDraft[] }> {
  const r = await adminFetch(`/api/admin/growth/drafts?status=${encodeURIComponent(status)}`);
  if (!r.ok) throw new Error(`加载草稿失败 (${r.status})`);
  return r.json();
}

export async function approveDraft(id: number): Promise<{ success: boolean; sent: boolean; reason: string }> {
  const r = await adminFetch(`/api/admin/growth/drafts/${id}/approve`, { method: "POST" });
  if (!r.ok) throw new Error(`批准失败 (${r.status})`);
  return r.json();
}

export async function rejectDraft(id: number): Promise<{ success: boolean; changed: boolean }> {
  const r = await adminFetch(`/api/admin/growth/drafts/${id}/reject`, { method: "POST" });
  if (!r.ok) throw new Error(`驳回失败 (${r.status})`);
  return r.json();
}

export type GrowthOpportunity = Record<string, unknown>;
export type GrowthOpportunityList = {
  success: boolean; kind: string; kind_label: string; window_days: number;
  count: number; opportunities: GrowthOpportunity[];
};

export async function getOpportunities(
  kind = "stale_pending_order", windowDays = 14
): Promise<GrowthOpportunityList> {
  const r = await adminFetch(
    `/api/admin/growth/opportunities?kind=${encodeURIComponent(kind)}&window_days=${windowDays}`);
  if (!r.ok) throw new Error(`加载商机失败 (${r.status})`);
  return r.json();
}

// ---- 店铺人格(N1:品牌语气可配置)----
export type ShopProfile = { shop_name: string; tone: string; banned_words: string };
export type ShopProfileResult = {
  success: boolean; profile: ShopProfile; default_tone: string; max_tone_chars: number;
};

export async function getShopProfile(): Promise<ShopProfileResult> {
  const r = await adminFetch("/api/admin/shop/profile");
  if (!r.ok) throw new Error(`加载店铺人格失败 (${r.status})`);
  return r.json();
}

/** 保存店铺人格。留空 tone = 恢复默认(不是错误);后端校验超长会返回 400。 */
export async function putShopProfile(p: ShopProfile): Promise<{ success: boolean }> {
  const r = await adminFetch("/api/admin/shop/profile", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(p),
  });
  if (!r.ok) {
    let detail = "";
    try { detail = (await r.json()).detail || ""; } catch { /* 忽略非 JSON 响应体 */ }
    throw new Error(detail || `保存失败 (${r.status})`);
  }
  return r.json();
}
