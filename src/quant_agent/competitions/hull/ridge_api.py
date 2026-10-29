"""Replay an already-fitted forecast policy on development-only rows via original API.

@author cylovelife
"""

import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import polars as pl

from quant_agent.competitions.hull.data import LAGGED
from quant_agent.competitions.hull.ridge import ForecastWindow
from quant_agent.competitions.hull.sequential import SequentialPredictor
from quant_agent.competitions.hull.study import write_new_json


def replay_development(
    raw_api: Path,
    rows: pd.DataFrame,
    fitted: ForecastWindow,
    output: Path,
) -> dict:
    """Use real earlier development observations, never the overlapping mock/holdout."""
    if os.getenv("KAGGLE_IS_COMPETITION_RERUN"):
        raise ValueError("development replay is local only")
    if rows.empty or not rows.date_id.isin(fitted.predictions.date_id).all():
        raise ValueError("replay requires previously evaluated development IDs")
    output.mkdir(parents=True, exist_ok=False)
    inputs = output / "input"
    inputs.mkdir()
    base = [c for c in fitted.features if c not in LAGGED]
    visible = rows[["date_id", *fitted.features]].copy()
    visible["is_scored"] = True
    visible = visible[["date_id", *base, "is_scored", *LAGGED]]
    visible.to_csv(inputs / "test.csv", index=False, mode="x")
    expected = fitted.predictions.set_index("date_id").loc[visible.date_id, "allocation"].to_numpy()
    sys.path.insert(0, str(raw_api))
    from kaggle_evaluation.default_inference_server import DefaultInferenceServer

    adapter = SequentialPredictor(fitted.features, fitted.allocate)

    def predict(batch: pl.DataFrame) -> float:
        return adapter.predict(batch)

    previous = Path.cwd()
    started = time.perf_counter()
    try:
        os.chdir(output)
        server = DefaultInferenceServer(predict)
        server.run_local_gateway((str(inputs),))
        actual = pd.read_parquet("submission.parquet")
        if list(actual) != ["date_id", "prediction"] or not np.array_equal(
            actual.date_id, visible.date_id
        ):
            raise ValueError("official replay ID/schema mismatch")
        if not np.array_equal(actual.prediction, expected) or adapter.rows_seen != len(visible):
            raise ValueError("official rowwise allocation differs from vectorized research")
        result = {
            "official_gateway_passed": True,
            "rows": len(visible),
            "id_range": [int(visible.date_id.min()), int(visible.date_id.max())],
            "batch_rows": 1,
            "input_columns": len(visible.columns),
            "exact_research_allocation_match": True,
            "reserved_rows_used": 0,
            "elapsed_seconds": time.perf_counter() - started,
            "online_submitted": False,
        }
        write_new_json(Path("api_result.json"), result)
        return result
    finally:
        os.chdir(previous)
