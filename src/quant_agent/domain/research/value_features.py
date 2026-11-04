"""Continuous value features and explicit evidence gaps, separate from ranking weights.

@author cylovelife
"""

import math

VALUE_FEATURES = (
    "vf_roe",
    "vf_roe_floor",
    "vf_growth",
    "vf_profit_growth",
    "vf_cash_conversion",
    "vf_leverage",
    "vf_liquidity",
    "vf_earnings_yield",
    "vf_pb_percentile",
    "vf_quality_value",
    "vf_cash_value",
)
VALUE_FEATURE_VERSION = 2


def _number(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _first(*values: object) -> float | None:
    return next((v for x in values if (v := _number(x)) is not None), None)


def _scaled(value: object, scale: float = 100.0) -> float | None:
    number = _number(value)
    return number / scale if number is not None else None


def financial_like(metrics: dict) -> bool:
    """Keep the existing heuristic explicit; this is not verified industry membership."""
    return (
        _number(metrics.get("gross_margin")) is None
        and (_number(metrics.get("debt_ratio")) or 0) > 80
    )


def value_features(metrics: dict) -> dict[str, float | None]:
    """Use already as-of-filtered metrics; never convert missing fields to neutral scores."""
    roe = _scaled(_first(metrics.get("roe_avg3"), metrics.get("roe")))
    floor = _scaled(metrics.get("roe_min3"))
    pe = _number(metrics.get("pe_ttm"))
    earnings_yield = 1 / pe if pe is not None and pe > 0 else None
    eps = _number(metrics.get("eps"))
    cash = _number(metrics.get("ocf_to_profit"))
    # Both negative cash flow and negative earnings can produce a positive ratio.
    if financial_like(metrics) or eps is None or eps <= 0:
        cash = None
    return {
        "vf_roe": roe,
        "vf_roe_floor": floor / roe if floor is not None and roe is not None and roe > 0 else None,
        "vf_growth": _first(metrics.get("rev_cagr3"), _scaled(metrics.get("rev_yoy"))),
        "vf_profit_growth": _first(metrics.get("profit_cagr3"), _scaled(metrics.get("profit_yoy"))),
        "vf_cash_conversion": cash,
        "vf_leverage": None if financial_like(metrics) else _scaled(metrics.get("debt_ratio")),
        "vf_liquidity": None if financial_like(metrics) else _number(metrics.get("current_ratio")),
        "vf_earnings_yield": earnings_yield,
        "vf_pb_percentile": _number(metrics.get("pb_pct")),
        "vf_quality_value": roe * earnings_yield
        if roe is not None and earnings_yield is not None
        else None,
        "vf_cash_value": cash * earnings_yield
        if cash is not None and earnings_yield is not None
        else None,
    }


def value_evidence(metrics: dict) -> dict:
    """Facts and review flags, not a new score, probability, or promotion decision."""
    features = value_features(metrics)
    observed = {
        "quality": features["vf_roe"] is not None,
        "growth": features["vf_growth"] is not None or features["vf_profit_growth"] is not None,
        "cashflow": features["vf_cash_conversion"] is not None,
        "balance": features["vf_leverage"] is not None or features["vf_liquidity"] is not None,
        "valuation": features["vf_earnings_yield"] is not None
        or features["vf_pb_percentile"] is not None,
    }
    warnings = []
    if financial_like(metrics):
        warnings.append("financial_sector_metrics_required")
    eps, pe = _number(metrics.get("eps")), _number(metrics.get("pe_ttm"))
    if (eps is not None and eps <= 0) or (pe is not None and pe <= 0):
        warnings.append("nonpositive_earnings")
    if features["vf_cash_conversion"] is not None and features["vf_cash_conversion"] < 0.75:
        warnings.append("weak_cash_conversion")
    if features["vf_profit_growth"] is not None and features["vf_profit_growth"] < 0:
        warnings.append("earnings_declining")
    if features["vf_leverage"] is not None and features["vf_leverage"] > 0.7:
        warnings.append("high_nonfinancial_leverage")
    missing = [name for name, available in observed.items() if not available]
    if missing:
        warnings.append("missing_dimensions")
    if features["vf_earnings_yield"] is not None and features["vf_earnings_yield"] >= 0.08:
        if not observed["quality"] or not observed["cashflow"] or warnings:
            warnings.append("cheapness_requires_quality_review")
    count = sum(observed.values())
    return {
        "status": "insufficient_data"
        if count < 3
        else ("needs_review" if warnings else "reviewable"),
        "observed_dimensions": count,
        "total_dimensions": 5,
        "missing_dimensions": missing,
        "missing_features": [name for name, value in features.items() if value is None],
        "warnings": warnings,
        "promotion_allowed": False,
    }
