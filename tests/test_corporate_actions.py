"""Corporate action and external cash-flow behavior on an isolated paper ledger.

@author cylovelife
"""

# Imported pytest fixtures intentionally share names with test parameters.
# ruff: noqa: F811

import json
import sqlite3
from decimal import Decimal

import pytest
from test_paper_ledger import inputs
from test_paper_ledger import ledger as ledger
from test_paper_ledger import modules as modules
from test_paper_ledger import policy as policy


def action(kind, action_id, day, phase="preopen", **fields):
    return {
        "kind": kind,
        "action_id": action_id,
        "date": day,
        "phase": phase,
        "source": "fixture",
        "known_at": day + "@" + phase,
        **fields,
    }


def test_dividend_entitlement_survives_sale_and_is_paid_once(modules, ledger):
    execution, _ = modules
    ledger.execute("fixture", *inputs(execution))
    declaration = action(
        "dividend_entitlement",
        "div-1",
        "2026-10-27",
        phase="close",
        market="cn",
        code="600000",
        net_per_share="0.10",
    )
    received = ledger.apply_action("fixture", declaration)
    assert received["quantity_basis"] == 100 and received["receivable_delta"] == "0.00"
    assert ledger.snapshot("fixture")["dividend_receivable"] == "0.00"
    accrual = action("dividend_accrual", "accrue-1", "2026-10-28", entitlement_id="div-1")
    assert ledger.apply_action("fixture", accrual)["receivable_delta"] == "10.00"
    assert ledger.snapshot("fixture")["dividend_receivable"] == "10.00"
    ledger.execute(
        "fixture",
        *inputs(execution, "sell", side="sell", signal="2026-10-27", session="2026-10-28"),
    )
    payment = action("dividend_payment", "pay-1", "2026-10-29", accrual_id="accrue-1")
    first = ledger.apply_action("fixture", payment)
    assert first["cash_delta"] == "10.00"
    assert ledger.snapshot("fixture")["cash"] == "49997.48"
    assert ledger.snapshot("fixture")["dividend_receivable"] == "0.00"
    assert ledger.apply_action("fixture", payment)["replayed"]
    with pytest.raises(modules[1].LedgerConflict):
        ledger.apply_action("fixture", {**payment, "action_id": "pay-again"})


def test_split_changes_qty_without_cash_and_preserves_sellability(modules, ledger):
    execution, _ = modules
    ledger.execute("fixture", *inputs(execution))
    split = action(
        "split",
        "split-1",
        "2026-10-28",
        market="cn",
        code="600000",
        numerator=2,
        denominator=1,
    )
    result = ledger.apply_action("fixture", split)
    assert result["quantity_delta"] == 100 and result["cash_delta"] == "0.00"
    assert ledger.snapshot("fixture")["positions"][0]["quantity"] == 200
    assert ledger.apply_action("fixture", split)["replayed"]
    sold = ledger.execute(
        "fixture",
        *inputs(
            execution,
            "sell",
            side="sell",
            quantity=200,
            signal="2026-10-27",
            session="2026-10-28",
        ),
    )
    assert sold["status"] == "filled" and ledger.snapshot("fixture")["positions"] == []


def test_fractional_split_is_not_rounded_into_invented_shares(modules, ledger):
    execution, module = modules
    ledger.execute("fixture", *inputs(execution))
    before = ledger.snapshot("fixture")
    split = action(
        "split",
        "split-bad",
        "2026-10-28",
        market="cn",
        code="600000",
        numerator=1,
        denominator=3,
    )
    with pytest.raises(module.LedgerConflict):
        ledger.apply_action("fixture", split)
    assert ledger.snapshot("fixture") == before


def test_external_cash_flows_are_separate_and_cannot_overdraw(modules, ledger):
    module = modules[1]
    deposit = action("deposit", "in-1", "2026-10-27", amount="100.00")
    withdraw = action("withdrawal", "out-1", "2026-10-27", phase="close", amount="25.00")
    ledger.apply_action("fixture", deposit)
    ledger.apply_action("fixture", withdraw)
    snap = ledger.snapshot("fixture")
    assert snap["cash"] == "50075.00" and snap["net_external_flow"] == "75.00"
    assert ledger.apply_action("fixture", deposit)["replayed"]
    with pytest.raises(module.LedgerConflict):
        ledger.apply_action(
            "fixture", action("withdrawal", "out-2", "2026-10-28", amount="50076.00")
        )
    assert ledger.snapshot("fixture") == snap


