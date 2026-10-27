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


def test_volume_rounding_does_not_block_independent_signals(params):
    origin = watch(params)
    frame = bars()
    frame["volume"] += 0.25
    frame = append(append(frame, "2026-10-21", 10.3), "2026-10-22", 10.1, 60)
    result = evaluate_watch(origin, frame, "2026-10-22")
    assert {s["status"] for s in result["signals"].values()} == {"met"}
    review = result["revision_review"]
    assert review["volume"]["classification"] == "rounding_compatible"
    assert review["volume"]["changed_rows"] == 100
    assert review["volume"]["max_absolute_change"] == 0.25
    lower, upper = result["signals"]["volume"]["value_range"]
    assert lower < 0.6 and upper == pytest.approx(0.6)
    assert origin["history"][-1]["volume"] == 100.0  # frozen input is never rewritten


@pytest.mark.parametrize("delta,volume", [(0.25, 40.0), (-0.25, 90.0)])
def test_volume_rounding_at_threshold_is_unknown_only_for_volume(params, delta, volume):
    origin = watch(params)
    frame = bars()
    frame["volume"] += delta
    frame = append(frame, "2026-10-21", 9.8, volume)
    result = evaluate_watch(origin, frame, "2026-10-21")
    assert result["signals"]["volume"]["status"] == "unavailable"
    assert "阈值" in result["signals"]["volume"]["evidence"]
    assert all(
        result["signals"][key]["status"] != "unavailable"
        for key in ("price", "macd", "trend")
    )


def test_material_volume_revision_keeps_price_checks_but_blocks_volume(params):
    origin = watch(params)
    frame = bars()
    frame["volume"] *= 2
    frame = append(append(frame, "2026-10-21", 10.3), "2026-10-22", 10.1, 60)
    result = evaluate_watch(origin, frame, "2026-10-22")
    assert result["signals"]["volume"]["status"] == "unavailable"
    assert result["revision_review"]["volume"]["classification"] == "material_revision"
    assert all(
        result["signals"][key]["status"] == "met" for key in ("price", "macd", "trend")
    )


@pytest.mark.parametrize("delta", [0.50001, 1.0])
def test_volume_rounding_allowance_has_an_absolute_limit(params, delta):
    origin = watch(params)
    frame = bars()
    frame["volume"] += delta
    frame = append(frame, "2026-10-21", 9.8, 60)
    result = evaluate_watch(origin, frame, "2026-10-21")
    assert result["signals"]["volume"]["status"] == "unavailable"
    assert result["signals"]["trend"]["status"] != "unavailable"


