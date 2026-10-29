"""One transparent contrarian policy with causal risk overlay; not the author alpha.

@author cylovelife
"""

from collections import deque
from dataclasses import dataclass
from numbers import Integral

import numpy as np
import pandas as pd

from quant_agent.competitions.hull.metrics import validate_ids
from quant_agent.competitions.hull.published_portfolio import (
    PublishedRiskOverlay,
    RiskDecision,
    RiskSettings,
)


def reversal_exposure(excess_history: np.ndarray) -> tuple[float, float | None]:
    values = np.asarray(excess_history, dtype=float)
    if values.ndim != 1 or not np.isfinite(values).all():
        raise ValueError("finite observed lagged excess returns required")
    if len(values) < 21:
        return 1.0, None
    sigma = float(np.std(values[-21:], ddof=1))
    if sigma <= 1e-6:
        return 1.0, None
    z = float(np.sum(values[-5:]) / (sigma * np.sqrt(5)))
    active = -0.25 * np.sign(z) * max(abs(z) - 1.0, 0.0)
    return float(np.float32(1 + np.clip(active, -0.5, 0.5))), z


@dataclass(frozen=True)
class ReversalDecision:
    date_id: int
    base_position: float
    position: float
    standardized_move: float | None
    pre_overlay_position: float
    risk_decision: RiskDecision


class ReversalPolicy:
    """Ingest every observation, even purge rows without a portfolio decision.

    Current r/f labels are never accepted. Each next observed row supplies the
    prior row's realized r/f via the official lag fields. Missing OOF decisions
    therefore create no invented PnL, while market feature history stays complete.
    """

    def __init__(self, settings: RiskSettings, initial_excess: tuple[float, ...] = ()) -> None:
        values = np.asarray(initial_excess, dtype=float)
        if values.ndim != 1 or not np.isfinite(values).all():
            raise ValueError("finite initial lagged history required")
        self.excess_history = deque(values[-21:].tolist(), maxlen=21)
        self.risk_history: deque[dict] = deque(
            maxlen=max(settings.signal_window, settings.portfolio_window)
        )
        self.overlay = PublishedRiskOverlay(settings, 2)
        self.last_id: int | None = None
        self.pending: tuple[int, float, float] | None = None

    def step(
        self,
        date_id: int,
        lagged_return: float,
        lagged_cash: float,
        *,
        base_override: float | None = None,
        decide: bool = True,
    ) -> ReversalDecision | None:
        if isinstance(date_id, bool) or not isinstance(date_id, Integral):
            raise ValueError("integer date ID required")
        if self.last_id is not None and date_id <= self.last_id:
            raise ValueError("observations must be strictly ordered")
        if not np.isfinite([lagged_return, lagged_cash]).all():
            raise ValueError("finite lagged returns required")
        if base_override is not None and (
            not np.isfinite(base_override) or not 0 <= base_override <= 2
        ):
            raise ValueError("finite frozen exposure within 0..2 required")
        excess = deque(self.excess_history, maxlen=21)
        excess.append(float(lagged_return - lagged_cash))
        history = deque(self.risk_history, maxlen=self.risk_history.maxlen)
        if self.pending is not None:
            previous_id, base, pre_overlay = self.pending
            history.append(
                {
                    "date_id": previous_id,
                    "label_end_id": int(date_id),
                    "signal_return_0": lagged_cash + base * (lagged_return - lagged_cash),
                    "signal_return_1": lagged_return,
                    "portfolio_return": lagged_cash + pre_overlay * (lagged_return - lagged_cash),
                }
            )
        result, pending = None, None
        if decide:
            base, z = reversal_exposure(np.asarray(excess))
            if base_override is not None:
                base = float(np.float32(base_override))
            risk_frame = pd.DataFrame(
                list(history),
                columns=[
                    "date_id",
                    "label_end_id",
                    "signal_return_0",
                    "signal_return_1",
                    "portfolio_return",
                ],
            )
            risk_frame = risk_frame.astype({"date_id": "int64", "label_end_id": "int64"})
            risk = self.overlay.allocate(int(date_id), np.array([base, 1.0]), risk_frame)
            pre_overlay = (
                float(np.asarray(risk.signal_weights) @ np.array([base, 1.0]))
                if risk.signal_weights
                else self.overlay.settings.warmup_position
            )
            result = ReversalDecision(int(date_id), base, risk.position, z, pre_overlay, risk)
            pending = (int(date_id), base, pre_overlay)
        self.excess_history, self.risk_history = excess, history
        self.pending, self.last_id = pending, int(date_id)
        return result

    def predict_visible(self, row: pd.DataFrame) -> float:
        forbidden = {"forward_returns", "risk_free_rate", "market_forward_excess_returns"}
        if set(row) & forbidden:
            raise ValueError("current labels are forbidden")
        required = {"date_id", "lagged_forward_returns", "lagged_risk_free_rate"}
        if len(row) != 1 or not required.issubset(row):
            raise ValueError("one row with date ID and official lagged returns required")
        validate_ids(row)
        decision = self.step(
            int(row.date_id.iloc[0]),
            float(row.lagged_forward_returns.iloc[0]),
            float(row.lagged_risk_free_rate.iloc[0]),
        )
        return decision.position


def new_policy(history: pd.DataFrame, first_id: int, settings: RiskSettings) -> ReversalPolicy:
    lagged_excess = (history.forward_returns - history.risk_free_rate).shift(1)
    initial = lagged_excess.loc[history.date_id < first_id].dropna().tail(21)
    return ReversalPolicy(settings, tuple(initial))


def bootstrap_reversal(
    history: pd.DataFrame,
    start: int,
    settings: RiskSettings,
    *,
    blocks: int = 3,
    block_rows: int = 252,
) -> ReversalPolicy:
    """Same past calibration cohorts as the controlled study; no future training labels."""
    validate_ids(history)
    earlier = history.loc[history.date_id < start].copy()
    if earlier.empty or history.date_id.max() >= start:
        raise ValueError("bootstrap must contain only strictly earlier observations")
    first_id = start - blocks * block_rows
    ends = earlier.date_id.shift(-1)
    # The final earlier row's known end is the first incoming observed ID.
    ends.iloc[-1] = start
    selected = set()
    for begin in range(first_id, start, block_rows):
        selected.update(
            earlier.loc[
                (earlier.date_id >= begin)
                & (earlier.date_id < begin + block_rows)
                & (ends < begin + block_rows),
                "date_id",
            ].tolist()
        )
    policy = new_policy(earlier, first_id, settings)
    lagged_r, lagged_f = earlier.forward_returns.shift(1), earlier.risk_free_rate.shift(1)
    for idx in earlier.index[earlier.date_id >= first_id]:
        policy.step(
            int(earlier.loc[idx, "date_id"]),
            float(lagged_r.loc[idx]),
            float(lagged_f.loc[idx]),
            decide=int(earlier.loc[idx, "date_id"]) in selected,
        )
    return policy
