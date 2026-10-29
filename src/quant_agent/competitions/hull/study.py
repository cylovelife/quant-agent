"""Freeze real Hull file audit and development-only constant allocations.

@author cylovelife
"""

import argparse
import hashlib
import importlib.metadata
import json
import logging
import time
from pathlib import Path

from quant_agent.competitions.hull.data import load_dataset, mature_window
from quant_agent.competitions.hull.metrics import constant_scores

LOGGER = logging.getLogger(__name__)


def write_new_json(path: Path, value: dict) -> None:
    with path.open("x") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def run_study(raw: Path, manifest: Path, protocol: Path, output: Path) -> dict:
    """Read real inputs only; no model fitting, reserved scoring or online submission."""
    started = time.perf_counter()
    output.mkdir(parents=True, exist_ok=False)
    try:
        plan = json.loads(protocol.read_text())
        if plan["schema_version"] != 1 or plan["reserved_window_evaluation_allowed"]:
            raise ValueError("unsupported protocol or reserved scoring enabled")
        data = load_dataset(raw, manifest)
        files = json.loads(manifest.read_text())["files"]
        train_sha = next(item["sha256"] for item in files if item["path"] == "train.csv")
        if (
            len(data.train) != plan["expected_training_rows"]
            or train_sha != plan["expected_train_sha256"]
        ):
            raise ValueError("protocol input identity mismatch")
        reserved_start, reserved_end = plan["reserved_window"]
        windows = plan["development_windows"]
        if len(windows) < 2 or not reserved_start < reserved_end:
            raise ValueError("two development windows and a valid reserved window required")
        if any(start >= end or end > reserved_start for start, end in windows):
            raise ValueError("development window crosses reserved boundary")
        ordered = sorted(windows)
        if any(a[1] > b[0] for a, b in zip(ordered, ordered[1:])):
            raise ValueError("development evaluation windows overlap")
        if (
            not (data.train.date_id >= reserved_start).any()
            or reserved_end <= data.train.date_id.max()
        ):
            raise ValueError("reserved window does not cover the latest observations")
        allocations = tuple(plan["allocations"])
        if allocations != (0.5, 1.0, 1.5):
            raise ValueError("only the three fixed allocation references are allowed")
        frozen = {
            "protocol": plan,
            "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
            "protocol_sha256": hashlib.sha256(protocol.read_bytes()).hexdigest(),
            "source_sha256": {
                p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(Path(__file__).parent.glob("*.py"))
            },
            "packages": {
                p: importlib.metadata.version(p)
                for p in ("numpy", "pandas", "polars", "grpcio", "protobuf", "pyarrow")
            },
        }
        write_new_json(output / "frozen.json", frozen)
        write_new_json(output / "audit.json", data.audit)
        records = []
        # Physically exclude the reserved segment from all scoring calls.
        development = data.train.loc[data.train.date_id < reserved_start].copy()
        for start, end in windows:
            fit, valid = mature_window(development, start, end)
            records.append(
                {
                    "window": [start, end],
                    "training_mature_rows": len(fit),
                    "scoring_mature_rows": len(valid),
                    "scoring_id_range": [int(valid.date_id.min()), int(valid.date_id.max())],
                    "constant_scores": constant_scores(valid, allocations),
                }
            )
        result = {
            "task": "historical_constant_references",
            "windows": records,
            "reserved_window": [reserved_start, reserved_end],
            "reserved_evaluations": 0,
            "model_fits": 0,
            "selected_model": None,
            "submitted": False,
            "elapsed_seconds": time.perf_counter() - started,
        }
        write_new_json(output / "results.json", result)
        return result
    except Exception as exc:
        write_new_json(output / "failure.json", {"type": type(exc).__name__, "message": str(exc)})
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("raw", "manifest", "protocol", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    result = run_study(args.raw, args.manifest, args.protocol, args.output)
    LOGGER.info(
        "development_windows=%d reserved_evaluations=0 output=%s",
        len(result["windows"]),
        args.output,
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
