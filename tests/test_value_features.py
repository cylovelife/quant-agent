"""Value evidence and continuous features must preserve missing/sector semantics.

@author cylovelife
"""

import numpy as np
import pandas as pd
import pytest

from quant_agent.domain.research.value_features import VALUE_FEATURES, value_features
from quant_agent.domain.strategies.value import score_longterm


def metrics():
    return {
        "roe_avg3": 16.0,
        "roe_min3": 12.0,
        "roe_std3": 2.0,
        "rev_yoy": 15.0,
        "profit_yoy": 12.0,
        "ocf_to_profit": 1.2,
        "eps": 2.0,
        "pe_ttm": 10.0,
        "pb_pct": 0.2,
        "debt_ratio": 45.0,
        "current_ratio": 1.8,
        "gross_margin": 30.0,
        "n_annual": 3,
    }


def test_missing_values_are_not_neutral_quality_or_zero_growth():
    frame = value_features({})
    assert set(frame) == set(VALUE_FEATURES)
    assert frame["vf_growth"] is None
    assert frame["vf_earnings_yield"] is None
    score = score_longterm({}, {})
    assert score["value_evidence"]["status"] == "insufficient_data"
    assert score["value_evidence"]["observed_dimensions"] == 0
    assert score["value_evidence"]["warnings"]


def test_continuous_ratios_and_quality_value_interactions():
    m = metrics()
    out = value_features(m)
    assert out["vf_earnings_yield"] == pytest.approx(0.1)
    assert out["vf_roe"] == pytest.approx(0.16)
    assert out["vf_quality_value"] == pytest.approx(0.016)
    assert out["vf_cash_value"] == pytest.approx(0.12)
    assert out["vf_growth"] == pytest.approx(0.15)
    m["rev_cagr3"] = 0.0
    assert value_features(m)["vf_growth"] == 0  # Zero is known, not a missing value.
    assert score_longterm(metrics(), {})["value_evidence"]["status"] == "reviewable"


def test_financial_neutral_scores_are_not_observed_cashflow_evidence():
    m = metrics() | {"gross_margin": None, "debt_ratio": 92.0}
    score = score_longterm(m, {})
    assert score["v_cashflow"] == 60.0
    assert score["value_evidence"]["observed_dimensions"] == 3
    assert value_features(m)["vf_cash_conversion"] is None
    assert value_features(m)["vf_leverage"] is None
    assert "financial_sector_metrics_required" in score["value_evidence"]["warnings"]


def test_negative_profit_and_negative_cashflow_are_not_quality():
    m = metrics() | {"eps": -2.0, "ocf_to_profit": 1.5, "pe_ttm": -10.0}
    out = value_features(m)
    assert out["vf_cash_conversion"] is None
    assert out["vf_earnings_yield"] is None
    evidence = score_longterm(m, {})["value_evidence"]
    assert evidence["status"] == "needs_review"
    assert "nonpositive_earnings" in evidence["warnings"]


def test_nonfinite_financials_stay_missing():
    out = value_features(metrics() | {"pe_ttm": np.inf, "roe_avg3": np.nan})
    assert out["vf_earnings_yield"] is None
    assert out["vf_quality_value"] is None
    assert not np.isinf(pd.Series(out, dtype=float)).any()


def evaluation_panel():
    rng = np.random.default_rng(42)
    records = []
    for day in pd.date_range("2024-01-01", periods=40, freq="10D"):
        for code in range(10):
            signal = rng.normal()
            record = {
                "date": str(day.date()),
                "code": str(code),
                "label_end_ret_20": str((day + pd.Timedelta(days=3)).date()),
                "score": rng.normal(),
                "v_quality": rng.normal(),
                "ret_20": signal * 0.1 + rng.normal(0, 0.001),
                **{name: rng.normal() for name in VALUE_FEATURES},
            }
            record["vf_earnings_yield"] = signal
            records.append(record)
    frame = pd.DataFrame(records)
    days = sorted(frame.date.unique())
    options = dict(
        features=["v_quality"],
        kinds=("ridge", "ridge_enriched"),
        min_train_periods=3,
        min_train_rows=20,
        min_oof_periods=3,
        n_perm=0,
        boundaries={
            "validation_start": days[20],
            "test_start": days[30],
            "test_end": str((pd.Timestamp(days[-1]) + pd.Timedelta(days=4)).date()),
        },
    )
    return frame, options


def test_value_feature_ablation_is_paired_and_test_labels_do_not_select(monkeypatch):
    from quant_agent.domain.research import ml_eval

    frame, options = evaluation_panel()
    predicted = []
    original = ml_eval._fit_predict

    def capture(*args):
        result = original(*args)
        predicted.append(result.copy())
        return result

    monkeypatch.setattr(ml_eval, "_fit_predict", capture)
    first = ml_eval.evaluate_against_rule(frame, "ret_20", **options)
    first_predictions = predicted[-1]
    assert first["best"] == "ridge_enriched"
    assert first["feature_ablation"]["accepted_for_test"]
    assert first["feature_ablation"]["mean_ic_gain"] > 0.1
    assert not first["gate"]["passed"]
    frame.loc[frame.date >= options["boundaries"]["test_start"], "ret_20"] *= -1
    changed = ml_eval.evaluate_against_rule(frame, "ret_20", **options)
    assert first["feature_ablation"] == changed["feature_ablation"]
    assert first["selection_candidates"] == changed["selection_candidates"]
    np.testing.assert_array_equal(first_predictions, predicted[-1])


