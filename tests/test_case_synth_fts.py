"""WS2 接入 case_synthesis:SOURCE_FTS 第三采样源的纪律测试。

守四条:FTS 路只取首个 user 轮(与索引面对齐)、与轨迹/关键词源去重、
卖家侧硬隔离且理由报出、stats 三源分报 + fts_state 披露。
"""

from app.agent.skills.case_synthesis import SOURCE_FTS, synthesize_gate_cases

_BUYER_MD = ("---\nname: return-handling\ndescription: 退换货处理。"
             "适用关键词:退货、换货、挤脚\n---\n\n先 `query_order` 再答复。\n")
_SELLER_MD = ("---\nname: refund-attribution\nactor: seller\n"
              "description: 退款率归因。适用关键词:退款\n---\n\n五步分析链。\n")


def _archive(sid, users):
    msgs = []
    for i, u in enumerate(users):
        msgs.append({"role": "user", "content": u})
        if i == 0:
            msgs.append({"role": "assistant", "content": "查一下",
                         "tool_calls": [{"function": {"name": "query_order",
                                                      "arguments": "{}"}}]})
        else:
            msgs.append({"role": "assistant", "content": "好的"})
    return {"session_id": sid, "messages": msgs, "summary": ""}


def test_fts_source_takes_first_user_turn_only():
    # 文本刻意不含声明关键词(退货/换货/挤脚):关键词路捞不到它,只有 FTS 路给得
    # 出来——否则两路重叠,测不出第三源的增量。
    archives = [_archive("s1", ["鞋子穿着偏小想换大一码", "第二轮追问细节补充说明谢谢"])]
    cases, stats = synthesize_gate_cases(
        "return-handling", traces=[], archives=archives, skill_md=_BUYER_MD,
        fts_sids=["s1"], fts_state="ok")
    assert stats["from_fts"] == 1
    assert stats["from_keyword"] == 0
    assert len(cases) == 1
    assert cases[0]["turns"] == ["鞋子穿着偏小想换大一码"]   # 只取被索引的首句
    assert stats["fts_state"] == "ok"
    assert [p["source"] for p in stats["provenance"]] == [SOURCE_FTS]


def test_fts_dedups_against_trace_and_keyword():
    archives = [_archive("s1", ["我要退货,订单号 ORD-20240115-001"])]
    traces = [{"session_id": "s1", "skill_name": "return-handling",
               "outcome": "success", "tool_calls": []}]
    cases, stats = synthesize_gate_cases(
        "return-handling", traces=traces, archives=archives, skill_md=_BUYER_MD,
        fts_sids=["s1"], fts_state="ok")
    assert stats["from_fts"] == 0          # 已被轨迹源覆盖,不重复 sampling
    assert stats["from_trace"] >= 0


def test_seller_skill_blocks_fts_with_reason():
    archives = [_archive("s1", ["我要退货,订单号 ORD-20240115-001"])]
    cases, stats = synthesize_gate_cases(
        "refund-attribution", traces=[], archives=archives, skill_md=_SELLER_MD,
        fts_sids=["s1"], fts_state="ok")
    assert stats["from_fts"] == 0
    assert stats["fts_blocked"]            # 理由必须报出,不是一句"0 条"
    assert stats["keyword_blocked"]        # 关键词路同款隔离仍在


def test_fts_unavailable_state_is_disclosed():
    archives = [_archive("s1", ["鞋子挤脚想换大一码"])]
    cases, stats = synthesize_gate_cases(
        "return-handling", traces=[], archives=archives, skill_md=_BUYER_MD,
        fts_sids=[], fts_state="unavailable", fts_reason="FTS 索引不可用,本源跳过")
    assert stats["fts_state"] == "unavailable"
    assert stats["fts_reason"]
    assert stats["from_fts"] == 0
