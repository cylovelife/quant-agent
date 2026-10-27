"""Append-only cash/quantity events for explicitly created paper accounts.

@author cylovelife
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path

from execution import (
    AccountView,
    ExecutionPolicy,
    Instrument,
    OpenQuote,
    Order,
    PositionLot,
    _day,
    _identity,
    match_order,
    policy_from_definition,
    _money,
)
from store import _paper_path, create_paper_store, paper_connection
from corporate_actions import apply_to_state, slot

MAX_MINOR = 2**63 - 1
FEE_KINDS = ("commission", "stamp_duty", "transfer")
TERMINAL = {"filled", "rejected", "unfilled", "canceled"}


class LedgerError(RuntimeError):
    """Ledger operation cannot safely complete."""


class LedgerConflict(LedgerError):
    """Identity, ordering or immutable account intent conflicts."""


class LedgerCorruption(LedgerError):
    """Stored evidence cannot reconcile; never substitute an empty account."""


def _json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _minor(value: str) -> int:
    if not isinstance(value, str):
        raise ValueError("money must be supplied as a decimal string")
    try:
        amount = Decimal(value)
        with localcontext() as context:
            context.prec = 40
            if (
                not amount.is_finite()
                or amount.copy_abs() > Decimal(MAX_MINOR) / 100
                or amount != amount.quantize(Decimal("0.01"))
            ):
                raise ValueError(
                    "ledger amount must be exact cents within SQLite integer range"
                )
            return int(amount * 100)
    except InvalidOperation as exc:
        raise ValueError("invalid ledger decimal") from exc


def _major(value: int) -> str:
    absolute = abs(value)
    return ("-" if value < 0 else "") + f"{absolute // 100}.{absolute % 100:02d}"


def _key(account: str, order: str | None, kind: str) -> str:
    return _identity([account, order, kind])


def _identifier(value: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ValueError(
            "account/order identity must be a nonempty string <= 128 chars"
        )


def _quantity(lots: list[dict]) -> int:
    return sum(lot["quantity"] for lot in lots)


def _expected_events(result: dict) -> dict[str, tuple[int, int]]:
    """Reconcile every economic component independently, including zero fees."""
    if result["status"] != "filled":
        return {}
    sign = 1 if result["side"] == "buy" else -1
    fees = {name: _minor(result["fees"][name]) for name in FEE_KINDS}
    notional = _minor(result["notional"])
    quantity = result["filled_quantity"]
    if (
        notional <= 0
        or result["side"] not in {"buy", "sell"}
        or not isinstance(quantity, int)
        or isinstance(quantity, bool)
        or quantity <= 0
        or any(value < 0 for value in fees.values())
        or sum(fees.values()) != _minor(result["fees"]["total"])
        or -sign * notional - sum(fees.values()) != _minor(result["cash_delta"])
        or sign * quantity != result["quantity_delta"]
        or _minor(_money(Decimal(result["fill_price"]) * quantity)) != notional
    ):
        raise LedgerCorruption("fill arithmetic does not reconcile")
    return {
        "trade_cash": (-sign * notional, 0),
        **{name: (-value, 0) for name, value in fees.items()},
        "quantity": (0, sign * quantity),
    }


@dataclass
class _State:
    origin: dict
    policy: ExecutionPolicy
    cash: int
    lots: dict[tuple[str, str], list[dict]]
    orders: list[dict]
    events: list[dict]
    fees: dict[str, int]
    actions: list[dict] = field(default_factory=list)
    receivables: dict[str, dict] = field(default_factory=dict)
    entitlements: dict[str, dict] = field(default_factory=dict)
    net_flow: int = 0

    def view(self, order: Order) -> AccountView:
        lots = tuple(
            PositionLot(row["quantity"], row["acquired_date"])
            for row in self.lots.get((order.market, order.code), [])
        )
        return AccountView(
            order.execution_date, _major(self.cash), lots, order.market, order.code
        )


def _audit(conn: sqlite3.Connection, account_id: str) -> _State:
    origin = conn.execute(
        """SELECT account_id,currency,start_date,opening_minor,policy_id,policy_json,created_at
                             FROM paper_accounts WHERE account_id=?""",
        (account_id,),
    ).fetchone()
    if origin is None:
        raise LedgerError("paper account does not exist")
    origin = dict(origin)
    try:
        policy = policy_from_definition(json.loads(origin["policy_json"]))
        if policy.policy_id != origin["policy_id"]:
            raise LedgerCorruption("frozen policy fingerprint mismatch")
        orders = [
            dict(row)
            for row in conn.execute(
                """
            SELECT o.sequence,o.account_id,o.order_id,o.intent_hash,o.intent_json,o.execution_date,o.status,o.result_json,
                   (SELECT MAX(a.sequence) FROM paper_attempts a WHERE a.order_sequence=o.sequence) AS last_attempt
            FROM paper_orders o WHERE o.account_id=? ORDER BY last_attempt,o.sequence""",
                (account_id,),
            )
        ]
        events = [
            dict(row)
            for row in conn.execute(
                """
            SELECT sequence,account_id,order_sequence,event_key,kind,event_date,cash_minor,quantity_delta,market,code
            FROM paper_events WHERE account_id=? ORDER BY sequence""",
                (account_id,),
            )
        ]
        attempts = [
            dict(row)
            for row in conn.execute(
                """
            SELECT a.sequence,a.order_sequence,a.fingerprint,a.input_json,a.result_json
            FROM paper_attempts a JOIN paper_orders o ON a.order_sequence=o.sequence
            WHERE o.account_id=? ORDER BY a.sequence""",
                (account_id,),
            )
        ]
        actions = []
        if conn.execute("PRAGMA user_version").fetchone()[0] >= 2:
            actions = [
                dict(row)
                for row in conn.execute(
                    """SELECT sequence,account_id,action_id,after_attempt,action_date,phase,
                          intent_hash,intent_json,result_json,cash_minor,quantity_delta
                   FROM paper_actions WHERE account_id=? ORDER BY after_attempt,sequence""",
                    (account_id,),
                )
            ]
        latest = {}
        for attempt in attempts:
            if _identity(json.loads(attempt["input_json"])) != attempt["fingerprint"]:
                raise LedgerCorruption("attempt evidence fingerprint mismatch")
            latest[attempt["order_sequence"]] = attempt
        grouped = {}
        for event in events:
            grouped.setdefault(event["order_sequence"], []).append(event)
        initial = grouped.pop(None, [])
        if len(initial) != 1:
            raise LedgerCorruption("opening event missing or unexpected unowned events")
        opening = initial[0]
        if (
            opening["kind"] != "opening_cash"
            or opening["cash_minor"] != origin["opening_minor"]
            or opening["quantity_delta"] != 0
            or opening["event_date"] != origin["start_date"]
            or opening["event_key"] != _key(account_id, None, "opening_cash")
            or opening["market"]
            or opening["code"]
        ):
            raise LedgerCorruption("opening cash does not reconcile")
        state = _State(
            origin,
            policy,
            origin["opening_minor"],
            {},
            orders,
            events,
            {kind: 0 for kind in FEE_KINDS},
        )
        state.actions = actions
        action_index = 0

        def replay_action(row: dict) -> None:
            intent = json.loads(row["intent_json"])
            if (
                row["intent_hash"] != _identity(intent)
                or row["action_id"] != intent["action_id"]
                or row["action_date"] != intent["date"]
                or row["phase"] != intent["phase"]
            ):
                raise LedgerCorruption("corporate action identity mismatch")
            try:
                result = apply_to_state(state, intent)
            except (LedgerConflict, ValueError) as exc:
                raise LedgerCorruption("invalid stored corporate action") from exc
            result["account_id"] = account_id
            if (
                result != json.loads(row["result_json"])
                or _minor(result["cash_delta"]) != row["cash_minor"]
                or result["quantity_delta"] != row["quantity_delta"]
            ):
                raise LedgerCorruption("corporate action does not reconcile")

        for stored in orders:
            while (
                action_index < len(actions)
                and actions[action_index]["after_attempt"] < stored["last_attempt"]
            ):
                replay_action(actions[action_index])
                action_index += 1
            intent = json.loads(stored["intent_json"])
            result = json.loads(stored["result_json"])
            attempt = latest.get(stored["sequence"])
            if (
                _identity(intent) != stored["intent_hash"]
                or intent["policy_id"] != policy.policy_id
                or intent["order"]["order_id"] != stored["order_id"]
                or intent["order"]["execution_date"] != stored["execution_date"]
                or stored["status"] not in TERMINAL | {"unavailable"}
                or result["status"] != stored["status"]
                or result["policy_id"] != policy.policy_id
                or result["account_id"] != account_id
                or result["order"] != intent["order"]
                or result["instrument"] != intent["instrument"]
                or any(
                    result[key] != intent["order"][key]
                    for key in ("order_id", "market", "code", "side", "execution_date")
                )
                or (
                    result["status"] == "filled"
                    and result["filled_quantity"] != intent["order"]["quantity"]
                )
                or attempt is None
                or attempt["result_json"] != stored["result_json"]
            ):
                raise LedgerCorruption("order/attempt evidence mismatch")
            expected = _expected_events(result)
            rows = grouped.pop(stored["sequence"], [])
            if len(rows) != len(expected) or {row["kind"] for row in rows} != set(
                expected
            ):
                raise LedgerCorruption("order event components missing or duplicated")
            for event in rows:
                if (
                    (event["cash_minor"], event["quantity_delta"])
                    != expected[event["kind"]]
                    or event["market"] != result["market"]
                    or event["code"] != result["code"]
                    or event["event_date"] != stored["execution_date"]
                    or event["event_key"]
                    != _key(account_id, stored["order_id"], event["kind"])
                ):
                    raise LedgerCorruption("event does not match frozen fill")
            if not expected:
                if (
                    _minor(result["cash_delta"]) != 0
                    or result["quantity_delta"] != 0
                    or result["filled_quantity"] != 0
                    or _minor(result["fees"]["total"]) != 0
                    or any(_minor(result["fees"][kind]) != 0 for kind in FEE_KINDS)
                    or _minor(result["notional"]) != 0
                    or result["fill_price"] is not None
                    or result["cash_before"] != _major(state.cash)
                    or result["cash_after"] != _major(state.cash)
                    or result["quantity_before"]
                    != _quantity(state.lots.get((result["market"], result["code"]), []))
                    or result["quantity_after"] != result["quantity_before"]
                ):
                    raise LedgerCorruption("unfilled order has economic deltas")
                continue
            key = result["market"], result["code"]
            lots = state.lots.setdefault(key, [])
            before_quantity = _quantity(lots)
            if (
                result["cash_before"] != _major(state.cash)
                or result["quantity_before"] != before_quantity
            ):
                raise LedgerCorruption("fill starting balance does not reconcile")
            state.cash += _minor(result["cash_delta"])
            if state.cash < 0 or state.cash > MAX_MINOR:
                raise LedgerCorruption("cash outside ledger bounds")
            if result["side"] == "buy":
                lots.append(
                    {
                        "quantity": result["filled_quantity"],
                        "acquired_date": stored["execution_date"],
                        "source_order_id": stored["order_id"],
                        "fill_price": result["fill_price"],
                    }
                )
            else:
                remaining = result["filled_quantity"]
                for lot in lots:
                    if lot["acquired_date"] >= stored["execution_date"]:
                        continue
                    take = min(remaining, lot["quantity"])
                    lot["quantity"] -= take
                    remaining -= take
                if remaining:
                    raise LedgerCorruption("sell exceeds available older quantities")
                lots[:] = [lot for lot in lots if lot["quantity"]]
            if result["cash_after"] != _major(state.cash) or result[
                "quantity_after"
            ] != _quantity(lots):
                raise LedgerCorruption("fill ending balance does not reconcile")
            for kind in FEE_KINDS:
                state.fees[kind] += _minor(result["fees"][kind])
        while action_index < len(actions):
            replay_action(actions[action_index])
            action_index += 1
        if (
            grouped
            or sum(row["cash_minor"] for row in events)
            + sum(row["cash_minor"] for row in actions)
            != state.cash
        ):
            raise LedgerCorruption("unexpected events or cash sum mismatch")
        return state
    except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
        raise LedgerCorruption("malformed ledger evidence") from exc


def _snapshot(state: _State) -> dict:
    return {
        "account_id": state.origin["account_id"],
        "currency": "CNY",
        "start_date": state.origin["start_date"],
        "opening_cash": _major(state.origin["opening_minor"]),
        "cash": _major(state.cash),
        "net_external_flow": _major(state.net_flow),
        "dividend_receivable": _major(
            sum(item["amount"] for item in state.receivables.values())
        ),
        "corporate_action_coverage": "unverified",
        "total_return": None,
        "total_return_status": "corporate_actions_unavailable",
        "dividend_entitlements": [
            {
                "action_id": key,
                **{k: v for k, v in value.items() if k != "amount"},
                "amount": _major(value["amount"]),
            }
            for key, value in state.entitlements.items()
        ],
        "policy_id": state.policy.policy_id,
        "positions": [
            {"market": market, "code": code, "quantity": _quantity(lots), "lots": lots}
            for (market, code), lots in sorted(state.lots.items())
            if lots
        ],
        "fees": {
            **{kind: _major(value) for kind, value in state.fees.items()},
            "total": _major(sum(state.fees.values())),
        },
        "order_count": len(state.orders),
        "event_count": len(state.events) + len(state.actions),
        "unresolved_orders": [
            row["order_id"] for row in state.orders if row["status"] == "unavailable"
        ],
        "last_execution_date": max(
            (
                row["execution_date"]
                for row in state.orders
                if row["status"] in TERMINAL
            ),
            default=None,
        ),
        "reconciled": True,
    }


def _event_records(state: _State) -> list[dict]:
    order_ids = {row["sequence"]: row["order_id"] for row in state.orders}
    attempt_ids = {row["sequence"]: row["last_attempt"] for row in state.orders}
    records = [
        {
            "_sort": (attempt_ids.get(row["order_sequence"], -1), 0, row["sequence"]),
            "event_id": row["event_key"],
            "order_id": order_ids.get(row["order_sequence"]),
            "kind": row["kind"],
            "date": row["event_date"],
            "market": row["market"],
            "code": row["code"],
            "cash_delta": _major(row["cash_minor"]),
            "quantity_delta": row["quantity_delta"],
        }
        for row in state.events
    ]
    records.extend(
        {
            "_sort": (row["after_attempt"], 1, row["sequence"]),
            "event_id": _key(state.origin["account_id"], row["action_id"], "action"),
            "order_id": None,
            "action_id": row["action_id"],
            "kind": json.loads(row["intent_json"])["kind"],
            "date": row["action_date"],
            "market": json.loads(row["result_json"])["market"],
            "code": json.loads(row["result_json"])["code"],
            "cash_delta": _major(row["cash_minor"]),
            "quantity_delta": row["quantity_delta"],
            "receivable_delta": json.loads(row["result_json"])["receivable_delta"],
        }
        for row in state.actions
    )
    records.sort(key=lambda row: row["_sort"])
    return [
        {key: value for key, value in row.items() if key != "_sort"} for row in records
    ]


def _intent(order: Order, instrument: Instrument, policy_id: str) -> dict:
    return {
        "order": asdict(order),
        "instrument": asdict(instrument),
        "policy_id": policy_id,
    }


def _guard_daily_close(
    conn, account_id: str, day: str, *, allow_unsealed: bool
) -> None:
    present = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_daily'"
    ).fetchone()
    if present:
        last = conn.execute(
            "SELECT MAX(day) FROM paper_daily WHERE account_id=?", (account_id,)
        ).fetchone()[0]
        if last and (day <= last or not allow_unsealed):
            raise LedgerConflict(
                "daily-managed accounts require the daily settlement entry point"
            )


def _guard_experiment(conn, account_id: str, *, within_daily: bool) -> None:
    if not within_daily:
        from paper_experiment import experiment_for_account

        if experiment_for_account(conn, account_id) is not None:
            raise LedgerConflict("frozen experiment requires paired daily settlement")


def initialize_account(
    path: str | Path,
    account_id: str,
    cash: str,
    currency: str,
    start_date: str,
    policy: ExecutionPolicy,
) -> dict:
    """Publish a complete new ledger atomically, or register in an existing ledger."""
    target = _paper_path(path)
    _identifier(account_id)
    _day(start_date)
    if currency != "CNY" or _minor(cash) <= 0:
        raise ValueError("initial capital must be positive and currency must be CNY")
    if policy_from_definition(json.loads(policy.definition_json)) != policy:
        raise ValueError("policy object differs from its frozen definition")
    if target.exists():
        return PaperLedger(target).create_account(account_id, cash, start_date, policy)
    with tempfile.TemporaryDirectory(
        prefix=".paper-init-", dir=target.parent
    ) as staging:
        staged = PaperLedger.create(Path(staging) / "ledger.db")
        staged.create_account(account_id, cash, start_date, policy)
        try:
            os.link(staged.path, target)
        except FileExistsError:
            # Another initializer published first; verify its immutable origin.
            pass
    return PaperLedger(target).create_account(account_id, cash, start_date, policy)


class PaperLedger:
    """Explicit separate file, unique business keys, locked read/compute/write transaction."""

    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        with paper_connection(self.path):
            pass

    @classmethod
    def create(cls, path: str | Path) -> PaperLedger:
        """Create an empty store only. Funding is a separate explicit operation."""
        return cls(create_paper_store(path))

    def create_account(
        self,
        account_id: str,
        cash: str,
        start_date: str,
        policy: ExecutionPolicy,
        *,
        _conn=None,
    ) -> dict:
        """Register an immutable account origin and its opening cash exactly once."""
        _identifier(account_id)
        _day(start_date)
        opening = _minor(cash)
        if opening < 0:
            raise ValueError("negative opening capital")
        restored = policy_from_definition(json.loads(policy.definition_json))
        if restored != policy:
            raise ValueError("policy object differs from its frozen definition")
        with (
            nullcontext(_conn)
            if _conn is not None
            else paper_connection(self.path, write=True)
        ) as conn:
            cursor = conn.execute(
                """INSERT INTO paper_accounts
                (account_id,currency,start_date,opening_minor,policy_id,policy_json,created_at)
                VALUES (?,'CNY',?,?,?,?,?) ON CONFLICT(account_id) DO NOTHING""",
                (
                    account_id,
                    start_date,
                    opening,
                    policy.policy_id,
                    policy.definition_json,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            if cursor.rowcount:
                conn.execute(
                    """INSERT INTO paper_events
                    (account_id,event_key,kind,event_date,cash_minor)
                    VALUES (?,?,'opening_cash',?,?)""",
                    (
                        account_id,
                        _key(account_id, None, "opening_cash"),
                        start_date,
                        opening,
                    ),
                )
            state = _audit(conn, account_id)
            if (
                state.origin["start_date"],
                state.origin["opening_minor"],
                state.origin["policy_id"],
            ) != (start_date, opening, policy.policy_id):
                raise LedgerConflict(
                    "account origin is immutable; initialization cannot refund cash"
                )
            return _snapshot(state)

    def snapshot(self, account_id: str) -> dict:
        with paper_connection(self.path) as conn:
            return _snapshot(_audit(conn, account_id))

    def apply_action(self, account_id: str, action: dict, *, _conn=None) -> dict:
        """Record one explicitly sourced action or external flow, atomically and idempotently."""
        with (
            nullcontext(_conn)
            if _conn is not None
            else paper_connection(self.path, write=True)
        ) as conn:
            state = _audit(conn, account_id)
            _identifier(action.get("action_id"))
            prior = next(
                (
                    row
                    for row in state.actions
                    if row["action_id"] == action["action_id"]
                ),
                None,
            )
            if prior:
                if prior["intent_hash"] != _identity(action):
                    raise LedgerConflict(
                        "action ID reused with different immutable intent"
                    )
                return {**json.loads(prior["result_json"]), "replayed": True}
            _guard_experiment(conn, account_id, within_daily=_conn is not None)
            _guard_daily_close(
                conn, account_id, action["date"], allow_unsealed=_conn is not None
            )
            event_slot = slot(action["date"], action["phase"])
            if any(row["status"] == "unavailable" for row in state.orders):
                raise LedgerConflict(
                    "unresolved order must be resolved before applying an action"
                )
            previous = [
                slot(row["execution_date"], "open")
                for row in state.orders
                if row["status"] in TERMINAL
            ] + [slot(row["action_date"], row["phase"]) for row in state.actions]
            if previous and event_slot < max(previous):
                raise LedgerConflict("action violates ledger chronology")
            result = apply_to_state(state, action)
            result["account_id"] = account_id
            after_attempt = conn.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM paper_attempts"
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO paper_actions
                (account_id,action_id,after_attempt,action_date,phase,intent_hash,intent_json,result_json,cash_minor,quantity_delta)
                VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    account_id,
                    action["action_id"],
                    after_attempt,
                    action["date"],
                    action["phase"],
                    _identity(action),
                    _json(action),
                    _json(result),
                    _minor(result["cash_delta"]),
                    result["quantity_delta"],
                ),
            )
            _audit(conn, account_id)
            return result

    def events(self, account_id: str) -> list[dict]:
        with paper_connection(self.path) as conn:
            return _event_records(_audit(conn, account_id))

    def inspect(self, account_id: str) -> dict:
        """Balances and events from one consistent read transaction."""
        with paper_connection(self.path) as conn:
            state = _audit(conn, account_id)
            return {**_snapshot(state), "events": _event_records(state)}

    def terminal_result(
        self, account_id: str, order: Order, instrument: Instrument
    ) -> dict | None:
        """Retry an already settled intent without needing the original market file."""
        with paper_connection(self.path) as conn:
            state = _audit(conn, account_id)
            stored = next(
                (row for row in state.orders if row["order_id"] == order.order_id), None
            )
            if stored is None:
                return None
            if stored["intent_hash"] != _identity(
                _intent(order, instrument, state.policy.policy_id)
            ):
                raise LedgerConflict("order ID reused with different immutable intent")
            if stored["status"] in TERMINAL:
                return {**json.loads(stored["result_json"]), "replayed": True}
            return None

    def execute(
        self,
        account_id: str,
        order: Order,
        instrument: Instrument,
        quote: OpenQuote,
        *,
        _conn=None,
    ) -> dict:
        """Use locked ledger cash/lots, not caller balances or precomputed fill deltas."""
        _identifier(order.order_id)
        _day(order.signal_date)
        _day(order.execution_date)
        with (
            nullcontext(_conn)
            if _conn is not None
            else paper_connection(self.path, write=True)
        ) as conn:
            state = _audit(conn, account_id)
            intent = _intent(order, instrument, state.policy.policy_id)
            conn.execute(
                """INSERT INTO paper_orders
                (account_id,order_id,intent_hash,intent_json,execution_date,status)
                VALUES (?,?,?,?,?,'unavailable') ON CONFLICT(account_id,order_id) DO NOTHING""",
                (
                    account_id,
                    order.order_id,
                    _identity(intent),
                    _json(intent),
                    order.execution_date,
                ),
            )
            stored = dict(
                conn.execute(
                    """SELECT sequence,intent_hash,status,result_json FROM paper_orders
                                         WHERE account_id=? AND order_id=?""",
                    (account_id, order.order_id),
                ).fetchone()
            )
            if stored["intent_hash"] != _identity(intent):
                raise LedgerConflict("order ID reused with different immutable intent")
            if stored["status"] in TERMINAL:
                return {**json.loads(stored["result_json"]), "replayed": True}
            _guard_experiment(conn, account_id, within_daily=_conn is not None)
            _guard_daily_close(
                conn, account_id, order.execution_date, allow_unsealed=_conn is not None
            )
            if (
                order.signal_date < state.origin["start_date"]
                or order.execution_date < state.origin["start_date"]
            ):
                raise LedgerConflict("order predates account start")
            last = _snapshot(state)["last_execution_date"]
            if last and order.execution_date < last:
                raise LedgerConflict("new order violates ledger chronology")
            if state.actions and slot(order.execution_date, "open") < max(
                slot(row["action_date"], row["phase"]) for row in state.actions
            ):
                raise LedgerConflict("order precedes a recorded action")
            if any(
                row["status"] == "unavailable"
                and row["order_id"] != order.order_id
                and row["execution_date"] < order.execution_date
                for row in state.orders
            ):
                raise LedgerConflict(
                    "unresolved earlier order must be retried or canceled first"
                )
            view = state.view(order)
            evidence = {
                "quote": asdict(quote),
                "account": asdict(view),
                "policy_id": state.policy.policy_id,
            }
            fingerprint = _identity(evidence)
            prior = conn.execute(
                """SELECT result_json FROM paper_attempts WHERE order_sequence=? AND fingerprint=?""",
                (stored["sequence"], fingerprint),
            ).fetchone()
            if prior:
                return {**json.loads(prior["result_json"]), "replayed": True}
            with localcontext() as context:
                context.prec = 40
                result = match_order(order, instrument, quote, view, state.policy)
            count = conn.execute(
                "SELECT count(*) FROM paper_attempts WHERE order_sequence=?",
                (stored["sequence"],),
            ).fetchone()[0]
            result.update(
                account_id=account_id,
                cash_before=view.available_cash,
                cash_after=_major(state.cash + _minor(result["cash_delta"])),
                quantity_before=sum(lot.quantity for lot in view.lots),
                quantity_after=sum(lot.quantity for lot in view.lots)
                + result["quantity_delta"],
                attempt=count + 1,
                ledger_order_sequence=stored["sequence"],
                recorded=True,
                replayed=False,
            )
            self._persist(
                conn, account_id, stored["sequence"], order.order_id, evidence, result
            )
            _audit(conn, account_id)
            return result

    @staticmethod
    def _persist(
        conn: sqlite3.Connection,
        account_id: str,
        sequence: int,
        order_id: str,
        evidence: dict,
        result: dict,
    ) -> None:
        conn.execute(
            """INSERT INTO paper_attempts(order_sequence,fingerprint,input_json,result_json,created_at)
            VALUES (?,?,?,?,?)""",
            (
                sequence,
                _identity(evidence),
                _json(evidence),
                _json(result),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        for kind, (cash, quantity) in _expected_events(result).items():
            conn.execute(
                """INSERT INTO paper_events
                (account_id,order_sequence,event_key,kind,event_date,cash_minor,quantity_delta,market,code)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    account_id,
                    sequence,
                    _key(account_id, order_id, kind),
                    kind,
                    result["execution_date"],
                    cash,
                    quantity,
                    result["market"],
                    result["code"],
                ),
            )
        cursor = conn.execute(
            """UPDATE paper_orders SET status=?,result_json=?
                                 WHERE sequence=? AND status='unavailable' """,
            (result["status"], _json(result), sequence),
        )
        if cursor.rowcount != 1:
            raise LedgerConflict("order status changed before commit")

    def cancel_pending(self, account_id: str, order_id: str, reason: str) -> dict:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("explicit cancellation reason required")
        with paper_connection(self.path, write=True) as conn:
            state = _audit(conn, account_id)
            stored = next(
                (row for row in state.orders if row["order_id"] == order_id), None
            )
            if stored is None:
                raise LedgerError("order does not exist")
            if stored["status"] == "canceled":
                return {**json.loads(stored["result_json"]), "replayed": True}
            if stored["status"] != "unavailable":
                raise LedgerConflict("terminal order cannot be canceled or refunded")
            result = json.loads(stored["result_json"])
            quantity = _quantity(state.lots.get((result["market"], result["code"]), []))
            result.update(
                status="canceled",
                reason=reason,
                cash_before=_major(state.cash),
                cash_after=_major(state.cash),
                quantity_before=quantity,
                quantity_after=quantity,
                attempt=result["attempt"] + 1,
                replayed=False,
            )
            self._persist(
                conn,
                account_id,
                stored["sequence"],
                order_id,
                {"cancel_reason": reason, "previous_attempt": result["attempt"] - 1},
                result,
            )
            _audit(conn, account_id)
            return result


