import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { ServiceQualityTable } from "@/components/OperationsView";
import type { SkillQuality } from "@/lib/api";

/** 按 skill 的服务质量表。
 *
 * 补这张表的硬理由:告警的判定窗已经收到近 1 天(见 AnomalyScopeNote),那么
 * "某个 skill 前几天坏过、今天已经好了"就只能靠这张按经营窗统计的表来看。
 * 没有它,缩窗等于用"少报假警"换"看不见历史"——那不算修好。
 *
 * 数据后端一直在返回(quality.skills),前端此前只取了同一响应里的 emotion。
 */
const rows: SkillQuality[] = [
  { skill_name: "track-order", total: 47, success_rate: 0.213,
    tool_error_rate: 0.787, human_rate: 0, other: 0 },
  { skill_name: "process-return", total: 32, success_rate: 0.969,
    tool_error_rate: 0.031, human_rate: 0, other: 0 },
];

describe("ServiceQualityTable", () => {
  it("每个 skill 一行，把跨线异常区看不到的分布摆出来", () => {
    render(<ServiceQualityTable skills={rows} windowDays={7} />);
    expect(screen.getAllByTestId("service-quality-row")).toHaveLength(2);
    expect(screen.getByText("track-order")).toBeInTheDocument();
    expect(screen.getByText("78.7%")).toBeInTheDocument();
  });

  it("明说这是历史分布而不是告警——否则和「当前无跨线异常」同屏就像看板出错", () => {
    render(<ServiceQualityTable skills={rows} windowDays={7} />);
    const el = screen.getByTestId("service-quality-table");
    expect(el.textContent).toContain("不是告警");
    expect(el.textContent).toContain("今天已恢复");
    expect(el.textContent).toContain("统计窗口 7 天");
  });

  it("other 非零时说出来：那些执行不进任何比率，但计入了执行次数", () => {
    render(<ServiceQualityTable windowDays={7} skills={[
      { skill_name: "x", total: 10, success_rate: 0.5,
        tool_error_rate: 0.2, human_rate: 0.1, other: 2 },
    ]} />);
    expect(screen.getByTestId("service-quality-table").textContent)
      .toContain("另有 2 次执行的 outcome");
  });

  it("other 全为 0 时不显示那句话，不给看板加噪声", () => {
    render(<ServiceQualityTable skills={rows} windowDays={7} />);
    expect(screen.getByTestId("service-quality-table").textContent)
      .not.toContain("另有");
  });

  it("没有执行记录时走空态，不渲染一张空表", () => {
    render(<ServiceQualityTable skills={[]} windowDays={7} />);
    expect(screen.getByTestId("service-quality-empty").textContent)
      .toContain("过去 7 天没有 skill 执行记录");
  });

  it("老响应体没有 skills 键时不崩（后端刚补上，前端不假设它一定在）", () => {
    render(<ServiceQualityTable skills={undefined} windowDays={7} />);
    expect(screen.getByTestId("service-quality-empty")).toBeInTheDocument();
  });
});
