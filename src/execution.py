"""Deterministic, side-effect-free CNY paper fills; separate from qfq return labels.

@author cylovelife
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
from pathlib import Path

EXECUTION_VERSION = "raw_next_open_ioc_v1"
CENT = Decimal("0.01")
ZERO_FEES = {
    "commission": "0.00",
    "stamp_duty": "0.00",
    "transfer": "0.00",
    "total": "0.00",
}


def _decimal(value: str | Decimal | int) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("boolean is not an amount")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("invalid decimal amount") from exc
    if not parsed.is_finite() or parsed < 0 or parsed > Decimal("1e18"):
        raise ValueError("amount must be finite, nonnegative and <= 1e18")
    return parsed


def _day(value: str) -> str:
    if not isinstance(value, str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError("expected canonical ISO date")
    return value


def _integer(value: int, minimum: int = 0) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _money(value: Decimal) -> str:
    return str(value.quantize(CENT, rounding=ROUND_HALF_UP))


def _identity(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()


@dataclass(frozen=True)
class Instrument:
    market: str
    code: str
    exchange: str
    profile: str


@dataclass(frozen=True)
class Order:
    order_id: str
    market: str
    code: str
    side: str
    quantity: int
    signal_date: str
    execution_date: str
    limit_price: str | None = None


@dataclass(frozen=True)
class PositionLot:
    quantity: int
    acquired_date: str


@dataclass(frozen=True)
class AccountView:
    date: str
    available_cash: str
    lots: tuple[PositionLot, ...]
    market: str
    code: str


@dataclass(frozen=True)
class SessionFacts:
    date: str
    source: str
    known_at: str
    trading_status: str
    instrument_status: str
    lower_limit: str | None
    upper_limit: str | None
    opening_capacity: int | None
    capacity_kind: str
    previous_session: str
    market: str
    code: str


@dataclass(frozen=True)
class OpenQuote:
    market: str
    code: str
    date: str
    open_price: str | None
    adjust: str
    source: str | None
    fetched_at: str | None
    data_version: str
    facts: SessionFacts
    issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class FeeProfile:
    market: str
    buy_lot: int
    tick: str
    max_quantity: int
    settlement_days: int
    commission_rate: str
    commission_min: str
    stamp_sell_rate: str
    transfer_rate: str
    slippage_bps: str


@dataclass(frozen=True)
class ExecutionPolicy:
    version: str
    effective_from: str
    policy_id: str
    profiles: tuple[tuple[str, FeeProfile], ...]
    definition_json: str

    def profile(self, key: str) -> FeeProfile | None:
        return dict(self.profiles).get(key)


def load_policy(path: str | Path) -> ExecutionPolicy:
    """Load explicit, fingerprinted assumptions; never infer an account tariff."""
    return policy_from_definition(json.loads(Path(path).read_text(encoding="utf-8")))


def policy_from_definition(value: dict) -> ExecutionPolicy:
    """Restore a frozen policy without consulting a mutable file or network."""
    if set(value) != {
        "version",
        "effective_from",
        "verified_on",
        "sources",
        "assumptions",
        "profiles",
    }:
        raise ValueError("unknown or missing execution policy fields")
    _day(value["effective_from"])
    _day(value["verified_on"])
    if not value["version"] or not value["sources"] or not value["assumptions"]:
        raise ValueError("policy identity and provenance are required")
    profiles = []
    for name, values in sorted(value["profiles"].items()):
        if name not in {"cn_mainboard", "cn_equity_etf"}:
            raise ValueError("unsupported execution profile")
        try:
            profile = FeeProfile(**values)
        except TypeError as exc:
            raise ValueError("unknown or missing profile fields") from exc
        expected_market = "cn" if name == "cn_mainboard" else "etf"
        expected_tick = "0.01" if name == "cn_mainboard" else "0.001"
        if (
            profile.market != expected_market
            or profile.buy_lot != 100
            or profile.tick != expected_tick
            or profile.settlement_days != 1
            or not _integer(profile.max_quantity, 100)
            or profile.max_quantity > 1000000
        ):
            raise ValueError("profile exceeds supported rule scope")
        for field in ("commission_rate", "stamp_sell_rate", "transfer_rate"):
            if _decimal(getattr(profile, field)) >= 1:
                raise ValueError("fee rate must be < 1")
        _decimal(profile.commission_min)
        if _decimal(profile.slippage_bps) >= 10000:
            raise ValueError("slippage must be < 10000 bps")
        profiles.append((name, profile))
    if not profiles:
        raise ValueError("empty execution profiles")
    return ExecutionPolicy(
        value["version"],
        value["effective_from"],
        _identity(value),
        tuple(profiles),
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ),
    )


def _instrument_matches(instrument: Instrument, profile: FeeProfile | None) -> bool:
    if (
        profile is None
        or instrument.market != profile.market
        or instrument.exchange not in {"SSE", "SZSE"}
    ):
        return False
    if (
        not isinstance(instrument.code, str)
        or len(instrument.code) != 6
        or not instrument.code.isdigit()
    ):
        return False
    if instrument.profile == "cn_mainboard":
        prefixes = (
            ("600", "601", "603", "605")
            if instrument.exchange == "SSE"
            else ("000", "001", "002", "003")
        )
        return instrument.code.startswith(prefixes)
    # The declaration must be a domestic equity ETF, not inferred from a code prefix.
    prefix = "5" if instrument.exchange == "SSE" else "15"
    return instrument.profile == "cn_equity_etf" and instrument.code.startswith(prefix)


def match_order(
    order: Order,
    instrument: Instrument,
    quote: OpenQuote,
    account: AccountView,
    policy: ExecutionPolicy,
) -> dict:
    """All-or-no-fill opening simulation. Returns deltas; never mutates/persists state.

    Opening capacity and limits must be supplied with their origin. No inference
    from daily high/low/close/volume and no claim of an actual exchange execution.
    """
    result = {
        "execution_version": EXECUTION_VERSION,
        "policy_version": policy.version,
        "policy_id": policy.policy_id,
        "order_id": order.order_id,
        "order": asdict(order),
        "instrument": asdict(instrument),
        "market": order.market,
        "code": order.code,
        "side": order.side,
        "execution_date": order.execution_date,
        "execution_phase": "open",
        "status": "unavailable",
        "reason": None,
        "filled_quantity": 0,
        "fill_price": None,
        "notional": "0.00",
        "fees": dict(ZERO_FEES),
        "cash_delta": "0.00",
        "quantity_delta": 0,
        "slippage_cost": "0.00",
        "data_version": quote.data_version,
        "price_adjust": quote.adjust,
        "price_source": quote.source,
        "raw_open": quote.open_price,
        "fetched_at": quote.fetched_at,
        "session_facts": asdict(quote.facts),
        "point_in_time_complete": False,
    }

    def fail(status: str, reason: str) -> dict:
        return {**result, "status": status, "reason": reason}

    try:
        _day(order.signal_date)
        _day(order.execution_date)
    except (ValueError, TypeError):
        return fail("rejected", "invalid_order_date")
    if (
        not order.order_id
        or order.side not in {"buy", "sell"}
        or order.signal_date >= order.execution_date
    ):
        return fail("rejected", "invalid_order")
    if order.execution_date < policy.effective_from:
        return fail("unavailable", "policy_not_effective")
    profile = policy.profile(instrument.profile)
    if not _instrument_matches(instrument, profile):
        return fail("unavailable", "instrument_profile_mismatch")
    if (order.market, order.code) != (instrument.market, instrument.code) or (
        quote.market,
        quote.code,
    ) != (order.market, order.code):
        return fail("unavailable", "instrument_identity_mismatch")
    if quote.date != order.execution_date or quote.facts.date != order.execution_date:
        return fail("unavailable", "execution_session_mismatch")
    if (quote.facts.market, quote.facts.code) != (order.market, order.code):
        return fail("unavailable", "session_instrument_mismatch")
    if quote.facts.previous_session != order.signal_date:
        return fail("unavailable", "next_session_unverified")
    if (
        quote.adjust != "raw"
        or quote.issues
        or not quote.source
        or not quote.data_version
    ):
        return fail("unavailable", "raw_price_or_provenance_unavailable")
    try:
        if not isinstance(quote.fetched_at, str) or len(quote.fetched_at) <= 10:
            raise ValueError("missing acquisition time")
        datetime.fromisoformat(quote.fetched_at)
        opening = _decimal(quote.open_price)
        tick = _decimal(profile.tick)
        if opening <= 0 or opening > Decimal("1e12") or opening % tick:
            raise ValueError("invalid opening price")
    except (ValueError, TypeError):
        return fail("unavailable", "invalid_opening_price_or_acquisition")
    if not _integer(order.quantity, 1) or order.quantity > profile.max_quantity:
        return fail("rejected", "invalid_quantity")
    try:
        _day(account.date)
        cash = _decimal(account.available_cash)
        if account.date != order.execution_date:
            raise ValueError("account date mismatch")
        for lot in account.lots:
            if not _integer(lot.quantity, 1) or _day(lot.acquired_date) > account.date:
                raise ValueError("invalid holding lot")
    except (ValueError, TypeError, AttributeError):
        return fail("unavailable", "invalid_account_snapshot")
    total = sum(lot.quantity for lot in account.lots)
    if (account.market, account.code) != (order.market, order.code):
        return fail("unavailable", "account_instrument_mismatch")
    sellable = sum(
        lot.quantity for lot in account.lots if lot.acquired_date < order.execution_date
    )
    if order.side == "buy" and order.quantity % profile.buy_lot:
        return fail("rejected", "invalid_buy_lot")
    if order.side == "sell":
        if order.quantity > sellable:
            return fail("rejected", "insufficient_sellable_quantity")
        if order.quantity % profile.buy_lot not in (0, total % profile.buy_lot):
            return fail("rejected", "invalid_residual_lot")
    facts = quote.facts
    if not facts.source:
        return fail("unavailable", "session_source_missing")
    if facts.known_at not in {facts.date + "@preopen", facts.date + "@open"}:
        return fail("unavailable", "future_session_facts")
    if facts.instrument_status != "regular":
        return fail("unavailable", "instrument_status_unsupported")
    if facts.trading_status == "suspended":
        return fail("unfilled", "suspended")
    if facts.trading_status != "trading":
        return fail("unavailable", "trading_status_unknown")
    try:
        lower, upper = _decimal(facts.lower_limit), _decimal(facts.upper_limit)
        if lower <= 0 or lower >= upper or lower % tick or upper % tick:
            raise ValueError("invalid limits")
    except (ValueError, TypeError):
        return fail("unavailable", "price_limits_unknown")
    if not lower <= opening <= upper:
        return fail("unavailable", "opening_outside_price_limits")
    if order.side == "buy" and opening == upper:
        return fail("unfilled", "limit_up_queue_unknown")
    if order.side == "sell" and opening == lower:
        return fail("unfilled", "limit_down_queue_unknown")
    if facts.capacity_kind not in {"observed", "assumed"} or not _integer(
        facts.opening_capacity
    ):
        return fail("unavailable", "opening_liquidity_unknown")
    if order.quantity > facts.opening_capacity:
        return fail("unfilled", "opening_capacity_insufficient")
    direction = Decimal(1) if order.side == "buy" else Decimal(-1)
    theoretical = opening * (1 + direction * _decimal(profile.slippage_bps) / 10000)
    rounding = ROUND_CEILING if order.side == "buy" else ROUND_FLOOR
    price = (theoretical / tick).to_integral_value(rounding=rounding) * tick
    if price <= 0 or not lower <= price <= upper:
        return fail("unfilled", "slippage_outside_price_limits")
    if order.limit_price is not None:
        try:
            limit = _decimal(order.limit_price)
            if limit <= 0 or limit % tick or not lower <= limit <= upper:
                raise ValueError("invalid limit")
        except (ValueError, TypeError):
            return fail("rejected", "invalid_limit_price")
        if (order.side == "buy" and price > limit) or (
            order.side == "sell" and price < limit
        ):
            return fail("unfilled", "limit_not_met")
    notional = Decimal(_money(price * order.quantity))
    commission = Decimal(
        _money(
            max(
                notional * _decimal(profile.commission_rate),
                _decimal(profile.commission_min),
            )
        )
    )
    stamp = (
        Decimal(_money(notional * _decimal(profile.stamp_sell_rate)))
        if order.side == "sell"
        else Decimal(0)
    )
    transfer = Decimal(_money(notional * _decimal(profile.transfer_rate)))
    fee = commission + stamp + transfer
    delta = -notional - fee if order.side == "buy" else notional - fee
    if cash + delta < 0:
        return fail("rejected", "insufficient_cash")
    result.update(
        status="filled",
        reason="simulated_open_fill",
        filled_quantity=order.quantity,
        fill_price=str(price),
        notional=_money(notional),
        fees={
            "commission": _money(commission),
            "stamp_duty": _money(stamp),
            "transfer": _money(transfer),
            "total": _money(fee),
        },
        cash_delta=_money(delta),
        quantity_delta=order.quantity if order.side == "buy" else -order.quantity,
        slippage_cost=_money(abs(price - opening) * order.quantity),
    )
    return result
