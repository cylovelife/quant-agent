"""Frozen prediction replay; this entry point never fits or calls a predictor.

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

from quant_agent.competitions.hull.mapping import AllocationScale, risk_report
from quant_agent.competitions.hull.metrics import validate_ids
from quant_agent.competitions.hull.study import write_new_json

LOGGER = logging.getLogger(__name__)
PROFILES = [
    {"name": "original", "strength": 0.25, "market_vol_floor": False},
    {"name": "half_strength", "strength": 0.125, "market_vol_floor": False},
    {"name": "market_vol_floor", "strength": 0.25, "market_vol_floor": True},
    {"name": "floor_half_strength", "strength": 0.125, "market_vol_floor": True},
]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_plan(plan: dict) -> None:
    if plan["schema_version"] != 1 or plan["profiles"] != PROFILES:
        raise ValueError("only the registered four mapping profiles are allowed")
    if any(
        plan[key]
        for key in (
            "reserved_evaluation_allowed",
            "model_fits_allowed",
            "model_predictions_allowed",
            "selection_allowed",
            "promotion_allowed",
            "platform_submission_allowed",
        )
    ):
        raise ValueError("mapping-only development diagnosis required")
    windows = plan["development_windows"]
    reserved_start, reserved_end = plan["reserved_window"]
    if (
        len(windows) != 2
        or reserved_start >= reserved_end
        or any(a >= b or b > reserved_start for a, b in windows)
        or windows[0][1] > windows[1][0]
    ):
        raise ValueError("invalid development/reserved boundaries")
    if (
        plan["wire_precision"] != "float32"
        or plan["benchmark_allocation"] != 1
        or plan["expected_model_window_cases"] != 16
        or plan["benchmark_window_cases"] != 2
    ):
        raise ValueError("fixed execution budget or wire contract changed")
    expected = {"frozen.json", "results.json"}
    expected.update(
        f"development-{i}-{m}-{kind}.csv"
        for i in (1, 2)
        for m in ("ridge", "tree")
        for kind in ("predictions", "oof")
    )
    if set(plan["reference_files"]) != expected:
        raise ValueError("exact frozen reference file set required")


def read_history(raw: Path, manifest: Path, plan: dict) -> pd.DataFrame:
    """Hash the original file, but parse only registered development rows and labels."""
    if sha256(manifest) != plan["expected_manifest_sha256"]:
        raise ValueError("manifest hash mismatch")
    declared = next(
        x["sha256"] for x in json.loads(manifest.read_text())["files"] if x["path"] == "train.csv"
    )
    if declared != plan["expected_train_sha256"] or sha256(raw / "train.csv") != declared:
        raise ValueError("training file hash mismatch")
    history = pd.read_csv(
        raw / "train.csv",
        usecols=["date_id", "forward_returns", "risk_free_rate"],
        nrows=plan["development_input_rows"],
        float_precision="round_trip",
    )
    validate_ids(history)
    if (
        len(history) != plan["development_input_rows"]
        or history.date_id.max() != plan["development_last_id"]
        or history.date_id.max() >= plan["reserved_window"][0]
    ):
        raise ValueError("development-only input row boundary mismatch")
    if not np.isfinite(history[["forward_returns", "risk_free_rate"]].to_numpy()).all():
        raise ValueError("nonfinite development returns")
    history["label_end_id"] = history.date_id.shift(-1)
    return history


def mature_rows(history: pd.DataFrame, start: int, end: int) -> pd.DataFrame:
    return history.loc[
        (history.date_id >= start) & (history.date_id < end) & (history.label_end_id < end)
    ].copy()


def validate_forecasts(
    history: pd.DataFrame,
    predictions: pd.DataFrame,
    oof: pd.DataFrame,
    start: int,
    end: int,
    calibration: dict,
    record: dict,
) -> pd.DataFrame:
    validate_ids(predictions)
    validate_ids(oof)
    valid = mature_rows(history, start, end)
    if not np.array_equal(predictions.date_id, valid.date_id):
        raise ValueError("frozen development forecast IDs differ from mature cohort")
    blocks, rows = calibration["blocks"], calibration["block_rows"]
    expected_oof = pd.concat(
        [mature_rows(history, a, a + rows) for a in range(start - blocks * rows, start, rows)]
    )
    if (
        not np.array_equal(oof.date_id, expected_oof.date_id)
        or not np.array_equal(oof.label_end_id, expected_oof.label_end_id)
        or len(oof) != record["oof_rows"]
        or not (oof.label_end_id < start).all()
    ):
        raise ValueError("OOF cohort or label maturity mismatch")
    for array in (predictions.forecast, predictions.allocation, oof.forecast):
        if not np.isfinite(array.to_numpy(dtype=float)).all():
            raise ValueError("frozen forecast/weight must be finite")
    rms = float(np.sqrt(np.mean(oof.forecast.to_numpy() ** 2)))
    if not np.isclose(rms, record["mapping"]["rms"], rtol=1e-12, atol=1e-15):
        raise ValueError("OOF scale differs from frozen calibration")
    return valid


def run_study(raw: Path, manifest: Path, protocol: Path, reference: Path, output: Path) -> dict:
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    started = time.perf_counter()
    try:
        protocol_bytes = protocol.read_bytes()
        plan = json.loads(protocol_bytes)
        validate_plan(plan)
        inputs = output / "inputs"
        inputs.mkdir()
        for name, expected_hash in plan["reference_files"].items():
            contents = (reference / name).read_bytes()
            if hashlib.sha256(contents).hexdigest() != expected_hash:
                raise ValueError(f"frozen reference hash mismatch: {name}")
            with (inputs / name).open("xb") as handle:
                handle.write(contents)
        parent = json.loads((inputs / "frozen.json").read_text())["protocol"]
        for key in ("expected_train_sha256", "development_windows", "reserved_window"):
            if parent[key] != plan[key]:
                raise ValueError(f"parent protocol changed: {key}")
        previous = json.loads((inputs / "results.json").read_text())
        if previous["reserved_predictions"] or previous["reserved_evaluations"]:
            raise ValueError("reference consumed reserved rows")
        history = read_history(raw, manifest, plan)
        write_new_json(
            output / "frozen.json",
            {
                "protocol": plan,
                "protocol_sha256": hashlib.sha256(protocol_bytes).hexdigest(),
                "source_sha256": {
                    p.name: sha256(p) for p in sorted(Path(__file__).parent.glob("*.py"))
                },
                "packages": {
                    p: importlib.metadata.version(p) for p in ("numpy", "pandas", "scikit-learn")
                },
                "reference_inputs_copied_and_verified": True,
                "parsed_raw_columns": ["date_id", "forward_returns", "risk_free_rate"],
                "parsed_raw_rows": len(history),
                "parsed_max_id": int(history.date_id.max()),
            },
        )
        records = []
        cases = 0
        for number, (start, end) in enumerate(plan["development_windows"], 1):
            valid = mature_rows(history, start, end)
            record = {
                "window": [start, end],
                "evaluation_rows": len(valid),
                "evaluation_id_range": [int(valid.date_id.min()), int(valid.date_id.max())],
                "market": risk_report(valid, np.ones(len(valid))),
            }
            if previous["windows"][number - 1]["window"] != [start, end]:
                raise ValueError("parent evaluation window changed")
            for model in ("ridge", "tree"):
                stem = f"development-{number}-{model}"
                predictions = pd.read_csv(
                    inputs / f"{stem}-predictions.csv",
                    float_precision="round_trip",
                    usecols=["date_id", "forecast", "allocation"],
                )
                oof = pd.read_csv(inputs / f"{stem}-oof.csv", float_precision="round_trip")
                old = previous["windows"][number - 1][model]
                valid = validate_forecasts(
                    history, predictions, oof, start, end, parent["calibration"], old
                )
                earlier = history.set_index("date_id").loc[oof.date_id]
                excess = (earlier.forward_returns - earlier.risk_free_rate).to_numpy()
                market_std = float(np.std(excess, ddof=1))
                corr = (
                    float(np.corrcoef(oof.forecast, excess)[0, 1])
                    if np.std(oof.forecast) > 0
                    else None
                )
                reports = {}
                allocations = pd.DataFrame({"date_id": valid.date_id.to_numpy()})
                for profile in plan["profiles"]:
                    name = profile["name"]
                    scale = AllocationScale.calibrate(
                        old["mapping"]["rms"],
                        market_std,
                        profile["strength"],
                        profile["market_vol_floor"],
                    )
                    weights = scale.apply(predictions.forecast.to_numpy())
                    report = risk_report(valid, weights)
                    report["scale"] = asdict(scale)
                    if name == "original":
                        if not np.array_equal(
                            weights,
                            predictions.allocation.to_numpy().astype(np.float32).astype(float),
                        ):
                            raise ValueError("original allocation reproduction failed")
                        if not np.isclose(
                            report["official_score"],
                            old["portfolio"]["official_score"],
                            rtol=0,
                            atol=1e-10,
                        ):
                            raise ValueError("original score reproduction failed")
                    reports[name] = report
                    allocations[name] = weights
                    cases += 1
                record[model] = {
                    "oof_rows": len(oof),
                    "oof_market_excess_std": market_std,
                    "oof_forecast_excess_correlation": corr,
                    "correlation_used_for_mapping": False,
                    "original_reproduced": True,
                    "profiles": reports,
                }
                allocations.to_csv(output / f"{stem}-allocations.csv", index=False, mode="x")
            records.append(record)
        if cases != plan["expected_model_window_cases"]:
            raise ValueError("mapping evaluation budget exceeded")
        if any(sha256(reference / n) != h for n, h in plan["reference_files"].items()):
            raise ValueError("source reference changed during replay")
        result = {
            "task": "frozen_forecast_mapping_risk",
            "windows": records,
            "total_model_fits": 0,
            "new_model_predictions": 0,
            "model_window_profile_cases": cases,
            "benchmark_window_cases": len(records),
            "reserved_predictions": 0,
            "reserved_evaluations": 0,
            "reserved_window": plan["reserved_window"],
            "reference_source_unchanged": True,
            "selected_for_promotion": False,
            "submitted": False,
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
                "completed_model_fits": 0,
                "new_model_predictions": 0,
            },
        )
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("raw", "manifest", "protocol", "reference", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    result = run_study(args.raw, args.manifest, args.protocol, args.reference, args.output)
    LOGGER.info(
        "mapping_cases=%d fits=0 new_predictions=0 reserved_scores=0",
        result["model_window_profile_cases"],
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