def test_action_identity_temporal_guards_and_transaction_rollback(modules, ledger):
    execution, module = modules
    ledger.execute("fixture", *inputs(execution))
    before = ledger.snapshot("fixture")
    split = action(
        "split",
        "split-1",
        "2026-10-28",
        market="cn",
        code="600000",
        numerator=2,
        denominator=1,
    )
    with sqlite3.connect(ledger.path) as conn:
        conn.execute(
            "CREATE TRIGGER fault_action BEFORE INSERT ON paper_actions BEGIN SELECT RAISE(ABORT,'fixture fault'); END"
        )
    with pytest.raises(sqlite3.DatabaseError):
        ledger.apply_action("fixture", split)
    assert ledger.snapshot("fixture") == before
    with sqlite3.connect(ledger.path) as conn:
        conn.execute("DROP TRIGGER fault_action")
    ledger.apply_action("fixture", split)
    with pytest.raises(module.LedgerConflict):
        ledger.apply_action("fixture", {**split, "numerator": 3})
    with pytest.raises(module.LedgerConflict):
        ledger.apply_action("fixture", action("deposit", "past", "2026-10-27", amount="1.00"))
    with pytest.raises(module.LedgerConflict):
        ledger.apply_action(
            "fixture",
            action(
                "deposit",
                "future-known",
                "2026-10-29",
                amount="1.00",
                known_at="2026-10-30@close",
            ),
        )


def test_unknown_actions_never_become_total_return():
    from quant_agent.application.research.value_tracking import price_change_observation

    result = price_change_observation("10", "9")
    assert result["price_return"] == pytest.approx(-0.1)
    assert (
        result["total_return"] is None
        and result["total_return_status"] == "corporate_actions_unavailable"
    )


def test_v1_ledger_can_be_upgraded_only_to_new_copy(modules, policy, tmp_path, monkeypatch):
    import quant_agent.infrastructure.persistence.store as store

    execution, module = modules
    monkeypatch.setattr(store, "PAPER_SCHEMA_VERSION", 1)
    old = module.PaperLedger.create(tmp_path / "old.db")
    old.create_account("fixture", "50000", "2026-10-26", policy)
    old.execute("fixture", *inputs(execution))
    before = old.snapshot("fixture")
    original = old.path.read_bytes()
    monkeypatch.setattr(store, "PAPER_SCHEMA_VERSION", 2)
    migrated = module.migrate_ledger_copy(old.path, tmp_path / "copy.db")
    assert migrated.snapshot("fixture")["cash"] == before["cash"]
    assert old.path.read_bytes() == original
    with pytest.raises(FileExistsError):
        module.migrate_ledger_copy(old.path, migrated.path)
    with pytest.raises(ValueError):
        old.execute("fixture", *inputs(execution, "new"))
    migrated.apply_action("fixture", action("deposit", "in", "2026-10-28", amount="10.00"))
    assert migrated.snapshot("fixture")["cash"] == "49003.99"


def test_cash_and_action_ordering_is_visible_and_cannot_be_backdated(modules, ledger):
    execution, module = modules
    ledger.execute("fixture", *inputs(execution))
    ledger.apply_action(
        "fixture",
        action("deposit", "cash-close", "2026-10-27", phase="close", amount="5.00"),
    )
    with pytest.raises(module.LedgerConflict):
        ledger.execute("fixture", *inputs(execution, "too-late-same-open"))
    entries = ledger.events("fixture")
    assert [row["kind"] for row in entries][-1] == "deposit"
    assert sum(Decimal(row["cash_delta"]) for row in entries) == Decimal(
        ledger.snapshot("fixture")["cash"]
    )


