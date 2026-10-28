"""Causal event-clock and training-only calendar gate contracts.

@author cylovelife
"""

from pathlib import Path
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from competition_event_clock import event_features, gated_predict  # noqa: E402


def prices():
    dates = pd.date_range("2016-01-01", periods=260)
    close = np.exp(np.arange(260) * 0.001 + np.sin(np.arange(260)) * 0.01) * 100
    frame = pd.DataFrame(
        {
            "Date": dates,
            "Open": close,
            "Close": close,
            "High": close + 1,
            "Low": close - 1,
        }
    )
    frame.loc[101, ["Open", "Close", "High", "Low"]] = frame.loc[
        100, ["Open", "Close", "High", "Low"]
    ].to_numpy()
    return frame


def test_event_clock_is_causal_and_repeat_does_not_add_price_observation():
    frame = prices()
    result = event_features(frame)
    changed = frame.copy()
    changed.loc[180:, ["Open", "Close", "High", "Low"]] *= 5
    pd.testing.assert_frame_equal(result.iloc[:180], event_features(changed).iloc[:180])
    pd.testing.assert_frame_equal(result.iloc[:180], event_features(frame.iloc[:180]))
    assert result.loc[101, "event_return_1"] == result.loc[100, "event_return_1"]
    assert result.loc[101, "days_since_change"] == 1
    assert result.loc[102, "days_since_change"] == 0


def test_gate_uses_training_labels_only_and_requires_repeated_evidence():
    frame = prices()[["Date"]]
    frame["Target"] = np.arange(len(frame)) % 2
    frame["x"] = np.sin(np.arange(len(frame)))
    frame.loc[frame.Date.dt.dayofweek.eq(4), "Target"] = 0
    train, valid = frame.iloc[:210], frame.iloc[210:].copy()
    pred, gate = gated_predict(train, valid, ["x"])
    valid["Target"] = 1 - valid.Target
    changed, changed_gate = gated_predict(train, valid, ["x"])
    np.testing.assert_array_equal(pred, changed)
    assert gate == changed_gate == [4]
    assert (pred[valid.Date.dt.dayofweek.eq(4)] == 0).all()
    with pytest.raises(ValueError, match="classes"):
        gated_predict(train.assign(Target=0), valid, ["x"])


def test_event_clock_rejects_reordered_dates():
    with pytest.raises(ValueError, match="ordered"):
        event_features(prices().iloc[::-1])
