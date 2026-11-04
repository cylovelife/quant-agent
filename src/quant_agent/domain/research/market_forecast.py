"""Causal short-horizon forecasts, measured against simple baselines before display.

No trading policy or parameter promotion. All comparison windows are development data.
@author cylovelife
"""

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class ForecastSettings:
    horizon: int = 5
    min_train: int = 80
    folds: int = 3
    fold_size: int = 20
    alpha: float = 10.0

    def __post_init__(self) -> None:
        sizes = (self.horizon, self.min_train, self.folds, self.fold_size)
        if any(isinstance(x, bool) or not isinstance(x, int) for x in sizes):
            raise ValueError("integer forecast sizes required")
        if (
            self.horizon < 1
            or self.min_train < 20
            or self.folds < 2
            or self.fold_size < self.horizon + 10
            or not np.isfinite(self.alpha)
            or self.alpha <= 0
        ):
            raise ValueError("invalid forecast settings")


def build_forecast_panel(
    bars: pd.DataFrame, *, adjust: str, horizon: int = 5
) -> tuple[pd.DataFrame, list[str]]:
    """Features use only observations through t; labels end at the actual t+h row."""
    if adjust != "qfq":
        raise ValueError("short forecasts require explicit qfq bars")
    provenance = bars.attrs.get("data_provenance") or {}
    if not isinstance(provenance, dict):
        raise ValueError("invalid bar provenance")
    declared = (bars.attrs.get("adjust"), provenance.get("adjust"))
    if any(value is not None and value != adjust for value in declared):
        raise ValueError("bar provenance conflicts with requested adjustment")
    if isinstance(horizon, bool) or not isinstance(horizon, int) or horizon < 1:
        raise ValueError("positive integer horizon required")
    required = ["date", "open", "high", "low", "close"]
    if bars.empty or not set(required).issubset(bars):
        raise ValueError("nonempty dated OHLC bars required")
    work = bars.reset_index(drop=True).copy()
    dates = pd.to_datetime(work.date, errors="coerce")
    if (
        dates.isna().any()
        or not dates.is_unique
        or not dates.is_monotonic_increasing
        or not dates.eq(dates.dt.normalize()).all()
    ):
        raise ValueError("unique ordered daily dates required")
    prices = work[required[1:]].apply(pd.to_numeric, errors="coerce")
    if not np.isfinite(prices.to_numpy()).all() or (prices <= 0).any().any():
        raise ValueError("finite positive OHLC prices required")
    if (prices.high < prices[["open", "close", "low"]].max(axis=1)).any() or (
        prices.low > prices[["open", "close", "high"]].min(axis=1)
    ).any():
        raise ValueError("inconsistent OHLC prices")
    close = prices.close
    daily = close.pct_change(fill_method=None)
    values = {}
    for window in (1, 5, 20, 60):
        values[f"return_{window}"] = close.pct_change(window, fill_method=None)
    for window in (5, 20, 60):
        values[f"daily_rms_{window}"] = np.sqrt(daily.pow(2).rolling(window).mean())
    sigma = daily.rolling(20).std(ddof=1).replace(0, np.nan)
    values["standardized_move_5"] = daily.rolling(5).sum() / (sigma * np.sqrt(5))
    values["vol_ratio_5_60"] = values["daily_rms_5"] / values["daily_rms_60"].replace(0, np.nan)
    values["trend_gap_60"] = close / close.rolling(60).mean() - 1
    values["drawdown_60"] = close / close.rolling(60).max() - 1
    values["intraday"] = close / prices.open - 1
    values["open_gap"] = prices.open / close.shift(1) - 1
    values["range"] = (prices.high - prices.low) / close
    width = (prices.high - prices.low).replace(0, np.nan)
    values["close_position"] = (close - prices.low) / width
    values["repeated_ohlc"] = prices.eq(prices.shift(1)).all(axis=1).astype(float)
    for name in ("volume", "amount"):
        series = pd.to_numeric(work.get(name, pd.Series(np.nan, index=work.index)), errors="coerce")
        series = series.where(np.isfinite(series) & (series >= 0))
        values[f"{name}_ratio_20"] = series / series.rolling(20).mean().replace(0, np.nan)
        values[f"{name}_missing"] = series.isna().astype(float)
    frame = pd.DataFrame(values).replace([np.inf, -np.inf], np.nan)
    features = list(frame)
    frame["date"] = dates
    frame["target_return"] = close.shift(-horizon) / close - 1
    frame["target_rms"] = np.sqrt(daily.pow(2).rolling(horizon).mean().shift(-horizon))
    frame["label_end"] = dates.shift(-horizon)
    # Warmup is excluded rather than imputed as a collection of zero indicators.
    frame["ready"] = np.arange(len(frame)) >= 60
    return frame, features


