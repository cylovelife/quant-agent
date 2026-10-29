"""Allocation-only sensitivity and financial risk accounting for frozen forecasts.

@author cylovelife
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from quant_agent.competitions.hull.ridge_study import portfolio_report


@dataclass(frozen=True)
class AllocationScale:
    prediction_rms: float
    market_excess_std: float
    strength: float
    denominator: float
    gain: float

    @classmethod
    def calibrate(
        cls, rms: float, market_std: float, strength: float, market_vol_floor: bool
    ) -> "AllocationScale":
        if not np.isfinite([rms, market_std, strength]).all() or min(rms, strength) < 0:
            raise ValueError("allocation scales must be finite and nonnegative")
        if market_std <= 0:
            raise ValueError("positive market excess volatility required")
        denominator = max(rms, market_std) if market_vol_floor else rms
        gain = strength / denominator if rms >= 1e-8 else 0.0
        return cls(rms, market_std, strength, denominator, gain)

    def apply(self, forecasts: np.ndarray) -> np.ndarray:
        forecasts = np.asarray(forecasts, dtype=float)
        if forecasts.ndim != 1 or not np.isfinite(forecasts).all():
            raise ValueError("forecasts must be a finite vector")
        return np.clip(1 + self.gain * forecasts, 0, 2).astype(np.float32).astype(float)


def risk_report(solution: pd.DataFrame, weights: np.ndarray) -> dict:
    """Use the unchanged official score and reconcile independent risk identities."""
    report = portfolio_report(solution, weights)
    market = solution.forward_returns.to_numpy()
    cash = solution.risk_free_rate.to_numpy()
    excess = market - cash
    strategy = cash * (1 - weights) + weights * market
    active_weights = weights - 1
    active = active_weights * excess
    market_var = float(np.var(market, ddof=1))
    active_var = float(np.var(active, ddof=1))
    covariance = float(np.cov(market, active, ddof=1)[0, 1])
    mean_exposure = float(np.mean(active_weights) * np.mean(excess))
    timing_covariance = float(
        np.mean((active_weights - active_weights.mean()) * (excess - excess.mean()))
    )
    strategy_excess = strategy - cash
    geom_strategy = float(np.prod(1 + strategy_excess) ** (1 / len(strategy)) - 1)
    geom_market = float(np.prod(1 + excess) ** (1 / len(market)) - 1)
    raw_sharpe = geom_strategy / np.std(strategy, ddof=1) * np.sqrt(252)
    vol_penalty = 1 + max(0, report["market_volatility_ratio"] - 1.2)
    return_gap = max(0, (geom_market - geom_strategy) * 100 * 252)
    return_penalty = 1 + return_gap**2 / 100
    reconstructed = float(min(raw_sharpe / (vol_penalty * return_penalty), 1e6))
    if not np.isclose(reconstructed, report["official_score"], rtol=0, atol=1e-12):
        raise ValueError("official score component reconstruction failed")
    report.update(
        {
            "active_weight_rms": float(np.sqrt(np.mean(active_weights**2))),
            "active_return_rms_annualized": float(np.sqrt(np.mean(active**2)) * np.sqrt(252)),
            "tracking_error_annualized": float(np.std(active, ddof=1) * np.sqrt(252)),
            "mean_daily_active_return": float(np.mean(active)),
            "mean_exposure_contribution_daily": mean_exposure,
            "timing_covariance_daily": timing_covariance,
            "mean_active_identity_error": float(
                abs(np.mean(active) - mean_exposure - timing_covariance)
            ),
            "market_variance_daily": market_var,
            "active_variance_daily": active_var,
            "twice_market_active_covariance_daily": 2 * covariance,
            "variance_identity_error": float(
                abs(np.var(strategy, ddof=1) - (market_var + active_var + 2 * covariance))
            ),
            "score_components": {
                "raw_sharpe": float(raw_sharpe),
                "geometric_daily_strategy_excess": geom_strategy,
                "geometric_daily_market_excess": geom_market,
                "volatility_penalty": float(vol_penalty),
                "return_penalty": float(return_penalty),
                "annualized_return_gap_percentage_points": float(return_gap),
                "reconstructed_score": reconstructed,
            },
        }
    )
    return report
