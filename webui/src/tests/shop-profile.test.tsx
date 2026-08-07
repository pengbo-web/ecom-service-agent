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

  it("含承诺词被后端拒存时,原样显示后端点名的措辞", async () => {
    // GET 走默认 DATA;PUT 模拟后端 400 + detail(与 validate_tone 的拒绝文案一致)。
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(
      async (_url: string, opts?: RequestInit) => {
        if (opts?.method === "PUT") {
          return {
            ok: false,
            status: 400,
            json: async () => ({
              detail: "语气设定包含承诺类措辞:全额退。语气设定只管说话方式，" +
                "不能用来承诺退款、包邮、赔付等具体授权与验证流程。",
            }),
          };
        }
        return { ok: true, json: async () => DATA };
      });
    render(<ShopProfilePanel />);
    fireEvent.click(await screen.findByRole("button", { name: /保存/ }));
    expect(await screen.findByText(/全额退/)).toBeInTheDocument();
    expect(await screen.findByText(/只管说话方式/)).toBeInTheDocument();
  });
});
