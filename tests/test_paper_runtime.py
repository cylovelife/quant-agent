"""Prospective runtime handoffs preserve account origins and sealed history.

@author cylovelife
"""

import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

from quant_agent.infrastructure.configuration import load_policy

ROOT = Path(__file__).resolve().parents[1]

from test_paper_experiment import (  # noqa: E402
    CLOSE1,
    CLOSE2,
    NOW,
    ZONE,
    buy,
    calendar,
    initial,
    spec,
)

import quant_agent.application.paper.experiment as exp  # noqa: E402
import quant_agent.application.paper.runtime as runtime  # noqa: E402
from quant_agent.application.paper.ledger import (  # noqa: E402
    LedgerConflict,
    LedgerCorruption,
    PaperLedger,
)
from quant_agent.infrastructure.persistence.store import paper_connection  # noqa: E402


@pytest.fixture
def source(tmp_path, monkeypatch):
    monkeypatch.setattr(exp, "code_fingerprints", lambda: {"fixture.py": "a" * 64})
    path = tmp_path / "original.db"
    policy = load_policy(ROOT / "params/execution_v1.json")
    exp.start_experiment(path, spec(), policy, now=NOW)
    return PaperLedger(path)


def request(source, tmp_path, **overrides):
    return {
        "source": str(source.path),
        "target": str(tmp_path / "runtime-v2.db"),
        "experiment_id": "study-1",
        "transition_id": "runtime-v2",
        "effective_from": "2026-11-01",
        "reason": "verified bug fixes only",
        "calendar": calendar(),
        **overrides,
    }


def switch(monkeypatch, letter="b"):
    monkeypatch.setattr(exp, "code_fingerprints", lambda: {"fixture.py": letter * 64})


def origin_rows(book):
    with paper_connection(book.path) as conn:
        return {
            name: [tuple(r) for r in conn.execute(f'SELECT {columns} FROM "{name}" ORDER BY rowid')]
            for name, columns in (
                (
                    "paper_accounts",
                    "account_id,currency,start_date,opening_minor,policy_id,policy_json,created_at",
                ),
                (
                    "paper_experiments",
                    "experiment_id,account_id,benchmark_id,intent_hash,manifest_json,manifest_hash",
                ),
                (
                    "paper_events",
                    "sequence,account_id,order_sequence,event_key,kind,event_date,cash_minor,quantity_delta,market,code",
                ),
            )
        }


def active_manifest(book):
    with paper_connection(book.path) as conn:
        return exp.experiment_for_account(conn, "strategy")


def test_copy_handoff_preserves_origin_capital_and_original_file(source, tmp_path, monkeypatch):
    before = source.path.read_bytes()
    rows = origin_rows(source)
    switch(monkeypatch)
    result = runtime.transition_copy(**request(source, tmp_path), now=NOW)
    target = PaperLedger(result["path"])
    assert source.path.read_bytes() == before
    assert origin_rows(target) == rows
    assert target.snapshot("strategy")["cash"] == "50000.00"
    assert target.snapshot("benchmark")["cash"] == "50000.00"
    assert target.snapshot("strategy")["order_count"] == 0
    manifest = active_manifest(target)
    exp.verify_runtime(manifest)
    assert manifest["runtime"]["transition_id"] == "runtime-v2"
    assert manifest["runtime"]["code_sha256"] == {"fixture.py": "b" * 64}
    assert manifest["code_sha256"] == {"fixture.py": "a" * 64}
    assert exp.compare(target, "study-1")["runtime"]["code_matches"]
    assert initial(target)["strategy"]["runtime_id"] == manifest["runtime"]["runtime_id"]
    assert buy(target)["strategy"]["positions"][0]["quantity"] == 100
    exp.verify_runtime(active_manifest(target))


def test_same_handoff_retry_reads_original_even_after_date_advanced(source, tmp_path, monkeypatch):
    switch(monkeypatch)
    first = runtime.transition_copy(**request(source, tmp_path), now=NOW)
    again = runtime.transition_copy(**request(source, tmp_path), now=CLOSE2)
    assert again == {**first, "replayed": True}
    changed = request(source, tmp_path, reason="different request")
    with pytest.raises(LedgerConflict):
        runtime.transition_copy(**changed, now=NOW)
    switch(monkeypatch, "c")
    with pytest.raises(LedgerConflict):
        runtime.transition_copy(**request(source, tmp_path), now=NOW)


@pytest.mark.parametrize("effective", ["2026-10-31", "2026-11-02"])
def test_handoff_cannot_backdate_or_skip_unsettled_start(source, tmp_path, monkeypatch, effective):
    switch(monkeypatch)
    req = request(source, tmp_path, effective_from=effective)
    with pytest.raises((LedgerConflict, ValueError)):
        runtime.transition_copy(**req, now=NOW)
    assert not Path(req["target"]).exists()


