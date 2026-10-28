"""Transactional virtual-cash ledger, recovery and legacy isolation regressions.

@author cylovelife
"""

import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from decimal import Decimal
from pathlib import Path

import pytest

from quant_agent.infrastructure.configuration import load_policy

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def modules():
    import quant_agent.application.paper.ledger as paper_ledger
    import quant_agent.domain.trading.execution as execution

    return execution, paper_ledger


@pytest.fixture
def policy(modules):
    return load_policy(ROOT / "params/execution_v1.json")


@pytest.fixture
def ledger(modules, policy, tmp_path):
    book = modules[1].PaperLedger.create(tmp_path / "paper.db")
    book.create_account("fixture", "50000", "2026-10-26", policy)
    return book


def inputs(
    execution,
    order_id="buy-1",
    side="buy",
    quantity=100,
    code="600000",
    signal="2026-10-26",
    session="2026-10-27",
    price="10",
    status="trading",
):
    instrument = execution.Instrument("cn", code, "SSE", "cn_mainboard")
    order = execution.Order(order_id, "cn", code, side, quantity, signal, session)
    facts = execution.SessionFacts(
        session,
        "fixture",
        session + "@open",
        status,
        "regular",
        "9",
        "11",
        10000,
        "assumed",
        signal,
        "cn",
        code,
    )
    quote = execution.OpenQuote(
        "cn",
        code,
        session,
        price,
        "raw",
        "fixture",
        session + "T09:25:00+08:00",
        "raw-fixture",
        facts,
    )
    return order, instrument, quote


def test_buy_sell_events_reconcile_to_cash_quantity_and_fees(modules, ledger):
    execution, _ = modules
    buy = ledger.execute("fixture", *inputs(execution))
    assert buy["cash_before"] == "50000.00" and buy["cash_after"] == "48993.99"
    assert buy["quantity_after"] == 100
    before = ledger.snapshot("fixture")
    assert before["positions"][0]["quantity"] == 100
    same_day = ledger.execute("fixture", *inputs(execution, "sell-too-soon", side="sell"))
    assert same_day["reason"] == "insufficient_sellable_quantity"
    assert ledger.snapshot("fixture")["cash"] == before["cash"]
    sell = ledger.execute(
        "fixture",
        *inputs(execution, "sell-1", side="sell", signal="2026-10-27", session="2026-10-28"),
    )
    assert sell["status"] == "filled" and sell["quantity_after"] == 0
    snapshot = ledger.snapshot("fixture")
    assert snapshot["cash"] == "49987.48" and snapshot["positions"] == []
    assert snapshot["fees"] == {
        "commission": "10.00",
        "stamp_duty": "0.50",
        "transfer": "0.02",
        "total": "10.52",
    }
    events = ledger.events("fixture")
    assert len(events) == 11
    assert sum(Decimal(row["cash_delta"]) for row in events) == Decimal(snapshot["cash"])
    assert sum(row["quantity_delta"] for row in events) == 0
    assert snapshot["reconciled"] is True


def test_repeat_is_exactly_once_even_after_refetch_or_reopen(modules, ledger):
    execution, module = modules
    order, instrument, quote = inputs(execution)
    first = ledger.execute("fixture", order, instrument, quote)
    reopened = module.PaperLedger(ledger.path)
    revised = replace(quote, open_price="10.50", fetched_at="2026-10-28T18:00:00+08:00")
    from quant_agent.application.investing.portfolio import record_paper_order

    again = record_paper_order(reopened, "fixture", order, instrument, revised)
    assert again["replayed"] is True
    assert {k: v for k, v in first.items() if k != "replayed"} == {
        k: v for k, v in again.items() if k != "replayed"
    }
    assert reopened.snapshot("fixture")["cash"] == "48993.99"
    assert len(reopened.events("fixture")) == 6


