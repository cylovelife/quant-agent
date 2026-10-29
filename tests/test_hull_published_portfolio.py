"""Public portfolio formulas and bounded causal behavior, never original-alpha proof.

@author cylovelife
"""

import numpy as np
import pandas as pd
import pytest

from quant_agent.competitions.hull.published_portfolio import (
    PublishedRiskOverlay,
    RiskSettings,
    inverse_volatility_weights,
    target_position,
)


def settings(**overrides):
    values = dict(
        signal_window=4,
        portfolio_window=4,
        min_observations=3,
        update_every=2,
        target_annual_volatility=0.15,
        daily_volatility_floor=1e-6,
        warmup_position=1.0,
        portfolio_return_basis="pre_overlay",
    )
    return RiskSettings(**(values | overrides))


def history():
    return pd.DataFrame(
        {
            "date_id": [0, 1, 2, 3],
            "label_end_id": [1, 2, 3, 4],
            "signal_return_0": [-0.01, 0.01, -0.02, 0.02],
            "signal_return_1": [-0.02, 0.02, -0.04, 0.04],
            "portfolio_return": [-0.01, 0.01, -0.02, 0.02],
        }
    )


def test_inverse_volatility_weights_are_normalized_and_match_known_ratio():
    np.testing.assert_allclose(
        inverse_volatility_weights(np.array([0.1, 0.2]), 1e-6), [2 / 3, 1 / 3]
    )
    np.testing.assert_allclose(inverse_volatility_weights(np.array([0, 0]), 1e-6), [0.5, 0.5])
    for bad in ([np.nan, 1], [-1, 1], [], [np.inf, 1]):
        with pytest.raises(ValueError):
            inverse_volatility_weights(np.array(bad), 1e-6)
    with pytest.raises(ValueError):
        inverse_volatility_weights(np.array([0.1]), 0)


def test_targeting_scales_total_exposure_and_enforces_official_bounds():
    assert target_position(1.2, 0.2, 0.1, 1e-6) == pytest.approx(0.6)
    assert target_position(1.2, 0.05, 0.2, 1e-6) == 2
    assert target_position(-1, 0.2, 0.1, 1e-6) == 0
    # This is exposure multiplication from the writeup, not shrinkage around one.
    assert target_position(1, 0.2, 0.1, 1e-6) == 0.5
    assert target_position(1.23456789, 0.2, 0.1, 1e-6) == float(np.float32(1.23456789 / 2))


def test_overlay_matches_public_formulas_and_updates_only_on_declared_schedule():
    risk = history()
    overlay = PublishedRiskOverlay(settings(), 2)
    first = overlay.allocate(4, np.array([1.5, 0.5]), risk)
    sigma = np.std(risk[["signal_return_0", "signal_return_1"]], axis=0, ddof=1)
    weights = (1 / sigma) / np.sum(1 / sigma)
    portfolio_vol = np.std(risk.portfolio_return, ddof=1) * np.sqrt(252)
    expected = target_position(
        float(weights @ np.array([1.5, 0.5])), portfolio_vol, 0.15, 1e-6 * np.sqrt(252)
    )
    assert first.position == expected
    np.testing.assert_allclose(first.signal_weights, [2 / 3, 1 / 3])
    changed = risk.copy()
    changed.loc[:, ["signal_return_0", "signal_return_1", "portfolio_return"]] *= 2
    second = overlay.allocate(5, np.array([1.5, 0.5]), changed)
    assert second.position == first.position
    assert not second.risk_updated
    third = overlay.allocate(6, np.array([1.5, 0.5]), changed)
    assert third.risk_updated
    assert third.annualized_portfolio_volatility == pytest.approx(
        first.annualized_portfolio_volatility * 2
    )
    assert third.position == float(np.float32(first.position / 2))


def test_warmup_future_label_rejection_and_failed_call_do_not_advance_state():
    overlay = PublishedRiskOverlay(settings(warmup_position=0.75), 2)
    result = overlay.allocate(2, np.array([1, 1]), history().iloc[:2])
    assert result.position == 0.75 and result.status == "warmup"
    future = history().copy()
    future.loc[3, "label_end_id"] = 10
    with pytest.raises(ValueError, match="mature"):
        overlay.allocate(4, np.array([1, 1]), future)
    valid = overlay.allocate(4, np.array([1, 1]), history())
    assert valid.risk_updated
    with pytest.raises(ValueError, match="ordered"):
        overlay.allocate(4, np.array([1, 1]), history())
    with pytest.raises(ValueError):
        overlay.allocate(5, np.array([np.nan, 1]), history())
    assert overlay.allocate(5, np.array([1, 1]), history()).position == valid.position


def test_settings_require_explicit_supported_units_and_no_silent_original_defaults():
    for changes in (
        {"min_observations": 1},
        {"min_observations": 5},
        {"update_every": 0},
        {"signal_window": True},
        {"target_annual_volatility": np.nan},
        {"portfolio_return_basis": "unspecified"},
        {"warmup_position": 3},
    ):
        with pytest.raises(ValueError):
            PublishedRiskOverlay(settings(**changes), 2)
    assert (
        PublishedRiskOverlay(
            settings(portfolio_return_basis="post_overlay"), 2
        ).settings.portfolio_return_basis
        == "post_overlay"
    )


def test_matching_aggregate_return_and_volatility_cannot_recover_daily_policy():
    # Permuting a return path preserves total compounded return, mean and std.
    # Its drawdown and per-day decisions still differ: aggregate metrics are not an oracle.
    a = np.array([0.04, -0.03, 0.04, -0.03])
    b = np.array([0.04, 0.04, -0.03, -0.03])
    assert np.prod(1 + a) == pytest.approx(np.prod(1 + b))
    assert np.std(a) == pytest.approx(np.std(b))

    def drawdown(r):
        equity = np.r_[1, np.cumprod(1 + r)]
        return np.min(equity / np.maximum.accumulate(equity) - 1)

    assert drawdown(a) != pytest.approx(drawdown(b))
