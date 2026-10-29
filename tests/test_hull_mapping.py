"""Frozen forecast allocation risk contracts, using only temporary market histories.

@author cylovelife
"""

import hashlib
import json

import numpy as np
import pandas as pd
import pytest

from quant_agent.competitions.hull.mapping import AllocationScale, risk_report
from quant_agent.competitions.hull.mapping_study import (
    PROFILES,
    read_history,
    run_study,
    validate_forecasts,
    validate_plan,
)
from quant_agent.competitions.hull.ridge_study import portfolio_report


def test_normalization_cancels_forecast_shrinkage_but_vol_floor_does_not():
    p = np.array([-0.002, -0.001, 0.001, 0.002])
    rms = float(np.sqrt(np.mean(p**2)))
    a = AllocationScale.calibrate(rms, 0.01, 0.25, False)
    b = AllocationScale.calibrate(rms / 10, 0.01, 0.25, False)
    np.testing.assert_array_equal(a.apply(p), b.apply(p / 10))
    safe = AllocationScale.calibrate(rms, 0.01, 0.25, True)
    shrunk = AllocationScale.calibrate(rms / 10, 0.01, 0.25, True)
    assert np.max(abs(shrunk.apply(p / 10) - 1)) < np.max(abs(safe.apply(p) - 1))
    np.testing.assert_array_equal(a.apply(p), [a.apply(np.array([x]))[0] for x in p])
    assert np.array_equal(a.apply(p), a.apply(p).astype(np.float32).astype(float))


def test_zero_scale_neutral_and_invalid_values_rejected():
    scale = AllocationScale.calibrate(0, 0.01, 0.25, False)
    np.testing.assert_array_equal(scale.apply(np.array([-10.0, 10.0])), [1, 1])
    scale = AllocationScale.calibrate(0.001, 0.01, 0.25, False)
    np.testing.assert_array_equal(scale.apply(np.array([-10.0, 10.0])), [0, 2])
    with pytest.raises(ValueError, match="finite"):
        scale.apply(np.array([np.nan]))
    with pytest.raises(ValueError):
        AllocationScale.calibrate(-1, 0.01, 0.25, False)


def test_risk_accounting_and_negative_score_penalty():
    solution = pd.DataFrame(
        {
            "date_id": range(6),
            "forward_returns": [-0.04, 0.01, -0.02, 0.01, -0.03, 0.01],
            "risk_free_rate": 0.0001,
        }
    )
    w = np.array([1.7, 1.3, 1.6, 1.4, 1.7, 1.2])
    report = risk_report(solution, w)
    assert report["variance_identity_error"] < 1e-15
    assert report["mean_active_identity_error"] < 1e-15
    parts = report["score_components"]
    assert parts["raw_sharpe"] < 0
    assert parts["volatility_penalty"] > 1
    assert parts["reconstructed_score"] > parts["raw_sharpe"]
    assert report["official_score"] == pytest.approx(parts["reconstructed_score"], abs=1e-12)
    market = risk_report(solution, np.ones(6))
    assert market["active_return_rms_annualized"] == 0
    assert market["tracking_error_annualized"] == 0


@pytest.fixture
def frozen_inputs(tmp_path):
    raw = tmp_path / "raw"
    raw.mkdir()
    reference = tmp_path / "reference"
    reference.mkdir()
    ids = np.arange(55)
    history = pd.DataFrame(
        {
            "date_id": ids,
            "forward_returns": 0.003 + 0.01 * np.sin(ids),
            "risk_free_rate": 0.0001,
            "unused_feature": 17.0,
        }
    )
    history.to_csv(raw / "train.csv", index=False)

    def digest(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    train_sha = digest(raw / "train.csv")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"files": [{"path": "train.csv", "sha256": train_sha}]}))
    parent = {
        "development_windows": [[30, 40], [40, 50]],
        "reserved_window": [50, 55],
        "expected_train_sha256": train_sha,
        "calibration": {"blocks": 2, "block_rows": 5},
    }
    result = {"windows": [], "reserved_predictions": 0, "reserved_evaluations": 0}
    for number, (start, end) in enumerate(parent["development_windows"], 1):
        record = {"window": [start, end]}
        valid = history.loc[history.date_id.between(start, end - 2)]
        for model in ("ridge", "tree"):
            oof_ids = np.r_[np.arange(start - 10, start - 6), np.arange(start - 5, start - 1)]
            p = np.sin(oof_ids) * (0.002 if model == "ridge" else 0.001)
            rms = float(np.sqrt(np.mean(p**2)))
            forecasts = np.cos(valid.date_id.to_numpy()) * 0.002
            weights = AllocationScale.calibrate(rms, 0.01, 0.25, False).apply(forecasts)
            pd.DataFrame({"date_id": oof_ids, "label_end_id": oof_ids + 1, "forecast": p}).to_csv(
                reference / f"development-{number}-{model}-oof.csv", index=False
            )
            pd.DataFrame(
                {"date_id": valid.date_id, "forecast": forecasts, "allocation": weights}
            ).to_csv(reference / f"development-{number}-{model}-predictions.csv", index=False)
            record[model] = {
                "mapping": {"rms": rms},
                "oof_rows": len(p),
                "portfolio": portfolio_report(valid, weights),
            }
        result["windows"].append(record)
    (reference / "frozen.json").write_text(json.dumps({"protocol": parent}))
    (reference / "results.json").write_text(json.dumps(result))
    plan = {
        "schema_version": 1,
        "expected_train_sha256": train_sha,
        "expected_manifest_sha256": digest(manifest),
        "development_input_rows": 50,
        "development_last_id": 49,
        "development_windows": parent["development_windows"],
        "reserved_window": parent["reserved_window"],
        "reserved_evaluation_allowed": False,
        "model_fits_allowed": 0,
        "model_predictions_allowed": 0,
        "profiles": PROFILES,
        "wire_precision": "float32",
        "benchmark_allocation": 1.0,
        "reference_files": {p.name: digest(p) for p in reference.iterdir()},
        "expected_model_window_cases": 16,
        "benchmark_window_cases": 2,
        "selection_allowed": False,
        "promotion_allowed": False,
        "platform_submission_allowed": False,
    }
    protocol = tmp_path / "protocol.json"
    protocol.write_text(json.dumps(plan))
    return raw, manifest, protocol, reference


