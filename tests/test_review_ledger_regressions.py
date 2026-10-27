"""Review regressions for immutable daily evidence and acquisition-only retries.

@author cylovelife
"""

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import sqlite3
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import execution  # noqa: E402
import execution_cli  # noqa: E402
import paper_experiment as experiment  # noqa: E402
from paper_daily import settle_day  # noqa: E402
from paper_ledger import LedgerConflict, LedgerCorruption, PaperLedger  # noqa: E402
from test_paper_daily import close, day, order_event  # noqa: E402
from test_paper_experiment import NOW, ZONE, calendar, quote, spec  # noqa: E402
from test_paper_ledger import inputs  # noqa: E402


def _book(tmp_path):
    book = PaperLedger.create(tmp_path / "paper.db")
    policy = execution.load_policy(ROOT / "params/execution_v1.json")
    book.create_account("fixture", "50000.00", "2026-10-26", policy)
    return book


def _evidence(book):
    with sqlite3.connect(book.path) as conn:
        return conn.execute(
            "SELECT account_id,day,request_hash,request_json,result_hash,result_json "
            "FROM paper_daily ORDER BY account_id,day"
        ).fetchall()


def _settled_buy(tmp_path):
    book = _book(tmp_path)
    settle_day(book, "fixture", day("2026-10-26"))
    request = day(
        "2026-10-27",
        "2026-10-26",
        [order_event(execution)],
        [close("2026-10-27", "11.00")],
    )
    result = settle_day(book, "fixture", request)
    return book, request, result


def test_acquisition_only_retry_reads_original_sealed_evidence(tmp_path):
    book, request, result = _settled_buy(tmp_path)
    stored = _evidence(book)
    repeated = deepcopy(request)
    repeated["events"][0]["quote"]["fetched_at"] = "2026-10-27T17:00:00+08:00"
    repeated["events"][0]["quote"]["data_version"] = "same-open-refetched"
    repeated["closes"][0]["data_version"] = "same-close-refetched"
    replay = settle_day(book, "fixture", repeated)
    assert replay == {**result, "replayed": True}
    assert _evidence(book) == stored
    assert book.snapshot("fixture")["order_count"] == 1


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r: r["closes"][0].update(close="12.00"),
        lambda r: r["closes"][0].update(source="different-source"),
        lambda r: r["events"][0]["quote"].update(open_price="10.10"),
        lambda r: r["events"][0]["quote"].update(source="different-source"),
        lambda r: r["events"][0]["quote"].update(adjust="qfq"),
        lambda r: r["events"][0]["quote"].update(fetched_at="invalid-time"),
        lambda r: r["events"][0]["order"].update(quantity=200),
        lambda r: r["events"][0]["order"].update(quantity=100.0),
        lambda r: r["events"][0]["quote"]["facts"].update(opening_capacity=9999),
        lambda r: r.update(previous_day="2026-10-25"),
        lambda r: r["calendar"].update(source="different-calendar"),
        lambda r: r["events"].clear(),
    ],
)
def test_replay_still_rejects_changed_economic_or_business_inputs(tmp_path, mutation):
    book, request, _ = _settled_buy(tmp_path)
    original = _evidence(book)
    repeated = deepcopy(request)
    repeated["events"][0]["quote"]["fetched_at"] = "2026-10-27T17:00:00+08:00"
    repeated["events"][0]["quote"]["data_version"] = "refreshed-open"
    repeated["closes"][0]["data_version"] = "refreshed-close"
    mutation(repeated)
    with pytest.raises(LedgerConflict):
        settle_day(book, "fixture", repeated)
    assert _evidence(book) == original


def test_replay_checks_original_request_hash_before_comparing(tmp_path):
    book, request, _ = _settled_buy(tmp_path)
    with sqlite3.connect(book.path) as conn:
        conn.execute("DROP TRIGGER paper_daily_no_update")
        stored = json.loads(
            conn.execute(
                "SELECT request_json FROM paper_daily WHERE account_id=? AND day=?",
                ("fixture", "2026-10-27"),
            ).fetchone()[0]
        )
        stored["closes"][0]["data_version"] = "corrupted-evidence"
        conn.execute(
            "UPDATE paper_daily SET request_json=? WHERE account_id=? AND day=?",
            (json.dumps(stored), "fixture", "2026-10-27"),
        )
    with pytest.raises(LedgerCorruption, match="request evidence"):
        settle_day(book, "fixture", request)


