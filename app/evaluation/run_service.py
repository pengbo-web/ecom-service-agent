"""在进程内跑一次评估（供前端"一键跑评估"），复用 Evaluator/Sandbox。需 API Key。"""

from app.config.settings import settings
from app.evaluation.dataset import load_dataset
from app.evaluation.evaluator import Evaluator
from app.evaluation.independence import judge_client, judge_model
from app.evaluation.sandbox import Sandbox


def run_evaluation(mode: str = "single", use_judge: bool = False) -> dict:
    # client/model 在 Evaluator 里**只喂 LLM-as-judge**;沙箱里的 Agent 用线上那套。
    # 裁判独立于被评对象是论文唯一的架构性硬要求,见 evaluation/independence.py。
    sandbox = Sandbox(mode=mode)
    evaluator = Evaluator(
        sandbox=sandbox, client=judge_client(), model=judge_model(),
        use_judge=use_judge, pass_threshold=settings.eval_pass_threshold,
    )
    cases = load_dataset(settings.eval_dataset_path)
    return evaluator.run_all(cases)
