"""Ridge prediction, causal calibration and allocation boundary regression tests.

@author cylovelife
"""

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from quant_agent.competitions.hull.data import HullData
from quant_agent.competitions.hull.ridge import (
    LinearAllocation,
    fit_ridge,
    prepare_panel,
    run_window,
)
from quant_agent.competitions.hull.ridge_study import run_study


@pytest.fixture
def history():
    n = 240
    rng = np.random.default_rng(21)
    m = np.sin(np.arange(n) * 0.3)
    target = m * 0.005 + rng.normal(0, 0.001, n)
    train = pd.DataFrame(
        {
            "date_id": np.arange(n),
            "M1": m,
            "E1": np.cos(np.arange(n) * 0.2),
            "D1": np.arange(n) % 2,
            "forward_returns": target + 0.0002,
            "risk_free_rate": 0.0001,
            "market_forward_excess_returns": target,
        }
    )
    train.loc[:110, "E1"] = np.nan
    return HullData(train, pd.DataFrame(), ("M1", "E1", "D1"), {})


def test_mapping_has_fixed_strength_bounds_and_zero_signal_fallback():
    rule = LinearAllocation.from_oof(np.array([0.01, -0.01]))
    assert rule.gain == pytest.approx(25)
    np.testing.assert_array_equal(rule.apply(np.array([-1, 0, 0.01, 1])), [0, 1, 1.25, 2])
    neutral = LinearAllocation.from_oof(np.zeros(10))
    np.testing.assert_array_equal(neutral.apply(np.array([-1, 1])), [1, 1])
    with pytest.raises(ValueError):
        LinearAllocation.from_oof(np.array([np.nan, 1]))
    with pytest.raises(ValueError):
        rule.apply(np.array([np.inf]))


def test_preprocessing_keeps_empty_columns_and_is_not_refit_on_inference(history):
    panel, features = prepare_panel(history, 200)
    model = fit_ridge(panel.iloc[:100], features)
    before = model.named_steps["features"].transformer_list[0][1].named_steps["scale"].mean_.copy()
    first = model.predict(panel.iloc[120:130][list(features)])
    changed = panel.iloc[120:140][list(features)].copy()
    changed.iloc[10:] *= 1000
    np.testing.assert_allclose(first, model.predict(changed)[:10])
    np.testing.assert_array_equal(
        before, model.named_steps["features"].transformer_list[0][1].named_steps["scale"].mean_
    )
    assert model.named_steps["ridge"].n_features_in_ == 2 * len(features)
    assert np.isfinite(first).all()


def test_validation_labels_and_future_features_cannot_change_predictions_or_mapping(history):
    panel, features = prepare_panel(history, 200)
    a = run_window(panel, features, 130, 160, blocks=2, block_rows=20)
    changed = panel.copy()
    changed.loc[changed.date_id >= 130, "market_forward_excess_returns"] = 50
    changed.loc[changed.date_id >= 160, list(features)] = 999
    b = run_window(changed, features, 130, 160, blocks=2, block_rows=20)
    np.testing.assert_array_equal(a.predictions.forecast, b.predictions.forecast)
    np.testing.assert_array_equal(a.predictions.allocation, b.predictions.allocation)
    assert a.mapping == b.mapping
    assert a.oof.label_end_id.max() < 130
    assert a.model_fits == 3
    assert a.oof.shape[0] == 38


def test_reserved_rows_are_physically_absent_and_lag_is_one_step(history):
    panel, features = prepare_panel(history, 200)
    changed = history.train.copy()
    changed.loc[
        changed.date_id >= 200, ["M1", "forward_returns", "market_forward_excess_returns"]
    ] = 999
    other, names = prepare_panel(HullData(changed, history.mock, history.features, {}), 200)
    pd.testing.assert_frame_equal(panel, other)
    assert names == features
    assert panel.date_id.max() == 199
    assert panel.loc[20, "lagged_forward_returns"] == history.train.loc[19, "forward_returns"]


def test_fitted_policy_rowwise_allocations_match_vectorized_predictions(history):
    panel, features = prepare_panel(history, 200)
    fitted = run_window(panel, features, 130, 160, blocks=2, block_rows=20)
    rows = panel.loc[panel.date_id.isin(fitted.predictions.date_id), list(features)]
    rowwise = np.array([fitted.allocate(row) for _, row in rows.groupby(rows.index)])
    np.testing.assert_array_equal(rowwise, fitted.predictions.allocation)
    np.testing.assert_array_equal(
        fitted.mapping.apply(fitted.predictions.forecast), fitted.predictions.allocation
    )


