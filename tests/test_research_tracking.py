"""Daily confirmation, rank comparison and persistence contracts.

@author caiyanliangbj
"""

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from research_tracking import create_watch, evaluate_watch, compare_priorities  # noqa: E402
import daily_research  # noqa: E402


@pytest.fixture
def params():
    return json.loads((ROOT / "params/params.json").read_text())


def bars():
    frame = pd.DataFrame(
        {
            "date": pd.bdate_range(end="2026-10-20", periods=100),
            "open": 10.0,
            "close": 10.0,
            "high": 10.2,
            "low": 9.8,
            "volume": 100.0,
            "amount": 1000.0,
            "src": "fixture",
        }
    )
    frame.loc[99, ["open", "close", "high", "low"]] = [9.8, 9.8, 10.0, 9.6]
    return frame


def append(frame, day, close, volume=100.0):
    row = {
        "date": pd.Timestamp(day),
        "open": close,
        "close": close,
        "high": close + 0.1,
        "low": close - 0.1,
        "volume": volume,
        "amount": volume * close,
        "src": "fixture",
    }
    return pd.concat([frame, pd.DataFrame([row])], ignore_index=True)


def watch(params, created_at="2026-10-20T18:30:00+08:00"):
    record = {
        "data_date": "2026-10-20",
        "created_at": created_at,
        "run_id": "origin",
        "parameters": params,
    }
    row = {
        "market": "cn",
        "code": "000001",
        "name": "测试标的",
        "date": "2026-10-20",
        "high": 10.0,
        "low": 9.6,
    }
    return create_watch(record, row, bars(), "scope")


def test_signals_wait_then_confirm_and_record_first_dates(params):
    origin = watch(params)
    initial = evaluate_watch(origin, bars(), "2026-10-20")
    assert {s["status"] for s in initial["signals"].values()} == {"pending"}
    first = append(bars(), "2026-10-21", 10.3)
    result = evaluate_watch(origin, first, "2026-10-21")
    for key in ("price", "macd", "trend"):
        assert result["signals"][key]["status"] == "met"
        assert result["signals"][key]["first_met_on"] == "2026-10-21"
    assert result["signals"]["volume"]["status"] == "pending"
    second = append(first, "2026-10-22", 10.15, volume=60)
    result = evaluate_watch(origin, second, "2026-10-22")
    assert result["signals"]["volume"]["status"] == "met"
    assert result["signals"]["volume"]["first_met_on"] == "2026-10-22"
    assert result["signals"]["price"]["first_met_on"] == "2026-10-21"


def test_condition_reversal_and_support_break_are_distinct(params):
    origin = watch(params)
    frame = append(append(bars(), "2026-10-21", 10.3), "2026-10-22", 10.15, 60)
    frame = append(frame, "2026-10-23", 10.0)
    result = evaluate_watch(origin, frame, "2026-10-23")
    assert result["signals"]["price"]["status"] == "invalidated"
    assert result["signals"]["price"]["first_met_on"] == "2026-10-21"
    assert result["invalidated_on"] is None
    frame = append(frame, "2026-10-26", 9.5)
    broken = evaluate_watch(origin, frame, "2026-10-26")
    assert broken["invalidated_on"] == "2026-10-26"
    assert {s["status"] for s in broken["signals"].values()} == {"invalidated"}
    origin["evaluation"] = broken
    recovered = evaluate_watch(origin, append(frame, "2026-10-27", 10.5), "2026-10-27")
    assert recovered["invalidated_on"] == "2026-10-26"


def test_missing_or_revised_data_is_not_a_false_invalidation(params):
    origin = watch(params)
    assert {
        s["status"]
        for s in evaluate_watch(origin, None, "2026-10-21")["signals"].values()
    } == {"unavailable"}
    frame = append(bars(), "2026-10-21", 10.3)
    frame.loc[20, "close"] += 0.01
    revised = evaluate_watch(origin, frame, "2026-10-21")
    assert "历史" in revised["note"]
    assert {s["status"] for s in revised["signals"].values()} == {"unavailable"}
    gap = append(bars(), "2026-10-22", 10.3)
    incomplete = evaluate_watch(origin, gap, "2026-10-22", ["2026-10-21", "2026-10-22"])
    assert {s["status"] for s in incomplete["signals"].values()} == {"unavailable"}
    assert "中间交易日" in incomplete["note"]


def test_registration_time_blocks_retrospective_confirmation_and_future_bars(params):
    origin = watch(params, "2026-10-22T18:30:00+08:00")
    future = append(append(bars(), "2026-10-21", 10.3), "2026-10-22", 10.4)
    future = append(future, "2026-10-23", 10.5)
    result = evaluate_watch(origin, future, "2026-10-22")
    assert {s["status"] for s in result["signals"].values()} == {"pending"}
    assert (
        evaluate_watch(origin, future, "2026-10-23")["signals"]["price"]["first_met_on"]
        == "2026-10-23"
    )


