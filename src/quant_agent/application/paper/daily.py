"""Atomic daily paper settlement with explicit raw-price evidence.

@author cylovelife
"""

import json
from contextlib import nullcontext
from decimal import ROUND_HALF_UP, Decimal, localcontext

from quant_agent.application.paper.actions import slot
from quant_agent.application.paper.ledger import (
    LedgerConflict,
    LedgerCorruption,
    PaperLedger,
    _audit,
    _json,
    _major,
    _minor,
    _snapshot,
)
from quant_agent.domain.market.quality import _valid_acquisition
from quant_agent.domain.trading.execution import (
    Instrument,
    OpenQuote,
    Order,
    SessionFacts,
    _day,
    _identity,
)
from quant_agent.infrastructure.persistence.store import paper_connection

DDL = (
    """CREATE TABLE IF NOT EXISTS paper_daily (
       account_id TEXT NOT NULL REFERENCES paper_accounts(account_id), day TEXT NOT NULL,
       request_hash TEXT NOT NULL, request_json TEXT NOT NULL, result_hash TEXT NOT NULL,
       result_json TEXT NOT NULL, PRIMARY KEY(account_id,day))""",
    """CREATE TRIGGER IF NOT EXISTS paper_daily_no_update BEFORE UPDATE ON paper_daily
       BEGIN SELECT RAISE(ABORT,'daily records are append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS paper_daily_no_delete BEFORE DELETE ON paper_daily
       BEGIN SELECT RAISE(ABORT,'daily records are append-only'); END""",
)
CENT = Decimal("0.01")


def _costs(state):
    """Replay fill cash and FIFO lot costs; split retains each lot's total cost."""
    timeline = [(r["last_attempt"], 0, r["sequence"], "order", r) for r in state.orders]
    timeline += [(r["after_attempt"], 1, r["sequence"], "action", r) for r in state.actions]
    lots = {}
    realized = Decimal(0)
    for _, _, _, typ, row in sorted(timeline):
        if typ == "action":
            action = json.loads(row["intent_json"])
            if action["kind"] == "split":
                key = (action["market"], action["code"])
                for lot in lots.get(key, []):
                    lot["quantity"] = lot["quantity"] * action["numerator"] // action["denominator"]
            continue
        result = json.loads(row["result_json"])
        if result["status"] != "filled":
            continue
        key = (result["market"], result["code"])
        bucket = lots.setdefault(key, [])
        qty = result["filled_quantity"]
        cash = Decimal(_minor(result["cash_delta"]))
        if result["side"] == "buy":
            bucket.append(
                {
                    "quantity": qty,
                    "cost": -cash,
                    "acquired_date": result["execution_date"],
                }
            )
        else:
            remaining, basis = qty, Decimal(0)
            for lot in bucket:
                if lot["acquired_date"] >= result["execution_date"]:
                    continue
                take = min(remaining, lot["quantity"])
                part = (
                    lot["cost"] if take == lot["quantity"] else lot["cost"] * take / lot["quantity"]
                )
                lot["quantity"] -= take
                lot["cost"] -= part
                basis += part
                remaining -= take
                if not remaining:
                    break
            if remaining:
                raise LedgerCorruption("FIFO cost replay does not reconcile")
            bucket[:] = [lot for lot in bucket if lot["quantity"]]
            realized += cash - basis
    return lots, realized


def _amount(value):
    return _major(int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))


def _price(row, day):
    if set(row) != {
        "market",
        "code",
        "date",
        "adjust",
        "close",
        "source",
        "data_version",
    }:
        raise ValueError("close requires explicit raw price identity and source")
    if row["date"] != day or row["adjust"] != "raw" or not row["source"] or not row["data_version"]:
        raise ValueError("close must be same-day raw data with provenance")
    if not isinstance(row["close"], str):
        raise ValueError("close must be a decimal string")
    value = Decimal(row["close"])
    if not value.is_finite() or value <= 0 or value > Decimal("1000000000"):
        raise ValueError("invalid close price")
    return value


def _replay_intent(request: dict) -> str:
    """Compare economic inputs while keeping original acquisition receipts sealed.

    Local raw quote versions include the acquisition timestamp. A same-price
    refresh may therefore change both the timestamp and the opaque version;
    all explicit prices, sources, identities, facts and business events remain
    part of the comparison. This projection is used only for existing days.
    """
    value = json.loads(_json(request))
    for entry in value["events"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("quote"), dict):
            continue
        quote = entry["quote"]
        if _valid_acquisition(quote.get("fetched_at")):
            quote["fetched_at"] = None
        if isinstance(quote.get("data_version"), str) and quote["data_version"]:
            quote["data_version"] = None
    for close in value["closes"]:
        if isinstance(close.get("data_version"), str) and close["data_version"]:
            close["data_version"] = None
    return _identity(value)


