"""Small autoregressive return models for one unidentified OHLC sequence.

@author cylovelife
"""

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler


def ar_features(prices: pd.DataFrame, lags: int = 5) -> pd.DataFrame:
    """Features at t forecast the return ending at t+1; no future values."""
    dates = pd.to_datetime(prices.Date)
    if lags < 1 or dates.isna().any() or not dates.is_unique or not dates.is_monotonic_increasing:
        raise ValueError("positive lags and unique ordered dates required")
    close = prices.Close.astype(float)
    returns = close.pct_change(fill_method=None)
    result = pd.DataFrame({"Date": dates})
    for lag in range(lags):
        result[f"return_lag_{lag}"] = returns.shift(lag)
    for day in range(7):
        result[f"weekday_{day}"] = dates.dt.dayofweek.eq(day).astype(float)
    result["body"] = (close - prices.Open) / prices.Open
    result["upper_wick"] = (prices.High - prices[["Open", "Close"]].max(axis=1)) / prices.Open
    result["lower_wick"] = (prices[["Open", "Close"]].min(axis=1) - prices.Low) / prices.Open
    result["range"] = (prices.High - prices.Low) / close
    result["gap"] = prices.Open / close.shift(1) - 1
    return result.replace([np.inf, -np.inf], np.nan)


def fit_arx(
    train: pd.DataFrame, evaluate: pd.DataFrame, features: list[str]
) -> tuple[np.ndarray, np.ndarray, dict]:
    """Fixed-alpha Ridge AR/ARX; fit and structural zero gate use train only."""
    if not np.isfinite(train.future_return).all():
        raise ValueError("mature training returns required")
    counts = train.groupby(train.Date.dt.dayofweek).Target.agg(["size", "sum"])
    gate = counts.index[(counts["size"] >= 20) & counts["sum"].eq(0)].tolist()
    active = train.loc[~train.Date.dt.dayofweek.isin(gate)]
    if active.empty:
        raise ValueError("empty directional training sample")
    model = make_pipeline(StandardScaler(), Ridge(alpha=10.0))
    model.fit(active[features], active.future_return)
    forecast = model.predict(evaluate[features])
    prediction = (forecast > 0).astype(int)
    prediction[evaluate.Date.dt.dayofweek.isin(gate)] = 0
    return (
        prediction,
        forecast,
        {
            "gate_weekdays": gate,
            "active_training_rows": len(active),
            "standardized_coefficients": dict(zip(features, model[-1].coef_.tolist(), strict=True)),
            "intercept": float(model[-1].intercept_),
        },
    )
