"""Explicit paper preview, durable booking and read-only account inspection.

@author cylovelife
"""

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict
from pathlib import Path

from quant_agent.application.paper.ledger import (
    LedgerError,
    PaperLedger,
    initialize_account,
    migrate_ledger_copy,
)
from quant_agent.domain.trading.execution import (
    AccountView,
    Instrument,
    Order,
    PositionLot,
    SessionFacts,
    match_order,
)
from quant_agent.infrastructure.configuration import load_policy
from quant_agent.infrastructure.persistence.execution_data import (
    local_closing_quote,
    local_opening_quote,
)
from quant_agent.infrastructure.persistence.local import LocalData
from quant_agent.runtime.paths import WORKSPACE_ROOT

ROOT = WORKSPACE_ROOT


def run_runtime_transition(request_path: str) -> int:
    from quant_agent.application.paper.runtime import transition_copy

    try:
        path = Path(request_path).resolve()
        request = json.loads(path.read_text(encoding="utf-8"))
        if set(request) != {
            "source",
            "target",
            "experiment_id",
            "transition_id",
            "effective_from",
            "reason",
            "calendar",
        }:
            raise ValueError(
                "runtime transition requires source, new target, experiment, transition, effective date, reason and calendar"
            )
        for key in ("source", "target"):
            target = Path(request[key])
            request[key] = target if target.is_absolute() else path.parent / target
        result = transition_copy(**request)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        sqlite3.Error,
        LedgerError,
        ArithmeticError,
    ) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


def run_experiment_start(request_path: str) -> int:
    from quant_agent.application.paper.experiment import start_experiment

    try:
        path = Path(request_path).resolve()
        request = json.loads(path.read_text(encoding="utf-8"))
        if set(request) != {"ledger_path", "policy_path", "experiment"}:
            raise ValueError("start request requires ledger, policy and experiment")

        def relative(value):
            value = Path(value)
            return value if value.is_absolute() else path.parent / value

        result = start_experiment(
            relative(request["ledger_path"]),
            request["experiment"],
            load_policy(relative(request["policy_path"])),
        )
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        sqlite3.Error,
        LedgerError,
        ArithmeticError,
    ) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


def run_experiment_advance(request_path: str) -> int:
    from quant_agent.application.paper.experiment import settle_pair
    from quant_agent.infrastructure.persistence.store import paper_connection

    try:
        path = Path(request_path).resolve()
        request = json.loads(path.read_text(encoding="utf-8"))
        if set(request) != {
            "ledger_path",
            "db_path",
            "experiment_id",
            "calendar",
            "days",
        } or not isinstance(request["days"], list):
            raise ValueError(
                "advance request requires ledger, market database, experiment, calendar and days"
            )

        def relative(value):
            value = Path(value)
            return value if value.is_absolute() else path.parent / value

        book = PaperLedger(relative(request["ledger_path"]))
        reader = LocalData(relative(request["db_path"]))
        with paper_connection(book.path) as conn:
            row = conn.execute(
                "SELECT manifest_json FROM paper_experiments WHERE experiment_id=?",
                (request["experiment_id"],),
            ).fetchone()
            if row is None:
                raise ValueError("unknown experiment")
            spec = json.loads(row["manifest_json"])["spec"]
        strategy = spec["strategy"]
        instrument = Instrument(
            **{k: strategy[k] for k in ("market", "code", "exchange", "profile")}
        )
        results = []
        for entry in request["days"]:
            if set(entry) != {
                "day",
                "previous_day",
                "actions",
                "session",
                "close_symbols",
            }:
                raise ValueError(
                    "day needs date, previous day, actions, optional session and close symbols"
                )
            quote = None
            if entry["session"] is not None:
                facts = SessionFacts(**entry["session"])
                quote = asdict(local_opening_quote(reader, instrument, entry["day"], facts))
            closes = []
            for symbol in entry["close_symbols"]:
                if set(symbol) != {"market", "code"}:
                    raise ValueError("close symbol requires market and code")
                closes.append(
                    local_closing_quote(reader, symbol["market"], symbol["code"], entry["day"])
                )
            results.append(
                settle_pair(
                    book,
                    request["experiment_id"],
                    entry["day"],
                    entry["previous_day"],
                    request["calendar"],
                    entry["actions"],
                    closes,
                    quote,
                )
            )
        print(json.dumps({"days": results}, ensure_ascii=False, allow_nan=False))
        return 0
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        sqlite3.Error,
        LedgerError,
        ArithmeticError,
    ) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


def run_experiment_compare(request_path: str) -> int:
    from quant_agent.application.paper.experiment import compare

    try:
        path = Path(request_path).resolve()
        request = json.loads(path.read_text(encoding="utf-8"))
        if set(request) != {"ledger_path", "experiment_id"}:
            raise ValueError("comparison requires ledger and experiment id")
        target = Path(request["ledger_path"])
        if not target.is_absolute():
            target = path.parent / target
        print(
            json.dumps(
                compare(PaperLedger(target), request["experiment_id"]),
                ensure_ascii=False,
                allow_nan=False,
            )
        )
        return 0
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        sqlite3.Error,
        LedgerError,
        ArithmeticError,
    ) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


