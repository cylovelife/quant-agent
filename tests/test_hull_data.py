"""Synthetic Hull file identity, lag semantics and mature-window tests.

@author cylovelife
"""

import hashlib
import json

import numpy as np
import pandas as pd
import pytest

from quant_agent.competitions.hull.data import load_dataset, mature_window, training_features
from quant_agent.competitions.hull.study import run_study


@pytest.fixture
def source(tmp_path):
    train = pd.DataFrame(
        {
            "date_id": np.arange(30),
            "D1": np.arange(30) % 2,
            "M1": np.arange(30, dtype=float),
            "E1": np.arange(30, dtype=float),
            "forward_returns": np.sin(np.arange(30)) * 0.01,
            "risk_free_rate": 0.0001,
            "market_forward_excess_returns": np.cos(np.arange(30)) * 0.01,
        }
    )
    train.loc[:3, "E1"] = np.nan
    mock = train.loc[20:24, ["date_id", "D1", "M1", "E1"]].copy()
    mock["is_scored"] = ["false", "true", "true", "true", "true"]
    for name in ("forward_returns", "risk_free_rate", "market_forward_excess_returns"):
        mock["lagged_" + name] = train[name].shift(1).loc[20:24]
    train.to_csv(tmp_path / "train.csv", index=False)
    mock.to_csv(tmp_path / "test.csv", index=False)

    def freeze():
        meta = {
            "competition": "hull-tactical-market-prediction",
            "files": [
                {"path": name, "sha256": hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()}
                for name in ("train.csv", "test.csv")
            ],
        }
        (tmp_path / "manifest.json").write_text(json.dumps(meta))

    freeze()
    return tmp_path, tmp_path / "manifest.json", freeze


def test_lags_missingness_and_scoring_mask_are_separate(source):
    root, manifest, _ = source
    data = load_dataset(root, manifest)
    assert data.features == ("D1", "M1", "E1")
    assert data.audit["mock_feature_copy_matches"]
    assert data.audit["lag_mapping_matches"]
    assert data.mock.is_scored.tolist() == [False, True, True, True, True]
    features = training_features(data)
    assert not {"forward_returns", "risk_free_rate", "market_forward_excess_returns"} & set(
        features
    )
    assert features.lagged_forward_returns.iloc[1] == data.train.forward_returns.iloc[0]
    assert pd.isna(features.lagged_forward_returns.iloc[0])
    assert features.E1.iloc[:4].isna().all()


@pytest.mark.parametrize(
    "defect", ["hash", "lag", "unknown", "duplicate", "order", "mask", "inf", "target_missing"]
)
def test_bad_input_is_rejected(source, defect):
    root, manifest, freeze = source
    mock = pd.read_csv(root / "test.csv")
    if defect in ("hash", "lag"):
        mock.loc[0, "lagged_forward_returns"] = 99
    elif defect == "unknown":
        mock["future_secret"] = 1
    elif defect == "duplicate":
        mock.loc[1, "date_id"] = mock.loc[0, "date_id"]
    elif defect == "order":
        mock = mock.iloc[::-1]
    elif defect == "mask":
        mock["is_scored"] = "yes"
    elif defect == "inf":
        mock.loc[0, "M1"] = np.inf
    elif defect == "target_missing":
        train = pd.read_csv(root / "train.csv")
        train.loc[0, "forward_returns"] = np.nan
        train.to_csv(root / "train.csv", index=False)
    mock.to_csv(root / "test.csv", index=False)
    if defect != "hash":
        freeze()
    with pytest.raises(ValueError):
        load_dataset(root, manifest)


def test_purge_uses_next_observed_id_and_does_not_invent_calendar_dates(source):
    data = load_dataset(*source[:2])
    train, valid = mature_window(data.train, 10, 20)
    assert train.date_id.max() == 8
    assert valid.date_id.tolist() == list(range(10, 19))
    sparse = data.train.loc[data.train.date_id != 8]
    train, _ = mature_window(sparse, 10, 20)
    assert train.date_id.max() == 7


def test_constant_study_never_scores_reserved_labels_and_refuses_overwrite(source):
    root, manifest, freeze = source
    protocol = root / "protocol.json"

    def plan():
        protocol.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "reserved_window_evaluation_allowed": False,
                    "expected_training_rows": 30,
                    "expected_train_sha256": hashlib.sha256(
                        (root / "train.csv").read_bytes()
                    ).hexdigest(),
                    "development_windows": [[5, 10], [10, 15]],
                    "reserved_window": [25, 30],
                    "allocations": [0.5, 1.0, 1.5],
                }
            )
        )

    plan()
    a = run_study(root, manifest, protocol, root / "first")
    with pytest.raises(FileExistsError):
        run_study(root, manifest, protocol, root / "first")
    train = pd.read_csv(root / "train.csv")
    train.loc[train.date_id >= 25, "forward_returns"] = 99
    train.to_csv(root / "train.csv", index=False)
    freeze()
    plan()
    b = run_study(root, manifest, protocol, root / "perturbed")
    assert a["windows"] == b["windows"]
    assert a["reserved_evaluations"] == b["reserved_evaluations"] == 0
