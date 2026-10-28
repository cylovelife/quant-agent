"""Autoregressive features, fit boundaries and server-contract regression tests.

@author cylovelife
"""

from pathlib import Path
import sys

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from competition_timeseries import ar_features, fit_arx  # noqa: E402
from competition_submission import server_rows  # noqa: E402


def test_autoregressive_features_are_prefix_invariant():
    dates = pd.date_range("2017-01-01", periods=150)
    close = 100 * np.exp(np.sin(np.arange(150)) * 0.01)
    frame = pd.DataFrame(
        {
            "Date": dates,
            "Close": close,
            "Open": close * 0.999,
            "High": close * 1.01,
            "Low": close * 0.99,
        }
    )
    base = ar_features(frame)
    pd.testing.assert_frame_equal(base.iloc[:100], ar_features(frame.iloc[:100]))
    frame.loc[100:, ["Close", "Open", "High", "Low"]] *= 4
    pd.testing.assert_frame_equal(base.iloc[:100], ar_features(frame).iloc[:100])
    assert base.loc[10, "return_lag_0"] == pytest.approx(close[10] / close[9] - 1)
    assert base.loc[10, "return_lag_4"] == pytest.approx(close[6] / close[5] - 1)


def test_arx_forecast_cannot_read_evaluation_labels():
    n = 250
    frame = pd.DataFrame(
        {
            "Date": pd.date_range("2017-01-01", periods=n),
            "x": np.sin(np.arange(n) * 0.4),
        }
    )
    frame["future_return"] = frame.x * 0.01 + 0.001
    frame["Target"] = (frame.future_return > 0).astype(int)
    fit, valid = frame.iloc[:210], frame.iloc[210:].copy()
    a, forecast, _ = fit_arx(fit, valid, ["x"])
    valid["future_return"] = 999
    valid["Target"] = 1 - valid.Target
    b, changed, _ = fit_arx(fit, valid, ["x"])
    np.testing.assert_array_equal(a, b)
    np.testing.assert_array_equal(forecast, changed)
    assert ((forecast > 0).astype(int) == frame.Target.iloc[210:]).mean() > 0.9


def test_server_schema_has_365_predictions_plus_separate_header():
    source = pd.DataFrame({"Id": np.arange(364), "Target": np.arange(364) % 2})
    result = server_rows(source, missing_prior=0, complement=False)
    assert list(result) == ["ID", "Target"]
    assert len(result) == 365 and result.ID.tolist() == list(range(365))
    assert result.iloc[-1].to_dict() == {"ID": 364, "Target": 0}
    np.testing.assert_array_equal(result.Target.iloc[:364], source.Target)
    inverted = server_rows(source, missing_prior=0, complement=True)
    np.testing.assert_array_equal(inverted.Target, 1 - result.Target)
    with pytest.raises(ValueError):
        server_rows(source.iloc[::-1], missing_prior=0, complement=False)
    with pytest.raises(ValueError):
        server_rows(source, missing_prior=2, complement=False)
