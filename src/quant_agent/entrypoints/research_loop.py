"""Explicit research requests and a private, phase-only subprocess worker.

@author cylovelife
"""

import json
import sys
from pathlib import Path


def main(request_path: str) -> int:
    from quant_agent.application.research.controlled_loop import ResearchLoop, register

    try:
        path = Path(request_path).resolve()
        request = json.loads(path.read_text())
        operation = request["operation"]
        required = {
            "operation",
            "registry",
            "manifest" if operation == "register" else "experiment_id",
        }
        if set(request) != required:
            raise ValueError("unknown or missing research request fields")
        registry = (path.parent / request["registry"]).resolve()
        if operation == "register":
            result = register(registry, path.parent / request["manifest"])
        else:
            loop = ResearchLoop(registry)
            methods = {
                "validate": loop.validate,
                "lock": loop.lock,
                "test": loop.test,
                "status": loop.registry.inspect,
            }
            if operation not in methods:
                raise ValueError("unknown research operation")
            result = methods[operation](request["experiment_id"])
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0 if result.get("outcome") != "failed" else 2
    except Exception as exc:
        print(
            json.dumps({"error_type": type(exc).__name__, "error": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2


def worker() -> int:
    from quant_agent.application.research.loop_account import evaluate_account
    from quant_agent.application.research.loop_data import check_phase, read_artifact

    try:
        payload = json.load(sys.stdin)
        data = read_artifact(payload["artifact"])
        check_phase(data, payload["phase"], payload["split"])
        if data["instruments"] != payload["instruments"]:
            raise ValueError("phase universe differs from registration")
        accounts = [
            evaluate_account(data, item, payload["policy"], payload["initial_cash"])
            for item in payload["candidates"]
        ]
        print(
            json.dumps({"accounts": accounts, "candidates": payload["candidates"]}, allow_nan=False)
        )
        return 0
    except Exception as exc:
        print(json.dumps({"error_type": type(exc).__name__, "error": str(exc)}, allow_nan=False))
        return 2


if __name__ == "__main__":
    if sys.argv[1:] != ["--worker"]:
        raise SystemExit("use quant-agent --research-loop REQUEST_JSON")
    raise SystemExit(worker())
