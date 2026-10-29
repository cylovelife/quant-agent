"""Factorial controls, lag causality and self-financing fee regression checks.

@author cylovelife
"""

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from test_hull_reversal import write_reference

from quant_agent.competitions.hull.ablation import cost_path, evaluate_window, validate_plan


def test_post_fee_target_entry_and_mark_to_market_drift():
    # Buy-and-hold market pays once on entry; constant target 1 needs no rebalance.
    market = np.array([0.1, -0.05, 0.02])
    frame = cost_path(market, np.zeros(3), np.ones(3), 10)
    fee = 0.001 / 1.001
    assert frame.fee_fraction.iloc[0] == pytest.approx(fee)
    np.testing.assert_allclose(frame.traded_fraction.iloc[1:], 0, atol=1e-15)
    assert np.prod(1 + frame.net_return) == pytest.approx(np.prod(1 + market) / 1.001)
    # Leverage drifts down after an up day; restoring 1.5 buys additional shares.
    path = cost_path(np.array([0.1, 0.0]), np.zeros(2), np.full(2, 1.5), 10)
    drift = 1.5 * 1.1 / 1.15
    assert path.pretrade_risky_weight.iloc[1] == pytest.approx(drift)
    assert path.fee_fraction.iloc[1] == pytest.approx(0.001 * (1.5 - drift) / 1.0015)


def test_sell_fee_self_financing_and_future_cannot_change_past():
    market = np.array([0.1, -0.05, 0.02, 0.03])
    weights = np.array([1.5, 0.5, 1.0, 1.0])
    frame = cost_path(market, np.zeros(4), weights, 10)
    row = frame.iloc[1]
    assert row.fee_fraction == pytest.approx(
        0.001 * (row.pretrade_risky_weight - 0.5) / (1 - 0.001 * 0.5)
    )
    for _, r in frame.iterrows():
        assert r.fee_fraction == pytest.approx(0.001 * r.traded_fraction)
        assert r.traded_fraction == pytest.approx(
            abs(r.target_weight * (1 - r.fee_fraction) - r.pretrade_risky_weight)
        )
    changed = cost_path(np.r_[market[:2], 0.9, 0.9], np.zeros(4), weights, 10)
    pd.testing.assert_frame_equal(frame.iloc[:2], changed.iloc[:2])


def test_cost_zero_identity_monotonicity_and_rejections():
    r = np.array([0.01, -0.02, 0.03, 0.005])
    f = np.full(4, 0.0001)
    w = np.array([1.2, 0.7, 1.4, 0.6])
    zero = cost_path(r, f, w, 0)
    np.testing.assert_allclose(zero.net_return, f + w * (r - f), atol=1e-16)
    wealth = [np.prod(1 + cost_path(r, f, w, c).net_return) for c in (0, 1, 5, 20)]
    assert np.all(np.diff(wealth) < 0)
    for bad in (-1, np.nan, 5000):
        with pytest.raises(ValueError):
            cost_path(r, f, w, bad)
    with pytest.raises(ValueError):
        cost_path(r, f, np.array([1, 1, np.inf, 1]), 1)
    with pytest.raises(ValueError):
        cost_path(np.array([-0.9]), np.zeros(1), np.array([2.0]), 1)


def load_synthetic(root):
    _, protocol, reference = write_reference(root)
    plan = json.loads(protocol.read_text())
    history = pd.read_csv(root / "train.csv").iloc[:900].copy()
    history["label_end_id"] = history.date_id.shift(-1)
    preds, oofs = {}, {}
    for m in ("ridge", "tree"):
        preds[m] = pd.read_csv(reference / f"development-1-{m}-predictions.csv")
        oofs[m] = pd.read_csv(reference / f"development-1-{m}-oof.csv")
    return history, preds, oofs, plan


def test_factorial_full_matches_prior_policy_and_market_stabilizer_is_identity(tmp_path):
    from quant_agent.competitions.hull.reversal_study import evaluate_window as old_evaluate

    history, preds, oofs, _ = load_synthetic(tmp_path / "fixture")
    record, traces = evaluate_window(history, preds, oofs, 700, 800, 520, [0, 1, 2, 5, 10, 20])
    _, old = old_evaluate(history, preds, oofs, 700, 800, 520)
    for m, trace in traces.items():
        np.testing.assert_array_equal(trace.base, old[m].base_position)
        np.testing.assert_array_equal(trace.stabilizer_and_target, old[m].position)
        assert (trace.risk_label_end_max <= trace.date_id).all()
        np.testing.assert_allclose(
            record["comparators"][m]["arms"]["base"]["costs"]["0"]["net_total_return"],
            record["comparators"][m]["arms"]["base"]["gross"]["gross_total_return"],
        )
    np.testing.assert_array_equal(traces["market"].stabilizer_only, np.ones(99))
    np.testing.assert_array_equal(
        traces["market"].target_only, traces["market"].stabilizer_and_target
    )


