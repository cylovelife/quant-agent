"""Competition integrity, causal features, purged validation and submission tests.

@author cylovelife
"""

import hashlib
import json

import numpy as np
import pandas as pd
import pytest

from quant_agent.competitions.stock_price.baseline import (
    build_features,
    choose_model,
    development_scores,
    fit_predict,
    run_study,
)
from quant_agent.competitions.stock_price.data import load_dataset  # noqa: E402
from quant_agent.competitions.stock_price.metrics import (  # noqa: E402
    classification_metrics,
    write_submission,
)
from quant_agent.competitions.stock_price.validation import purged_window  # noqa: E402


@pytest.fixture
def source(tmp_path):
    dates = pd.date_range("2015-01-01", periods=220)
    close = 100 + np.sin(np.arange(220) * 0.7) + np.arange(220) * 0.02
    frame = pd.DataFrame(
        {
            "Date": dates,
            "High": close + 2,
            "Low": close - 2,
            "Open": close - 0.3,
            "Close": close,
        }
    )
    train = frame.iloc[:190].copy()
    train["Target"] = (train.Close.shift(-1) > train.Close).astype(int)
    test = frame.iloc[190:].copy()
    sample = pd.DataFrame({"Id": np.arange(len(test)), "Target": 0})
    raw = tmp_path / "raw"
    raw.mkdir()
    files = {}
    for name, sheet, data in (
        ("Train.xlsx", "Train", train),
        ("Test.xlsx", "Test", test),
        ("sample_submission.xlsx", "Sample_submission", sample),
    ):
        path = raw / name
        data.to_excel(path, index=False, sheet_name=sheet)
        duplicate = raw / "StockTrends" / name
        duplicate.parent.mkdir(exist_ok=True)
        duplicate.write_bytes(path.read_bytes())
        files[name] = {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "rows": len(data),
            "sheet": sheet,
            "columns": list(data),
            "identical_duplicate": "StockTrends/" + name,
        }
    contract = {
        "schema_version": 1,
        "competition": "stock-price-predictor",
        "dataset": {"canonical_files": files},
        "label": {"exclude_train_dates": [str(train.Date.iloc[-1].date())]},
    }
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(contract))
    return raw, path


def test_adapter_deduplicates_excludes_terminal_and_keeps_calendar(source):
    data = load_dataset(*source)
    assert len(data.prices) == 220
    assert len(data.labels) == 189
    assert data.labels.label_end.max() == pd.Timestamp("2015-07-09")
    assert data.prices.Date.dt.dayofweek.ge(5).any()
    assert data.sample.Id.tolist() == list(range(30))


def test_adapter_rejects_tampered_file(source):
    raw, contract = source
    with (raw / "Train.xlsx").open("ab") as stream:
        stream.write(b"tampered")
    with pytest.raises(ValueError, match="hash"):
        load_dataset(raw, contract)


def test_adapter_rejects_changed_duplicate(source):
    raw, contract = source
    (raw / "StockTrends/Test.xlsx").write_bytes(b"changed")
    with pytest.raises(ValueError, match="duplicate"):
        load_dataset(raw, contract)


@pytest.mark.parametrize(
    "defect",
    ["order", "duplicate_date", "bad_price", "test_target", "bad_label", "bad_id"],
)
def test_adapter_rejects_semantic_defects_even_with_matching_hash(source, defect):
    raw, contract_path = source
    contract = json.loads(contract_path.read_text())
    name = "Train.xlsx"
    if defect == "test_target":
        name = "Test.xlsx"
    elif defect == "bad_id":
        name = "sample_submission.xlsx"
    meta = contract["dataset"]["canonical_files"][name]
    frame = pd.read_excel(raw / name)
    if defect == "order":
        frame = frame.iloc[::-1]
    elif defect == "duplicate_date":
        frame.loc[1, "Date"] = frame.loc[0, "Date"]
    elif defect == "bad_price":
        frame.loc[0, "Low"] = frame.loc[0, "High"] + 1
    elif defect == "test_target":
        frame["Target"] = 1
    elif defect == "bad_label":
        frame.loc[0, "Target"] = 1 - frame.loc[0, "Target"]
    else:
        frame.loc[0, "Id"] = 9
    frame.to_excel(raw / name, index=False, sheet_name=meta["sheet"])
    content = (raw / name).read_bytes()
    (raw / meta["identical_duplicate"]).write_bytes(content)
    meta["sha256"] = hashlib.sha256(content).hexdigest()
    contract_path.write_text(json.dumps(contract))
    with pytest.raises(ValueError):
        load_dataset(raw, contract_path)


def test_features_do_not_change_when_future_prices_or_targets_change(source):
    data = load_dataset(*source)
    base = build_features(data.prices)
    changed = data.prices.copy()
    changed.loc[150:, ["Open", "High", "Low", "Close"]] *= 3
    changed["Target"] = np.arange(len(changed))
    other = build_features(changed)
    pd.testing.assert_frame_equal(base.iloc[:150], other.iloc[:150])
    assert "Target" not in base
    assert base.drop(columns="Date").shape[1] == 18
    pd.testing.assert_frame_equal(base.iloc[:150], build_features(data.prices.iloc[:150]))


