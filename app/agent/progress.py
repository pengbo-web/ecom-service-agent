"""L3③:生成前进度事件——统一的"当前阶段→中文文案"映射与发射工具。

设计原则:文案必须**如实反映此刻正在做什么**,不是靠猜的花哨等待动效。
三个阶段对应问题描述里买家等待的真实来源(理解 → 检索 → 生成):
  understanding  查询理解 LLM 调用进行中
  retrieving     KB/知识检索进行中(domain 已知时给出更具体的文案)
  generating     即将/正在调用生成模型(ReAct 第一步)

domain 未知(查询理解尚未产出结果,或 QU 关闭)时用不带业务细节的通用文案,
不去猜"这轮到底是查订单还是查政策"——猜错了就是文案说谎,比空白等待更糟。

发出方式与既有事件通道一致(`event_sink`/`agent._emit`),新增帧类型
`{"type": "progress", "stage": ..., "message": ...}`,不改动任何既有帧字段。
"""

from app.config.settings import settings

_UNDERSTANDING_MSG = "正在理解您的问题…"
_GENERATING_MSG = "正在为您生成回复…"
_RETRIEVING_GENERIC_MSG = "正在为您检索相关信息…"
_RETRIEVING_BY_DOMAIN = {
    "midsale": "正在为您查询订单和物流信息…",
    "aftersale": "正在为您查询售后政策…",
    "presale": "正在为您查询商品与优惠信息…",
}


def retrieving_message(domain: str | None) -> str:
    """按已知 domain 给出具体一点的检索文案;domain 未知/不在三域内则用通用文案
    (宁可笼统,不可编造一个不确定的具体说法)。"""
    return _RETRIEVING_BY_DOMAIN.get(domain, _RETRIEVING_GENERIC_MSG)


def stage_message(stage: str, domain: str | None = None) -> str:
    if stage == "understanding":
        return _UNDERSTANDING_MSG
    if stage == "generating":
        return _GENERATING_MSG
    if stage == "retrieving":
        return retrieving_message(domain)
    return ""


def emit_progress(sink, stage: str, domain: str | None = None) -> None:
    """按 settings.progress_events_enabled 门控发一条 progress 帧;sink 为 None
    (未接事件流/裸引擎测试)时直接跳过。fail-soft:sink 调用本身出错不影响主流程
    ——与项目里其它旁路埋点(_record_skill_turn 等)同姿态。"""
    if not settings.progress_events_enabled or sink is None:
        return
    try:
        sink({"type": "progress", "stage": stage, "message": stage_message(stage, domain)})
    except Exception:  # noqa: BLE001 进度提示是体验增强,不能影响主流程
        pass


# 阶段序号:进度帧必须单调不倒退——买家看到"退回上一阶段"的提示,比什么都
# 不显示更糟(这正是本任务的字面要求)。数字越大越接近生成完成。
STAGE_RANK: dict[str, int] = {"understanding": 0, "retrieving": 1, "generating": 2}


class ProgressStageGate:
    """L3③ 防御性兜底(实测发现的真实回归:并发预取的结果解析顺序与代码
    路径的直觉顺序不一致时,曾经出现过 generating 之后又发一条 retrieving
    的倒序帧)。根因修复是让"retrieving"在代码结构上必然发生在"generating"
    之前(见 EcomAgent._react_loop/_build_messages);这层是双保险——哪怕
    未来某处改动又引入了乱序,买家也绝不会看到阶段倒退,顶多是"该有的一条
    提示没发"(不完整,但不撒谎)。

    每轮开始时必须调 `reset()`(_turn_progress_gate 由 EcomAgent 每轮重建,
    见 chat()),否则上一轮的阶段位置会残留到下一轮,把这一轮合法的
    "retrieving"误判成"倒退"而吞掉。
    """

    def __init__(self):
        self._last_rank = -1

    def reset(self) -> None:
        self._last_rank = -1

    def emit(self, sink, stage: str, domain: str | None = None) -> None:
        rank = STAGE_RANK.get(stage, 0)
        if rank < self._last_rank:
            return   # 倒退——吞掉不发,不让买家看到"退回上一阶段"的假象
        self._last_rank = rank
        emit_progress(sink, stage, domain)
