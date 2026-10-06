"""Eval suite. Heavy deps (ragas/pandas) are imported lazily so `evals.pipeline` works without them."""


def __getattr__(name):
    if name in ("run_pipeline", "load_golden_dataset"):
        from evals import pipeline

        return getattr(pipeline, name)
    if name in ("run_guardrails_eval", "compute_guardrails_metrics"):
        from evals import guardrails_eval

        return getattr(guardrails_eval, name)
    if name == "run_all_metrics":
        from evals.metrics import run_all_metrics

        return run_all_metrics
    raise AttributeError(name)
