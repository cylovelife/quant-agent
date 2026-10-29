"""Transparent reversal logic, causal bootstrap and online sequence equivalence.

@author cylovelife
"""

import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from quant_agent.competitions.hull.published_portfolio import RiskSettings
from quant_agent.competitions.hull.reversal import ReversalPolicy, reversal_exposure


def risk_settings():
    return RiskSettings(252, 252, 126, 21, 0.15, 1e-6, 1.0, "pre_overlay")


def test_reversal_has_dead_zone_contrarian_sign_and_bounded_size():
    alternating = np.tile([-0.01, 0.01], 11)[:21]
    assert reversal_exposure(alternating)[0] == 1
    fallen = np.r_[np.tile([-0.005, 0.005], 8), np.full(5, -0.02)]
    risen = -fallen
    assert 1 < reversal_exposure(fallen)[0] <= 1.5
    assert 0.5 <= reversal_exposure(risen)[0] < 1
    assert reversal_exposure(np.zeros(21))[0] == 1
    assert reversal_exposure(np.ones(20) * 0.01)[0] == 1
    with pytest.raises(ValueError):
        reversal_exposure(np.array([np.nan]))


def replay(returns, cash=None, skip=None):
    policy = ReversalPolicy(risk_settings())
    cash = np.zeros(len(returns)) if cash is None else cash
    records = []
    for i, (r, f) in enumerate(zip(returns, cash, strict=True)):
        decision = policy.step(i, float(r), float(f), decide=(skip is None or i not in skip))
        if decision is not None:
            records.append(decision)
    return policy, records


def test_future_returns_and_skipped_decisions_preserve_past_policy_outputs():
    returns = np.random.default_rng(7).normal(0, 0.01, 350)
    policy, a = replay(returns, skip={155, 199})
    modified = returns.copy()
    modified[250:] = 0.5
    _, b = replay(modified, skip={155, 199})
    assert a[:248] == b[:248]
    assert all(0 <= d.position <= 2 for d in a)
    assert len(policy.risk_history) <= 252
    # A skipped decision creates no invented portfolio PnL for that date.
    assert 155 not in [x["date_id"] for x in policy.risk_history]
    assert 199 not in [x["date_id"] for x in policy.risk_history]


def test_policy_rejects_invalid_inputs_without_corrupting_sequence_state():
    p = ReversalPolicy(risk_settings())
    p.step(0, 0.01, 0)
    with pytest.raises(ValueError):
        p.step(1, np.nan, 0)
    with pytest.raises(ValueError):
        p.step(1, 0.01, 0, base_override=np.nan)
    d = p.step(1, 0.01, 0)
    assert d.base_position == 1
    with pytest.raises(ValueError, match="ordered"):
        p.step(1, 0.01, 0)


def test_same_policy_and_history_can_receive_frozen_comparator_exposures():
    p = ReversalPolicy(risk_settings())
    for i in range(160):
        d = p.step(i, float(0.01 * np.sin(i)), 0, base_override=1.25)
        assert d.base_position == 1.25
    assert d.risk_decision.status == "calibrated"
    assert d.risk_decision.risk_label_end_max <= d.date_id


def test_visible_row_adapter_uses_only_lagged_fields_and_matches_direct_steps():
    a, b = ReversalPolicy(risk_settings()), ReversalPolicy(risk_settings())
    for i in range(160):
        r = float(0.01 * np.sin(i))
        frame = pd.DataFrame(
            {"date_id": [i], "lagged_forward_returns": [r], "lagged_risk_free_rate": [0.0]}
        )
        assert a.predict_visible(frame) == b.step(i, r, 0).position
    with pytest.raises(ValueError, match="labels"):
        a.predict_visible(frame.assign(forward_returns=1))


