"""Hull scorer provenance, alignment and numeric boundary regression tests.

@author cylovelife
"""

import hashlib
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from quant_agent.competitions.hull.metrics import constant_scores, score_allocations


@pytest.fixture
def solution():
    return pd.DataFrame(
        {
            "date_id": np.arange(20),
            "forward_returns": np.tile([0.012, -0.004, 0.008, -0.003], 5),
            "risk_free_rate": np.full(20, 0.0001),
        },
        index=np.arange(100, 120),
    )


def test_official_scorer_keeps_frozen_source_bytes():
    import quant_agent.competitions.hull as hull

    source = Path(hull.__file__).parent / "_official_metric_v3.py"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == (
        "2f75bc0b1602afd3dd3d7da3415841c98ef49639b10d6dd53d261ea612c80ae6"
    )


def test_full_investment_equals_independent_market_sharpe_and_keeps_inputs(solution):
    original = solution.copy(deep=True)
    predictions = pd.DataFrame({"date_id": solution.date_id.to_numpy(), "prediction": 1.0})
    returns = solution.forward_returns - solution.risk_free_rate
    expected = (1 + returns).prod() ** (1 / len(returns)) - 1
    expected = expected / solution.forward_returns.std() * np.sqrt(252)
    assert score_allocations(solution, predictions) == pytest.approx(expected)
    pd.testing.assert_frame_equal(solution, original)
    assert list(predictions) == ["date_id", "prediction"]


@pytest.mark.parametrize(
    "defect", ["short", "shuffle", "duplicate", "nan", "inf", "text", "bool", "low", "high"]
)
def test_invalid_allocations_are_rejected_before_scoring(solution, defect):
    prediction = pd.DataFrame({"date_id": solution.date_id.to_numpy(), "prediction": 1.0})
    if defect == "short":
        prediction = prediction.iloc[:-1]
    elif defect == "shuffle":
        prediction = prediction.iloc[::-1]
    elif defect == "duplicate":
        prediction.loc[1, "date_id"] = prediction.loc[0, "date_id"]
    elif defect == "text":
        prediction["prediction"] = "1"
    elif defect == "bool":
        prediction["prediction"] = True
    else:
        prediction.loc[0, "prediction"] = {"nan": np.nan, "inf": np.inf, "low": -0.1, "high": 2.1}[
            defect
        ]
    with pytest.raises(ValueError):
        score_allocations(solution, prediction)


def test_solution_nonfinite_returns_and_zero_variance_are_rejected(solution):
    prediction = pd.DataFrame({"date_id": solution.date_id.to_numpy(), "prediction": 1.0})
    solution.loc[solution.index[0], "forward_returns"] = np.nan
    with pytest.raises(ValueError):
        score_allocations(solution, prediction)
    solution["forward_returns"] = 0.01
    with pytest.raises(ValueError, match="zero"):
        score_allocations(solution, prediction)


def test_constant_baselines_keep_unscorable_zero_separate(solution):
    result = constant_scores(solution, (0.0, 0.5, 1.0, 1.5))
    assert result[0]["status"] == "not_evaluable"
    assert result[0]["score"] is None
    assert all(item["status"] == "evaluated" for item in result[1:])
    assert [item["allocation"] for item in result] == [0.0, 0.5, 1.0, 1.5]
