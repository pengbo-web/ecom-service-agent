"""确定性技能匹配纯逻辑测试:关键词抽取 + 匹配排序,不依赖 LLM。"""

from app.agent.skills.matcher import extract_keywords, match_skill

CATALOG = [
    {"name": "process-return",
     "description": "当用户要退货、退款、换货时使用。适用关键词：退货、退款、换货、不想要了、质量问题、尺码不合适。"},
    {"name": "track-order",
     "description": "当用户想查订单状态、物流进度、快递到哪了时使用。适用关键词：查订单、物流、快递、到哪了、发货了吗、什么时候到。"},
]


def test_extract_keywords_from_real_format():
    desc = "适用关键词：退货、退款、换货、不想要了、质量问题、尺码不合适。"
    assert extract_keywords(desc) == [
        "退货", "退款", "换货", "不想要了", "质量问题", "尺码不合适",
    ]


def test_extract_keywords_no_marker_returns_empty():
    assert extract_keywords("这是一段没有关键词标记的描述。") == []
    assert extract_keywords("") == []
    assert extract_keywords(None) == []


def test_match_return_phrasing():
    assert match_skill("我要退货,订单号 ORD-20240115-001,尺码不合适", CATALOG) == "process-return"


def test_match_logistics_phrasing():
    assert match_skill("订单 ORD-20240115-001 的快递到哪了", CATALOG) == "track-order"


def test_no_match_returns_none():
    assert match_skill("今天天气怎么样", CATALOG) is None


def test_empty_input_returns_none():
    assert match_skill("", CATALOG) is None
    assert match_skill(None, CATALOG) is None


def test_deterministic_same_input_same_result():
    text = "我要退货,尺码不合适"
    results = {match_skill(text, CATALOG) for _ in range(20)}
    assert results == {"process-return"}


def test_tie_broken_by_name_order():
    """两个 skill 各命中 1 个等长关键词 → 平手按 name 升序取胜。"""
    catalog = [
        {"name": "zzz-skill", "description": "适用关键词：退货。"},
        {"name": "aaa-skill", "description": "适用关键词：退款。"},
    ]
    # "退货退款" 同时命中两个 skill 各 1 个关键词,长度相同(2 字),按 name 升序取 aaa-skill
    assert match_skill("退货退款都想问问", catalog) == "aaa-skill"


def test_real_catalog_return_phrasing():
    from app.agent.skills.loader import SkillManager

    mgr = SkillManager(skills_dir="app/agent/skills/definitions", enabled=True)
    catalog = mgr.get_catalog()
    assert match_skill("我要退货,订单号 ORD-20240115-001,尺码不合适", catalog) == "process-return"


def test_real_catalog_track_order_phrasing():
    from app.agent.skills.loader import SkillManager

    mgr = SkillManager(skills_dir="app/agent/skills/definitions", enabled=True)
    catalog = mgr.get_catalog()
    assert match_skill("订单 ORD-20240115-001 的快递到哪了", catalog) == "track-order"