def test_full_replay_uses_no_learners_or_reserved_rows(frozen_inputs, tmp_path, monkeypatch):
    from sklearn.pipeline import Pipeline

    def forbidden(*args, **kwargs):
        raise AssertionError("learner called during mapping-only replay")

    monkeypatch.setattr(Pipeline, "fit", forbidden)
    monkeypatch.setattr(Pipeline, "predict", forbidden)
    raw, manifest, protocol, reference = frozen_inputs
    result = run_study(raw, manifest, protocol, reference, tmp_path / "run")
    assert result["total_model_fits"] == result["new_model_predictions"] == 0
    assert result["reserved_predictions"] == result["reserved_evaluations"] == 0
    assert len(result["windows"]) == 2
    assert result["model_window_profile_cases"] == 16
    for p in (tmp_path / "run").glob("*-allocations.csv"):
        assert pd.read_csv(p).date_id.max() < 50
    # Modify all reserved values, re-register file identity, and require identical reports.
    frame = pd.read_csv(raw / "train.csv", float_precision="round_trip")
    frame.loc[frame.date_id >= 50, ["forward_returns", "risk_free_rate", "unused_feature"]] = 99
    frame.to_csv(raw / "train.csv", index=False)
    new_sha = hashlib.sha256((raw / "train.csv").read_bytes()).hexdigest()
    parent_path = reference / "frozen.json"
    parent = json.loads(parent_path.read_text())
    parent["protocol"]["expected_train_sha256"] = new_sha
    parent_path.write_text(json.dumps(parent))
    data = json.loads(manifest.read_text())
    data["files"][0]["sha256"] = new_sha
    manifest.write_text(json.dumps(data))
    plan = json.loads(protocol.read_text())
    plan["expected_train_sha256"] = new_sha
    plan["expected_manifest_sha256"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
    plan["reference_files"]["frozen.json"] = hashlib.sha256(parent_path.read_bytes()).hexdigest()
    protocol.write_text(json.dumps(plan))
    again = run_study(raw, manifest, protocol, reference, tmp_path / "again")
    assert result["windows"] == again["windows"]
    with pytest.raises(FileExistsError):
        run_study(raw, manifest, protocol, reference, tmp_path / "run")


def test_corrupt_frozen_inputs_fail_closed(frozen_inputs, tmp_path):
    raw, manifest, protocol, reference = frozen_inputs
    with (reference / "development-1-tree-predictions.csv").open("a") as handle:
        handle.write("50,0,1\n")
    with pytest.raises(ValueError, match="hash"):
        run_study(raw, manifest, protocol, reference, tmp_path / "failed")
    assert not (tmp_path / "failed" / "results.json").exists()


def test_maturity_and_raw_identity_fail_closed(frozen_inputs):
    raw, manifest, protocol, reference = frozen_inputs
    plan = json.loads(protocol.read_text())
    history = read_history(raw, manifest, plan)
    pred = pd.read_csv(reference / "development-1-ridge-predictions.csv")
    oof = pd.read_csv(reference / "development-1-ridge-oof.csv")
    old = json.loads((reference / "results.json").read_text())["windows"][0]["ridge"]
    calibration = {"blocks": 2, "block_rows": 5}
    oof.loc[oof.index[-1], "label_end_id"] = 30
    with pytest.raises(ValueError, match="maturity"):
        validate_forecasts(history, pred, oof, 30, 40, calibration, old)
    with (raw / "train.csv").open("a") as handle:
        handle.write("56,0,0,1\n")
    with pytest.raises(ValueError, match="hash"):
        read_history(raw, manifest, plan)
    plan["development_windows"][1][1] = 51
    with pytest.raises(ValueError, match="boundaries"):
        validate_plan(plan)


def test_evaluation_labels_cannot_change_allocations(frozen_inputs):
    raw, manifest, protocol, reference = frozen_inputs
    plan = json.loads(protocol.read_text())
    history = read_history(raw, manifest, plan)
    ids = pd.read_csv(reference / "development-1-ridge-oof.csv").date_id
    p = pd.read_csv(reference / "development-1-ridge-predictions.csv").forecast.to_numpy()

    def replay(frame):
        earlier = frame.set_index("date_id").loc[ids]
        sigma = float(np.std(earlier.forward_returns - earlier.risk_free_rate, ddof=1))
        return AllocationScale.calibrate(0.002, sigma, 0.25, True).apply(p)

    weights = replay(history)
    history.loc[history.date_id >= 30, ["forward_returns", "risk_free_rate"]] = 99
    np.testing.assert_array_equal(weights, replay(history))
