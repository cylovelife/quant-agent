"""Observable daily-data provenance and fail-closed admission for paper signals.

@author cylovelife
"""

import hashlib
import json
from datetime import date, datetime

import numpy as np
import pandas as pd

PRICE_FIELDS = ("open", "close", "high", "low")
VALUE_FIELDS = (*PRICE_FIELDS, "volume", "amount")


def completed_bars(frame: pd.DataFrame, cutoff: str) -> pd.DataFrame:
    """Exclude unfinished sessions while retaining evidence of malformed input dates."""
    dates = pd.to_datetime(frame["date"], errors="coerce")
    result = frame[dates.dt.strftime("%Y-%m-%d") <= cutoff].copy()
    if dates.isna().any():
        result.attrs["ingest_issues"] = list(
            dict.fromkeys([*frame.attrs.get("ingest_issues", []), "invalid_dates"])
        )
    return result


def _valid_acquisition(value: object) -> bool:
    if not isinstance(value, str) or len(value) <= 10:
        return False
    try:
        # Preserve old SQLite times' unspecified timezone; reject placeholders.
        datetime.fromisoformat(str(value))
        return True
    except ValueError:
        return False


def price_fingerprint(frame: pd.DataFrame) -> str:
    """Hash dates and numerical observations, independently of acquisition time."""
    records = []
    for row in frame.reindex(columns=("date", *VALUE_FIELDS)).to_dict("records"):
        values = {"date": str(row["date"])[:10]}
        for field in VALUE_FIELDS:
            value = pd.to_numeric(row[field], errors="coerce")
            values[field] = float(value) if pd.notna(value) and np.isfinite(value) else None
        records.append(values)
    return hashlib.sha256(json.dumps(records, sort_keys=True, allow_nan=False).encode()).hexdigest()


def capture_provenance(frame: pd.DataFrame, market: str, code: str, adjust: str) -> dict:
    """Preserve observed source/times, never infer publication or historical visibility."""
    existing = frame.attrs.get("data_provenance", {})
    if frame.attrs.get("adjust", adjust) != adjust:
        raise ValueError("response adjustment does not match stored partition")
    for key, value in (("market", market), ("code", code), ("adjust", adjust)):
        if existing.get(key, value) != value:
            raise ValueError("series provenance does not match market/code/adjustment")
    prior = {row["date"]: row for row in existing.get("observations", [])}
    observations = []
    for row in frame.to_dict("records"):
        day = str(row["date"])[:10]
        known = prior.get(day, {})
        source = row.get("src", known.get("source", frame.attrs.get("source")))
        fetched = row.get("fetched_at", known.get("fetched_at", frame.attrs.get("fetched_at")))
        observations.append(
            {
                "date": day,
                "source": source if pd.notna(source) else None,
                "fetched_at": fetched if pd.notna(fetched) else None,
                "published_at": known.get("published_at"),
            }
        )
    result = {
        "format": 1,
        "market": market,
        "code": code,
        "adjust": adjust,
        "price_version": price_fingerprint(frame),
        "observations": observations,
        "publication_status": "not_provided",
        "point_in_time_complete": False,
        "history_scope": "versions_observed_and_frozen_locally",
    }
    if "aggregation" in existing:
        result["aggregation"] = existing["aggregation"]
    if frame.attrs.get("ingest_issues") or existing.get("ingest_issues"):
        result["ingest_issues"] = frame.attrs.get("ingest_issues") or existing["ingest_issues"]
    return result


def assess_bars(
    frame: pd.DataFrame | None,
    asof: str,
    max_age_days: int = 7,
    sessions: list[str] | None = None,
    require_current: bool = True,
    freshness_asof: str | None = None,
) -> dict:
    """Validate without repairing prices, filling gaps, or declaring suspensions."""
    issues = list(
        (
            frame.attrs.get("ingest_issues")
            or frame.attrs.get("data_provenance", {}).get("ingest_issues", [])
        )
        if frame is not None
        else []
    )
    result = {
        "issues": issues,
        "last_date": None,
        "missing_sessions": [],
        "new_signal_allowed": False,
    }
    if frame is None or frame.empty:
        issues.append("missing")
        return result
    if not {"date", *PRICE_FIELDS}.issubset(frame.columns):
        issues.append("missing_price_columns")
        return result
    dates = pd.to_datetime(frame["date"], errors="coerce")
    if dates.isna().any():
        issues.append("invalid_dates")
        return result
    days = dates.dt.strftime("%Y-%m-%d")
    result["last_date"] = days.max()
    if days.duplicated().any():
        issues.append("duplicate_dates")
    if not days.is_monotonic_increasing:
        issues.append("unordered_dates")
    if (days > asof).any():
        issues.append("future_or_unclosed_bars")
    prices = frame[list(PRICE_FIELDS)].apply(pd.to_numeric, errors="coerce")
    if not np.isfinite(prices.to_numpy()).all() or (prices <= 0).any().any():
        issues.append("invalid_price")
    if (prices.high < prices[["open", "close", "low"]].max(axis=1)).any() or (
        prices.low > prices[["open", "close", "high"]].min(axis=1)
    ).any():
        issues.append("invalid_ohlc_range")
    for field in ("volume", "amount"):
        if field in frame:
            values = pd.to_numeric(frame[field], errors="coerce")
            if not np.isfinite(values).all() or (values < 0).any():
                issues.append("invalid_" + field)
    if "volume" in frame and pd.to_numeric(frame.volume, errors="coerce").iloc[-1] == 0:
        issues.append("zero_volume_status_unknown")
    if (
        date.fromisoformat(freshness_asof or asof) - date.fromisoformat(days.max())
    ).days > max_age_days:
        issues.append("stale")
    if require_current and days.max() != asof:
        issues.append("latest_session_missing")
    missing = [
        day for day in (sessions or []) if days.min() <= day <= asof and day not in set(days)
    ]
    if missing:
        result["missing_sessions"] = missing
        issues.append("missing_observed_sessions")
    result["new_signal_allowed"] = not issues
    return result


