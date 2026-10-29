"""Public inverse-volatility/targeting structure, not YannFb's undisclosed alpha.

All parameters and input signal exposures are caller supplied. Demonstration settings
and sample-standard-deviation estimates do not establish original-strategy identity.

@author cylovelife
"""

from dataclasses import dataclass
from numbers import Integral
from typing import Literal

import numpy as np
import pandas as pd
from pandas.api.types import is_bool_dtype, is_integer_dtype

from quant_agent.competitions.hull.metrics import validate_ids


def inverse_volatility_weights(volatilities: np.ndarray, floor: float) -> np.ndarray:
    values = np.asarray(volatilities, dtype=float)
    if (
        values.ndim != 1
        or not len(values)
        or not np.isfinite(values).all()
        or (values < 0).any()
        or not np.isfinite(floor)
        or floor <= 0
    ):
        raise ValueError("finite nonnegative volatilities and positive floor required")
    safe = np.maximum(values, floor)
    inverse = safe.min() / safe
    return inverse / inverse.sum()


def target_position(
    exposure: float, annual_volatility: float, annual_target: float, annual_floor: float
) -> float:
    if (
        not np.isfinite([exposure, annual_volatility, annual_target, annual_floor]).all()
        or annual_volatility < 0
        or min(annual_target, annual_floor) <= 0
    ):
        raise ValueError("finite exposure, nonnegative risk and positive target/floor required")
    scaled = exposure * (annual_target / max(annual_volatility, annual_floor))
    return float(np.float32(np.clip(scaled, 0.0, 2.0)))


@dataclass(frozen=True)
class RiskSettings:
    signal_window: int
    portfolio_window: int
    min_observations: int
    update_every: int
    target_annual_volatility: float
    daily_volatility_floor: float
    warmup_position: float
    portfolio_return_basis: Literal["pre_overlay", "post_overlay"]

    def validate(self) -> None:
        for value in (
            self.signal_window,
            self.portfolio_window,
            self.min_observations,
            self.update_every,
        ):
            if isinstance(value, bool) or not isinstance(value, Integral) or value < 1:
                raise ValueError("positive integer windows and update interval required")
        if self.min_observations < 2 or self.min_observations > min(
            self.signal_window, self.portfolio_window
        ):
            raise ValueError("minimum observations must be between two and both windows")
        scalars = [self.target_annual_volatility, self.daily_volatility_floor, self.warmup_position]
        if (
            not np.isfinite(scalars).all()
            or min(scalars[:2]) <= 0
            or not 0 <= self.warmup_position <= 2
        ):
            raise ValueError("invalid explicit target, floor or warmup position")
        if self.portfolio_return_basis not in ("pre_overlay", "post_overlay"):
            raise ValueError("explicit portfolio-return basis required")


@dataclass(frozen=True)
class RiskDecision:
    position: float
    status: str
    risk_updated: bool
    signal_weights: tuple[float, ...] | None
    annualized_portfolio_volatility: float | None
    risk_label_end_max: int | None
    portfolio_return_basis: str


class PublishedRiskOverlay:
    """Causal structural demonstration with supplied signal and portfolio PnL histories.

    History contains realized returns, not forecast magnitudes. Its label-end IDs
    must already be observable at the decision. The caller owns how those returns
    were computed and declares pre/post-overlay basis explicitly; this class cannot
    infer the author's signal definitions, estimator or original parameters.
    """

    def __init__(self, settings: RiskSettings, signal_count: int) -> None:
        settings.validate()
        if (
            isinstance(signal_count, bool)
            or not isinstance(signal_count, Integral)
            or signal_count < 1
        ):
            raise ValueError("positive integer signal count required")
        self.settings = settings
        self.signal_columns = tuple(f"signal_return_{i}" for i in range(signal_count))
        self._last_id: int | None = None
        self._calls = 0
        self._last_refresh_call: int | None = None
        self._weights: tuple[float, ...] | None = None
        self._annual_volatility: float | None = None
        self._risk_label_end_max: int | None = None

    def allocate(self, date_id: int, exposures: np.ndarray, history: pd.DataFrame) -> RiskDecision:
        if isinstance(date_id, bool) or not isinstance(date_id, Integral):
            raise ValueError("integer decision ID required")
        if self._last_id is not None and date_id <= self._last_id:
            raise ValueError("decision IDs must be strictly ordered")
        values = np.asarray(exposures, dtype=float)
        if values.shape != (len(self.signal_columns),) or not np.isfinite(values).all():
            raise ValueError("finite exposure for every declared signal required")
        expected = ["date_id", "label_end_id", *self.signal_columns, "portfolio_return"]
        if list(history) != expected:
            raise ValueError("declared realized-history schema required")
        validate_ids(history)
        ends = history.label_end_id
        if (
            not is_integer_dtype(ends)
            or is_bool_dtype(ends)
            or ends.isna().any()
            or not (ends > history.date_id).all()
            or not (ends <= date_id).all()
            or not (history.date_id < date_id).all()
        ):
            raise ValueError("only already mature history is allowed")
        if not np.isfinite(
            history[[*self.signal_columns, "portfolio_return"]].to_numpy(dtype=float)
        ).all():
            raise ValueError("finite realized returns required")
        enough = len(history) >= self.settings.min_observations
        if not enough and self._weights is not None:
            raise ValueError("insufficient history for existing risk state")
        updated = enough and (
            self._weights is None
            or self._calls - self._last_refresh_call >= self.settings.update_every
        )
        weights, annual_volatility, last_end = (
            self._weights,
            self._annual_volatility,
            self._risk_label_end_max,
        )
        if updated:
            sample = history.tail(self.settings.signal_window)
            volatilities = np.std(sample[list(self.signal_columns)].to_numpy(), axis=0, ddof=1)
            weights = tuple(
                float(x)
                for x in inverse_volatility_weights(
                    volatilities, self.settings.daily_volatility_floor
                )
            )
            annual_volatility = float(
                history.portfolio_return.tail(self.settings.portfolio_window).std(ddof=1)
                * np.sqrt(252)
            )
            last_end = int(history.label_end_id.max())
        if weights is None:
            position = float(np.float32(self.settings.warmup_position))
        else:
            combined = float(np.asarray(weights) @ values)
            position = target_position(
                combined,
                annual_volatility,
                self.settings.target_annual_volatility,
                self.settings.daily_volatility_floor * np.sqrt(252),
            )
        result = RiskDecision(
            position,
            "calibrated" if weights is not None else "warmup",
            updated,
            weights,
            annual_volatility,
            last_end,
            self.settings.portfolio_return_basis,
        )
        # Commit only after validation and all calculations succeeded.
        self._last_id, self._calls = int(date_id), self._calls + 1
        if updated:
            self._weights, self._annual_volatility, self._risk_label_end_max = (
                weights,
                annual_volatility,
                last_end,
            )
            self._last_refresh_call = self._calls - 1
        return result
