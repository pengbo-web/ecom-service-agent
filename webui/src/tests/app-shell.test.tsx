import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, within } from "@testing-library/react";
import { AppShell } from "@/components/AppShell";

// 导航按**谁在用**分组,不是按功能类型。原来九个 tab 平铺在顶栏,买家的
// "我的订单"和运维的"评估"并排——看不出这个系统同时服务三类角色,而那正是
// 它的架构。这里钉的是分组存在、深色可切、窄屏不挤压。

function shell(over: Partial<React.ComponentProps<typeof AppShell>> = {}) {
  return render(
    <AppShell view="chat" onView={() => {}} onReset={() => {}} {...over}>
      <div>内容</div>
    </AppShell>
  );
}

/** happy-dom 的 matchMedia 默认 matches=false;窄屏用例要显式伪造。 */
function stubMatchMedia(matches: boolean) {
  vi.stubGlobal("matchMedia", (query: string) => ({
    matches, media: query,
    addEventListener: () => {}, removeEventListener: () => {},
    addListener: () => {}, removeListener: () => {},
  }));
}

describe("AppShell 导航", () => {
  beforeEach(() => { localStorage.clear(); stubMatchMedia(false); });
  afterEach(() => { vi.unstubAllGlobals(); document.documentElement.className = ""; });

  it("按角色分四组,而不是平铺", () => {
    shell();
    for (const g of ["买家侧", "坐席侧", "经营侧", "系统侧"]) {
      expect(screen.getByText(g)).toBeInTheDocument();
    }
  });

  it("多智能体协作入口在经营侧", () => {
    shell();
    expect(screen.getByText("多智能体协作")).toBeInTheDocument();
  });

  it("购物车入口受开关控制,关闭时退回改造前的样子", () => {
    // N5:购物车是全新入口,不能靠"后端永远不会产生 unpaid 订单"隐式兜底。
    const { unmount } = shell({ showCart: false });
    expect(screen.queryByText("购物车")).toBeNull();
    unmount();
    shell({ showCart: true });
    expect(screen.getByText("购物车")).toBeInTheDocument();
  });

  it("购物车徽标在有件数时才出现", () => {
    shell({ showCart: true, cartCount: 3 });
    expect(screen.getByText("3")).toBeInTheDocument();
  });

  it("选中项标记 aria-current,不只靠背景色", () => {
    // 纯背景差在投影仪/低对比度屏幕上经常看不出来,而"我现在在哪一页"不该靠猜。
    shell({ view: "collab" });
    const active = document.querySelector('[aria-current="page"]');
    expect(active).not.toBeNull();
    expect(within(active as HTMLElement).getByText("多智能体协作")).toBeInTheDocument();
  });

  it("顶栏显示当前页标题与说明", () => {
    shell({ view: "collab" });
    expect(screen.getByRole("heading", { name: "多智能体协作" })).toBeInTheDocument();
  });
});

describe("AppShell 主题", () => {
  beforeEach(() => { localStorage.clear(); stubMatchMedia(false); });
  afterEach(() => { vi.unstubAllGlobals(); document.documentElement.className = ""; });

  it("切换深色会打上 dark 类并记住选择", () => {
    shell();
    fireEvent.click(screen.getByLabelText("切换主题"));
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    expect(localStorage.getItem("xiaoxi_theme")).toBe("dark");
  });

  it("已有选择时不跟随系统", () => {
    // 控制台常投在会议室的屏上,必须能手动定住,不能被系统偏好覆盖。
    localStorage.setItem("xiaoxi_theme", "light");
    stubMatchMedia(true);            // 系统是深色
    shell();
    expect(document.documentElement.classList.contains("dark")).toBe(false);
  });
});

describe("AppShell 窄屏", () => {
  beforeEach(() => { localStorage.clear(); });
  afterEach(() => { vi.unstubAllGlobals(); document.documentElement.className = ""; });

  it("窄屏默认收起侧栏", () => {
    // 固定 224px 的侧栏在 ~1000px 以下会吃掉三分之一横向空间,把内容区挤到
    // 标题竖排断行——控制台里的表格与时间线本来就横向吃紧。
    stubMatchMedia(true);
    shell();
    expect(screen.getByTitle("展开侧栏")).toBeInTheDocument();
    // 收起后只留图标,文字标签不再占位
    expect(screen.queryByText("客服工作台")).toBeNull();
  });

  it("宽屏默认展开", () => {
    stubMatchMedia(false);
    shell();
    expect(screen.getByTitle("收起侧栏")).toBeInTheDocument();
    expect(screen.getByText("客服工作台")).toBeInTheDocument();
  });

  it("窄屏下仍可手动展开(自动只是默认值,不是锁)", () => {
    stubMatchMedia(true);
    shell();
    fireEvent.click(screen.getByTitle("展开侧栏"));
    expect(screen.getByText("客服工作台")).toBeInTheDocument();
  });
});
