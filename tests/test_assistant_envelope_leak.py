"""内部 JSON 信封不能被模型学去、也不能出现在买家眼前。

**实测缺陷**:买家发「知道了」,收到的回复是——

    好的，感谢您的理解与支持。如有其他问题，随时欢迎联系我。

    {"intent": "acknowledgement", "confidence": 1.0, "reply": "好的，感谢…",
     "requires_human": false, "follow_up_question": null}

内部字段(置信度、是否转人工)直接摆到买家眼前,正文还重复了两遍。

**根因不在护栏、也不在解析**:`chat.py` 那句
`raw_messages.append({"role": "assistant", "content": result.model_dump_json()})`
把整个结构化响应的 JSON dump 写进了对话历史。于是历史里每轮 assistant 都长成
JSON,模型下一轮照着历史的格式模仿。实测口径:全部会话 221 条 assistant 消息里
100 条(45%)是 JSON 信封,而且**混着**纯文本——这恰恰是格式模仿最容易出错的情形。

**修在送模型的那条边界上,而不是改持久化格式**,因为那个 JSON 是承重的:
`api/history.py`、`skills/golden_corpus.py`、`skills/user_modeling.py`、
`evaluation/trace_to_case.py` 四处都在 `json.loads` 它。所以本组测试有两半:
解包要对,**持久化格式一个字节都不能变**。
"""

import json

from app.agent.history_utils import sanitize_tool_pairs, unwrap_assistant_envelope


ENVELOPE = json.dumps({
    "intent": "acknowledgement", "confidence": 1.0,
    "reply": "好的，感谢您的理解与支持。如有其他问题，随时欢迎联系我。",
    "requires_human": False, "follow_up_question": None,
}, ensure_ascii=False)


# --------------------------------------------------------------------------
# 解包本身
# --------------------------------------------------------------------------

def test_envelope_becomes_the_plain_reply():
    """模型看到的历史里,assistant 只能是买家看到的那句话。"""
    out = unwrap_assistant_envelope([
        {"role": "user", "content": "知道了"},
        {"role": "assistant", "content": ENVELOPE},
    ])
    assert out[1]["content"] == "好的，感谢您的理解与支持。如有其他问题，随时欢迎联系我。"
    assert "requires_human" not in out[1]["content"]
    assert "confidence" not in out[1]["content"]


def test_no_internal_field_survives_anywhere():
    """整条送模型的消息序列里不该再出现任何内部字段名——模型学不到就不会模仿。"""
    out = unwrap_assistant_envelope([
        {"role": "assistant", "content": ENVELOPE},
        {"role": "user", "content": "再问一句"},
        {"role": "assistant", "content": ENVELOPE},
    ])
    blob = json.dumps(out, ensure_ascii=False)
    for field in ("requires_human", "follow_up_question", "confidence", '"intent"'):
        assert field not in blob, f"{field} 泄漏进了送模型的历史"


def test_plain_text_reply_untouched():
    """纯文本回复原样保留——快路径/护栏/转人工那几条分支写的就是纯文本。"""
    msgs = [{"role": "assistant", "content": "好的～有需要随时喊我 😊"}]
    assert unwrap_assistant_envelope(msgs) == msgs


def test_tool_call_message_untouched():
    """带 tool_calls 的 assistant 消息(content 为空)绝不能被碰坏,
    否则 sanitize_tool_pairs 的配对自愈会连带出问题。"""
    msgs = [{"role": "assistant", "content": None,
             "tool_calls": [{"id": "c1", "function": {"name": "query_order",
                                                      "arguments": "{}"}}]}]
    assert unwrap_assistant_envelope(msgs) == msgs


def test_malformed_json_untouched():
    """解析不出来的畸形内容原样保留:宁可漏解包一条(退回改造前的行为),
    也不能把一条正常消息弄坏。"""
    msgs = [{"role": "assistant", "content": '{"reply": 这不是合法 JSON'}]
    assert unwrap_assistant_envelope(msgs) == msgs


def test_json_without_reply_key_untouched():
    """长得像 JSON 但没有 reply 字段的,不动——它可能是买家问题里粘的一段 JSON。"""
    msgs = [{"role": "assistant", "content": '{"foo": 1}'}]
    assert unwrap_assistant_envelope(msgs) == msgs


def test_non_string_reply_untouched():
    """reply 不是字符串时不动:替换进去会让 content 变成非法类型,模型侧直接 400。"""
    msgs = [{"role": "assistant", "content": json.dumps({"reply": {"a": 1}})}]
    assert unwrap_assistant_envelope(msgs) == msgs


def test_user_message_with_json_untouched():
    """买家自己粘了一段带 reply 的 JSON 进来,不该被当成信封解包。"""
    msgs = [{"role": "user", "content": ENVELOPE}]
    assert unwrap_assistant_envelope(msgs) == msgs


def test_does_not_mutate_input():
    """纯函数:不能就地改调用方的 raw_messages(那是要落库的那份)。"""
    original = {"role": "assistant", "content": ENVELOPE}
    msgs = [original]
    unwrap_assistant_envelope(msgs)
    assert original["content"] == ENVELOPE, "就地改了 raw_messages,会污染持久化"


def test_composes_with_sanitize_tool_pairs():
    """两个净化步骤串起来仍然正确(_build_messages 里就是这么用的)。"""
    out = sanitize_tool_pairs(unwrap_assistant_envelope([
        {"role": "user", "content": "查订单"},
        {"role": "assistant", "content": ENVELOPE},
    ]))
    assert out[-1]["content"].startswith("好的")


# --------------------------------------------------------------------------
# 持久化格式不能变(四个消费方的依据)
# --------------------------------------------------------------------------