def test_fractional_volume_revisions_are_not_assumed_to_be_rounding(params):
    frame = bars()
    frame["volume"] += 0.1
    origin = create_watch(
        {
            "data_date": "2026-10-20",
            "created_at": "2026-10-20T18:30:00+08:00",
            "run_id": "fractional",
            "parameters": params,
        },
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
    frame["volume"] += 0.1
    result = evaluate_watch(origin, append(frame, "2026-10-21", 9.8, 60), "2026-10-21")
    assert result["signals"]["volume"]["status"] == "unavailable"
    assert result["signals"]["price"]["status"] != "unavailable"


@pytest.mark.parametrize("delta", [0.5, 100.0])
def test_volume_revision_cannot_hide_a_known_support_break(params, delta):
    origin = watch(params)
    frame = bars()
    frame["volume"] += delta
    result = evaluate_watch(origin, append(frame, "2026-10-21", 9.5), "2026-10-21")
    assert result["invalidated_on"] == "2026-10-21"
    assert {s["status"] for s in result["signals"].values()} == {"invalidated"}
    assert "低点" in result["note"]
    origin["evaluation"] = result
    recovered = evaluate_watch(origin, append(frame, "2026-10-22", 10.5), "2026-10-22")
    assert recovered["invalidated_on"] == "2026-10-21"


def test_amount_revision_is_audited_without_blocking_unused_inputs(params):
    origin = watch(params)
    frame = bars()
    frame["amount"] *= 2
    result = evaluate_watch(origin, append(frame, "2026-10-21", 10.3), "2026-10-21")
    assert result["signals"]["price"]["status"] == "met"
    assert all(s["status"] != "unavailable" for s in result["signals"].values())
    assert result["revision_review"]["amount"]["changed_rows"] == 100
    assert "成交额" in result["note"]


@pytest.mark.parametrize("field", ["open", "close", "high", "low"])
def test_price_revision_still_blocks_every_signal(params, field):
    origin = watch(params)
    frame = bars()
    frame.loc[20, field] += 0.01
    frame["volume"] += 0.25
    result = evaluate_watch(origin, append(frame, "2026-10-21", 9.5), "2026-10-21")
    assert (
        result["invalidated_on"] is None
    )  # revised prices must not fake a support break
    assert {s["status"] for s in result["signals"].values()} == {"unavailable"}


@pytest.mark.parametrize(
    "volume,status", [(30.0, "pending"), (60.0, "met"), (110.0, "pending")]
)
def test_half_hand_rounding_can_confirm_a_robust_volume_result(params, volume, status):
    origin = watch(params)
    frame = bars()
    frame["volume"] += 0.5
    result = evaluate_watch(
        origin, append(frame, "2026-10-21", 9.8, volume), "2026-10-21"
    )
    assert result["signals"]["volume"]["status"] == status
    assert result["revision_review"]["volume"]["max_absolute_change"] == 0.5


def test_volume_interval_covers_mixed_rounding_not_just_two_endpoint_series(params):
    origin = watch(params)
    frame = bars()
    frame.loc[95:99, "volume"] = [100.5, 99.5, 100.5, 99.5, 100.0]
    # Both complete versions have mean=100 and ratio=0.4. Their per-bar
    # uncertainty envelope straddles 0.4, so comparing only two ratios is unsafe.
    result = evaluate_watch(origin, append(frame, "2026-10-21", 9.8, 40), "2026-10-21")
    signal = result["signals"]["volume"]
    assert signal["status"] == "unavailable"
    lower, upper = signal["value_range"]
    assert lower < 0.4 < upper


def test_unknown_volume_preserves_first_met_date_and_non_pullback_state(params):
    origin = watch(params)
    frame = append(bars(), "2026-10-21", 9.8, 60)
    origin["evaluation"] = evaluate_watch(origin, frame, "2026-10-21")
    assert origin["evaluation"]["signals"]["volume"]["first_met_on"] == "2026-10-21"
    frame.loc[frame.date <= "2026-10-20", "volume"] += 0.25
    frame = append(frame, "2026-10-22", 9.8, 36.85)
    result = evaluate_watch(origin, frame, "2026-10-22")
    signal = result["signals"]["volume"]
    assert signal["status"] == "unavailable"
    assert signal["first_met_on"] == "2026-10-21"
    later = evaluate_watch(origin, append(frame, "2026-10-23", 10.3), "2026-10-23")
    assert later["signals"]["volume"] == signal


def test_zero_denominator_in_volume_interval_stays_unknown(params):
    frame = bars()
    frame["volume"] = 0.0
    origin = create_watch(
        {
            "data_date": "2026-10-20",
            "created_at": "2026-10-20T18:30:00+08:00",
            "run_id": "zero",
            "parameters": params,
        },
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
    frame["volume"] = 0.25
    result = evaluate_watch(origin, append(frame, "2026-10-21", 9.8, 0.1), "2026-10-21")
    assert result["signals"]["volume"]["status"] == "unavailable"
    assert "value_range" not in result["signals"]["volume"]


def test_revision_audit_is_visible_after_json_roundtrip(params):
    from research_report import render_tracking

    origin = watch(params)
    frame = bars()
    frame["volume"] += 0.25
    origin["evaluation"] = evaluate_watch(
        origin, append(frame, "2026-10-21", 9.8, 60), "2026-10-21"
    )
    record = {
        "tracking": {
            "watches": [origin],
            "priority_changes": {"available": False, "note": "no baseline"},
        }
    }
    restored = json.loads(json.dumps(record, sort_keys=True))
    detailed = "\n".join(render_tracking(restored, detailed=True))
    assert "历史输入修订 volume" in detailed
    assert "rounding_compatible" in detailed
    assert "0.250000000" in detailed


def test_daily_revision_rerun_retains_history_and_does_not_increase_evidence(
    tmp_path, monkeypatch, params
):
    db = tmp_path / "bars.db"
    setup_db(db)
    write_bars(db, bars())
    params["score_threshold"] = 0
    freeze_time(monkeypatch, "2026-10-20T18:30:00+08:00")
    args = (db, {"cn": ["000001"]}, params, tmp_path / "research")
    origin = daily_research.research_daily(*args, asof="2026-10-20")
    write_bars(db, append(bars(), "2026-10-21", 10.3))
    freeze_time(monkeypatch, "2026-10-21T18:30:00+08:00")
    first = daily_research.research_daily(*args, asof="2026-10-21")
    previous_report = Path(first["report_path"]).read_bytes()
    revised = bars()
    revised["volume"] += 0.25
    write_bars(db, append(revised, "2026-10-21", 10.3))
    current = daily_research.research_daily(*args, asof="2026-10-21")
    assert current["run_id"] != first["run_id"]
    assert current["evidence_days"] == first["evidence_days"] == 2
    evaluation = current["tracking"]["watches"][0]["evaluation"]
    assert evaluation["signals"]["price"]["status"] == "met"
    assert evaluation["signals"]["price"]["first_met_on"] == "2026-10-21"
    assert (
        current["tracking"]["watches"][0]["watch_id"]
        == origin["tracking"]["watches"][0]["watch_id"]
    )
    assert Path(first["report_path"]).read_bytes() == previous_report
    latest_details = Path(current["daily_details_path"]).read_text()
    assert "历史输入修订 volume" in latest_details
    repeated = daily_research.research_daily(*args, asof="2026-10-21")
    assert repeated["reused"] is True
    assert repeated["tracking"] == current["tracking"]
    with sqlite3.connect(tmp_path / "research/ledger.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM research_watches").fetchone()[0] == 1
        payload = json.loads(
            conn.execute("SELECT payload FROM research_watches").fetchone()[0]
        )
        assert payload["history"][-1]["volume"] == 100.0
