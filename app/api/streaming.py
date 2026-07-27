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
                        hitl=None, confirm: bool = False,
                        hmdp_token: str = "") -> Iterator[dict]:
    q: "queue.Queue" = queue.Queue()

    # 本轮授权的风险动作:显式 confirm 标志,或用户这轮说了确认语(退款/成交等才放行)
    from app.agent.consent import RISK_ACTIONS, consent_scope
    from app.agent.consent import confirm_targets_pending

    # Phase 4/R3:用户确认 + 会话状态里存在挂起动作 → 服务端确定性重放(不赌模型重调工具)
    # 挂起动作随会话持久化在 agent 上(R3),重启/换实例后仍在。
    _raw_pending = getattr(agent, "_pending", None)
    # 重放门:确认信号必须与挂起动作绑定,泛化词"可以/好的"不足以重放不可逆动作(P0-2)
    pending = _raw_pending if confirm_targets_pending(user_input, _raw_pending, confirm) else None
    # 本轮是否走确定性重放(仅当确认信号绑定到挂起动作)。风险动作只经此路径执行,
    # normal flow 恒不授权风险动作(见 _normal_flow 的空 consent_scope)。
    confirmed = pending is not None

    # Langfuse 桥(可选体验层):门控关/未装时为 None,零开销
    from app.observability.langfuse_bridge import langfuse_turn
    lf_turn = langfuse_turn(session_id, getattr(agent, "user_id", None), user_input)

    def sink(ev: dict) -> None:
        if tracer is not None:
            tracer.on_event(ev)
        if lf_turn is not None:
            lf_turn.on_event(ev)
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

    def _human_request_flow(_sink) -> str:
        """明确要求转人工:前置短路,零 LLM(文档9.④强制转人工词+8.3'必须明确提示正在转接')。"""
        reply = "好的,正在为您转接人工客服,请稍候~ 转接期间您可以继续补充问题,人工客服会看到完整对话记录。"
        recent = list(getattr(agent, "raw_messages", []))[-6:]
        hid = hitl.escalate(session_id, user_input, reply, "human_request", 1.0,
                            ["用户明确要求转人工"], recent_context=recent)
        from app.agent.memory.profile import record_ticket
        record_ticket(getattr(agent, "user_id", None), hid, "escalated",
                      "用户明确要求转人工")
        _sink({"type": "handoff", "reasons": ["用户明确要求转人工"], "handoff_id": hid})
        _sink({"type": "reply", "content": reply})
        _sink({"type": "metadata", "intent": "human_request", "confidence": 1.0,
               "requires_human": True, "follow_up_question": None})
        # 短路轮同样写进会话历史并落盘(照 app.py fast-path 先例):刷新可回显,
        # 下一轮 LLM 也知道用户刚要求过转接;失败不影响已发出的转接
        try:
            from app.schemas.response import CustomerServiceResponse, IntentType
            _res = CustomerServiceResponse(intent=IntentType.OTHER, confidence=1.0,
                                           reply=reply, requires_human=True,
                                           follow_up_question=None)
            agent.raw_messages.append({"role": "user", "content": user_input})
            agent.raw_messages.append({"role": "assistant",
                                       "content": _res.model_dump_json()})
            if hasattr(agent, "save"):
                agent.save()
        except Exception:
            pass
        return "human_request"

    def _normal_flow(_sink) -> str:
        # normal flow 恒不授权任何风险动作(风险动作只走 _replay_flow 的确定性重放)。
        # 空 scope 是 fail-closed 的显式表达:模型当轮无法直接执行退款/取消等不可逆动作。
        with consent_scope(frozenset()):
            result = agent.chat(user_input)
        # 观察/变换:输出护栏变换 + 事后升级判定,均不否决已发生的动作
        reply = _finalize(result.reply, _sink)
        _sink({"type": "reply", "content": reply})
        qu = getattr(agent, "_turn_qu", None)
        reasons = []
        if hitl is not None:
            all_user = [m.get("content", "") for m in getattr(agent, "raw_messages", [])
                        if m.get("role") == "user"]
            prior_user = all_user[:-1] if all_user else []   # 排除本轮
            reasons = hitl.evaluate(result.intent.value, result.confidence,
                                    result.requires_human, user_input=user_input,
                                    qu_intent=(qu.intent if qu is not None else ""),
                                    prior_user_msgs=prior_user)
            if reasons:
                # 升级动作整体兜底:escalate/记工单任一失败,不能吞掉后面的 metadata(F2)
                try:
                    recent = list(getattr(agent, "raw_messages", []))[-6:]
                    hid = hitl.escalate(session_id, user_input, reply,
                                        result.intent.value, result.confidence,
                                        reasons, recent_context=recent)
                    from app.agent.memory.profile import record_ticket
                    record_ticket(getattr(agent, "user_id", None), hid, "escalated",
                                  ";".join(reasons))
                    _sink({"type": "handoff", "reasons": reasons, "handoff_id": hid})
                except Exception:
                    _sink({"type": "handoff", "reasons": reasons, "handoff_id": None})
        # metadata 最后发:requires_human 反映事后升级结果,避免与 handoff 横幅自相矛盾
        requires_human_out = result.requires_human or bool(reasons)
        intent_out = result.intent.value
        if qu is not None and qu.intent == "投诉" and intent_out not in ("complaint",):
            intent_out = "complaint"   # QU 前置判定优先:情绪/投诉轮生成侧意图常漂移
        _sink({"type": "metadata", "intent": intent_out,
               "confidence": result.confidence,
               "requires_human": requires_human_out,
               "follow_up_question": result.follow_up_question})
        return intent_out   # P1:trace.intent(消费本返回值)与 metadata 用同一覆盖后意图,消除三面漂移

    def _replay_flow(_sink) -> str:
        """确认轮:用记住的真实参数,由服务端授权重放挂起动作(确定性,不经模型)。"""
        from app.agent.consent import consent_scope as _scope
        from app.agent.tools.bargain import set_current_session
        from app.agent.runtime_context import set_current_user
        from app.schemas.response import CustomerServiceResponse, IntentType

        _sink({"type": "tool_call", "name": pending.tool_name, "args": pending.args})
        set_current_session(session_id)   # negotiate_price 需要会话上下文
        set_current_user(getattr(agent, "user_id", None))   # P0-1:重放在新线程,须设身份否则 owned_order 判空
        # P1-③:走 agent 的 ToolManager(与 ReAct 同路径:MCP 身份透传/结果落盘一致),
        # 无 tool_manager(裸引擎/测试桩缺失)时回退 registry
        _tm = getattr(agent, "tool_manager", None)
        with _scope(RISK_ACTIONS):
            if _tm is not None and hasattr(_tm, "execute_tool"):
                result_str = _tm.execute_tool(pending.tool_name, pending.args)
            else:
                from app.agent.tools.registry import execute_tool as _exec
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
        from app.hitl.escalation import match_human_fast
        if hitl is not None and match_human_fast(user_input):
            return _human_request_flow(_sink)
        return _normal_flow(_sink)

    def worker():
        from contextlib import nullcontext
        # hmdp 凭据透传:在 worker 线程(ContextVar 按线程)设当前用户的 hmdp token,
        # 供 MCP 侧调 hmdp 登录保护接口(/order/**)携带;无则不影响公开接口。
        from app.agent.runtime_context import set_current_token
        set_current_token(hmdp_token or None)
        real_client = getattr(agent, "client", None)
        agent.event_sink = sink
        try:
            # Langfuse 根上下文须在 worker 线程内进入(OTel 上下文按线程传播,
            # drop-in 的 generation 才会嵌进本轮的阶段 span 下)
            with (lf_turn if lf_turn is not None else nullcontext()):
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
