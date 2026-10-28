"""Read-only raw opening-price adapter; never repurpose valuation/qfq labels as fills.

@author cylovelife
"""

from dataclasses import replace

import pandas as pd

from quant_agent.domain.market.quality import capture_provenance
from quant_agent.domain.trading.execution import Instrument, OpenQuote, SessionFacts, _identity
from quant_agent.infrastructure.persistence.local import LocalData


def opening_quote(
    frame: pd.DataFrame | None,
    instrument: Instrument,
    session: str,
    facts: SessionFacts,
) -> OpenQuote:
    """Read only the target open and its source, not subsequent daily fields.

    This is a historical/paper price observation, not proof it was delivered at
    the opening instant. Suspension, exchange limits and opening capacity are
    supplied separately with their own timing and origin.
    """
    empty = OpenQuote(
        instrument.market, instrument.code, session, None, "raw", None, None, "", facts
    )
    if frame is None or frame.empty:
        return replace(empty, issues=("raw_session_missing",))
    provenance = frame.attrs.get("data_provenance", {})
    if provenance.get("aggregation"):
        return replace(empty, issues=("non_daily_raw_bars",))
    if provenance.get("adjust", frame.attrs.get("adjust")) != "raw":
        return replace(empty, issues=("raw_adjustment_unverified",))
    if not {"date", "open"}.issubset(frame.columns):
        return replace(empty, issues=("raw_open_missing",))
    dates = pd.to_datetime(frame["date"], errors="coerce").dt.strftime("%Y-%m-%d")
    target = frame.loc[dates == session, ["date", "open"]].copy()
    if len(target) != 1:
        return replace(empty, issues=("raw_session_missing_or_duplicate",))
    try:
        metadata = capture_provenance(target, instrument.market, instrument.code, "raw")
    except ValueError:
        return replace(empty, issues=("raw_identity_mismatch",))
    if metadata.get("ingest_issues"):
        return replace(empty, issues=tuple(metadata["ingest_issues"]))
    observation = metadata["observations"][0]
    opening = str(target.open.iloc[0])
    version = _identity([instrument.market, instrument.code, "raw", session, opening, observation])
    return replace(
        empty,
        open_price=opening,
        source=observation["source"],
        fetched_at=observation["fetched_at"],
        data_version=version,
    )


def local_opening_quote(
    reader: LocalData, instrument: Instrument, session: str, facts: SessionFacts
) -> OpenQuote:
    """Exact raw partition and exact session; no network, stale fallback or date advance."""
    frames = reader.get_data(
        instrument.market, [instrument.code], adjust="raw", start=session, end=session
    )
    return opening_quote(frames[instrument.code], instrument, session, facts)


def local_closing_quote(reader: LocalData, market: str, code: str, session: str) -> dict:
    """Read the exact raw close and preserve its observed source/version."""
    frame = reader.get_data(market, [code], adjust="raw", start=session, end=session)[code]
    provenance = frame.attrs.get("data_provenance", {})
    observations = provenance.get("observations", [])
    if (
        len(frame) != 1
        or provenance.get("adjust") != "raw"
        or provenance.get("ingest_issues")
        or len(observations) != 1
        or not observations[0].get("source")
    ):
        raise ValueError("same-day raw close or source unavailable")
    return {
        "market": market,
        "code": code,
        "date": session,
        "adjust": "raw",
        "close": str(frame["close"].iloc[0]),
        "source": observations[0]["source"],
        "data_version": _identity(
            [market, code, session, str(frame["close"].iloc[0]), observations[0]]
        ),
    }
