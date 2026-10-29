"""Bounded context/recency ablation inspired by public methods, development only.

@author cylovelife
"""

import argparse
import hashlib
import importlib.metadata
import json
import logging
import re
import time
from dataclasses import asdict
from pathlib import Path

import numpy as np
import pandas as pd
from threadpoolctl import threadpool_limits

from quant_agent.competitions.hull.context import ContextState, add_context
from quant_agent.competitions.hull.data import TARGETS, HullData, mature_window
from quant_agent.competitions.hull.mapping import AllocationScale, risk_report
from quant_agent.competitions.hull.mapping_study import read_history, sha256
from quant_agent.competitions.hull.ridge import RIDGE_PARAMETERS, TARGET, prepare_panel, run_window
from quant_agent.competitions.hull.study import write_new_json

LOGGER = logging.getLogger(__name__)
CASES = [
    {"name": "base_expanding", "context": False, "max_train_rows": None},
    {"name": "base_recent", "context": False, "max_train_rows": 756},
    {"name": "context_expanding", "context": True, "max_train_rows": None},
    {"name": "context_recent", "context": True, "max_train_rows": 756},
]


def validate_plan(plan: dict) -> None:
    if plan["schema_version"] != 1 or plan["cases"] != CASES:
        raise ValueError("only the registered context/recency cases are allowed")
    if plan["ridge"] != RIDGE_PARAMETERS or plan["target"] != TARGET:
        raise ValueError("fixed Ridge and target required")
    if any(
        plan[k]
        for k in (
            "reserved_evaluation_allowed",
            "selection_allowed",
            "promotion_allowed",
            "platform_submission_allowed",
        )
    ):
        raise ValueError("development diagnosis only; reserved evaluation forbidden")
    windows = plan["development_windows"]
    if (
        len(windows) != 2
        or any(a >= b or b > plan["reserved_window"][0] for a, b in windows)
        or windows[0][1] > windows[1][0]
    ):
        raise ValueError("invalid development/reserved boundaries")
    calibration = plan["calibration"]
    if calibration["blocks"] < 1 or calibration["block_rows"] < 3:
        raise ValueError("invalid calibration blocks")
    if plan["max_model_fits"] != len(windows) * len(CASES) * (calibration["blocks"] + 1):
        raise ValueError("fixed fit budget mismatch")
    if (
        plan["new_candidate_configurations"] != 3
        or plan["reference_reproduction_configurations"] != 1
        or plan["mapping"]["name"] != "market_vol_floor"
        or plan["mapping"]["strength"] != 0.25
        or plan["mapping"]["wire_precision"] != "float32"
        or plan["rowwise_replay_rows"] != 16
    ):
        raise ValueError("fixed comparison/mapping/replay contract changed")
    expected_context = {
        "rank_window": 252,
        "rank_min_periods": 63,
        "return_means": [5, 21],
        "return_compounding": [21, 63],
        "excess_volatility": [21, 63],
        "volatility_ratio": [21, 63],
    }
    if any(plan["context"][k] != v for k, v in expected_context.items()):
        raise ValueError("context feature specification changed")
    expected_files = {"frozen.json", "results.json"}
    expected_files.update(
        f"development-{i}-ridge-{kind}.csv" for i in (1, 2) for kind in ("predictions", "oof")
    )
    if set(plan["reference_files"]) != expected_files:
        raise ValueError("exact frozen Ridge input set required")


def read_development(raw: Path, manifest: Path, plan: dict) -> tuple[pd.DataFrame, tuple[str, ...]]:
    checked = read_history(raw, manifest, plan)
    # Same parser as the original learner; nrows excludes reserved rows before features exist.
    train = pd.read_csv(raw / "train.csv", nrows=plan["development_input_rows"])
    if not np.array_equal(train.date_id, checked.date_id):
        raise ValueError("development input identity changed")
    features = tuple(c for c in train if c not in ("date_id", *TARGETS))
    if (
        not features
        or any(not re.fullmatch(r"(?:MOM|[DEIMPSV])\d+", c) for c in features)
        or list(train) != ["date_id", *features, *TARGETS]
    ):
        raise ValueError("unknown feature schema")
    if not np.isfinite(train[list(TARGETS)].to_numpy(dtype=float)).all():
        raise ValueError("nonfinite current labels")
    if np.isinf(train[list(features)].to_numpy(dtype=float)).any():
        raise ValueError("infinite feature values")
    return prepare_panel(HullData(train, pd.DataFrame(), features, {}), plan["reserved_window"][0])


def correlation(a: np.ndarray, b: np.ndarray) -> float | None:
    return float(np.corrcoef(a, b)[0, 1]) if np.std(a) > 0 and np.std(b) > 0 else None


