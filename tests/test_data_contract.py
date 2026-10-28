"""Provenance, market-data quality and disclosure-time regression contracts.

@author cylovelife
"""

import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]

import quant_agent.application.investing.evaluation as ev  # noqa: E402
import quant_agent.application.investing.portfolio as portfolio  # noqa: E402
import quant_agent.application.research.value_tracking as vt  # noqa: E402
from quant_agent.application.research.daily import research_daily  # noqa: E402
from quant_agent.infrastructure.persistence.local import LocalData  # noqa: E402
from quant_agent.infrastructure.persistence.snapshots import (  # noqa: E402
    publish_snapshot,
    read_snapshot,
    sync_snapshot,
)  # noqa: E402


@pytest.fixture
def frame():
    dates = pd.bdate_range(end="2026-10-26", periods=100)
    return pd.DataFrame(
        {
            "date": dates,
            "open": 10.0,
            "close": 10.0,
            "high": 10.2,
            "low": 9.8,
            "volume": 100.0,
            "amount": 1000.0,
            "src": "fixture",
        }
    )


@pytest.fixture
def db(tmp_path, frame):
    path = tmp_path / "bars.db"
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE kline (
            market TEXT,code TEXT,adjust TEXT,date TEXT,open REAL,close REAL,
            high REAL,low REAL,volume REAL,amount REAL,src TEXT,fetched_at TEXT,
            PRIMARY KEY(market,code,adjust,date))""")
        conn.executemany(
            "INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    "cn",
                    "000001",
                    "qfq",
                    str(r.date)[:10],
                    r.open,
                    r.close,
                    r.high,
                    r.low,
                    r.volume,
                    r.amount,
                    r.src,
                    "2026-10-26 18:00:00",
                )
                for r in frame.itertuples()
            ],
        )
    return path


def test_query_and_snapshot_preserve_acquisition_without_inventing_publication(db, tmp_path):
    bars = LocalData(db).get_data("cn", ["000001"])["000001"]
    provenance = bars.attrs["data_provenance"]
    assert provenance["observations"][-1]["fetched_at"] == "2026-10-26 18:00:00"
    assert provenance["observations"][-1]["published_at"] is None
    assert provenance["point_in_time_complete"] is False
    path = publish_snapshot({("cn", "000001", "qfq"): bars}, tmp_path / "snapshots")
    restored = read_snapshot(path)[("cn", "000001", "qfq")]
    assert restored.attrs["data_provenance"] == provenance


def test_refetch_is_provenance_update_not_price_revision_or_new_sample(db, tmp_path):
    params = json.loads((ROOT / "params/params.json").read_text())
    args = (db, {"cn": ["000001"]}, params, tmp_path / "research")
    first = research_daily(*args, asof="2026-10-26")
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE kline SET fetched_at='2026-10-26 19:00:00'")
    again = research_daily(*args, asof="2026-10-26")
    assert again["dataset_id"] == first["dataset_id"]
    assert again["snapshot_id"] != first["snapshot_id"]
    assert again["change"] == "provenance_update"
    assert again["evidence_days"] == first["evidence_days"]
    assert (
        read_snapshot(first["snapshot_path"])[("cn", "000001", "qfq")].attrs["data_provenance"][
            "observations"
        ][-1]["fetched_at"]
        == "2026-10-26 18:00:00"
    )


def test_revision_changes_price_version_and_keeps_prior_snapshot(db, tmp_path):
    params = json.loads((ROOT / "params/params.json").read_text())
    args = (
        db,
        {"cn": ["000001", "delisted_or_unknown"]},
        params,
        tmp_path / "research",
    )
    first = research_daily(*args, asof="2026-10-26")
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE kline SET close=10.1 WHERE date='2026-10-23'")
    revised = research_daily(*args, asof="2026-10-26")
    assert revised["dataset_id"] != first["dataset_id"]
    assert revised["evidence_days"] == first["evidence_days"]
    contract = revised["data_contract"]
    assert contract["universe"]["cn"] == ["000001", "delisted_or_unknown"]
    assert contract["historical_membership_complete"] is False
    assert contract["listing_status_coverage"] == "unknown"
    assert read_snapshot(first["snapshot_path"])[("cn", "000001", "qfq")].close.iloc[-2] == 10.0


@pytest.mark.parametrize(
    "defect",
    [
        "duplicate",
        "unsorted",
        "bad-price",
        "bad-range",
        "future",
        "stale",
        "zero-volume",
        "gap",
    ],
)
def test_bad_series_is_blocked_without_guessing_suspension(frame, defect):
    from quant_agent.domain.market.quality import build_data_contract

    broken = frame.copy()
    if defect == "duplicate":
        broken = pd.concat([broken, broken.tail(1)], ignore_index=True)
    elif defect == "unsorted":
        broken = broken.iloc[::-1]
    elif defect == "bad-price":
        broken.loc[99, "close"] = np.nan
    elif defect == "bad-range":
        broken.loc[99, "high"] = 1.0
    elif defect == "future":
        broken.loc[99, "date"] = pd.Timestamp("2026-10-27")
    elif defect == "stale":
        broken = broken.iloc[:-10]
    elif defect == "zero-volume":
        broken.loc[99, "volume"] = 0.0
    elif defect == "gap":
        broken = broken.drop(index=96)
    contract = build_data_contract(
        {("cn", "000001", "qfq"): broken, ("etf", "510300", "qfq"): frame},
        {"cn": ["000001"]},
        "2026-10-26",
        require_provenance=False,
    )
    assert contract["series"]["cn:000001"]["new_signal_allowed"] is False
    assert contract["suspension_status_coverage"] == "unknown"
    assert contract["series"]["cn:000001"]["issues"]


def test_reference_gap_blocks_new_signals_but_does_not_fabricate_holiday(frame):
    from quant_agent.domain.market.quality import build_data_contract

    contract = build_data_contract(
        {("cn", "000001", "qfq"): frame, ("etf", "510300", "qfq"): frame.iloc[:-1]},
        {"cn": ["000001"]},
        "2026-10-26",
        require_provenance=False,
    )
    assert not contract["series"]["cn:000001"]["new_signal_allowed"]
    assert contract["calendar"]["kind"] == "observed_reference_sessions"
    assert contract["calendar"]["official_calendar"] is False


def test_quality_gate_prevents_archiving_bad_new_signal(frame, tmp_path, monkeypatch):
    from quant_agent.domain.market.quality import build_data_contract

    monkeypatch.setattr(ev, "STATE", str(tmp_path))
    bad = frame.copy()
    bad.loc[99, "high"] = 1.0
    contract = build_data_contract(
        {("cn", "000001", "qfq"): bad, ("etf", "510300", "qfq"): frame},
        {"cn": ["000001"]},
        "2026-10-26",
        require_provenance=False,
    )
    path = ev.save_today_picks(
        [{"market": "cn", "code": "000001", "score": 90}],
        {},
        "2026-10-26",
        params={"backtest": {"hold_days": 5}},
        data_context=contract,
    )
    assert path is None
    assert not list(tmp_path.glob("picks_v2_*.json"))


def test_bad_ohlc_preserves_existing_pending_position(frame, tmp_path, monkeypatch):
    monkeypatch.setattr(portfolio, "TRADES_PATH", str(tmp_path / "trades.csv"))
    params = {"backtest": {"hold_days": 1}}
    book = {"positions": []}
    signal_day = str(frame.date.iloc[-2])[:10]
    portfolio.add_positions(book, {"cn": [{"code": "000001", "score": 90}]}, params, signal_day)
    bad = frame.copy()
    bad.loc[99, "high"] = 1.0
    closed = portfolio.settle_book(book, {}, {}, params, "2026-10-26", kline_fn=lambda *a: bad)
    assert not closed
    assert book["positions"][0]["status"] == "pending"
    assert book["positions"][0]["verification_status"] == "unavailable"
    assert book["positions"][0]["entry_price"] is None


@pytest.mark.parametrize(
    "row",
    [
        {"REPORT_DATE": "2026-06-30"},
        {"REPORT_DATE": "2026-06-30", "NOTICE_DATE": "bad"},
        {"REPORT_DATE": "2026-06-30", "NOTICE_DATE": "2026-10-26"},
        {
            "REPORT_DATE": "2026-06-30",
            "NOTICE_DATE": "2026-08-31",
            "_notice_source": "statutory_deadline",
        },
    ],
)
def test_unknown_future_or_proxy_disclosure_is_not_available(row):
    assert vt._asof_row([row], "2026-10-26") == (None, None)
    assert vt._rows_upto([row], "2026-10-26") == []


def test_disclosure_becomes_available_after_its_date():
    row = {"REPORT_DATE": "2026-06-30", "NOTICE_DATE": "2026-10-23 00:00:00"}
    assert vt._asof_row([row], "2026-10-26")[0] == row


@pytest.mark.parametrize("request_adjust,returned_key", [("qfq", "day"), ("raw", "qfqday")])
def test_adapter_does_not_relabel_other_adjustment(
    request_adjust, returned_key, tmp_path, monkeypatch
):
    import quant_agent.infrastructure.market_data.fetcher as fetcher
    import quant_agent.infrastructure.market_data.fundamentals as fundamentals
    import quant_agent.infrastructure.persistence.store as store

    class Response:
        def json(self):
            return {
                "data": {
                    "sh600519": {
                        returned_key: [["2026-10-23", "10", "10", "11", "9", "100", "1000"]]
                    }
                }
            }

    monkeypatch.setattr(fetcher, "_get", lambda *a, **kw: Response())
    monkeypatch.setattr(store, "enabled", lambda: False)
    monkeypatch.setattr(store, "load_kline", lambda *a, **kw: None)
    monkeypatch.setattr(fundamentals, "KLINE_DIR", str(tmp_path))
    result = (
        fetcher.fetch_kline("600519", "cn", use_db=False)
        if request_adjust == "qfq"
        else fundamentals.fetch_kline_raw("600519", "cn")
    )
    assert result.empty


def test_legacy_snapshot_can_still_be_verified_and_mirrored(frame, tmp_path):
    from quant_agent.domain.market.serialization import canonical_json, digest

    rows = frame.copy()
    rows["date"] = rows.date.dt.strftime("%Y-%m-%d")
    payload = {
        "format": 1,
        "series": [
            {
                "market": "cn",
                "code": "000001",
                "adjust": "qfq",
                "bars": rows.to_dict("records"),
            }
        ],
    }
    data = canonical_json(payload)
    identity = digest(data)
    source = tmp_path / identity
    source.mkdir()
    (source / "bars.json").write_bytes(data)
    (source / "manifest.json").write_bytes(
        canonical_json(
            {
                "format": 1,
                "snapshot_id": identity,
                "files": [{"path": "bars.json", "size": len(data), "sha256": identity}],
            }
        )
    )
    assert len(read_snapshot(source)[("cn", "000001", "qfq")]) == 100
    assert sync_snapshot(source, tmp_path / "mirror").name == identity


def test_frozen_contract_keeps_universe_and_invalid_series_decision(frame, tmp_path):
    from quant_agent.application.data_contracts import freeze_data_contract

    frame.attrs.update(source="fixture", fetched_at="2026-10-26T18:00:00+08:00")
    broken = pd.concat([frame, frame.tail(1)], ignore_index=True)
    context = freeze_data_contract(
        {("etf", "510300", "qfq"): frame, ("cn", "000001", "qfq"): broken},
        {"cn": ["000001", "unknown"]},
        "2026-10-26",
        str(tmp_path),
        reference_key=("etf", "510300", "qfq"),
    )
    assert context["series"]["cn:000001"]["new_signal_allowed"] is False
    assert context["excluded_from_snapshot"] == ["cn:000001"]
    assert context["universe"]["cn"] == ["000001", "unknown"]
    assert json.loads((tmp_path / "latest.json").read_text()) == context
    assert (tmp_path / f"check_{context['record_id']}.json").exists()
    assert set(read_snapshot(context["snapshot_path"])) == {("etf", "510300", "qfq")}


def test_held_symbol_remains_usable_after_leaving_candidate_universe(frame):
    from quant_agent.domain.market.quality import build_data_contract

    frame.attrs.update(source="fixture", fetched_at="2026-10-26T18:00:00+08:00")
    context = build_data_contract({("cn", "000001", "qfq"): frame}, {}, "2026-10-26")
    assert context["series"]["cn:000001"]["data_usable"]
    assert not context["series"]["cn:000001"]["new_signal_allowed"]


@pytest.mark.parametrize(
    "metadata",
    [
        {},
        {"source": "fixture", "fetched_at": "unknown"},
        {
            "source": "fixture",
            "fetched_at": "2026-10-26T18:00:00+08:00",
            "adjust": "raw",
        },
    ],
)
def test_incomplete_or_mismatched_provenance_cannot_admit(frame, metadata):
    from quant_agent.domain.market.quality import build_data_contract

    frame = frame.drop(columns="src")
    frame.attrs.update(metadata)
    context = build_data_contract(
        {("cn", "000001", "qfq"): frame}, {"cn": ["000001"]}, "2026-10-26"
    )
    assert not context["series"]["cn:000001"]["new_signal_allowed"]


def test_quality_context_blocks_settlement_without_erasing_existing_fill(
    frame, tmp_path, monkeypatch
):
    monkeypatch.setattr(portfolio, "TRADES_PATH", str(tmp_path / "trades.csv"))
    params = {"backtest": {"hold_days": 5}}
    book = {"positions": []}
    portfolio.add_positions(
        book,
        {"cn": [{"code": "000001", "score": 90}]},
        params,
        str(frame.date.iloc[-3])[:10],
    )
    portfolio.settle_book(
        book,
        {},
        {},
        params,
        str(frame.date.iloc[-2])[:10],
        kline_fn=lambda *a: frame.iloc[:-1],
    )
    saved = dict(book["positions"][0])
    assert saved["status"] == "open"
    closed = portfolio.settle_book(
        book,
        {},
        {},
        params,
        "2026-10-26",
        kline_fn=lambda *a: frame,
        data_context={"series": {}},
    )
    assert not closed
    assert {k: v for k, v in book["positions"][0].items() if k != "verification_status"} == {
        k: v for k, v in saved.items() if k != "verification_status"
    }
    assert book["positions"][0]["verification_status"] == "unavailable"


def test_report_discloses_admission_and_point_in_time_gaps():
    from quant_agent.infrastructure.reporting.investment import data_health_md

    text = data_health_md(
        {
            "_data_contract": {
                "series": {
                    "cn:000001": {
                        "new_signal_allowed": False,
                        "issues": ["invalid_price"],
                    }
                },
                "record_id": "fixture-version",
                "snapshot_id": "fixture-snapshot",
            }
        }
    )
    assert "fixture-version" in text
    assert "invalid_price" in text
    assert "完整历史时点" in text


def test_invalid_ingest_dates_are_not_hidden_by_completed_session_filter(frame):
    from quant_agent.domain.market.quality import build_data_contract, completed_bars

    frame["date"] = frame.date.astype(str)
    frame.loc[99, "date"] = "bad"
    clipped = completed_bars(frame, "2026-10-26")
    context = build_data_contract(
        {("cn", "000001", "qfq"): clipped},
        {"cn": ["000001"]},
        "2026-10-23",
        require_provenance=False,
    )
    assert "invalid_dates" in context["series"]["cn:000001"]["issues"]


def test_missing_explicit_reference_blocks_without_crashing(frame):
    from quant_agent.domain.market.quality import build_data_contract

    context = build_data_contract(
        {("cn", "000001", "qfq"): frame},
        {"cn": ["000001"]},
        "2026-10-26",
        reference_key=("etf", "510300", "qfq"),
    )
    assert not context["series"]["cn:000001"]["new_signal_allowed"]
    assert not context["calendar"]["usable_at_cutoff"]


def test_mature_return_with_missing_provenance_waits_instead_of_entering_training(
    frame, tmp_path, monkeypatch
):
    monkeypatch.setattr(ev, "STATE", str(tmp_path))
    signal = str(frame.date.iloc[-3])[:10]
    ev.save_today_picks(
        [{"market": "cn", "code": "000001", "score": 90}],
        {},
        signal,
        params={"backtest": {"hold_days": 1}},
    )
    result = ev.evaluate_previous(
        None, "2026-10-26", kline_fn=lambda *a: frame, require_data_quality=True
    )
    assert result["count"] == 0
    assert result["unavailable"] == 1
    assert not (tmp_path / "outcomes_v2.csv").exists()
    frame.attrs.update(source="fixture", fetched_at="2026-10-26T18:00:00+08:00")
    settled = ev.evaluate_previous(
        None, "2026-10-26", kline_fn=lambda *a: frame, require_data_quality=True
    )
    assert settled["count"] == 1


def test_snapshot_writer_does_not_sort_or_deduplicate_corrupt_input(frame, tmp_path):
    with pytest.raises(ValueError, match="sorted and unique"):
        publish_snapshot({("cn", "000001", "qfq"): frame.iloc[::-1]}, tmp_path)


def test_adapter_does_not_hide_malformed_rows_or_cache_repaired_input(monkeypatch):
    import quant_agent.infrastructure.market_data.fetcher as fetcher
    import quant_agent.infrastructure.persistence.store as store

    class Response:
        def json(self):
            return {
                "data": {
                    "sh600519": {
                        "qfqday": [
                            ["2026-10-26", "10", "10", "11", "9", "100", "1000"],
                            ["2026-10-23", "bad", "10", "11", "9", "100", "1000"],
                        ]
                    }
                }
            }

    writes = []
    monkeypatch.setattr(fetcher, "_get", lambda *a, **kw: Response())
    monkeypatch.setattr(store, "enabled", lambda: False)
    monkeypatch.setattr(store, "upsert_kline", lambda *a, **kw: writes.append(a))
    result = fetcher.fetch_kline("600519", "cn")
    assert "invalid_source_rows" in result.attrs["ingest_issues"]
    assert not writes


def test_bad_data_context_does_not_backfill_new_pending_records(frame, tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "STATE", str(tmp_path))
    monkeypatch.setattr(portfolio, "TRADES_PATH", str(tmp_path / "trades.csv"))
    params = {"backtest": {"hold_days": 1}}
    path = ev.save_today_picks(
        [{"market": "cn", "code": "000001", "score": 90}],
        {},
        str(frame.date.iloc[-3])[:10],
        params=params,
    )
    book = {"positions": []}
    result = portfolio.backfill_from_picks(
        book,
        path,
        lambda *a: frame,
        params,
        "2026-10-26",
        {},
        data_context={"series": {}},
    )
    assert not book["positions"]
    assert result["skipped"] == 1


def test_old_reference_cannot_make_itself_fresh_by_backdating_signal(frame):
    from quant_agent.domain.market.quality import build_data_contract

    context = build_data_contract(
        {("cn", "000001", "qfq"): frame},
        {"cn": ["000001"]},
        "2026-10-26",
        freshness_asof="2026-11-10",
        require_provenance=False,
    )
    assert "stale" in context["series"]["cn:000001"]["issues"]
    assert not context["series"]["cn:000001"]["new_signal_allowed"]
    recent = build_data_contract(
        {("cn", "000001", "qfq"): frame},
        {"cn": ["000001"]},
        "2026-10-26",
        freshness_asof="2026-10-27",
        require_provenance=False,
    )
    assert recent["series"]["cn:000001"]["data_usable"]
    assert not recent["series"]["cn:000001"]["new_signal_allowed"]
