"""One observed row per prediction, with explicit visibility and reset boundaries.

@author cylovelife
"""

from collections.abc import Callable
from numbers import Real

import numpy as np
import pandas as pd
import polars as pl

from quant_agent.competitions.hull.data import TARGETS, scored_mask
from quant_agent.competitions.hull.metrics import validate_ids


class SequentialPredictor:
    """Guard a stateless fitted callback; reset sequence state between independent runs."""

    def __init__(self, features: tuple[str, ...], callback: Callable[[pd.DataFrame], float]):
        if not features or len(features) != len(set(features)):
            raise ValueError("nonempty unique feature names required")
        if set(features) & {"date_id", "is_scored", *TARGETS}:
            raise ValueError("metadata or current labels cannot be model features")
        self.features = features
        self.callback = callback
        self.last_id: int | None = None
        self.rows_seen = 0

    def reset(self) -> None:
        self.last_id = None
        self.rows_seen = 0

    def predict(self, batch: pd.DataFrame | pl.DataFrame) -> float:
        if isinstance(batch, pl.DataFrame):
            batch = batch.to_pandas()
        if not isinstance(batch, pd.DataFrame) or len(batch) != 1:
            raise ValueError("exactly one observed row required")
        validate_ids(batch)
        if set(batch) != {"date_id", "is_scored", *self.features}:
            raise ValueError("unexpected visible columns or current labels")
        scored_mask(batch.is_scored)
        current = int(batch.date_id.iloc[0])
        if self.last_id is not None and current <= self.last_id:
            raise ValueError("prediction order must strictly increase; reset before replay")
        value = self.callback(batch[list(self.features)].copy())
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ValueError("prediction must be a numeric scalar")
        if not np.isfinite(value) or not 0 <= value <= 2:
            raise ValueError("prediction must be finite and within [0, 2]")
        self.last_id = current
        self.rows_seen += 1
        return float(value)
