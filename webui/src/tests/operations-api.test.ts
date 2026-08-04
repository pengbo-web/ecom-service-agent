import { describe, it, expect, vi, beforeEach } from "vitest";
import { getSellerOverview, sellerChat } from "@/lib/api";

describe("seller api", () => {
  beforeEach(() => { localStorage.clear(); });

  it("overview 带 window_days", async () => {
    // 显式标注参数,使 mock.calls 推断为 [url, init] 二元组(否则严格模式下 calls[0][0] 越界报错)
    const f = vi.fn(async (_url: string, _init?: RequestInit) => ({ ok: true, json: async () => ({ overview: {}, anomalies: [] }) }));
    vi.stubGlobal("fetch", f);
    await getSellerOverview(14);
    expect(String(f.mock.calls[0][0])).toContain("window_days=14");
  });

  it("chat 提交 session_id 与 message", async () => {
    const f = vi.fn(async (_url: string, _init: RequestInit) => ({ ok: true, json: async () => ({ success: true, reply: "ok" }) }));
    vi.stubGlobal("fetch", f);
    await sellerChat("s1", "近7天退款率");
    const body = JSON.parse(String(f.mock.calls[0][1].body));
    expect(body).toEqual({ session_id: "s1", message: "近7天退款率" });
  });

  it("失败抛错而不是静默返回空", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 403, json: async () => ({}) })));
    await expect(getSellerOverview()).rejects.toBeTruthy();
  });
});
