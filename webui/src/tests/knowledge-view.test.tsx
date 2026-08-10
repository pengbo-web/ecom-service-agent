import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { KnowledgeView } from "@/components/KnowledgeView";

// 知识库文档管理:代替去 ApeRAG 自己的页面上传。检索侧不变。
// 契约已在真服务上验证(scripts/aperag_write_smoke.py 退出码 0):
// 索引生命周期 PENDING → CREATING → ACTIVE(**不是 COMPLETE**),约 15 秒。

const DOCS = {
  success: true,
  collection_id: "colbc6732423a8ef83e",
  base_url: "http://127.0.0.1:8100",
  documents: [
    { id: "d1", name: "退换货政策.md", size: 2048, vector_index_status: "ACTIVE",
      created: "2026-08-10 10:00:00" },
    { id: "d2", name: "运费规则.md", size: 1024, vector_index_status: "CREATING",
      created: "2026-08-10 11:00:00" },
    { id: "d3", name: "扫描件.pdf", size: 5_000_000, vector_index_status: "FAILED",
      created: "2026-08-10 12:00:00" },
  ],
};

function stub(over: Record<string, unknown> = {}, onCall?: (u: string, m: string) => void) {
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    const u = String(url);
    const m = init?.method || "GET";
    onCall?.(u, m);
    if (m === "DELETE") {
      return over.deleteFails
        ? { ok: false, status: 502, json: async () => ({ detail: "删除失败" }) }
        : { ok: true, json: async () => ({ success: true }) };
    }
    if (m === "POST") {
      return { ok: true, json: async () => over.upload ?? { success: true, name: "新政策.md",
                                                            note: "索引正在异步创建（约 15 秒）" } };
    }
    if (over.listFails) return { ok: false, status: 502, json: async () => ({ detail: "知识库服务连不上" }) };
    return { ok: true, json: async () => over.docs ?? DOCS };
  }));
}

describe("KnowledgeView 文档列表", () => {
  beforeEach(() => { localStorage.clear(); vi.useRealTimers(); });
  afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

  it("渲染文档与索引状态,ACTIVE 显示为已生效", async () => {
    stub();
    render(<KnowledgeView />);
    const row = await screen.findByTestId("kb-doc-d1");
    expect(within(row).getByText("退换货政策.md")).toBeInTheDocument();
    expect(within(row).getByText("已生效")).toBeInTheDocument();
  });

  it("建索引中的文档标明「完成后才会被召回」", async () => {
    // 状态不是终态时必须说清后果,否则店主以为传上去就生效了
    stub();
    render(<KnowledgeView />);
    const row = await screen.findByTestId("kb-doc-d2");
    expect(within(row).getByText("建索引中")).toBeInTheDocument();
    expect(within(row).getByText(/完成后才会被召回/)).toBeInTheDocument();
  });

  it("建索引失败的文档单独横幅提示「不会被召回」", async () => {
    // 它们在列表里长得和别的一样,但不会被召回——店主会以为政策已经生效
    stub();
    render(<KnowledgeView />);
    const banner = await screen.findByTestId("kb-failed-banner");
    expect(within(banner).getByText(/不会被客服召回/)).toBeInTheDocument();
  });

  it("下发上游原始状态值,不翻译成自己一套词", async () => {
    stub();
    render(<KnowledgeView />);
    const row = await screen.findByTestId("kb-doc-d1");
    expect(within(row).getByText(/vector_index_status=ACTIVE/)).toBeInTheDocument();
  });

  it("显示写入目标(地址与 collection),不让人猜传到哪了", async () => {
    stub();
    render(<KnowledgeView />);
    expect(await screen.findByText("colbc6732423a8ef83e")).toBeInTheDocument();
    expect(screen.getByText("http://127.0.0.1:8100")).toBeInTheDocument();
  });

  it("读不到时报错,而不是显示成空知识库", async () => {
    // 「连不上」与「知识库是空的」是两件事,不能都渲染成空列表
    stub({ listFails: true });
    render(<KnowledgeView />);
    expect(await screen.findByTestId("kb-error")).toBeInTheDocument();
    expect(screen.queryByText(/知识库还是空的/)).toBeNull();
  });

  it("真的空时给出为什么要传文档", async () => {
    stub({ docs: { ...DOCS, documents: [] } });
    render(<KnowledgeView />);
    expect(await screen.findByText(/靠模型常识/)).toBeInTheDocument();
  });
});

