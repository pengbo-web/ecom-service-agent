"""评估后台执行器：把耗时的评估跑在后台线程，供前端"一键跑评估 + 轮询状态"。"""

import threading

from app.evaluation.regression import compare_to_baseline, load_baseline


class EvalRunner:
    """单实例评估任务管理：start() 触发后台跑，status() 轮询结果。同一时刻只跑一个。"""

    def __init__(self, eval_fn, baseline_path, tolerance: float = 0.05):
        self._eval_fn = eval_fn            # callable() -> report dict（含 "summary"）
        self.baseline_path = baseline_path
        self._tolerance = tolerance
        self._state = {"status": "idle", "result": None, "error": None}
        self._lock = threading.Lock()

    def status(self) -> dict:
        with self._lock:
            return dict(self._state)

    def start(self) -> dict:
        with self._lock:
            if self._state["status"] == "running":
                return {"status": "running"}
            self._state = {"status": "running", "result": None, "error": None}
        threading.Thread(target=self._run, daemon=True).start()
        return {"status": "running"}

    def _run(self) -> None:
        try:
            report = self._eval_fn()
            summary = report.get("summary", {})
            baseline = load_baseline(self.baseline_path)
            regression = (compare_to_baseline(summary, baseline, self._tolerance)
                          if baseline else None)
            with self._lock:
                self._state = {"status": "done",
                               "result": {"summary": summary, "regression": regression},
                               "error": None}
        except Exception as e:  # noqa: BLE001 —— 后台任务异常记入状态
            with self._lock:
                self._state = {"status": "error", "result": None, "error": str(e)}