def test_entitlement_cannot_be_accrued_twice(modules, ledger):
    execution, module = modules
    ledger.execute("fixture", *inputs(execution))
    ledger.apply_action(
        "fixture",
        action(
            "dividend_entitlement",
            "e",
            "2026-10-27",
            phase="close",
            market="cn",
            code="600000",
            net_per_share="0.10",
        ),
    )
    ledger.apply_action(
        "fixture", action("dividend_accrual", "a", "2026-10-28", entitlement_id="e")
    )
    before = ledger.snapshot("fixture")
    with pytest.raises(module.LedgerConflict):
        ledger.apply_action(
            "fixture",
            action("dividend_accrual", "duplicate", "2026-10-29", entitlement_id="e"),
        )
    assert ledger.snapshot("fixture") == before


def test_copy_migration_failure_removes_candidate_and_preserves_source(
    modules, policy, tmp_path, monkeypatch
):
    import quant_agent.infrastructure.persistence.store as store

    execution, module = modules
    monkeypatch.setattr(store, "PAPER_SCHEMA_VERSION", 1)
    old = module.PaperLedger.create(tmp_path / "old-fail.db")
    old.create_account("fixture", "50000", "2026-10-26", policy)
    old.execute("fixture", *inputs(execution))
    old_bytes = old.path.read_bytes()
    monkeypatch.setattr(store, "PAPER_SCHEMA_VERSION", 2)
    monkeypatch.setattr(store, "PAPER_V2_DDL", (*store.PAPER_V2_DDL, "INVALID DDL"))
    target = tmp_path / "failed-copy.db"
    with pytest.raises(sqlite3.DatabaseError):
        module.migrate_ledger_copy(old.path, target)
    assert not target.exists() and old.path.read_bytes() == old_bytes


def test_action_cli_books_only_declared_temporary_ledger(modules, ledger, tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    request = {
        "ledger_path": str(ledger.path),
        "account_id": "fixture",
        "action": action("deposit", "cli-deposit", "2026-10-27", amount="30.00"),
    }
    file = tmp_path / "action.json"
    file.write_text(json.dumps(request))
    command = [sys.executable, str(root / "run.py"), "--record-action", str(file)]
    first = subprocess.run(command, cwd=tmp_path, text=True, capture_output=True, timeout=20)
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout)["cash_after"] == "50030.00"
    again = subprocess.run(command, cwd=tmp_path, text=True, capture_output=True, timeout=20)
    assert again.returncode == 0 and json.loads(again.stdout)["replayed"]
    assert ledger.snapshot("fixture")["cash"] == "50030.00"
    assert not (tmp_path / "state").exists() and not (tmp_path / "logs").exists()


def test_preopen_cash_flow_is_listed_before_same_day_order(modules, ledger):
    execution, _ = modules
    ledger.apply_action("fixture", action("deposit", "early", "2026-10-27", amount="5.00"))
    ledger.execute("fixture", *inputs(execution))
    events = ledger.events("fixture")
    assert [item["kind"] for item in events][:3] == [
        "opening_cash",
        "deposit",
        "trade_cash",
    ]
    assert ledger.snapshot("fixture")["cash"] == "48998.99"


def test_store_script_initializes_only_its_explicit_test_market_db(tmp_path):
    import os
    import subprocess
    import sys

    market = tmp_path / "market.db"
    env = {**os.environ, "QUANT_DB": str(market), "QUANT_STORE": "sqlite"}
    run = subprocess.run(
        [sys.executable, "-m", "quant_agent.infrastructure.persistence.store"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert run.returncode == 0, run.stderr
    with sqlite3.connect(market) as con:
        assert con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='kline'"
        ).fetchone()


def test_corrupted_action_is_reported_as_corruption_not_a_valid_account(modules, ledger):
    from quant_agent.domain.trading.execution import _identity

    module = modules[1]
    bad = action("deposit", "bad-record", "2026-10-27", amount="1.00", source="")
    payload = json.dumps(bad, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    with sqlite3.connect(ledger.path) as con:
        con.execute(
            """INSERT INTO paper_actions(account_id,action_id,after_attempt,action_date,phase,intent_hash,intent_json,result_json,cash_minor,quantity_delta)
                       VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                "fixture",
                "bad-record",
                0,
                "2026-10-27",
                "preopen",
                _identity(bad),
                payload,
                "{}",
                100,
                0,
            ),
        )
    with pytest.raises(module.LedgerCorruption):
        ledger.snapshot("fixture")
