"""Development-only Ridge comparison, with fixed allocation and reserved-score denial.

@author cylovelife
"""

import argparse
import hashlib
import importlib.metadata
import json
import logging
import time
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd

from quant_agent.competitions.hull.data import load_dataset, mature_window
from quant_agent.competitions.hull.metrics import score_allocations
from quant_agent.competitions.hull.ridge import RIDGE_PARAMETERS, TARGET, prepare_panel, run_window
from quant_agent.competitions.hull.study import write_new_json

LOGGER = logging.getLogger(__name__)


def portfolio_report(solution: pd.DataFrame, allocations: np.ndarray) -> dict:
    predictions = pd.DataFrame({"date_id": solution.date_id.to_numpy(), "prediction": allocations})
    score = score_allocations(solution, predictions)
    market = solution.forward_returns.to_numpy()
    cash = solution.risk_free_rate.to_numpy()
    returns = cash * (1 - allocations) + allocations * market
    equity = np.r_[1.0, np.cumprod(1 + returns)]
    market_equity = np.cumprod(1 + market)
    volatility = float(np.std(returns, ddof=1))
    market_volatility = float(np.std(market, ddof=1))
    return {
        "official_score": score,
        "gross_total_return": float(equity[-1] - 1),
        "market_total_return": float(market_equity[-1] - 1),
        "gross_return_difference": float(equity[-1] - market_equity[-1]),
        "annualized_volatility": volatility * np.sqrt(252),
        "market_volatility_ratio": volatility / market_volatility,
        "max_drawdown": float(np.min(equity / np.maximum.accumulate(equity) - 1)),
        "allocation_mean": float(np.mean(allocations)),
        "allocation_min": float(np.min(allocations)),
        "allocation_max": float(np.max(allocations)),
        "allocation_boundary_fraction": float(np.mean((allocations == 0) | (allocations == 2))),
        "mean_absolute_allocation_change": float(np.mean(np.abs(np.diff(allocations)))),
        "transaction_costs_included": False,
    }


def validate_plan(
    plan: dict,
    train: pd.DataFrame,
    train_sha: str,
    *,
    estimator_key: str = "ridge",
    expected_parameters: dict | None = None,
) -> None:
    if plan["schema_version"] != 1 or plan["reserved_window_evaluation_allowed"]:
        raise ValueError("unsupported protocol or forbidden reserved evaluation")
    if plan["expected_training_rows"] != len(train) or plan["expected_train_sha256"] != train_sha:
        raise ValueError("frozen input identity mismatch")
    parameters = RIDGE_PARAMETERS if expected_parameters is None else expected_parameters
    if plan[estimator_key] != parameters or plan["target"] != TARGET or plan["search_allowed"]:
        raise ValueError("only the fixed estimator target and parameters are allowed")
    reserved_start, reserved_end = plan["reserved_window"]
    windows = plan["development_windows"]
    if reserved_start >= reserved_end or reserved_end <= train.date_id.max():
        raise ValueError("reserved window must include latest observations")
    if len(windows) != 2 or any(a >= b or b > reserved_start for a, b in windows):
        raise ValueError("two development windows strictly before reserved segment required")
    ordered = sorted(windows)
    if ordered[0][1] > ordered[1][0]:
        raise ValueError("overlapping development evaluation windows")
    calibration = plan["calibration"]
    if calibration["blocks"] < 1 or calibration["block_rows"] < 3:
        raise ValueError("invalid calibration windows")
    if any(
        a - calibration["blocks"] * calibration["block_rows"] <= train.date_id.min()
        for a, _ in windows
    ):
        raise ValueError("insufficient earlier training history for calibration")
    if plan["max_model_fits"] != len(windows) * (calibration["blocks"] + 1):
        raise ValueError("fit budget differs from fixed execution")
    mapping = plan["mapping"]
    if (mapping["center"], mapping["strength"], mapping["rms_floor"], mapping["bounds"]) != (
        1.0,
        0.25,
        1e-8,
        [0.0, 2.0],
    ):
        raise ValueError("allocation mapping differs from preregistered implementation")
    if (
        plan["candidate_configurations"] != 1
        or plan["platform_submission_allowed"]
        or plan["promotion_to_personal_strategy_allowed"]
    ):
        raise ValueError("research-only single configuration required")