def _market(path, market, code, day, *, fetched="16:00:00"):
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS kline (market TEXT,code TEXT,adjust TEXT,date TEXT,"
            "open REAL,close REAL,high REAL,low REAL,volume REAL,amount REAL,src TEXT,fetched_at TEXT)"
        )
        conn.execute(
            "INSERT INTO kline VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                market,
                code,
                "raw",
                day,
                10,
                11,
                11,
                10,
                10000,
                100000,
                "fixture",
                f"{day}T{fetched}+08:00",
            ),
        )


def _refetch(path):
    with sqlite3.connect(path) as conn:
        conn.execute(
            "UPDATE kline SET fetched_at=replace(fetched_at,'16:00:00','17:00:00')"
        )


def test_daily_cli_resumes_after_refetch_and_partial_multiday_failure(tmp_path, capsys):
    book = _book(tmp_path)
    market = tmp_path / "market.db"
    _market(market, "cn", "600000", "2026-10-27")
    order, instrument, opening = inputs(execution)
    request = {
        "ledger_path": str(book.path),
        "db_path": str(market),
        "account_id": "fixture",
        "calendar": {"source": "fixture", "sessions": ["2026-10-27", "2026-10-28"]},
        "days": [
            {
                "day": "2026-10-26",
                "previous_day": None,
                "events": [],
                "close_symbols": [],
            },
            {
                "day": "2026-10-27",
                "previous_day": "2026-10-26",
                "events": [
                    {
                        "order": asdict(order),
                        "instrument": asdict(instrument),
                        "session": asdict(opening.facts),
                    }
                ],
                "close_symbols": [{"market": "cn", "code": "600000"}],
            },
            {
                "day": "2026-10-28",
                "previous_day": "2026-10-27",
                "events": [],
                "close_symbols": [{"market": "cn", "code": "600000"}],
            },
        ],
    }
    path = tmp_path / "days.json"
    path.write_text(json.dumps(request))
    assert execution_cli.run_daily(str(path)) == 2
    capsys.readouterr()
    original = _evidence(book)
    assert len(original) == 2
    _refetch(market)
    _market(market, "cn", "600000", "2026-10-28")
    assert execution_cli.run_daily(str(path)) == 0
    result = json.loads(capsys.readouterr().out)
    assert [r["replayed"] for r in result["days"]] == [True, True, False]
    assert _evidence(book)[:2] == original
    assert book.snapshot("fixture")["order_count"] == 1


def test_paired_cli_resumes_after_refetch_and_partial_multiday_failure(
    tmp_path, monkeypatch, capsys
):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            current = datetime(2026, 11, 3, 18, tzinfo=ZONE)
            return current.astimezone(tz) if tz else current.replace(tzinfo=None)

    monkeypatch.setattr(experiment, "datetime", Clock)
    policy = execution.load_policy(ROOT / "params/execution_v1.json")
    experiment.start_experiment(tmp_path / "paper.db", spec(), policy, now=NOW)
    book = PaperLedger(tmp_path / "paper.db")
    market = tmp_path / "market.db"
    _market(market, "etf", "510300", "2026-11-02")
    request = {
        "ledger_path": str(book.path),
        "db_path": str(market),
        "experiment_id": "study-1",
        "calendar": calendar(),
        "days": [
            {
                "day": "2026-11-01",
                "previous_day": None,
                "actions": [],
                "session": None,
                "close_symbols": [],
            },
            {
                "day": "2026-11-02",
                "previous_day": "2026-11-01",
                "actions": [],
                "session": quote()["facts"],
                "close_symbols": [{"market": "etf", "code": "510300"}],
            },
            {
                "day": "2026-11-03",
                "previous_day": "2026-11-02",
                "actions": [],
                "session": None,
                "close_symbols": [{"market": "etf", "code": "510300"}],
            },
        ],
    }
    path = tmp_path / "advance.json"
    path.write_text(json.dumps(request))
    assert execution_cli.run_experiment_advance(str(path)) == 2
    capsys.readouterr()
    original = _evidence(book)
    assert len(original) == 4
    _refetch(market)
    _market(market, "etf", "510300", "2026-11-03")
    assert execution_cli.run_experiment_advance(str(path)) == 0
    result = json.loads(capsys.readouterr().out)
    for side in ("strategy", "benchmark"):
        assert [r[side]["replayed"] for r in result["days"]] == [True, True, False]
    current = _evidence(book)
    assert [row for row in current if row[1] != "2026-11-03"] == original
    assert book.snapshot("strategy")["order_count"] == 1
    assert book.snapshot("benchmark")["order_count"] == 0