def replay_rows(
    panel: pd.DataFrame,
    base: tuple[str, ...],
    fitted,
    mapping: AllocationScale,
    start: int,
    count: int,
    use_context: bool,
) -> dict:
    prefix = panel.loc[panel.date_id < start, ["date_id", *base]]
    state = ContextState(prefix, base) if use_context else None
    predictions, allocations = [], []
    wanted = fitted.predictions.head(count)
    indexed = panel.set_index("date_id", drop=False)
    for row_id in wanted.date_id:
        visible = indexed.loc[[row_id], ["date_id", *base]]
        row = state.push(visible) if state else visible
        with threadpool_limits(limits=1):
            p = fitted.model.predict(row[list(fitted.features)])
        predictions.append(float(p[0]))
        allocations.append(float(mapping.apply(p)[0]))
    if not np.allclose(predictions, wanted.forecast, rtol=0, atol=1e-12):
        raise ValueError("rowwise context forecasts differ from batch")
    if not np.array_equal(allocations, mapping.apply(wanted.forecast.to_numpy())):
        raise ValueError("rowwise float32 allocations differ from batch")
    return {
        "rows": len(wanted),
        "first_id": int(wanted.date_id.min()),
        "last_id": int(wanted.date_id.max()),
        "forecast_atol": 1e-12,
        "float32_allocations_exact": True,
        "official_api_replay": False,
    }


