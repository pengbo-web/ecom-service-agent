import time

from app.evaluation.runner import EvalRunner
from app.evaluation.regression import save_baseline


def _wait_done(runner, timeout=2.0):
    end = time.time() + timeout
    while time.time() < end:
        s = runner.status()
        if s["status"] in ("done", "error"):
            return s
        time.sleep(0.01)
    return runner.status()


def test_runner_runs_and_reports(tmp_path):
    report = {"summary": {"pass_rate": 0.9, "avg_process_score": 0.8, "avg_result_score": 0.8}}
    runner = EvalRunner(eval_fn=lambda: report, baseline_path=tmp_path / "nope.json")
    assert runner.status()["status"] == "idle"
    runner.start()
    s = _wait_done(runner)
    assert s["status"] == "done"
    assert s["result"]["summary"]["pass_rate"] == 0.9
    assert s["result"]["regression"] is None  # 无基线


def test_runner_with_baseline_computes_regression(tmp_path):
    bp = tmp_path / "b.json"
    save_baseline({"pass_rate": 0.9, "avg_process_score": 0.8, "avg_result_score": 0.8}, bp)
    report = {"summary": {"pass_rate": 0.6, "avg_process_score": 0.8, "avg_result_score": 0.8}}
    runner = EvalRunner(eval_fn=lambda: report, baseline_path=bp)
    runner.start()
    s = _wait_done(runner)
    assert s["result"]["regression"]["regressed"] is True


def test_runner_error(tmp_path):
    def boom():
        raise RuntimeError("炸了")
    runner = EvalRunner(eval_fn=boom, baseline_path=tmp_path / "b.json")
    runner.start()
    s = _wait_done(runner)
    assert s["status"] == "error"
    assert "炸了" in s["error"]
