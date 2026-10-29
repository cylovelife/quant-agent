"""Read-only Hull file audit and explicit feature/label visibility boundaries.

@author cylovelife
"""

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from pandas.api.types import is_bool_dtype, is_numeric_dtype

from quant_agent.competitions.hull.metrics import validate_ids

TARGETS = ("forward_returns", "risk_free_rate", "market_forward_excess_returns")
LAGGED = tuple("lagged_" + name for name in TARGETS)


@dataclass(frozen=True)
class HullData:
    train: pd.DataFrame
    mock: pd.DataFrame
    features: tuple[str, ...]
    audit: dict


def scored_mask(values: pd.Series) -> pd.Series:
    """Parse only actual booleans or the CSV literals true/false, never bool('false')."""
    if is_bool_dtype(values) and not values.isna().any():
        return values.astype(bool)
    if not values.isin(["true", "false"]).all():
        raise ValueError("is_scored must contain true/false")
    return values.map({"true": True, "false": False}).astype(bool)


def load_dataset(raw_root: str | Path, manifest_path: str | Path) -> HullData:
    """Verify immutable files before interpreting the downloaded historical mock."""
    root = Path(raw_root).resolve()
    manifest = json.loads(Path(manifest_path).read_text())
    if manifest.get("competition") != "hull-tactical-market-prediction":
        raise ValueError("unexpected competition manifest")
    names = [item["path"] for item in manifest["files"]]
    if len(names) != len(set(names)) or not {"train.csv", "test.csv"}.issubset(names):
        raise ValueError("duplicate or missing manifest files")
    for item in manifest["files"]:
        path = (root / item["path"]).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError("invalid manifest path")
        if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
            raise ValueError(f"file hash mismatch: {item['path']}")
    train, mock = (pd.read_csv(root / name) for name in ("train.csv", "test.csv"))
    validate_ids(train)
    validate_ids(mock)
    if not set(TARGETS).issubset(train):
        raise ValueError("missing training labels")
    features = tuple(c for c in train if c not in ("date_id", *TARGETS))
    if not features or any(not re.fullmatch(r"(?:MOM|[DEIMPSV])\d+", c) for c in features):
        raise ValueError("unknown training feature columns")
    if list(train) != ["date_id", *features, *TARGETS]:
        raise ValueError("unexpected training column order")
    if list(mock) != ["date_id", *features, "is_scored", *LAGGED]:
        raise ValueError("unexpected mock schema")
    for frame, columns in ((train, (*features, *TARGETS)), (mock, (*features, *LAGGED))):
        for name in columns:
            if (
                not is_numeric_dtype(frame[name])
                or np.isinf(frame[name].to_numpy(dtype=float)).any()
            ):
                raise ValueError(f"invalid numeric column: {name}")
    if not np.isfinite(train[list(TARGETS)].to_numpy(dtype=float)).all():
        raise ValueError("training labels must be finite")
    mock["is_scored"] = scored_mask(mock.is_scored)
    indexed = train.set_index("date_id")
    if not mock.date_id.isin(indexed.index).all():
        raise ValueError("historical mock IDs are absent from training history")
    copied = indexed.loc[mock.date_id, list(features)].to_numpy(dtype=float)
    if not np.allclose(copied, mock[list(features)], rtol=0, atol=1e-12, equal_nan=True):
        raise ValueError("mock feature copy mismatch")
    previous = train.date_id.shift(1)
    prior_ids = pd.Series(previous.to_numpy(), index=train.date_id).reindex(mock.date_id)
    if prior_ids.isna().any():
        raise ValueError("mock lag has no observed predecessor")
    for name in TARGETS:
        expected = indexed.loc[prior_ids.to_numpy(dtype=int), name].to_numpy()
        if not np.allclose(expected, mock["lagged_" + name], rtol=0, atol=1e-12, equal_nan=True):
            raise ValueError(f"mock lag mapping mismatch: {name}")
    missing = train[list(features)].isna().sum()
    groups = {}
    for name in features:
        prefix = re.match(r"[A-Z]+", name).group()
        groups[prefix] = groups.get(prefix, 0) + 1
    audit = {
        "train_shape": list(train.shape),
        "mock_shape": list(mock.shape),
        "train_id_range": [int(train.date_id.min()), int(train.date_id.max())],
        "mock_id_range": [int(mock.date_id.min()), int(mock.date_id.max())],
        "feature_columns": list(features),
        "feature_groups": groups,
        "feature_missing_cells": int(missing.sum()),
        "missing_by_feature": missing.astype(int).to_dict(),
        "all_missing_features": missing.index[missing.eq(len(train))].tolist(),
        "mock_scored_rows": int(mock.is_scored.sum()),
        "mock_feature_copy_matches": True,
        "lag_mapping_matches": True,
        "lag_availability": "previous observed training record; verified on all mock rows",
        "calendar_dates_known": False,
        "mock_is_independent_test": False,
    }
    return HullData(train, mock, features, audit)


def training_features(data: HullData) -> pd.DataFrame:
    """Construct historical inputs; current-day targets never enter the feature matrix."""
    result = data.train[["date_id", *data.features]].copy()
    for name in TARGETS:
        result["lagged_" + name] = data.train[name].shift(1)
    return result


def mature_window(
    train: pd.DataFrame, start: int, end_exclusive: int
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Use next observed ID as label availability; terminal labels remain unmatured."""
    validate_ids(train)
    if start >= end_exclusive:
        raise ValueError("invalid window")
    labelled = train.copy()
    labelled["label_end_id"] = train.date_id.shift(-1)
    fit = labelled.loc[(labelled.date_id < start) & (labelled.label_end_id < start)].copy()
    valid = labelled.loc[
        (labelled.date_id >= start)
        & (labelled.date_id < end_exclusive)
        & (labelled.label_end_id < end_exclusive)
    ].copy()
    if fit.empty or len(valid) < 2:
        raise ValueError("insufficient mature training/evaluation rows")
    return fit, valid
