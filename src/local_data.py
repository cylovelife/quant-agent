"""Offline batch market-data queries; synchronization is a separate operation.

@author caiyanliangbj
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path
from typing import Sequence

import pandas as pd

BAR_FIELDS = ("date", "open", "close", "high", "low", "volume", "amount", "src")
PRICE_FIELDS = ("open", "close", "high", "low")


def iso_date(value: str) -> str:
    """Require canonical ISO dates so SQL lexical bounds stay meaningful."""
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("date must be YYYY-MM-DD")
    return value


class LocalData:
    """Read existing quant-agent SQLite without creating/migrating it or networking."""

    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()

    def get_data(
        self,
        market: str,
        codes: Sequence[str],
        *,
        adjust: str = "qfq",
        start: str | None = None,
        end: str | None = None,
        frequency: str = "1d",
        fields: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> dict[str, pd.DataFrame]:
        """Inclusive range, latest limit per symbol, ascending actual observation dates.

        Weekly/monthly bars can be partial at the requested cutoff; their date is
        the last observed session, never a future period-end label. No adjustment
        conversion is performed: callers explicitly select the stored series.
        """
        if market not in {"cn", "etf", "hk", "us", "fund"}:
            raise ValueError("unsupported market")
        if adjust not in {"qfq", "raw", "nav"}:
            raise ValueError("unsupported stored adjustment")
        if frequency not in {"1d", "1w", "1M"}:
            raise ValueError(
                "frequency must be 1d, 1w or 1M; minute data is unavailable"
            )
        projection = list(BAR_FIELDS if fields is None else fields)
        if (
            not projection
            or len(set(projection)) != len(projection)
            or any(field not in BAR_FIELDS for field in projection)
        ):
            raise ValueError("unknown or duplicate fields")
        if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or limit < 1
        ):
            raise ValueError("limit must be a positive integer")
        if start:
            iso_date(start)
        if end:
            iso_date(end)
        if start and end and start > end:
            raise ValueError("start must not follow end")
        if isinstance(codes, str) or any(
            not isinstance(c, str) or not c.strip() for c in codes
        ):
            raise ValueError("codes must be a sequence of nonempty strings")
        symbols = list(dict.fromkeys(codes))
        if not self.path.is_file():
            raise FileNotFoundError(self.path)
        result = {code: pd.DataFrame(columns=projection) for code in symbols}
        if not symbols:
            return result
        # One read transaction for the entire batch, including SQLite parameter chunks.
        with closing(
            sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True)
        ) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN")
            for offset in range(0, len(symbols), 300):
                batch = symbols[offset : offset + 300]
                placeholders = ",".join("?" for _ in batch)
                sql = (
                    f"SELECT code, {','.join(BAR_FIELDS)} FROM kline "
                    f"WHERE market=? AND adjust=? AND code IN ({placeholders})"
                )
                values = [market, adjust, *batch]
                if start:
                    sql += " AND date>=?"
                    values.append(start)
                if end:
                    sql += " AND date<=?"
                    values.append(end)
                sql += " ORDER BY code,date"
                rows = [dict(row) for row in conn.execute(sql, values)]
                if not rows:
                    continue
                data = pd.DataFrame(rows)
                data["date"] = pd.to_datetime(data["date"], errors="raise")
                for code, group in data.groupby("code", sort=False):
                    bars = group[list(BAR_FIELDS)].reset_index(drop=True)
                    if frequency != "1d":
                        bars = _aggregate(bars, frequency)
                    if limit is not None:
                        bars = bars.tail(limit)
                    result[code] = bars[projection].reset_index(drop=True)
        return result


def _aggregate(bars: pd.DataFrame, frequency: str) -> pd.DataFrame:
    period = bars["date"].dt.to_period("W-FRI" if frequency == "1w" else "M")
    return (
        bars.groupby(period, sort=True)
        .agg(
            date=("date", "last"),
            open=("open", "first"),
            close=("close", "last"),
            high=("high", "max"),
            low=("low", "min"),
            volume=("volume", lambda s: s.sum(min_count=1)),
            amount=("amount", lambda s: s.sum(min_count=1)),
            src=("src", "last"),
        )
        .reset_index(drop=True)[list(BAR_FIELDS)]
    )