describe("KnowledgeView 上传", () => {
  beforeEach(() => { localStorage.clear(); });
  afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

  function drop(file: File) {
    const zone = screen.getByTestId("kb-dropzone");
    fireEvent.drop(zone, { dataTransfer: { files: [file] } });
  }

  it("拖拽上传打到正确端点,用 multipart", async () => {
    const calls: [string, string][] = [];
    stub({}, (u, m) => calls.push([u, m]));
    render(<KnowledgeView />);
    await screen.findByTestId("kb-dropzone");
    drop(new File(["# 政策"], "新政策.md", { type: "text/markdown" }));
    await waitFor(() => expect(
      calls.some(([u, m]) => m === "POST" && u === "/api/admin/kb/documents/upload")).toBe(true));
  });

  it("上传成功后提示索引是异步的", async () => {
    // 不说清就会有人传完立刻去问客服,发现"没生效"以为坏了
    stub();
    render(<KnowledgeView />);
    await screen.findByTestId("kb-dropzone");
    drop(new File(["x"], "新政策.md"));
    const ok = await screen.findByTestId("kb-upload-ok");
    expect(ok.textContent).toMatch(/异步/);
  });

  it("上传成功但确认失败要明确说这份文档不会被检索到", async () => {
    // 实测未 confirm 的文档不出现在列表里 —— 不说清就是个查不到的孤儿
    stub({ upload: { success: false, document_id: "d9",
                     reason: "已上传但确认入库失败，该文档不会被检索到" } });
    render(<KnowledgeView />);
    await screen.findByTestId("kb-dropzone");
    drop(new File(["x"], "坏的.md"));
    const err = await screen.findByTestId("kb-upload-error");
    expect(err.textContent).toMatch(/不会被检索到/);
  });

  it("上传失败只影响上传区,列表照常渲染", async () => {
    stub({ upload: { success: false, reason: "上传失败" } });
    render(<KnowledgeView />);
    await screen.findByTestId("kb-dropzone");
    drop(new File(["x"], "a.md"));
    expect(await screen.findByTestId("kb-upload-error")).toBeInTheDocument();
    expect(screen.getByTestId("kb-doc-d1")).toBeInTheDocument();
  });
});

describe("KnowledgeView 删除", () => {
  beforeEach(() => { localStorage.clear(); });
  afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

  it("删除前二次确认,并说明会失去政策依据", async () => {
    // 删掉一份政策文档之后,客服对该类问题的回答退化成模型常识,且不可撤销
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);
    const calls: [string, string][] = [];
    stub({}, (u, m) => calls.push([u, m]));
    render(<KnowledgeView />);
    const row = await screen.findByTestId("kb-doc-d1");
    fireEvent.click(within(row).getByRole("button", { name: /删除/ }));
    expect(confirmSpy).toHaveBeenCalled();
    expect(confirmSpy.mock.calls[0][0]).toMatch(/政策依据/);
    // 点了取消就不该发请求
    expect(calls.some(([, m]) => m === "DELETE")).toBe(false);
  });

  it("确认后打到正确端点", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    const calls: [string, string][] = [];
    stub({}, (u, m) => calls.push([u, m]));
    render(<KnowledgeView />);
    const row = await screen.findByTestId("kb-doc-d1");
    fireEvent.click(within(row).getByRole("button", { name: /删除/ }));
    await waitFor(() => expect(
      calls.some(([u, m]) => m === "DELETE" && u === "/api/admin/kb/documents/d1")).toBe(true));
  });

  it("删除失败的原因记在该行,不冒泡成页面级错误", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    stub({ deleteFails: true });
    render(<KnowledgeView />);
    const row = await screen.findByTestId("kb-doc-d1");
    fireEvent.click(within(row).getByRole("button", { name: /删除/ }));
    await waitFor(() => expect(within(row).getByText(/删除失败/)).toBeInTheDocument());
    expect(screen.queryByTestId("kb-error")).toBeNull();
  });
});
