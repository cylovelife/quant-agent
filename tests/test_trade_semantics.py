"""Cross-module signal, fill and label regression cases.

@author cylovelife
"""

import json
import sys
from copy import deepcopy
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import evaluate as ev  # noqa: E402
import portfolio  # noqa: E402
from backtest import backtest_symbol  # noqa: E402
from factor_eval import build_panel, forward_return  # noqa: E402


@pytest.fixture
def params():
    return {
        "weights": {
            "boll": 0.0,
            "rsi": 0.0,
            "volume": 1.0,
            "trend": 0.0,
            "macd": 0.0,
            "drawdown": 0.0,
        },
        "score_threshold": 100.0,
        "backtest": {"hold_days": 5, "stop_loss": -0.5, "take_profit": 0.5},
    }


@pytest.fixture
def bars():
    dates = list(pd.bdate_range(end="2026-09-25", periods=71))
    dates += list(
        pd.to_datetime(
            [
                "2026-09-28",
                "2026-09-29",
                "2026-09-30",
                "2026-10-09",
                "2026-10-12",
                "2026-10-13",
                "2026-10-14",
                "2026-10-15",
                "2026-10-16",
                "2026-10-19",
                "2026-10-20",
                "2026-10-21",
                "2026-10-22",
                "2026-10-23",
                "2026-10-26",
            ]
        )
    )
    close = [100.0] * 71 + [108.0, 112.0, 107.0, 114.0, 121.0] + [500.0] * 10
    volume = [100.0] * len(dates)
    volume[70] = 50.0
    return pd.DataFrame(
        {
            "date": dates,
            "open": [100.0] * 71 + [110.0] * 15,
            "high": np.maximum(close, 110),
            "low": np.minimum(close, 100),
            "close": close,
            "volume": volume,
        }
    )


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "STATE", str(tmp_path))
    monkeypatch.setattr(ev, "PARAMS_DIR", str(tmp_path))
    monkeypatch.setattr(portfolio, "TRADES_PATH", str(tmp_path / "trades.csv"))
    return tmp_path


def quote_must_not_be_used(*args):
    raise AssertionError("a current quote cannot fill a historical signal")


def archive(params, day="2026-09-25", code="000001", market="cn"):
    path = ev.save_today_picks(
        [
            {
                "code": code,
                "name": "fixture",
                "market": market,
                "score": 100,
                "entry_price": 100.0,
            }
        ],
        {},
        data_date=day,
        params=params,
    )
    return json.loads(Path(path).read_text())


@pytest.mark.parametrize("early_stop", [False, True])
def test_same_trade_across_research_backtest_review_and_book(
    params, bars, isolated, early_stop
):
    if early_stop:
        params["backtest"]["stop_loss"] = -0.05
        bars.loc[72, "close"] = 99.0
        bars.loc[72, "low"] = 99.0
    expected = -0.1 if early_stop else 0.1
    expected_end = "2026-09-29" if early_stop else "2026-10-12"
    panel = build_panel({("cn", "000001"): bars}, params, label_spec=params["backtest"])
    signal = panel[panel.date == "2026-09-25"].iloc[0]
    assert signal.fwd_ret == pytest.approx(expected)
    assert signal.entry_date == "2026-09-28"
    assert signal.label_end_date == expected_end
    bt = backtest_symbol(bars, params)
    assert bt["signals"] == 1
    assert bt["avg_ret"] == pytest.approx(expected)
    saved = archive(params)
    book = {"positions": []}
    portfolio.add_positions(book, {"cn": saved["picks"]}, params, "2026-09-25")
    assert book["positions"][0]["status"] == "pending"
    assert book["positions"][0]["entry_price"] is None
    assert not portfolio.open_positions(book)
    cal = portfolio.trading_calendar({("cn", "000001"): bars})

    def kfn(*args):
        return bars

    result = ev.evaluate_previous(quote_must_not_be_used, "2026-10-26", kline_fn=kfn)
    row = result["details"][0]
    assert row["entry_price"] == 110.0
    assert row["ret"] == pytest.approx(expected)
    assert row["label_end_date"] == expected_end
    assert row["signal_time"] == "2026-09-25@close"
    assert row["entry_time"] == "2026-09-28@open"
    assert row["label_end_time"] == expected_end + "@close"
    closed = portfolio.settle_book(book, {}, cal, params, "2026-10-26", kline_fn=kfn)
    assert closed[0]["entry_price"] == row["entry_price"]
    assert closed[0]["exit_ret"] == row["ret"]
    assert closed[0]["exit_date"] == row["label_end_date"]
    assert closed[0]["days_held"] == (2 if early_stop else 5)
    assert not portfolio.settle_book(book, {}, cal, params, "2026-10-26", kline_fn=kfn)
    ev.evaluate_previous(quote_must_not_be_used, "2026-10-26", kline_fn=kfn)
    assert len(pd.read_csv(isolated / "outcomes_v2.csv")) == 1
    assert len(pd.read_csv(isolated / "trades_v2.csv")) == 1