def test_same_day_after_close_and_after_first_entry_are_rejected(source, tmp_path, monkeypatch):
    switch(monkeypatch)
    with pytest.raises(LedgerConflict):
        runtime.transition_copy(**request(source, tmp_path), now=CLOSE1)
    initial_code = {"fixture.py": "a" * 64}
    monkeypatch.setattr(exp, "code_fingerprints", lambda: initial_code)
    initial(source)
    switch(monkeypatch)
    req = request(source, tmp_path, effective_from="2026-11-02")
    with pytest.raises(LedgerConflict):
        runtime.transition_copy(**req, now=datetime(2026, 11, 2, 10, tzinfo=ZONE))


def test_prior_sealed_days_replay_without_rewriting_and_new_days_use_new_runtime(
    source, tmp_path, monkeypatch
):
    closed = initial(source)
    with paper_connection(source.path) as conn:
        original = [
            tuple(r)
            for r in conn.execute(
                "SELECT account_id,day,request_hash,request_json,result_hash,result_json FROM paper_daily ORDER BY rowid"
            )
        ]
    switch(monkeypatch)
    result = runtime.transition_copy(
        **request(source, tmp_path, effective_from="2026-11-02"),
        now=datetime(2026, 11, 2, 8, tzinfo=ZONE),
    )
    target = PaperLedger(result["path"])
    replay = exp.settle_pair(target, "study-1", "2026-11-01", None, calendar(), [], [], now=CLOSE2)
    assert replay["strategy"] == {**closed["strategy"], "replayed": True}
    with paper_connection(target.path) as conn:
        assert [
            tuple(r)
            for r in conn.execute(
                "SELECT account_id,day,request_hash,request_json,result_hash,result_json FROM paper_daily ORDER BY rowid"
            )
        ] == original
    buy(target)
    exp.verify_runtime(active_manifest(target))
    switch(monkeypatch, "c")
    with pytest.raises(LedgerConflict, match="code differs"):
        exp.verify_runtime(active_manifest(target))


def test_gap_before_effective_session_is_rejected(source, tmp_path, monkeypatch):
    initial(source)
    switch(monkeypatch)
    req = request(source, tmp_path, effective_from="2026-11-03")
    with pytest.raises(LedgerConflict, match="unsettled|gap"):
        runtime.transition_copy(**req, now=datetime(2026, 11, 3, 8, tzinfo=ZONE))


def test_transition_chain_and_copied_prefix_are_verified(source, tmp_path, monkeypatch):
    switch(monkeypatch)
    result = runtime.transition_copy(**request(source, tmp_path), now=NOW)
    target = PaperLedger(result["path"])
    with sqlite3.connect(target.path) as conn:
        conn.execute("DROP TRIGGER paper_runtime_no_update")
        conn.execute("UPDATE paper_runtime_transitions SET payload_json='{}'")
    with pytest.raises((LedgerCorruption, ValueError)):
        active_manifest(target)


def test_legacy_prefix_changes_cannot_be_hidden_by_valid_runtime_row(source, tmp_path, monkeypatch):
    switch(monkeypatch)
    result = runtime.transition_copy(**request(source, tmp_path), now=NOW)
    target = PaperLedger(result["path"])
    with sqlite3.connect(target.path) as conn:
        conn.execute("DROP TRIGGER paper_experiments_no_update")
        conn.execute("UPDATE paper_experiments SET intent_hash='changed'")
    with pytest.raises(LedgerCorruption, match="prefix|origin|evidence"):
        active_manifest(target)


def test_failed_candidate_does_not_publish_target_or_change_source(source, tmp_path, monkeypatch):
    before = source.path.read_bytes()
    switch(monkeypatch)

    def fail(*a, **k):
        raise OSError("fixture publish failure")

    monkeypatch.setattr(runtime.os, "link", fail)
    with pytest.raises(OSError):
        runtime.transition_copy(**request(source, tmp_path), now=NOW)
    assert source.path.read_bytes() == before
    assert not Path(request(source, tmp_path)["target"]).exists()


def test_second_handoff_keeps_the_full_chain_and_closed_history(source, tmp_path, monkeypatch):
    switch(monkeypatch)
    first = runtime.transition_copy(**request(source, tmp_path), now=NOW)
    book = PaperLedger(first["path"])
    initial(book)
    switch(monkeypatch, "c")
    second = runtime.transition_copy(
        **request(
            book,
            tmp_path,
            target=str(tmp_path / "runtime-v3.db"),
            transition_id="runtime-v3",
            effective_from="2026-11-02",
        ),
        now=datetime(2026, 11, 2, 8, tzinfo=ZONE),
    )
    target = PaperLedger(second["path"])
    assert active_manifest(target)["runtime"]["sequence"] == 2
    buy(target)
    assert len(exp.compare(target, "study-1")["points"]) == 2
    assert book.snapshot("strategy")["positions"] == []


