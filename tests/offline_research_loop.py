#!/usr/bin/env python3
"""Run the public research CLI on fresh synthetic artifacts and retain receipts.

@author cylovelife
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from research_loop_fixture import make_fixture

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path, help="new output directory only")
    output = parser.parse_args().output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    manifest = make_fixture(output / "fixture")
    env = os.environ.copy()
    env.update(PYTHONPATH=str(ROOT / "src"), QUANT_AGENT_HOME=str(output / "workspace"))
    eid = None
    receipts = {}
    for operation in ("register", "validate", "lock", "test", "status"):
        request = {"operation": operation, "registry": str(output / "registry.db")}
        if operation == "register":
            request["manifest"] = str(manifest)
        else:
            request["experiment_id"] = eid
        path = output / f"{operation}-request.json"
        path.write_text(json.dumps(request, sort_keys=True))
        completed = subprocess.run(
            [sys.executable, str(ROOT / "run.py"), "--research-loop", str(path)],
            env=env,
            cwd=output,
            check=True,
            text=True,
            capture_output=True,
            timeout=120,
        )
        receipt = json.loads(completed.stdout)
        (output / f"{operation}-receipt.json").write_text(
            json.dumps(receipt, indent=2, ensure_ascii=False)
        )
        receipts[operation] = receipt
        if operation == "register":
            eid = receipt["experiment_id"]
    details = receipts["test"]["details"]
    summary = {
        "experiment_id": eid,
        "selected": receipts["lock"]["scheme"],
        "selected_net_assets": details["selected"]["net_assets"],
        "default_net_assets": details["default_baseline"]["net_assets"],
        "cash_net_assets": details["cash_baseline"]["net_assets"],
        "attempts": len(receipts["status"]["attempts"]),
        "promotion_allowed": details["promotion_allowed"],
        "independent_new_samples": details["independent_new_samples"],
        "evidence_kind": "synthetic",
        "output": str(output),
    }
    assert summary["attempts"] == 4 and summary["promotion_allowed"] is False
    assert details["selected"]["reconciled"] is True
    (output / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