def session(day, rankings):
    return {
        "data_date": day,
        "created_at": day + "T18:30:00+08:00",
        "params_id": "same",
        "observations": [
            {"market": "cn", "code": code, "name": code, "score": score}
            for code, _, score in rankings
        ],
        "research_summary": {
            "groups": {
                "cn": [
                    {"code": code, "name": code, "priority": rank, "score": score}
                    for code, rank, score in rankings
                    if rank is not None
                ],
                "etf": [],
            }
        },
    }


def test_ranking_changes_use_exact_previous_session_and_handle_missing_data():
    old = session(
        "2026-10-20", [("A", 1, 80), ("B", 2, 70), ("C", 3, 65), ("D", None, 50)]
    )
    now = session(
        "2026-10-21", [("B", 1, 85), ("D", 2, 78), ("A", 3, 75), ("C", None, 50)]
    )
    result = compare_priorities(now, old, "2026-10-20")
    by_code = {r["code"]: r for r in result["rows"]}
    assert by_code["A"]["status"] == "down"
    assert by_code["B"]["status"] == "up"
    assert by_code["D"]["status"] == "entered"
    assert by_code["C"]["status"] == "exited"
    assert by_code["B"]["score_delta"] == 15
    assert compare_priorities(now, None, "2026-10-20")["available"] is False
    # A record before the exact previous session must not masquerade as yesterday.
    assert compare_priorities(now, old, "2026-10-19")["available"] is False
    now["observations"] = [r for r in now["observations"] if r["code"] != "C"]
    assert (
        next(
            r
            for r in compare_priorities(now, old, "2026-10-20")["rows"]
            if r["code"] == "C"
        )["status"]
        == "unavailable"
    )


