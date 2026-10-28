"""Research review regressions using isolated caches and synthetic price histories.

@author cylovelife
"""

import copy
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import quant_agent.application.research.model_selection as ms  # noqa: E402
import quant_agent.domain.research.exit_model as em  # noqa: E402
import quant_agent.domain.research.ml_eval as ml_eval  # noqa: E402
import quant_agent.infrastructure.persistence.phase_cache as pc  # noqa: E402


def parameters():
    return {
        "weights": dict.fromkeys(("boll", "rsi", "volume", "trend", "macd", "drawdown"), 1 / 6),
        "score_threshold": 0.0,
        "indicators": {"rsi_n": 14},
        "backtest": {"hold_days": 5, "stop_loss": None, "take_profit": None},
    }


def prices():
    close = 100.0 + np.arange(500)
    return pd.DataFrame(
        {
            "date": pd.bdate_range("2023-01-02", periods=len(close)),
            "open": close,
            "close": close,
            "high": close + 1,
            "low": close - 1,
            "volume": np.full(len(close), 1000.0),
        }
    )


def selection_config():
    return {
        "temporal_validation": {
            "validation_start": "2024-01-01",
            "test_start": "2024-07-01",
            "test_end": "2024-12-31",
        },
        "model_select": {
            "shorts": {
                "hold_days": [1],
                "stop_loss": [None],
                "take_profit": [None],
            }
        },
    }


@pytest.mark.parametrize(
    ("threshold", "statistic", "expected"),
    [(-2.0, -1.2, "WATCH"), (-2.0, -2.1, "SELL"), (None, None, "SELL")],
)
def test_live_exit_uses_same_selected_gate_as_policy_simulation(threshold, statistic, expected):
    state = {"n": 100, "v": -0.01, "v_t": statistic}
    key = em.state_key(2, em._rbucket(0.01), 0)
    model = {
        "states": {key: state},
        "coarse": {},
        "score_edges": [],
        "tau": 0.0,
        "t_min": threshold,
        "min_bucket_n": 20,
    }
    position = {
        "code": "fixture",
        "days_held": 2,
        "ret": 0.01,
        "entry_score": 70,
    }
    params = parameters()
    params["backtest"].update(stop_loss=-0.05, take_profit=0.08)
    live = em.exit_advice([position], model, params)[0]
    paths = [pd.DataFrame([{"d": 2, "ret0": 0.01, "ret_expire": -0.02, "score": 70}])]
    simulated = em._simulate(paths, model["states"], {}, [], 0.0, 20, True, 5, threshold)
    assert live["advice"] == expected
    assert (simulated["early_exit_ratio"] > 0) == (expected == "SELL")


@pytest.mark.parametrize(
    ("returns", "drawdown"),
    [([-0.1], -0.1), ([-0.1, -0.2], -0.28), ([0.1, -0.1], -0.1)],
)
def test_exit_evaluation_counts_initial_capital_in_drawdown(returns, drawdown):
    paths = [
        pd.DataFrame([{"d": 1, "ret0": 0.0, "ret_expire": value, "score": 70}]) for value in returns
    ]
    result = em._simulate(paths, {}, {}, [], 0.0, 20, False, 5)
    assert result["max_dd"] == pytest.approx(drawdown)


def test_disabled_stops_and_targets_are_accepted_by_exit_advice():
    result = em.exit_advice([{"code": "fixture", "days_held": 1, "ret": -0.10}], None, parameters())
    assert result[0]["advice"] == "HOLD"


def test_scan_keeps_full_current_horizon_when_grid_is_shorter():
    result = em.parameter_scan({("etf", "fixture"): prices()}, parameters(), horizons=(1,))
    assert result["current"] == {"horizon": 5, "stop": None, "take": None}
    assert result["baseline"]["avg"] > 0.0
    assert result["by_horizon"][0]["avg"] == 0.0


def test_signal_collection_includes_last_fully_matured_label():
    frame = prices().iloc[:100].copy()
    signals = em._collect_signals({("etf", "fixture"): frame}, parameters(), 5, with_date=True)
    assert len(signals) == len(frame) - 5 - em.WARMUP
    assert signals[-1][0] == str(frame.date.iloc[-6])[:10]
    assert signals[-1][3] == str(frame.date.iloc[-1])[:10]


def test_short_selection_keeps_full_current_horizon_when_grid_is_shorter():
    result = ms.select_short(selection_config(), parameters(), {("etf", "fixture"): prices()})
    assert result is not None
    assert result["current"]["hold_days"] == 5
    assert result["current_stats"]["avg_days"] == 5.0
    assert result["current_stats"]["avg"] > 0.0
    assert result["folds"][0]["oos_current"]["avg_days"] == 5.0


def test_indicator_parameters_invalidate_each_dependent_phase_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(pc, "CACHE_DIR", str(tmp_path))
    params = parameters()
    changed = copy.deepcopy(params)
    changed["indicators"]["rsi_n"] = 7
    signature = pc.kline_signature({("etf", "fixture"): prices()})
    for phase in ("exit_model", "param_scan", "select_short"):
        old_key = pc.cache_key(phase, signature, pc.params_signature(params, {}))
        assert pc.save(phase, old_key, {"rsi_n": 14})
        new_key = pc.cache_key(phase, signature, pc.params_signature(changed, {}))
        assert new_key != old_key
        assert pc.load(phase, new_key) is None


