"""Run the downloaded official Hull gateway locally in a fresh output directory.

@author cylovelife
"""

import argparse
import hashlib
import logging
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import polars as pl

from quant_agent.competitions.hull.data import LAGGED, load_dataset
from quant_agent.competitions.hull.sequential import SequentialPredictor
from quant_agent.competitions.hull.study import write_new_json

LOGGER = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("raw", "manifest", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    raw, manifest, output = args.raw.resolve(), args.manifest.resolve(), args.output.resolve()
    if os.getenv("KAGGLE_IS_COMPETITION_RERUN"):
        raise ValueError("historical acceptance entrypoint is local only")
    data = load_dataset(raw, manifest)
    output.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0, str(raw))
    # Original gateway files remain in the ignored download directory.
    from kaggle_evaluation.default_inference_server import DefaultInferenceServer

    model = SequentialPredictor((*data.features, *LAGGED), lambda _: 1.0)

    def predict(batch: pl.DataFrame) -> float:
        return model.predict(batch)

    started = time.perf_counter()
    original_cwd = Path.cwd()
    try:
        os.chdir(output)
        server = DefaultInferenceServer(predict)
        server.run_local_gateway((str(raw),))
        actual = pd.read_parquet("submission.parquet")
        if list(actual) != ["date_id", "prediction"]:
            raise ValueError("unexpected official output schema")
        if (
            not np.array_equal(actual.date_id, data.mock.date_id)
            or not actual.prediction.eq(1.0).all()
        ):
            raise ValueError("official gateway output does not match sequential predictions")
        if model.rows_seen != len(data.mock):
            raise ValueError("official gateway did not call once per mock row")
        result = {
            "official_gateway_passed": True,
            "rows": len(actual),
            "calls": model.rows_seen,
            "batch_rows": 1,
            "input_columns": len(data.mock.columns),
            "callback_feature_columns": len(model.features),
            "mock_scored_rows": int(data.mock.is_scored.sum()),
            "scored_false_rows_also_predicted": True,
            "submission_sha256": hashlib.sha256(
                Path("submission.parquet").read_bytes()
            ).hexdigest(),
            "date_ids": actual.date_id.astype(int).tolist(),
            "allocation": 1.0,
            "elapsed_seconds": time.perf_counter() - started,
            "online_submission": False,
            "mock_score_reported": False,
        }
        write_new_json(Path("api_result.json"), result)
        LOGGER.info("official gateway accepted %d sequential rows", len(actual))
    finally:
        os.chdir(original_cwd)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
