"""把阻塞式 agent.chat() 桥接成 SSE 事件生成器（W2 tracer + W3 guardrails/consent 可选）。"""

import json
import queue
import threading
from typing import Iterator

from app.guardrails.base import SAFE_FALLBACK

_SENTINEL = object()


def _build_confirm_reply(action: str, result: dict) -> str:
    """据重放工具的真实返回,拼一句确定性回复(不泄漏内部字段如议价 rationale)。"""
    if not result.get("success"):
        # 已在处理中/订单不存在等:如实转达工具给的原因
        return result.get("message") or result.get("error") or "抱歉,该操作暂时无法完成,请稍后再试或转人工。"
    if action == "refund":
        return "✅ " + result.get("message", "退款申请已提交,预计 1-3 个工作日内审核完成。")
    if action == "deal_close":
        price = result.get("suggested_price")
        name = result.get("product_name", "该商品")
        return f"✅ 已为您锁定「{name}」的成交价 ¥{price},即将为您生成订单,请稍候完成支付～"
    # 其它风险动作(取消订单/改地址等):直接用工具返回的真实消息
    return "✅ " + (result.get("message") or "操作已完成。")


def run_agent_streaming(agent, user_input: str, tracer=None,
                        session_id: str = "", guard_pipeline=None,
                        hitl=None, confirm: bool = False) -> Iterator[dict]:
    q: "queue.Queue" = queue.Queue()

    # 本轮授权的风险动作:显式 confirm 标志,或用户这轮说了确认语(退款/成交等才放行)
    from app.agent.consent import RISK_ACTIONS, consent_scope, is_confirmation
    confirmed = confirm or is_confirmation(user_input)
    granted = RISK_ACTIONS if confirmed else frozenset()

    # Phase 4/R3:用户确认 + 会话状态里存在挂起动作 → 服务端确定性重放(不赌模型重调工具)
    # 挂起动作随会话持久化在 agent 上(R3),重启/换实例后仍在。
    pending = getattr(agent, "_pending", None) if confirmed else None

    def sink(ev: dict) -> None:
        if tracer is not None:
            tracer.on_event(ev)
        q.put(ev)

    # ── 观察/变换阶段(observe):不能否决已发生的动作,只做变换与埋点 ──
    def _finalize(reply: str, _sink) -> str:
        """输出护栏 = finalize(text)->text 变换缝:改写文案、发观察事件,不改动作结果。"""
        if guard_pipeline is None:
            return reply
        reply, out_results = guard_pipeline.check_output(reply)
        for gr in out_results:
            _sink({"type": "guard", "stage": "output", "action": gr.action,
                   "guard": gr.guard, "reason": gr.reason})
        return reply

    def _blocked_flow(_sink, gr) -> str:
        _sink({"type": "guard", "stage": "input", "action": "block",
               "guard": gr.guard, "reason": gr.reason})
        _sink({"type": "reply", "content": SAFE_FALLBACK})
        _sink({"type": "metadata", "intent": "blocked", "confidence": 1.0,
               "requires_human": False, "follow_up_question": None})
        return "blocked"

    def _normal_flow(_sink) -> str:
        # 授权闸门(authorize):风险动作前置授权在动作边界强制(consent_scope)
        with consent_scope(granted):
            result = agent.chat(user_input)
        # 观察/变换:输出护栏变换 + 事后升级判定,均不否决已发生的动作
        reply = _finalize(result.reply, _sink)
        _sink({"type": "reply", "content": reply})
        _sink({"type": "metadata", "intent": result.intent.value,
               "confidence": result.confidence,
               "requires_human": result.requires_human,
               "follow_up_question": result.follow_up_question})
        if hitl is not None:
            reasons = hitl.evaluate(result.intent.value, result.confidence,
                                    result.requires_human, user_input=user_input)
            if reasons:
                recent = list(getattr(agent, "raw_messages", []))[-6:]
                hid = hitl.escalate(session_id, user_input, reply,
                                    result.intent.value, result.confidence,
                                    reasons, recent_context=recent)
                from app.agent.memory.profile import record_ticket
                record_ticket(getattr(agent, "user_id", None), hid, "escalated",
                              ";".join(reasons))
                _sink({"type": "handoff", "reasons": reasons, "handoff_id": hid})
        return result.intent.value

    def _replay_flow(_sink) -> str:
        """确认轮:用记住的真实参数,由服务端授权重放挂起动作(确定性,不经模型)。"""
        from app.agent.consent import consent_scope as _scope
        from app.agent.tools.registry import execute_tool as _exec
        from app.agent.tools.bargain import set_current_session
        from app.schemas.response import CustomerServiceResponse, IntentType

        _sink({"type": "tool_call", "name": pending.tool_name, "args": pending.args})
        set_current_session(session_id)   # negotiate_price 需要会话上下文
        with _scope(RISK_ACTIONS):
            result_str = _exec(pending.tool_name, pending.args)
        _sink({"type": "tool_result", "content": result_str})
        try:
            result = json.loads(result_str)
        except (ValueError, TypeError):
            result = {}

        reply = _finalize(_build_confirm_reply(pending.action, result), _sink)

        intent = IntentType.AFTER_SALE if pending.action == "refund" else IntentType.PRODUCT_CONSULT
        _sink({"type": "reply", "content": reply})
        _sink({"type": "metadata", "intent": intent.value, "confidence": 1.0,
               "requires_human": False, "follow_up_question": None})

        # 重放成功即清挂起,防二次"确认"重复执行;失败(如已在处理中)也清,避免卡死
        agent._pending = None

        # 把这一轮写回 Agent 历史,保持后续对话上下文连贯
        resp = CustomerServiceResponse(intent=intent, confidence=1.0, reply=reply,
                                       requires_human=False, follow_up_question=None)
        msgs = getattr(agent, "raw_messages", None)
        if isinstance(msgs, list):
            msgs.append({"role": "user", "content": user_input})
            msgs.append({"role": "assistant", "content": resp.model_dump_json()})
            save = getattr(agent, "save", None)
            if callable(save):
                try:
                    save()
                except Exception:  # noqa: BLE001  保存失败不影响本轮回复
                    pass
        return intent.value

    def _drive(_sink) -> str:
        if pending is not None:
            return _replay_flow(_sink)
        if guard_pipeline is not None:
            gin = guard_pipeline.check_input(user_input)
            if gin.action == "block":
                return _blocked_flow(_sink, gin)
        return _normal_flow(_sink)

    def worker():
        real_client = getattr(agent, "client", None)
        agent.event_sink = sink
        try:
            if tracer is None:
                _drive(sink)
            else:
                from app.observability.client_proxy import TracingClient
                with tracer.start_trace(session_id, user_input) as trace:
                    if real_client is not None:
                        agent.client = TracingClient(real_client, tracer)
                    try:
                        trace.intent = _drive(sink)
                    finally:
                        if real_client is not None:
                            agent.client = real_client
        except Exception as e:  # noqa: BLE001
            q.put({"type": "error", "message": str(e)})
        finally:
            agent.event_sink = None
            q.put(_SENTINEL)

    threading.Thread(target=worker, daemon=True).start()

    while True:
        event = q.get()
        if event is _SENTINEL:
            yield {"type": "done"}
            return
        yield event