def test_old_replay_panels_skip_enrichment_without_invented_features():
    from quant_agent.domain.research import ml_eval

    frame, options = evaluation_panel()
    result = ml_eval.evaluate_against_rule(
        frame.drop(columns=list(VALUE_FEATURES)), "ret_20", **options
    )
    assert "ridge_enriched" in result["skipped_candidates"]
    assert result["best"] == "ridge"


def test_constant_added_features_cannot_claim_gain_from_baseline_missingness():
    from quant_agent.domain.research import ml_eval

    _, options = evaluation_panel()
    rng = np.random.default_rng(41)
    records = []
    for day in pd.date_range("2024-01-01", periods=40, freq="10D"):
        for code in range(20):
            x = rng.normal()
            missing = code % 2 == 0
            records.append(
                {
                    "date": str(day.date()),
                    "code": str(code),
                    "label_end_ret_20": str((day + pd.Timedelta(days=3)).date()),
                    "score": rng.normal(),
                    "v_quality": np.nan if missing else x,
                    "ret_20": 0.01 * x + 0.3 * missing + rng.normal(0, 0.005),
                    **{name: 0.0 for name in VALUE_FEATURES},
                }
            )
    result = ml_eval.evaluate_against_rule(pd.DataFrame(records), "ret_20", **options)
    assert not result["feature_ablation"]["accepted_for_test"]
    assert result["feature_ablation"]["mean_ic_gain"] == pytest.approx(0)
    assert result["best"] == "ridge"


def test_feature_ablation_requires_same_model_control():
    from quant_agent.domain.research import ml_eval

    frame, options = evaluation_panel()
    options["kinds"] = ("gbr", "ridge_enriched")
    result = ml_eval.evaluate_against_rule(frame, "ret_20", **options)
    assert "ridge_enriched" in result["skipped_candidates"]
    assert "ridge_enriched" not in result["selection_candidates"]


def test_feature_gain_cannot_override_better_original_model(monkeypatch):
    from quant_agent.domain.research import ml_eval

    frame, options = evaluation_panel()
    options["kinds"] = ("ridge", "gbr", "ridge_enriched")
    original = ml_eval._fit_predict

    def predict(model, xtr, ytr, xte):
        if model.__class__.__name__ == "HistGradientBoostingRegressor":
            # A controlled stronger comparator; production code still only sees predictions.
            return frame.loc[xte.index, "ret_20"].to_numpy()
        return original(model, xtr, ytr, xte)

    monkeypatch.setattr(ml_eval, "_fit_predict", predict)
    result = ml_eval.evaluate_against_rule(frame, "ret_20", **options)
    assert result["feature_ablation"]["baseline"] == "ridge"
    assert result["feature_ablation"]["passed"]
    assert result["model_comparison"]["baseline"] == "gbr"
    assert not result["model_comparison"]["passed"]
    assert not result["feature_ablation"]["accepted_for_test"]
    assert result["best"] == "gbr"


def test_validation_extremes_do_not_change_fitted_preprocessing():
    from quant_agent.domain.research import ml_eval

    train = pd.DataFrame({"v_quality": [1.0, np.nan, 3.0, 5.0], "vf_growth": [np.nan] * 4})
    target = pd.Series([0.1, -0.1, 0.3, 0.5])
    validation = pd.DataFrame({"v_quality": [np.nan, 2.0], "vf_growth": [np.nan, 0.0]})
    extreme = pd.concat(
        [validation, pd.DataFrame({"v_quality": [1e10], "vf_growth": [1e10]})],
        ignore_index=True,
    )
    for kind in ("ridge", "ridge_enriched"):
        first, second = ml_eval._model(kind), ml_eval._model(kind)
        expected = ml_eval._fit_predict(first, train, target, validation)
        actual = ml_eval._fit_predict(second, train, target, extreme)
        np.testing.assert_allclose(expected, actual[: len(validation)])
        np.testing.assert_array_equal(
            first.named_steps["simpleimputer"].statistics_,
            second.named_steps["simpleimputer"].statistics_,
        )
        np.testing.assert_array_equal(
            first.named_steps["standardscaler"].mean_,
            second.named_steps["standardscaler"].mean_,
        )