def settle_day(
    book: PaperLedger, account_id: str, request: dict, *, _conn=None, _pair=False
) -> dict:
    """Apply one fully specified session, then atomically seal its valuation."""
    if set(request) != {"day", "previous_day", "events", "closes", "calendar"}:
        raise ValueError("daily request requires day, previous_day, events, closes and calendar")
    day = _day(request["day"])
    if request["previous_day"] is not None:
        _day(request["previous_day"])
    calendar = request["calendar"]
    if (
        not isinstance(calendar, dict)
        or set(calendar) != {"source", "sessions"}
        or not isinstance(calendar["source"], str)
        or not calendar["source"].strip()
        or not isinstance(calendar["sessions"], list)
    ):
        raise ValueError("calendar requires a source and ordered sessions")
    sessions = [_day(value) for value in calendar["sessions"]]
    if sessions != sorted(set(sessions)):
        raise ValueError("calendar sessions must be unique and ordered")
    if request["previous_day"] is not None and [
        d for d in sessions if request["previous_day"] < d <= day
    ] != [day]:
        raise LedgerConflict("missing or skipped declared calendar session")
    request = {
        **request,
        "calendar": {
            "source": calendar["source"],
            "sessions": [
                d
                for d in sessions
                if d <= day and (request["previous_day"] is None or d > request["previous_day"])
            ],
        },
    }
    if not isinstance(request["events"], list) or not isinstance(request["closes"], list):
        raise ValueError("events and closes must be lists")
    request_hash = _identity(request)
    prices = {}
    for close in request["closes"]:
        value = _price(close, day)
        key = close["market"], close["code"]
        if key in prices:
            raise ValueError("duplicate close price")
        prices[key] = value
    with localcontext() as context:
        context.prec = 40
        with (
            nullcontext(_conn) if _conn is not None else paper_connection(book.path, write=True)
        ) as conn:
            for ddl in DDL:
                conn.execute(ddl)
            state = _audit(conn, account_id)
            from quant_agent.application.paper.experiment import (
                check_daily_intent,
                experiment_for_account,
            )

            manifest = experiment_for_account(conn, account_id)
            prior = conn.execute(
                "SELECT request_hash,request_json,result_hash,result_json FROM paper_daily WHERE account_id=? AND day=?",
                (account_id, day),
            ).fetchone()
            if manifest:
                if not _pair:
                    raise LedgerConflict("frozen experiment requires paired daily settlement")
                check_daily_intent(manifest, account_id, request, replay=prior is not None)
            if prior:
                original = json.loads(prior["request_json"])
                if prior["request_hash"] != _identity(original):
                    raise LedgerCorruption("daily request evidence mismatch")
                result = json.loads(prior["result_json"])
                if prior["result_hash"] != _identity(result):
                    raise LedgerCorruption("daily result evidence mismatch")
                if prior["request_hash"] != request_hash and _replay_intent(
                    original
                ) != _replay_intent(request):
                    raise LedgerConflict("daily intent differs from closed day")
                return {**result, "replayed": True}
            last = conn.execute(
                "SELECT day,result_hash,result_json FROM paper_daily WHERE account_id=? ORDER BY day DESC LIMIT 1",
                (account_id,),
            ).fetchone()
            if last:
                previous = json.loads(last["result_json"])
                if last["result_hash"] != _identity(previous):
                    raise LedgerCorruption("previous daily result evidence mismatch")
                if request["previous_day"] != last["day"] or day <= last["day"]:
                    raise LedgerConflict("settle days in declared chronological order")
                units = Decimal(previous["units"])
                previous_nav = Decimal(previous["unit_value"])
            else:
                if request["previous_day"] is not None or day != state.origin["start_date"]:
                    raise LedgerConflict("first daily record must be account start date")
                units = Decimal(state.origin["opening_minor"]) / 100
                previous_nav = Decimal(1)
            last_day = last["day"] if last else None
            if any(
                last_day is None or row["execution_date"] > last_day for row in state.orders
            ) or any(last_day is None or row["action_date"] > last_day for row in state.actions):
                raise LedgerConflict(
                    "daily settlement cannot absorb events recorded after the previous close"
                )
            if any(row["status"] == "unavailable" for row in state.orders):
                raise LedgerConflict("unresolved earlier order")
            pre_flow = post_flow = Decimal(0)
            order_results = []
            event_results = []
            previous_slot = slot(day, "preopen")
            for item in request["events"]:
                if set(item) == {"action"}:
                    action = item["action"]
                    if action["date"] != day:
                        raise ValueError("action date must equal settlement day")
                    event_slot = slot(day, action["phase"])
                    if action["kind"] in {"deposit", "withdrawal"} and action["phase"] not in {
                        "preopen",
                        "close",
                    }:
                        raise ValueError("external flow must be preopen or close")
                    if event_slot < previous_slot:
                        raise LedgerConflict("daily events must be in time order")
                    result = book.apply_action(account_id, action, _conn=conn)
                    event_results.append(result)
                    if action["kind"] in {"deposit", "withdrawal"}:
                        if action["phase"] == "preopen":
                            pre_flow += Decimal(result["cash_delta"])
                        else:
                            post_flow += Decimal(result["cash_delta"])
                elif set(item) == {"order", "instrument", "quote"}:
                    order = Order(**item["order"])
                    instrument = Instrument(**item["instrument"])
                    quote_data = dict(item["quote"])
                    quote_data["facts"] = SessionFacts(**quote_data["facts"])
                    quote_data["issues"] = tuple(quote_data.get("issues", ()))
                    quote = OpenQuote(**quote_data)
                    event_slot = slot(day, "open")
                    if order.execution_date != day or event_slot < previous_slot:
                        raise LedgerConflict("order date or daily event order invalid")
                    result = book.execute(account_id, order, instrument, quote, _conn=conn)
                    order_results.append(result)
                    event_results.append(result)
                    if result["status"] == "unavailable":
                        raise LedgerConflict("unavailable order blocks day settlement")
                else:
                    raise ValueError("event must contain one action or a complete order")
                previous_slot = event_slot
            state = _audit(conn, account_id)
            snapshot = _snapshot(state)
            if manifest:
                from quant_agent.application.paper.experiment import check_daily_result

                check_daily_result(manifest, account_id, event_results, snapshot)
            costs, realized = _costs(state)
            needed = set(state.lots)
            needed = {key for key in needed if state.lots[key]}
            allowed_extra = {
                (r["market"], r["code"]) for r in order_results if r["status"] != "filled"
            }
            if not needed.issubset(prices) or set(prices) - needed - allowed_extra:
                raise LedgerConflict("each position requires exactly one same-day raw close")
            market_minor = Decimal(0)
            cost_minor = Decimal(0)
            positions = []
            for market, code in sorted(needed):
                key = market, code
                quantity = sum(lot["quantity"] for lot in state.lots[key])
                lot_costs = costs.get(key, [])
                if sum(lot["quantity"] for lot in lot_costs) != quantity:
                    raise LedgerCorruption("position and FIFO cost disagree")
                value = (prices[key] * quantity * 100).quantize(
                    Decimal("1"), rounding=ROUND_HALF_UP
                )
                basis = sum((lot["cost"] for lot in lot_costs), Decimal(0))
                market_minor += value
                cost_minor += basis
                positions.append(
                    {
                        "market": market,
                        "code": code,
                        "quantity": quantity,
                        "close": str(prices[key]),
                        "market_value": _amount(value),
                        "cost_basis": _amount(basis),
                        "unrealized_trading_pnl": _amount(value - basis),
                    }
                )
            receivable = sum(item["amount"] for item in state.receivables.values())
            assets = Decimal(state.cash + receivable) + market_minor
            units += pre_flow / previous_nav
            pre_close_assets = assets / 100 - post_flow
            if units <= 0 or pre_close_assets <= 0:
                raise LedgerConflict("unit valuation requires positive equity and units")
            unit_value = pre_close_assets / units
            units += post_flow / unit_value
            if units <= 0:
                raise LedgerConflict("external withdrawal exhausts units")
            result = {
                "account_id": account_id,
                "date": day,
                "previous_day": request["previous_day"],
                "cash": snapshot["cash"],
                "dividend_receivable": snapshot["dividend_receivable"],
                "positions": positions,
                "market_value": _amount(market_minor),
                "net_assets": _amount(assets),
                "net_external_flow": snapshot["net_external_flow"],
                "profit_after_external_flows": _amount(
                    assets - state.origin["opening_minor"] - state.net_flow
                ),
                "fees": snapshot["fees"],
                "realized_trading_pnl": _amount(realized),
                "unrealized_trading_pnl": _amount(market_minor - cost_minor),
                "units": str(units),
                "unit_value": str(unit_value),
                "corporate_action_coverage": "unverified",
                "calendar_source": calendar["source"],
                "calendar_coverage": "source_declared_unverified",
                "total_return": None,
                "total_return_status": "corporate_actions_unavailable",
                "replayed": False,
            }
            if manifest:
                result["runtime_id"] = manifest["runtime"]["runtime_id"]
            conn.execute(
                "INSERT INTO paper_daily(account_id,day,request_hash,request_json,result_hash,result_json) VALUES(?,?,?,?,?,?)",
                (
                    account_id,
                    day,
                    request_hash,
                    _json(request),
                    _identity(result),
                    _json(result),
                ),
            )
            return result


def daily_curve(book: PaperLedger, account_id: str) -> list[dict]:
    with paper_connection(book.path) as conn:
        _audit(conn, account_id)
        if not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_daily'"
        ).fetchone():
            return []
        result = []
        for row in conn.execute(
            "SELECT result_hash,result_json FROM paper_daily WHERE account_id=? ORDER BY day",
            (account_id,),
        ):
            item = json.loads(row["result_json"])
            if row["result_hash"] != _identity(item):
                raise LedgerCorruption("daily record evidence mismatch")
            result.append(item)
        return result
