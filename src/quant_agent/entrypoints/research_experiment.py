"""Explicit local research experiment ledger operations.

@author cylovelife
"""

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

from quant_agent.application.research.experiments import ExperimentRegistry


def main(request_path):
    try:
        path = Path(request_path).resolve()
        request = json.loads(path.read_text(encoding="utf-8"))
        op = request.get("operation")
        required = {
            "register": {"operation", "registry_path", "snapshot_path", "spec"},
            "candidate": {"operation", "registry_path", "experiment_id", "definition"},
            "begin": {
                "operation",
                "registry_path",
                "experiment_id",
                "candidate_id",
                "attempt_id",
                "phase",
            },
            "finish": {
                "operation",
                "registry_path",
                "attempt_id",
                "outcome",
                "details",
            },
            "lock": {"operation", "registry_path", "experiment_id", "candidate_id"},
            "status": {"operation", "registry_path", "experiment_id"},
        }
        if op not in required or set(request) != required[op]:
            raise ValueError("unsupported operation or invalid fields")

        def relative(value):
            target = Path(value)
            return target if target.is_absolute() else path.parent / target

        target = relative(request["registry_path"]).resolve()
        if op == "register" and not target.exists():
            with tempfile.TemporaryDirectory(
                prefix=".research-registry-", dir=target.parent
            ) as staging:
                candidate = ExperimentRegistry.create(Path(staging) / "registry.db")
                result = candidate.register(relative(request["snapshot_path"]), request["spec"])
                try:
                    os.link(candidate.path, target)
                    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
                    return 0
                except FileExistsError:
                    pass
        registry = ExperimentRegistry(target)
        if op == "register":
            result = registry.register(relative(request["snapshot_path"]), request["spec"])
        elif op == "candidate":
            result = registry.add_candidate(request["experiment_id"], request["definition"])
        elif op == "begin":
            result = registry.begin(
                request["experiment_id"],
                request["candidate_id"],
                request["attempt_id"],
                request["phase"],
            )
        elif op == "finish":
            result = registry.finish(request["attempt_id"], request["outcome"], request["details"])
        elif op == "lock":
            result = registry.lock_candidate(request["experiment_id"], request["candidate_id"])
        else:
            result = registry.inspect(request["experiment_id"])
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        sqlite3.Error,
        RuntimeError,
        ArithmeticError,
    ) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