def write_reference(root, alter_reserved=False):
    from quant_agent.competitions.hull.ridge_study import portfolio_report

    root.mkdir()
    ids = np.arange(1000)
    frame = pd.DataFrame(
        {
            "date_id": ids,
            "forward_returns": 0.0004 + 0.012 * np.sin(ids / 5) + 0.003 * np.cos(ids / 11),
            "risk_free_rate": 0.0001,
        }
    )
    if alter_reserved:
        frame.loc[frame.date_id >= 900, ["forward_returns", "risk_free_rate"]] = 99
    frame.to_csv(root / "train.csv", index=False)

    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    manifest = root / "manifest.json"
    manifest.write_text(
        json.dumps({"files": [{"path": "train.csv", "sha256": sha(root / "train.csv")}]})
    )
    windows = [[700, 800], [800, 900]]
    parent = {
        "expected_train_sha256": sha(root / "train.csv"),
        "development_windows": windows,
        "reserved_window": [900, 1000],
        "calibration": {"blocks": 3, "block_rows": 60},
    }
    previous = {"windows": [], "reserved_predictions": 0, "reserved_evaluations": 0}
    reference = root / "reference"
    reference.mkdir()
    for number, (start, end) in enumerate(windows, 1):
        record = {}
        valid = frame.loc[frame.date_id.between(start, end - 2)]
        oof_ids = np.concatenate([np.arange(a, a + 59) for a in range(start - 180, start, 60)])
        for model in ["ridge", "tree"]:
            p = 0.001 * np.sin(oof_ids / 7) + (0.0003 if model == "tree" else 0)
            rms = float(np.sqrt(np.mean(p**2)))
            current = 0.001 * np.sin(valid.date_id.to_numpy() / 7)
            weights = np.clip(1 + 0.25 * current / rms, 0, 2).astype(np.float32).astype(float)
            pd.DataFrame({"date_id": oof_ids, "label_end_id": oof_ids + 1, "forecast": p}).to_csv(
                reference / f"development-{number}-{model}-oof.csv", index=False
            )
            pd.DataFrame(
                {"date_id": valid.date_id, "forecast": current, "allocation": weights}
            ).to_csv(reference / f"development-{number}-{model}-predictions.csv", index=False)
            record[model] = {
                "mapping": {"rms": rms},
                "oof_rows": len(oof_ids),
                "portfolio": portfolio_report(valid, weights),
            }
        previous["windows"].append(record)
    (reference / "frozen.json").write_text(json.dumps({"protocol": parent}))
    (reference / "results.json").write_text(json.dumps(previous))
    plan = json.loads(
        (
            Path(__file__).resolve().parents[1] / "docs/competitions/hull_reversal_protocol.json"
        ).read_text()
    )
    plan.update(
        {
            **{
                k: parent[k]
                for k in ("expected_train_sha256", "development_windows", "reserved_window")
            },
            "expected_manifest_sha256": sha(manifest),
            "development_input_rows": 900,
            "development_last_id": 899,
            "reference_files": {p.name: sha(p) for p in reference.iterdir()},
        }
    )
    protocol = root / "protocol.json"
    protocol.write_text(json.dumps(plan))
    return manifest, protocol, reference


def test_complete_control_is_reserved_invariant_and_never_calls_learners(tmp_path, monkeypatch):
    from sklearn.pipeline import Pipeline

    from quant_agent.competitions.hull.reversal_study import run_study

    def forbidden(*args, **kwargs):
        raise AssertionError("learner invoked")

    monkeypatch.setattr(Pipeline, "fit", forbidden)
    monkeypatch.setattr(Pipeline, "predict", forbidden)
    results = []
    for name, alter in [("first", False), ("changed", True)]:
        root = tmp_path / name
        manifest, protocol, reference = write_reference(root, alter)
        result = run_study(root, manifest, protocol, reference, root / "run")
        assert result["scored_cases"] == 16 and result["predictor_fits"] == 0
        assert result["reserved_predictions"] == result["reserved_evaluations"] == 0
        assert result["frozen_model_allocations_reproduced"]
        with pytest.raises(FileExistsError):
            run_study(root, manifest, protocol, reference, root / "run")
        for p in (root / "run").glob("*-allocations.csv"):
            assert pd.read_csv(p).date_id.max() < 900
        results.append(result)
    assert results[0]["windows"] == results[1]["windows"]


def test_frozen_oof_normalization_uses_only_earlier_predictions():
    from quant_agent.competitions.hull.reversal_study import causal_oof_weights

    frame = pd.DataFrame({"date_id": np.arange(150), "forecast": 0.001 * np.sin(np.arange(150))})
    a = causal_oof_weights(frame)
    frame.loc[100:, "forecast"] = 99
    b = causal_oof_weights(frame)
    np.testing.assert_array_equal(a.iloc[:100], b.iloc[:100])
    np.testing.assert_array_equal(a.iloc[:63], np.ones(63))


def test_standalone_export_has_no_project_runtime_import_and_matches_policy(tmp_path):
    import ast
    import runpy

    from quant_agent.competitions.hull.reversal import bootstrap_reversal
    from quant_agent.competitions.hull.reversal_package import export_script

    root = tmp_path / "input"
    root.mkdir()
    ids = np.arange(700)
    history = pd.DataFrame(
        {"date_id": ids, "forward_returns": 0.01 * np.sin(ids / 5), "risk_free_rate": 0.0001}
    )
    history.to_csv(root / "train.csv", index=False)
    script = export_script(tmp_path / "submission", blocks=3, block_rows=60)
    tree = ast.parse(script.read_text())
    assert not any(
        isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("quant_agent")
        for n in ast.walk(tree)
    )
    loaded = runpy.run_path(str(script), run_name="standalone_policy")
    predict = loaded["make_predictor"](root)
    expected = bootstrap_reversal(history, 700, risk_settings(), blocks=3, block_rows=60)
    last_r = float(history.forward_returns.iloc[-1])
    for i in range(700, 716):
        row = pd.DataFrame(
            {"date_id": [i], "lagged_forward_returns": [last_r], "lagged_risk_free_rate": [0.0001]}
        )
        assert predict(row) == expected.predict_visible(row)
        last_r = float(0.01 * np.sin(i / 5))
    with pytest.raises(FileExistsError):
        export_script(tmp_path / "submission", blocks=3, block_rows=60)
