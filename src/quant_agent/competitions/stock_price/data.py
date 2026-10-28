"""Strict, read-only adapter for the audited stock-price-predictor dataset.

@author cylovelife
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

PRICE_COLUMNS = ["Date", "High", "Low", "Open", "Close"]
FILE_COLUMNS = {
    "Train.xlsx": PRICE_COLUMNS + ["Target"],
    "Test.xlsx": PRICE_COLUMNS,
    "sample_submission.xlsx": ["Id", "Target"],
}


@dataclass(frozen=True)
class CompetitionData:
    prices: pd.DataFrame
    labels: pd.DataFrame
    sample: pd.DataFrame
    file_hashes: dict[str, str]


def _safe_file(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("file path escapes dataset root")
    return path


def _validate_prices(frame: pd.DataFrame) -> None:
    dates = pd.to_datetime(frame["Date"], errors="coerce")
    if (
        dates.isna().any()
        or not dates.is_unique
        or not dates.is_monotonic_increasing
        or not dates.eq(dates.dt.normalize()).all()
        or not dates.diff().dropna().eq(pd.Timedelta(days=1)).all()
    ):
        raise ValueError("dates must be unique, ordered calendar days")
    prices = frame[PRICE_COLUMNS[1:]].apply(pd.to_numeric, errors="coerce")
    if not np.isfinite(prices.to_numpy()).all() or (prices <= 0).any().any():
        raise ValueError("invalid or missing prices")
    if (prices.High < prices[["Low", "Open", "Close"]].max(axis=1)).any() or (
        prices.Low > prices[["High", "Open", "Close"]].min(axis=1)
    ).any():
        raise ValueError("inconsistent OHLC prices")
    frame["Date"] = dates
    frame[PRICE_COLUMNS[1:]] = prices


def load_dataset(raw_root: str | Path, contract_path: str | Path) -> CompetitionData:
    """Validate file identity before parsing; never generate targets from test prices."""
    root = Path(raw_root)
    contract = json.loads(Path(contract_path).read_text())
    if (
        contract.get("schema_version") != 1
        or contract.get("competition") != "stock-price-predictor"
    ):
        raise ValueError("unsupported competition contract")
    declared = contract["dataset"]["canonical_files"]
    if set(declared) != set(FILE_COLUMNS):
        raise ValueError("unexpected canonical files")
    frames, hashes = {}, {}
    for name, columns in FILE_COLUMNS.items():
        meta = declared[name]
        path = _safe_file(root, name)
        content = path.read_bytes()
        fingerprint = hashlib.sha256(content).hexdigest()
        if fingerprint != meta["sha256"]:
            raise ValueError(f"file hash mismatch: {name}")
        duplicate = _safe_file(root, meta["identical_duplicate"])
        if duplicate.read_bytes() != content:
            raise ValueError(f"duplicate mismatch: {name}")
        with pd.ExcelFile(path, engine="openpyxl") as book:
            if book.sheet_names != [meta["sheet"]]:
                raise ValueError(f"unexpected sheets: {name}")
            frame = pd.read_excel(book, sheet_name=meta["sheet"])
        if list(frame) != columns or list(frame) != meta["columns"] or len(frame) != meta["rows"]:
            raise ValueError(f"unexpected columns or row count: {name}")
        if frame.isna().any().any():
            raise ValueError(f"missing source values: {name}")
        frames[name], hashes[name] = frame, fingerprint
    train, test, sample = (frames[n] for n in FILE_COLUMNS)
    _validate_prices(train)
    _validate_prices(test)
    if test.Date.iloc[0] != train.Date.iloc[-1] + pd.Timedelta(days=1):
        raise ValueError("train/test dates overlap or are not contiguous")
    if not train.Target.isin([0, 1]).all():
        raise ValueError("training label must be binary")
    excluded = contract["label"]["exclude_train_dates"]
    if excluded != [train.Date.iloc[-1].date().isoformat()]:
        raise ValueError("only the audited terminal training label may be excluded")
    labels = train[["Date", "Target"]].copy()
    labels["label_end"] = train.Date.shift(-1)
    # Compare supplied TRAIN labels only; the terminal row remains excluded.
    expected = (train.Close.shift(-1) > train.Close).iloc[:-1].astype(int)
    if not expected.equals(train.Target.iloc[:-1].astype(int)):
        raise ValueError("provided training labels disagree with next calendar row")
    labels = labels.iloc[:-1].copy()
    if not np.array_equal(sample.Id.to_numpy(), np.arange(len(test))):
        raise ValueError("sample Id must match original test row order")
    if not sample.Target.isin([0]).all():
        raise ValueError("sample targets must remain placeholders")
    prices = pd.concat([train[PRICE_COLUMNS], test[PRICE_COLUMNS]], ignore_index=True)
    return CompetitionData(prices, labels, sample, hashes)
