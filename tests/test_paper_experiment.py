"""Frozen prospective experiment, matched cash baseline and atomic settlement.

@author cylovelife
"""

import json
import sqlite3
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from quant_agent.infrastructure.configuration import load_policy

ROOT = Path(__file__).resolve().parents[1]
import quant_agent.application.paper.experiment as exp  # noqa: E402
import quant_agent.domain.trading.execution as execution  # noqa: E402
from quant_agent.application.paper.daily import daily_curve, settle_day  # noqa: E402
from quant_agent.application.paper.ledger import LedgerConflict, PaperLedger  # noqa: E402

ZONE = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 11, 1, 10, tzinfo=ZONE)
CLOSE1 = datetime(2026, 11, 1, 16, tzinfo=ZONE)
CLOSE2 = datetime(2026, 11, 2, 16, tzinfo=ZONE)


def spec():
    return {
        "experiment_id": "study-1",
        "account_id": "strategy",
        "benchmark_id": "benchmark",
        "opening_cash": "50000.00",
        "currency": "CNY",
        "start_date": "2026-11-01",
        "first_trade_date": "2026-11-02",
        "strategy": {
            "kind": "buy_and_hold",
            "market": "etf",
            "code": "510300",
            "exchange": "SSE",
            "profile": "cn_equity_etf",
            "quantity": 100,
        },
        "risk": {"max_order_cash": "2000.00", "min_cash_reserve": "1000.00"},
        "data_sources": {
            "market": "fixture",
            "calendar": "fixture-calendar",
            "corporate_actions": "fixture-action",
        },
        "benchmark": {"kind": "cash", "cashflow_policy": "mirror"},
    }


@pytest.fixture
def policy():
    return load_policy(ROOT / "params/execution_v1.json")


@pytest.fixture
def book(tmp_path, policy):
    path = tmp_path / "experiment.db"
    exp.start_experiment(path, spec(), policy, now=NOW)
    return PaperLedger(path)


def calendar():
    return {
        "source": "fixture-calendar",
        "sessions": ["2026-11-01", "2026-11-02", "2026-11-03"],
    }


def quote():
    facts = execution.SessionFacts(
        "2026-11-02",
        "fixture",
        "2026-11-02@open",
        "trading",
        "regular",
        "9.00",
        "11.00",
        10000,
        "assumed",
        "2026-11-01",
        "etf",
        "510300",
    )
    return asdict(
        execution.OpenQuote(
            "etf",
            "510300",
            "2026-11-02",
            "10.00",
            "raw",
            "fixture",
            "2026-11-02T09:25:00+08:00",
            "fixture-v1",
            facts,
        )
    )


def close(day="2026-11-02"):
    return {
        "market": "etf",
        "code": "510300",
        "date": day,
        "adjust": "raw",
        "close": "11.00",
        "source": "fixture",
        "data_version": "fixture-v1",
    }


def initial(book):
    return exp.settle_pair(book, "study-1", "2026-11-01", None, calendar(), [], [], now=CLOSE1)


def buy(book):
    return exp.settle_pair(
        book,
        "study-1",
        "2026-11-02",
        "2026-11-01",
        calendar(),
        [],
        [close()],
        quote(),
        now=CLOSE2,
    )


def test_registration_atomic_idempotent_and_no_backdated_start(tmp_path, policy, book):
    left = book.snapshot("strategy")
    right = book.snapshot("benchmark")
    assert left["cash"] == right["cash"] == "50000.00"
    assert left["positions"] == right["positions"] == []
    replay = exp.start_experiment(book.path, spec(), policy, now=CLOSE2)
    assert replay["replayed"] and replay["registered_at"] == NOW.isoformat()
    changed = {**spec(), "opening_cash": "60000.00"}
    with pytest.raises(LedgerConflict):
        exp.start_experiment(book.path, changed, policy, now=CLOSE2)
    with pytest.raises(ValueError):
        exp.start_experiment(tmp_path / "past.db", spec(), policy, now=CLOSE2)
    assert not (tmp_path / "past.db").exists()


