/**
 * 坐席工作台的会话面板必须**关得住高度**:消息再多也不能撑破网格。
 *
 * **实测缺陷**(用户报的)。会话消息一多:
 *
 *     容器(main/grid)  h = 663
 *     MessageThread     h = 3113   ← 撑破了
 *     输入框            top = 3101 (视口 720) ← 看不见
 *     页面              canScroll = false     ← AppShell 的 main 是 overflow-hidden
 *
 * 结果:坐席**既看不到输入框、也滚不动**,这一路会话等于废了。
 *
 * 原因是 CSS 的默认行为——grid/flex item 的**自动最小尺寸等于内容尺寸**,所以子元素
 * 写了 `h-full` 也不肯缩到容器高度以下。必须两处都给:
 *
 *   - 网格容器 `grid-rows-[minmax(0,1fr)]` —— 行轨道不许超过容器;
 *   - 每个 item `min-h-0` —— 允许它缩下去。
 *
 * 只给其中一处仍然溢出(实测)。
 *
 * jsdom 不做布局,量不到真实高度,所以这里断言的是**那两个类名还在**。这不是理想的
 * 测试(它盯的是实现而不是效果),但比没有强:真实修复已经在浏览器里量过
 * (thread 663 = 容器高、输入框 bottom 708 < 720、消息区 532/2299 且滚动生效),
 * 这条只负责在有人顺手删掉这些类名时立刻报警。
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const read = (p: string) => readFileSync(resolve(__dirname, "../components", p), "utf-8");

describe("坐席工作台布局约束", () => {
  it("网格行轨道不许被内容顶开", () => {
    const src = read("WorkbenchView.tsx");
    expect(src).toMatch(/grid-rows-\[minmax\(0,1fr\)\]/);
  });

  it("三列 item 都要能缩到内容高度以下", () => {
    // 只修中间那列不够:左右两列各自也有长列表,今天没炸只是因为它们内部先有了
    // 滚动条——那是运气,不是设计。
    for (const [file, marker] of [
      ["workbench/MessageThread.tsx", "flex h-full min-h-0 flex-col"],
      ["workbench/ConversationList.tsx", "flex h-full min-h-0 flex-col border-r"],
      ["workbench/ContextPanel.tsx", "hidden h-full min-h-0 flex-col"],
    ] as const) {
      expect(read(file), file).toContain(marker);
    }
  });

  it("消息区是 flex-1 + min-h-0 的滚动容器,输入框在它外面", () => {
    const src = read("workbench/MessageThread.tsx");
    // 滚动区要能缩;输入框必须是根节点的兄弟(不在滚动区里),否则会跟着一起滚走
    expect(src).toMatch(/<ScrollArea className="min-h-0 flex-1">/);
    const scrollEnd = src.indexOf("</ScrollArea>");
    const textareaAt = src.indexOf("<textarea");
    expect(scrollEnd).toBeGreaterThan(0);
    expect(textareaAt).toBeGreaterThan(scrollEnd);   // 输入框在滚动区之后
  });
});