@pytest.mark.parametrize(
    "field,value",
    [("quantity", 200), ("side", "sell"), ("code", "600519"), ("limit_price", "10.50")],
)
def test_same_order_id_with_changed_intent_is_conflict(modules, ledger, field, value):
    execution, module = modules
    order, instrument, quote = inputs(execution)
    ledger.execute("fixture", order, instrument, quote)
    before = ledger.snapshot("fixture")
    with pytest.raises(module.LedgerConflict):
        ledger.execute("fixture", replace(order, **{field: value}), instrument, quote)
    assert ledger.snapshot("fixture") == before


def test_many_orders_share_one_cash_balance_and_failed_order_has_no_events(
    modules, policy, tmp_path
):
    execution, module = modules
    book = module.PaperLedger.create(tmp_path / "small.db")
    book.create_account("small", "1006.01", "2026-10-26", policy)
    assert book.execute("small", *inputs(execution))["status"] == "filled"
    failed = book.execute("small", *inputs(execution, "buy-2", code="600519"))
    assert failed["reason"] == "insufficient_cash"
    assert failed["cash_after"] == "0.00"
    assert len(book.events("small")) == 6
    assert book.snapshot("small")["positions"][0]["code"] == "600000"


def test_duplicate_and_competing_writers_are_serialized(modules, policy, tmp_path):
    execution, module = modules
    book = module.PaperLedger.create(tmp_path / "concurrent.db")
    book.create_account("one", "1006.01", "2026-10-26", policy)

    def same(_):
        return module.PaperLedger(book.path).execute("one", *inputs(execution))

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(same, range(8)))
    assert sum(not result["replayed"] for result in results) == 1
    assert len(book.events("one")) == 6
    book.create_account("two", "1006.01", "2026-10-26", policy)

    def competing(number):
        return module.PaperLedger(book.path).execute("two", *inputs(execution, f"order-{number}"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(competing, range(2)))
    assert sorted(result["status"] for result in results) == ["filled", "rejected"]
    assert book.snapshot("two")["cash"] == "0.00"


def test_error_mid_write_rolls_back_every_row_and_retry_succeeds(modules, ledger):
    execution, _ = modules
    before = ledger.snapshot("fixture")
    with sqlite3.connect(ledger.path) as con:
        con.execute(
            "CREATE TRIGGER fail_fee BEFORE INSERT ON paper_events WHEN NEW.kind='commission' BEGIN SELECT RAISE(ABORT,'fixture storage failure'); END"
        )
    with pytest.raises(sqlite3.DatabaseError, match="fixture storage failure"):
        ledger.execute("fixture", *inputs(execution))
    assert ledger.snapshot("fixture") == before
    with sqlite3.connect(ledger.path) as con:
        assert con.execute("SELECT count(*) FROM paper_orders").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM paper_attempts").fetchone()[0] == 0
        con.execute("DROP TRIGGER fail_fee")
    assert ledger.execute("fixture", *inputs(execution))["status"] == "filled"
    assert len(ledger.events("fixture")) == 6


def test_unknown_data_can_retry_but_blocks_skipping_to_future(modules, ledger):
    execution, module = modules
    order, instrument, quote = inputs(execution)
    missing = replace(quote, open_price=None, issues=("missing",))
    first = ledger.execute("fixture", order, instrument, missing)
    assert first["status"] == "unavailable"
    assert len(ledger.events("fixture")) == 1
    assert ledger.execute("fixture", order, instrument, missing)["replayed"]
    with pytest.raises(module.LedgerConflict, match="unresolved"):
        ledger.execute(
            "fixture",
            *inputs(execution, "later", signal="2026-10-27", session="2026-10-28"),
        )
    fixed = ledger.execute("fixture", order, instrument, quote)
    assert fixed["status"] == "filled" and fixed["attempt"] == 2
    assert len(ledger.events("fixture")) == 6


def test_cancel_unknown_order_releases_chronology_without_money_change(modules, ledger):
    execution, _ = modules
    order, instrument, quote = inputs(execution)
    ledger.execute("fixture", order, instrument, replace(quote, issues=("missing",)))
    canceled = ledger.cancel_pending("fixture", order.order_id, "skip unknown session")
    assert canceled["status"] == "canceled"
    assert ledger.snapshot("fixture")["cash"] == "50000.00"
    assert len(ledger.events("fixture")) == 1
    assert ledger.execute("fixture", order, instrument, quote)["status"] == "canceled"
    assert (
        ledger.execute(
            "fixture",
            *inputs(execution, "later", signal="2026-10-27", session="2026-10-28"),
        )["status"]
        == "filled"
    )


def test_no_backdated_new_order_or_prelaunch_signal(modules, ledger):
    execution, module = modules
    ledger.execute(
        "fixture",
        *inputs(execution, "later", signal="2026-10-28", session="2026-10-29"),
    )
    with pytest.raises(module.LedgerConflict, match="chronology"):
        ledger.execute("fixture", *inputs(execution))
    with pytest.raises(module.LedgerConflict, match="start"):
        ledger.execute(
            "fixture",
            *inputs(execution, "before-start", signal="2026-10-23", session="2026-10-26"),
        )


def test_fifo_sell_keeps_unsold_lots_and_no_negative_quantity(modules, ledger):
    execution, _ = modules
    ledger.execute("fixture", *inputs(execution, "lot-1", quantity=200))
    ledger.execute(
        "fixture",
        *inputs(execution, "lot-2", quantity=100, signal="2026-10-27", session="2026-10-28"),
    )
    ledger.execute(
        "fixture",
        *inputs(
            execution,
            "sell",
            side="sell",
            quantity=200,
            signal="2026-10-28",
            session="2026-10-29",
        ),
    )
    snapshot = ledger.snapshot("fixture")
    lots = snapshot["positions"][0]["lots"]
    assert len(lots) == 1 and lots[0]["source_order_id"] == "lot-2" and lots[0]["quantity"] == 100
    result = ledger.execute(
        "fixture",
        *inputs(
            execution,
            "oversell",
            side="sell",
            quantity=200,
            signal="2026-10-28",
            session="2026-10-29",
        ),
    )
    assert result["status"] == "rejected"
    assert ledger.snapshot("fixture")["positions"][0]["quantity"] == 100


def test_initial_funding_is_idempotent_accounts_are_separate_and_policy_is_frozen(
    modules, policy, ledger, tmp_path
):
    execution, module = modules
    initial = ledger.snapshot("fixture")
    assert ledger.create_account("fixture", "50000.00", "2026-10-26", policy) == initial
    with pytest.raises(module.LedgerConflict):
        ledger.create_account("fixture", "60000", "2026-10-26", policy)
    raw = json.loads((ROOT / "params/execution_v1.json").read_text())
    raw["profiles"]["cn_mainboard"]["commission_min"] = "10"
    altered = tmp_path / "cost.json"
    altered.write_text(json.dumps(raw))
    changed = load_policy(altered)
    with pytest.raises(module.LedgerConflict):
        ledger.create_account("fixture", "50000", "2026-10-26", changed)
    ledger.create_account("other", "50000", "2026-10-26", changed)
    first = ledger.execute("fixture", *inputs(execution))
    other = ledger.execute("other", *inputs(execution))
    assert first["fees"]["commission"] == "5.00" and other["fees"]["commission"] == "10.00"
    assert first["policy_id"] == policy.policy_id and other["policy_id"] == changed.policy_id


def test_events_are_append_only_and_corrupt_book_never_becomes_empty(modules, ledger):
    execution, module = modules
    ledger.execute("fixture", *inputs(execution))
    with sqlite3.connect(ledger.path) as con:
        with pytest.raises(sqlite3.DatabaseError):
            con.execute("UPDATE paper_events SET cash_minor=0")
        con.execute(
            "INSERT INTO paper_events(account_id,event_key,kind,event_date,cash_minor,quantity_delta,market,code) VALUES ('fixture','bad-extra','trade_cash','2026-10-27',1,0,'cn','600000')"
        )
    with pytest.raises(module.LedgerCorruption):
        ledger.snapshot("fixture")
    with pytest.raises(module.LedgerCorruption):
        ledger.execute("fixture", *inputs(execution, "buy-2"))


def test_market_store_or_missing_db_cannot_be_reinitialized_as_cash_ledger(modules, tmp_path):
    module = modules[1]
    market = tmp_path / "market.db"
    with sqlite3.connect(market) as con:
        con.execute("CREATE TABLE kline(date TEXT)")
    before = market.read_bytes()
    with pytest.raises((ValueError, module.LedgerError)):
        module.PaperLedger(market)
    with pytest.raises(FileExistsError):
        module.PaperLedger.create(market)
    assert market.read_bytes() == before
    missing = tmp_path / "missing.db"
    with pytest.raises((FileNotFoundError, sqlite3.OperationalError)):
        module.PaperLedger(missing)
    assert not missing.exists()


def test_legacy_rehearsal_copies_but_never_invents_cash_or_quantity(modules, tmp_path, monkeypatch):
    module = modules[1]
    old = tmp_path / "positions.json"
    old.write_text(
        json.dumps(
            {
                "positions": [
                    {
                        "market": "cn",
                        "code": "600000",
                        "entry_price": 10,
                        "status": "open",
                    }
                ]
            }
        )
    )
    original = old.read_bytes()
    target = tmp_path / "review"
    report = module.stage_legacy_book(old, target)
    assert report["migration_allowed"] is False and report["position_count"] == 1
    assert (target / "positions.json").read_bytes() == original
    assert old.read_bytes() == original
    with pytest.raises(FileExistsError):
        module.stage_legacy_book(old, target)
    broken = tmp_path / "broken.json"
    broken.write_text("{bad")
    with pytest.raises(ValueError):
        module.stage_legacy_book(broken, tmp_path / "failed")
    assert not (tmp_path / "failed").exists()
    import shutil

    def fail_copy(source, destination):
        Path(destination).write_bytes(b"partial")
        raise OSError("fixture copy failure")

    monkeypatch.setattr(shutil, "copyfile", fail_copy)
    with pytest.raises(OSError, match="fixture copy failure"):
        module.stage_legacy_book(old, tmp_path / "failed-copy")
    assert not (tmp_path / "failed-copy").exists() and old.read_bytes() == original


def test_cli_records_using_ledger_cash_instead_of_request_account(modules, ledger, tmp_path):
    execution, _ = modules
    order, instrument, quote = inputs(execution)
    bars = tmp_path / "prices.db"
    with sqlite3.connect(bars) as con:
        con.execute(
            "CREATE TABLE kline(market TEXT,code TEXT,adjust TEXT,date TEXT,open REAL,close REAL,high REAL,low REAL,volume REAL,amount REAL,src TEXT,fetched_at TEXT)"
        )
        con.execute(
            "INSERT INTO kline VALUES ('cn','600000','raw','2026-10-27',10,10,11,9,100,1000,'fixture','2026-10-27 18:00:00')"
        )
    request = {
        "ledger_path": str(ledger.path),
        "account_id": "fixture",
        "db_path": str(bars),
        "order": asdict(order),
        "instrument": asdict(instrument),
        "session": asdict(quote.facts),
    }
    file = tmp_path / "record.json"
    file.write_text(json.dumps(request))
    before = bars.read_bytes()
    result = subprocess.run(
        [sys.executable, str(ROOT / "run.py"), "--record-order", str(file)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["cash_after"] == "48993.99"
    again = subprocess.run(
        [sys.executable, str(ROOT / "run.py"), "--record-order", str(file)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert again.returncode == 0 and json.loads(again.stdout)["replayed"]
    assert len(ledger.events("fixture")) == 6 and bars.read_bytes() == before
    assert not (tmp_path / "state").exists() and not (tmp_path / "logs").exists()
    bars.unlink()
    retried_without_prices = subprocess.run(
        [sys.executable, str(ROOT / "run.py"), "--record-order", str(file)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert retried_without_prices.returncode == 0, retried_without_prices.stderr
    assert json.loads(retried_without_prices.stdout)["replayed"]
    status_file = tmp_path / "status.json"
    status_file.write_text(json.dumps({"ledger_path": str(ledger.path), "account_id": "fixture"}))
    status = subprocess.run(
        [sys.executable, str(ROOT / "run.py"), "--ledger-status", str(status_file)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert status.returncode == 0, status.stderr
    inspected = json.loads(status.stdout)
    assert inspected["cash"] == "48993.99" and len(inspected["events"]) == 6
    request["account"] = {"available_cash": "999999"}
    file.write_text(json.dumps(request))
    invalid = subprocess.run(
        [sys.executable, str(ROOT / "run.py"), "--record-order", str(file)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert invalid.returncode == 2 and len(ledger.events("fixture")) == 6


@pytest.mark.parametrize("cash", ["NaN", "0.001", "-1", True, "1000000000000000000"])
def test_invalid_initial_cash_cannot_create_partial_origin(modules, policy, ledger, cash):
    before = ledger.snapshot("fixture")
    with pytest.raises(ValueError):
        ledger.create_account("bad-origin", cash, "2026-10-26", policy)
    with sqlite3.connect(ledger.path) as con:
        assert (
            con.execute(
                "SELECT count(*) FROM paper_accounts WHERE account_id='bad-origin'"
            ).fetchone()[0]
            == 0
        )
    assert ledger.snapshot("fixture") == before


def test_retried_same_day_pending_uses_cash_after_intervening_order(modules, ledger):
    execution, _ = modules
    order, instrument, quote = inputs(execution, "pending-first")
    ledger.execute("fixture", order, instrument, replace(quote, issues=("missing",)))
    ledger.execute("fixture", *inputs(execution, "filled-second"))
    result = ledger.execute("fixture", order, instrument, quote)
    assert result["cash_before"] == "48993.99" and result["cash_after"] == "47987.98"
    assert ledger.snapshot("fixture")["positions"][0]["quantity"] == 200
    assert len(ledger.events("fixture")) == 11


def test_market_cache_mode_never_disables_cash_accounting(modules, ledger, monkeypatch):
    execution, module = modules
    import quant_agent.infrastructure.persistence.store as store

    monkeypatch.setattr(store, "_ENABLED", False)
    assert ledger.execute("fixture", *inputs(execution))["status"] == "filled"
    assert module.PaperLedger(ledger.path).snapshot("fixture")["cash"] == "48993.99"


def test_filled_order_cannot_be_canceled_into_a_refund(modules, ledger):
    execution, module = modules
    ledger.execute("fixture", *inputs(execution))
    before = ledger.snapshot("fixture")
    with pytest.raises(module.LedgerConflict):
        ledger.cancel_pending("fixture", "buy-1", "pretend refund")
    assert ledger.snapshot("fixture") == before


def test_subcent_digits_beyond_decimal_context_are_not_rounded_away(modules, policy, ledger):
    with pytest.raises(ValueError):
        ledger.create_account("fractional", "1.0000000000000000000000000001", "2026-10-26", policy)


def test_failed_schema_creation_leaves_no_half_initialized_ledger(modules, tmp_path, monkeypatch):
    import quant_agent.infrastructure.persistence.store as store

    original = store.PAPER_DDL
    monkeypatch.setattr(store, "PAPER_DDL", (*original, "INVALID DDL"))
    path = tmp_path / "creation-failed.db"
    with pytest.raises(sqlite3.DatabaseError):
        modules[1].PaperLedger.create(path)
    assert not path.exists()
    monkeypatch.setattr(store, "PAPER_DDL", original)
    modules[1].PaperLedger.create(path)


def test_cash_ledger_cannot_be_migrated_by_market_cache_connector(modules, ledger, tmp_path):
    import os

    before = ledger.path.read_bytes()
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT / "src"),
        "QUANT_DB": str(ledger.path),
        "QUANT_STORE": "sqlite",
    }
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from quant_agent.infrastructure.persistence import store; assert store.connect() is None; print(str(store.last_error()))",
        ],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "cash ledger" in result.stdout and ledger.path.read_bytes() == before
