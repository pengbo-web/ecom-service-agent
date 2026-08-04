import { describe, it, expect, vi, beforeEach } from "vitest";
import { getSkillsOverview } from "@/lib/api";

const OVERVIEW = {
  live: [{ name: "process-return", description: "退货退款处理。适用关键词：退货、退款。" }],
  candidates: [
    { name: "track-order", path: "p1", valid: true, unknown_tools: [], errors: [],
      is_improvement: true, risk: "low", policy: "canary_ab" },
    { name: "broken-one", path: "p2", valid: false, unknown_tools: ["order_list"],
      errors: ["引用了未知工具: order_list"], is_improvement: false, risk: null, policy: null },
  ],
  traces: { "track-order": { success: 3, tool_error: 11 } },
  traces_window: { limit: 500, note: "按最近轨迹计数的窗口值,非全时段统计" },
  canaries: [{ skill_name: "track-order", candidate_path: "p1", percent: 50, risk: "low",
               policy: "canary_ab", status: "active", started_at: "2026-08-04 23:13:07",
               finished_at: null }],
};

describe("skills overview api", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => OVERVIEW })));
    localStorage.clear();
  });

  it("解析总览四段", async () => {
    const d = await getSkillsOverview();
    expect(d.live[0].name).toBe("process-return");
    expect(d.candidates).toHaveLength(2);
    expect(d.candidates[0].policy).toBe("canary_ab");
    expect(d.traces["track-order"].tool_error).toBe(11);
    expect(d.traces_window.limit).toBe(500);
    expect(d.canaries[0].percent).toBe(50);
  });

  it("判不了风险的候选保留 null(不得被当成低危)", async () => {
    const d = await getSkillsOverview();
    expect(d.candidates[1].risk).toBeNull();
    expect(d.candidates[1].policy).toBeNull();
  });
});

describe("upload skill bundle api", () => {
  beforeEach(() => { localStorage.clear(); });

  it("上传成功返回名字/风险档/包内文件", async () => {
    const { uploadSkillBundle } = await import("@/lib/api");
    // 显式标注参数,使 mock.calls 推断为 [url, init] 二元组(否则严格模式下 calls[0][1] 越界报错)
    const spy = vi.fn(async (_url: string, _init: RequestInit) => ({
      ok: true,
      json: async () => ({ accepted: true, name: "upload-demo", replaced: false,
                           risk: "low", policy: "canary_ab",
                           files: ["references/policy.md"], errors: [], unknown_tools: [] }),
    }));
    vi.stubGlobal("fetch", spy);

    const f = new File(["dummy"], "skill.zip", { type: "application/zip" });
    const r = await uploadSkillBundle(f);

    expect(r.accepted).toBe(true);
    expect(r.name).toBe("upload-demo");
    expect(r.files).toEqual(["references/policy.md"]);
    // 必须以 FormData 提交(后端是 multipart),不能是 JSON
    expect(spy.mock.calls[0][1].body).toBeInstanceOf(FormData);
  });

  it("校验未过时把错误带回前端", async () => {
    const { uploadSkillBundle } = await import("@/lib/api");
    vi.stubGlobal("fetch", vi.fn(async () => ({
      ok: true,
      json: async () => ({ accepted: false, name: "", replaced: false, risk: null,
                           policy: null, files: [],
                           errors: ["引用了未知工具: order_list"],
                           unknown_tools: ["order_list"] }),
    })));
    const r = await uploadSkillBundle(new File(["x"], "SKILL.md"));
    expect(r.accepted).toBe(false);
    expect(r.errors[0]).toContain("order_list");
  });
});

describe("distill skill api", () => {
  beforeEach(() => { localStorage.clear(); });

  it("蒸馏成功返回候选名与风险档", async () => {
    const { distillSkillFromDoc } = await import("@/lib/api");
    vi.stubGlobal("fetch", vi.fn(async () => ({
      ok: true,
      json: async () => ({ created: true, name: "sop-return", risk: "high",
                           policy: "manual", errors: [] }),
    })));
    const r = await distillSkillFromDoc("退货 SOP 正文");
    expect(r.created).toBe(true);
    expect(r.policy).toBe("manual");
  });

  it("失败时把原因带回前端", async () => {
    const { distillSkillFromDoc } = await import("@/lib/api");
    vi.stubGlobal("fetch", vi.fn(async () => ({
      ok: true,
      json: async () => ({ created: false, name: null, risk: null, policy: null,
                           errors: ["LLM 产物未通过校验"] }),
    })));
    const r = await distillSkillFromDoc("x");
    expect(r.created).toBe(false);
    expect(r.errors[0]).toContain("校验");
  });
});
