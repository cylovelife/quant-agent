"""Regressions for financial availability and durable research tracking.

@author cylovelife
"""

import copy
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import fundamentals as fd  # noqa: E402
import portfolio  # noqa: E402
import value_track as vt  # noqa: E402


def test_quarter_only_financials_use_latest_observed_metrics():
    result = fd.derived_metrics(
        [
            {
                "REPORT_DATE": "2026-09-30",
                "REPORT_TYPE": "三季报",
                "ROEJQ": 8.0,
                "EPSJB": 1.0,
                "XSMLL": 25.0,
            }
        ]
    )
    assert result["n_annual"] == 0
    assert result["roe_avg3"] == 8.0
    assert result["gross_margin_avg3"] == 25.0


def test_pb_does_not_use_a_report_until_after_its_actual_disclosure():
    dates = pd.bdate_range("2026-01-01", "2026-07-31")
    prices = pd.DataFrame({"date": dates, "close": 100.0})
    rows = [
        {"REPORT_DATE": "2025-03-31", "NOTICE_DATE": "2025-04-30", "BPS": 10},
        {"REPORT_DATE": "2025-06-30", "NOTICE_DATE": "2025-08-30", "BPS": 10},
        {"REPORT_DATE": "2026-03-31", "NOTICE_DATE": "2026-07-31", "BPS": 20},
    ]
    result = fd.valuation_percentile(prices, rows)
    assert result["pb_min"] == result["pb_max"] == 10.0
    assert result["pb_pct"] == 1.0
    next_day = pd.DataFrame({"date": [pd.Timestamp("2026-08-03")], "close": [100.0]})
    assert fd.valuation_percentile(pd.concat([prices, next_day]), rows)["pb_min"] == 5.0


def test_pb_excludes_missing_and_proxy_disclosure_dates():
    prices = pd.DataFrame(
        {"date": pd.bdate_range("2026-01-01", periods=100), "close": 100.0}
    )
    rows = [
        {"REPORT_DATE": "2025-03-31", "BPS": 10},
        {
            "REPORT_DATE": "2025-06-30",
            "NOTICE_DATE": "2025-08-31",
            "BPS": 10,
            "_notice_source": "statutory_deadline",
        },
        {"REPORT_DATE": "2025-09-30", "NOTICE_DATE": "invalid", "BPS": 10},
    ]
    assert fd.valuation_percentile(prices, rows)["pb_pct"] is None


def test_pb_uses_latest_report_period_when_an_older_report_is_redisclosed():
    prices = pd.DataFrame(
        {"date": pd.bdate_range("2026-01-01", periods=100), "close": 100.0}
    )
    rows = [
        {"REPORT_DATE": "2024-12-31", "NOTICE_DATE": "2025-04-30", "BPS": 10},
        {"REPORT_DATE": "2025-06-30", "NOTICE_DATE": "2025-08-30", "BPS": 20},
        {"REPORT_DATE": "2024-12-31", "NOTICE_DATE": "2026-02-01", "BPS": 5},
    ]
    result = fd.valuation_percentile(prices, rows)
    assert result["pb_min"] == result["pb_max"] == 5.0