def stable_improvement(candidate: list[float], baseline: list[float]) -> bool:
    """A fixed development gate; this is not a significance or promotion test."""
    a, b = np.asarray(candidate), np.asarray(baseline)
    if len(a) < 2 or a.shape != b.shape or not np.isfinite([a, b]).all():
        return False
    if b.mean() <= 1e-12:
        return False
    return bool(a.mean() <= b.mean() * 0.98 and np.all(a <= b * 1.05 + 1e-12))


def _ridge(alpha: float):
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import Ridge
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    return make_pipeline(
        SimpleImputer(strategy="median", add_indicator=True, keep_empty_features=True),
        StandardScaler(),
        Ridge(alpha=alpha),
    )


def _predict(
    train: pd.DataFrame, valid: pd.DataFrame, features: list[str], alpha: float
) -> tuple[np.ndarray, np.ndarray]:
    model = _ridge(alpha)
    model.fit(train[features], train.target_return)
    returns = model.predict(valid[features])
    risk = _ridge(alpha)
    risk.fit(train[features], np.log(train.target_rms.clip(lower=1e-10)))
    # Numerical guard only; no future distribution is used to set a clipping threshold.
    rms = np.exp(np.clip(risk.predict(valid[features]), -30, 20))
    if not np.isfinite(returns).all() or not np.isfinite(rms).all():
        raise ValueError("nonfinite model predictions")
    return returns, rms


def _errors(actual: np.ndarray, predicted: np.ndarray) -> dict:
    error = actual - predicted
    return {"mae": float(np.abs(error).mean()), "rmse": float(np.sqrt((error**2).mean()))}


