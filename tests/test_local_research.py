"""Local data, frozen snapshots and daily evidence contracts.

@author caiyanliangbj
"""

import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]

from quant_agent.application.research.daily import completed_date, research_daily  # noqa: E402
from quant_agent.infrastructure.persistence.local import LocalData  # noqa: E402
from quant_agent.infrastructure.persistence.snapshots import (  # noqa: E402
    publish_snapshot,
    read_snapshot,
    sync_snapshot,
)  # noqa: E402


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "bars.db"
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE kline (
            market TEXT, code TEXT, adjust TEXT, date TEXT,
            open REAL, high REAL, low REAL, close REAL, volume REAL, amount REAL,
            src TEXT, fetched_at TEXT, PRIMARY KEY(market,code,adjust,date))""")
    return path


def insert(db, code="000001", market="cn", adjust="qfq", dates=None, scale=1):
    dates = dates if dates is not None else pd.bdate_range("2026-10-01", periods=15)
    rng = np.random.default_rng(sum(map(ord, code)))
    closes = scale * (10 + np.cumsum(rng.normal(0.01, 0.08, len(dates))))
    with sqlite3.connect(db) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO kline VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    market,
                    code,
                    adjust,
                    str(d)[:10],
                    float(c),
                    float(c + 0.2),
                    float(c - 0.2),
                    float(c + 0.03),
                    100.0,
                    1000.0,
                    "fixture",
                    "2026-10-26 18:00:00",
                )
                for d, c in zip(dates, closes)
            ],
        )


def test_batch_projection_dates_and_adjustment(db):
    insert(db)
    insert(db, "000002")
    insert(db, adjust="raw", scale=2)
    result = LocalData(db).get_data(
        "cn",
        ["000001", "000002", "missing"],
        start="2026-10-05",
        end="2026-10-09",
        fields=["date", "close"],
    )
    assert list(result) == ["000001", "000002", "missing"]
    assert list(result["000001"].columns) == ["date", "close"]
    assert len(result["000001"]) == 5
    assert result["missing"].empty
    raw = LocalData(db).get_data("cn", ["000001"], adjust="raw")["000001"]
    qfq = LocalData(db).get_data("cn", ["000001"])["000001"]
    assert raw.close.iloc[0] > qfq.close.iloc[0] * 1.9


@pytest.mark.parametrize(
    "kwargs",
    [
        {"fields": ["close; DROP TABLE kline"]},
        {"frequency": "1m"},
        {"adjust": "hfq"},
        {"limit": 0},
        {"start": "bad"},
        {"start": "2026-10-20", "end": "2026-10-01"},
    ],
)
def test_query_rejects_invalid_contract(db, kwargs):
    with pytest.raises(ValueError):
        LocalData(db).get_data("cn", ["000001"], **kwargs)


@pytest.mark.parametrize("frequency", ["1w", "1M"])
def test_aggregation_is_cutoff_safe_and_uses_actual_date(db, frequency):
    insert(db)
    reader = LocalData(db)
    daily = reader.get_data("cn", ["000001"], end="2026-10-07")["000001"]
    bars = reader.get_data("cn", ["000001"], end="2026-10-07", frequency=frequency)["000001"]
    last = bars.iloc[-1]
    assert last.date == pd.Timestamp("2026-10-07")
    subset = daily if frequency == "1M" else daily[daily.date >= "2026-10-05"]
    assert last.open == subset.open.iloc[0]
    assert last.close == subset.close.iloc[-1]
    assert last.high == subset.high.max()
    assert last.low == subset.low.min()
    assert last.volume == subset.volume.sum()


def test_limit_is_per_symbol_and_sql_is_parameterized(db):
    for code in ["000001", "000002", "x' OR 1=1 --"]:
        insert(db, code)
    result = LocalData(db).get_data("cn", ["000001", "x' OR 1=1 --"], limit=2)
    assert len(result) == 2
    assert all(len(frame) == 2 for frame in result.values())
    assert result["000001"].date.iloc[-1] == pd.Timestamp("2026-10-21")


def snapshot_frames(db):
    return {("cn", c, "qfq"): f for c, f in LocalData(db).get_data("cn", ["000001"]).items()}


def test_snapshots_are_content_addressed_and_sync_offline(db, tmp_path):
    insert(db)
    frames = snapshot_frames(db)
    first = publish_snapshot(frames, tmp_path / "snapshots")
    again = publish_snapshot(frames, tmp_path / "snapshots")
    assert first == again
    restored = read_snapshot(first)
    pd.testing.assert_frame_equal(
        restored[("cn", "000001", "qfq")],
        frames[("cn", "000001", "qfq")],
        check_dtype=False,
    )
    target = sync_snapshot(first, tmp_path / "mirror")
    assert target.name == first.name
    assert sync_snapshot(first, tmp_path / "mirror") == target
    assert not list((tmp_path / "mirror").glob("*.part*"))
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE kline SET close=close+0.01 WHERE date='2026-10-01'")
    revised = publish_snapshot(snapshot_frames(db), tmp_path / "snapshots")
    assert revised != first
    assert (
        read_snapshot(first)[("cn", "000001", "qfq")].close.iloc[0]
        != read_snapshot(revised)[("cn", "000001", "qfq")].close.iloc[0]
    )


def test_corrupt_snapshot_is_rejected_without_touching_destination(db, tmp_path):
    insert(db)
    source = publish_snapshot(snapshot_frames(db), tmp_path / "source")
    target = sync_snapshot(source, tmp_path / "target")
    old_bytes = (target / "bars.json").read_bytes()
    (source / "bars.json").write_text("broken")
    with pytest.raises(ValueError, match="integrity"):
        sync_snapshot(source, tmp_path / "target")
    assert (target / "bars.json").read_bytes() == old_bytes


@pytest.mark.parametrize("unsafe", ["../outside", "/tmp/outside", "sub/bars.json"])
def test_snapshot_manifest_paths_are_allowlisted(db, tmp_path, unsafe):
    insert(db)
    source = publish_snapshot(snapshot_frames(db), tmp_path / "source")
    manifest = json.loads((source / "manifest.json").read_text())
    manifest["files"][0]["path"] = unsafe
    (source / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        read_snapshot(source)


def test_query_never_creates_missing_database(tmp_path):
    missing = tmp_path / "missing.db"
    with pytest.raises(FileNotFoundError):
        LocalData(missing).get_data("cn", ["000001"])
    assert not missing.exists()


def test_intraday_completed_cutoff():
    tz = ZoneInfo("Asia/Shanghai")
    assert completed_date(datetime(2026, 10, 26, 15, 30, tzinfo=tz)) == "2026-10-25"
    assert completed_date(datetime(2026, 10, 26, 18, 30, tzinfo=tz)) == "2026-10-26"


def test_daily_ledger_reuses_input_and_detects_revision(db, tmp_path):
    insert(db)
    params = json.loads((ROOT / "params/params.json").read_text())
    universe = {"cn": ["000001", "missing"]}
    first = research_daily(db, universe, params, tmp_path / "research", asof="2026-10-26")
    again = research_daily(db, universe, params, tmp_path / "research", asof="2026-10-27")
    assert first["run_id"] == again["run_id"]
    assert again["reused"] is True
    assert first["promotion_allowed"] is False
    assert "cn:missing" in first["quality"]["missing"]
    assert first["status"] == "insufficient_evidence"
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE kline SET close=close+0.01 WHERE date='2026-10-01'")
    revision = research_daily(db, universe, params, tmp_path / "research", asof="2026-10-26")
    assert revision["run_id"] != first["run_id"]
    assert revision["change"] == "historical_revision"
    assert revision["evidence_days"] == first["evidence_days"] == 1
    assert json.loads((ROOT / "params/params.json").read_text()) == params


def test_daily_uses_frozen_valid_data_and_records_metrics(db, tmp_path):
    dates = pd.bdate_range(end="2026-10-26", periods=130)
    codes = [f"{i:06d}" for i in range(1, 7)]
    for code in codes:
        insert(db, code, dates=dates)
    params = json.loads((ROOT / "params/params.json").read_text())
    rec = research_daily(db, {"cn": codes}, params, tmp_path / "research", asof="2026-10-26")
    assert rec["status"] == "observed"
    assert rec["metrics"]["factors"]["score"]["rank_ic"]["n"] > 3
    assert rec["promotion_allowed"] is False
    assert rec["evaluation_kind"] == "historical_descriptive"
    assert Path(rec["report_path"]).is_file()
    assert Path(rec["snapshot_path"]).is_dir()
    assert rec["data_date"] == "2026-10-26"


def test_invalid_ohlcv_is_not_silently_used(db, tmp_path):
    insert(db)
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE kline SET high=0 WHERE date='2026-10-01'")
    params = json.loads((ROOT / "params/params.json").read_text())
    rec = research_daily(db, {"cn": ["000001"]}, params, tmp_path / "research", asof="2026-10-26")
    assert rec["status"] == "data_blocked"
    assert rec["quality"]["invalid"] == ["cn:000001"]
    assert rec["promotion_allowed"] is False


def test_tencent_current_endpoint_fallback_and_extended_amount(monkeypatch):
    import quant_agent.infrastructure.market_data.fetcher as fetcher

    calls = []

    class Response:
        def json(self):
            return {
                "data": {
                    "sh510300": {
                        "qfqday": [
                            [
                                "2026-10-23",
                                "4.1",
                                "4.2",
                                "4.3",
                                "4.0",
                                "123",
                                {},
                                "0.6",
                                "51.66",
                                "",
                            ]
                        ]
                    }
                }
            }

    def get(url, params, **kwargs):
        calls.append(url)
        if "web.ifzq" in url:
            raise RuntimeError("legacy endpoint HTTP 501")
        return Response()

    monkeypatch.setattr(fetcher, "_get", get)
    frame = fetcher.fetch_kline("510300", "etf", use_db=False)
    assert len(calls) == 2
    assert "proxy.finance.qq.com" in calls[-1]
    assert frame.close.iloc[-1] == 4.2
    assert frame.amount.iloc[-1] == pytest.approx(516600.0)
    assert frame.volume.iloc[-1] == 123.0  # preserve the existing volume convention
    assert frame.attrs["source"] == "tencent_proxy_ifzq_qfq"


def test_tencent_legacy_numeric_amount_does_not_drop_bar(monkeypatch):
    import quant_agent.infrastructure.market_data.fetcher as fetcher

    class Response:
        def json(self):
            return {
                "data": {
                    "sh600519": {"qfqday": [["2026-10-23", "10", "11", "12", "9", "100", 1234.0]]}
                }
            }

    monkeypatch.setattr(fetcher, "_get", lambda *a, **kw: Response())
    frame = fetcher.fetch_kline("600519", "cn", use_db=False)
    assert len(frame) == 1
    assert frame.amount.iloc[0] == 1234.0


def test_query_cli_outputs_parseable_json_and_preserves_real_exit_code(db, tmp_path):
    insert(db)
    env = dict(os.environ, QUANT_DB=str(db))
    proc = subprocess.run(
        [
            sys.executable,
            str(ROOT / "run.py"),
            "--data-query",
            "--market",
            "cn",
            "--codes",
            "000001",
            "--bars",
            "2",
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert len(json.loads(proc.stdout)["cn:000001"]) == 2
    failed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "run.py"),
            "--daily",
            "--offline",
            "--market",
            "cn",
            "--codes",
            "missing",
            "--research-dir",
            str(tmp_path / "research"),
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert failed.returncode == 2
    assert '"status": "data_blocked"' in failed.stdout


def test_offline_research_does_not_network(db, tmp_path, monkeypatch):
    import socket

    import requests

    def forbidden(*args, **kwargs):
        raise AssertionError("offline research attempted a network request")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    insert(db)
    params = json.loads((ROOT / "params/params.json").read_text())
    research_daily(db, {"cn": ["000001"]}, params, tmp_path / "research", asof="2026-10-26")


def test_sync_storage_failure_is_reported(db, monkeypatch):
    import quant_agent.infrastructure.market_data.fetcher as fetcher
    import quant_agent.infrastructure.persistence.store as store
    from quant_agent.entrypoints.research import synchronize

    insert(db)
    frame = LocalData(db).get_data("cn", ["000001"])["000001"]
    monkeypatch.setattr(store, "enabled", lambda: True)
    monkeypatch.setattr(store, "connect", lambda: object())
    monkeypatch.setattr(store, "kline_is_fresh", lambda *a, **kw: False)
    monkeypatch.setattr(store, "upsert_kline", lambda *a, **kw: 0)
    monkeypatch.setattr(fetcher, "fetch_kline", lambda *a, **kw: frame)
    result = synchronize({"cn": ["000001"]}, 100, 10)
    assert result["failed"] == 1
    assert result["series"][0]["reason"] == "storage_write_failed_or_partial"


def test_expired_data_is_not_reused_as_current_evidence(db, tmp_path):
    insert(db)
    params = json.loads((ROOT / "params/params.json").read_text())
    output = tmp_path / "research"
    rec = research_daily(db, {"cn": ["000001"]}, params, output, asof="2026-10-26")
    stale = research_daily(db, {"cn": ["000001"]}, params, output, asof="2026-11-10")
    assert rec["data_date"] is not None
    assert stale["status"] == "data_blocked"
    assert stale["data_date"] is None
    assert stale["quality"]["stale"] == ["cn:000001"]
    assert stale["evidence_days"] == rec["evidence_days"]


def test_snapshot_preserves_float_precision(db, tmp_path):
    insert(db)
    frames = snapshot_frames(db)
    frames[("cn", "000001", "qfq")].loc[0, "close"] = 10.123456789012345
    path = publish_snapshot(frames, tmp_path / "snapshots")
    restored = read_snapshot(path)
    assert restored[("cn", "000001", "qfq")].close.iloc[0] == 10.123456789012345


def test_repeat_skips_evaluator_and_preserves_original_parameters(db, tmp_path, monkeypatch):
    import quant_agent.application.research.daily as daily_research

    insert(db)
    params = json.loads((ROOT / "params/params.json").read_text())
    args = (db, {"cn": ["000001"]}, params, tmp_path / "research")
    first = research_daily(*args, asof="2026-10-26")

    def must_not_recompute(*args, **kwargs):
        raise AssertionError("same data/parameters/code must reuse evaluation")

    monkeypatch.setattr(daily_research, "rolling_factor_ic", must_not_recompute)
    second = research_daily(*args, asof="2026-10-26")
    assert second["reused"] is True
    assert second["run_id"] == first["run_id"]
    assert second["parameters"] == params
    assert second["runtime"]["pandas"] == pd.__version__


def test_older_source_data_does_not_increase_evidence_days(db, tmp_path):
    insert(db)
    params = json.loads((ROOT / "params/params.json").read_text())
    args = (db, {"cn": ["000001"]}, params, tmp_path / "research")
    first = research_daily(*args, asof="2026-10-26")
    older = research_daily(*args, asof="2026-10-19")
    assert older["change"] == "data_regression"
    assert older["status"] == "data_blocked"
    assert older["evidence_days"] == first["evidence_days"]
    changed_params = dict(params, version="another-observation")
    again = research_daily(
        db, {"cn": ["000001"]}, changed_params, tmp_path / "research", asof="2026-10-26"
    )
    assert again["evidence_days"] == first["evidence_days"]


def test_daily_report_contains_symbol_data_from_completed_snapshot(db, tmp_path):
    dates = pd.bdate_range(end="2026-10-26", periods=130)
    insert(db, "000001", dates=dates)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE kline SET open=999,close=999,high=1000,low=998 WHERE date='2026-10-26'"
        )
    params = json.loads((ROOT / "params/params.json").read_text())
    rec = research_daily(db, {"cn": ["000001"]}, params, tmp_path / "research", asof="2026-10-23")
    observed = rec["observations"][0]
    source = LocalData(db).get_data("cn", ["000001"], end="2026-10-23")["000001"]
    assert observed["date"] == "2026-10-23"
    assert observed["close"] == source.close.iloc[-1]
    assert observed["change_pct"] == pytest.approx(
        (source.close.iloc[-1] / source.close.iloc[-2] - 1) * 100
    )
    assert observed["return_5d_pct"] == pytest.approx(
        (source.close.iloc[-1] / source.close.iloc[-6] - 1) * 100
    )
    assert observed["return_20d_pct"] == pytest.approx(
        (source.close.iloc[-1] / source.close.iloc[-21] - 1) * 100
    )
    assert observed["score"] is not None
    report = Path(rec["report_path"]).read_text()
    for label in (
        "逐标的行情",
        "收盘价",
        "近5日",
        "近20日",
        "RSI",
        "量比",
        "观察池评分",
        "000001",
    ):
        assert label in report
    assert f"{observed['close']:.3f}" in report
    assert "999.000" not in report
    assert "行情交易日：2026-10-23" in report
    assert "行情覆盖" in report and "129" in report
    assert Path(rec["daily_report_path"]).is_file()
    assert (tmp_path / "research" / "latest.md").read_text() == Path(
        rec["daily_report_path"]
    ).read_text()


def test_short_history_report_shows_prices_but_does_not_invent_scores(db, tmp_path):
    insert(db)
    params = json.loads((ROOT / "params/params.json").read_text())
    rec = research_daily(
        db,
        {"cn": ["000001", "missing"]},
        params,
        tmp_path / "research",
        asof="2026-10-26",
    )
    observed = rec["observations"][0]
    assert observed["close"] > 0
    assert observed["score"] is None
    assert observed["return_20d_pct"] is None
    assert observed["at_score_threshold"] is False
    report = Path(rec["report_path"]).read_text()
    assert "预热不足" in report
    assert "cn:missing" in report
    assert "nan" not in report.lower()
    assert "None" not in report


def test_names_and_per_symbol_explanations_are_wired_to_daily_report(db, tmp_path):
    dates = pd.bdate_range(end="2026-10-26", periods=130)
    insert(db, "000001", dates=dates)
    params = json.loads((ROOT / "params/params.json").read_text())
    metadata = {
        "source": "unit:verified",
        "verified_on": "2026-10-26",
        "names": {"cn": {"000001": "平安银行"}},
    }
    rec = research_daily(
        db,
        {"cn": ["000001"]},
        params,
        tmp_path / "research",
        asof="2026-10-26",
        instrument_metadata=metadata,
    )
    row = rec["observations"][0]
    assert row["name"] == "平安银行"
    assert row["name_source"] == "unit:verified"
    assert len(row["analysis"]["factors"]) == 6
    for factor in row["analysis"]["factors"]:
        assert factor["weight"] == params["weights"][factor["key"]]
        assert factor["points"] == row["factors"]["f_" + factor["key"]]
        assert factor["contribution"] == pytest.approx(factor["weight"] * factor["points"])
        assert factor["basis"]
    total = sum(f["contribution"] for f in row["analysis"]["factors"])
    assert total == pytest.approx(row["score"], abs=0.11)
    report = Path(rec["daily_details_path"]).read_text()
    for phrase in (
        "名称",
        "平安银行",
        "逐标的评分依据与研究结论",
        "加权贡献",
        "研究结论",
        "MA60",
        "MACD",
    ):
        assert phrase in report
    assert rec["instrument_metadata"] == metadata


def test_name_revision_changes_record_without_inventing_new_trading_day(db, tmp_path):
    insert(db)
    params = json.loads((ROOT / "params/params.json").read_text())
    args = (db, {"cn": ["000001"]}, params, tmp_path / "research")
    first = research_daily(*args, asof="2026-10-26")
    second = research_daily(
        *args,
        asof="2026-10-26",
        instrument_metadata={
            "source": "unit",
            "verified_on": "2026-10-26",
            "names": {"cn": {"000001": "平安银行"}},
        },
    )
    assert first["observations"][0]["name"] == "名称待核实"
    assert second["run_id"] != first["run_id"]
    assert second["evidence_days"] == first["evidence_days"] == 1
    assert second["observations"][0]["analysis"]["status"] == "insufficient_history"


def test_interpretation_distinguishes_volume_rally_from_selloff():
    from quant_agent.infrastructure.reporting.research import explain_observation

    params = json.loads((ROOT / "params/params.json").read_text())
    row = {
        "score": 71.9,
        "rsi": 27.5,
        "volume_ratio": 1.7,
        "boll_pb": 0.195,
        "close": 4.178,
        "ma60": 4.5,
        "macd_hist": -0.01,
        "macd_hist_prev": -0.02,
        "change_pct": 1.73,
        "return_20d_pct": -7.81,
        "factors": {
            "f_boll": 71.8,
            "f_rsi": 100.0,
            "f_volume": 20.0,
            "f_trend": 50.0,
            "f_macd": 100.0,
            "f_drawdown": 100.0,
        },
    }
    analysis = explain_observation(row, params)
    assert analysis["status"] == "threshold_met"
    text = " ".join(analysis["risks"]) + analysis["conclusion"]
    assert "放量上涨" in text
    assert "放量下跌" not in text
    assert "负柱收窄" in text and "尚未转正" in text
    low = dict(row, score=33.1)
    assert explain_observation(low, params)["status"] == "below_threshold"
    assert "基本面" in explain_observation(low, params)["conclusion"]


def test_main_report_and_details_are_separate_and_cross_linked(db, tmp_path):
    dates = pd.bdate_range(end="2026-10-26", periods=130)
    for code in ("000001", "000002"):
        insert(db, code, dates=dates)
    params = json.loads((ROOT / "params/params.json").read_text())
    rec = research_daily(
        db,
        {"cn": ["000001", "000002"]},
        params,
        tmp_path / "research",
        asof="2026-10-26",
    )
    main = Path(rec["daily_report_path"]).read_text()
    details = Path(rec["daily_details_path"]).read_text()
    assert main.index("## 结论汇总") < main.index("## 逐标的行情")
    for label in ("研究优先级", "关注理由", "主要风险", "后续确认信号"):
        assert label in main
    assert "| 因子 | 指标依据" not in main
    assert "逐标的评分依据与研究结论" not in main
    assert "逐标的评分依据与研究结论" in details
    assert details.count("研究结论：") == 2
    assert str(rec["daily_details_path"]) in main
    assert str(rec["daily_report_path"]) in details
    assert (tmp_path / "research" / "latest.details.md").read_text() == details
    assert Path(rec["details_path"]).is_file()


def test_summary_priority_is_existing_score_rank_with_no_forced_candidates():
    from quant_agent.infrastructure.reporting.research import build_research_summary

    observations = []
    for market in ("cn", "etf"):
        for i, score in enumerate((70, 90, 80, 50)):
            observations.append(
                {
                    "market": market,
                    "code": f"{i:06d}",
                    "name": f"标的{i}",
                    "score": score,
                    "at_score_threshold": score >= 62,
                    "rsi": 30,
                    "boll_pb": 0.2,
                    "volume_ratio": 1.7,
                    "ma60": 4.5,
                    "close": 4.1,
                    "high": 4.2,
                    "low": 4.0,
                    "macd_hist": -0.01,
                    "return_20d_pct": -7,
                    "analysis": {
                        "risks": ["收盘价低于MA60", "量比偏高"],
                        "drivers": [],
                    },
                }
            )
    record = {"status": "observed", "observations": observations}
    result = build_research_summary(record)
    for market in ("cn", "etf"):
        selected = result["groups"][market]
        assert [r["code"] for r in selected] == ["000001", "000002", "000000"]
        assert [r["priority"] for r in selected] == [1, 2, 3]
        assert selected[0]["score"] == 90
        assert selected[0]["reason"] and selected[0]["risks"]
        signals = " ".join(selected[0]["confirmation_signals"])
        assert "4.200" in signals and "4.000" in signals
        assert "后续" in signals and "尚待" in signals
    for row in observations:
        row["at_score_threshold"] = False
    assert all(not g for g in build_research_summary(record)["groups"].values())
    for row in observations:
        row["at_score_threshold"] = True
    record["status"] = "data_blocked"
    assert all(not g for g in build_research_summary(record)["groups"].values())
