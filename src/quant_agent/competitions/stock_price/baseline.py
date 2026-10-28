"""Budgeted stock competition baselines, causal features and frozen evaluation.

@author cylovelife
"""

import argparse
import hashlib
import importlib.metadata
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline, make_pipeline
from sklearn.preprocessing import StandardScaler
from threadpoolctl import threadpool_limits

from quant_agent.competitions.stock_price.data import load_dataset
from quant_agent.competitions.stock_price.metrics import classification_metrics, write_submission
from quant_agent.competitions.stock_price.validation import purged_window

LOGGER = logging.getLogger(__name__)
CANDIDATES = ("calendar", "logistic", "hist_gradient")
LOGISTIC = {"C": 0.1, "max_iter": 1000, "random_state": 0}
HISTOGRAM = {
    "max_depth": 2,
    "max_leaf_nodes": 4,
    "max_iter": 60,
    "learning_rate": 0.05,
    "min_samples_leaf": 40,
    "l2_regularization": 10.0,
    "early_stopping": False,
    "random_state": 0,
}


def build_features(prices: pd.DataFrame) -> pd.DataFrame:
    """No labels, forward shifts, backfilling or fitted full-data transforms."""
    dates = pd.to_datetime(prices.Date)
    if not dates.is_monotonic_increasing or not dates.is_unique:
        raise ValueError("features require unique ordered dates")
    close = prices.Close.astype(float)
    result = pd.DataFrame({"Date": dates})
    for day in range(7):
        result[f"weekday_{day}"] = dates.dt.dayofweek.eq(day).astype(float)
    for lag in (1, 3, 7, 14):
        result[f"return_{lag}"] = close.pct_change(lag, fill_method=None)
    result["intraday"] = close / prices.Open - 1
    result["open_gap"] = prices.Open / close.shift(1) - 1
    result["range"] = (prices.High - prices.Low) / close
    width = (prices.High - prices.Low).replace(0, np.nan)
    result["close_position"] = ((close - prices.Low) / width).fillna(0.5)
    result["volatility_7"] = result.return_1.rolling(7, min_periods=7).std()
    result["distance_mean_14"] = close / close.rolling(14, min_periods=14).mean() - 1
    columns = ["High", "Low", "Open", "Close"]
    result["repeated_ohlc"] = prices[columns].eq(prices[columns].shift(1)).all(axis=1).astype(float)
    return result.replace([np.inf, -np.inf], np.nan)


@dataclass(frozen=True)
class CalendarModel:
    rates: dict[int, float]
    fallback: float

    def predict(self, frame: pd.DataFrame) -> np.ndarray:
        rates = frame.Date.dt.dayofweek.map(self.rates).fillna(self.fallback)
        return (rates >= 0.5).to_numpy(dtype=int)


def fit_predict(
    kind: str, train: pd.DataFrame, evaluate: pd.DataFrame, features: list[str]
) -> tuple[np.ndarray, CalendarModel | Pipeline | HistGradientBoostingClassifier]:
    if train.Target.nunique() < 2:
        raise ValueError("training window needs both classes")
    if kind == "calendar":
        rates = train.groupby(train.Date.dt.dayofweek).Target.mean().to_dict()
        model = CalendarModel(rates, float(train.Target.mean()))
        return model.predict(evaluate), model
    if kind == "logistic":
        model = make_pipeline(
            SimpleImputer(strategy="median"),
            StandardScaler(),
            LogisticRegression(**LOGISTIC),
        )
    elif kind == "hist_gradient":
        model = HistGradientBoostingClassifier(**HISTOGRAM)
    else:
        raise ValueError(f"unknown model: {kind}")
    with threadpool_limits(limits=1):
        model.fit(train[features], train.Target)
        prediction = model.predict(evaluate[features])
    return prediction.astype(int), model


def development_scores(panel: pd.DataFrame, features: list[str], protocol: dict) -> dict:
    """Selection cannot read labels from the reserved final window."""
    cutoff = pd.Timestamp(protocol["holdout_start"])
    development = panel.loc[(panel.Date < cutoff) & (panel.label_end < cutoff)]
    results = {name: [] for name in CANDIDATES}
    for start, end in protocol["development_windows"]:
        if pd.Timestamp(end) > cutoff:
            raise ValueError("development window crosses final holdout")
        train, valid = purged_window(development, start, end)
        for name in CANDIDATES:
            pred, _ = fit_predict(name, train, valid, features)
            results[name].append(classification_metrics(valid.Target, pred))
    return results


def choose_model(scores: dict[str, list[float]], margin: float) -> str:
    if not 0 <= margin <= 1:
        raise ValueError("invalid selection margin")
    selected = CANDIDATES[0]
    for name in CANDIDATES[1:]:
        if (
            np.mean(scores[name]) >= np.mean(scores[selected]) + margin
            and min(scores[name]) >= min(scores[selected]) - margin
        ):
            selected = name
    return selected