def forecast_short_term(
    bars: pd.DataFrame,
    *,
    adjust: str,
    asof: str,
    settings: ForecastSettings = ForecastSettings(),
) -> dict:
    """Research projection using matured labels and a bounded set of fixed models.

    The three trailing development windows are reused for model selection and residual
    summaries. Neither their scores nor the empirical range are confirmation evidence.
    Input must already be limited to completed daily bars by the application boundary.
    """
    result = {
        "version": 1,
        "settings": asdict(settings),
        "status": "insufficient_history",
        "reason": "insufficient purged training or validation history",
        "horizon": settings.horizon,
        "price_basis": adjust,
        "history_basis": "current_qfq_snapshot",
        "point_in_time_complete": False,
        "evaluation_kind": "rolling_development",
        "promotion_allowed": False,
        "expected_return": None,
        "daily_rms": None,
        "return_reference_range": None,
        "range_kind": "development_residual_quantiles_not_calibrated",
        "validation": [],
    }
    try:
        cutoff = pd.Timestamp(asof)
        if pd.isna(cutoff) or cutoff != cutoff.normalize():
            raise ValueError("daily cutoff required")
        dates = pd.to_datetime(bars["date"], errors="coerce")
        if dates.isna().any():
            raise ValueError("invalid observation dates")
        visible = bars.loc[dates <= cutoff].copy()
        panel, features = build_forecast_panel(visible, adjust=adjust, horizon=settings.horizon)
    except (KeyError, TypeError, ValueError) as exc:
        return result | {"status": "data_blocked", "reason": str(exc)}
    result.update(
        asof=str(panel.date.iloc[-1].date()),
        requested_asof=asof,
        bars=len(panel),
        features=features,
        missing_inputs=[
            name for name in ("volume", "amount") if panel[f"{name}_missing"].iloc[-1] == 1
        ],
    )
    valid_labels = panel.ready & panel.label_end.notna()
    folds, held = [], []
    end_position = len(panel) - 1  # The live row never supplies a development outcome.
    first_position = end_position - settings.folds * settings.fold_size
    if first_position < 0:
        return result
    for position in range(first_position, end_position, settings.fold_size):
        begin = panel.date.iloc[position]
        end = panel.date.iloc[position + settings.fold_size]
        train = panel.loc[valid_labels & (panel.label_end < begin)]
        val = panel.loc[
            valid_labels & (panel.date >= begin) & (panel.date < end) & (panel.label_end < end)
        ]
        if len(train) < settings.min_train or len(val) < 10:
            return result
        try:
            predicted_return, predicted_rms = _predict(train, val, features, settings.alpha)
        except ImportError as exc:
            return result | {"status": "dependency_unavailable", "reason": str(exc)}
        except (ValueError, FloatingPointError, np.linalg.LinAlgError) as exc:
            return result | {"status": "model_unavailable", "reason": str(exc)}
        compared = val[
            ["date", "target_return", "target_rms", "daily_rms_20", "vol_ratio_5_60"]
        ].copy()
        compared["predicted_return"] = predicted_return
        compared["predicted_rms"] = predicted_rms
        held.append(compared)
        actual = val.target_return.to_numpy()
        risk = val.target_rms.to_numpy()
        folds.append(
            {
                "start": str(begin.date()),
                "end_exclusive": str(end.date()),
                "train_label_end": str(train.label_end.max().date()),
                "label_end": str(val.label_end.max().date()),
                "train_rows": len(train),
                "n": len(val),
                "return_ridge": _errors(actual, predicted_return),
                "return_zero": _errors(actual, np.zeros(len(val))),
                "risk_ridge": _errors(risk, predicted_rms),
                "risk_history": _errors(risk, val.daily_rms_20.to_numpy()),
            }
        )
    return_gain = stable_improvement(
        [f["return_ridge"]["mae"] for f in folds], [f["return_zero"]["mae"] for f in folds]
    )
    risk_gain = stable_improvement(
        [f["risk_ridge"]["mae"] for f in folds], [f["risk_history"]["mae"] for f in folds]
    )
    latest = panel.tail(1)
    train = panel.loc[valid_labels & (panel.label_end < latest.date.iloc[0])]
    try:
        predicted_return, predicted_rms = _predict(train, latest, features, settings.alpha)
    except (ValueError, FloatingPointError, np.linalg.LinAlgError) as exc:
        return result | {"status": "model_unavailable", "reason": str(exc)}
    point = float(predicted_return[0]) if return_gain else 0.0
    risk = float(predicted_rms[0]) if risk_gain else float(latest.daily_rms_20.iloc[0])
    oof = pd.concat(held, ignore_index=True)
    residuals = oof.target_return - (oof.predicted_return if return_gain else 0.0)
    bounds = np.quantile(residuals, [0.1, 0.9]) + point
    slices = {}
    for name, mask in {
        "volatility_expanding": oof.vol_ratio_5_60 > 1.25,
        "other_observed": oof.vol_ratio_5_60 <= 1.25,
    }.items():
        sub = oof.loc[mask]
        if len(sub):
            slices[name] = {
                "n": len(sub),
                "return_ridge": _errors(
                    sub.target_return.to_numpy(), sub.predicted_return.to_numpy()
                ),
                "return_zero": _errors(sub.target_return.to_numpy(), np.zeros(len(sub))),
            }
    return result | {
        "status": "observed",
        "reason": "development selection only; no independent confirmation",
        "return_model": "ridge" if return_gain else "zero",
        "risk_model": "ridge_log_rms" if risk_gain else "historical_rms_20",
        "expected_return": point,
        "daily_rms": risk,
        "return_reference_range": bounds.tolist(),
        "validation": folds,
        "slices": slices,
        "train_rows": len(train),
        "train_label_end": str(train.label_end.max().date()),
        "validation_rows": len(oof),
        "candidate_prediction": {
            "expected_return": float(predicted_return[0]),
            "daily_rms": float(predicted_rms[0]),
        },
    }
