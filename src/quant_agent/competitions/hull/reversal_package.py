"""Export the same policy as an offline standalone Kaggle script and replay locally.

@author cylovelife
"""

import ast
import inspect
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from quant_agent.competitions.hull import published_portfolio, reversal
from quant_agent.competitions.hull.metrics import validate_ids
from quant_agent.competitions.hull.reversal_study import RISK
from quant_agent.competitions.hull.study import write_new_json


def standalone_source(*, blocks: int, block_rows: int) -> str:
    """Remove only local import statements; retain the actual domain implementation."""
    bodies = [
        "# Generated from frozen own source; @author cylovelife\n",
        "import numpy as np\nimport pandas as pd\nfrom pandas.api.types import is_bool_dtype, is_integer_dtype\n",
        inspect.getsource(validate_ids),
    ]
    for module in (published_portfolio, reversal):
        parsed = ast.parse(inspect.getsource(module))
        parsed.body = [
            node
            for node in parsed.body
            if not (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.startswith("quant_agent.")
            )
        ]
        bodies.append(ast.unparse(parsed))
    bodies.append(f"RISK_SETTINGS = {RISK!r}\nBOOT_BLOCKS = {blocks}\nBOOT_ROWS = {block_rows}\n")
    bodies.append("""
def make_predictor(root):
    from pathlib import Path
    root = Path(root)
    policy = None
    def predict(batch):
        nonlocal policy
        if not isinstance(batch, pd.DataFrame):
            batch = batch.to_pandas()
        if policy is None:
            validate_ids(batch)
            if len(batch) != 1:
                raise ValueError("one observed row required")
            first = int(batch.date_id.iloc[0])
            past = pd.read_csv(root / "train.csv", usecols=["date_id", "forward_returns", "risk_free_rate"],
                               nrows=first, float_precision="round_trip")
            if not np.array_equal(past.date_id.to_numpy(), np.arange(first)):
                raise ValueError("complete earlier zero-based observed history required for bootstrap")
            policy = bootstrap_reversal(past, first, RiskSettings(**RISK_SETTINGS),
                                        blocks=BOOT_BLOCKS, block_rows=BOOT_ROWS)
        return policy.predict_visible(batch)
    return predict

def main():
    import argparse
    import os
    import sys
    from pathlib import Path
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-root", type=Path)
    args = parser.parse_args()
    root = args.input_root
    if root is None:
        candidates = [p.parent for p in Path("/kaggle/input").rglob("train.csv")
                      if p.parent.name == "hull-tactical-market-prediction"]
        if len(candidates) != 1:
            raise ValueError("one attached Hull competition input required")
        root = candidates[0]
    sys.path.insert(0, str(root))
    from kaggle_evaluation.default_inference_server import DefaultInferenceServer
    server = DefaultInferenceServer(make_predictor(root))
    if os.getenv("KAGGLE_IS_COMPETITION_RERUN"):
        server.serve()
    else:
        server.run_local_gateway((str(root),))

if __name__ == "__main__":
    main()
""")
    return "\n\n".join(bodies)


def export_script(output: Path, *, blocks: int, block_rows: int) -> Path:
    output.mkdir(parents=True, exist_ok=False)
    script = output / "main.py"
    source = standalone_source(blocks=blocks, block_rows=block_rows)
    compile(source, str(script), "exec")
    with script.open("x") as h:
        h.write(source)
    write_new_json(
        output / "kernel-metadata.json",
        {
            "id": "leon0330/hull-transparent-reversal-20261029",
            "title": "Hull Transparent Reversal 20261029",
            "code_file": "main.py",
            "language": "python",
            "kernel_type": "script",
            "is_private": True,
            "enable_gpu": False,
            "enable_internet": False,
            "competition_sources": ["hull-tactical-market-prediction"],
        },
    )
    return script


def replay_official(
    script: Path,
    raw_api: Path,
    history: pd.DataFrame,
    expected: pd.DataFrame,
    start: int,
    output: Path,
    count: int = 16,
) -> dict:
    """New process and original API, restricted to the requested development prefix."""
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    inputs = output / "input"
    inputs.mkdir()
    history.loc[history.date_id < start, ["date_id", "forward_returns", "risk_free_rate"]].to_csv(
        inputs / "train.csv", index=False, mode="x"
    )
    lag = history[["date_id"]].copy()
    lag["lagged_forward_returns"] = history.forward_returns.shift(1)
    lag["lagged_risk_free_rate"] = history.risk_free_rate.shift(1)
    rows = lag.loc[lag.date_id.isin(expected.head(count).date_id)].copy()
    rows.to_csv(inputs / "test.csv", index=False, mode="x")
    (inputs / "kaggle_evaluation").symlink_to(
        raw_api.resolve() / "kaggle_evaluation", target_is_directory=True
    )
    completed = subprocess.run(
        [sys.executable, str(script.resolve()), "--input-root", str(inputs)],
        cwd=output,
        text=True,
        capture_output=True,
        timeout=90,
    )
    (output / "gateway.log").write_text(completed.stdout + completed.stderr)
    if completed.returncode != 0:
        raise RuntimeError("official gateway failed; inspect local gateway.log")
    actual = pd.read_parquet(output / "submission.parquet")
    wanted = expected.head(count)
    if list(actual) != ["date_id", "prediction"] or not np.array_equal(
        actual.date_id, wanted.date_id
    ):
        raise ValueError("official replay output schema/IDs changed")
    if not np.array_equal(actual.prediction, wanted.position):
        raise ValueError("standalone/API position mismatch")
    result = {
        "official_gateway_passed": True,
        "rows": len(actual),
        "id_range": [int(actual.date_id.min()), int(actual.date_id.max())],
        "float32_allocations_exact": True,
        "new_process": True,
        "reserved_predictions": 0,
        "reserved_evaluations": 0,
        "online_submitted": False,
        "rank": None,
    }
    write_new_json(output / "api_result.json", result)
    return result