def test_frozen_strategy_and_cash_benchmark_are_advanced_together(book):
    initial(book)
    result = buy(book)
    assert result["strategy"]["positions"][0]["quantity"] == 100
    assert result["strategy"]["net_assets"] == "50099.40"
    assert result["benchmark"]["net_assets"] == "50000.00"
    assert buy(book)["strategy"]["replayed"]
    comp = exp.compare(book, "study-1")
    assert comp["points"][-1]["difference"] == "99.40"
    assert comp["total_return_status"] == "corporate_actions_unavailable"


def test_risk_violation_rolls_back_both_accounts(tmp_path, policy):
    config = spec()
    config["risk"]["max_order_cash"] = "1000.00"
    path = tmp_path / "risk.db"
    exp.start_experiment(path, config, policy, now=NOW)
    book = PaperLedger(path)
    initial(book)
    with pytest.raises(LedgerConflict, match="reserve|cap"):
        buy(book)
    assert book.snapshot("strategy")["cash"] == book.snapshot("benchmark")["cash"] == "50000.00"
    assert len(daily_curve(book, "strategy")) == len(daily_curve(book, "benchmark")) == 1


def test_single_account_and_code_drift_are_blocked(book, monkeypatch):
    with pytest.raises(LedgerConflict, match="paired"):
        settle_day(
            book,
            "strategy",
            {
                "day": "2026-11-01",
                "previous_day": None,
                "events": [],
                "closes": [],
                "calendar": calendar(),
            },
        )
    with pytest.raises(LedgerConflict, match="paired"):
        book.apply_action(
            "strategy",
            {
                "kind": "deposit",
                "action_id": "bad",
                "date": "2026-11-01",
                "phase": "preopen",
                "source": "fixture",
                "known_at": "2026-11-01@preopen",
                "amount": "1.00",
            },
        )
    monkeypatch.setattr(exp, "code_fingerprints", lambda: {"changed": "bad"})
    with pytest.raises(LedgerConflict, match="code differs"):
        initial(book)


