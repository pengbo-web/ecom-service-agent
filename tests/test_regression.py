from app.evaluation.regression import compare_to_baseline, save_baseline, load_baseline


def test_no_regression_when_equal():
    s = {"pass_rate": 0.9, "avg_process_score": 0.8, "avg_result_score": 0.85}
    r = compare_to_baseline(s, s, tolerance=0.05)
    assert r["regressed"] is False


def test_regression_when_drop_exceeds_tolerance():
    base = {"pass_rate": 0.9, "avg_process_score": 0.8, "avg_result_score": 0.85}
    cur = {"pass_rate": 0.7, "avg_process_score": 0.8, "avg_result_score": 0.85}  # 掉 0.2
    r = compare_to_baseline(cur, base, tolerance=0.05)
    assert r["regressed"] is True
    diffs = {d["metric"]: d for d in r["diffs"]}
    assert diffs["pass_rate"]["regressed"] is True


def test_small_drop_within_tolerance_ok():
    base = {"pass_rate": 0.90, "avg_process_score": 0.80, "avg_result_score": 0.85}
    cur = {"pass_rate": 0.87, "avg_process_score": 0.80, "avg_result_score": 0.85}  # 掉 0.03
    assert compare_to_baseline(cur, base, tolerance=0.05)["regressed"] is False


def test_improvement_not_regression():
    base = {"pass_rate": 0.7, "avg_process_score": 0.7, "avg_result_score": 0.7}
    cur = {"pass_rate": 0.9, "avg_process_score": 0.8, "avg_result_score": 0.9}
    assert compare_to_baseline(cur, base, tolerance=0.05)["regressed"] is False


def test_none_metrics_skipped():
    base = {"pass_rate": 0.9, "avg_process_score": None, "avg_result_score": 0.8}
    cur = {"pass_rate": 0.9, "avg_process_score": None, "avg_result_score": 0.8}
    r = compare_to_baseline(cur, base, tolerance=0.05)
    assert r["regressed"] is False
    assert all(d["metric"] != "avg_process_score" for d in r["diffs"])


def test_save_and_load_baseline(tmp_path):
    p = tmp_path / "baseline.json"
    assert load_baseline(p) is None
    save_baseline({"pass_rate": 0.9}, p)
    assert load_baseline(p)["pass_rate"] == 0.9
