"""Date grouping, actual label horizons and out-of-time isolation.

@author cylovelife
"""

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from temporal_validation import split_three_way  # noqa: E402


def test_group_by_whole_dates_and_purge_actual_ends():
    rows = []
    for d in pd.date_range("2026-01-01", periods=10):
        for sym in ("a", "b"):
            rows.append({"date": d, "end": d + pd.Timedelta(days=1), "symbol": sym})
    rows[8]["end"] = pd.Timestamp("2026-01-08")
    p = split_three_way(pd.DataFrame(rows), date_col="date", end_col="end")
    assert p.validation_start == "2026-01-07" and p.test_start == "2026-01-09"
    assert not (
        set(p.train.date) & set(p.validation.date)
        or set(p.validation.date) & set(p.test.date)
    )
    assert all(p.train.end < pd.Timestamp(p.validation_start))
    assert all(p.validation.end < pd.Timestamp(p.test_start))
    assert not (
        (p.train.date == pd.Timestamp("2026-01-05")) & (p.train.symbol == "a")
    ).any()


def test_missing_or_invalid_end_never_inferred():
    df = pd.DataFrame({"date": pd.date_range("2026-01-01", periods=5)})
    with pytest.raises(ValueError, match="actual label end"):
        split_three_way(df, date_col="date", end_col="end")
    df["end"] = [None, None, None, None, None]
    with pytest.raises(ValueError, match="three"):
        split_three_way(df, date_col="date", end_col="end")


def test_fixed_boundaries_ignore_future_rows_and_unmatured_test_labels():
    days = pd.date_range("2026-01-01", periods=12)
    df = pd.DataFrame(
        {"date": days, "end": days + pd.Timedelta(days=1), "x": range(12)}
    )
    bounds = {
        "validation_start": "2026-01-05",
        "test_start": "2026-01-08",
        "test_end": "2026-01-10",
    }
    p = split_three_way(df, date_col="date", end_col="end", **bounds)
    assert p.test.x.tolist() == [7, 8]
    changed = df.copy()
    changed.loc[changed.date > pd.Timestamp(bounds["test_end"]), "x"] = 999
    q = split_three_way(changed, date_col="date", end_col="end", **bounds)
    pd.testing.assert_frame_equal(p.train, q.train)
    pd.testing.assert_frame_equal(p.validation, q.validation)
    pd.testing.assert_frame_equal(p.test, q.test)


def test_short_selection_keeps_test_out_of_selection_and_groups_dates(monkeypatch):
    import numpy as np
    import model_select

    dates = pd.date_range("2026-01-01", periods=60)
    signals = []
    for i, d in enumerate(dates):
        for j in range(6):
            signals.append(
                (
                    str(d.date()),
                    10.0,
                    np.array([10 + j * 0.01, 10.2 + j * 0.02]),
                    str((d + pd.Timedelta(days=2)).date()),
                )
            )
    monkeypatch.setattr(
        model_select.em, "_collect_signals", lambda *a, **kw: list(signals)
    )
    cfg = {
        "temporal_validation": {
            "validation_start": "2026-02-05",
            "test_start": "2026-02-20",
            "test_end": "2026-03-05",
        },
        "model_select": {
            "shorts": {"hold_days": [1, 2], "stop_loss": [None], "take_profit": [None]}
        },
    }
    params = {"backtest": {"hold_days": 2, "stop_loss": -0.05, "take_profit": 0.08}}
    first = model_select.select_short(cfg, params, {})
    assert first is not None and not first["switch_recommended"]
    changed = [
        (date, entry, seg * (0.5 if date >= "2026-02-20" else 1), end)
        for date, entry, seg, end in signals
    ]
    monkeypatch.setattr(model_select.em, "_collect_signals", lambda *a, **kw: changed)
    again = model_select.select_short(cfg, params, {})
    assert again["recommend"] == first["recommend"]
    assert again["recommend_stats"] == first["recommend_stats"]
    assert (
        again["folds"][0]["oos_picked"]["avg"] != first["folds"][0]["oos_picked"]["avg"]
    )
    assert model_select.select_short({}, params, {}) is None
    import report

    assert "验证集候选" in report.model_selection_md(first)


