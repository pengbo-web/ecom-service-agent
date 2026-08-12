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
// 空列表有两种完全不同的含义:这家店真的没有商品(degraded=false),或者商品
// 服务连不上(degraded=true)。改造前两者都返回 [],页面一律显示"暂无商品",
// 于是一次内网故障在买家眼里就是"这家店是空的"。后端现在把 degraded/reason
// 一并下发,这里如实透出——**不要在前端把它折回成一个数组**。
export type ProductList = { products: Product[]; degraded: boolean; reason?: string };

export async function getProducts(keyword = ""): Promise<ProductList> {
  try {
    const r = await fetch(`/api/products?keyword=${encodeURIComponent(keyword)}`);
    if (!r.ok) return { products: [], degraded: true, reason: `商品接口 ${r.status}` };
    const d = await r.json();
    return {
      products: (d.products || []) as Product[],
      // 老服务端没有这个字段。此时不能默认 true(会把正常的空店铺报成故障),
      // 也不能因为缺字段就丢掉商品——按"未降级"处理,退回改造前的语义。
      degraded: !!d.degraded,
      reason: d.reason,
    };
  } catch (e) {
    return { products: [], degraded: true, reason: `商品接口请求失败(${String(e)})` };
  }
}
/** 单个商品。**null 有两种含义,必须分开**——与 getProducts 同一条口径:
 * `degraded=false` 是"没有这个商品"(下架/id 不对),`degraded=true` 是"商品服务
 * 连不上"。实测 hmdp 挂掉时这个接口只返回 `{"product": null}`,而紧挨着的列表接口
 * 老老实实报了 degraded——同一个故障两个相邻端点两种说法,买家看到的是"商品下架了"。 */
export type ProductDetail = { product: Product | null; degraded: boolean; reason?: string };

export async function getProductDetail(itemId: string): Promise<ProductDetail> {
  try {
    const r = await fetch(`/api/product/${encodeURIComponent(itemId)}`);
    if (!r.ok) return { product: null, degraded: true, reason: `商品接口 ${r.status}` };
    const d = await r.json();
    return { product: (d.product as Product | null) ?? null, degraded: !!d.degraded,
             reason: d.reason };
  } catch (e) {
    return { product: null, degraded: true, reason: `商品接口请求失败(${String(e)})` };
  }
}

/** 兼容既有调用方:只要商品本身。**拿不到时分不出是下架还是故障**,所以新代码
 * 应该用 `getProductDetail`,这个薄封装只为不惊动现有调用点。 */
export async function getProduct(itemId: string): Promise<Product | null> {
  return (await getProductDetail(itemId)).product;
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

/** 待支付订单的「去支付」:unpaid → pending(待发货)。失败(越权/已支付/不存在)抛错。 */
export async function payOrder(orderId: string): Promise<{ success: boolean; status: string; status_label: string }> {
  const r = await fetch(`/api/order/${encodeURIComponent(orderId)}/pay`, {
    method: "POST", headers: authHeaders(),
  });
  if (!r.ok) {
    let detail = "";
    try { detail = (await r.json()).detail || ""; } catch { /* 忽略非 JSON 响应体 */ }
    throw new Error(detail || `支付失败 (${r.status})`);
  }
  return r.json();
}

// ---- 购物车(N5:只收集意向,不做结算——下单仍走既有自助下单路径)----
// 商品信息由**后端**补齐,不在前端拉商品列表自己 join——购物车里的价格必须
// 与商城页、与最终下单金额同源,各查各的迟早对不上。
// price/subtotal 为 null 表示该商品查不到(下架或商品服务抖动),此时
// product_missing=true:**绝不能把 null 渲染成 ¥0**,那会让买家以为免费。
export type CartItem = {
  id: number; user_id: string; sku: string; quantity: number; added_at: string; status: string;
  title?: string | null; price?: number | null; image?: string | null;
  stock?: number | null; subtotal?: number | null; product_missing?: boolean;
};

/** 购物车。`degraded=true` 时行还在、但价格取不到——**含义从"这些商品没了"变成
 * "这一刻取不到"**,前端必须分得出来,否则买家会以为自己加的东西全下架了。 */
export type CartPayload = { items: CartItem[]; degraded: boolean };

export async function getCartPayload(): Promise<CartPayload> {
  const r = await fetch("/api/cart", { headers: authHeaders() });
  if (!r.ok) throw new Error(`加载购物车失败 (${r.status})`);
  const d = await r.json();
  return { items: (d.items as CartItem[]) || [], degraded: !!d.degraded };
}

export async function getCart(): Promise<CartItem[]> {
  return (await getCartPayload()).items;
}

export async function addToCartApi(itemId: string, quantity = 1): Promise<{ success: boolean }> {
  const r = await fetch("/api/cart", {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ item_id: itemId, quantity }),
  });
  if (!r.ok) throw new Error(`加入购物车失败 (${r.status})`);
  return r.json();
}

