"""Causality and conservative fallback of the reusable short-horizon forecast.

@author cylovelife
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from quant_agent.domain.research.market_forecast import (
    ForecastSettings,
    build_forecast_panel,
    forecast_short_term,
    stable_improvement,
)


def bars(n=320):
    rng = np.random.default_rng(19)
    ret = rng.normal(0.0003, 0.012, n)
    close = 100 * np.cumprod(1 + ret)
    opening = close * (1 + rng.normal(0, 0.003, n))
    return pd.DataFrame(
        {
            "date": pd.bdate_range("2023-01-02", periods=n),
            "open": opening,
            "close": close,
            "high": np.maximum(opening, close) * 1.01,
            "low": np.minimum(opening, close) * 0.99,
            "volume": rng.uniform(1000, 10000, n),
            "amount": rng.uniform(1e8, 3e8, n),
        }
    )


def test_features_are_prefix_invariant_and_targets_use_actual_dates():
    source = bars().drop(index=[180, 181]).reset_index(drop=True)
    whole, features = build_forecast_panel(source, adjust="qfq", horizon=5)
    prefix, _ = build_forecast_panel(source.iloc[:200], adjust="qfq", horizon=5)
    pd.testing.assert_frame_equal(whole.loc[:199, features], prefix[features])
    assert whole.loc[179, "label_end"] == source.loc[184, "date"]
    assert prefix.tail(5)["target_return"].isna().all()
    future = source.close.pct_change(fill_method=None).iloc[180:185]
    assert whole.loc[179, "target_rms"] == pytest.approx(np.sqrt((future**2).mean()))
    assert whole.loc[179, "target_return"] == pytest.approx(
        source.close.iloc[184] / source.close.iloc[179] - 1
    )
    assert "label_end" not in features and "target_return" not in features


def test_future_data_cannot_change_cutoff_prediction_or_validation():
    source = bars()
    cutoff = str(source.date.iloc[280].date())
    before = forecast_short_term(source.iloc[:281], adjust="qfq", asof=cutoff)
    source.loc[281:, "close"] = -1  # Future invalid values must be truncated first.
    after = forecast_short_term(source, adjust="qfq", asof=cutoff)
    assert before == after
    assert before["status"] == "observed"
    assert before["promotion_allowed"] is False
    for fold in before["validation"]:
        assert fold["train_label_end"] < fold["start"]
        assert fold["label_end"] < fold["end_exclusive"]
        assert fold["n"] >= 10
        assert fold["train_rows"] >= 80


def test_large_average_gain_cannot_hide_one_bad_window():
    assert not stable_improvement([0.5, 0.5, 1.08], [1, 1, 1])
    assert not stable_improvement([0.99, 0.99, 0.99], [1, 1, 1])
    assert not stable_improvement([0, 0, 0], [0, 0, 0])
    assert stable_improvement([0.8, 0.8, 1.01], [1, 1, 1])


def test_flat_market_and_missing_volume_do_not_create_alpha():
    source = bars()
    source[["open", "close", "high", "low"]] = 100.0
    source[["volume", "amount"]] = np.nan
    result = forecast_short_term(source, adjust="qfq", asof="2025-01-01")
    assert result["status"] == "observed"
    assert result["return_model"] == "zero"
    assert result["risk_model"] == "historical_rms_20"
    assert result["expected_return"] == 0
    assert result["daily_rms"] == 0
    assert result["return_reference_range"] == [0, 0]
    assert "volume" in result["missing_inputs"]


@pytest.mark.parametrize("defect", ["duplicate", "order", "negative", "ohlc", "raw"])
def test_invalid_market_data_is_explicitly_blocked(defect):
    source = bars()
    adjust = "qfq"
    if defect == "duplicate":
        source.loc[2, "date"] = source.loc[1, "date"]
    elif defect == "order":
        source = source.iloc[::-1]
    elif defect == "negative":
        source.loc[3, "close"] = -1
    elif defect == "ohlc":
        source.loc[3, "high"] = 0.1
    else:
        adjust = "raw"
    result = forecast_short_term(source, adjust=adjust, asof="2025-01-01")
    assert result["status"] == "data_blocked"
    assert result["expected_return"] is None
    assert result["reason"]


def test_insufficient_history_and_invalid_settings():
    result = forecast_short_term(bars(90), adjust="qfq", asof="2025-01-01")
    assert result["status"] == "insufficient_history"
    assert result["expected_return"] is None
    with pytest.raises(ValueError):
        ForecastSettings(horizon=0)


@pytest.mark.parametrize("direct", [None, "qfq"])
def test_reader_raw_provenance_cannot_be_relabelled_qfq(direct):
    source = bars()
    source.attrs["data_provenance"] = {"market": "cn", "code": "000001", "adjust": "raw"}
    if direct:
        source.attrs["adjust"] = direct
    result = forecast_short_term(source, adjust="qfq", asof="2025-01-01")
    assert result["status"] == "data_blocked"
    assert "provenance" in result["reason"]


def test_optional_sklearn_absence_is_reported(monkeypatch):
    import builtins

    original = builtins.__import__

    def without_sklearn(name, *args, **kwargs):
        if name.startswith("sklearn"):
            raise ImportError("sklearn unavailable in this environment")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_sklearn)
    result = forecast_short_term(bars(), adjust="qfq", asof="2025-01-01")
    assert result["status"] == "dependency_unavailable"
    assert result["expected_return"] is None


def test_application_does_not_forecast_markets_without_completed_bar_policy(monkeypatch):
    from quant_agent.application.research import forecasting

    calls = []

    def predict(frame, **kwargs):
        calls.append(frame)
        return {"status": "observed"}

    monkeypatch.setattr(forecasting, "forecast_short_term", predict)
    source = bars()
    markets = ["cn", "etf", "hk", "us", "fund"]
    observations = [{"market": market, "code": "fixture"} for market in markets]
    frames = {(market, "fixture"): source for market in markets}
    result = forecasting.attach_forecasts(observations, frames, asof="2025-01-01")
    assert len(calls) == 2
    assert [row["market"] for row in result if "forecast" in row] == ["cn", "etf"]
    assert all("forecast" not in row for row in observations)


def test_revised_qfq_snapshot_does_not_claim_historical_point_in_time_coverage():
    result = forecast_short_term(bars(), adjust="qfq", asof="2025-01-01")
    assert result["history_basis"] == "current_qfq_snapshot"
    assert result["point_in_time_complete"] is False


def test_both_investment_report_modes_render_forecasts_without_rescoring(tmp_path, monkeypatch):
    from quant_agent.application.research.forecasting import attach_forecasts
    from quant_agent.domain.strategies.short_term import score_candidates
    from quant_agent.domain.strategies.value import score_longterm
    from quant_agent.infrastructure.reporting import investment

    monkeypatch.setenv("QUANT_DB", str(tmp_path / "report-metadata.db"))

    source = bars()
    params = json.loads((Path(__file__).resolve().parents[1] / "params/params.json").read_text())
    candidates = pd.DataFrame(
        [
            {
                "code": "000001",
                "name": "fixture",
                "market": "cn",
                "price": float(source.close.iloc[-1]),
                "pct": 0.0,
                "amount": 1e9,
            }
        ]
    )
    scored = score_candidates(candidates, "cn", params, lambda *_: source)
    assert not scored.empty
    rows = attach_forecasts(
        scored.to_dict("records"), {("cn", "000001"): source}, asof="2025-01-01"
    )
    assert rows[0]["score"] == scored.score.iloc[0]
    assert "forecast" not in scored
    value = score_longterm({}, {})
    stock = {"code": "fixture", "name": "fixture", **value}
    longterm = {"enabled": True, "picks": {"stocks": pd.DataFrame([stock])}}
    monkeypatch.setattr(investment, "REPORT_DIR", str(tmp_path))
    for brief in (False, True):
        path, _ = investment.generate_report(
            {"cn": pd.DataFrame(rows)},
            {},
            {},
            {"updated": False},
            params,
            longterm=longterm,
            mode="all",
            brief=brief,
        )
        text = Path(path).read_text()
        assert "短期收益与波动研究" in text
        assert "未经独立校准" in text
        assert "长期价值证据与待复核项" in text
        assert "数据不足" in text
