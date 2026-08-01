import { describe, it, expect, vi, beforeEach } from "vitest";
import { adminListConversations } from "@/lib/api";

describe("workbench api", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(async () => ({
      ok: true,
      json: async () => ({ conversations: [{ conversation_id: "c-1", user_id: "alice", status: "open", created_at: "", manual: false, preview: "hi", turns: 1 }] }),
    })));
    localStorage.clear();
  });
  it("parses conversation list", async () => {
    const list = await adminListConversations();
    expect(list[0].user_id).toBe("alice");
  });
});