def test_signal_archive_is_frozen_and_keeps_legacy_file(params, isolated):
    old = isolated / "picks_2026-09-25.json"
    old.write_text('{"date":"2026-09-25","picks":[]}')
    original = old.read_bytes()
    first = archive(params)
    assert first["picks"][0]["entry_price"] is None
    assert first["picks"][0]["reference_price"] == 100.0
    assert first["label_spec"] == params["backtest"]
    assert archive(params, code="000002") == first
    assert old.read_bytes() == original


def test_immature_label_is_not_written_and_book_is_open(params, bars, isolated):
    saved = archive(params)
    result = ev.evaluate_previous(
        quote_must_not_be_used, "2026-10-09", kline_fn=lambda *a: bars
    )
    assert result.get("skipped")
    assert not (isolated / "outcomes_v2.csv").exists()
    book = {"positions": []}
    portfolio.add_positions(book, {"cn": saved["picks"]}, params, "2026-09-25")
    cal = portfolio.trading_calendar({("cn", "000001"): bars})
    assert not portfolio.settle_book(
        book, {}, cal, params, "2026-10-09", kline_fn=lambda *a: bars
    )
    pos = portfolio.open_positions(book)[0]
    assert pos["entry_price"] == 110.0
    assert pos["days_held"] == 4
    assert pos["label_end_date"] is None


@pytest.mark.parametrize(
    "problem", ["none", "empty", "missing-signal", "bad-open", "bad-close"]
)
def test_missing_or_bad_bars_do_not_create_labels(params, bars, isolated, problem):
    archive(params)
    if problem == "none":
        frame = None
    elif problem == "empty":
        frame = bars.iloc[:0]
    elif problem == "missing-signal":
        frame = bars.iloc[71:]
    else:
        frame = bars.copy()
        frame.loc[71, "open" if problem == "bad-open" else "close"] = np.nan
    result = ev.evaluate_previous(
        quote_must_not_be_used, "2026-10-26", kline_fn=lambda *a: frame
    )
    assert result.get("skipped")
    assert not (isolated / "outcomes_v2.csv").exists()


def test_missed_runs_settle_all_archives_and_keep_saved_policy(params, bars, isolated):
    first = archive(params)
    archive(params, day="2026-09-28", code="000002")
    params["backtest"]["hold_days"] = 1
    result = ev.evaluate_previous(
        quote_must_not_be_used, "2026-10-26", kline_fn=lambda *a: bars
    )
    assert result["count"] == 2
    original = next(r for r in result["details"] if r["code"] == "000001")
    assert original["label_end_date"] == "2026-10-12"
    assert original["label_id"] == first["label_id"]


def test_unversioned_picks_outcomes_and_positions_are_not_reinterpreted(
    params, bars, isolated
):
    legacy = {
        "date": "2026-09-25",
        "picks": [{"code": "000001", "market": "cn", "entry_price": 100}],
    }
    old = isolated / "picks_2026-09-25.json"
    old.write_text(json.dumps(legacy))
    old_outcome = isolated / "outcomes.csv"
    old_outcome.write_text("date,code,ret\n2026-09-25,000001,0.5\n")
    before = old_outcome.read_bytes()
    assert ev.evaluate_previous(
        quote_must_not_be_used, "2026-10-26", kline_fn=lambda *a: bars
    ).get("skipped")
    assert not ev.update_weights(params, min_samples=1, min_days=1)["updated"]
    book = {
        "positions": [
            {
                "code": "000001",
                "market": "cn",
                "status": "open",
                "entry_date": "2026-09-25",
                "entry_price": 100.0,
                "last_price": 100.0,
                "ret": 0.0,
            }
        ]
    }
    original = deepcopy(book["positions"][0])
    assert not portfolio.settle_book(
        book,
        {("cn", "000001"): {"price": 500}},
        {},
        params,
        "2026-10-26",
        kline_fn=lambda *a: bars,
    )
    for key, value in original.items():
        assert book["positions"][0][key] == value
    assert book["positions"][0]["verification_status"] == "legacy_unverified"
    assert old_outcome.read_bytes() == before


def test_backtest_does_not_expire_at_an_incomplete_tail(params, bars):
    assert backtest_symbol(bars.iloc[:74], params) == {"signals": 0}


def test_forward_return_rejects_nonpositive_fill(bars):
    bars.loc[71, "open"] = 0.0
    assert pd.isna(forward_return(bars, 5).iloc[70])