export async function removeFromCartApi(sku: string): Promise<{ success: boolean }> {
  const r = await fetch(`/api/cart/${encodeURIComponent(sku)}`, {
    method: "DELETE", headers: authHeaders(),
  });
  if (!r.ok) throw new Error(`移除失败 (${r.status})`);
  return r.json();
}

/** 把某个 sku 的数量设置为一个具体值(而不是累加)。增/减都走这一个端点,
 * 不按方向拆成两条不同路径。quantity 必须是正整数,设为 0 请改用移除。 */
export async function setCartQuantity(sku: string, quantity: number): Promise<{ success: boolean }> {
  const r = await fetch(`/api/cart/${encodeURIComponent(sku)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ quantity }),
  });
  if (!r.ok) {
    let detail = "";
    try { detail = (await r.json()).detail || ""; } catch { /* 忽略非 JSON 响应体 */ }
    throw new Error(detail || `设置数量失败 (${r.status})`);
  }
  return r.json();
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
// unpaid_flow_enabled(N5):购物车 Tab / 待支付「去支付」按钮是否出现由它决定——
// 关闭时前端须退回改造前的样子,不能只靠"后端永远不会产生 unpaid 订单"这个
// 事实隐式兜底(购物车本身是全新入口,不隐式消失)。
export async function getConfig(): Promise<{ demo_mode: boolean; demo_user_id: string; unpaid_flow_enabled: boolean }> {
  try {
    const r = await fetch("/api/config");
    return r.ok ? r.json() : { demo_mode: false, demo_user_id: "", unpaid_flow_enabled: false };
  } catch {
    return { demo_mode: false, demo_user_id: "", unpaid_flow_enabled: false };
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
// busy=true:会话正在处理上一条消息,本次没有巩固(不是"没有可记的事实")。
// 两者都是 count=0,但含义完全相反——一个是"再聊几句",一个是"稍后重试"。
export type ConsolidateResult = {
  enabled: boolean; curation?: boolean; count: number; facts: LtmFact[];
  busy?: boolean; reason?: string;
};

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
// 坐席侧读某个客户的订单。买家开口第一句几乎总是关于某一笔订单,坐席看不到
// 订单就只能反问"您的订单号是多少"——把 AI 已经知道的事情重新问一遍人。
// 与买家自己的 /api/orders 共用同一条读取路径:两边看到的必须是同一份事实。
export type CustomerOrders = {
  success: boolean; user_id: string; orders: MyOrder[]; degraded: string;
};

export async function adminCustomerOrders(userId: string): Promise<CustomerOrders> {
  const r = await adminFetch(`/api/admin/customer/${encodeURIComponent(userId)}/orders`);
  if (!r.ok) throw new Error(`加载客户订单失败 (${r.status})`);
  return r.json();
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
  // 失败要**可行动**。改造前只回一句三选一的「frontmatter 不全 / 工具名不实 /
  // 名字非法」,店主既不知道是哪一种也不知道该改什么;实测真因往往只是资料里写了
  // 一个本店没有的工具名(SOP 里的『走人工工单』被模型写成 escalate_to_human)。
  unknown_tools?: string[];
  available_tools?: string[];
  // 2 = 已自动带着精确原因重试过一次仍未通过(只重试一次,不做无限循环烧钱)
  attempts?: number;
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
    // 数据源口径:这些数字只覆盖 agent 订单库,不含 hmdp 渠道的成交(两库无同步)。
    // 后端刚补上,老响应体没有这个键时不显示这一条,不崩。
    data_scope?: string;
  };
  products: { success: boolean; window_days: number; products: Array<{
    sku: string; name: string; orders: number; revenue: number;
    refunds: number; refund_rate: number; stock: number | null;
    refund_reasons: Array<{ reason: string; count: number }>;
  }> };
  anomalies: SellerAnomaly[];
  // 扫描的口径与盲区。两个窗口都在:`quality` 是按经营窗(默认 7 天)算的服务
  // 质量,告警按 service_window_days(默认 1 天)判——两个数会不一样,而且应该
  // 不一样(7 天里坏过、今天已经好了)。控制台必须标出哪个是哪个,否则会被读成
  // 「看板自相矛盾」。service_insufficient 则让"没报警"和"没数据所以报不了警"
  // 成为两件可分辨的事。
  anomaly_scope?: {
    window_days: number; service_window_days: number;
    service_insufficient: Array<{ skill_name: string; total: number; min_samples: number }>;
    products_examined: number; products_truncated: boolean;
    reviews_examined: number; reviews_truncated: boolean;
  };
  // `skills` 一直在响应里,但前端此前只取了 `emotion`,把每个 skill 的成功率/
  // 工具失败率/转人工率丢掉了——那是这一页最该显示的东西。缺了它,告警窗缩到
  // 近 1 天之后,"某个 skill 前几天坏过"在界面上就彻底无处可见了。
  quality?: {
    success: boolean; window_days: number; emotion: EmotionDistribution;
    skills?: SkillQuality[];
  };
  reviews?: ReviewInsights;
};

/** 单个 skill 的服务质量。`other` 是 outcome 不属于成功/工具失败/转人工三类的
 * 行数——仍计入 total 但不进任何比率,所以三个 rate 不保证求和为 1。后端刻意
 * 把它显式给出来,不然未来新增或拼错的 outcome 会悄悄从比率里消失,长得和
 * 「这个 skill 一直很健康」一模一样。 */
export type SkillQuality = {
  skill_name: string; total: number; success_rate: number;
  tool_error_rate: number; human_rate: number; other: number;
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
  // N6:offer.coupon_code 非空时,后端附上这张券的文案(如「满300减30」),
  // 唯一来自 app.agent.coupons.grants.COUPON_BY_CODE(再往上追溯是
  // order_ops._COUPONS)。券码不是店铺已知券(模型编的)时为空字符串——
  // 前端据此判断"这不是一张真实的券",而不是自己另存一份券码/文案表。
  coupon_discount?: string;
  // 可达性:投递通道是"把消息追加进买家自己的客服会话",买家没有会话就送不出去,
  // 而且重试永远失败。与 coupon_discount 同一条原则——把"批准之后会发生什么"在
  // 按钮按下**之前**摆出来。商机发现器读 orders/carts,与 conversations 无关,
  // 所以待审队列里本来就会混进结构上不可达的目标。后端刚补上,老响应没有这两个
  // 键时按"可达"处理(不凭空标红)。
  deliverable?: boolean;
  undeliverable_reason?: string;
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

// 商机类型全集(kind + 中文标签),唯一口径在后端 app/agent/tools/growth.py
// 的 OPPORTUNITY_KINDS——前端不再自己抄一份,新增一个 kind 后端一改,这里
// 就跟着长出来,不需要同步改前端。
export type OpportunityKind = { kind: string; label: string };

export async function getOpportunityKinds(): Promise<{ success: boolean; kinds: OpportunityKind[] }> {
  const r = await adminFetch("/api/admin/growth/opportunity-kinds");
  if (!r.ok) throw new Error(`加载商机类型失败 (${r.status})`);
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

// ---- 触达转化归因(N3):发送时记基线,到期按订单状态是否推进判定 ----
export type OutreachStats = {
  success: boolean; window_days: number; window_hours: number;
  sent: number; converted: number; conversion_rate: number;
};

export async function getOutreachStats(windowDays = 30): Promise<OutreachStats> {
  const r = await adminFetch(`/api/admin/growth/outreach-stats?window_days=${windowDays}`);
  if (!r.ok) throw new Error(`加载触达效果失败 (${r.status})`);
  return r.json();
}

// ---- 协作健康:失败事件 + worker 心跳 ----
// 系统刻意不自动重试失败事件(坏事件会无限循环),所以这两样必须**看得见**,
// 否则"留在表里供人工决定"就等于留给没人。stale_seconds/healthy 由后端算好
// 下发,前端不拿本地时钟去减——两边时钟不一致会算出负数或夸张数值,而这个
// 数字正是运维判断"要不要去看一眼"的唯一依据。
export type CollabFailedEvent = {
  id: number; event_type: string; source_agent: string; target_agent: string;
  correlation_id: string; created_at: string; consumed_at: string | null;
};
export type CollabHealth = {
  success: boolean;
  failed_count: number;
  failed: CollabFailedEvent[];
  worker: {
    name: string; last_success_at: string | null; last_error_at: string | null;
    last_error: string | null; stale_seconds: number | null;
    threshold_seconds: number; healthy: boolean;
  };
  // 预算是**本 API 进程**的计数器,恒为 0——真正花钱的是 worker 进程。
  // scope/note 由后端下发,前端必须原样显示:只摆一个 spent=0 会让人得出
  // "worker 没花过钱"的错误结论。可选字段(老服务端没有这个键时走空态)。
  budget?: { limit: number; spent: number; enabled: boolean; scope: string; note: string };
  // 降级统计。参谋归因的 LLM 调用失败时降级为纯统计,而事件正常走完 → done。
  // 实测一轮 20 条全部降级,worker 报告的却是 done:20 / failed:0,链上一片绿色。
  // 没有这个字段,**一次完全无效的运行和一次健康的运行在界面上长得一模一样**。
  degraded?: {
    window: number; diagnoses: number; degraded: number; rate: number;
    all_degraded: boolean; note: string;
  };
};

export async function getCollabHealth(): Promise<CollabHealth> {
  const r = await adminFetch("/api/admin/collab/health");
  if (!r.ok) throw new Error(`加载协作健康失败 (${r.status})`);
  return r.json();
}

export async function retryFailedEvent(eventId: number): Promise<{ success: boolean; changed: boolean; message: string }> {
  const r = await adminFetch(`/api/admin/collab/failed/${eventId}/retry`, { method: "POST" });
  if (!r.ok) throw new Error(`重试失败 (${r.status})`);
  return r.json();
}

// ---- Skill 转正 / 驳回 / 回滚(自进化闭环的最后一环)----
// 改造前候选**只进不出**:能从界面产生,却只能登进服务器敲
// `python -m app.scripts.promote_skill` 才上得线。7 步闭环因此断在最后一环。
//
// 门禁是**显式选项**:gate_candidate 会真跑两轮评测(候选 vs 现行,各自真调 LLM),
// 一两分钟且花钱,不能挂在按钮上默认同步等。不跑门禁时后端 fail-closed 拒绝,
// 于是这个按钮**不会静默绕过门禁**——要放行必须显式 force。
export type SkillPromoteResult = {
  success: boolean; promoted: boolean; reason?: string;
  backup?: string | null; risk?: string | null; policy?: string | null;
  gate_note?: string;
};

export async function promoteSkill(
  name: string, opts: { force?: boolean; runGate?: boolean } = {}
): Promise<SkillPromoteResult> {
  const q = new URLSearchParams();
  if (opts.force) q.set("force", "true");
  if (opts.runGate) q.set("run_gate", "true");
  const r = await adminFetch(
    `/api/admin/skills/${encodeURIComponent(name)}/promote?${q}`, { method: "POST" });
  if (!r.ok) {
    let detail = "";
    try { detail = (await r.json()).detail || ""; } catch { /* 忽略非 JSON 响应体 */ }
    throw new Error(detail || `转正失败 (${r.status})`);
  }
  return r.json();
}

export async function rejectSkill(name: string): Promise<{ success: boolean; archived_to: string }> {
  const r = await adminFetch(
    `/api/admin/skills/${encodeURIComponent(name)}/reject`, { method: "POST" });
  if (!r.ok) {
    let detail = "";
    try { detail = (await r.json()).detail || ""; } catch { /* 忽略非 JSON 响应体 */ }
    throw new Error(detail || `驳回失败 (${r.status})`);
  }
  return r.json();
}

export async function rollbackSkill(name: string): Promise<{ success: boolean; restored?: string }> {
  const r = await adminFetch(
    `/api/admin/skills/${encodeURIComponent(name)}/rollback`, { method: "POST" });
  if (!r.ok) {
    let detail = "";
    try { detail = (await r.json()).detail || ""; } catch { /* 忽略非 JSON 响应体 */ }
    throw new Error(detail || `回滚失败 (${r.status})`);
  }
  return r.json();
}

// ---- 知识库文档管理(代替去 ApeRAG 自己的页面上传)----
// 检索侧不变,仍走 aperag_search 读同一个 collection;这里只把"写"搬进本管理端。
// 已在真服务上验证:API 写入的文档与 ApeRAG UI 上传的文档落在同一 collection、
// 走同一条索引流水线、被同一次检索并排召回。
//
// 索引状态用**上游原值**:PENDING / CREATING / ACTIVE / DELETING / FAILED。
// 终态是 ACTIVE 而不是 COMPLETE——项目里此前两处写成 COMPLETE,导致轮询永远
// 等不到终态。不在前端翻译成自己一套词,免得又多一处会漂移的口径。
export type KbDocument = {
  id: string; name: string; size?: number | null;
  status?: string | null;
  vector_index_status?: string | null;
  fulltext_index_status?: string | null;
  created?: string | null;
};
export type KbDocumentList = {
  success: boolean; collection_id: string; base_url: string; documents: KbDocument[];
};

export async function getKbDocuments(): Promise<KbDocumentList> {
  const r = await adminFetch("/api/admin/kb/documents");
  if (!r.ok) {
    let detail = "";
    try { detail = (await r.json()).detail || ""; } catch { /* 忽略非 JSON 响应体 */ }
    throw new Error(detail || `加载知识库失败 (${r.status})`);
  }
  return r.json();
}

export type KbUploadResult = {
  success: boolean; document_id?: string; name?: string;
  note?: string; reason?: string;
};

/** 上传一份文档。后端**上传即确认**,返回后索引仍在异步建(约 15 秒)。 */
export async function uploadKbDocument(file: File): Promise<KbUploadResult> {
  const form = new FormData();
  form.append("file", file);
  // 不要手动设 Content-Type,交给浏览器带上 multipart 边界
  const r = await adminFetch("/api/admin/kb/documents/upload", { method: "POST", body: form });
  if (!r.ok) {
    let detail = "";
    try { detail = (await r.json()).detail || ""; } catch { /* 忽略非 JSON 响应体 */ }
    throw new Error(detail || `上传失败 (${r.status})`);
  }
  return r.json();
}

export async function deleteKbDocument(docId: string): Promise<{ success: boolean }> {
  const r = await adminFetch(`/api/admin/kb/documents/${encodeURIComponent(docId)}`,
                             { method: "DELETE" });
  if (!r.ok) {
    let detail = "";
    try { detail = (await r.json()).detail || ""; } catch { /* 忽略非 JSON 响应体 */ }
    throw new Error(detail || `删除失败 (${r.status})`);
  }
  return r.json();
}

// ---- 协作链:清单 → 时间线 ----
// 后端事件表的 correlation_id 串起一整条"谁因为什么唤醒了谁"。清单端点是
// 时间线的**唯一入口**——时间线要求先知道 correlation_id,而在清单之前,一条
// 协作链除非恰好失败(才会出现在 failed 列表里)否则无处可寻。
export type CollabChain = {
  correlation_id: string; events: number;
  started_at: string; last_at: string;
  failed: number; pending: number; skipped: number;
  // 注意这里**没有** degraded:降级诊断不产生任何总线事件(路由算不出目标时
  // publish 不插行),按链统计必然恒为 0。降级走 CollabHealth.degraded。
  agents: string[];
};

export async function getCollabChains(limit = 30): Promise<{ chains: CollabChain[] }> {
  const r = await adminFetch(`/api/admin/collab/chains?limit=${limit}`);
  if (!r.ok) throw new Error(`加载协作链失败 (${r.status})`);
  return r.json();
}

// 事件的四态:pending(待认领)/ done(已处理)/ failed(处理失败,不自动重试)/
// skipped(消费闸拦下,是**正常**结果不是错误——营销静默期就走这条)。
// skipped 必须与 failed 分开渲染:把"刻意没做"显示成"出错了"会让运营去修一个
// 根本不存在的故障。
export type CollabEvent = {
  id: number; event_type: string; source_agent: string; target_agent: string;
  correlation_id: string; status: string; priority: number;
  created_at: string; consumed_at: string | null;
  payload: Record<string, unknown>;
};

export type CollabSharedRow = {
  key: string; value: unknown; source_agent: string;
  correlation_id: string; updated_at: string; expires_at: string | null;
};

export type CollabTimeline = {
  success: boolean; events: CollabEvent[]; shared: CollabSharedRow[];
};

export async function getCollabTimeline(correlationId: string, limit = 100): Promise<CollabTimeline> {
  const r = await adminFetch(
    `/api/admin/collab/timeline?correlation_id=${encodeURIComponent(correlationId)}&limit=${limit}`);
  if (!r.ok) throw new Error(`加载时间线失败 (${r.status})`);
  return r.json();
}

// ---- 人工闸待办 ----
// 路由表把 drafts_ready / outreach_converted / outreach_no_change 都投给 human,
// 但 worker 只消费 analyst 与 growth——human 的事件没有消费方。在这个入口之前
// 也没有任何界面列出它们(实测积压 325 条,永远 pending)。
// 与失败事件曾经的处境完全一样:"留给人工"事实上是"留给没人"。
export type CollabInbox = {
  success: boolean; pending: number; events: CollabEvent[];
};

export async function getCollabInbox(limit = 50): Promise<CollabInbox> {
  const r = await adminFetch(`/api/admin/collab/inbox?limit=${limit}`);
  if (!r.ok) throw new Error(`加载人工待办失败 (${r.status})`);
  return r.json();
}

export async function ackCollabEvent(eventId: number): Promise<{ success: boolean; changed: boolean; message: string }> {
  const r = await adminFetch(`/api/admin/collab/inbox/${eventId}/ack`, { method: "POST" });
  if (!r.ok) throw new Error(`确认失败 (${r.status})`);
  return r.json();
}

// ---- 路由订阅表(隐式编排的权威声明)----
// 三个 Expert 之间没有任何一方在指挥另一方,它们只是各自订阅了关心的事件。
// 这份表是那句话唯一的证据,唯一口径在 app/multi_agent/routing.py。
// 中文标签(priority_label / agents[].label)也由后端给,前端不另抄一份映射。
export type RoutingSubscription = {
  event_type: string; target: string; conditional: boolean; reason: string;
  priority: number | null; priority_dynamic: boolean; priority_label: string;
};
export type RoutingGate = { target: string; name: string; reason: string };
export type RoutingAgent = { key: string; label: string; side: string; desc: string };
export type CollabRouting = {
  success: boolean;
  subscriptions: RoutingSubscription[];
  gates: RoutingGate[];
  agents: RoutingAgent[];
};

export async function getCollabRouting(): Promise<CollabRouting> {
  const r = await adminFetch("/api/admin/collab/routing");
  if (!r.ok) throw new Error(`加载路由表失败 (${r.status})`);
  return r.json();
}

// ---- 跟进序列(N7:持续沟通=序列自动推进,不是自动发送;每一步仍是待审草稿)----
// kind_label/stop_reason_label 中文标签由后端同源给出,唯一口径分别在
// app/agent/tools/growth.py 的 OPPORTUNITY_KINDS 与
// app/multi_agent/followup.py 的 STOP_REASON_LABELS,前端不重抄一份。
export type OutreachFollowup = {
  id: number; user_id: string; kind: string; kind_label?: string;
  correlation_id: string; step: number; max_steps: number;
  next_touch_at: string; status: "active" | "done" | "stopped";
  stop_reason: string | null; stop_reason_label?: string;
  created_at: string; updated_at: string;
};

export async function getFollowups(status = ""): Promise<{ success: boolean; followups: OutreachFollowup[] }> {
  const r = await adminFetch(`/api/admin/growth/followups?status=${encodeURIComponent(status)}`);
  if (!r.ok) throw new Error(`加载跟进链失败 (${r.status})`);
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