def stage_legacy_book(source: str | Path, target: str | Path) -> dict:
    """Copy-only rehearsal: missing quantities/cash must never become invented ledger assets."""
    source, target = Path(source).resolve(), Path(target).resolve()
    original = source.read_bytes()
    book = json.loads(original)
    if not isinstance(book, dict) or not isinstance(book.get("positions"), list):
        raise ValueError("legacy book must contain an explicit positions list")
    target.mkdir()  # Exclusive directory; an existing review is never overwritten.
    try:
        destination = target / "positions.json"
        shutil.copyfile(source, destination)
        if destination.read_bytes() != original or source.read_bytes() != original:
            raise LedgerConflict("legacy source changed during staging")
        report = {
            "source": str(source),
            "source_sha256": hashlib.sha256(original).hexdigest(),
            "position_count": len(book["positions"]),
            "migration_allowed": False,
            "reason": "legacy research prices do not prove quantities, cash or execution fees",
            "cash_created": "0.00",
            "quantity_created": 0,
        }
        (target / "review.json").write_text(_json(report), encoding="utf-8")
        return report
    except BaseException:
        shutil.rmtree(target)
        raise


def migrate_ledger_copy(source: str | Path, target: str | Path) -> PaperLedger:
    """Upgrade a v1 database into an exclusively created v2 copy; leave source untouched."""
    from contextlib import closing
    from store import PAPER_V2_DDL, _paper_path

    source, target = Path(source).resolve(), _paper_path(target)
    with paper_connection(source) as old:
        if old.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise ValueError("copy migration requires a version 1 ledger")
        accounts = [
            row[0] for row in old.execute("SELECT account_id FROM paper_accounts")
        ]
        for account_id in accounts:
            _audit(old, account_id)
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        try:
            with closing(sqlite3.connect(target, isolation_level=None)) as copied:
                copied.row_factory = sqlite3.Row
                old.backup(copied)
                copied.execute("PRAGMA foreign_keys=ON")
                copied.execute("PRAGMA synchronous=FULL")
                copied.execute("BEGIN IMMEDIATE")
                try:
                    for ddl in PAPER_V2_DDL:
                        copied.execute(ddl)
                    copied.execute("PRAGMA user_version=2")
                    for account_id in accounts:
                        _audit(copied, account_id)
                    copied.commit()
                except BaseException:
                    copied.rollback()
                    raise
            migrated = PaperLedger(target)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
    return migrated