def write_source(root, train):
    root.mkdir()
    mock = train.loc[220:224, ["date_id", "M1", "E1", "D1"]].copy()
    mock["is_scored"] = True
    for name in ("forward_returns", "risk_free_rate", "market_forward_excess_returns"):
        mock["lagged_" + name] = train[name].shift(1).loc[220:224]
    train.to_csv(root / "train.csv", index=False)
    mock.to_csv(root / "test.csv", index=False)
    entries = [
        {"path": name, "sha256": hashlib.sha256((root / name).read_bytes()).hexdigest()}
        for name in ("train.csv", "test.csv")
    ]
    (root / "manifest.json").write_text(
        json.dumps({"competition": "hull-tactical-market-prediction", "files": entries})
    )
    protocol = json.loads(
        (
            Path(__file__).resolve().parents[1] / "docs/competitions/hull_ridge_protocol.json"
        ).read_text()
    )
    protocol.update(
        {
            "expected_training_rows": len(train),
            "expected_train_sha256": entries[0]["sha256"],
            "development_windows": [[130, 160], [160, 190]],
            "reserved_window": [200, 240],
            "calibration": {"blocks": 2, "block_rows": 20},
            "max_model_fits": 6,
        }
    )
    (root / "protocol.json").write_text(json.dumps(protocol))


def test_full_study_is_reserved_invariant_and_refuses_reserved_scoring(history, tmp_path):
    first = tmp_path / "first"
    write_source(first, history.train.copy())
    a = run_study(first, first / "manifest.json", first / "protocol.json", first / "result")
    with pytest.raises(FileExistsError):
        run_study(first, first / "manifest.json", first / "protocol.json", first / "result")
    altered = history.train.copy()
    altered.loc[
        altered.date_id >= 200,
        ["M1", "forward_returns", "risk_free_rate", "market_forward_excess_returns"],
    ] = 999
    second = tmp_path / "second"
    write_source(second, altered)
    b = run_study(second, second / "manifest.json", second / "protocol.json", second / "result")
    assert a["windows"] == b["windows"]
    assert a["reserved_evaluations"] == b["reserved_evaluations"] == 0
    assert a["reserved_predictions"] == b["reserved_predictions"] == 0
    assert a["model_fits"] == 6
    assert len(list((first / "result").glob("development-*-predictions.csv"))) == 2
    protocol = json.loads((first / "protocol.json").read_text())
    protocol["reserved_window_evaluation_allowed"] = True
    (first / "bad.json").write_text(json.dumps(protocol))
    with pytest.raises(ValueError, match="reserved"):
        run_study(first, first / "manifest.json", first / "bad.json", first / "bad-result")
    assert not (first / "bad-result/frozen.json").exists()


def test_tree_comparison_reproduces_reference_and_remains_reserved_invariant(history, tmp_path):
    from quant_agent.competitions.hull.tree_study import run_study as compare

    def execute(root, train):
        write_source(root, train)
        reference = root / "ridge-result"
        run_study(root, root / "manifest.json", root / "protocol.json", reference)
        plan = json.loads(
            (
                Path(__file__).resolve().parents[1] / "docs/competitions/hull_tree_protocol.json"
            ).read_text()
        )
        original = json.loads((root / "protocol.json").read_text())
        for key in (
            "expected_training_rows",
            "expected_train_sha256",
            "development_windows",
            "reserved_window",
            "calibration",
            "max_model_fits",
        ):
            plan[key] = original[key]
        plan["max_total_comparison_fits"] = original["max_model_fits"] * 2
        plan["ridge_reference"] = {
            "protocol_sha256": hashlib.sha256((root / "protocol.json").read_bytes()).hexdigest(),
            "results_sha256": hashlib.sha256((reference / "results.json").read_bytes()).hexdigest(),
            "prediction_sha256": {
                str(i): hashlib.sha256(
                    (reference / f"development-{i}-predictions.csv").read_bytes()
                ).hexdigest()
                for i in (1, 2)
            },
        }
        protocol = root / "tree-protocol.json"
        protocol.write_text(json.dumps(plan))
        result = compare(root, root / "manifest.json", protocol, reference, root / "comparison")
        return result, protocol, reference

    first = tmp_path / "first"
    a, protocol, reference = execute(first, history.train.copy())
    assert all(w["ridge"]["previous_frozen_result_reproduced"] for w in a["windows"])
    assert a["model_fits"] == {"ridge_reproduction": 6, "tree": 6}
    assert all(w["tree"]["boosting_iterations"] == 80 for w in a["windows"])
    changed = history.train.copy()
    changed.loc[
        changed.date_id >= 200,
        ["M1", "forward_returns", "risk_free_rate", "market_forward_excess_returns"],
    ] = 999
    b, _, _ = execute(tmp_path / "altered", changed)
    assert a["windows"] == b["windows"]
    assert a["reserved_evaluations"] == b["reserved_evaluations"] == 0
    assert a["reserved_predictions"] == b["reserved_predictions"] == 0
    with pytest.raises(FileExistsError):
        compare(first, first / "manifest.json", protocol, reference, first / "comparison")
    with (reference / "development-1-predictions.csv").open("a") as stream:
        stream.write("\n")
    with pytest.raises(ValueError, match="reference prediction hash"):
        compare(first, first / "manifest.json", protocol, reference, first / "bad-reference")
    assert not (first / "bad-reference/frozen.json").exists()