def test_purge_uses_actual_label_end_and_disallows_bad_dates():
    panel = pd.DataFrame(
        {
            "Date": pd.date_range("2015-01-01", periods=10),
            "label_end": pd.date_range("2015-01-02", periods=10),
            "Target": [0, 1] * 5,
        }
    )
    panel.loc[1, "label_end"] = pd.Timestamp("2015-01-07")
    fit, valid = purged_window(panel, "2015-01-05", "2015-01-09")
    assert fit.Date.dt.day.tolist() == [1, 3]
    assert valid.Date.dt.day.tolist() == [5, 6, 7]
    broken = panel.copy()
    broken.loc[0, "label_end"] = pd.NaT
    with pytest.raises(ValueError, match="date|end"):
        purged_window(broken, "2015-01-05", "2015-01-09")


def test_metrics_count_accuracy_and_reject_invalid_predictions():
    result = classification_metrics([0, 0, 1, 1], [0, 1, 1, 1])
    assert result["accuracy"] == 0.75
    assert result["confusion_matrix"] == [[1, 1], [0, 2]]
    for bad in ([0.2, 1, 1, 1], [0, np.nan, 1, 1], [1, 0], []):
        with pytest.raises(ValueError):
            classification_metrics([0, 0, 1, 1], bad)


def test_submission_exact_ids_binary_domain_and_no_overwrite(tmp_path):
    path = tmp_path / "submission.csv"
    write_submission(pd.Series([0, 1, 2]), [1, 0, 1], path)
    assert path.read_text().splitlines() == ["Id,Target", "0,1", "1,0", "2,1"]
    with pytest.raises(FileExistsError):
        write_submission(pd.Series([0, 1, 2]), [1, 0, 1], path)
    with pytest.raises(ValueError):
        write_submission(pd.Series([1, 0, 2]), [1, 0, 1], tmp_path / "bad.csv")
    assert not (tmp_path / "bad.csv").exists()


def test_more_complex_model_requires_prefrozen_material_increment():
    scores = {
        "calendar": [0.7, 0.7],
        "logistic": [0.705, 0.705],
        "hist_gradient": [0.74, 0.68],
    }
    assert choose_model(scores, 0.01) == "calendar"
    scores["logistic"] = [0.72, 0.72]
    assert choose_model(scores, 0.01) == "logistic"


def test_prediction_does_not_refit_scaling_on_inference(source):
    data = load_dataset(*source)
    features = build_features(data.prices).dropna()
    names = list(features.columns.drop("Date"))
    panel = data.labels.merge(features, on="Date")
    pred, model = fit_predict("logistic", panel.iloc[:100], panel.iloc[100:120], names)
    before = model.named_steps["standardscaler"].mean_.copy()
    first = model.predict(panel.iloc[100:110][names])
    changed = panel.iloc[100:120][names].copy()
    changed.iloc[10:] *= 10
    np.testing.assert_array_equal(first, model.predict(changed)[:10])
    np.testing.assert_array_equal(before, model.named_steps["standardscaler"].mean_)
    assert set(pred) <= {0, 1}


def test_holdout_labels_do_not_affect_development_or_selected_model(source):
    data = load_dataset(*source)
    features = build_features(data.prices).dropna()
    panel = data.labels.merge(features, on="Date")
    protocol = {
        "development_windows": [
            ["2015-03-01", "2015-04-01"],
            ["2015-04-01", "2015-05-01"],
        ],
        "holdout_start": "2015-05-01",
        "selection_margin": 0.01,
    }
    names = list(features.columns.drop("Date"))
    a = development_scores(panel, names, protocol)
    panel.loc[panel.Date >= protocol["holdout_start"], "Target"] ^= 1
    b = development_scores(panel, names, protocol)
    assert a == b


def test_runner_has_exclusive_output_and_deterministic_prediction(source, tmp_path):
    raw, contract = source
    protocol = tmp_path / "protocol.json"
    protocol.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "development_windows": [
                    ["2015-03-01", "2015-04-01"],
                    ["2015-04-01", "2015-05-01"],
                ],
                "holdout_start": "2015-05-01",
                "holdout_end_exclusive": "2015-07-09",
                "selection_margin": 0.01,
                "max_candidate_evaluations": 6,
            }
        )
    )
    out = tmp_path / "study"
    result = run_study(raw, contract, protocol, out)
    assert result["submission"]["rows"] == 30
    assert result["selected_model"] in {"calendar", "logistic", "hist_gradient"}
    assert result["test_labels_derived"] is False
    with pytest.raises(FileExistsError):
        run_study(raw, contract, protocol, out)