@pytest.fixture
def tracking_files(tmp_path, monkeypatch):
    monkeypatch.setattr(vt, "STATE", str(tmp_path))
    monkeypatch.setattr(vt, "OUTCOMES", str(tmp_path / "outcomes.csv"))
    (tmp_path / "longterm_picks_2026-10-19.json").write_text(
        json.dumps(
            {
                "date": "2026-10-19",
                "stocks": [{"code": "000001", "market": "cn", "name": "fixture"}],
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


def test_long_tracking_is_idempotent_for_leading_zero_codes(tracking_files):
    bars = pd.DataFrame(
        {"date": pd.to_datetime(["2026-10-19", "2026-10-20"]), "close": [100, 110]}
    )
    cfg = {"longterm": {"track_milestones": [1]}}
    assert vt.settle_tracking(lambda *_: bars, cfg, "2026-10-27")["new_rows"] == 1
    again = vt.settle_tracking(lambda *_: bars, cfg, "2026-10-27")
    assert again["new_rows"] == 0
    assert again["summary"][1]["n"] == 1


def test_long_tracking_benchmark_uses_the_same_observed_dates(tracking_files):
    stock = pd.DataFrame(
        {"date": pd.to_datetime(["2026-10-19", "2026-10-21"]), "close": [100, 110]}
    )
    bench = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-10-19", "2026-10-20", "2026-10-21"]),
            "close": [100, 120, 130],
        }
    )
    result = vt.settle_tracking(
        lambda _, m: bench if m == "etf" else stock,
        {"longterm": {"track_milestones": [1]}},
        "2026-10-27",
    )
    assert result["summary"][1]["avg_excess"] == -20.0


def test_long_tracking_does_not_settle_a_future_milestone(tracking_files):
    bars = pd.DataFrame(
        {"date": pd.to_datetime(["2026-10-19", "2026-10-21"]), "close": [100, 110]}
    )
    result = vt.settle_tracking(
        lambda *_: bars, {"longterm": {"track_milestones": [1]}}, "2026-10-20"
    )
    assert result["new_rows"] == 0


@pytest.mark.parametrize(
    "damage",
    [
        "utf8",
        "parse",
        "missing_column",
        "extra_field",
        "missing_code",
        "bad_milestone",
        "bad_return",
    ],
)
def test_invalid_long_tracking_history_is_not_ignored_or_appended(
    tracking_files, damage
):
    path = tracking_files / "outcomes.csv"
    bars = pd.DataFrame(
        {"date": pd.to_datetime(["2026-10-19", "2026-10-20"]), "close": [100, 110]}
    )
    vt.settle_tracking(
        lambda *_: bars, {"longterm": {"track_milestones": [1]}}, "2026-10-27"
    )
    header, row = path.read_text(encoding="utf-8").splitlines()
    fields, values = header.split(","), row.split(",")
    if damage == "utf8":
        content = b"\xff\xfe"
    elif damage == "parse":
        content = (header + '\n"unterminated').encode()
    else:
        if damage == "missing_column":
            index = fields.index("market")
            del fields[index]
            del values[index]
        elif damage == "extra_field":
            values.append("extra")
        else:
            field = {
                "missing_code": "code",
                "bad_milestone": "milestone",
                "bad_return": "ret",
            }[damage]
            values[fields.index(field)] = "" if damage == "missing_code" else "bad"
        content = (",".join(fields) + "\n" + ",".join(values) + "\n").encode()
    path.write_bytes(content)
    with pytest.raises((ValueError, UnicodeError)):
        vt.settle_tracking(
            lambda *_: bars, {"longterm": {"track_milestones": [1]}}, "2026-10-27"
        )
    assert path.read_bytes() == content


def test_duplicate_long_tracking_history_cannot_be_counted_as_new_evidence(
    tracking_files,
):
    path = tracking_files / "outcomes.csv"
    bars = pd.DataFrame(
        {"date": pd.to_datetime(["2026-10-19", "2026-10-20"]), "close": [100, 110]}
    )
    vt.settle_tracking(
        lambda *_: bars, {"longterm": {"track_milestones": [1]}}, "2026-10-27"
    )
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    content = "".join(lines + lines[1:])
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        vt.settle_tracking(
            lambda *_: bars, {"longterm": {"track_milestones": [1]}}, "2026-10-27"
        )
    assert path.read_text(encoding="utf-8") == content


@pytest.mark.parametrize(
    "content", ['{"positions": [', "{}", '{"positions": {}}', '{"positions": [1]}']
)
def test_corrupt_or_incomplete_book_cannot_become_an_empty_account(tmp_path, content):
    path = tmp_path / "positions.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises((ValueError, TypeError)):
        portfolio.load_book(str(path))
    assert path.read_text(encoding="utf-8") == content


def test_missing_book_can_start_empty(tmp_path):
    assert portfolio.load_book(str(tmp_path / "missing.json"))["positions"] == []


def test_failed_book_serialization_preserves_the_prior_file(tmp_path):
    path = tmp_path / "positions.json"
    original = '{"positions": [{"code": "000001", "status": "open"}]}'
    path.write_text(original, encoding="utf-8")
    with pytest.raises(TypeError):
        portfolio.save_book({"positions": [], "bad": object()}, str(path))
    assert path.read_text(encoding="utf-8") == original


def test_replaying_after_a_crash_does_not_duplicate_completed_trades(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(portfolio, "TRADES_PATH", str(tmp_path / "trades.csv"))
    pending = {"positions": []}
    portfolio.add_positions(
        pending,
        {"cn": [{"code": "000001", "score": 70}]},
        {"backtest": {"hold_days": 1}},
        "2026-10-19",
    )
    bars = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-10-19", "2026-10-20"]),
            "open": [100, 100],
            "close": [100, 110],
            "high": [100, 110],
            "low": [100, 100],
            "volume": [1, 1],
        }
    )
    for _ in range(2):
        portfolio.settle_book(
            copy.deepcopy(pending),
            {},
            {"cn": ["2026-10-19", "2026-10-20"]},
            {},
            "2026-10-20",
            kline_fn=lambda *_: bars,
        )
    assert len(pd.read_csv(tmp_path / "trades_v2.csv")) == 1


def _completed_trade():
    from trade_semantics import LABEL_VERSION

    return {
        "market": "cn",
        "code": "000001",
        "signal_date": "2026-10-19",
        "label_id": "fixture-label",
        "label_version": LABEL_VERSION,
        "exit_ret": 0.1,
    }


def test_completed_trade_conflicts_preserve_existing_history(tmp_path):
    path = str(tmp_path / "trades.csv")
    trade = _completed_trade()
    portfolio._append_trades([trade], path)
    target = tmp_path / "trades_v2.csv"
    before = target.read_bytes()
    with pytest.raises(ValueError, match="conflicts"):
        portfolio._append_trades([{**trade, "exit_ret": 0.2}], path)
    assert target.read_bytes() == before


@pytest.mark.parametrize("content", ["", "wrong,header\n1,2\n"])
def test_invalid_existing_trades_cannot_be_overwritten(tmp_path, content):
    target = tmp_path / "trades_v2.csv"
    target.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        portfolio._append_trades([_completed_trade()], str(tmp_path / "trades.csv"))
    assert target.read_text(encoding="utf-8") == content


def test_failed_atomic_trade_publication_preserves_existing_history(
    tmp_path, monkeypatch
):
    path = str(tmp_path / "trades.csv")
    trade = _completed_trade()
    portfolio._append_trades([trade], path)
    target = tmp_path / "trades_v2.csv"
    before = target.read_bytes()

    def fail_replace(*_):
        raise OSError("fixture publication failure")

    monkeypatch.setattr(portfolio.os, "replace", fail_replace)
    with pytest.raises(OSError, match="publication"):
        portfolio._append_trades([{**trade, "signal_date": "2026-10-20"}], path)
    assert target.read_bytes() == before
    assert not list(tmp_path.glob(".portfolio-*"))
