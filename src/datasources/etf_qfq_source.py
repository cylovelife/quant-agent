"""Sina ETF daily bars adjusted only with explicit cash-dividend qfq evidence.

@author cylovelife
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from data_quality import assess_bars
from .base import SourceGuard

SOURCE = "sina_etf_cash_qfq_v1"
RAW_URL = "https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_quant_etf_kline=/CN_MarketDataService.getKLineData"
FACTOR_URL = "https://finance.sina.com.cn/realstock/company/{symbol}/qfq.js"
TURNOVER_URL = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
_GUARD = None


def guard() -> SourceGuard:
    global _GUARD
    if _GUARD is None:
        _GUARD = SourceGuard(SOURCE, min_gap=0.5, budget_sec=90, retries=1)
    return _GUARD


def _symbol(code: str) -> str:
    if not isinstance(code, str) or not re.fullmatch(r"[15]\d{5}", code):
        raise ValueError("six-digit Shanghai/Shenzhen ETF code required")
    return ("sh" if code.startswith("5") else "sz") + code


def _assignment(text: str, variable: str, *, parenthesized: bool = False) -> object:
    match = re.search(r"\bvar\s+" + re.escape(variable) + r"\s*=\s*", text)
    if not match:
        raise ValueError("Sina response identity or assignment is missing")
    payload = text[match.end() :].lstrip()
    if parenthesized:
        if not payload.startswith("("):
            raise ValueError("invalid Sina daily JSONP envelope")
        payload = payload[1:]
    value, end = json.JSONDecoder().raw_decode(payload)
    tail = payload[end:].strip()
    if parenthesized:
        if not tail.startswith(")"):
            raise ValueError("unterminated Sina daily JSONP envelope")
        tail = tail[1:].lstrip()
    if tail.startswith(";"):
        tail = tail[1:].lstrip()
    if tail and not re.fullmatch(r"/\*.*?\*/", tail, flags=re.S):
        raise ValueError("unexpected executable text after Sina payload")
    return value


def normalize_qfq(
    code: str, raw_text: str, factor_text: str, *, supplement: dict | None = None
) -> tuple[pd.DataFrame, dict]:
    """Apply raw-u only for explicit f=s=1 factors; reject unsupported splits."""
    symbol = _symbol(code)
    raw = _assignment(raw_text, "_quant_etf_kline", parenthesized=True)
    factors = _assignment(factor_text, symbol + "qfq")
    if not isinstance(raw, list) or not raw or not isinstance(factors, dict):
        raise ValueError("Sina daily bars or qfq factors are empty")
    factor_rows = factors.get("data")
    if (
        not isinstance(factor_rows, list)
        or not factor_rows
        or type(factors.get("total")) is not int
        or factors["total"] != len(factor_rows)
    ):
        raise ValueError("Sina qfq factor coverage/count is incomplete")
    try:
        table = pd.DataFrame(factor_rows)
        dates = pd.to_datetime(table["d"], errors="raise")
        numbers = table[["f", "s", "u"]].apply(pd.to_numeric, errors="raise")
        if (
            dates.duplicated().any()
            or not dates.is_monotonic_decreasing
            or not np.isfinite(numbers.to_numpy()).all()
            or (numbers[["f", "s"]] != 1).any().any()
            or (numbers.u < 0).any()
            or not numbers.u.is_monotonic_increasing
            or numbers.u.iloc[0] != 0
        ):
            raise ValueError("unsupported or inconsistent cash-only qfq factors")
        frame = pd.DataFrame(raw).rename(columns={"day": "date"})
        frame = frame[["date", "open", "close", "high", "low", "volume"]].copy()
        frame["date"] = pd.to_datetime(frame["date"], errors="raise")
        for column in ("open", "close", "high", "low", "volume"):
            frame[column] = pd.to_numeric(frame[column], errors="raise")
    except (KeyError, TypeError) as exc:
        raise ValueError("Sina daily/factor fields are missing or malformed") from exc
    if dates.min() > frame.date.min() or dates.max() > frame.date.max():
        raise ValueError(
            "Sina qfq factors do not cover the raw window or are future-dated"
        )
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    last_day = str(frame.date.max())[:10]
    if (
        last_day > today
        or assess_bars(frame, last_day, require_current=False)["issues"]
    ):
        raise ValueError("invalid Sina raw daily window")
    if not isinstance(supplement, dict) or supplement.get("symbol") != symbol:
        raise ValueError("matching raw turnover source identity is required")
    if not supplement.get("source") or not isinstance(supplement.get("day"), list):
        raise ValueError("raw turnover source or rows are missing")
    turnover = {}
    for row in supplement["day"]:
        if not isinstance(row, list) or len(row) <= 8 or not isinstance(row[6], dict):
            raise ValueError("raw turnover source lacks actual amount fields")
        day = str(row[0])
        if day in turnover:
            raise ValueError("raw turnover source contains duplicate dates")
        try:
            values = np.asarray([float(row[i]) for i in (1, 2, 3, 4, 8)])
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "raw turnover source contains invalid prices or amounts"
            ) from exc
        if not np.isfinite(values).all() or (values[:4] <= 0).any() or values[4] < 0:
            raise ValueError("raw turnover source contains invalid prices or amounts")
        turnover[day] = values
    actual_amounts = []
    for row in frame.itertuples():
        observed = turnover.get(str(row.date)[:10])
        if observed is None or not np.allclose(
            observed[:4], [row.open, row.close, row.high, row.low], rtol=0, atol=5e-7
        ):
            raise ValueError(
                "raw turnover source is missing a date or differs from Sina OHLC"
            )
        actual_amounts.append(float(observed[4]) * 10000)
    adjustments = []
    for day in frame.date:
        eligible = np.flatnonzero(dates <= day)
        if not len(eligible):
            raise ValueError("Sina qfq factor missing for a raw daily bar")
        adjustments.append(float(numbers.u.iloc[eligible[0]]))
    for column in ("open", "close", "high", "low"):
        frame[column] = (frame[column] - np.asarray(adjustments)).round(6)
    # Sina daily volume is shares; Tencent/project daily volume is 100-share lots.
    frame["volume"] = frame.volume / 100
    frame["amount"] = actual_amounts
    if assess_bars(frame, last_day, require_current=False)["issues"]:
        raise ValueError("Sina qfq adjustment produced unusable daily bars")
    acquired = datetime.now(timezone.utc).isoformat()
    receipt = {
        "format": 1,
        "market": "etf",
        "code": code,
        "symbol": symbol,
        "method": "cash_only_qfq_price_equals_raw_minus_u_f_and_s_equal_one",
        "raw_source": RAW_URL,
        "raw_parameters": {
            "symbol": symbol,
            "scale": "240",
            "ma": "no",
            "datalen": "1023",
        },
        "factor_url": FACTOR_URL.format(symbol=symbol),
        "fetched_at": acquired,
        "factor_rows": factor_rows,
        "factor_response_sha256": hashlib.sha256(factor_text.encode()).hexdigest(),
        "raw_response_sha256": hashlib.sha256(raw_text.encode()).hexdigest(),
        "raw_first_date": str(frame.date.iloc[0])[:10],
        "raw_last_date": str(frame.date.iloc[-1])[:10],
        "volume_unit": "100_shares",
        "turnover_supplement": supplement,
        "amount_status": "actual_tencent_day_field_8_in_10000_CNY",
        "split_support": False,
    }
    frame.attrs.update(adjust="qfq", fetched_at=acquired, adjustment_provenance=receipt)
    return frame, receipt


def _publish_receipt(receipt: dict, directory: Path | None) -> str:
    data = json.dumps(
        receipt,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()
    digest = hashlib.sha256(data).hexdigest()
    if directory is not None:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{digest}.json"
        descriptor, temporary = tempfile.mkstemp(prefix=".receipt-", dir=directory)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
        finally:
            os.unlink(temporary)
        if path.read_bytes() != data:
            raise ValueError("ETF qfq factor receipt is incomplete or corrupted")
    return digest


def fetch_etf_qfq(
    code: str,
    limit: int,
    *,
    get: Callable,
    receipt_dir: Path | None = None,
    supplement: dict | None = None,
) -> pd.DataFrame:
    """Guarded, bounded fallback; no usable qfq is returned without factor evidence."""
    symbol = _symbol(code)
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("ETF qfq fallback supports 1..1000 daily bars")

    def download() -> pd.DataFrame:
        raw = get(
            RAW_URL,
            {"symbol": symbol, "scale": "240", "ma": "no", "datalen": "1023"},
            retries=1,
            timeout=8,
            min_gap=0.35,
        ).text
        original_raw_sha256 = hashlib.sha256(raw.encode()).hexdigest()
        factors = get(
            FACTOR_URL.format(symbol=symbol), retries=1, timeout=8, min_gap=0.35
        ).text
        # Keep the full requested window consistent across all actual fields.
        raw_rows = _assignment(raw, "_quant_etf_kline", parenthesized=True)
        if not isinstance(raw_rows, list) or not raw_rows:
            raise ValueError("Sina raw daily window is empty")
        raw_rows = raw_rows[-(limit + 1) :]
        raw = "var _quant_etf_kline=(" + json.dumps(raw_rows) + ");"
        required_days = {row.get("day") for row in raw_rows if isinstance(row, dict)}
        selected = supplement
        supplied_days = {
            str(row[0])
            for row in (selected or {}).get("day", [])
            if isinstance(row, list) and row
        }
        if not required_days.issubset(supplied_days):
            params = {"param": f"{symbol},day,,,{min(1000, limit + 10)},"}
            reply = get(TURNOVER_URL, params, retries=1, timeout=8, min_gap=0.35).json()
            if not isinstance(reply, dict) or not isinstance(reply.get("data"), dict):
                raise ValueError("invalid raw turnover response envelope")
            payload = reply["data"].get(symbol)
            if not isinstance(payload, dict) or not isinstance(
                payload.get("day"), list
            ):
                raise ValueError("raw turnover response lacks matching symbol/day rows")
            selected = {
                "symbol": symbol,
                "source": "tencent_proxy_ifzq_day",
                "endpoint": TURNOVER_URL,
                "parameters": params,
                "day": payload["day"],
            }
        # Unsupported actions/coverage are specific to this ETF. A rejected
        # factor table must not trip a source-wide schema breaker for other ETFs.
        frame, receipt = normalize_qfq(code, raw, factors, supplement=selected)
        receipt["raw_response_sha256"] = original_raw_sha256
        receipt_id = _publish_receipt(
            receipt, Path(receipt_dir) if receipt_dir is not None else None
        )
        frame.attrs["source"] = SOURCE + ":" + receipt_id
        return frame.tail(limit + 1).reset_index(drop=True)

    result = guard().call("etf", download)
    if result.ok:
        return result.df
    empty = pd.DataFrame()
    empty.attrs["fetch_failure"] = {
        "kind": "adjustment_unavailable",
        "reason": result.error or result.kind,
    }
    return empty
