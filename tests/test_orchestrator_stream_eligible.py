"""E1(回复流式化):streaming.py 对着总控(MultiAgentOrchestrator)调
set_turn_stream_eligible,总控必须在 chat() 里原样转给真正跑 ReAct 循环的
引擎(self.engine)——否则生产路径(app.py 走的就是总控)永远读不到这个开关,
只有裸 EcomAgent 测试才会生效,买家侧永远享受不到流式。"""

from app.agent.recall.service import RecallResult
from app.multi_agent.orchestrator import MultiAgentOrchestrator


def _orch(tmp_path, monkeypatch):
    o = MultiAgentOrchestrator(session_path=str(tmp_path / "s.json"), user_id="u1")
    o.engine._react_loop = lambda: '{"intent":"other","confidence":0.9,"reply":"ok","requires_human":false}'
    o.engine.memory_manager.update_short_term = lambda *a, **k: None
    o.engine._reply_pipeline.run = lambda *a, **k: a[3]
    monkeypatch.setattr("app.agent.recall.service.build_recall_sections",
                        lambda mm, q, include_kb=True, kb_domain=None: RecallResult())
    return o


def test_default_is_not_eligible(tmp_path, monkeypatch):
    o = _orch(tmp_path, monkeypatch)
    o.chat("你好")
    assert o.engine._turn_stream_eligible is False


def test_set_turn_stream_eligible_forwards_to_engine(tmp_path, monkeypatch):
    o = _orch(tmp_path, monkeypatch)
    o.set_turn_stream_eligible(True)
    o.chat("你好")
    assert o.engine._turn_stream_eligible is True


def test_eligibility_is_re_read_each_turn(tmp_path, monkeypatch):
    """不是"设一次永久生效":streaming.py 每轮都会按当轮的护栏判断重新调用,
    所以总控每次 chat() 都要用当时的值,不能缓存住上一轮的判断。"""
    o = _orch(tmp_path, monkeypatch)
    o.set_turn_stream_eligible(True)
    o.chat("第一轮")
    assert o.engine._turn_stream_eligible is True
    o.set_turn_stream_eligible(False)
    o.chat("第二轮")
    assert o.engine._turn_stream_eligible is False
