"""Binary Accuracy and exact-ID competition submission validation.

@author cylovelife
"""

import csv
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pandas as pd


def _binary(values: Sequence | np.ndarray | pd.Series) -> np.ndarray:
    array = np.asarray(values)
    if array.ndim != 1 or array.size == 0 or not np.isin(array, [0, 1]).all():
        raise ValueError("nonempty one-dimensional binary values required")
    return array.astype(int)


def classification_metrics(
    target: Sequence | np.ndarray | pd.Series,
    prediction: Sequence | np.ndarray | pd.Series,
) -> dict:
    truth, pred = _binary(target), _binary(prediction)
    if truth.shape != pred.shape:
        raise ValueError("target and prediction lengths differ")
    matrix = [[int(((truth == a) & (pred == b)).sum()) for b in (0, 1)] for a in (0, 1)]
    recalls = [matrix[a][a] / sum(matrix[a]) for a in (0, 1) if sum(matrix[a])]
    return {
        "n": len(truth),
        "correct": int((truth == pred).sum()),
        "accuracy": float((truth == pred).mean()),
        "balanced_accuracy": float(np.mean(recalls)) if len(recalls) == 2 else None,
        "confusion_matrix": matrix,
    }


def write_submission(ids: pd.Series, prediction: Sequence | np.ndarray, path: str | Path) -> None:
    pred = _binary(prediction)
    if not np.array_equal(np.asarray(ids), np.arange(len(pred))):
        raise ValueError("Id must preserve zero-based test row order and prediction length")
    with Path(path).open("x", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(["Id", "Target"])
        writer.writerows(zip(np.asarray(ids).astype(int), pred, strict=True))
