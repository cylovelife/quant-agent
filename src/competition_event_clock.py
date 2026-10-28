"""Exploratory causal OHLC-change clock and training-only zero-label gate.

An unchanged OHLC row is not called a non-trading session: the instrument
and exchange are unknown. Official labels and scoring rows stay unchanged.

@author cylovelife
"""

import numpy as np
import pandas as pd

from competition_baseline import fit_predict


def event_features(prices: pd.DataFrame) -> pd.DataFrame:
    """Count past OHLC changes, carrying only already-observed event features."""
    dates = pd.to_datetime(prices.Date)
    if dates.isna().any() or not dates.is_monotonic_increasing or not dates.is_unique:
        raise ValueError("event features require unique ordered dates")
    columns = ["Open", "Close", "High", "Low"]
    changed = ~prices[columns].eq(prices[columns].shift(1)).all(axis=1)
    events = prices.loc[changed].reset_index(drop=True)
    close = events.Close.astype(float)
    result = pd.DataFrame({"Date": pd.to_datetime(events.Date)})
    result["last_change_date"] = result.Date
    for lag in (1, 3, 5, 10):
        result[f"event_return_{lag}"] = close.pct_change(lag, fill_method=None)
    returns = close.pct_change(fill_method=None)
    for window in (5, 20):
        result[f"event_volatility_{window}"] = returns.rolling(window).std()
        result[f"event_distance_{window}"] = close / close.rolling(window).mean() - 1
    result["event_intraday"] = close / events.Open - 1
    result["event_range"] = (events.High - events.Low) / close
    width = (events.High - events.Low).replace(0, np.nan)
    result["event_close_position"] = ((close - events.Low) / width).fillna(0.5)
    result = result.set_index("Date").reindex(dates, method="ffill").reset_index()
    result["days_since_change"] = (result.Date - result.pop("last_change_date")).dt.days
    for day in range(7):
        result[f"weekday_{day}"] = result.Date.dt.dayofweek.eq(day).astype(float)
    return result.replace([np.inf, -np.inf], np.nan)


def gated_predict(
    train: pd.DataFrame, evaluate: pd.DataFrame, features: list[str]
) -> tuple[np.ndarray, list[int]]:
    """Fit direction only outside well-supported all-zero training weekdays."""
    if train.Target.nunique() < 2:
        raise ValueError("training needs both classes")
    counts = train.groupby(train.Date.dt.dayofweek).Target.agg(["size", "sum"])
    gate = counts.index[(counts["size"] >= 20) & counts["sum"].eq(0)].tolist()
    active_train = train.loc[~train.Date.dt.dayofweek.isin(gate)]
    active_evaluate = ~evaluate.Date.dt.dayofweek.isin(gate)
    prediction = np.zeros(len(evaluate), dtype=int)
    if active_evaluate.any():
        prediction[active_evaluate], _ = fit_predict(
            "logistic", active_train, evaluate.loc[active_evaluate], features
        )
    return prediction, gate