def run_daily(request_path: str) -> int:
    """Process explicitly ordered days from a local raw market store."""
    from quant_agent.application.paper.daily import settle_day

    try:
        path = Path(request_path).resolve()
        request = json.loads(path.read_text(encoding="utf-8"))
        if set(request) != {
            "ledger_path",
            "db_path",
            "account_id",
            "days",
            "calendar",
        } or not isinstance(request["days"], list):
            raise ValueError(
                "daily request requires ledger, market database, account, calendar and days"
            )

        def relative(value):
            target = Path(value)
            return target if target.is_absolute() else path.parent / target

        book = PaperLedger(relative(request["ledger_path"]))
        reader = LocalData(relative(request["db_path"]))
        results = []
        for entry in request["days"]:
            if set(entry) != {"day", "previous_day", "events", "close_symbols"}:
                raise ValueError("daily entry requires day, previous_day, events and close_symbols")
            events = []
            for event in entry["events"]:
                if set(event) == {"action"}:
                    events.append(event)
                elif set(event) == {"order", "instrument", "session"}:
                    order = Order(**event["order"])
                    instrument = Instrument(**event["instrument"])
                    facts = SessionFacts(**event["session"])
                    quote = local_opening_quote(reader, instrument, order.execution_date, facts)
                    events.append(
                        {
                            "order": event["order"],
                            "instrument": event["instrument"],
                            "quote": asdict(quote),
                        }
                    )
                else:
                    raise ValueError("unknown daily event")
            closes = []
            for symbol in entry["close_symbols"]:
                if set(symbol) != {"market", "code"}:
                    raise ValueError("close symbol requires market and code")
                closes.append(
                    local_closing_quote(reader, symbol["market"], symbol["code"], entry["day"])
                )
            results.append(
                settle_day(
                    book,
                    request["account_id"],
                    {
                        "day": entry["day"],
                        "previous_day": entry["previous_day"],
                        "events": events,
                        "closes": closes,
                        "calendar": request["calendar"],
                    },
                )
            )
        print(json.dumps({"days": results}, ensure_ascii=False, allow_nan=False))
        return 0
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        sqlite3.Error,
        LedgerError,
        ArithmeticError,
    ) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


def run_daily_curve(request_path: str) -> int:
    from quant_agent.application.paper.daily import daily_curve

    try:
        path = Path(request_path).resolve()
        request = json.loads(path.read_text(encoding="utf-8"))
        if set(request) != {"ledger_path", "account_id"}:
            raise ValueError("curve request requires ledger and account")
        target = Path(request["ledger_path"])
        if not target.is_absolute():
            target = path.parent / target
        print(
            json.dumps(
                {"days": daily_curve(PaperLedger(target), request["account_id"])},
                ensure_ascii=False,
                allow_nan=False,
            )
        )
        return 0
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        sqlite3.Error,
        LedgerError,
    ) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


def run_initialize(request_path: str) -> int:
    try:
        path = Path(request_path).resolve()
        request = json.loads(path.read_text(encoding="utf-8"))
        if set(request) != {
            "ledger_path",
            "account_id",
            "opening_cash",
            "currency",
            "start_date",
            "policy_path",
        }:
            raise ValueError(
                "initialization requires ledger, identity, capital, currency, start date and policy"
            )

        def relative(value: str) -> Path:
            target = Path(value)
            return target if target.is_absolute() else path.parent / target

        policy = load_policy(relative(request["policy_path"]))
        result = initialize_account(
            relative(request["ledger_path"]),
            request["account_id"],
            request["opening_cash"],
            request["currency"],
            request["start_date"],
            policy,
        )
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        sqlite3.Error,
        LedgerError,
    ) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


def run_preview(request_path: str) -> int:
    """Print a serializable simulation result; input errors have process status 2."""
    try:
        path = Path(request_path).resolve()
        request = json.loads(path.read_text(encoding="utf-8"))
        required = {"db_path", "order", "instrument", "session", "account"}
        if not required.issubset(request) or set(request) - required - {"policy_path"}:
            raise ValueError("unknown or missing order request fields")
        db = Path(request["db_path"])
        if not db.is_absolute():
            db = path.parent / db
        policy_path = Path(request.get("policy_path", ROOT / "params/execution_v1.json"))
        if not policy_path.is_absolute():
            policy_path = path.parent / policy_path
        policy = load_policy(policy_path)
        instrument = Instrument(**request["instrument"])
        order = Order(**request["order"])
        facts = SessionFacts(**request["session"])
        account_input = dict(request["account"])
        account_input["lots"] = tuple(PositionLot(**lot) for lot in account_input["lots"])
        account = AccountView(**account_input)
        quote = local_opening_quote(LocalData(db), instrument, order.execution_date, facts)
        result = match_order(order, instrument, quote, account, policy)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