def test_replay_value_features_ignore_future_disclosures():
    from quant_agent.application.research.value_tracking import replay_score

    reports = [
        {
            "REPORT_DATE": f"{year}-12-31",
            "NOTICE_DATE": f"{year + 1}-03-30",
            "REPORT_TYPE": "年报",
            "ROEJQ": 15,
            "XSMLL": 30,
            "BPS": 10,
            "EPSJB": 2,
            "TOTALOPERATEREVETZ": 10,
            "PARENTNETPROFITTZ": 10,
            "MGJYXJJE": 2.5,
            "ZCFZL": 40,
            "LD": 1.8,
        }
        for year in (2019, 2020, 2021, 2022)
    ]
    raw = pd.DataFrame({"date": pd.bdate_range("2022-01-03", periods=400), "close": 20.0})
    first = replay_score(reports, raw, "2023-07-01")
    future = reports[-1] | {"REPORT_DATE": "2023-06-30", "NOTICE_DATE": "2023-07-01", "ROEJQ": 999}
    second = replay_score(reports + [future], raw, "2023-07-01")
    assert first is not None
    assert first == second
    assert all(name in first for name in VALUE_FEATURES)
    revised = reports[-1] | {"NOTICE_DATE": "2023-07-01", "EPSJB": 100, "ROEJQ": 999}
    assert replay_score(reports + [revised], raw, "2023-07-01") == first
    assert replay_score(reports + [revised], raw, "2023-07-02")["vf_roe"] != first["vf_roe"]


def test_replay_cache_rejects_old_financial_semantics(tmp_path, monkeypatch):
    import json

    from quant_agent.application.investing import workflow
    from quant_agent.domain.research.value_features import VALUE_FEATURE_VERSION

    monkeypatch.setattr(workflow, "ROOT", str(tmp_path))
    state = tmp_path / "state"
    state.mkdir()
    cache = {"temporal_test_end": "2025-01-01", "panel": [{"vf_growth": 0.1}]}
    path = state / "longterm_replay.json"
    path.write_text(json.dumps(cache | {"value_feature_version": VALUE_FEATURE_VERSION - 1}))
    assert workflow._load_replay_panel("2025-01-01") is None
    path.write_text(json.dumps(cache | {"value_feature_version": VALUE_FEATURE_VERSION}))
    assert workflow._load_replay_panel("2025-01-01").iloc[0].vf_growth == 0.1
    assert workflow._load_replay_panel("2024-12-31") is None


def test_ttm_eps_uses_matching_cumulative_periods_not_growth_extrapolation():
    from quant_agent.infrastructure.market_data.fundamentals import derived_metrics

    reports = [
        {
            "REPORT_DATE": "2025-03-31",
            "REPORT_TYPE": "一季报",
            "EPSJB": 1.2,
            "PARENTNETPROFITTZ": 100.0,
        },
        {"REPORT_DATE": "2024-12-31", "REPORT_TYPE": "年报", "EPSJB": 4.0},
        {"REPORT_DATE": "2024-03-31", "REPORT_TYPE": "一季报", "EPSJB": 1.0},
    ]
    assert derived_metrics(reports)["eps_ttm"] == pytest.approx(4.2)
    assert derived_metrics(reports[:2])["eps_ttm"] is None
    reports[0]["EPSJB"] = None
    assert derived_metrics(reports)["eps_ttm"] is None


def test_ttm_eps_uses_latest_disclosed_revision_for_same_period():
    from quant_agent.infrastructure.market_data.fundamentals import derived_metrics

    reports = [
        {
            "REPORT_DATE": "2024-12-31 00:00:00",
            "REPORT_TYPE": "年报",
            "EPSJB": 4.0,
            "NOTICE_DATE": "2025-03-20",
        },
        {
            "REPORT_DATE": "2024-12-31",
            "REPORT_TYPE": "年报",
            "EPSJB": 3.5,
            "NOTICE_DATE": "2025-04-20",
        },
    ]
    assert derived_metrics(reports)["eps_ttm"] == pytest.approx(3.5)


def test_calendar_year_metrics_exclude_interim_reports_and_missing_year_proxies():
    from quant_agent.infrastructure.market_data.fundamentals import derived_metrics

    reports = [
        {
            "REPORT_DATE": f"{year}-12-31",
            "REPORT_TYPE": "年报",
            "ROEJQ": roe,
            "TOTALOPERATEREVE": revenue,
            "PARENTNETPROFIT": revenue,
        }
        for year, roe, revenue in [(2018, 90, 10), (2019, 80, 20), (2020, 70, 40), (2024, 10, 80)]
    ]
    reports.append({"REPORT_DATE": "2024-06-30", "REPORT_TYPE": "半年报", "ROEJQ": 999})
    result = derived_metrics(reports)
    assert result["n_annual"] == 4
    assert result["roe_avg3"] == 10
    assert result.get("rev_cagr3") is None
    assert result.get("profit_cagr3") is None
    reports.append(
        {
            "REPORT_DATE": "2021-12-31",
            "REPORT_TYPE": "年报",
            "TOTALOPERATEREVE": 10,
            "PARENTNETPROFIT": 10,
        }
    )
    assert derived_metrics(reports)["rev_cagr3"] == pytest.approx(1.0)
