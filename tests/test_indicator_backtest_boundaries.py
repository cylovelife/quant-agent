"""Regression tests for RSI and closed-trade equity boundaries.

@author cylovelife
"""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]

import quant_agent.infrastructure.persistence.phase_cache as pc  # noqa: E402
from quant_agent.application.research.backtest import backtest_symbol  # noqa: E402
from quant_agent.domain.market.indicators import add_indicators, rsi  # noqa: E402
from quant_agent.domain.strategies.short_term import score_row  # noqa: E402


@pytest.mark.parametrize(
    ("prices", "expected"),
    [
        (np.arange(100.0, 140.0), 100.0),
        (np.arange(140.0, 100.0, -1), 0.0),
        (np.full(40, 100.0), 50.0),
    ],
    ids=["rising", "falling", "flat"],
)
def test_rsi_one_direction_and_flat(prices, expected):
    close = pd.Series(prices, index=pd.bdate_range("2026-01-01", periods=40))
    result = rsi(close)
    assert result.iloc[0] == 50.0
    assert (result.iloc[1:] == expected).all()
    pd.testing.assert_index_equal(result.index, close.index)


def test_rsi_mixed_moves_keep_existing_smoothing():
    # n=2: after +2,-1,+2, smoothed gain/loss are (2,0),(1,.5),(1.5,.25).
    actual = rsi(pd.Series([100.0, 102.0, 101.0, 103.0]), n=2)
    np.testing.assert_allclose(actual, [50.0, 100.0, 200.0 / 3, 600.0 / 7])


def test_rsi_empty_and_single_price():
    assert rsi(pd.Series(dtype=float)).empty
    assert rsi(pd.Series([100.0])).tolist() == [50.0]


def bars(closes: list[float], volumes: list[float] | None = None) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.bdate_range("2026-01-01", periods=len(closes)),
            "open": np.full(len(closes), 100.0),
            "high": np.maximum(100.0, closes),
            "low": np.minimum(100.0, closes),
            "close": closes,
            "volume": volumes if volumes is not None else np.full(len(closes), 100.0),
        }
    )


def parameters() -> dict:
    return {
        "weights": {
            "boll": 0.0,
            "rsi": 0.0,
            "volume": 1.0,
            "trend": 0.0,
            "macd": 0.0,
            "drawdown": 0.0,
        },
        "score_threshold": 0.0,
        "backtest": {"hold_days": 1, "stop_loss": -0.99, "take_profit": 10.0},
    }


def test_rsi_flows_into_real_indicator_and_scoring_pipeline():
    frame = bars(list(np.arange(100.0, 180.0)))
    params = parameters()
    result = add_indicators(frame, params)
    assert result.rsi.iloc[-1] == 100.0
    assert score_row(result.iloc[-1], params["weights"])["f_rsi"] == 10.0
    assert "rsi" not in frame.columns


def test_rsi_and_scores_do_not_change_when_future_is_added():
    frame = bars([100.0 + i * 0.3 + np.sin(i) for i in range(100)])
    params = parameters()
    prefix = add_indicators(frame.iloc[:80], params)
    changed = frame.copy()
    changed.loc[80:, "close"] *= 2
    for extended in (frame, changed):
        full = add_indicators(extended, params)
        pd.testing.assert_frame_equal(prefix, full.iloc[:80])
        row = prefix.iloc[-1].to_dict()
        row["macd_hist_prev"] = prefix.macd_hist.iloc[-2]
        full_row = full.iloc[79].to_dict()
        full_row["macd_hist_prev"] = full.macd_hist.iloc[78]
        assert score_row(row, params["weights"]) == score_row(full_row, params["weights"])


@pytest.mark.parametrize(
    ("trade_closes", "expected_return", "expected_drawdown"),
    [
        ([90.0], -0.10, -0.10),
        ([90.0, 80.0], -0.28, -0.28),
        ([110.0, 90.0], -0.01, -0.10),
        ([110.0, 120.0], 0.32, 0.0),
        ([100.0, 100.0], 0.0, 0.0),
    ],
    ids=["first-loss", "consecutive-losses", "gain-then-loss", "gains", "flat"],
)
def test_closed_trade_drawdown_includes_initial_equity(
    trade_closes, expected_return, expected_drawdown
):
    closes = [100.0] * (70 + 2 * len(trade_closes))
    for number, close in enumerate(trade_closes):
        closes[71 + 2 * number] = close
    stat = backtest_symbol(bars(closes), parameters())
    assert stat["signals"] == len(trade_closes)
    assert stat["total_ret"] == pytest.approx(expected_return)
    assert stat["max_dd"] == pytest.approx(expected_drawdown)
    assert stat["avg_days"] == 1.0


def test_no_signal_preserves_empty_result():
    params = parameters()
    params["score_threshold"] = 101.0
    assert backtest_symbol(bars([100.0] * 90), params) == {"signals": 0}


def test_idle_bars_between_signals_do_not_erase_initial_loss():
    params = parameters()
    params["score_threshold"] = 100.0
    closes = [100.0] * 85
    volumes = [100.0] * 85
    volumes[70] = volumes[78] = 50.0
    closes[71], closes[79] = 90.0, 110.0
    stat = backtest_symbol(bars(closes, volumes), params)
    assert stat["signals"] == 2
    assert stat["total_ret"] == pytest.approx(-0.01)
    assert stat["max_dd"] == pytest.approx(-0.10)


@pytest.mark.parametrize("phase", ["exit_model", "param_scan", "select_short"])
def test_previous_rsi_phase_cache_is_not_reused(tmp_path, monkeypatch, phase):
    monkeypatch.setattr(pc, "CACHE_DIR", str(tmp_path))
    params = parameters()
    signature = pc.kline_signature({("cn", "fixture"): bars([100.0] * 90)})
    # Version 2 caches can contain models and selections computed with the old RSI.
    with monkeypatch.context() as old:
        old.setattr(pc, "FORMAT", 2)
        old_key = pc.cache_key(phase, signature, pc.params_signature(params, {}))
        assert pc.save(phase, old_key, {"computed_with": "old-rsi"})
    new_key = pc.cache_key(phase, signature, pc.params_signature(params, {}))
    assert new_key != old_key
    assert pc.load(phase, old_key) is None
    assert pc.load(phase, new_key) is None
    assert pc.save(phase, new_key, {"computed_with": "fixed-rsi"})
    assert pc.load(phase, new_key) == {"computed_with": "fixed-rsi"}