def test_ablation_future_and_reserved_perturbations_do_not_change_past(tmp_path):
    history, preds, oofs, _ = load_synthetic(tmp_path / "fixture")
    args = (preds, oofs, 700, 800, 520, [0, 1, 2, 5, 10, 20])
    record, traces = evaluate_window(history, *args)
    history.loc[history.date_id >= 850, ["forward_returns", "risk_free_rate"]] = 99
    other, _ = evaluate_window(history, *args)
    assert record == other
    history.loc[history.date_id >= 750, "forward_returns"] = 0.5
    _, changed = evaluate_window(history, *args)
    for m in traces:
        pd.testing.assert_frame_equal(traces[m].iloc[:51], changed[m].iloc[:51])


def test_protocol_rejects_budget_signal_and_reserved_changes():
    plan = json.loads(
        (Path(__file__).parents[1] / "docs/competitions/hull_ablation_protocol.json").read_text()
    )
    validate_plan(plan)
    for key, value in [
        ("cost_bps", [0, 5]),
        ("reserved_evaluation_allowed", True),
        ("new_signal_configurations", 1),
        ("max_predictor_fits", 1),
    ]:
        with pytest.raises(ValueError):
            validate_plan(plan | {key: value})


def test_full_study_frozen_references_reserved_boundary_and_no_learner(tmp_path, monkeypatch):
    from sklearn.pipeline import Pipeline

    from quant_agent.competitions.hull.ablation import run_study
    from quant_agent.competitions.hull.reversal_study import run_study as make_prior

    def forbidden(*args, **kwargs):
        raise AssertionError("learner invoked")

    monkeypatch.setattr(Pipeline, "fit", forbidden)
    monkeypatch.setattr(Pipeline, "predict", forbidden)
    results = []
    for name, changed in [("original", False), ("changed_reserved", True)]:
        root = tmp_path / name
        manifest, old_protocol, reference = write_reference(root, changed)
        prior = root / "prior"
        make_prior(root, manifest, old_protocol, reference, prior)
        fixed = json.loads(
            (
                Path(__file__).parents[1] / "docs/competitions/hull_ablation_protocol.json"
            ).read_text()
        )
        parent = json.loads(old_protocol.read_text())
        for key in (
            "expected_train_sha256",
            "expected_manifest_sha256",
            "development_windows",
            "development_input_rows",
            "development_last_id",
            "reserved_window",
            "reference_files",
        ):
            fixed[key] = parent[key]
        import hashlib

        fixed["prior_reversal_files"] = {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in prior.iterdir()
            if p.is_file()
        }
        protocol = root / "ablation.json"
        protocol.write_text(json.dumps(fixed))
        result = run_study(root, manifest, protocol, reference, prior, root / "output")
        assert result["scored_cases"] == 32 and result["cost_cases"] == 192
        assert result["reserved_predictions"] == result["reserved_evaluations"] == 0
        assert result["predictor_fits"] == result["model_predictions"] == 0
        frozen = json.loads((root / "output/frozen.json").read_text())
        assert frozen["parsed_max_id"] == 899
        for path in (root / "output").glob("*-allocations.csv"):
            assert pd.read_csv(path).date_id.max() < 900
        with pytest.raises(FileExistsError):
            run_study(root, manifest, protocol, reference, prior, root / "output")
        if not changed:
            (prior / "results.json").write_text("{}")
            with pytest.raises(ValueError, match="hash"):
                run_study(root, manifest, protocol, reference, prior, root / "corrupted")
        results.append(result)
    assert results[0] == results[1]


def test_net_sharpe_uses_excess_volatility_when_cash_rate_varies():
    from quant_agent.competitions.hull.ablation import cost_report

    solution = pd.DataFrame(
        {"forward_returns": [0.01, -0.02, 0.03], "risk_free_rate": [0.001, 0.004, 0.002]}
    )
    report = cost_report(solution, np.ones(3), 0)
    excess = solution.forward_returns - solution.risk_free_rate
    assert report["arithmetic_excess_sharpe"] == pytest.approx(
        excess.mean() / excess.std(ddof=1) * np.sqrt(252)
    )
