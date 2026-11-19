"""Online evaluation module for multi-agent system.

The judge names load on first use, so `src.evaluation.code_checks` (standard library only)
can be imported without the backend's dependencies, as evals/ci_eval.py does.
"""

__all__ = ["evaluate_brand_voice_async", "GEvalMetric"]


def __getattr__(name: str):
    if name in __all__:
        from . import judge

        return getattr(judge, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
