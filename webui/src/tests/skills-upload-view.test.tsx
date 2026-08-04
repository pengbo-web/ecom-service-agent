import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { SkillsView } from "@/components/SkillsView";

const EMPTY_OVERVIEW = {
  live: [], candidates: [], traces: {},
  traces_window: { limit: 500, note: "窗口值" }, canaries: [],
};

function pickFile(name: string) {
  const input = document.querySelector('input[type="file"]') as HTMLInputElement;
  const file = new File(["dummy"], name, { type: "application/zip" });
  Object.defineProperty(input, "files", { value: [file], configurable: true });
  fireEvent.change(input);
  return input;
}

describe("SkillsView 上传", () => {
  beforeEach(() => { localStorage.clear(); });

  it("上传请求失败时在上传卡片里报「上传请求失败」,不是顶部的「读取失败」", async () => {
    let call = 0;
    vi.stubGlobal("fetch", vi.fn(async () => {
      call += 1;
      if (call === 1) return { ok: true, json: async () => EMPTY_OVERVIEW };
      return { ok: false, status: 500, json: async () => ({}) };   // 上传请求失败
    }));

    render(<SkillsView />);
    await screen.findByText(/上传技能包/);
    pickFile("skill.zip");

    expect(await screen.findByText(/上传请求失败/)).toBeInTheDocument();
    expect(screen.queryByText(/读取失败/)).toBeNull();
  });

  it("校验未过时展示服务端返回的错误原因", async () => {
    let call = 0;
    vi.stubGlobal("fetch", vi.fn(async () => {
      call += 1;
      if (call === 1) return { ok: true, json: async () => EMPTY_OVERVIEW };
      return { ok: true, json: async () => ({
        accepted: false, name: "", replaced: false, risk: null, policy: null,
        files: [], errors: ["引用了未知工具: order_list"], unknown_tools: ["order_list"],
      }) };
    }));

    render(<SkillsView />);
    await screen.findByText(/上传技能包/);
    pickFile("skill.zip");

    expect(await screen.findByText(/order_list/)).toBeInTheDocument();
  });

  it("处理完会清空 input,同名文件可以再次选择", async () => {
    let call = 0;
    vi.stubGlobal("fetch", vi.fn(async () => {
      call += 1;
      // call 1: 初始总览拉取; call 2: 上传请求; call 3: accepted 后 load() 再次拉取总览
      if (call === 2) return { ok: true, json: async () => ({
        accepted: true, name: "demo", replaced: false, risk: "low",
        policy: "canary_ab", files: [], errors: [], unknown_tools: [],
      }) };
      return { ok: true, json: async () => EMPTY_OVERVIEW };
    }));

    render(<SkillsView />);
    await screen.findByText(/上传技能包/);
    const input = pickFile("skill.zip");

    await screen.findByText(/已收为候选/);
    expect(input.value).toBe("");
  });
});
