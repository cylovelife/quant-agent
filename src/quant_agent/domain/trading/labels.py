"""Daily signal, next-open fill and close-label contract for research simulation.

@author cylovelife
"""

import hashlib
import json
from dataclasses import asdict, dataclass
from math import isfinite

import numpy as np
import pandas as pd

from quant_agent.domain.market.quality import assess_bars

LABEL_VERSION = "next_open_close_v1"
SUPPORTED_MARKETS = {"cn", "etf"}


@dataclass(frozen=True)
class LabelSpec:
    hold_days: int = 5
    stop_loss: float | None = None
    take_profit: float | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.hold_days, bool)
            or not isinstance(self.hold_days, int)
            or self.hold_days < 1
        ):
            raise ValueError("hold_days must be a positive integer")
        for name, value in (
            ("stop_loss", self.stop_loss),
            ("take_profit", self.take_profit),
        ):
            if value is not None and (
                not isfinite(value) or (value > 0 if name == "stop_loss" else value < 0)
            ):
                raise ValueError(f"invalid {name}")

    @classmethod
    def from_dict(cls, value: dict) -> "LabelSpec":
        return cls(value.get("hold_days", 5), value.get("stop_loss"), value.get("take_profit"))

    @classmethod
    def from_params(cls, params: dict) -> "LabelSpec":
        return cls.from_dict(params.get("backtest", {}))

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def label_id(self) -> str:
        payload = json.dumps([LABEL_VERSION, self.to_dict()], sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


def positive_price(value: object) -> bool:
    try:
        return isfinite(float(value)) and float(value) > 0
    except (TypeError, ValueError):
        return False


def resolve_trade(
    k: pd.DataFrame | None,
    signal_date: str,
    spec: LabelSpec,
    asof: str | None = None,
    calendar: list[str] | None = None,
) -> dict:
    """Resolve only bars available by asof; entry day counts as holding day one.

    A supplied calendar detects missing expected bars. Without it, dates refer to
    observed instrument bars, not a verified exchange calendar or executable fills.
    """
    result = {
        "label_version": LABEL_VERSION,
        "label_id": spec.label_id,
        "signal_date": signal_date,
        "signal_time": signal_date + "@close",
        "entry_date": None,
        "entry_time": None,
        "entry_price": None,
        "label_end_date": None,
        "label_end_time": None,
        "exit_price": None,
        "ret": None,
        "days_held": 0,
        "reason": None,
        "status": "unavailable",
        "last_price": None,
        "last_date": None,
        "peak_ret": 0.0,
    }
    if k is None or k.empty or not {"date", "open", "close"}.issubset(k.columns):
        return result
    dates = pd.to_datetime(k["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    if dates.isna().any():
        return result
    mask = dates <= asof if asof else dates.notna()
    frame = k.loc[mask].copy()
    frame["date"] = dates.loc[mask]
    if frame.empty or frame.date.duplicated().any() or not frame.date.is_monotonic_increasing:
        return result
    if signal_date not in set(frame.date):
        return result
    segment = frame[frame.date > signal_date].head(spec.hold_days)
    expected = [
        day for day in (calendar or []) if signal_date < day <= (asof or frame.date.iloc[-1])
    ]
    result["status"] = "pending"
    if segment.empty:
        if expected:
            result["status"] = "unavailable"
        return result
    entry = segment.iloc[0]["open"]
    if not positive_price(entry):
        result["status"] = "unavailable"
        return result
    result.update(
        entry_date=segment.date.iloc[0],
        entry_time=segment.date.iloc[0] + "@open",
        entry_price=float(entry),
        status="open",
    )
    for number, row in enumerate(segment.itertuples(), 1):
        if expected and (number > len(expected) or row.date != expected[number - 1]):
            result["status"] = "unavailable"
            return result
        if assess_bars(segment.iloc[[number - 1]], row.date, require_current=False)["issues"]:
            result["status"] = "unavailable"
            return result
        if not positive_price(row.close):
            result["status"] = "unavailable"
            return result
        ret = float(row.close) / float(entry) - 1
        result.update(
            days_held=number,
            last_price=float(row.close),
            last_date=row.date,
            peak_ret=max(result["peak_ret"], ret),
        )
        reason = None
        if spec.stop_loss is not None and ret <= spec.stop_loss:
            reason = "止损"
        elif spec.take_profit is not None and ret >= spec.take_profit:
            reason = "止盈"
        elif number == spec.hold_days:
            reason = "持有到期"
        if reason:
            result.update(
                status="mature",
                label_end_date=row.date,
                label_end_time=row.date + "@close",
                exit_price=float(row.close),
                ret=ret,
                reason=reason,
            )
            return result
    if len(segment) < min(len(expected), spec.hold_days):
        result["status"] = "unavailable"
    return result


def forward_labels(k: pd.DataFrame, spec: LabelSpec) -> pd.DataFrame:
    """One resolved label per signal bar; the no-stop label keeps a vectorized path."""
    dates = pd.to_datetime(k["date"]).dt.strftime("%Y-%m-%d")
    if spec.stop_loss is not None or spec.take_profit is not None:
        return pd.DataFrame([resolve_trade(k, day, spec) for day in dates], index=k.index)
    entry = pd.to_numeric(k["open"], errors="coerce").shift(-1)
    exit_ = pd.to_numeric(k["close"], errors="coerce").shift(-spec.hold_days)
    valid = (entry > 0) & np.isfinite(entry) & (exit_ > 0) & np.isfinite(exit_)
    return pd.DataFrame(
        {
            "signal_date": dates,
            "entry_date": dates.shift(-1),
            "label_end_date": dates.shift(-spec.hold_days),
            "entry_price": entry,
            "exit_price": exit_,
            "ret": (exit_ / entry.where(valid) - 1).where(valid),
            "label_version": LABEL_VERSION,
            "label_id": spec.label_id,
        },
        index=k.index,
    )
