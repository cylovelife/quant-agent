"""Controlled CPU tree/Ridge comparison with shared visibility and allocation rules.

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
from threadpoolctl import threadpool_limits

from quant_agent.competitions.hull.data import load_dataset, mature_window
from quant_agent.competitions.hull.ridge import (
    RIDGE_PARAMETERS,
    TARGET,
    ForecastWindow,
    fit_ridge,
    prepare_panel,
    run_window,
)
from quant_agent.competitions.hull.ridge_api import replay_development
from quant_agent.competitions.hull.ridge_study import portfolio_report, validate_plan
from quant_agent.competitions.hull.study import write_new_json
from quant_agent.competitions.hull.tree import TREE_PARAMETERS, fit_tree

LOGGER = logging.getLogger(__name__)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_reference(plan: dict, reference: Path) -> dict:
    """Require the original successful Ridge run and exact shared protocol semantics."""
    frozen = json.loads((reference / "frozen.json").read_text())
    declared = plan["ridge_reference"]
    if sha256(reference / "results.json") != declared["results_sha256"]:
        raise ValueError("Ridge reference result hash mismatch")
    if frozen["protocol_sha256"] != declared["protocol_sha256"]:
        raise ValueError("Ridge reference protocol hash mismatch")
    for key in (
        "expected_train_sha256",
        "expected_training_rows",
        "development_windows",
        "reserved_window",
        "target",
        "ridge",
        "calibration",
        "mapping",
    ):
        if frozen["protocol"][key] != plan[key]:
            raise ValueError(f"comparison changes frozen Ridge semantics: {key}")
    if plan["ridge"] != RIDGE_PARAMETERS:
        raise ValueError("Ridge reference parameters changed")
    for number in (1, 2):
        if (
            sha256(reference / f"development-{number}-predictions.csv")
            != declared["prediction_sha256"][str(number)]
        ):
            raise ValueError("Ridge reference prediction hash mismatch")
    result = json.loads((reference / "results.json").read_text())
    if result["reserved_evaluations"] or result["reserved_predictions"]:
        raise ValueError("reference consumed reserved segment")
    return result


def forecast_report(fitted: ForecastWindow, panel: pd.DataFrame, start: int, end: int) -> dict:
    train, valid = mature_window(panel, start, end)
    y = valid[TARGET].to_numpy()
    prediction = fitted.predictions.forecast.to_numpy()
    baseline = np.full(len(valid), fitted.training_target_mean)
    with threadpool_limits(limits=1):
        train_prediction = fitted.model.predict(train[list(fitted.features)])
    oof_targets = panel.set_index("date_id").loc[fitted.oof.date_id, TARGET].to_numpy()
    mse = float(np.mean((y - prediction) ** 2))
    baseline_mse = float(np.mean((y - baseline) ** 2))
    evaluation_rms = float(np.sqrt(np.mean(prediction**2)))
    return {
        "training_mse": float(np.mean((train[TARGET].to_numpy() - train_prediction) ** 2)),
        "oof_mse": float(np.mean((oof_targets - fitted.oof.forecast.to_numpy()) ** 2)),
        "development_mse": mse,
        "training_mean_reference_mse": baseline_mse,
        "relative_mse_improvement": 1 - mse / baseline_mse if baseline_mse > 0 else None,
        "pearson_correlation": float(np.corrcoef(y, prediction)[0, 1])
        if np.std(y) > 0 and np.std(prediction) > 0
        else None,
        "evaluation_forecast_mean": float(np.mean(prediction)),
        "evaluation_forecast_std": float(np.std(prediction)),
        "evaluation_forecast_rms": evaluation_rms,
        "evaluation_to_oof_rms_ratio": evaluation_rms / fitted.mapping.rms
        if fitted.mapping.rms > 0
        else None,
        "portfolio": portfolio_report(valid, fitted.predictions.allocation.to_numpy()),
        "mapped_training_target_mean": portfolio_report(
            valid, fitted.predictions.mean_allocation.to_numpy()
        ),
        "mapping": asdict(fitted.mapping),
        "oof_rows": len(fitted.oof),
        "training_rows": fitted.training_rows,
        "folds": fitted.folds,
    }


def run_study(
    raw: Path,
    manifest: Path,
    protocol: Path,
    ridge_reference: Path,
    output: Path,
    *,
    check_api: bool = False,
) -> dict:
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    fits = {"ridge_reproduction": 0, "tree": 0}
    try:
        plan = json.loads(protocol.read_text())
        data = load_dataset(raw, manifest)
        train_sha = next(
            item["sha256"]
            for item in json.loads(manifest.read_text())["files"]
            if item["path"] == "train.csv"
        )
        validate_plan(
            plan, data.train, train_sha, estimator_key="tree", expected_parameters=TREE_PARAMETERS
        )
        reference = validate_reference(plan, ridge_reference)
        if plan["max_total_comparison_fits"] != plan["max_model_fits"] * 2:
            raise ValueError("total comparison fit budget mismatch")
        panel, features = prepare_panel(data, plan["reserved_window"][0])
        write_new_json(
            output / "frozen.json",
            {
                "protocol": plan,
                "protocol_sha256": sha256(protocol),
                "manifest_sha256": sha256(manifest),
                "source_sha256": {
                    p.name: sha256(p) for p in sorted(Path(__file__).parent.glob("*.py"))
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
                "api_check_requested": check_api,
            },
        )
        records = []
        last_policies = {}
        for number, (start, end) in enumerate(plan["development_windows"], 1):
            _, valid = mature_window(panel, start, end)
            if valid.date_id.max() >= plan["reserved_window"][0]:
                raise ValueError("reserved evaluation forbidden")
            record = {
                "window": [start, end],
                "evaluation_rows": len(valid),
                "evaluation_id_range": [int(valid.date_id.min()), int(valid.date_id.max())],
                "constants": {
                    str(w): portfolio_report(valid, np.full(len(valid), w)) for w in (0.5, 1.0, 1.5)
                },
            }
            for name, fit_model in (("ridge", fit_ridge), ("tree", fit_tree)):
                fitted = run_window(
                    panel,
                    features,
                    start,
                    end,
                    blocks=plan["calibration"]["blocks"],
                    block_rows=plan["calibration"]["block_rows"],
                    fit_model=fit_model,
                )
                fits["ridge_reproduction" if name == "ridge" else "tree"] += fitted.model_fits
                report = forecast_report(fitted, panel, start, end)
                if name == "ridge":
                    old = pd.read_csv(ridge_reference / f"development-{number}-predictions.csv")
                    if not np.array_equal(old.date_id, fitted.predictions.date_id):
                        raise ValueError("reproduced Ridge row IDs changed")
                    if not np.allclose(
                        old.forecast, fitted.predictions.forecast, rtol=0, atol=1e-12
                    ):
                        raise ValueError("reproduced Ridge forecasts changed")
                    old_weights = old.allocation.to_numpy().astype(np.float32).astype(float)
                    if not np.array_equal(old_weights, fitted.predictions.allocation):
                        raise ValueError("reproduced Ridge allocations changed")
                    if not np.isclose(
                        report["portfolio"]["official_score"],
                        reference["windows"][number - 1]["ridge"]["official_score"],
                        rtol=0,
                        atol=1e-10,
                    ):
                        raise ValueError("reproduced Ridge score changed")
                    report["previous_frozen_result_reproduced"] = True
                else:
                    report["boosting_iterations"] = int(fitted.model.named_steps["tree"].n_iter_)
                record[name] = report
                fitted.predictions.to_csv(
                    output / f"development-{number}-{name}-predictions.csv", index=False, mode="x"
                )
                fitted.oof.to_csv(
                    output / f"development-{number}-{name}-oof.csv", index=False, mode="x"
                )
                last_policies[name] = fitted
            a = last_policies["ridge"].predictions.forecast.to_numpy()
            b = last_policies["tree"].predictions.forecast.to_numpy()
            record["ridge_tree_forecast_correlation"] = (
                float(np.corrcoef(a, b)[0, 1]) if np.std(a) > 0 and np.std(b) > 0 else None
            )
            records.append(record)
        api = {}
        if check_api:
            for name, fitted in last_policies.items():
                rows = panel.loc[panel.date_id.isin(fitted.predictions.date_id)].head(16)
                api[name] = replay_development(
                    raw.resolve(), rows, fitted, output / f"api-{name}-development-replay"
                )
        result = {
            "task": "limited_cpu_tree_versus_frozen_ridge",
            "windows": records,
            "new_candidate_configurations": 1,
            "frozen_reference_configurations": 1,
            "model_fits": fits,
            "total_model_fits": sum(fits.values()),
            "model_input_columns": len(features),
            "transformed_input_columns": len(features) * 2,
            "reserved_predictions": 0,
            "reserved_evaluations": 0,
            "reserved_window": plan["reserved_window"],
            "official_api_development_replay": api,
            "selected_for_promotion": False,
            "submitted": False,
            "elapsed_seconds": time.perf_counter() - started,
        }
        write_new_json(output / "results.json", result)
        return result
    except Exception as exc:
        write_new_json(
            output / "failure.json",
            {"type": type(exc).__name__, "message": str(exc), "completed_fits": fits},
        )
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("raw", "manifest", "protocol", "ridge-reference", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--check-api", action="store_true")
    args = parser.parse_args()
    result = run_study(
        args.raw,
        args.manifest,
        args.protocol,
        args.ridge_reference,
        args.output,
        check_api=args.check_api,
    )
    LOGGER.info("comparison_fits=%d reserved_evaluations=0", result["total_model_fits"])


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
