"""Append-only prospective runtime handoffs on an audited copy of a paper ledger.

@author cylovelife
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from contextlib import closing
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from quant_agent.application.paper.ledger import (
    LedgerConflict,
    LedgerCorruption,
    _audit,
    _identifier,
    _json,
    _minor,
    _snapshot,
)
from quant_agent.domain.trading.execution import _day, _identity
from quant_agent.infrastructure.persistence.store import _paper_path, paper_connection

ZONE = ZoneInfo("Asia/Shanghai")
TABLE = "paper_runtime_transitions"
DDL = (
    """CREATE TABLE IF NOT EXISTS paper_runtime_transitions (
        experiment_id TEXT NOT NULL REFERENCES paper_experiments(experiment_id),
        sequence INTEGER NOT NULL CHECK(sequence>0), transition_id TEXT NOT NULL UNIQUE,
        payload_json TEXT NOT NULL, runtime_id TEXT NOT NULL UNIQUE,
        PRIMARY KEY(experiment_id,sequence))""",
    """CREATE TRIGGER IF NOT EXISTS paper_runtime_no_update BEFORE UPDATE ON paper_runtime_transitions
        BEGIN SELECT RAISE(ABORT,'runtime handoff is immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS paper_runtime_no_delete BEFORE DELETE ON paper_runtime_transitions
        BEGIN SELECT RAISE(ABORT,'runtime handoff is immutable'); END""",
)


def _quoted(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _prefix(conn: sqlite3.Connection) -> list[dict]:
    """Record rowid watermarks and hashes, so future appends do not hide old edits."""
    tables = conn.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    result = []
    for table in tables:
        name = table["name"]
        columns = [row[1] for row in conn.execute(f"PRAGMA table_info({_quoted(name)})")]
        watermark = conn.execute(f"SELECT COALESCE(MAX(rowid),0) FROM {_quoted(name)}").fetchone()[
            0
        ]
        rows = [
            list(row)
            for row in conn.execute(
                f"SELECT {','.join(map(_quoted, columns))} FROM {_quoted(name)} WHERE rowid<=? ORDER BY rowid",
                (watermark,),
            )
        ]
        result.append(
            {
                "table": name,
                "sql": table["sql"],
                "columns": columns,
                "watermark": watermark,
                "rows": len(rows),
                "sha256": _identity(rows),
            }
        )
    return result


def _verify_prefix(conn: sqlite3.Connection, prefix: list[dict]) -> None:
    for item in prefix:
        name = item["table"]
        schema = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        if schema is None or schema[0] != item["sql"]:
            raise LedgerCorruption("inherited ledger prefix schema differs")
        rows = [
            list(row)
            for row in conn.execute(
                f"SELECT {','.join(map(_quoted, item['columns']))} FROM {_quoted(name)} WHERE rowid<=? ORDER BY rowid",
                (item["watermark"],),
            )
        ]
        if len(rows) != item["rows"] or _identity(rows) != item["sha256"]:
            raise LedgerCorruption("inherited ledger prefix evidence differs")


def _origin(conn: sqlite3.Connection, experiment_id: str) -> tuple[dict, str]:
    row = conn.execute(
        "SELECT manifest_json,manifest_hash FROM paper_experiments WHERE experiment_id=?",
        (experiment_id,),
    ).fetchone()
    if row is None:
        raise LedgerConflict("unknown experiment")
    manifest = json.loads(row["manifest_json"])
    if _identity(manifest) != row["manifest_hash"]:
        raise LedgerCorruption("experiment origin evidence differs")
    return manifest, row["manifest_hash"]


def resolve_runtime(conn: sqlite3.Connection, manifest: dict) -> dict:
    """Return an enriched view; the original registration is never updated."""
    original = {key: value for key, value in manifest.items() if key != "runtime"}
    origin_id = _identity(original)
    active = {
        "runtime_id": origin_id,
        "sequence": 0,
        "transition_id": None,
        "code_sha256": original["code_sha256"],
        "python_version": original["python_version"],
        "effective_from": original["spec"]["start_date"],
        "registered_at": original["registered_at"],
    }
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
    ).fetchone():
        return {**original, "runtime": active}
    records = conn.execute(
        "SELECT sequence,transition_id,payload_json,runtime_id FROM paper_runtime_transitions WHERE experiment_id=? ORDER BY sequence",
        (original["spec"]["experiment_id"],),
    ).fetchall()
    for row in records:
        payload = json.loads(row["payload_json"])
        if (
            _identity(payload) != row["runtime_id"]
            or payload.get("origin_id") != origin_id
            or payload.get("experiment_id") != original["spec"]["experiment_id"]
            or payload.get("previous_runtime_id") != active["runtime_id"]
            or payload.get("sequence") != active["sequence"] + 1
            or row["sequence"] != payload["sequence"]
            or row["transition_id"] != payload.get("transition_id")
        ):
            raise LedgerCorruption("runtime handoff chain evidence differs")
        if active["sequence"] and payload["effective_from"] <= active["effective_from"]:
            raise LedgerCorruption("runtime handoff dates are not increasing")
        if _identity(payload["inherited_prefix"]) != payload["source_digest"]:
            raise LedgerCorruption("runtime source prefix evidence differs")
        _verify_prefix(conn, payload["inherited_prefix"])
        active = {**payload, "runtime_id": row["runtime_id"]}
    return {**original, "runtime": active}


def _checkpoint(conn: sqlite3.Connection, manifest: dict) -> dict:
    ids = [manifest["spec"]["account_id"], manifest["spec"]["benchmark_id"]]
    accounts = {row[0] for row in conn.execute("SELECT account_id FROM paper_accounts")}
    experiments = conn.execute("SELECT COUNT(*) FROM paper_experiments").fetchone()[0]
    if accounts != set(ids) or experiments != 1:
        raise LedgerConflict("runtime handoff requires an isolated single-experiment ledger")
    result = {}
    flows = []
    has_daily = bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_daily'"
        ).fetchone()
    )
    for account_id in ids:
        state = _audit(conn, account_id)
        records = (
            conn.execute(
                "SELECT day,request_hash,request_json,result_hash,result_json FROM paper_daily WHERE account_id=? ORDER BY day",
                (account_id,),
            ).fetchall()
            if has_daily
            else []
        )
        for row in records:
            if (
                _identity(json.loads(row["request_json"])) != row["request_hash"]
                or _identity(json.loads(row["result_json"])) != row["result_hash"]
            ):
                raise LedgerCorruption("sealed daily evidence differs")
        dates = [row["day"] for row in records]
        last = dates[-1] if dates else None
        if any(
            order["status"] == "unavailable" or last is None or order["execution_date"] > last
            for order in state.orders
        ):
            raise LedgerConflict("unsettled orders prevent runtime handoff")
        actions = [
            json.loads(row[0])
            for row in conn.execute(
                "SELECT intent_json FROM paper_actions WHERE account_id=? ORDER BY action_date,sequence",
                (account_id,),
            )
        ]
        if any(last is None or action["date"] > last for action in actions):
            raise LedgerConflict("unsettled actions prevent runtime handoff")
        flows.append(
            [
                (a["date"], a["phase"], a["kind"], a["amount"])
                for a in actions
                if a["kind"] in {"deposit", "withdrawal"}
            ]
        )
        result[account_id] = {
            "snapshot": _snapshot(state),
            "dates": dates,
            "last_result_hash": records[-1]["result_hash"] if records else None,
        }
    if result[ids[0]]["dates"] != result[ids[1]]["dates"] or flows[0] != flows[1]:
        raise LedgerConflict("benchmark dates or external cash flows differ")
    for key in ("opening_cash", "currency", "start_date"):
        if result[ids[0]]["snapshot"][key] != result[ids[1]]["snapshot"][key]:
            raise LedgerCorruption("account origins differ")
    origin = result[ids[0]]["snapshot"]
    spec = manifest["spec"]
    if (
        _minor(origin["opening_cash"]) != _minor(spec["opening_cash"])
        or origin["currency"] != spec["currency"]
        or origin["start_date"] != spec["start_date"]
    ):
        raise LedgerCorruption("account origin differs from frozen experiment")
    return result


def _validate_effective(
    manifest: dict, checkpoint: dict, effective: str, calendar: dict, now: datetime
) -> None:
    effective = _day(effective)
    today = now.date().isoformat()
    if effective < today or (effective == today and now.hour >= 16):
        raise LedgerConflict("runtime handoff cannot backdate a completed session")
    if effective == today == manifest["spec"]["first_trade_date"] and (
        now.hour,
        now.minute,
    ) >= (9, 15):
        raise LedgerConflict("runtime must be frozen before the first opening trade")
    if (
        not isinstance(calendar, dict)
        or set(calendar) != {"source", "sessions"}
        or calendar["source"] != manifest["spec"]["data_sources"]["calendar"]
        or not isinstance(calendar["sessions"], list)
    ):
        raise ValueError("handoff requires the frozen calendar source and declared sessions")
    sessions = [_day(day) for day in calendar["sessions"]]
    if sessions != sorted(set(sessions)) or effective not in sessions:
        raise ValueError("handoff calendar must include its effective session")
    dates = checkpoint[manifest["spec"]["account_id"]]["dates"]
    if not dates:
        if effective > manifest["spec"]["start_date"]:
            raise LedgerConflict("unsettled start day before handoff")
    else:
        last = dates[-1]
        if last not in sessions or effective <= last:
            raise LedgerConflict("handoff cannot cover sealed sessions")
        if any(last < day < effective for day in sessions):
            raise LedgerConflict("unsettled session gap before handoff")
    runtime = manifest["runtime"]
    if runtime["sequence"] and effective <= runtime["effective_from"]:
        raise LedgerConflict("runtime effective dates must increase")
    if now < datetime.fromisoformat(runtime["registered_at"]):
        raise LedgerConflict("handoff registration cannot predate the prior runtime")


def transition_copy(
    source: str | Path,
    target: str | Path,
    experiment_id: str,
    transition_id: str,
    effective_from: str,
    reason: str,
    calendar: dict,
    *,
    now: datetime | None = None,
) -> dict:
    """Publish a new ledger copy with one explicit runtime revision, never reinject cash."""
    from quant_agent.application.paper.experiment import code_fingerprints

    source, target = _paper_path(source), _paper_path(target)
    if source == target:
        raise ValueError("runtime handoff requires a new ledger copy")
    _identifier(experiment_id)
    _identifier(transition_id)
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("runtime handoff requires a reason")
    now = now or datetime.now(ZONE)
    if now.tzinfo is None:
        raise ValueError("runtime handoff requires timezone-aware registration")
    now = now.astimezone(ZONE)
    request = {
        "source": str(source),
        "target": str(target),
        "experiment_id": experiment_id,
        "transition_id": transition_id,
        "effective_from": effective_from,
        "reason": reason,
        "calendar": calendar,
    }
    code = code_fingerprints()
    with paper_connection(source) as origin_conn:
        original, origin_id = _origin(origin_conn, experiment_id)
        manifest = resolve_runtime(origin_conn, original)
        prefix = _prefix(origin_conn)
        intent = _identity(
            {
                **request,
                "source_digest": _identity(prefix),
                "code_sha256": code,
                "python_version": sys.version,
            }
        )

        def existing() -> dict:
            with paper_connection(target) as conn:
                recorded, _ = _origin(conn, experiment_id)
                resolved = resolve_runtime(conn, recorded)
                row = conn.execute(
                    "SELECT payload_json,runtime_id FROM paper_runtime_transitions WHERE transition_id=?",
                    (transition_id,),
                ).fetchone()
                if row is None:
                    raise LedgerConflict("target does not contain this handoff")
                payload = json.loads(row["payload_json"])
                if payload["intent_hash"] != intent:
                    raise LedgerConflict("handoff target or runtime intent differs")
                _checkpoint(conn, resolved)
                return {
                    "path": str(target),
                    "runtime_id": row["runtime_id"],
                    "transition": payload,
                    "replayed": True,
                }

        if target.exists():
            return existing()
        checkpoint = _checkpoint(origin_conn, manifest)
        _validate_effective(manifest, checkpoint, effective_from, calendar, now)
        previous = manifest["runtime"]
        if code == previous["code_sha256"] and sys.version == previous["python_version"]:
            raise LedgerConflict("no runtime change to register")
        payload = {
            "format": 1,
            "experiment_id": experiment_id,
            "transition_id": transition_id,
            "sequence": previous["sequence"] + 1,
            "origin_id": origin_id,
            "previous_runtime_id": previous["runtime_id"],
            "effective_from": effective_from,
            "registered_at": now.isoformat(),
            "reason": reason,
            "calendar": calendar,
            "source_path": str(source),
            "source_digest": _identity(prefix),
            "inherited_prefix": prefix,
            "checkpoint": checkpoint,
            "intent_hash": intent,
            "code_sha256": code,
            "python_version": sys.version,
        }
        runtime_id = _identity(payload)
        with tempfile.TemporaryDirectory(prefix=".paper-runtime-", dir=target.parent) as staging:
            candidate = Path(staging) / "ledger.db"
            with closing(sqlite3.connect(candidate)) as copy:
                os.chmod(candidate, 0o600)
                origin_conn.backup(copy)
            with paper_connection(candidate, write=True) as conn:
                _verify_prefix(conn, prefix)
                for sql in DDL:
                    conn.execute(sql)
                conn.execute(
                    "INSERT INTO paper_runtime_transitions (experiment_id,sequence,transition_id,payload_json,runtime_id) VALUES (?,?,?,?,?)",
                    (
                        experiment_id,
                        payload["sequence"],
                        transition_id,
                        _json(payload),
                        runtime_id,
                    ),
                )
                resolved = resolve_runtime(conn, original)
                if _checkpoint(conn, resolved) != checkpoint:
                    raise LedgerCorruption("handoff copy changed account checkpoint")
                if code_fingerprints() != code:
                    raise LedgerConflict("code changed while preparing handoff")
            try:
                os.link(candidate, target)
            except FileExistsError:
                return existing()
        return {
            "path": str(target),
            "runtime_id": runtime_id,
            "transition": payload,
            "replayed": False,
        }


def runtime_status(manifest: dict, *, now: datetime | None = None) -> dict:
    from quant_agent.application.paper.experiment import code_fingerprints

    active = manifest.get("runtime") or {
        "runtime_id": _identity(manifest),
        "sequence": 0,
        "code_sha256": manifest["code_sha256"],
        "python_version": manifest["python_version"],
        "effective_from": manifest["spec"]["start_date"],
        "transition_id": None,
    }
    code_matches = code_fingerprints() == active["code_sha256"]
    python_matches = sys.version == active["python_version"]
    local = now or datetime.now(ZONE)
    if local.tzinfo is None:
        raise ValueError("runtime readiness requires timezone-aware time")
    local = local.astimezone(ZONE)
    effective = active["effective_from"] <= local.date().isoformat()
    ready = code_matches and python_matches and effective
    return {
        "runtime_id": active["runtime_id"],
        "sequence": active["sequence"],
        "transition_id": active["transition_id"],
        "effective_from": active["effective_from"],
        "code_matches": code_matches,
        "python_matches": python_matches,
        "activation_state": "active" if effective else "scheduled",
        "ready": ready,
        "can_settle_current_day": ready and local.hour >= 16,
    }
