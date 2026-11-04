"""Compose pure forecasts with existing observation records without changing scores.

@author cylovelife
"""

import pandas as pd

from quant_agent.domain.research.market_forecast import forecast_short_term
from quant_agent.domain.trading.labels import SUPPORTED_MARKETS


def attach_forecasts(
    observations: list[dict], frames: dict[tuple[str, str], pd.DataFrame], *, asof: str
) -> list[dict]:
    results = []
    forecasts = {}
    for row in observations:
        # Only these markets have the application's completed-bar/admission policy.
        if row["market"] not in SUPPORTED_MARKETS:
            results.append(dict(row))
            continue
        key = (row["market"], str(row["code"]))
        bars = frames.get(key)
        if bars is None or bars.empty:
            results.append(dict(row))
            continue
        if key not in forecasts:
            forecasts[key] = forecast_short_term(bars, adjust="qfq", asof=asof)
        results.append(dict(row, forecast=forecasts[key]))
    return results