def test_failure_in_benchmark_rolls_back_strategy(book):
    initial(book)
    with sqlite3.connect(book.path) as conn:
        conn.execute(
            "CREATE TRIGGER fixture_failure BEFORE INSERT ON paper_daily WHEN NEW.account_id='benchmark' BEGIN SELECT RAISE(ABORT, 'test'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        buy(book)
    assert book.snapshot("strategy")["positions"] == []
    assert len(daily_curve(book, "strategy")) == len(daily_curve(book, "benchmark")) == 1


def test_flow_is_mirrored_and_first_trade_cannot_be_skipped(book):
    initial(book)
    with pytest.raises(LedgerConflict, match="skipped|first scheduled"):
        exp.settle_pair(
            book,
            "study-1",
            "2026-11-03",
            "2026-11-01",
            calendar(),
            [],
            [],
            now=datetime(2026, 11, 3, 16, tzinfo=ZONE),
        )
    buy(book)
    action = {
        "kind": "deposit",
        "action_id": "in-1",
        "date": "2026-11-03",
        "phase": "close",
        "source": "fixture",
        "known_at": "2026-11-03@close",
        "amount": "100.00",
    }
    got = exp.settle_pair(
        book,
        "study-1",
        "2026-11-03",
        "2026-11-02",
        calendar(),
        [action],
        [close("2026-11-03")],
        now=datetime(2026, 11, 3, 16, tzinfo=ZONE),
    )
    assert got["strategy"]["net_external_flow"] == got["benchmark"]["net_external_flow"] == "100.00"
    assert exp.compare(book, "study-1")["matching_cashflows"]


def test_start_cli_creates_only_declared_test_ledger(tmp_path):
    today = datetime.now(ZONE).date()
    config = spec()
    config["start_date"] = (today + timedelta(days=1)).isoformat()
    config["first_trade_date"] = (today + timedelta(days=2)).isoformat()
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "ledger_path": "paper.db",
                "policy_path": str(ROOT / "params/execution_v1.json"),
                "experiment": config,
            }
        )
    )
    run = subprocess.run(
        [
            sys.executable,
            str(ROOT / "run.py"),
            "--start-paper-experiment",
            str(request),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)["spec"]["opening_cash"] == "50000.00"
    assert (tmp_path / "paper.db").exists()


def test_late_start_and_unverified_source_do_not_advance(tmp_path, policy, book):
    with pytest.raises(ValueError, match="before market close"):
        exp.start_experiment(tmp_path / "late.db", spec(), policy, now=CLOSE1)
    assert not (tmp_path / "late.db").exists()
    initial(book)
    bad = {**calendar(), "source": "wrong-calendar"}
    with pytest.raises(LedgerConflict, match="calendar source"):
        exp.settle_pair(
            book,
            "study-1",
            "2026-11-02",
            "2026-11-01",
            bad,
            [],
            [close()],
            quote(),
            now=CLOSE2,
        )
    assert len(daily_curve(book, "strategy")) == len(daily_curve(book, "benchmark")) == 1


def test_withdrawal_cannot_break_frozen_cash_reserve(book):
    initial(book)
    buy(book)
    withdraw = {
        "kind": "withdrawal",
        "action_id": "too-much",
        "date": "2026-11-03",
        "phase": "close",
        "source": "fixture",
        "known_at": "2026-11-03@close",
        "amount": "48500.00",
    }
    with pytest.raises(LedgerConflict, match="reserve"):
        exp.settle_pair(
            book,
            "study-1",
            "2026-11-03",
            "2026-11-02",
            calendar(),
            [withdraw],
            [close("2026-11-03")],
            now=datetime(2026, 11, 3, 16, tzinfo=ZONE),
        )
    assert book.snapshot("strategy")["net_external_flow"] == "0.00"
    assert book.snapshot("benchmark")["net_external_flow"] == "0.00"


def test_advance_cli_reads_raw_and_generates_frozen_order(book, tmp_path, monkeypatch, capsys):
    import quant_agent.entrypoints.execution as execution_cli

    class FrozenClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return CLOSE2.astimezone(tz) if tz else CLOSE2.replace(tzinfo=None)

    monkeypatch.setattr(exp, "datetime", FrozenClock)
    market = tmp_path / "market.db"
    with sqlite3.connect(market) as conn:
        conn.execute(
            "CREATE TABLE kline (market TEXT, code TEXT, adjust TEXT, date TEXT, open REAL, close REAL, high REAL, low REAL, volume REAL, amount REAL, src TEXT, fetched_at TEXT)"
        )
        conn.execute(
            "INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "etf",
                "510300",
                "raw",
                "2026-11-02",
                10,
                11,
                11,
                10,
                10000,
                100000,
                "fixture",
                "2026-11-02T16:00:00+08:00",
            ),
        )
    request = tmp_path / "advance.json"
    request.write_text(
        json.dumps(
            {
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
                ],
            }
        )
    )
    assert execution_cli.run_experiment_advance(str(request)) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["days"][-1]["strategy"]["net_assets"] == "50099.40"
    assert result["days"][-1]["benchmark"]["net_assets"] == "50000.00"
    query = tmp_path / "compare.json"
    query.write_text(json.dumps({"ledger_path": str(book.path), "experiment_id": "study-1"}))
    assert execution_cli.run_experiment_compare(str(query)) == 0
    assert json.loads(capsys.readouterr().out)["points"][-1]["difference"] == "99.40"


def test_reserve_applies_before_later_deposit(book):
    initial(book)
    buy(book)
    base = {
        "date": "2026-11-03",
        "source": "fixture",
        "known_at": "2026-11-03@close",
        "phase": "close",
    }
    actions = [
        {**base, "kind": "withdrawal", "action_id": "out", "amount": "48500.00"},
        {**base, "kind": "deposit", "action_id": "in", "amount": "1000.00"},
    ]
    with pytest.raises(LedgerConflict, match="reserve"):
        exp.settle_pair(
            book,
            "study-1",
            "2026-11-03",
            "2026-11-02",
            calendar(),
            actions,
            [close("2026-11-03")],
            now=datetime(2026, 11, 3, 16, tzinfo=ZONE),
        )
    assert book.snapshot("strategy")["net_external_flow"] == "0.00"
