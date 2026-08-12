/**
 * 回流候选在**人采纳之前**必须说清两件事。
 *
 * 走查评估页时实测:一次回流产出 16 条候选,16 条全部标着"需转人工",其中包括
 * "你们几点上班""帮我看下订单发货了吗"。查下去,38 次升级里 27 次是
 * 「同一问题重复3次未解决」——会话级判定,而候选是单轮用例,复现不了它。
 *
 * 后端已改成不再写这种不可复现的期望(见 tests/test_reflow_unreproducible_expectation.py)。
 * 但还剩两件只能在界面上说的事:
 *
 * ① `expected_intent` 仍然直接抄线上观测到的意图,而那可能本身就是误判(实测
 *    "你们几点上班"被记成 return_request)。照抄进回归集 = 把误判固化成标准答案,
 *    而回归集正是 skill 转正门禁的比较基准。
 * ② 丢掉了几条要说出来。不说的话,"共回流 N 条"读起来像"线上就这么点问题"。
 */
import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { EvalView } from "@/components/EvalView";

const CASE = {
  id: "reflow-abc", turns: ["我要投诉"],
  expected_intent: "complaint", expected_requires_human: true,
};

function stub(reflowBody: any) {
  vi.stubGlobal("fetch", vi.fn(async (url: any, init?: any) => {
    const u = String(url);
    if (u.includes("/api/reflow")) return { ok: true, json: async () => reflowBody };
    if (u.includes("/api/eval/status")) return { ok: true, json: async () => ({ status: "idle" }) };
    return { ok: true, json: async () => ({}) };      // baseline 等
  }));
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
});

describe("回流候选的采纳前提示", () => {
  it("说清期望取自线上实际行为、不是已验证的正确答案", async () => {
    stub({ count: 1, cases: [CASE], stats: { kept: 1, dropped_no_assertion: 0 } });
    render(<EvalView />);
    fireEvent.click(screen.getByText("回流一次"));

    const el = await screen.findByTestId("reflow-caveat");
    expect(el.textContent).toMatch(/实际发生的行为/);
    expect(el.textContent).toMatch(/人工核对|逐条/);
    // 说清后果:错的期望会进裁判尺
    expect(el.textContent).toMatch(/门禁|标准答案/);
  });

  it("丢弃的条数与样例要显示出来", async () => {
    stub({
      count: 1, cases: [CASE],
      stats: { kept: 1, dropped_no_assertion: 9,
               dropped_samples: ["你们几点上班", "我想问个事", "帮我看下订单发货了吗"] },
    });
    render(<EvalView />);
    fireEvent.click(screen.getByText("回流一次"));

    const el = await screen.findByTestId("reflow-dropped");
    expect(el.textContent).toMatch(/9/);
    expect(el.textContent).toMatch(/你们几点上班/);
    expect(el.textContent).toMatch(/会话级/);
  });

  it("没有丢弃时不显示那一段,不制造无谓噪音", async () => {
    stub({ count: 1, cases: [CASE], stats: { kept: 1, dropped_no_assertion: 0 } });
    render(<EvalView />);
    fireEvent.click(screen.getByText("回流一次"));

    await screen.findByTestId("reflow-caveat");
    expect(screen.queryByTestId("reflow-dropped")).toBeNull();
  });

  it("老后端没有 stats 时不崩、提示照常显示", async () => {
    stub({ count: 1, cases: [CASE] });
    render(<EvalView />);
    fireEvent.click(screen.getByText("回流一次"));

    await screen.findByTestId("reflow-caveat");
    expect(screen.queryByTestId("reflow-dropped")).toBeNull();
  });
});
