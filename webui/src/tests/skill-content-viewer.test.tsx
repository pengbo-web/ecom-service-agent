/** 点开看技能正文。
 *
 * 改造前这个页面上根本没有这个能力:只有名字和 description,而 description 只是
 * frontmatter 里的一句话。待审候选那一栏尤其要紧——「转正上线」会立刻把这份正文
 * 推给线上会话,而在此之前操作者看不到它写了什么。
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi, beforeEach } from "vitest";
import { SkillContentViewer } from "@/components/skills/SkillContentViewer";

const BODY = {
  name: "track-order", variant: "live", path: "app/.../track-order/SKILL.md",
  content: "---\nname: track-order\n---\n第一步：调用 `query_order`\n超过 3 天无更新则升级",
  attachment_text: "", files: [], version: 4, fingerprint: "cc7af8c4deadbeef",
  truncated: false, over_cap: false, unreadable: [], escaped: [],
};

let calls: string[] = [];

function stub(body: any = BODY, ok = true) {
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    calls.push(String(url));
    return { ok, json: async () => (ok ? body : { detail: "live 目录下没有技能 x" }) };
  }));
}

beforeEach(() => { vi.clearAllMocks(); localStorage.clear(); calls = []; });

describe("技能正文查看", () => {
  it("默认折叠,不预先拉取正文", () => {
    stub();
    render(<SkillContentViewer name="track-order" variant="live" />);
    expect(screen.queryByTestId("skill-body-live-track-order")).toBeNull();
    // 正文可以有几千字 × 13 个 skill,不该在页面加载时全都拖下来
    expect(calls.length).toBe(0);
  });

  it("点开后显示的是正文原文,不是 description", async () => {
    stub();
    render(<SkillContentViewer name="track-order" variant="live" />);
    fireEvent.click(screen.getByTestId("view-skill-live-track-order"));
    const body = await screen.findByTestId("skill-body-live-track-order");
    expect(body.textContent).toMatch(/第一步：调用/);
    expect(body.textContent).toMatch(/超过 3 天无更新/);
  });

  it("原样显示,不渲染成 Markdown", async () => {
    // 这份文本是**喂给模型的指令**。渲染会把标记吃掉,而模型看到的正是那些原始字符。
    stub({ ...BODY, content: "## 步骤\n- 先 `query_order`" });
    const { container } = render(<SkillContentViewer name="track-order" variant="live" />);
    fireEvent.click(screen.getByTestId("view-skill-live-track-order"));
    await screen.findByTestId("skill-body-live-track-order");
    expect(container.querySelector("h2")).toBeNull();
    expect(container.querySelector("pre")?.textContent).toMatch(/## 步骤/);
  });

  it("按 variant 请求候选,不会读成现行版", async () => {
    stub({ ...BODY, variant: "candidate" });
    render(<SkillContentViewer name="order-query" variant="candidate" />);
    fireEvent.click(screen.getByTestId("view-skill-candidate-order-query"));
    await waitFor(() => expect(calls.length).toBe(1));
    expect(calls[0]).toMatch(/variant=candidate/);
    expect(calls[0]).toMatch(/order-query/);
  });

  it("再次点开不重复请求(正文不会自己变)", async () => {
    stub();
    render(<SkillContentViewer name="track-order" variant="live" />);
    const btn = screen.getByTestId("view-skill-live-track-order");
    fireEvent.click(btn);
    await screen.findByTestId("skill-body-live-track-order");
    fireEvent.click(btn);
    fireEvent.click(btn);
    await screen.findByTestId("skill-body-live-track-order");
    expect(calls.length).toBe(1);
  });

  it("带上版本与指纹,好和轨迹对上账", async () => {
    stub();
    render(<SkillContentViewer name="track-order" variant="live" />);
    fireEvent.click(screen.getByTestId("view-skill-live-track-order"));
    const body = await screen.findByTestId("skill-body-live-track-order");
    expect(body.textContent).toMatch(/v4/);
    expect(body.textContent).toMatch(/cc7af8c4/);
  });

  it("读失败时报错,不显示一个空白框", async () => {
    stub(null, false);
    render(<SkillContentViewer name="x" variant="live" />);
    fireEvent.click(screen.getByTestId("view-skill-live-x"));
    expect((await screen.findByTestId("skill-body-err-live-x")).textContent)
      .toMatch(/没有技能/);
  });
});

describe("正文没读全必须显形", () => {
  it("截断要说出来", async () => {
    stub({ ...BODY, truncated: true });
    render(<SkillContentViewer name="track-order" variant="live" />);
    fireEvent.click(screen.getByTestId("view-skill-live-track-order"));
    expect((await screen.findByTestId("skill-body-disclosure-live-track-order")).textContent)
      .toMatch(/不是全部内容/);
  });

  it("逃逸符号链接要说清「模型读不到但转正会复制上线」", async () => {
    stub({ ...BODY, escaped: ["references/../../secret.md"] });
    render(<SkillContentViewer name="track-order" variant="live" />);
    fireEvent.click(screen.getByTestId("view-skill-live-track-order"));
    const d = await screen.findByTestId("skill-body-disclosure-live-track-order");
    expect(d.textContent).toMatch(/逃出技能目录/);
    expect(d.textContent).toMatch(/复制上线/);
  });

  it("一切正常时不出现这块,免得警告变成噪声", async () => {
    stub();
    render(<SkillContentViewer name="track-order" variant="live" />);
    fireEvent.click(screen.getByTestId("view-skill-live-track-order"));
    await screen.findByTestId("skill-body-live-track-order");
    expect(screen.queryByTestId("skill-body-disclosure-live-track-order")).toBeNull();
  });

  it("附带资料照样显示——它们会随转正一起上线", async () => {
    stub({ ...BODY, files: ["references/policy.md"],
           attachment_text: "退货政策全文……" });
    render(<SkillContentViewer name="track-order" variant="live" />);
    fireEvent.click(screen.getByTestId("view-skill-live-track-order"));
    const body = await screen.findByTestId("skill-body-live-track-order");
    expect(body.textContent).toMatch(/references\/policy\.md/);
    expect(body.textContent).toMatch(/退货政策全文/);
  });
});
