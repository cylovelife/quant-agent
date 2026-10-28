"""Date-grouped, label-end-purged train/validation/test partitioning.

@author cylovelife
"""

from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pandas as pd


@dataclass(frozen=True)
class TemporalSplit:
    train: pd.DataFrame
    validation: pd.DataFrame
    test: pd.DataFrame
    validation_start: str
    test_start: str


def configured_boundaries(cfg: dict) -> dict | None:
    """Explicit boundaries only; missing configuration cannot imply a sealed test."""
    value = cfg.get("temporal_validation") if isinstance(cfg, dict) else None
    if not isinstance(value, dict) or set(value) != {
        "validation_start",
        "test_start",
        "test_end",
    }:
        return None
    try:
        dates = [
            date.fromisoformat(value[key]) for key in ("validation_start", "test_start", "test_end")
        ]
    except (TypeError, ValueError):
        return None
    if [d.isoformat() for d in dates] != [
        value[key] for key in ("validation_start", "test_start", "test_end")
    ]:
        return None
    if not dates[0] < dates[1] <= dates[2] < datetime.now(ZoneInfo("Asia/Shanghai")).date():
        return None
    return value.copy()


def truncate_klines(kline_map: dict, cutoff: str) -> dict:
    """Limit all symbol histories before deriving signals or selecting sample windows."""
    end = pd.Timestamp(cutoff)
    result = {}
    for key, frame in (kline_map or {}).items():
        if frame is None or "date" not in frame:
            result[key] = frame
            continue
        dates = pd.to_datetime(frame["date"], errors="coerce")
        result[key] = frame.loc[dates.notna() & (dates <= end)].copy()
    return result


def split_three_way(
    frame: pd.DataFrame,
    *,
    date_col: str,
    end_col: str,
    train_fraction: float = 0.6,
    validation_fraction: float = 0.2,
    validation_start: str | None = None,
    test_start: str | None = None,
    test_end: str | None = None,
) -> TemporalSplit:
    """Group entire dates and drop labels crossing the next partition boundary.

    Test labels must be explicitly matured; callers decide when their observed data end.
    """
    if (
        not 0 < train_fraction < 1
        or not 0 < validation_fraction < 1
        or train_fraction + validation_fraction >= 1
    ):
        raise ValueError("invalid split fractions")
    if date_col not in frame or end_col not in frame:
        raise ValueError("actual label end dates are required")
    work = frame.copy()
    dates = pd.to_datetime(work[date_col], errors="coerce")
    ends = pd.to_datetime(work[end_col], errors="coerce")
    valid = dates.notna() & ends.notna() & (ends >= dates)
    work = work.loc[valid].copy()
    dates = dates.loc[valid]
    ends = ends.loc[valid]
    unique = sorted(dates.unique())
    if len(unique) < 3:
        raise ValueError("at least three distinct valid dates required")
    explicit = (validation_start, test_start, test_end)
    if any(x is not None for x in explicit):
        if not all(isinstance(x, str) for x in explicit):
            raise ValueError("all three explicit date boundaries are required")
        val_start, test_start_date, end_date = [pd.Timestamp(x) for x in explicit]
        if not val_start < test_start_date <= end_date:
            raise ValueError("date boundaries must be ordered")
    else:
        val_i = max(1, int(len(unique) * train_fraction))
        test_i = max(val_i + 1, int(len(unique) * (train_fraction + validation_fraction)))
        if test_i >= len(unique):
            raise ValueError("not enough dates for three partitions")
        val_start, test_start_date, end_date = unique[val_i], unique[test_i], None
    train = work.loc[(dates < val_start) & (ends < val_start)].copy()
    validation = work.loc[
        (dates >= val_start) & (dates < test_start_date) & (ends < test_start_date)
    ].copy()
    test_mask = dates >= test_start_date
    if end_date is not None:
        test_mask &= (dates <= end_date) & (ends <= end_date)
    test = work.loc[test_mask].copy()
    return TemporalSplit(train, validation, test, str(val_start)[:10], str(test_start_date)[:10])
