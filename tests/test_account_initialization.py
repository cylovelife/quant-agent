"""Paper account initialization through the public CLI, with isolated ledgers.

@author cylovelife
"""

import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def invoke(path):
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "run.py"),
            "--init-account",
            str(path),
        ],
        cwd=path.parent,
        capture_output=True,
        text=True,
        timeout=30,
    )


def request(tmp_path):
    data = {
        "ledger_path": "account.db",
        "account_id": "paper",
        "opening_cash": "50000.00",
        "currency": "CNY",
        "start_date": "2026-10-27",
        "policy_path": str(ROOT / "params/execution_v1.json"),
    }
    path = tmp_path / "request.json"
    path.write_text(json.dumps(data))
    return path, data


def test_cli_initializes_once_and_rejects_changed_origin(tmp_path):
    path, data = request(tmp_path)
    first = invoke(path)
    assert first.returncode == 0, first.stderr
    snapshot = json.loads(first.stdout)
    assert snapshot["opening_cash"] == snapshot["cash"] == "50000.00"
    assert snapshot["currency"] == "CNY" and snapshot["start_date"] == "2026-10-27"
    assert snapshot["positions"] == [] and snapshot["event_count"] == 1
    again = invoke(path)
    assert again.returncode == 0 and json.loads(again.stdout) == snapshot
    path.write_text(json.dumps({**data, "opening_cash": "60000.00"}))
    assert invoke(path).returncode == 2
    with sqlite3.connect(tmp_path / "account.db") as db:
        assert (
            db.execute(
                "SELECT count(*) FROM paper_events WHERE kind='opening_cash'"
            ).fetchone()[0]
            == 1
        )


def test_invalid_input_does_not_create_a_ledger(tmp_path):
    path, data = request(tmp_path)
    for change in (
        {"currency": "USD"},
        {"opening_cash": "-1"},
        {"opening_cash": "0"},
        {"start_date": "2026-02-30"},
        {"policy_path": "missing.json"},
    ):
        path.write_text(json.dumps({**data, **change}))
        assert invoke(path).returncode == 2
        assert not (tmp_path / "account.db").exists()


def test_existing_nonledger_never_overwritten(tmp_path):
    path, data = request(tmp_path)
    target = tmp_path / data["ledger_path"]
    target.write_bytes(b"user data")
    assert invoke(path).returncode == 2
    assert target.read_bytes() == b"user data"


def test_concurrent_same_account_books_one_opening(tmp_path):
    path, _ = request(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: invoke(path), range(2)))
    assert all(item.returncode == 0 for item in results), [r.stderr for r in results]
    with sqlite3.connect(tmp_path / "account.db") as db:
        assert db.execute("SELECT count(*) FROM paper_accounts").fetchone()[0] == 1
        assert (
            db.execute(
                "SELECT count(*) FROM paper_events WHERE kind='opening_cash'"
            ).fetchone()[0]
            == 1
        )


def test_retry_after_cash_flow_does_not_reset_account(tmp_path):
    path, _ = request(tmp_path)
    assert invoke(path).returncode == 0
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps(
            {
                "ledger_path": "account.db",
                "account_id": "paper",
                "action": {
                    "kind": "withdrawal",
                    "action_id": "withdraw-1",
                    "date": "2026-10-28",
                    "phase": "preopen",
                    "source": "fixture",
                    "known_at": "2026-10-28@preopen",
                    "amount": "100.00",
                },
            }
        )
    )
    booked = subprocess.run(
        [sys.executable, str(ROOT / "run.py"), "--record-action", str(event)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert booked.returncode == 0, booked.stderr
    retried = invoke(path)
    assert retried.returncode == 0, retried.stderr
    assert json.loads(retried.stdout)["cash"] == "49900.00"


def test_failed_staged_initialization_publishes_nothing(tmp_path, monkeypatch):
    sys.path.insert(0, str(ROOT / "src"))
    import execution
    import paper_ledger
    import pytest

    policy = execution.load_policy(ROOT / "params/execution_v1.json")

    def fail(*args, **kwargs):
        raise RuntimeError("injected failure")

    monkeypatch.setattr(paper_ledger.PaperLedger, "create_account", fail)
    with pytest.raises(RuntimeError, match="injected failure"):
        paper_ledger.initialize_account(
            tmp_path / "account.db", "paper", "50000.00", "CNY", "2026-10-27", policy
        )
    assert list(tmp_path.iterdir()) == []