def setup_db(path):
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE kline (market TEXT,code TEXT,adjust TEXT,date TEXT,
            open REAL,close REAL,high REAL,low REAL,volume REAL,amount REAL,src TEXT,
            PRIMARY KEY(market,code,adjust,date))""")


def write_bars(path, frame, code="000001"):
    with sqlite3.connect(path) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO kline VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    "cn",
                    code,
                    "qfq",
                    str(r["date"])[:10],
                    r["open"],
                    r["close"],
                    r["high"],
                    r["low"],
                    r["volume"],
                    r["amount"],
                    r["src"],
                )
                for r in frame.to_dict("records")
            ],
        )


def freeze_time(monkeypatch, timestamp):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromisoformat(timestamp).astimezone(tz)

    monkeypatch.setattr(daily_research, "datetime", Clock)


def test_daily_tracking_persists_and_is_idempotent(tmp_path, monkeypatch, params):
    db = tmp_path / "bars.db"
    setup_db(db)
    write_bars(db, bars())
    params["score_threshold"] = 0
    freeze_time(monkeypatch, "2026-10-20T18:30:00+08:00")
    args = (db, {"cn": ["000001"]}, params, tmp_path / "research")
    first = daily_research.research_daily(*args, asof="2026-10-20")
    assert first["tracking"]["priority_changes"]["available"] is False
    repeated = daily_research.research_daily(*args, asof="2026-10-20")
    assert repeated["reused"] is True
    with sqlite3.connect(tmp_path / "research/ledger.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM research_watches").fetchone()[0] == 1
        assert (
            conn.execute("SELECT COUNT(*) FROM research_signal_checks").fetchone()[0]
            == 1
        )
    write_bars(db, append(bars(), "2026-10-21", 10.3))
    freeze_time(monkeypatch, "2026-10-21T18:30:00+08:00")
    second = daily_research.research_daily(*args, asof="2026-10-21")
    assert second["tracking"]["priority_changes"]["previous_date"] == "2026-10-20"
    assert (
        second["tracking"]["watches"][0]["evaluation"]["signals"]["price"]["status"]
        == "met"
    )
    assert second["tracking"]["watches"][0]["high"] == 10.0
    assert (
        "10.000"
        in second["research_summary"]["groups"]["cn"][0]["confirmation_signals"][0]
    )
    main = Path(second["daily_report_path"]).read_text()
    assert "确认信号逐日跟踪" in main and "已满足" in main and "上一交易日" in main
    with sqlite3.connect(tmp_path / "research/ledger.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM research_watches").fetchone()[0] == 1
        assert (
            conn.execute("SELECT COUNT(*) FROM research_signal_checks").fetchone()[0]
            == 2
        )


def test_tracking_and_research_writes_roll_back_together(tmp_path, monkeypatch, params):
    db = tmp_path / "bars.db"
    setup_db(db)
    write_bars(db, bars())
    params["score_threshold"] = 0
    original = daily_research.track_research

    def fail_after_tracking(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("interrupted before research insert")

    monkeypatch.setattr(daily_research, "track_research", fail_after_tracking)
    with pytest.raises(RuntimeError, match="interrupted"):
        daily_research.research_daily(
            db, {"cn": ["000001"]}, params, tmp_path / "research", asof="2026-10-20"
        )
    with sqlite3.connect(tmp_path / "research/ledger.db") as conn:
        for table in (
            "research_runs",
            "research_sessions",
            "research_watches",
            "research_signal_checks",
        ):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    monkeypatch.setattr(daily_research, "track_research", original)
    daily_research.research_daily(
        db, {"cn": ["000001"]}, params, tmp_path / "research", asof="2026-10-20"
    )
    with sqlite3.connect(tmp_path / "research/ledger.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM research_watches").fetchone()[0] == 1


def test_signal_columns_do_not_swap_after_json_roundtrip(params):
    from research_report import render_tracking

    origin = watch(params)
    result = evaluate_watch(origin, append(bars(), "2026-10-21", 10.3), "2026-10-21")
    result["signals"]["price"]["status"] = "invalidated"
    result["signals"]["macd"]["status"] = "met"
    origin["evaluation"] = result
    record = {
        "tracking": {
            "watches": [origin],
            "priority_changes": {"available": False, "note": "no baseline"},
        }
    }
    restored = json.loads(json.dumps(record, sort_keys=True))
    row = next(line for line in render_tracking(restored) if "测试标的" in line)
    cells = [cell.strip() for cell in row.split("|")]
    assert cells[3].startswith("已失效")  # price column
    assert cells[4].startswith("已满足")  # MACD column


def test_exit_then_reentry_creates_new_watch_and_retains_old_round(
    tmp_path, monkeypatch, params
):
    db = tmp_path / "bars.db"
    setup_db(db)
    write_bars(db, bars())
    args = (db, {"cn": ["000001"]}, params, tmp_path / "research")
    params["score_threshold"] = 0
    freeze_time(monkeypatch, "2026-10-20T18:30:00+08:00")
    first = daily_research.research_daily(*args, asof="2026-10-20")
    frame = append(bars(), "2026-10-21", 10.3)
    write_bars(db, frame)
    params["score_threshold"] = 101
    freeze_time(monkeypatch, "2026-10-21T18:30:00+08:00")
    exited = daily_research.research_daily(*args, asof="2026-10-21")
    assert exited["tracking"]["watches"][0]["closed_date"] == "2026-10-21"
    write_bars(db, append(frame, "2026-10-22", 10.15, 60))
    params["score_threshold"] = 0
    freeze_time(monkeypatch, "2026-10-22T18:30:00+08:00")
    reentered = daily_research.research_daily(*args, asof="2026-10-22")
    current = reentered["tracking"]["watches"][0]
    assert current["watch_id"] != first["tracking"]["watches"][0]["watch_id"]
    assert current["origin_date"] == "2026-10-22"
    assert current["evaluation"]["signals"]["price"]["status"] == "pending"


def test_lagging_symbol_is_not_compared_or_falsely_closed(
    tmp_path, monkeypatch, params
):
    db = tmp_path / "bars.db"
    setup_db(db)
    for code in ("000001", "000002"):
        write_bars(db, bars(), code)
    params["score_threshold"] = 0
    freeze_time(monkeypatch, "2026-10-20T18:30:00+08:00")
    args = (db, {"cn": ["000001", "000002"]}, params, tmp_path / "research")
    daily_research.research_daily(*args, asof="2026-10-20")
    write_bars(db, append(bars(), "2026-10-21", 10.3), "000002")
    freeze_time(monkeypatch, "2026-10-21T18:30:00+08:00")
    rec = daily_research.research_daily(*args, asof="2026-10-21")
    lagging = next(w for w in rec["tracking"]["watches"] if w["code"] == "000001")
    assert lagging["closed_date"] is None
    assert {s["status"] for s in lagging["evaluation"]["signals"].values()} == {
        "unavailable"
    }
    change = next(
        r for r in rec["tracking"]["priority_changes"]["rows"] if r["code"] == "000001"
    )
    assert change["status"] == "unavailable"
    assert change["score_delta"] is None


@pytest.mark.parametrize(
    "volume,expected", [(40, "met"), (90, "met"), (39.9, "pending"), (90.1, "pending")]
)
def test_pullback_volume_boundary_and_non_applicable_day(params, volume, expected):
    origin = watch(params)
    frame = append(append(bars(), "2026-10-21", 10.3), "2026-10-22", 10.1, volume)
    result = evaluate_watch(origin, frame, "2026-10-22")
    assert result["signals"]["volume"]["status"] == expected
    later = evaluate_watch(origin, append(frame, "2026-10-23", 10.4, 300), "2026-10-23")
    assert later["signals"]["volume"]["status"] == expected
    assert later["signals"]["volume"]["checked_on"] == "2026-10-22"


def test_zero_average_volume_is_unknown_instead_of_failed_confirmation(params):
    frame = bars()
    frame["volume"] = 0.0
    record = {
        "data_date": "2026-10-20",
        "created_at": "2026-10-20T18:30:00+08:00",
        "run_id": "zero-volume",
        "parameters": params,
    }
    origin = create_watch(
        record,
        {
            "market": "cn",
            "code": "000001",
            "name": "测试",
            "date": "2026-10-20",
            "high": 10.0,
            "low": 9.6,
        },
        frame,
        "scope",
    )
    result = evaluate_watch(origin, append(frame, "2026-10-21", 9.8, 100), "2026-10-21")
    assert result["signals"]["volume"]["status"] == "unavailable"