def run_record(request_path: str) -> int:
    """Book an explicit simulated order using existing ledger cash and frozen policy."""
    try:
        path = Path(request_path).resolve()
        request = json.loads(path.read_text(encoding="utf-8"))
        if set(request) != {
            "ledger_path",
            "account_id",
            "db_path",
            "order",
            "instrument",
            "session",
        }:
            raise ValueError(
                "recording requires explicit ledger; caller cash/policy overrides are forbidden"
            )

        def relative(value: str) -> Path:
            target = Path(value)
            return target if target.is_absolute() else path.parent / target

        ledger = PaperLedger(relative(request["ledger_path"]))
        order = Order(**request["order"])
        instrument = Instrument(**request["instrument"])
        facts = SessionFacts(**request["session"])
        result = ledger.terminal_result(request["account_id"], order, instrument)
        if result is None:
            quote = local_opening_quote(
                LocalData(relative(request["db_path"])),
                instrument,
                order.execution_date,
                facts,
            )
            result = ledger.execute(request["account_id"], order, instrument, quote)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        sqlite3.Error,
        LedgerError,
    ) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


def run_status(request_path: str) -> int:
    try:
        path = Path(request_path).resolve()
        request = json.loads(path.read_text(encoding="utf-8"))
        if set(request) != {"ledger_path", "account_id"}:
            raise ValueError("ledger status requires ledger_path and account_id")
        target = Path(request["ledger_path"])
        if not target.is_absolute():
            target = path.parent / target
        result = PaperLedger(target).inspect(request["account_id"])
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        sqlite3.Error,
        LedgerError,
    ) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


def run_action(request_path: str) -> int:
    try:
        path = Path(request_path).resolve()
        request = json.loads(path.read_text(encoding="utf-8"))
        if set(request) != {"ledger_path", "account_id", "action"}:
            raise ValueError("action requires ledger_path, account_id and action")
        target = Path(request["ledger_path"])
        if not target.is_absolute():
            target = path.parent / target
        result = PaperLedger(target).apply_action(request["account_id"], request["action"])
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        sqlite3.Error,
        LedgerError,
    ) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


def run_migrate_copy(request_path: str) -> int:
    try:
        path = Path(request_path).resolve()
        request = json.loads(path.read_text(encoding="utf-8"))
        if set(request) != {"source", "target"}:
            raise ValueError("copy migration requires source and target")

        def resolve(value: str) -> Path:
            p = Path(value)
            return p if p.is_absolute() else path.parent / p

        result = migrate_ledger_copy(resolve(request["source"]), resolve(request["target"]))
        print(json.dumps({"path": str(result.path), "version": 2}, ensure_ascii=False))
        return 0
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        sqlite3.Error,
        LedgerError,
    ) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="单笔虚拟订单预演／显式记账")
    commands = parser.add_mutually_exclusive_group(required=True)
    commands.add_argument("--start-paper-experiment", metavar="REQUEST_JSON")
    commands.add_argument("--transition-paper-experiment", metavar="REQUEST_JSON")
    commands.add_argument("--advance-paper-experiment", metavar="REQUEST_JSON")
    commands.add_argument("--compare-paper-experiment", metavar="REQUEST_JSON")
    commands.add_argument("--settle-paper-days", metavar="REQUEST_JSON")
    commands.add_argument("--paper-curve", metavar="REQUEST_JSON")
    commands.add_argument("--init-account", metavar="REQUEST_JSON")
    commands.add_argument("--simulate-order", metavar="REQUEST_JSON")
    commands.add_argument("--record-order", metavar="REQUEST_JSON")
    commands.add_argument("--ledger-status", metavar="REQUEST_JSON")
    commands.add_argument("--record-action", metavar="REQUEST_JSON")
    commands.add_argument("--migrate-ledger-copy", metavar="REQUEST_JSON")
    args = parser.parse_args(argv)
    if args.transition_paper_experiment:
        return run_runtime_transition(args.transition_paper_experiment)
    if args.start_paper_experiment:
        return run_experiment_start(args.start_paper_experiment)
    if args.advance_paper_experiment:
        return run_experiment_advance(args.advance_paper_experiment)
    if args.compare_paper_experiment:
        return run_experiment_compare(args.compare_paper_experiment)
    if args.settle_paper_days:
        return run_daily(args.settle_paper_days)
    if args.paper_curve:
        return run_daily_curve(args.paper_curve)
    if args.init_account:
        return run_initialize(args.init_account)
    if args.record_action:
        return run_action(args.record_action)
    if args.migrate_ledger_copy:
        return run_migrate_copy(args.migrate_ledger_copy)
    if args.ledger_status:
        return run_status(args.ledger_status)
    return (
        run_preview(args.simulate_order) if args.simulate_order else run_record(args.record_order)
    )