def test_exit_threshold_selection_ignores_test_targets(monkeypatch):
    import exit_model

    rows = []
    for i, d in enumerate(pd.date_range("2026-01-01", periods=60)):
        for code in ("a", "b"):
            rows.append(
                {
                    "market": "cn",
                    "code": code,
                    "entry_date": str(d.date()),
                    "label_end_date": str((d + pd.Timedelta(days=2)).date()),
                    "date": str(d.date()),
                    "d": 1,
                    "ret0": (i % 5 - 2) / 100,
                    "ret_expire": (i % 7 - 3) / 100,
                    "score": 50 + i % 10,
                    "mu1": 0.001,
                }
            )
    frame = pd.DataFrame(rows)
    bounds = {
        "validation_start": "2026-02-05",
        "test_start": "2026-02-20",
        "test_end": "2026-03-05",
    }
    first = exit_model.evaluate_exit_policy(frame, 2, min_bucket_n=2, boundaries=bounds)
    assert first is not None
    changed = frame.copy()
    changed.loc[changed.entry_date >= bounds["test_start"], "ret_expire"] = -0.7
    again = exit_model.evaluate_exit_policy(
        changed, 2, min_bucket_n=2, boundaries=bounds
    )
    assert (again["tau"], again["t_min"], again["score_edges"]) == (
        first["tau"],
        first["t_min"],
        first["score_edges"],
    )
    assert again["validation_selection"] == first["validation_selection"]
    assert again["oos"]["baseline"]["avg_ret"] != first["oos"]["baseline"]["avg_ret"]
    assert (
        exit_model.evaluate_exit_policy(
            frame.drop(columns="label_end_date"), 2, boundaries=bounds
        )
        is None
    )
    monkeypatch.setattr(exit_model, "build_exit_panel", lambda *a, **k: frame)
    model = exit_model.fit_exit_model(
        {}, {"backtest": {"hold_days": 2}}, min_bucket_n=2, boundaries=bounds
    )
    monkeypatch.setattr(exit_model, "build_exit_panel", lambda *a, **k: changed)
    altered = exit_model.fit_exit_model(
        {}, {"backtest": {"hold_days": 2}}, min_bucket_n=2, boundaries=bounds
    )
    assert (
        model["states"] == altered["states"]
        and model["score_edges"] == altered["score_edges"]
    )
    assert (model["tau"], model["t_min"]) == (altered["tau"], altered["t_min"])


def test_ml_actual_end_and_test_targets_never_change_selection(monkeypatch):
    import numpy as np
    import ml_eval

    rng = np.random.default_rng(123)
    rows = []
    for i, d in enumerate(pd.date_range("2025-01-01", periods=45, freq="5D")):
        for sym in range(10):
            x = float(rng.normal())
            rows.append(
                {
                    "date": str(d.date()),
                    "label_end_ret_20": str((d + pd.Timedelta(days=2)).date()),
                    "score": float(rng.normal()),
                    "v_quality": x,
                    "ret_20": x + float(rng.normal(0, 0.2)),
                    "code": str(sym),
                }
            )
    panel = pd.DataFrame(rows)
    days = sorted(panel.date.unique())
    bounds = {
        "validation_start": days[25],
        "test_start": days[34],
        "test_end": str((pd.Timestamp(days[-1]) + pd.Timedelta(days=3)).date()),
    }
    kw = dict(
        features=["v_quality"],
        kinds=("ridge",),
        min_train_periods=3,
        min_train_rows=20,
        min_oof_periods=3,
        n_perm=0,
        boundaries=bounds,
    )
    predictions = []
    original = ml_eval._fit_predict

    def spy(*args):
        value = original(*args)
        predictions.append(np.array(value))
        return value

    monkeypatch.setattr(ml_eval, "_fit_predict", spy)
    first = ml_eval.evaluate_against_rule(panel, "ret_20", **kw)
    first_predictions = predictions[-1].copy()
    changed = panel.copy()
    changed.loc[changed.date >= bounds["test_start"], "ret_20"] *= -1
    again = ml_eval.evaluate_against_rule(changed, "ret_20", **kw)
    assert first["best"] == again["best"] == "ridge"
    assert first["selection_candidates"] == again["selection_candidates"]
    assert np.array_equal(first_predictions, predictions[-1])
    assert (
        first["candidates"]["ridge"]["ic_mean"]
        != again["candidates"]["ridge"]["ic_mean"]
    )
    assert not first["gate"]["passed"]
    cutoff = sorted(panel.date.unique())[-3]
    partial = panel[panel.date <= cutoff].copy()
    ml_eval.evaluate_against_rule(partial, "ret_20", **kw)
    assert np.array_equal(first_predictions[: len(predictions[-1])], predictions[-1])
    missing = ml_eval.evaluate_against_rule(
        panel.drop(columns="label_end_ret_20"), "ret_20", **kw
    )
    assert not missing["candidates"] and "标签结束" in missing["verdict"]


def test_walk_forward_uses_actual_end_not_approximate_horizon():
    import numpy as np
    import ml_eval

    rows = []
    for i, day in enumerate(pd.date_range("2026-01-01", periods=12)):
        for symbol in range(8):
            end = day + pd.Timedelta(days=1)
            if i == 0 and symbol == 0:
                end = pd.Timestamp("2026-01-10")
            rows.append(
                {
                    "date": str(day.date()),
                    "label_end_ret_1": str(end.date()),
                    "v_quality": float(symbol),
                    "ret_1": float(symbol) / 10,
                    "code": str(symbol),
                }
            )
    panel = pd.DataFrame(rows)
    changed = panel.copy()
    changed.loc[0, "ret_1"] = 10000
    kwargs = dict(
        features=["v_quality"],
        target="ret_1",
        kind="ridge",
        min_train_periods=2,
        min_train_rows=5,
        embargo_periods=0,
    )
    a = ml_eval.walk_forward_predict(panel, **kwargs)
    b = ml_eval.walk_forward_predict(changed, **kwargs)
    mask = panel.date < "2026-01-10"
    assert a[mask].notna().any()
    assert np.allclose(a[mask], b[mask], equal_nan=True)
    missing = ml_eval.walk_forward_predict(
        panel.drop(columns="label_end_ret_1"), **kwargs
    )
    assert missing.isna().all()


