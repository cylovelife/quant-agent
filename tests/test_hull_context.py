"""Causal history features, finite training memory and controlled ablation contracts.

@author cylovelife
"""

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from quant_agent.competitions.hull.context import ContextState, add_context
from quant_agent.competitions.hull.ridge import TARGET, fit_ridge, run_window


def history(n=250):
    ids = np.arange(n)
    rng = np.random.default_rng(8)
    return pd.DataFrame(
        {
            "date_id": ids,
            "M1": np.sin(ids / 10),
            "D1": ids % 2,
            "lagged_forward_returns": rng.normal(0.001, 0.01, n),
            "lagged_risk_free_rate": 0.0001,
            "lagged_market_forward_excess_returns": rng.normal(0, 0.01, n),
            TARGET: rng.normal(0, 0.01, n),
            "forward_returns": rng.normal(0, 0.01, n),
            "risk_free_rate": 0.0001,
        }
    )


FEATURES = (
    "M1",
    "D1",
    "lagged_forward_returns",
    "lagged_risk_free_rate",
    "lagged_market_forward_excess_returns",
)


def test_rolling_context_is_prefix_invariant_and_ignores_current_targets():
    panel = history()
    a, names = add_context(panel, FEATURES)
    other = panel.copy()
    other.loc[other.date_id >= 180, [TARGET, "forward_returns", "risk_free_rate"]] = 99
    other.loc[other.date_id > 180, list(FEATURES)] = 100
    b, _ = add_context(other, FEATURES)
    pd.testing.assert_frame_equal(a.loc[:180, list(names)], b.loc[:180, list(names)])
    prefix, _ = add_context(panel.iloc[:181], FEATURES)
    pd.testing.assert_frame_equal(a.loc[:180], prefix)
    assert "ctx_rank_D1" not in names
    assert a.loc[61, "ctx_rank_M1"] != a.loc[61, "ctx_rank_M1"]
    expected = panel.loc[:62, "M1"].rank(pct=True).iloc[-1]
    assert a.loc[62, "ctx_rank_M1"] == pytest.approx(expected)
    assert a.loc[100, "ctx_return_mean_5"] == pytest.approx(
        panel.loc[96:100, "lagged_forward_returns"].mean()
    )


def test_context_stream_matches_batch_and_rejects_order_or_schema():
    panel = history(400)
    batch, names = add_context(panel, FEATURES)
    stream = ContextState(panel.loc[:299, ["date_id", *FEATURES]], FEATURES)
    for i in range(300, 320):
        row = stream.push(panel.loc[[i], ["date_id", *FEATURES]])
        np.testing.assert_allclose(
            row[list(names)], batch.loc[[i], list(names)], rtol=1e-12, atol=1e-14, equal_nan=True
        )
    assert len(stream.history) <= 252
    with pytest.raises(ValueError, match="ordered"):
        stream.push(panel.loc[[319], ["date_id", *FEATURES]])
    with pytest.raises(ValueError, match="visible"):
        stream.push(panel.loc[[320]])
    before = stream.history.copy()
    broken = panel.loc[[320], ["date_id", *FEATURES]].copy()
    broken["M1"] = np.inf
    with pytest.raises(ValueError, match="infinite"):
        stream.push(broken)
    pd.testing.assert_frame_equal(stream.history, before)


def test_context_has_no_infinite_values_in_constant_or_missing_history():
    panel = history(300)
    panel["M1"] = np.nan
    panel["lagged_forward_returns"] = 0.0001
    augmented, names = add_context(panel, FEATURES)
    assert not np.isinf(augmented[list(names)].to_numpy()).any()
    assert augmented.ctx_vol_ratio_21_63.isna().all()
    with pytest.raises(ValueError, match="labels"):
        add_context(panel, (*FEATURES, TARGET))


def test_recent_training_cap_applies_to_all_folds_preprocessing_and_reference_mean():
    panel = history()
    seen = []

    def record_fit(train, names):
        seen.append(train.copy())
        return fit_ridge(train, names)

    fitted = run_window(
        panel, FEATURES, 180, 210, blocks=2, block_rows=20, max_train_rows=60, fit_model=record_fit
    )
    assert [len(p) for p in seen] == [60, 60, 60]
    assert [int(p.date_id.min()) for p in seen] == [79, 99, 119]
    assert all(f["training_rows"] == 60 for f in fitted.folds)
    assert fitted.training_rows == 60
    assert fitted.training_target_mean == pytest.approx(panel.loc[119:178, TARGET].mean())
    # Mature row179 ends at180 and must not enter the last fit.
    assert seen[-1].date_id.max() == 178
    changed = panel.copy()
    changed.loc[changed.date_id < 79, [*FEATURES, TARGET]] = 999
    changed.loc[changed.date_id >= 180, TARGET] = 99
    other = run_window(changed, FEATURES, 180, 210, blocks=2, block_rows=20, max_train_rows=60)
    np.testing.assert_array_equal(fitted.predictions.forecast, other.predictions.forecast)
    np.testing.assert_array_equal(fitted.oof.forecast, other.oof.forecast)
    for bad in (1, 0, -1, 2.5, True):
        with pytest.raises(ValueError, match="training"):
            run_window(panel, FEATURES, 180, 210, max_train_rows=bad)


