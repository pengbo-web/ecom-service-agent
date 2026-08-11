import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { AnomalyScopeNote } from "@/components/OperationsView";

/** 异常扫描的口径与盲区必须显示出来。
 *
 * 背景(实测):告警按近况判(服务健康默认近 1 天),而同一页上的「服务质量」卡
 * 按经营窗(7 天)算。两个数不一样是正常的——"7 天里坏过、今天已经好了"。不标
 * 出两个窗口,店主看到「工具失败率 78%」却「当前无跨线异常」只会得出一个结论:
 * 这看板不准。
 */
const scope = {
  window_days: 7,
  service_window_days: 1,
  service_insufficient: [] as Array<{ skill_name: string; total: number; min_samples: number }>,
  products_examined: 20,
  products_truncated: false,
  reviews_examined: 50,
  reviews_truncated: false,
};

describe("AnomalyScopeNote", () => {
  it("两个窗口都标出来，不让人以为只有一个口径", () => {
    render(<AnomalyScopeNote scope={scope} />);
    const el = screen.getByTestId("anomaly-scope");
    expect(el.textContent).toContain("近 1 天");
    expect(el.textContent).toContain("近 7 天");
  });

  it("近窗样本不足的 skill 如实列出——「没报警」和「没数据所以报不了警」是两件事", () => {
    render(<AnomalyScopeNote scope={{
      ...scope,
      service_insufficient: [{ skill_name: "track-order", total: 2, min_samples: 5 }],
    }} />);
    const el = screen.getByTestId("anomaly-scope");
    expect(el.textContent).toContain("无法判定");
    expect(el.textContent).toContain("track-order（2/5）");
  });

  it("样本充足时不显示那一行，不给看板加噪声", () => {
    render(<AnomalyScopeNote scope={scope} />);
    expect(screen.getByTestId("anomaly-scope").textContent).not.toContain("无法判定");
  });

  it("扫描被截断时说出来，不让「扫过一遍」看起来像「全店都看过了」", () => {
    render(<AnomalyScopeNote scope={{ ...scope, products_truncated: true }} />);
    const el = screen.getByTestId("anomaly-scope");
    expect(el.textContent).toContain("只看了前 20 名");
    expect(el.textContent).toContain("长尾未进入阈值判断");
  });

  it("后端没给 scope 时整块不渲染（旧响应兼容，不炸也不编）", () => {
    render(<AnomalyScopeNote scope={undefined} />);
    expect(screen.queryByTestId("anomaly-scope")).toBeNull();
  });
});
