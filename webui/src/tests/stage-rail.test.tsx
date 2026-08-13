/**
 * 协作链阶段轨:把"走到哪一步、卡在哪、在等谁"变成一眼能看懂的东西。
 *
 * **用户反馈**:「看不清 agent 的流转过程」。改造前列表上每条链只有
 * 「客服 Agent → 参谋 Agent · 1 个事件」——实测 30 条里 28 条长得一模一样,
 * 因为 worker 停摆、它们全卡在第一步。**看不出卡住,等于这个页面没在报信。**
 *
 * 三条语义是这次改动的全部要点,每条都有测试钉住:
 *  1. 未到达 ≠ 故障(降级诊断按规则不唤醒营销,链正常停在第 2 步);
 *  2. pending 表示事件**已产出、等收件人处理**——不是"等它产出"(方向别反);
 *  3. 拿不到进度数据要说"进度未知",**不能说成"尚未开始"**(那是编造)。
 */
import { describe, expect, it } from "vitest";
import { deriveStages, stageSummary } from "@/components/collab/StageRail";

const S = "signal.anomaly", D = "insight.diagnosis", A = "action.drafts_ready",
      T = "result.outreach_sent", C = "result.outreach_converted";

describe("deriveStages", () => {
  it("按事件类型还原五步", () => {
    const st = deriveStages([S, D], ["done", "done"], ["analyst", "growth"]);
    expect(st.map((x) => x.state)).toEqual(["done", "done", "missing", "missing", "missing"]);
  });

  it("同一步多行时取最坏状态", () => {
    // 一步里只要有一条没走完,这一步就不算走完——显示成 done 会让人以为可以往下看
    const st = deriveStages([S, S], ["done", "pending"], ["analyst", "analyst"]);
    expect(st[0].state).toBe("active");
  });

  it("failed 压过 pending", () => {
    const st = deriveStages([S, S], ["pending", "failed"], ["analyst", "analyst"]);
    expect(st[0].state).toBe("failed");
  });

  it("converted 与 no_change 合并成同一步", () => {
    expect(deriveStages(["result.outreach_no_change"], ["done"], ["human"])[4].state).toBe("done");
    expect(deriveStages([C], ["done"], ["human"])[4].state).toBe("done");
  });

  it("pending 才记 waitingOn;done 不记", () => {
    expect(deriveStages([S], ["pending"], ["analyst"])[0].waitingOn).toBe("参谋 Agent");
    expect(deriveStages([S], ["done"], ["analyst"])[0].waitingOn).toBeUndefined();
  });
});

describe("stageSummary", () => {
  it("拿不到数据说「进度未知」,不说「尚未开始」", () => {
    // 实测踩过:API 进程跑的是加字段之前的代码,30 条链齐刷刷写着"尚未开始",
    // 而它们其实都有待处理事件。把"我不知道"说成"还没开始"就是编造。
    expect(stageSummary(deriveStages(undefined, undefined, undefined)))
      .toEqual({ text: "进度未知", tone: "unknown" });
  });

  it("pending 说的是「等谁处理」,不是「等它产出」", () => {
    // 早先写成"等待:转化归因(归因 worker)",而那些事件正是归因 worker 产出的、
    // 在等人看——方向反了。
    const st = deriveStages([C], ["pending"], ["human"]);
    expect(stageSummary(st).text).toBe("等 人工 处理:转化归因");
  });

  it("失败优先于等待", () => {
    const st = deriveStages([S, D], ["failed", "pending"], ["analyst", "growth"]);
    expect(stageSummary(st)).toMatchObject({ text: "失败:发现异常", tone: "bad" });
  });

  it("正常终止说「止于」而不是「卡在」", () => {
    // 降级诊断不转营销,链停在第 1 步是**设计如此**,措辞不能把它说成故障
    const st = deriveStages([S], ["done"], ["analyst"]);
    expect(stageSummary(st)).toEqual({ text: "止于:发现异常", tone: "ok" });
  });

  it("走完第五步才算已闭环", () => {
    const st = deriveStages([S, D, A, T, C], Array(5).fill("done"),
                            ["analyst", "growth", "human", "analyst", "human"]);
    expect(stageSummary(st)).toEqual({ text: "已闭环", tone: "ok" });
  });

  it("被消费闸拦下不算失败", () => {
    const st = deriveStages([S, D], ["done", "skipped"], ["analyst", "growth"]);
    expect(stageSummary(st)).toMatchObject({ text: "已拦下:参谋归因", tone: "wait" });
  });
});
