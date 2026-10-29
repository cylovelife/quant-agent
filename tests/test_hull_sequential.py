"""Single-step visibility, response boundaries and reset tests.

@author cylovelife
"""

import numpy as np
import pandas as pd
import pytest

from quant_agent.competitions.hull.sequential import SequentialPredictor


def frame():
    return pd.DataFrame(
        {
            "date_id": [3, 4, 5],
            "M1": [0.1, 0.2, 0.3],
            "is_scored": [False, True, True],
            "lagged_forward_returns": [0.01, -0.02, 0.03],
        }
    )


def test_callback_sees_only_visible_features_and_reset_replays():
    seen = []

    def allocation(row):
        seen.append(row.copy())
        return 1 + float(row.M1.iloc[0])

    model = SequentialPredictor(("M1", "lagged_forward_returns"), allocation)
    data = frame()
    original = [model.predict(data.iloc[[i]]) for i in range(3)]
    assert original == [1.1, 1.2, 1.3]
    assert list(seen[0]) == ["M1", "lagged_forward_returns"]
    assert model.rows_seen == 3
    with pytest.raises(ValueError, match="order"):
        model.predict(data.iloc[[0]])
    model.reset()
    data.loc[2, "M1"] = 0.9
    assert [model.predict(data.iloc[[i]]) for i in range(2)] == original[:2]


@pytest.mark.parametrize(
    "defect", ["labels", "batch", "nan", "inf", "bool", "array", "low", "high"]
)
def test_bad_visibility_batch_or_output_is_rejected(defect):
    row = frame().iloc[[0]].copy()
    if defect == "labels":
        row["forward_returns"] = 99
    elif defect == "batch":
        row = frame()
    output = {
        "nan": np.nan,
        "inf": np.inf,
        "bool": True,
        "array": np.array([1.0]),
        "low": -1.0,
        "high": 3.0,
    }.get(defect, 1.0)
    model = SequentialPredictor(("M1", "lagged_forward_returns"), lambda _: output)
    with pytest.raises(ValueError):
        model.predict(row)
    assert model.rows_seen == 0