def run_study(
    raw: Path,
    manifest: Path,
    protocol: Path,
    output: Path,
    *,
    check_api: bool = False,
) -> dict:
    """No training, prediction, score or artifacts from the reserved rows."""
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    completed_fits = 0
    try:
        plan = json.loads(protocol.read_text())
        data = load_dataset(raw, manifest)
        entries = json.loads(manifest.read_text())["files"]
        train_sha = next(item["sha256"] for item in entries if item["path"] == "train.csv")
        validate_plan(plan, data.train, train_sha)
        panel, features = prepare_panel(data, plan["reserved_window"][0])
        frozen = {
            "protocol": plan,
            "api_check_requested": check_api,
            "protocol_sha256": hashlib.sha256(protocol.read_bytes()).hexdigest(),
            "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
            "source_sha256": {
                p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(Path(__file__).parent.glob("*.py"))
            },
            "packages": {
                p: importlib.metadata.version(p)
                for p in (
                    "numpy",
                    "pandas",
                    "scikit-learn",
                    "polars",
                    "pyarrow",
                    "grpcio",
                    "protobuf",
                )
            },
        }
        write_new_json(output / "frozen.json", frozen)
        records = []
        for number, (start, end) in enumerate(plan["development_windows"], 1):
            fitted = run_window(
                panel,
                features,
                start,
                end,
                blocks=plan["calibration"]["blocks"],
                block_rows=plan["calibration"]["block_rows"],
            )
            completed_fits += fitted.model_fits
            _, valid = mature_window(panel, start, end)
            if valid.date_id.max() >= plan["reserved_window"][0]:
                raise ValueError("reserved scoring forbidden")
            forecasts = fitted.predictions
            y = forecasts.target.to_numpy()
            pred = forecasts.forecast.to_numpy()
            mean = forecasts.mean_forecast.to_numpy()
            mse = float(np.mean((y - pred) ** 2))
            reference_mse = float(np.mean((y - mean) ** 2))
            correlation = (
                float(np.corrcoef(y, pred)[0, 1]) if np.std(y) > 0 and np.std(pred) > 0 else None
            )
            record = {
                "window": [start, end],
                "training_mature_rows": fitted.training_rows,
                "evaluation_mature_rows": len(valid),
                "evaluation_id_range": [int(valid.date_id.min()), int(valid.date_id.max())],
                "mapping": asdict(fitted.mapping),
                "oof_rows": len(fitted.oof),
                "calibration_folds": fitted.folds,
                "prediction_metrics": {
                    "ridge_mse": mse,
                    "training_mean_mse": reference_mse,
                    "relative_mse_improvement": 1 - mse / reference_mse
                    if reference_mse > 0
                    else None,
                    "pearson_correlation": correlation,
                },
                "ridge": portfolio_report(valid, forecasts.allocation.to_numpy()),
                "mapped_training_target_mean": portfolio_report(
                    valid, forecasts.mean_allocation.to_numpy()
                ),
                "constants": {
                    str(w): portfolio_report(valid, np.full(len(valid), w)) for w in (0.5, 1.0, 1.5)
                },
            }
            records.append(record)
            forecasts.to_csv(
                output / f"development-{number}-predictions.csv", index=False, mode="x"
            )
            fitted.oof.to_csv(
                output / f"development-{number}-calibration-oof.csv", index=False, mode="x"
            )
            write_new_json(
                output / f"development-{number}-model.json",
                {
                    "features": list(features),
                    "transformed_features": ["value::" + c for c in features]
                    + ["missing::" + c for c in features],
                    "coefficients": fitted.model.named_steps["ridge"].coef_.tolist(),
                    "intercept": float(fitted.model.named_steps["ridge"].intercept_),
                    "iterations": fitted.model.named_steps["ridge"].n_iter_.tolist(),
                    "mapping": asdict(fitted.mapping),
                },
            )
        api = None
        if check_api:
            from quant_agent.competitions.hull.ridge_api import replay_development

            replay = panel.loc[panel.date_id.isin(fitted.predictions.date_id)].head(16)
            api = replay_development(
                raw.resolve(), replay, fitted, output / "api-development-replay"
            )
        result = {
            "task": "historical_ridge_fixed_mapping",
            "windows": records,
            "base_feature_columns": len(data.features),
            "model_input_columns": len(features),
            "transformed_input_columns": 2 * len(features),
            "model_fits": completed_fits,
            "candidate_configurations": 1,
            "reserved_evaluations": 0,
            "reserved_predictions": 0,
            "reserved_window": plan["reserved_window"],
            "selected_for_promotion": False,
            "submitted": False,
            "official_api_development_replay": api,
            "elapsed_seconds": time.perf_counter() - started,
        }
        write_new_json(output / "results.json", result)
        return result
    except Exception as exc:
        write_new_json(
            output / "failure.json",
            {
                "type": type(exc).__name__,
                "message": str(exc),
                "completed_model_fits": completed_fits,
            },
        )
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("raw", "manifest", "protocol", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--check-api", action="store_true")
    args = parser.parse_args()
    result = run_study(
        args.raw, args.manifest, args.protocol, args.output, check_api=args.check_api
    )
    LOGGER.info(
        "windows=%d fits=%d reserved_evaluations=0", len(result["windows"]), result["model_fits"]
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