def test_fixed_horizon_and_strategy_labels_are_explicit(params, bars):
    params["backtest"]["stop_loss"] = -0.05
    bars.loc[72, "close"] = 99.0
    bars.loc[72, "low"] = 99.0
    fixed = build_panel({("cn", "000001"): bars}, params)
    strategy = build_panel(
        {("cn", "000001"): bars}, params, label_spec=params["backtest"]
    )
    a = fixed[fixed.date == "2026-09-25"].iloc[0]
    b = strategy[strategy.date == "2026-09-25"].iloc[0]
    assert a.fwd_ret == pytest.approx(0.1)
    assert b.fwd_ret == pytest.approx(-0.1)
    assert a.label_id != b.label_id


def test_backfill_is_idempotent_and_uses_saved_next_open(params, bars, isolated):
    archive(params)
    path = isolated / "picks_v2_2026-09-25.json"
    book = {"positions": []}
    cal = portfolio.trading_calendar({("cn", "000001"): bars})
    params["backtest"]["hold_days"] = 1
    for _ in range(2):
        portfolio.backfill_from_picks(
            book, str(path), lambda *a: bars, params, "2026-10-26", cal
        )
    assert len(book["positions"]) == 1
    assert book["positions"][0]["entry_price"] == 110.0
    assert book["positions"][0]["exit_date"] == "2026-10-12"


def test_missing_expected_session_is_not_shifted_to_later_entry(params, bars, isolated):
    archive(params)
    calendar = {"cn": bars.date.dt.strftime("%Y-%m-%d").tolist()}
    missing = bars.drop(index=71)
    result = ev.evaluate_previous(
        quote_must_not_be_used,
        "2026-10-26",
        kline_fn=lambda *a: missing,
        calendar_by_market=calendar,
    )
    assert result.get("skipped")
    assert not (isolated / "outcomes_v2.csv").exists()


def test_same_code_on_two_markets_is_not_deduplicated(params, bars, isolated):
    ev.save_today_picks(
        [
            {"code": "000001", "name": "fixture", "market": market}
            for market in ("cn", "etf")
        ],
        {},
        "2026-09-25",
        params=params,
    )
    assert (
        ev.evaluate_previous(
            quote_must_not_be_used, "2026-10-26", kline_fn=lambda *a: bars
        )["count"]
        == 2
    )
    assert len(ev._read_outcomes(str(isolated / "outcomes_v2.csv"))) == 2


def test_quote_only_cannot_close_pending_signal(params, isolated):
    saved = archive(params)
    book = {"positions": []}
    portfolio.add_positions(book, {"cn": saved["picks"]}, params, "2026-09-25")
    assert not portfolio.settle_book(
        book, {("cn", "000001"): {"price": 999}}, {}, params, "2026-10-26"
    )
    assert book["positions"][0]["status"] == "pending"
    assert book["positions"][0]["entry_price"] is None


def test_future_changes_cannot_change_mature_review(params, bars, isolated):
    archive(params)
    frame = bars.copy()
    frame.loc[frame.date > "2026-10-12", "close"] *= 10
    result = ev.evaluate_previous(
        quote_must_not_be_used, "2026-10-12", kline_fn=lambda *a: frame
    )
    row = result["details"][0]
    assert row["ret"] == pytest.approx(0.1)
    frozen = (isolated / "outcomes_v2.csv").read_bytes()
    frame.loc[75, "close"] = 999
    assert ev.evaluate_previous(
        quote_must_not_be_used, "2026-10-26", kline_fn=lambda *a: frame
    ).get("skipped")
    assert (isolated / "outcomes_v2.csv").read_bytes() == frozen


def test_open_position_rejects_revised_fill_or_observed_close(params, bars, isolated):
    saved = archive(params)
    book = {"positions": []}
    portfolio.add_positions(book, {"cn": saved["picks"]}, params, "2026-09-25")
    calendar = portfolio.trading_calendar({("cn", "000001"): bars})
    portfolio.settle_book(
        book, {}, calendar, params, "2026-09-28", kline_fn=lambda *a: bars
    )
    observed = deepcopy(book["positions"][0])
    changed = bars.copy()
    changed.loc[71, "close"] += 1
    assert not portfolio.settle_book(
        book, {}, calendar, params, "2026-10-26", kline_fn=lambda *a: changed
    )
    assert book["positions"][0]["status"] == "open"
    assert book["positions"][0]["entry_price"] == observed["entry_price"]
    assert book["positions"][0]["verification_status"] == "revised_history"


def test_unmatched_target_cannot_update_current_weights(params, isolated):
    from trade_semantics import LABEL_VERSION, LabelSpec

    pd.DataFrame(
        [
            {
                "date": "2026-09-25",
                "market": "cn",
                "code": "000001",
                "ret": 1.0,
                "label_version": LABEL_VERSION,
                "label_id": LabelSpec(1).label_id,
            }
        ]
    ).to_csv(isolated / "outcomes_v2.csv", index=False)
    assert not ev.update_weights(params, min_samples=1, min_days=1)["updated"]


