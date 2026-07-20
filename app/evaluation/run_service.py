"""在进程内跑一次评估（供前端"一键跑评估"），复用 Evaluator/Sandbox。需 API Key。"""

from openai import OpenAI

from app.config.settings import settings
from app.evaluation.dataset import load_dataset
from app.evaluation.evaluator import Evaluator
from app.evaluation.sandbox import Sandbox


def run_evaluation(mode: str = "single", use_judge: bool = False) -> dict:
    client = OpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
    sandbox = Sandbox(mode=mode)
    evaluator = Evaluator(
        sandbox=sandbox, client=client, model=settings.model_name,
        use_judge=use_judge, pass_threshold=settings.eval_pass_threshold,
    )
    cases = load_dataset(settings.eval_dataset_path)
    return evaluator.run_all(cases)