def run_study(
    raw: Path,
    manifest: Path,
    protocol: Path,
    reference: Path,
    mapping_reference: Path,
    research_manifest: Path,
    output: Path,
) -> dict:
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    completed = 0
    started = time.perf_counter()
    try:
        plan = json.loads(protocol.read_text())
        validate_plan(plan)
        inputs = output / "inputs"
        inputs.mkdir()
        for name, expected in plan["reference_files"].items():
            contents = (reference / name).read_bytes()
            if hashlib.sha256(contents).hexdigest() != expected:
                raise ValueError(f"frozen reference hash mismatch: {name}")
            with (inputs / name).open("xb") as handle:
                handle.write(contents)
        for path, expected, name in (
            (mapping_reference, plan["mapping_reference_results_sha256"], "mapping-results.json"),
            (research_manifest, plan["research_github_manifest_sha256"], "research-github.json"),
        ):
            contents = path.read_bytes()
            if hashlib.sha256(contents).hexdigest() != expected:
                raise ValueError(f"reference hash mismatch: {name}")
            with (inputs / name).open("xb") as handle:
                handle.write(contents)
        parent = json.loads((inputs / "frozen.json").read_text())["protocol"]
        for key in (
            "expected_train_sha256",
            "development_windows",
            "reserved_window",
            "target",
            "ridge",
        ):
            if parent[key] != plan[key]:
                raise ValueError(f"parent comparison changed: {key}")
        for key in ("blocks", "block_rows"):
            if parent["calibration"][key] != plan["calibration"][key]:
                raise ValueError("parent calibration changed")
        old_mapping = json.loads((inputs / "mapping-results.json").read_text())
        if old_mapping["reserved_predictions"] or old_mapping["reserved_evaluations"]:
            raise ValueError("mapping reference consumed reserved data")
        panel, base_features = read_development(raw, manifest, plan)
        contextual, context_features = add_context(panel, base_features)
        write_new_json(
            output / "frozen.json",
            {
                "protocol": plan,
                "protocol_sha256": sha256(protocol),
                "source_sha256": {
                    p.name: sha256(p) for p in sorted(Path(__file__).parent.glob("*.py"))
                },
                "packages": {
                    p: importlib.metadata.version(p) for p in ("numpy", "pandas", "scikit-learn")
                },
                "base_features": list(base_features),
                "context_features": list(context_features),
                "parsed_raw_rows": len(panel),
                "parsed_raw_max_id": int(panel.date_id.max()),
            },
        )
        records = []
        for number, (start, end) in enumerate(plan["development_windows"], 1):
            _, valid = mature_window(panel, start, end)
            old = pd.read_csv(
                inputs / f"development-{number}-ridge-predictions.csv", float_precision="round_trip"
            )
            old_oof = pd.read_csv(
                inputs / f"development-{number}-ridge-oof.csv", float_precision="round_trip"
            )
            if not np.array_equal(old.date_id, valid.date_id):
                raise ValueError("development evaluation cohort changed")
            y = valid[TARGET].to_numpy()
            common_mse = float(np.mean((y - old.mean_forecast.to_numpy()) ** 2))
            record = {
                "window": [start, end],
                "evaluation_rows": len(valid),
                "evaluation_id_range": [int(valid.date_id.min()), int(valid.date_id.max())],
                "common_expanding_training_mean_mse": common_mse,
                "market": risk_report(valid, np.ones(len(valid))),
                "cases": {},
            }
            for case in plan["cases"]:
                data, features = (
                    (contextual, context_features) if case["context"] else (panel, base_features)
                )
                fitted = run_window(
                    data,
                    features,
                    start,
                    end,
                    blocks=plan["calibration"]["blocks"],
                    block_rows=plan["calibration"]["block_rows"],
                    max_train_rows=case["max_train_rows"],
                )
                completed += fitted.model_fits
                if completed > plan["max_model_fits"]:
                    raise ValueError("fit budget exceeded")
                oof_return = panel.set_index("date_id").loc[fitted.oof.date_id]
                sigma = float(
                    np.std(oof_return.forward_returns - oof_return.risk_free_rate, ddof=1)
                )
                mapping = AllocationScale.calibrate(fitted.mapping.rms, sigma, 0.25, True)
                p = fitted.predictions.forecast.to_numpy()
                weights = mapping.apply(p)
                portfolio = risk_report(valid, weights)
                if case["name"] == "base_expanding":
                    if not np.allclose(p, old.forecast, rtol=0, atol=1e-12):
                        raise ValueError("base Ridge prediction reproduction failed")
                    if not np.array_equal(fitted.oof.date_id, old_oof.date_id) or not np.allclose(
                        fitted.oof.forecast, old_oof.forecast, rtol=0, atol=1e-12
                    ):
                        raise ValueError("base Ridge OOF reproduction failed")
                    if not np.array_equal(
                        fitted.predictions.allocation,
                        old.allocation.to_numpy().astype(np.float32).astype(float),
                    ):
                        raise ValueError("base original allocation reproduction failed")
                    previous_score = old_mapping["windows"][number - 1]["ridge"]["profiles"][
                        "market_vol_floor"
                    ]["official_score"]
                    if not np.isclose(
                        portfolio["official_score"], previous_score, rtol=0, atol=1e-10
                    ):
                        raise ValueError("fixed floor score reproduction failed")
                mse = float(np.mean((y - p) ** 2))
                fit, _ = mature_window(data, start, end)
                if case["max_train_rows"] is not None:
                    fit = fit.tail(case["max_train_rows"])
                report = {
                    "feature_columns": len(features),
                    "transformed_columns": 2 * len(features),
                    "training_rows": fitted.training_rows,
                    "training_id_min": int(fit.date_id.min()),
                    "training_id_max": int(fit.date_id.max()),
                    "training_target_mean": fitted.training_target_mean,
                    "development_mse": mse,
                    "relative_mse_improvement_vs_common_mean": 1 - mse / common_mse,
                    "target_correlation": correlation(p, y),
                    "market_excess_correlation": correlation(
                        p, (valid.forward_returns - valid.risk_free_rate).to_numpy()
                    ),
                    "forecast_mean": float(np.mean(p)),
                    "forecast_std": float(np.std(p)),
                    "mapping": asdict(mapping),
                    "folds": fitted.folds,
                    "portfolio": portfolio,
                    "rowwise_replay": replay_rows(
                        panel,
                        base_features,
                        fitted,
                        mapping,
                        start,
                        plan["rowwise_replay_rows"],
                        case["context"],
                    ),
                }
                record["cases"][case["name"]] = report
                fitted.predictions.assign(floor_allocation=weights).to_csv(
                    output / f"development-{number}-{case['name']}-predictions.csv",
                    index=False,
                    mode="x",
                )
                fitted.oof.to_csv(
                    output / f"development-{number}-{case['name']}-oof.csv", index=False, mode="x"
                )
            records.append(record)
        if completed != plan["max_model_fits"]:
            raise ValueError("incomplete fit budget")
        if any(sha256(reference / n) != h for n, h in plan["reference_files"].items()):
            raise ValueError("frozen source inputs changed during ablation")
        result = {
            "task": "causal_context_recency_factorial",
            "windows": records,
            "total_model_fits": completed,
            "new_candidate_configurations": 3,
            "reference_reproduction_configurations": 1,
            "reserved_predictions": 0,
            "reserved_evaluations": 0,
            "reserved_window": plan["reserved_window"],
            "base_expanding_reproduced": True,
            "source_reference_unchanged": True,
            "selected_for_promotion": False,
            "submitted": False,
            "elapsed_seconds": time.perf_counter() - started,
        }
        write_new_json(output / "results.json", result)
        return result
    except Exception as exc:
        write_new_json(
            output / "failure.json",
            {"type": type(exc).__name__, "message": str(exc), "completed_model_fits": completed},
        )
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in (
        "raw",
        "manifest",
        "protocol",
        "reference",
        "mapping-reference",
        "research-manifest",
        "output",
    ):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    result = run_study(
        args.raw,
        args.manifest,
        args.protocol,
        args.reference,
        args.mapping_reference,
        args.research_manifest,
        args.output,
    )
    LOGGER.info("fits=%d reserved_predictions=0 reserved_scores=0", result["total_model_fits"])


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
