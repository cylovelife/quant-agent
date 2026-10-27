"""Explicit paper corporate actions and external cash flows, without source inference.

@author cylovelife
"""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext

PHASES = {"preopen": 0, "open": 1, "close": 2}


def slot(day: str, phase: str) -> tuple[str, int]:
    from execution import _day

    _day(day)
    if phase not in PHASES:
        raise ValueError("unknown event phase")
    return day, PHASES[phase]


def apply_to_state(state, action: dict) -> dict:
    """Calculate and apply a declared event to an in-memory audited state."""
    from paper_ledger import (
        LedgerConflict,
        MAX_MINOR,
        _identifier,
        _major,
        _minor,
        _quantity,
    )

    common = {"kind", "action_id", "date", "phase", "source", "known_at"}
    required = {
        "deposit": {"amount"},
        "withdrawal": {"amount"},
        "split": {"market", "code", "numerator", "denominator"},
        "dividend_entitlement": {"market", "code", "net_per_share"},
        "dividend_accrual": {"entitlement_id"},
        "dividend_payment": {"accrual_id"},
    }
    kind = action.get("kind")
    if kind not in required or set(action) != common | required[kind]:
        raise ValueError("unknown or missing action fields")
    _identifier(action["action_id"])
    when = slot(action["date"], action["phase"])
    if not isinstance(action["source"], str) or not action["source"].strip():
        raise LedgerConflict("action source unknown")
    try:
        known_day, known_phase = action["known_at"].split("@")
        if slot(known_day, known_phase) > when:
            raise LedgerConflict("action was not known at event time")
    except (AttributeError, ValueError) as exc:
        raise ValueError("invalid action known_at") from exc
    if action["date"] < state.origin["start_date"]:
        raise LedgerConflict("action predates account start")
    before_cash = state.cash
    before_recv = sum(x["amount"] for x in state.receivables.values())
    market, code = action.get("market", ""), action.get("code", "")
    if kind in {"split", "dividend_entitlement"}:
        if (
            market not in {"cn", "etf"}
            or not isinstance(code, str)
            or len(code) != 6
            or not code.isdigit()
        ):
            raise ValueError("action requires supported market and code")
    lots = state.lots.get((market, code), [])
    before_qty = _quantity(lots)
    delta_qty = delta_cash = delta_receivable = 0
    basis = None
    if kind in {"deposit", "withdrawal"}:
        value = _minor(action["amount"])
        if value <= 0:
            raise ValueError("external flow must be positive")
        delta_cash = value if kind == "deposit" else -value
        state.net_flow += delta_cash
    elif kind == "split":
        if action["phase"] != "preopen":
            raise LedgerConflict("split must be applied before opening")
        num, den = action["numerator"], action["denominator"]
        if any(
            isinstance(x, bool) or not isinstance(x, int) or not 1 <= x <= 1000000
            for x in (num, den)
        ):
            raise ValueError("split ratio must use positive bounded integers")
        revised = []
        for lot in lots:
            shares, remainder = divmod(lot["quantity"] * num, den)
            if remainder or shares <= 0 or shares > MAX_MINOR:
                raise LedgerConflict(
                    "fractional or invalid lot quantity requires explicit treatment"
                )
            revised.append(
                {
                    **lot,
                    "quantity": shares,
                    "fill_price_basis": "original_fill_not_adjusted_cost",
                    "split_actions": [
                        *lot.get("split_actions", []),
                        action["action_id"],
                    ],
                }
            )
        delta_qty = _quantity(revised) - before_qty
        if _quantity(revised) > MAX_MINOR or abs(delta_qty) > MAX_MINOR:
            raise LedgerConflict("split quantity exceeds ledger bounds")
        if revised:
            state.lots[(market, code)] = revised
    elif kind == "dividend_entitlement":
        if action["phase"] != "close":
            raise LedgerConflict("dividend entitlement uses record-day close")
        try:
            if not isinstance(action["net_per_share"], str):
                raise ValueError("net per share must be a decimal string")
            rate = Decimal(action["net_per_share"])
            if not rate.is_finite() or rate < 0 or rate > Decimal("1e12"):
                raise ValueError("invalid net dividend")
            with localcontext() as context:
                context.prec = 40
                amount = (rate * before_qty * 100).quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP
                )
                entitlement_amount = int(amount)
        except InvalidOperation as exc:
            raise ValueError("invalid net dividend") from exc
        if entitlement_amount > MAX_MINOR:
            raise ValueError("dividend exceeds integer range")
        basis = before_qty
        state.entitlements[action["action_id"]] = {
            "amount": entitlement_amount,
            "date": action["date"],
            "market": market,
            "code": code,
            "quantity": before_qty,
            "accrued": False,
            "paid": False,
        }
    elif kind == "dividend_accrual":
        if action["phase"] != "preopen":
            raise LedgerConflict("dividend accrual requires ex-date preopen")
        ref = action["entitlement_id"]
        _identifier(ref)
        entitlement = state.entitlements.get(ref)
        if entitlement is None or entitlement["accrued"]:
            raise LedgerConflict("dividend entitlement missing or already accrued")
        if action["date"] <= entitlement["date"]:
            raise LedgerConflict("ex-date must follow record date")
        entitlement["accrued"] = True
        market, code, basis = (
            entitlement["market"],
            entitlement["code"],
            entitlement["quantity"],
        )
        before_qty = _quantity(state.lots.get((market, code), []))
        delta_receivable = entitlement["amount"]
        state.receivables[action["action_id"]] = {
            **entitlement,
            "date": action["date"],
            "entitlement_id": ref,
        }
    else:
        ref = action["accrual_id"]
        _identifier(ref)
        if ref not in state.receivables:
            raise LedgerConflict("dividend entitlement missing or already paid")
        receivable = state.receivables.pop(ref)
        if action["date"] < receivable["date"]:
            raise LedgerConflict("payment precedes entitlement")
        market, code, basis = (
            receivable["market"],
            receivable["code"],
            receivable["quantity"],
        )
        before_qty = _quantity(state.lots.get((market, code), []))
        delta_cash = receivable["amount"]
        delta_receivable = -receivable["amount"]
        state.entitlements[receivable["entitlement_id"]]["paid"] = True
    state.cash += delta_cash
    if state.cash < 0 or state.cash > MAX_MINOR:
        raise LedgerConflict("external flow exceeds available cash or ledger bounds")
    after_recv = sum(x["amount"] for x in state.receivables.values())
    if after_recv > MAX_MINOR:
        raise LedgerConflict("total receivable exceeds ledger bounds")
    return {
        "action_id": action["action_id"],
        "kind": kind,
        "date": action["date"],
        "phase": action["phase"],
        "market": market,
        "code": code,
        "cash_before": _major(before_cash),
        "cash_after": _major(state.cash),
        "cash_delta": _major(delta_cash),
        "quantity_before": before_qty,
        "quantity_after": before_qty + delta_qty,
        "quantity_delta": delta_qty,
        "quantity_basis": basis,
        "receivable_before": _major(before_recv),
        "receivable_after": _major(after_recv),
        "receivable_delta": _major(delta_receivable),
        "recorded": True,
        "replayed": False,
    }
