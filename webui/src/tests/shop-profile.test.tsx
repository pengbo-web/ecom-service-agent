import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { ShopProfilePanel } from "@/components/operations/ShopProfilePanel";

const DATA = {
  success: true,
  profile: { shop_name: "并夕夕", tone: "简短口语", banned_words: "" },
  default_tone: "默认语气正文", max_tone_chars: 600,
};

describe("ShopProfilePanel", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: true, json: async () => DATA })));
    localStorage.clear();
  });

  it("载入当前人格", async () => {
    render(<ShopProfilePanel />);
    expect(await screen.findByDisplayValue("并夕夕")).toBeInTheDocument();
    expect(await screen.findByDisplayValue("简短口语")).toBeInTheDocument();
  });

  it("显示字数与上限", async () => {
    render(<ShopProfilePanel />);
    expect(await screen.findByText(/600/)).toBeInTheDocument();
  });

  it("超长时禁用保存并给出提示", async () => {
    render(<ShopProfilePanel />);
    const ta = await screen.findByDisplayValue("简短口语");
    fireEvent.change(ta, { target: { value: "啊".repeat(601) } });
    expect(await screen.findByText(/上限/)).toBeInTheDocument();
    expect((await screen.findByRole("button", { name: /保存/ })) as HTMLButtonElement)
      .toBeDisabled();
  });

  it("恢复默认只填回文本框,不直接提交", async () => {
    render(<ShopProfilePanel />);
    fireEvent.click(await screen.findByRole("button", { name: /恢复默认/ }));
    expect(await screen.findByDisplayValue("默认语气正文")).toBeInTheDocument();
    const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
    expect(calls.some((c) => (c[1] as RequestInit | undefined)?.method === "PUT")).toBe(false);
  });

  it("保存后提示下一轮生效", async () => {
    render(<ShopProfilePanel />);
    fireEvent.click(await screen.findByRole("button", { name: /保存/ }));
    await waitFor(async () =>
      expect(await screen.findByText(/下一轮/)).toBeInTheDocument());
  });
});
