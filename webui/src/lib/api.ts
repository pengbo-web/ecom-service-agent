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