def test_context_full_study_reproduces_parent_and_is_reserved_invariant(tmp_path):
    from test_hull_ridge import write_source

    from quant_agent.competitions.hull.context_study import run_study as context_study
    from quant_agent.competitions.hull.mapping import AllocationScale, risk_report
    from quant_agent.competitions.hull.ridge_study import run_study as ridge_study

    def execute(root, changed_reserved):
        frame = history(240)
        frame = frame.drop(columns=list(FEATURES[2:]))
        frame["E1"] = np.cos(frame.date_id / 10)
        frame = frame[["date_id", "M1", "E1", "D1", "forward_returns", "risk_free_rate", TARGET]]
        if changed_reserved:
            frame.loc[
                frame.date_id >= 200, ["M1", "E1", "forward_returns", "risk_free_rate", TARGET]
            ] = 999
        write_source(root, frame)
        original = ridge_study(root, root / "manifest.json", root / "protocol.json", root / "ridge")
        parent = root / "parent"
        parent.mkdir()
        (parent / "frozen.json").write_bytes((root / "ridge/frozen.json").read_bytes())
        (parent / "results.json").write_text(json.dumps(original))
        mapping_windows = []
        for i, (start, end) in enumerate([[130, 160], [160, 190]], 1):
            for old_name, kind in [
                (f"development-{i}-predictions.csv", "predictions"),
                (f"development-{i}-calibration-oof.csv", "oof"),
            ]:
                (parent / f"development-{i}-ridge-{kind}.csv").write_bytes(
                    (root / "ridge" / old_name).read_bytes()
                )
            oof = pd.read_csv(parent / f"development-{i}-ridge-oof.csv")
            pred = pd.read_csv(parent / f"development-{i}-ridge-predictions.csv")
            early = frame.set_index("date_id").loc[oof.date_id]
            sigma = float(np.std(early.forward_returns - early.risk_free_rate, ddof=1))
            scale = AllocationScale.calibrate(
                original["windows"][i - 1]["mapping"]["rms"], sigma, 0.25, True
            )
            valid = frame.loc[frame.date_id.isin(pred.date_id)]
            mapping_windows.append(
                {
                    "ridge": {
                        "profiles": {
                            "market_vol_floor": risk_report(
                                valid, scale.apply(pred.forecast.to_numpy())
                            )
                        }
                    }
                }
            )
        mapping_ref = root / "mapping.json"
        mapping_ref.write_text(
            json.dumps(
                {"windows": mapping_windows, "reserved_predictions": 0, "reserved_evaluations": 0}
            )
        )
        research = root / "research.json"
        research.write_text("{}")
        protocol_path = (
            Path(__file__).resolve().parents[1] / "docs/competitions/hull_context_protocol.json"
        )
        plan = json.loads(protocol_path.read_text())
        old_plan = json.loads((root / "protocol.json").read_text())
        for key in [
            "expected_train_sha256",
            "development_windows",
            "reserved_window",
            "calibration",
        ]:
            plan[key] = old_plan[key]
        plan.update(
            {
                "development_input_rows": 200,
                "development_last_id": 199,
                "max_model_fits": 24,
                "expected_manifest_sha256": hashlib.sha256(
                    (root / "manifest.json").read_bytes()
                ).hexdigest(),
                "reference_files": {
                    p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in parent.iterdir()
                },
                "mapping_reference_results_sha256": hashlib.sha256(
                    mapping_ref.read_bytes()
                ).hexdigest(),
                "research_github_manifest_sha256": hashlib.sha256(
                    research.read_bytes()
                ).hexdigest(),
            }
        )
        protocol = root / "context.json"
        protocol.write_text(json.dumps(plan))
        args = (
            root,
            root / "manifest.json",
            protocol,
            parent,
            mapping_ref,
            research,
            root / "context",
        )
        result = context_study(*args)
        with pytest.raises(FileExistsError):
            context_study(*args)
        assert result["total_model_fits"] == 24
        assert result["reserved_predictions"] == result["reserved_evaluations"] == 0
        for p in (root / "context").glob("*-predictions.csv"):
            assert pd.read_csv(p).date_id.max() < 200
        for w in result["windows"]:
            assert len(w["cases"]) == 4
            assert all(
                case["rowwise_replay"]["float32_allocations_exact"] for case in w["cases"].values()
            )
        # A corrupted declared reference fails before model fitting.
        with (parent / "results.json").open("a") as handle:
            handle.write(" ")
        with pytest.raises(ValueError, match="hash"):
            context_study(*args[:-1], root / "corrupt")
        assert json.loads((root / "corrupt/failure.json").read_text())["completed_model_fits"] == 0
        return result

    a = execute(tmp_path / "first", False)
    b = execute(tmp_path / "changed", True)
    assert a["windows"] == b["windows"]
