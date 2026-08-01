import { describe, it, expect } from "vitest";
import { initials, statusMeta, avatarColor } from "@/components/workbench/parts";

describe("workbench parts", () => {
  it("initials takes first chars", () => {
    expect(initials("小鱼同学")).toBe("小鱼");
    expect(initials("alice")).toBe("AL");
  });
  it("statusMeta maps manual/ai/closed", () => {
    expect(statusMeta({ status: "open", manual: true }).tone).toBe("manual");
    expect(statusMeta({ status: "open", manual: false }).tone).toBe("ai");
    expect(statusMeta({ status: "closed", manual: false }).tone).toBe("closed");
  });
  it("avatarColor is stable per seed", () => {
    expect(avatarColor("alice")).toBe(avatarColor("alice"));
  });
});
