/**
 * 数据还没到 ≠ 数量是 0。
 *
 * **实测缺陷**(前端体验时抓到)。打开「我的订单」,加载要 ~3 秒,而这期间标题一直
 * 写着「0 笔」——买家看到的是"我没有订单",而他其实有两笔。
 *
 * 全仓这个形态有 6 处(`{x?.length ?? 0}`):我的订单、协作链、跨线异常、现行技能、
 * 待审候选、活跃灰度。都是同一句话说了两件事:
 *
 *     null → 还不知道       0 → 确实没有
 *
 * 用 `?? 0` 把前者压成后者,等于**在自己都还不知道的时候先给一个确定的答案**。
 * 与本仓库既有纪律同源:`/api/products` 失败时不能用空列表冒充"这家店没商品";
 * 购物车取不到价格时给 `null` 而不是 0,因为「¥0」会被买家当真。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { countLabel } from "@/lib/count";

describe("countLabel", () => {
  it("未知显示破折号,不显示 0", () => {
    expect(countLabel(null)).toBe("—");
    expect(countLabel(undefined)).toBe("—");
  });

  it("真的是 0 就显示 0", () => {
    // 反向断言:不能为了躲开"假 0"而把真 0 也藏掉——那样买家就看不到"确实没有"了
    expect(countLabel(0)).toBe("0");
  });

  it("有数字就原样显示", () => {
    expect(countLabel(2)).toBe("2");
    expect(countLabel(391)).toBe("391");
  });
});

describe("六处调用点都改过来了", () => {
  it("没有残留的 `?? 0` 计数写法", () => {
    // 盯的是实现,但这类缺陷只在"加载中"那一两秒可见,靠人复现极不稳定;
    // 而它的根因是一个可以文本检出的固定写法。
    const files = ["OrdersView.tsx", "CollabView.tsx", "OperationsView.tsx", "SkillsView.tsx"];
    for (const f of files) {
      const src = readFileSync(resolve(__dirname, "../components", f), "utf-8");
      const bad = src.match(/\?\?\s*0\}\s*(笔|条|件|）)/g) || [];
      expect(bad, `${f} 仍有 ?? 0 计数`).toEqual([]);
      expect(src).toContain("countLabel(");
    }
  });
});