def _json_new(path: Path, value: dict) -> None:
    with path.open("x") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        stream.write("\n")


def run_study(
    raw_root: str | Path,
    contract_path: str | Path,
    protocol_path: str | Path,
    output: str | Path,
) -> dict:
    """Single-use local study directory. Errors remain recorded, not silently retried."""
    started = time.perf_counter()
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    protocol = json.loads(Path(protocol_path).read_text())
    if protocol.get("schema_version") != 1 or protocol["max_candidate_evaluations"] < len(
        CANDIDATES
    ):
        raise ValueError("unsupported protocol or insufficient fixed baseline budget")
    if (
        protocol.get("logistic", LOGISTIC) != LOGISTIC
        or protocol.get("hist_gradient", HISTOGRAM) != HISTOGRAM
    ):
        raise ValueError("declared model parameters differ from implementation")
    source = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(Path(__file__).parent.glob("*.py"))
    }
    frozen = {
        "protocol": protocol,
        "source_sha256": source,
        "contract_sha256": hashlib.sha256(Path(contract_path).read_bytes()).hexdigest(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "packages": {p: importlib.metadata.version(p) for p in ("numpy", "pandas", "scikit-learn")},
    }
    _json_new(output / "frozen_protocol.json", frozen)
    try:
        data = load_dataset(raw_root, contract_path)
        feature_frame = build_features(data.prices).dropna()
        names = list(feature_frame.columns.drop("Date"))
        panel = data.labels.merge(feature_frame, on="Date", validate="one_to_one")
        scores = development_scores(panel, names, protocol)
        references = []
        for start, end in protocol["development_windows"]:
            train, valid = purged_window(panel, start, end)
            majority = int(train.Target.mean() >= 0.5)
            references.append(classification_metrics(valid.Target, np.full(len(valid), majority)))
        selected = choose_model(
            {k: [m["accuracy"] for m in v] for k, v in scores.items()},
            protocol["selection_margin"],
        )
        # Persist the choice BEFORE consuming the reserved labels.
        _json_new(
            output / "locked_selection.json",
            {"selected_model": selected, "development": scores},
        )
        holdout_train, holdout = purged_window(
            panel, protocol["holdout_start"], protocol["holdout_end_exclusive"]
        )
        prediction, _ = fit_predict(selected, holdout_train, holdout, names)
        holdout_metrics = classification_metrics(holdout.Target, prediction)
        reference = int(holdout_train.Target.mean() >= 0.5)
        reference_metrics = classification_metrics(holdout.Target, np.full(len(holdout), reference))
        holdout_records = holdout[["Date", "Target"]].assign(Prediction=prediction)
        holdout_records.to_csv(output / "holdout_predictions.csv", index=False)
        final_train = panel.loc[panel.label_end < data.prices.Date.iloc[-len(data.sample)]]
        # The excluded terminal TRAIN price is history, never a test prediction.
        inference = feature_frame.loc[
            feature_frame.Date >= data.prices.Date.iloc[-len(data.sample)]
        ]
        if len(inference) != len(data.sample):
            raise ValueError("test features missing or reordered")
        final_prediction, _ = fit_predict(selected, final_train, inference, names)
        write_submission(data.sample.Id, final_prediction, output / "submission.csv")
        result = {
            "selected_model": selected,
            "features": names,
            "development": scores,
            "development_majority_reference": references,
            "holdout": holdout_metrics,
            "holdout_majority_reference": reference_metrics,
            "holdout_start": protocol["holdout_start"],
            "holdout_end_exclusive": protocol["holdout_end_exclusive"],
            "development_windows": protocol["development_windows"],
            "final_training_rows": len(final_train),
            "file_hashes": data.file_hashes,
            "candidate_configurations": len(CANDIDATES),
            "candidate_evaluations": len(CANDIDATES),
            "development_model_fits": len(CANDIDATES) * len(protocol["development_windows"]),
            "reserved_evaluations": 1,
            "submission_refits": 1,
            "submission": {
                "rows": len(final_prediction),
                "positive_predictions": int(final_prediction.sum()),
                "sha256": hashlib.sha256((output / "submission.csv").read_bytes()).hexdigest(),
                "platform_status": "not_submitted",
            },
            "elapsed_seconds": time.perf_counter() - started,
            "test_labels_derived": False,
            "promotion_allowed": False,
            "state_scope": "single-use output directory; not an OS-wide sealed-test access control",
        }
        _json_new(output / "results.json", result)
        return result
    except Exception as exc:
        _json_new(output / "failure.json", {"type": type(exc).__name__, "message": str(exc)})
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ("raw", "contract", "protocol", "output"):
        parser.add_argument("--" + option, type=Path, required=True)
    args = parser.parse_args()
    result = run_study(args.raw, args.contract, args.protocol, args.output)
    LOGGER.info(
        "selected=%s holdout_accuracy=%.5f output=%s",
        result["selected_model"],
        result["holdout"]["accuracy"],
        args.output,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
