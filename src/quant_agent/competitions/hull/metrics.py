"""Strict boundaries around the unchanged official Hull competition scorer.

@author cylovelife
"""

import numpy as np
import pandas as pd
from pandas.api.types import is_bool_dtype, is_integer_dtype, is_numeric_dtype

from quant_agent.competitions.hull._official_metric_v3 import ParticipantVisibleError, score


def validate_ids(frame: pd.DataFrame) -> None:
    """Preserve observed trading-day order without inventing calendar dates."""
    if (
        "date_id" not in frame
        or not is_integer_dtype(frame.date_id)
        or is_bool_dtype(frame.date_id)
    ):
        raise ValueError("date_id must be integer")
    if frame.date_id.isna().any() or not frame.date_id.is_unique:
        raise ValueError("date_id must be nonmissing and unique")
    if not frame.date_id.is_monotonic_increasing:
        raise ValueError("date_id must be ordered")


def score_allocations(solution: pd.DataFrame, predictions: pd.DataFrame) -> float:
    """Reject misalignment/nonfinite inputs and copy before upstream mutation."""
    required = {"date_id", "forward_returns", "risk_free_rate"}
    if not required.issubset(solution) or len(solution) < 2:
        raise ValueError("at least two labelled scoring rows required")
    if list(predictions) != ["date_id", "prediction"] or len(predictions) != len(solution):
        raise ValueError("prediction schema or length mismatch")
    validate_ids(solution)
    validate_ids(predictions)
    if not np.array_equal(solution.date_id.to_numpy(), predictions.date_id.to_numpy()):
        raise ValueError("prediction IDs must match solution order exactly")
    for frame, names in (
        (solution, ("forward_returns", "risk_free_rate")),
        (predictions, ("prediction",)),
    ):
        for name in names:
            if not is_numeric_dtype(frame[name]) or is_bool_dtype(frame[name]):
                raise ValueError(f"{name} must be numeric")
            if not np.isfinite(frame[name].to_numpy(dtype=float)).all():
                raise ValueError(f"{name} must be finite")
    if not predictions.prediction.between(0, 2).all():
        raise ValueError("allocations must be within [0, 2]")
    if solution.forward_returns.nunique() < 2:
        raise ValueError("market std is zero")
    safe_solution = (
        solution[["date_id", "forward_returns", "risk_free_rate"]].reset_index(drop=True).copy()
    )
    safe_predictions = predictions.reset_index(drop=True).copy()
    try:
        value = score(safe_solution, safe_predictions, "date_id")
    except ParticipantVisibleError as exc:
        raise ValueError(str(exc)) from exc
    if not np.isfinite(value):
        raise ValueError("official score is nonfinite")
    return float(value)


def constant_scores(
    solution: pd.DataFrame, allocations: tuple[float, ...] = (0.5, 1.0, 1.5)
) -> list[dict]:
    """Evaluate predeclared allocations; keep undefined scores distinct from zero."""
    results = []
    for allocation in allocations:
        predictions = pd.DataFrame(
            {"date_id": solution.date_id.to_numpy(), "prediction": allocation}
        )
        try:
            value = score_allocations(solution, predictions)
        except ValueError as exc:
            results.append(
                {
                    "allocation": allocation,
                    "status": "not_evaluable",
                    "score": None,
                    "reason": str(exc),
                }
            )
        else:
            results.append({"allocation": allocation, "status": "evaluated", "score": value})
    return results
