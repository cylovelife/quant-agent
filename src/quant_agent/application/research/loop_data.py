"""Strict synthetic phase contract for the first controlled research entry.

@author cylovelife
"""

from __future__ import annotations

import json
import math
from datetime import date
from decimal import Decimal
from pathlib import Path

import pandas as pd

from quant_agent.application.research.model_selection import FACTORS_CORE
from quant_agent.domain.market.serialization import digest
from quant_agent.domain.trading.execution import Instrument, SessionFacts


def read_artifact(artifact: dict) -> dict:
    """Hash the same bytes subsequently parsed; never reopen between checks."""
    raw = Path(artifact["path"]).read_bytes()
    if digest(raw) != artifact["sha256"]:
        raise ValueError("phase artifact digest differs")
    return json.loads(raw)


def check_phase(data: dict, phase: str, split: dict) -> None:
    if set(data) != {"kind", "no_corporate_actions", "sessions", "instruments", "rows"}:
        raise ValueError("unknown or missing synthetic data fields")
    if data["kind"] != "synthetic" or data["no_corporate_actions"] is not True:
        raise ValueError("only synthetic data without corporate actions is supported")
    dates = data["sessions"]
    if not isinstance(dates, list) or len(dates) < 2 or dates != sorted(set(dates)):
        raise ValueError("at least two ordered unique sessions required")
    for day in dates:
        if date.fromisoformat(day).isoformat() != day:
            raise ValueError("canonical session dates required")
        if phase == "validation":
            valid = split["validation_start"] <= day < split["test_start"]
        else:
            valid = split["test_start"] <= day <= split["test_end"]
        if not valid:
            raise ValueError("phase date crosses frozen boundary")
    instruments = [Instrument(**item) for item in data["instruments"]]
    codes = {item.code for item in instruments}
    if not codes or len(codes) != len(instruments):
        raise ValueError("unique explicit instruments required")
    if any(item.market != "cn" or item.profile != "cn_mainboard" for item in instruments):
        raise ValueError("first research entry supports mainland equities only")
    seen = set()
    for row in data["rows"]:
        if set(row) != {"date", "code", "adjust", "open", "close", "known_at", "factors", "facts"}:
            raise ValueError("unknown or missing phase row fields")
        key = row["date"], row["code"]
        if key in seen or key[0] not in dates or key[1] not in codes:
            raise ValueError("duplicate or undeclared row")
        seen.add(key)
        if row["adjust"] != "raw" or row["known_at"] != row["date"] + "@close":
            raise ValueError("raw prices and close-time factors required")
        for field in ("open", "close"):
            number = Decimal(row[field])
            if (
                not number.is_finite()
                or not 0 < number <= Decimal("1e12")
                or number % Decimal("0.01")
            ):
                raise ValueError("positive cent-aligned raw prices required")
        if set(row["factors"]) != set(FACTORS_CORE) or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0 <= value <= 100
            for value in row["factors"].values()
        ):
            raise ValueError("complete finite synthetic factor scores required")
        facts = SessionFacts(**row["facts"])
        if facts.date != row["date"] or facts.code != row["code"] or facts.market != "cn":
            raise ValueError("session facts identity mismatch")
        index = dates.index(row["date"])
        if index and facts.previous_session != dates[index - 1]:
            raise ValueError("next session mismatch")
    if len(seen) != len(dates) * len(codes):
        raise ValueError("complete declared universe required for every session")


def snapshot_frames(data: dict) -> dict[tuple[str, str, str], pd.DataFrame]:
    frames = {}
    for instrument in data["instruments"]:
        rows = [row for row in data["rows"] if row["code"] == instrument["code"]]
        frames[(instrument["market"], instrument["code"], "raw")] = pd.DataFrame(
            [
                {
                    "date": row["date"],
                    "open": float(row["open"]),
                    "close": float(row["close"]),
                    "high": max(float(row["open"]), float(row["close"])),
                    "low": min(float(row["open"]), float(row["close"])),
                    "volume": 10000.0,
                    "amount": 100000.0,
                    "src": "synthetic",
                }
                for row in sorted(rows, key=lambda item: item["date"])
            ]
        )
    return frames
