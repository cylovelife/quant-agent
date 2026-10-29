"""Causal relative feature positions and return history, without global ranking.

@author cylovelife
"""

import re

import numpy as np
import pandas as pd

from quant_agent.competitions.hull.data import LAGGED, TARGETS
from quant_agent.competitions.hull.metrics import validate_ids

RANK_WINDOW = 252
RANK_MIN_PERIODS = 63


def add_context(
    panel: pd.DataFrame, features: tuple[str, ...]
) -> tuple[pd.DataFrame, tuple[str, ...]]:
    """At t, use observed x_t and provided lagged returns; preserve warmup as missing."""
    validate_ids(panel)
    if set(features) & {*TARGETS, "date_id", "label_end_id", "is_scored"}:
        raise ValueError("current labels and metadata are forbidden features")
    if len(set(features)) != len(features) or not set(LAGGED).issubset(features):
        raise ValueError("unique visible features including lagged return fields required")
    if not set(features).issubset(panel):
        raise ValueError("missing visible inputs")
    if np.isinf(panel[list(features)].to_numpy(dtype=float)).any():
        raise ValueError("infinite visible inputs")
    derived = {}
    for name in features:
        if re.fullmatch(r"(?:MOM|[EIMPSV])\d+", name):
            derived[f"ctx_rank_{name}"] = (
                panel[name]
                .rolling(RANK_WINDOW, min_periods=RANK_MIN_PERIODS)
                .rank(method="average", pct=True)
            )
    returns = panel.lagged_forward_returns
    excess = returns - panel.lagged_risk_free_rate
    if (returns.dropna() <= -1).any():
        raise ValueError("log compounding requires returns greater than minus one")
    for window in (5, 21):
        derived[f"ctx_return_mean_{window}"] = returns.rolling(window).mean()
    for window in (21, 63):
        derived[f"ctx_return_cumulative_{window}"] = np.expm1(
            np.log1p(returns).rolling(window).sum()
        )
        derived[f"ctx_excess_std_{window}"] = excess.rolling(window).std(ddof=1)
    denominator = derived["ctx_excess_std_63"].replace(0, np.nan)
    derived["ctx_vol_ratio_21_63"] = derived["ctx_excess_std_21"] / denominator
    if set(derived) & set(panel):
        raise ValueError("context columns already exist")
    result = pd.concat([panel.copy(), pd.DataFrame(derived, index=panel.index)], axis=1)
    return result, (*features, *derived)


class ContextState:
    """Bounded visible history for one-row replay; never store current labels."""

    def __init__(self, prefix: pd.DataFrame, features: tuple[str, ...]) -> None:
        self.features = features
        if list(prefix) != ["date_id", *features] or prefix.empty:
            raise ValueError("visible ordered prefix required")
        validate_ids(prefix)
        self.history = prefix.tail(RANK_WINDOW).copy()

    def push(self, row: pd.DataFrame) -> pd.DataFrame:
        if list(row) != ["date_id", *self.features] or len(row) != 1:
            raise ValueError("one visible feature row required")
        validate_ids(row)
        if row.date_id.iloc[0] <= self.history.date_id.iloc[-1]:
            raise ValueError("rows must be strictly ordered")
        candidate = pd.concat([self.history, row], ignore_index=True).tail(RANK_WINDOW)
        result, _ = add_context(candidate, self.features)
        self.history = candidate
        return result.tail(1).set_axis(row.index)
