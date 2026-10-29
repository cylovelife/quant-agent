"""Fixed Ridge forecasts and causal out-of-fold scale for a linear allocation rule.

@author cylovelife
"""

from collections.abc import Callable
from dataclasses import dataclass, replace

import numpy as np
import pandas as pd
from sklearn.base import BaseEstimator
from sklearn.impute import MissingIndicator, SimpleImputer
from sklearn.linear_model import Ridge
from sklearn.pipeline import FeatureUnion, Pipeline
from sklearn.preprocessing import StandardScaler
from threadpoolctl import threadpool_limits

from quant_agent.competitions.hull.data import (
    LAGGED,
    TARGETS,
    HullData,
    mature_window,
    training_features,
)

TARGET = "market_forward_excess_returns"
RIDGE_PARAMETERS = {"alpha": 100.0, "solver": "lsqr", "tol": 1e-6, "max_iter": 10000}


def prepare_panel(data: HullData, reserved_start: int) -> tuple[pd.DataFrame, tuple[str, ...]]:
    """Remove reserved rows before constructing even the lagged model inputs."""
    history = replace(data, train=data.train.loc[data.train.date_id < reserved_start].copy())
    features = (*history.features, *LAGGED)
    inputs = training_features(history)
    panel = inputs.merge(history.train[["date_id", *TARGETS]], on="date_id", validate="one_to_one")
    return panel, features


def fit_ridge(train: pd.DataFrame, features: tuple[str, ...]) -> Pipeline:
    return fit_forecaster(train, features, Ridge(**RIDGE_PARAMETERS), name="ridge")


def fit_forecaster(
    train: pd.DataFrame, features: tuple[str, ...], estimator: BaseEstimator, *, name: str
) -> Pipeline:
    """Fit all preprocessing only on this mature historical training segment."""
    if not features or len(set(features)) != len(features):
        raise ValueError("unique nonempty features required")
    if set(features) & {"date_id", "is_scored", "label_end_id", *TARGETS}:
        raise ValueError("current labels and metadata are forbidden model inputs")
    if len(train) < 2 or not np.isfinite(train[TARGET]).all():
        raise ValueError("finite mature training targets required")
    values = train[list(features)]
    if np.isinf(values.to_numpy(dtype=float)).any():
        raise ValueError("infinite feature values are not allowed")
    preprocessing = FeatureUnion(
        [
            (
                "values",
                Pipeline(
                    [
                        ("impute", SimpleImputer(strategy="median", keep_empty_features=True)),
                        ("scale", StandardScaler()),
                    ]
                ),
            ),
            ("missing", MissingIndicator(features="all", error_on_new=False)),
        ]
    )
    model = Pipeline([("features", preprocessing), (name, estimator)])
    with threadpool_limits(limits=1):
        model.fit(values, train[TARGET])
    return model


@dataclass(frozen=True)
class LinearAllocation:
    rms: float
    gain: float

    @classmethod
    def from_oof(cls, forecasts: np.ndarray) -> "LinearAllocation":
        values = np.asarray(forecasts, dtype=float)
        if values.ndim != 1 or len(values) == 0 or not np.isfinite(values).all():
            raise ValueError("finite nonempty out-of-fold forecasts required")
        rms = float(np.sqrt(np.mean(values**2)))
        if not np.isfinite(rms):
            raise ValueError("invalid out-of-fold signal scale")
        return cls(rms=rms, gain=0.25 / rms if rms >= 1e-8 else 0.0)

    def apply(self, forecasts: np.ndarray) -> np.ndarray:
        values = np.asarray(forecasts, dtype=float)
        if values.ndim != 1 or not np.isfinite(values).all():
            raise ValueError("finite one-dimensional forecasts required")
        # Official Python-float scalar payload uses protobuf float32.
        return np.clip(1.0 + self.gain * values, 0.0, 2.0).astype(np.float32).astype(float)


@dataclass
class ForecastWindow:
    model: Pipeline
    features: tuple[str, ...]
    mapping: LinearAllocation
    predictions: pd.DataFrame
    oof: pd.DataFrame
    folds: list[dict]
    model_fits: int
    training_rows: int
    training_target_mean: float

    def allocate(self, row: pd.DataFrame) -> float:
        if list(row) != list(self.features) or len(row) != 1:
            raise ValueError("one visible feature row in frozen order required")
        with threadpool_limits(limits=1):
            forecast = self.model.predict(row)
        return float(self.mapping.apply(forecast)[0])


# Preserve the already-used Ridge API while sharing the identical policy container.
RidgeWindow = ForecastWindow


def run_window(
    panel: pd.DataFrame,
    features: tuple[str, ...],
    start: int,
    end: int,
    *,
    blocks: int = 3,
    block_rows: int = 252,
    fit_model: Callable[[pd.DataFrame, tuple[str, ...]], Pipeline] = fit_ridge,
    max_train_rows: int | None = None,
) -> ForecastWindow:
    """Calibrate strictly before the evaluation start; never fit on evaluation labels."""
    if blocks < 1 or block_rows < 3:
        raise ValueError("positive calibration blocks of at least three rows required")
    if max_train_rows is not None and (
        isinstance(max_train_rows, bool)
        or not isinstance(max_train_rows, int)
        or max_train_rows < 2
    ):
        raise ValueError("training row cap must be an integer of at least two")
    train, valid = mature_window(panel, start, end)
    if max_train_rows is not None:
        train = train.tail(max_train_rows).copy()
    forecasts, folds = [], []
    for begin in range(start - blocks * block_rows, start, block_rows):
        fit, calibration = mature_window(panel, begin, begin + block_rows)
        if max_train_rows is not None:
            fit = fit.tail(max_train_rows).copy()
        if calibration.label_end_id.max() >= start:
            raise ValueError("calibration labels cross evaluation start")
        model = fit_model(fit, features)
        with threadpool_limits(limits=1):
            prediction = model.predict(calibration[list(features)])
        forecasts.append(calibration[["date_id", "label_end_id"]].assign(forecast=prediction))
        folds.append(
            {
                "window": [begin, begin + block_rows],
                "training_rows": len(fit),
                "training_id_max": int(fit.date_id.max()),
                "training_label_end_max": int(fit.label_end_id.max()),
                "oof_rows": len(calibration),
                "oof_label_end_max": int(calibration.label_end_id.max()),
            }
        )
    oof = pd.concat(forecasts, ignore_index=True)
    mapping = LinearAllocation.from_oof(oof.forecast.to_numpy())
    model = fit_model(train, features)
    with threadpool_limits(limits=1):
        forecast = model.predict(valid[list(features)])
    mean = float(train[TARGET].mean())
    predictions = valid[["date_id", TARGET]].copy().rename(columns={TARGET: "target"})
    predictions["forecast"] = forecast
    predictions["allocation"] = mapping.apply(forecast)
    predictions["mean_forecast"] = mean
    predictions["mean_allocation"] = mapping.apply(np.full(len(valid), mean))
    return ForecastWindow(
        model, features, mapping, predictions, oof, folds, blocks + 1, len(train), mean
    )
