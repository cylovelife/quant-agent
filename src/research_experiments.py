"""Immutable research registrations, validation candidates and single-use tests.

@author cylovelife
"""

from __future__ import annotations

import json
import os
import platform
import sqlite3
import sys
from collections.abc import Callable
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from importlib import metadata
from pathlib import Path

from data_snapshot import canonical_json, digest, price_dataset_id, read_snapshot
from daily_research import code_signature
from temporal_validation import configured_boundaries

APP_ID = 0x51524531
ROOT = Path(__file__).resolve().parents[1]
DDL = (
    """CREATE TABLE experiments (experiment_id TEXT PRIMARY KEY, identity_json TEXT NOT NULL,
       test_start TEXT NOT NULL, test_end TEXT NOT NULL, created_at TEXT NOT NULL)""",
    """CREATE TABLE candidates (experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id),
       candidate_id TEXT NOT NULL, definition_json TEXT NOT NULL, created_at TEXT NOT NULL,
       PRIMARY KEY(experiment_id,candidate_id))""",
    """CREATE TABLE experiment_locks (experiment_id TEXT PRIMARY KEY REFERENCES experiments(experiment_id),
       candidate_id TEXT NOT NULL, created_at TEXT NOT NULL,
       FOREIGN KEY(experiment_id,candidate_id) REFERENCES candidates(experiment_id,candidate_id))""",
    """CREATE TABLE experiment_attempts (attempt_id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL,
       candidate_id TEXT NOT NULL, phase TEXT NOT NULL CHECK(phase IN ('validation','test')),
       started_at TEXT NOT NULL, FOREIGN KEY(experiment_id,candidate_id) REFERENCES candidates(experiment_id,candidate_id))""",
    """CREATE UNIQUE INDEX one_test_per_experiment ON experiment_attempts(experiment_id) WHERE phase='test' """,
    """CREATE TABLE experiment_results (attempt_id TEXT PRIMARY KEY REFERENCES experiment_attempts(attempt_id),
       outcome TEXT NOT NULL CHECK(outcome IN ('succeeded','failed')), result_json TEXT NOT NULL,
       elapsed_seconds REAL NOT NULL, finished_at TEXT NOT NULL)""",
    *tuple(
        f"""CREATE TRIGGER {name}_no_{op.lower()} BEFORE {op} ON {name}
       BEGIN SELECT RAISE(ABORT,'research evidence is append-only'); END"""
        for name in (
            "experiments",
            "candidates",
            "experiment_locks",
            "experiment_attempts",
            "experiment_results",
        )
        for op in ("UPDATE", "DELETE")
    ),
)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _identifier(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ValueError("id must be a nonempty string of at most 128 characters")


def _package(name):
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def _environment():
    lock = ROOT / "requirements.lock"
    return {
        "python": sys.version,
        "platform": platform.platform(),
        "numpy": _package("numpy"),
        "pandas": _package("pandas"),
        "scikit_learn": _package("scikit-learn"),
        "requirements_lock_sha256": digest(lock.read_bytes())
        if lock.exists()
        else None,
    }


def _json(value):
    return canonical_json(value).decode("utf-8")


class ExperimentRegistry:
    """Separate explicit SQLite file; creation never overwrites another database."""

    def __init__(self, path):
        self.path = Path(path).resolve()
        with self.connection():
            pass

    @classmethod
    def create(cls, path):
        target = Path(path).resolve()
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        try:
            with closing(sqlite3.connect(target, isolation_level=None)) as conn:
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("BEGIN IMMEDIATE")
                for sql in DDL:
                    conn.execute(sql)
                conn.execute(f"PRAGMA application_id={APP_ID}")
                conn.execute("PRAGMA user_version=1")
                conn.commit()
            return cls(target)
        except BaseException:
            target.unlink(missing_ok=True)
            raise

    @contextmanager
    def connection(self, write=False):
        if not self.path.is_file():
            raise FileNotFoundError(self.path)
        with closing(
            sqlite3.connect(
                self.path.as_uri() + ("?mode=rw" if write else "?mode=ro"),
                uri=True,
                isolation_level=None,
                timeout=30,
            )
        ) as conn:
            conn.row_factory = sqlite3.Row
            if (
                conn.execute("PRAGMA application_id").fetchone()[0] != APP_ID
                or conn.execute("PRAGMA user_version").fetchone()[0] != 1
            ):
                raise ValueError("unsupported research experiment registry")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            try:
                if write:
                    conn.execute("""CREATE TABLE IF NOT EXISTS managed_attempt_results (
                        attempt_id TEXT PRIMARY KEY REFERENCES experiment_results(attempt_id))""")
                    for op in ("UPDATE", "DELETE"):
                        conn.execute(f"""CREATE TRIGGER IF NOT EXISTS managed_results_no_{op.lower()}
                            BEFORE {op} ON managed_attempt_results
                            BEGIN SELECT RAISE(ABORT,'research evidence is append-only'); END""")
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def register(self, snapshot_path, spec):
        """Freeze actual local snapshot and complete protocol; no candidate or score is created."""
        required = {
            "universe",
            "parameters",
            "features",
            "label",
            "cost",
            "split",
            "seed",
            "budget",
        }
        if not isinstance(spec, dict) or set(spec) != required:
            raise ValueError("missing or unknown experiment fields")
        if (
            not isinstance(spec["features"], list)
            or not spec["features"]
            or any(not isinstance(f, str) or not f.strip() for f in spec["features"])
            or len(set(spec["features"])) != len(spec["features"])
        ):
            raise ValueError("features must be unique nonempty names")
        if not isinstance(spec["seed"], int) or isinstance(spec["seed"], bool):
            raise ValueError("seed must be an integer")
        for key in ("parameters", "label", "cost"):
            if not isinstance(spec[key], dict) or not spec[key]:
                raise ValueError(f"{key} must be an explicit nonempty object")
        split = configured_boundaries({"temporal_validation": spec["split"]})
        if split is None:
            raise ValueError("fixed completed date boundaries required")
        budget = spec["budget"]
        if not isinstance(budget, dict) or set(budget) != {
            "max_candidates",
            "max_attempts",
            "max_attempt_seconds",
        }:
            raise ValueError("budget requires candidate, attempt and duration limits")
        if any(
            isinstance(budget[k], bool)
            or not isinstance(budget[k], int)
            or budget[k] <= 0
            for k in budget
        ):
            raise ValueError("budget values must be positive integers")
        snapshot = Path(snapshot_path).resolve()
        frames = read_snapshot(snapshot)
        actual = sorted([list(key) for key in frames])
        if not isinstance(spec["universe"], list) or sorted(spec["universe"]) != actual:
            raise ValueError(
                "declared universe must match snapshot including adjustment"
            )
        dataset = price_dataset_id(frames)
        identity = {
            "format": 1,
            "snapshot_id": snapshot.name,
            "snapshot_path": str(snapshot),
            "price_dataset_id": dataset,
            "code_id": code_signature(),
            "environment": _environment(),
            **spec,
        }
        eid = digest(canonical_json(identity))
        with self.connection(write=True) as conn:
            old = conn.execute(
                "SELECT identity_json FROM experiments WHERE experiment_id=?", (eid,)
            ).fetchone()
            if old:
                if json.loads(old["identity_json"]) != identity:
                    raise RuntimeError("experiment identity mismatch")
                return {
                    "experiment_id": eid,
                    "identity": identity,
                    "replayed": True,
                    "promotion_allowed": False,
                }
            overlap = conn.execute(
                "SELECT experiment_id FROM experiments WHERE test_start<=? AND test_end>=? LIMIT 1",
                (split["test_end"], split["test_start"]),
            ).fetchone()
            if overlap:
                raise ValueError(
                    "test window overlaps another registered experiment in this registry"
                )
            conn.execute(
                "INSERT INTO experiments VALUES (?,?,?,?,?)",
                (eid, _json(identity), split["test_start"], split["test_end"], _now()),
            )
        return {
            "experiment_id": eid,
            "identity": identity,
            "replayed": False,
            "promotion_allowed": False,
        }

    def _experiment(self, conn, eid):
        row = conn.execute(
            "SELECT identity_json FROM experiments WHERE experiment_id=?", (eid,)
        ).fetchone()
        if not row:
            raise ValueError("unknown experiment")
        identity = json.loads(row["identity_json"])
        if digest(canonical_json(identity)) != eid:
            raise ValueError("corrupted experiment identity")
        return identity

    def _check_runtime(self, identity):
        if (
            code_signature() != identity["code_id"]
            or _environment() != identity["environment"]
        ):
            raise ValueError("frozen code or environment differs")
        path = Path(identity["snapshot_path"])
        frames = read_snapshot(path)
        if (
            path.name != identity["snapshot_id"]
            or price_dataset_id(frames) != identity["price_dataset_id"]
        ):
            raise ValueError("frozen dataset differs")

    def add_candidate(self, eid, definition):
        if not isinstance(definition, dict) or not definition:
            raise ValueError("candidate definition must be a nonempty object")
        cid = digest(canonical_json(definition))
        with self.connection(write=True) as conn:
            identity = self._experiment(conn, eid)
            self._check_runtime(identity)
            old = conn.execute(
                "SELECT definition_json FROM candidates WHERE experiment_id=? AND candidate_id=?",
                (eid, cid),
            ).fetchone()
            if old:
                if json.loads(old["definition_json"]) != definition:
                    raise ValueError("candidate identity mismatch")
                return {
                    "candidate_id": cid,
                    "replayed": True,
                    "promotion_allowed": False,
                }
            if conn.execute(
                "SELECT 1 FROM experiment_locks WHERE experiment_id=?", (eid,)
            ).fetchone():
                raise ValueError("candidate search closed before test")
            attempts = conn.execute(
                "SELECT count(*) FROM experiment_attempts WHERE experiment_id=?", (eid,)
            ).fetchone()[0]
            overrun = conn.execute(
                """SELECT 1 FROM experiment_results r JOIN experiment_attempts a ON a.attempt_id=r.attempt_id
                   WHERE a.experiment_id=? AND r.elapsed_seconds>? LIMIT 1""",
                (eid, identity["budget"]["max_attempt_seconds"]),
            ).fetchone()
            if overrun or attempts >= identity["budget"]["max_attempts"]:
                raise ValueError("attempt budget exhausted")
            count = conn.execute(
                "SELECT count(*) FROM candidates WHERE experiment_id=?", (eid,)
            ).fetchone()[0]
            if count >= identity["budget"]["max_candidates"]:
                raise ValueError("candidate budget exhausted")
            conn.execute(
                "INSERT INTO candidates VALUES (?,?,?,?)",
                (eid, cid, _json(definition), _now()),
            )
        return {"candidate_id": cid, "replayed": False, "promotion_allowed": False}

    def begin(self, eid, cid, attempt_id, phase):
        _identifier(attempt_id)
        if phase not in {"validation", "test"}:
            raise ValueError("phase must be validation or test")
        with self.connection(write=True) as conn:
            identity = self._experiment(conn, eid)
            self._check_runtime(identity)
            prior = conn.execute(
                "SELECT experiment_id,candidate_id,phase FROM experiment_attempts WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if prior:
                if tuple(prior) != (eid, cid, phase):
                    raise ValueError("attempt id conflicts with frozen intent")
                return {"attempt_id": attempt_id, "replayed": True}
            if not conn.execute(
                "SELECT 1 FROM candidates WHERE experiment_id=? AND candidate_id=?",
                (eid, cid),
            ).fetchone():
                raise ValueError("candidate must be registered first")
            locked = conn.execute(
                "SELECT candidate_id FROM experiment_locks WHERE experiment_id=?",
                (eid,),
            ).fetchone()
            if phase == "validation" and locked:
                raise ValueError("validation search is closed")
            if phase == "test" and (not locked or locked["candidate_id"] != cid):
                raise ValueError("test requires the previously locked candidate")
            pending = conn.execute(
                """SELECT 1 FROM experiment_attempts a LEFT JOIN experiment_results r ON r.attempt_id=a.attempt_id
                                    WHERE a.experiment_id=? AND r.attempt_id IS NULL LIMIT 1""",
                (eid,),
            ).fetchone()
            if pending:
                raise ValueError("previous attempt is unfinished")
            count = conn.execute(
                "SELECT count(*) FROM experiment_attempts WHERE experiment_id=?", (eid,)
            ).fetchone()[0]
            if count >= identity["budget"]["max_attempts"]:
                raise ValueError("attempt budget exhausted")
            if conn.execute(
                """SELECT 1 FROM experiment_results r JOIN experiment_attempts a ON a.attempt_id=r.attempt_id
                               WHERE a.experiment_id=? AND r.elapsed_seconds>? LIMIT 1""",
                (eid, identity["budget"]["max_attempt_seconds"]),
            ).fetchone():
                raise ValueError("a previous attempt exceeded the time budget")
            if (
                phase == "test"
                and conn.execute(
                    "SELECT 1 FROM experiment_attempts WHERE experiment_id=? AND phase='test'",
                    (eid,),
                ).fetchone()
            ):
                raise ValueError("test has already been consumed")
            conn.execute(
                "INSERT INTO experiment_attempts VALUES (?,?,?,?,?)",
                (attempt_id, eid, cid, phase, _now()),
            )
        return {"attempt_id": attempt_id, "replayed": False}

    def finish(self, attempt_id, outcome, details):
        return self._finish(attempt_id, outcome, details, managed=False)

    def _finish(self, attempt_id, outcome, details, *, managed):
        if outcome not in {"succeeded", "failed"} or not isinstance(details, dict):
            raise ValueError("outcome and details are required")
        with self.connection(write=True) as conn:
            row = conn.execute(
                "SELECT started_at FROM experiment_attempts WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if not row:
                raise ValueError("attempt not registered")
            old = conn.execute(
                "SELECT outcome,result_json,elapsed_seconds FROM experiment_results WHERE attempt_id=?",
                (attempt_id,),
            ).fetchone()
            if old:
                if (
                    managed
                    and not conn.execute(
                        "SELECT 1 FROM managed_attempt_results WHERE attempt_id=?",
                        (attempt_id,),
                    ).fetchone()
                ):
                    raise ValueError("existing result is not a managed attempt")
                if (
                    old["outcome"] != outcome
                    or json.loads(old["result_json"]) != details
                ):
                    raise ValueError("attempt result conflicts with previous result")
                return {
                    "attempt_id": attempt_id,
                    "outcome": outcome,
                    "elapsed_seconds": old["elapsed_seconds"],
                    "replayed": True,
                    "promotion_allowed": False,
                }
            end = _now()
            elapsed = max(
                0.0,
                (
                    datetime.fromisoformat(end)
                    - datetime.fromisoformat(row["started_at"])
                ).total_seconds(),
            )
            conn.execute(
                "INSERT INTO experiment_results VALUES (?,?,?,?,?)",
                (attempt_id, outcome, _json(details), elapsed, end),
            )
            if managed:
                conn.execute(
                    "INSERT INTO managed_attempt_results VALUES (?)", (attempt_id,)
                )
        return {
            "attempt_id": attempt_id,
            "outcome": outcome,
            "elapsed_seconds": elapsed,
            "replayed": False,
            "promotion_allowed": False,
        }

    def run_attempt(
        self,
        eid: str,
        cid: str,
        attempt_id: str,
        phase: str,
        operation: Callable[[], dict],
    ) -> dict:
        """Run once after durable admission; record failures and never rerun an ID."""
        admitted = self.begin(eid, cid, attempt_id, phase)
        if admitted["replayed"]:
            with self.connection() as conn:
                row = conn.execute(
                    """SELECT r.outcome,r.result_json,m.attempt_id AS managed
                       FROM experiment_results r LEFT JOIN managed_attempt_results m
                       ON m.attempt_id=r.attempt_id WHERE r.attempt_id=?""",
                    (attempt_id,),
                ).fetchone()
            if row is None:
                raise ValueError("attempt is unfinished; resolve it explicitly")
            if row["managed"] is None:
                raise ValueError("existing result is not a managed attempt")
            return {
                "attempt_id": attempt_id,
                "outcome": row["outcome"],
                "details": json.loads(row["result_json"]),
                "replayed": True,
            }
        try:
            details = operation()
            if not isinstance(details, dict):
                raise ValueError("attempt must return an object")
            _json(details)
            with self.connection() as conn:
                identity = self._experiment(conn, eid)
            self._check_runtime(identity)
        except Exception as exc:
            self._finish(
                attempt_id,
                "failed",
                {"error_type": type(exc).__name__, "error": str(exc)},
                managed=True,
            )
            raise
        result = self._finish(attempt_id, "succeeded", details, managed=True)
        return {**result, "details": details}

    def lock_candidate(self, eid, cid):
        with self.connection(write=True) as conn:
            identity = self._experiment(conn, eid)
            self._check_runtime(identity)
            old = conn.execute(
                "SELECT candidate_id FROM experiment_locks WHERE experiment_id=?",
                (eid,),
            ).fetchone()
            if old:
                if old["candidate_id"] != cid:
                    raise ValueError("test candidate is already locked")
                return {"candidate_id": cid, "replayed": True}
            success = conn.execute(
                """SELECT 1 FROM experiment_attempts a JOIN experiment_results r ON r.attempt_id=a.attempt_id
                WHERE a.experiment_id=? AND a.candidate_id=? AND a.phase='validation' AND r.outcome='succeeded' AND r.elapsed_seconds<=? LIMIT 1""",
                (eid, cid, identity["budget"]["max_attempt_seconds"]),
            ).fetchone()
            pending = conn.execute(
                """SELECT 1 FROM experiment_attempts a LEFT JOIN experiment_results r ON a.attempt_id=r.attempt_id
                                    WHERE a.experiment_id=? AND r.attempt_id IS NULL LIMIT 1""",
                (eid,),
            ).fetchone()
            if not success or pending:
                raise ValueError(
                    "successful validation and no pending attempts required"
                )
            count = conn.execute(
                "SELECT count(*) FROM experiment_attempts WHERE experiment_id=?", (eid,)
            ).fetchone()[0]
            overrun = conn.execute(
                """SELECT 1 FROM experiment_results r JOIN experiment_attempts a ON a.attempt_id=r.attempt_id
                   WHERE a.experiment_id=? AND r.elapsed_seconds>? LIMIT 1""",
                (eid, identity["budget"]["max_attempt_seconds"]),
            ).fetchone()
            if overrun or count >= identity["budget"]["max_attempts"]:
                raise ValueError("budget exhausted before test lock")
            conn.execute(
                "INSERT INTO experiment_locks VALUES (?,?,?)", (eid, cid, _now())
            )
        return {"candidate_id": cid, "replayed": False}

    def inspect(self, eid):
        with self.connection() as conn:
            identity = self._experiment(conn, eid)
            candidates = [
                dict(r)
                for r in conn.execute(
                    "SELECT candidate_id,definition_json,created_at FROM candidates WHERE experiment_id=? ORDER BY created_at,candidate_id",
                    (eid,),
                )
            ]
            attempts = [
                dict(r)
                for r in conn.execute(
                    """SELECT a.attempt_id,a.candidate_id,a.phase,a.started_at,r.outcome,r.result_json,r.elapsed_seconds,r.finished_at
                          FROM experiment_attempts a LEFT JOIN experiment_results r ON r.attempt_id=a.attempt_id
                          WHERE a.experiment_id=? ORDER BY a.started_at,a.attempt_id""",
                    (eid,),
                )
            ]
            lock = conn.execute(
                "SELECT candidate_id FROM experiment_locks WHERE experiment_id=?",
                (eid,),
            ).fetchone()
        return {
            "experiment_id": eid,
            "identity": identity,
            "candidates": candidates,
            "attempts": attempts,
            "locked_candidate": lock["candidate_id"] if lock else None,
            "independent_new_samples": 0,
            "promotion_allowed": False,
        }