def test_persisted_history_still_carries_the_envelope():
    """`api/history.py` 等四处消费方靠 json.loads 读这个信封取元数据。

    这条断言是**反向**的:它保证这次修复没有顺手把持久化格式改掉。如果哪天有人
    "顺手"把 chat.py 里那句 model_dump_json 换成纯文本,前端历史回显、金语料、
    用户建模、评估用例抽取会同时失效——而且是静默失效(解析不出来就当没有元数据)。
    """
    import ast
    import inspect
    import app.agent.chat as chat_mod

    src = inspect.getsource(chat_mod)
    tree = ast.parse(src)
    dumps = [n for n in ast.walk(tree)
             if isinstance(n, ast.Attribute) and n.attr == "model_dump_json"]
    assert dumps, (
        "chat.py 里不再有 result.model_dump_json() —— 持久化格式可能被改成了纯文本。"
        "若确实要改,必须同时改 api/history.py、skills/golden_corpus.py、"
        "skills/user_modeling.py、evaluation/trace_to_case.py 这四个 json.loads 的消费方")


def test_build_messages_applies_the_unwrap():
    """钉住接线:解包必须真的挂在送模型的那条路上,否则这组测试全绿也没用。"""
    import ast
    import inspect
    import app.agent.chat as chat_mod

    tree = ast.parse(inspect.getsource(chat_mod))
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "_build_messages"), None)
    assert fn is not None, "_build_messages 不见了,这道接线断言需要跟着改"
    called = {n.func.id for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "unwrap_assistant_envelope" in called, (
        "_build_messages 没有调用 unwrap_assistant_envelope——"
        "模型仍会在历史里看到 JSON 信封并照着模仿")


# --------------------------------------------------------------------------
# prompt 里的死指令
# --------------------------------------------------------------------------

def test_prompt_no_longer_asks_the_model_to_set_internal_fields():
    """E2 改造去掉了"第二次 LLM 调用解析结构化字段",模型已经没有结构化输出通道。
    prompt 却还留着"并如实设置 requires_human=true"——一条它无法执行的指令,而它
    唯一能想到的执行方式就是把整个 JSON 吐进正文。

    同一个文件第 27 行还写着"不要点出内部字段名(如 intent、confidence、
    requires_human)",两条指令自相矛盾。"""
    import inspect
    import app.prompts.agents as prompts

    src = inspect.getsource(prompts)
    assert "requires_human=true" not in src, (
        "prompt 仍在要求模型设置 requires_human——它没有这个通道,"
        "只会把 JSON 信封吐给买家")



def _one_guard_span(tmp_path, user_input: str, event: dict) -> dict:
    """跑一轮 trace 只发一个 guard 事件,取回落库后的那条 span。"""
    from app.observability.tracer import Tracer
    from app.observability.store import TraceStore

    store = TraceStore(str(tmp_path / "tr.db"))
    store.init_schema()
    tracer = Tracer(store)
    with tracer.start_trace("s-1", user_input) as t:
        tracer.on_event(event)
    saved = store.get_trace(t.trace_id)
    guards = [s for s in saved["spans"] if s["kind"] == "guard"]
    assert len(guards) == 1, f"期望恰好一条 guard span,拿到 {len(guards)}"
    return guards[0]

# --------------------------------------------------------------------------
# 检测器响了,但结论不能在观测边界上被丢掉
# --------------------------------------------------------------------------

def test_internal_leak_event_keeps_its_payload_in_the_span(tmp_path):
    """出话黑话检测的结论必须真的落进 trace。

    **实测过丢掉它的后果**:买家收到过一条把内部 JSON 信封整段吐出来的回复。
    检测器(`chat.py::_check_internal_leak`)**正常命中了**,发出
    `{"type":"guard","kind":"internal_leak","hits":[...]}`;但 tracer 的 guard
    分支只读 `guard` 键 → 落成一条 `guard:None`、meta 三个 null 的空 span。
    排查时在 trace 里搜不到任何线索,看起来像"检测器没工作"——而它工作了,是观测
    边界把结论扔了。与 /api/seller/overview 曾经只取 ["anomalies"] 是同一类错。
    """
    sp = _one_guard_span(tmp_path, "知道了",
                         {"type": "guard", "kind": "internal_leak",
                          "hits": ["requires_human", "confidence"]})
    assert sp["name"] == "guard:internal_leak", f"名字丢了: {sp['name']}"
    hits = (sp["meta"] or {}).get("hits")
    assert hits == ["requires_human", "confidence"], (
        f"命中的词丢了,排查时无从下手: meta={sp['meta']}")


def test_guard_pipeline_events_still_recorded_as_before(tmp_path):
    """护栏流水线那一路(键名是 `guard`)行为逐字不变——这次只是**多认**一种键。"""
    sp = _one_guard_span(tmp_path, "加我微信",
                         {"type": "guard", "guard": "contact_info", "stage": "output",
                          "action": "sanitize", "reason": "回复含引导站外联系"})
    assert sp["name"] == "guard:contact_info"
    meta = sp["meta"] or {}
    assert meta.get("action") == "sanitize"
    assert meta.get("stage") == "output"
    assert "hits" not in meta, "护栏流水线没有 hits,不该凭空多出一个键"


def test_guard_event_without_any_name_is_not_recorded_as_none(tmp_path):
    """两种键都没有时至少给个 'unknown',不要写字符串 "None" ——
    那种名字既搜不到、也说不出发生了什么。"""
    sp = _one_guard_span(tmp_path, "x", {"type": "guard", "stage": "output"})
    assert sp["name"] == "guard:unknown"