def build_data_contract(
    frames: dict,
    universe: dict[str, list[str]],
    asof: str,
    max_age_days: int = 7,
    require_provenance: bool = True,
    reference_key: tuple[str, str, str] | None = None,
    freshness_asof: str | None = None,
) -> dict:
    """Freeze current membership and quality facts, not a fabricated historical universe."""
    reference_key = reference_key or (
        ("etf", "510300", "qfq") if ("etf", "510300", "qfq") in frames else None
    )
    reference = frames.get(reference_key) if reference_key else None
    if reference_key:
        reference_check = assess_bars(reference, asof, max_age_days, freshness_asof=freshness_asof)
        reference_ok = reference_check["new_signal_allowed"]
        sessions = (
            pd.to_datetime(reference.date).dt.strftime("%Y-%m-%d").tolist() if reference_ok else []
        )
        calendar_kind = "observed_reference_sessions"
    else:
        sessions = sorted(
            {
                str(day)[:10]
                for frame in frames.values()
                if frame is not None
                for day in frame.get("date", [])
                if str(day)[:10] <= asof
            }
        )
        reference_ok = bool(sessions) and sessions[-1] == asof
        calendar_kind = "observed_universe_sessions"
    series = {}
    for (market, code, adjust), frame in frames.items():
        check = assess_bars(frame, asof, max_age_days, sessions, freshness_asof=freshness_asof)
        if not reference_ok:
            check["issues"].append("reference_session_unavailable")
        if adjust != "qfq":
            check["issues"].append("wrong_scoring_adjustment")
        try:
            metadata = (
                capture_provenance(frame, market, code, adjust) if frame is not None else None
            )
        except ValueError:
            metadata = None
            check["issues"].append("provenance_identity_mismatch")
        observations = (metadata or {}).get("observations", [])
        if require_provenance and (
            not observations
            or any(not r["source"] or not _valid_acquisition(r["fetched_at"]) for r in observations)
        ):
            check["issues"].append("acquisition_provenance_incomplete")
        check["data_usable"] = not check["issues"]
        check["new_signal_allowed"] = (
            check["data_usable"]
            and code in universe.get(market, [])
            and (freshness_asof is None or asof == freshness_asof)
        )
        check["price_version"] = (metadata or {}).get("price_version")
        check["provenance"] = metadata
        series[f"{market}:{code}"] = check
    for market, codes in universe.items():
        for code in codes:
            series.setdefault(
                f"{market}:{code}",
                {
                    "issues": ["not_loaded"],
                    "data_usable": False,
                    "new_signal_allowed": False,
                },
            )
    if (
        require_provenance
        and reference_key
        and not series.get(f"{reference_key[0]}:{reference_key[1]}", {}).get("data_usable")
    ):
        for check in series.values():
            check["issues"].append("reference_provenance_unavailable")
            check["data_usable"] = check["new_signal_allowed"] = False
    return {
        "format": 1,
        "asof": asof,
        "freshness_asof": freshness_asof or asof,
        "universe": {m: sorted(set(codes)) for m, codes in universe.items()},
        "universe_kind": "observed_or_configured_membership",
        "historical_membership_complete": False,
        "listing_status_coverage": "unknown",
        "suspension_status_coverage": "unknown",
        "publication_time_coverage": "unknown",
        "point_in_time_complete": False,
        "calendar": {
            "kind": calendar_kind,
            "official_calendar": False,
            "reference": list(reference_key) if reference_key else None,
            "sessions": sessions,
            "usable_at_cutoff": reference_ok,
        },
        "series": series,
    }


def financial_available_date(row: dict) -> str | None:
    """Only actual disclosure dates are eligible; report dates/deadlines are not substitutes."""
    if row.get("_notice_source") not in (None, "disclosure"):
        return None
    value = str(row.get("NOTICE_DATE") or "")[:10]
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return None