def test_numpy_nonfinite_cache_values_are_standard_json_null(tmp_path, monkeypatch):
    monkeypatch.setattr(pc, "CACHE_DIR", str(tmp_path))
    payload = {"nan": np.float64(np.nan), "inf": np.float32(np.inf)}
    assert pc.save("fixture", "key", payload)
    assert pc.load("fixture", "key") == {"nan": None, "inf": None}
    json.dumps(pc._sanitize(payload), allow_nan=False)


def ml_fixture():
    rng = np.random.default_rng(123)
    rows = []
    for day in pd.date_range("2025-01-01", periods=45, freq="5D"):
        for symbol in range(10):
            feature = float(rng.normal())
            rows.append(
                {
                    "date": str(day.date()),
                    "label_end_ret_20": str((day + pd.Timedelta(days=2)).date()),
                    "score": feature,
                    "v_quality": feature,
                    "ret_20": feature + float(rng.normal(0, 0.2)),
                    "code": str(symbol),
                }
            )
    panel = pd.DataFrame(rows)
    dates = sorted(panel.date.unique())
    options = {
        "features": ["v_quality"],
        "kinds": ("ridge",),
        "min_train_periods": 3,
        "min_train_rows": 20,
        "min_oof_periods": 3,
        "n_perm": 0,
        "boundaries": {
            "validation_start": dates[25],
            "test_start": dates[34],
            "test_end": str((pd.Timestamp(dates[-1]) + pd.Timedelta(days=3)).date()),
        },
    }
    return panel, options


def test_ml_and_rule_compare_identical_finite_test_rows():
    panel, options = ml_fixture()
    test = panel.date >= options["boundaries"]["test_start"]
    excluded = test & (panel.code.astype(int) >= 5)
    panel.loc[excluded, "score"] = np.nan
    panel.loc[test & (panel.code == "5"), "score"] = np.inf
    first = ml_eval.evaluate_against_rule(panel, "ret_20", **options)
    changed = panel.copy()
    changed.loc[excluded, "ret_20"] *= -10
    again = ml_eval.evaluate_against_rule(changed, "ret_20", **options)
    assert first["rule"] == again["rule"]
    assert first["candidates"] == again["candidates"]
    assert first["candidates"]["ridge"]["n_oof"] == int((test & ~excluded).sum())


def test_ml_and_rule_compare_identical_evaluable_test_dates():
    panel, options = ml_fixture()
    test_dates = sorted(
        panel.loc[panel.date >= options["boundaries"]["test_start"], "date"].unique()
    )
    constant_rule = panel.date == test_dates[0]
    constant_prediction = panel.date == test_dates[1]
    panel.loc[constant_rule, "score"] = 1.0
    panel.loc[constant_prediction, "v_quality"] = 1.0
    first = ml_eval.evaluate_against_rule(panel, "ret_20", **options)
    changed = panel.copy()
    changed.loc[constant_rule | constant_prediction, "ret_20"] *= -10
    again = ml_eval.evaluate_against_rule(changed, "ret_20", **options)
    assert first["rule"] == again["rule"]
    assert first["candidates"] == again["candidates"]
    assert first["rule"]["n_periods"] == len(test_dates) - 2
    assert first["candidates"]["ridge"]["n_periods"] == len(test_dates) - 2


def test_phase_verifier_rejects_missing_boundaries_before_reading_price_cache(monkeypatch, capsys):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent))
    import verify_phase_cache as verifier

    from quant_agent.application.investing import workflow as run

    monkeypatch.setattr(sys, "argv", ["verify_phase_cache.py"])
    monkeypatch.setattr(run, "load_configs", lambda: (parameters(), {}))

    def forbidden(*args, **kwargs):
        raise AssertionError("unconfigured verification must not read/fetch prices")

    monkeypatch.setattr(verifier.oe, "build_kline_map", forbidden)
    monkeypatch.setattr(run, "cached_phase", forbidden)
    assert verifier.main() == 2
    assert "固定" in capsys.readouterr().out


def test_phase_verifier_does_not_pass_when_all_stages_return_no_result(monkeypatch, capsys):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parent))
    import verify_phase_cache as verifier

    from quant_agent.application.investing import workflow as run

    original_params, original_cfg = parameters(), {}
    before = copy.deepcopy(original_params)
    monkeypatch.setattr(sys, "argv", ["verify_phase_cache.py", "--synthetic"])
    monkeypatch.setattr(run, "load_configs", lambda: (original_params, original_cfg))
    frames = verifier.oe.synthetic_kline_map()
    monkeypatch.setattr(
        verifier.oe,
        "synthetic_kline_map",
        lambda: {key: frame.iloc[:20] for key, frame in frames.items()},
    )
    old_cache, old_log = pc.CACHE_DIR, run.log
    assert verifier.main() == 1
    assert "无结果" in capsys.readouterr().out
    assert pc.CACHE_DIR == old_cache and run.log is old_log
    assert original_params == before and original_cfg == {}