def test_long_selection_does_not_use_test_returns():
    import numpy as np
    import model_select

    rng = np.random.default_rng(4)
    rows = []
    factors = model_select.FACTORS_CORE
    for d in pd.date_range("2025-01-01", periods=30, freq="5D"):
        for i in range(20):
            features = {f: float(rng.uniform(0, 100)) for f in factors}
            rows.append(
                {
                    "date": str(d.date()),
                    "code": str(i),
                    **features,
                    **{
                        f"ret_{n}": features["v_quality"] / 100
                        + float(rng.normal(0, 0.1))
                        for n in (20, 60, 120)
                    },
                    **{
                        f"label_end_ret_{n}": str((d + pd.Timedelta(days=1)).date())
                        for n in (20, 60, 120)
                    },
                }
            )
    panel = pd.DataFrame(rows)
    days = sorted(panel.date.unique())
    cfg = {
        "temporal_validation": {
            "validation_start": days[18],
            "test_start": days[24],
            "test_end": str((pd.Timestamp(days[-1]) + pd.Timedelta(days=2)).date()),
        }
    }
    first = model_select.select_long(panel, cfg)
    assert first is not None and not first["switch_recommended"]
    changed = panel.copy()
    mask = changed.date >= cfg["temporal_validation"]["test_start"]
    for n in (20, 60, 120):
        changed.loc[mask, f"ret_{n}"] *= -1
    again = model_select.select_long(changed, cfg)
    assert first["best"] == again["best"]
    assert first["candidates"] == again["candidates"]
    assert first["test_metrics"] != again["test_metrics"]
    assert (
        model_select.select_long(panel.drop(columns="label_end_ret_120"), cfg) is None
    )


def test_historical_replay_exposes_actual_label_ends(monkeypatch):
    import value_track

    dates = pd.bdate_range("2024-01-01", periods=140)
    bars = pd.DataFrame(
        {"date": dates, "close": [10.0 + i / 10 for i in range(len(dates))]}
    )
    monkeypatch.setattr(
        value_track,
        "replay_score",
        lambda *a, **k: {"score": 60, **{f: 60 for f in value_track.FACTORS_CORE}},
    )
    result = value_track.historical_replay(
        [{"code": "123456", "market": "cn"}],
        lambda *_: bars,
        {"123456": [{"fixture": True}]},
        {},
        {},
        n_periods=1,
        step=1,
        min_history=1,
    )
    rec = result["panel"].iloc[0]
    i = dates.get_loc(pd.Timestamp(rec["date"]))
    assert rec["label_end_ret_120"] == str(dates[i + 120].date())
    cutoff = str(dates[130].date())
    earlier = value_track.historical_replay(
        [{"code": "123456", "market": "cn"}],
        lambda *_: bars.iloc[:131],
        {"123456": [{"fixture": True}]},
        {},
        {},
        n_periods=3,
        step=1,
        min_history=1,
        end_date=cutoff,
    )
    extended = value_track.historical_replay(
        [{"code": "123456", "market": "cn"}],
        lambda *_: bars,
        {"123456": [{"fixture": True}]},
        {},
        {},
        n_periods=3,
        step=1,
        min_history=1,
        end_date=cutoff,
    )
    pd.testing.assert_frame_equal(earlier["panel"], extended["panel"])


def test_missing_unfinished_or_future_boundaries_fail_closed():
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo
    from temporal_validation import configured_boundaries

    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    bounds = {
        "validation_start": str(today - timedelta(days=5)),
        "test_start": str(today - timedelta(days=2)),
        "test_end": str(today),
    }
    assert configured_boundaries({"temporal_validation": bounds}) is None
    assert configured_boundaries({}) is None


def test_short_and_exit_cut_raw_bars_before_feature_generation(monkeypatch):
    import model_select
    import exit_model

    seen = []
    raw = {
        "symbol": pd.DataFrame(
            {"date": ["2026-01-01", "2026-01-09", "2026-02-01"], "close": [1, 2, 999]}
        )
    }
    bounds = {
        "validation_start": "2026-01-04",
        "test_start": "2026-01-06",
        "test_end": "2026-01-10",
    }

    def collect(kline_map, *args, **kwargs):
        seen.append(kline_map["symbol"]["date"].tolist())
        return []

    def build(kline_map, *args, **kwargs):
        seen.append(kline_map["symbol"]["date"].tolist())
        return pd.DataFrame()

    monkeypatch.setattr(model_select.em, "_collect_signals", collect)
    monkeypatch.setattr(exit_model, "build_exit_panel", build)
    assert model_select.select_short({"temporal_validation": bounds}, {}, raw) is None
    assert (
        exit_model.fit_exit_model(
            raw, {"backtest": {"hold_days": 2}}, boundaries=bounds
        )
        is None
    )
    assert seen == [["2026-01-01", "2026-01-09"], ["2026-01-01", "2026-01-09"]]