def test_unsupported_market_remains_archived_without_inventing_open(
    params, bars, isolated
):
    saved = archive(params, market="fund")
    result = ev.evaluate_previous(
        quote_must_not_be_used, "2026-10-26", kline_fn=lambda *a: bars
    )
    assert result.get("skipped")
    assert result["unavailable"] == 1
    book = {"positions": []}
    assert not portfolio.add_positions(
        book, {"fund": saved["picks"]}, params, "2026-09-25"
    )


def test_versioned_weights_keep_gates_writeback_and_idempotence(params, isolated):
    from trade_semantics import LABEL_VERSION, LabelSpec

    params["weights"] = {
        "boll": 0.25,
        "rsi": 0.25,
        "volume": 0.15,
        "trend": 0.1,
        "macd": 0.15,
        "drawdown": 0.1,
    }
    spec = LabelSpec.from_params(params)
    rows = []
    for i in range(40):
        factor = 20 + i * 1.5
        rows.append(
            {
                "date": "2026-09-25",
                "code": f"{i:06d}",
                "market": "cn",
                "f_boll": factor,
                "f_rsi": 50,
                "f_volume": 30,
                "f_trend": 40,
                "f_macd": 25,
                "f_drawdown": 15,
                "ret": (factor - 50) / 1000,
                "label_version": LABEL_VERSION,
                "label_id": spec.label_id,
            }
        )
    pd.DataFrame(rows).to_csv(isolated / "outcomes_v2.csv", index=False)
    assert not ev.update_weights(params, min_samples=20, min_days=20)["updated"]
    first = ev.update_weights(params, min_samples=20, min_days=1)
    assert first["updated"]
    assert params["weights"]["boll"] > 0.25
    assert not ev.update_weights(params, min_samples=20, min_days=1)["updated"]
    assert (
        json.loads((isolated / "params.json").read_text())["weights"]
        == params["weights"]
    )


def test_missing_expected_tail_is_unavailable_instead_of_immature(
    params, bars, isolated
):
    archive(params)
    calendar = {"cn": bars.date.dt.strftime("%Y-%m-%d").tolist()}
    result = ev.evaluate_previous(
        quote_must_not_be_used,
        "2026-10-12",
        kline_fn=lambda *a: bars.iloc[:75],
        calendar_by_market=calendar,
    )
    assert result["unavailable"] == 1
    assert result["pending"] == 0


def test_legacy_risk_is_visible_without_becoming_a_new_fill(params, isolated):
    from report import book_overview_md, brief_action_rows

    book = {
        "positions": [
            {
                "code": "000001",
                "name": "legacy",
                "market": "cn",
                "status": "open",
                "entry_date": "2026-09-25",
                "entry_price": 100.0,
                "last_price": 100.0,
                "ret": 0.0,
            }
        ]
    }
    params["backtest"]["stop_loss"] = -0.05
    assert not portfolio.settle_book(
        book,
        {("cn", "000001"): {"price": 90.0, "date": "2026-10-12"}},
        {},
        params,
        "2026-10-12",
    )
    pos = book["positions"][0]
    assert pos["status"] == "open"
    assert pos["last_price"] == 100.0
    assert pos["legacy_risk_review"]["trigger"] == "止损复核"
    text = book_overview_md(book, [])
    assert "旧口径止损复核" in text
    assert "不混入v2收益统计" in text
    assert "平均浮动" not in text
    candidates = {
        "cn": pd.DataFrame([{"code": "000001", "name": "legacy", "score": 90}])
    }
    assert not brief_action_rows(candidates, [], book=book)


def test_report_distinguishes_pending_signal_and_mature_return(params, bars, isolated):
    from report import book_overview_md, eval_md

    saved = archive(params)
    book = {"positions": []}
    portfolio.add_positions(book, {"cn": saved["picks"]}, params, "2026-09-25")
    assert "待入场信号" in book_overview_md(book, [])
    result = ev.evaluate_previous(
        quote_must_not_be_used, "2026-10-26", kline_fn=lambda *a: bars
    )
    rendered = eval_md(result)
    assert "未扣费模拟收益" in rendered
    assert "次日平均收益" not in rendered


def test_intraday_run_can_reuse_but_not_invent_previous_signal(params, isolated):
    assert (
        ev.save_today_picks([], {}, "2026-09-25", params=params, allow_new=False)
        is None
    )
    assert not list(isolated.glob("picks_v2_*.json"))
    archive(params)
    path = ev.save_today_picks([], {}, "2026-09-25", params=params, allow_new=False)
    assert len(json.loads(Path(path).read_text())["picks"]) == 1