def test_handoff_rejects_asymmetric_daily_history(source, tmp_path, monkeypatch):
    initial(source)
    with sqlite3.connect(source.path) as conn:
        conn.execute("DROP TRIGGER paper_daily_no_delete")
        conn.execute("DELETE FROM paper_daily WHERE account_id='benchmark'")
    switch(monkeypatch)
    with pytest.raises(LedgerConflict, match="benchmark dates"):
        runtime.transition_copy(
            **request(source, tmp_path, effective_from="2026-11-02"),
            now=datetime(2026, 11, 2, 8, tzinfo=ZONE),
        )
    assert not Path(request(source, tmp_path)["target"]).exists()


def test_same_intent_concurrent_publication_returns_the_existing_revision(
    source, tmp_path, monkeypatch
):
    switch(monkeypatch)
    original_link = runtime.os.link

    def race(candidate, target):
        original_link(candidate, target)
        raise FileExistsError(target)

    monkeypatch.setattr(runtime.os, "link", race)
    result = runtime.transition_copy(**request(source, tmp_path), now=NOW)
    assert result["replayed"]
    with paper_connection(result["path"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM paper_runtime_transitions").fetchone()[0] == 1


def test_runtime_python_drift_and_new_day_before_effective_are_blocked(
    source, tmp_path, monkeypatch
):
    initial(source)
    switch(monkeypatch)
    result = runtime.transition_copy(
        **request(source, tmp_path, effective_from="2026-11-02"),
        now=datetime(2026, 11, 2, 8, tzinfo=ZONE),
    )
    manifest = active_manifest(PaperLedger(result["path"]))
    with pytest.raises(LedgerConflict, match="precedes"):
        exp.verify_runtime(manifest, "2026-11-01")
    manifest["runtime"]["python_version"] = "different runtime"
    with pytest.raises(LedgerConflict, match="Python differs"):
        exp.verify_runtime(manifest)


def test_cli_handoff_creates_no_cash_events_and_original_source_stays_frozen(tmp_path, monkeypatch):
    import subprocess
    from datetime import timedelta

    now = datetime.now(ZONE)
    config = spec()
    config["start_date"] = (now.date() + timedelta(days=2)).isoformat()
    config["first_trade_date"] = (now.date() + timedelta(days=3)).isoformat()
    with monkeypatch.context() as old:
        old.setattr(exp, "code_fingerprints", lambda: {"previous.py": "a" * 64})
        exp.start_experiment(
            tmp_path / "source.db",
            config,
            load_policy(ROOT / "params/execution_v1.json"),
            now=now,
        )
    body = {
        "source": "source.db",
        "target": "target.db",
        "experiment_id": "study-1",
        "transition_id": "cli-v2",
        "effective_from": config["start_date"],
        "reason": "explicit user approved handoff",
        "calendar": {
            "source": "fixture-calendar",
            "sessions": [config["start_date"], config["first_trade_date"]],
        },
    }
    path = tmp_path / "transition.json"
    path.write_text(json.dumps(body))
    before = (tmp_path / "source.db").read_bytes()
    command = [
        sys.executable,
        str(ROOT / "run.py"),
        "--transition-paper-experiment",
        str(path),
    ]
    first = subprocess.run(command, capture_output=True, text=True)
    assert first.returncode == 0, first.stderr
    again = subprocess.run(command, capture_output=True, text=True)
    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout)["replayed"]
    assert (tmp_path / "source.db").read_bytes() == before
    target = PaperLedger(tmp_path / "target.db")
    assert origin_rows(target) == origin_rows(PaperLedger(tmp_path / "source.db"))
    assert exp.compare(target, "study-1")["runtime"]["code_matches"]


def test_handoff_rejects_other_accounts_outside_the_inherited_experiment(
    source, tmp_path, monkeypatch
):
    source.create_account(
        "other",
        "100.00",
        "2026-11-01",
        load_policy(ROOT / "params/execution_v1.json"),
    )
    switch(monkeypatch)
    with pytest.raises(LedgerConflict, match="isolated"):
        runtime.transition_copy(**request(source, tmp_path), now=NOW)
    assert not Path(request(source, tmp_path)["target"]).exists()


def test_future_runtime_is_scheduled_and_cannot_claim_current_readiness(source):
    manifest = active_manifest(source)
    status = runtime.runtime_status(manifest, now=datetime(2026, 10, 31, 18, tzinfo=ZONE))
    assert status["activation_state"] == "scheduled"
    assert not status["ready"]
    assert not status["can_settle_current_day"]
    intraday = runtime.runtime_status(manifest, now=NOW)
    assert intraday["ready"]
    assert not intraday["can_settle_current_day"]
    assert runtime.runtime_status(manifest, now=CLOSE1)["can_settle_current_day"]
