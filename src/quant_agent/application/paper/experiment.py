"""Frozen, prospective paper experiment and comparable cash benchmark.

@author cylovelife
"""

import json
import os
import sys
import tempfile
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from quant_agent.application.paper.ledger import (
    LedgerConflict,
    LedgerCorruption,
    PaperLedger,
    _audit,
    _identifier,
    _json,
    _minor,
    _snapshot,
)
from quant_agent.domain.trading.execution import _day, _identity
from quant_agent.infrastructure.persistence.store import _paper_path, paper_connection
from quant_agent.runtime.code_identity import code_fingerprints as code_fingerprints
from quant_agent.runtime.paths import WORKSPACE_ROOT

ROOT = WORKSPACE_ROOT
DDL = (
    """CREATE TABLE IF NOT EXISTS paper_experiments (
       experiment_id TEXT PRIMARY KEY, account_id TEXT NOT NULL UNIQUE REFERENCES paper_accounts(account_id),
       benchmark_id TEXT NOT NULL UNIQUE REFERENCES paper_accounts(account_id),
       intent_hash TEXT NOT NULL, manifest_json TEXT NOT NULL, manifest_hash TEXT NOT NULL)""",
    """CREATE TRIGGER IF NOT EXISTS paper_experiments_no_update BEFORE UPDATE ON paper_experiments
       BEGIN SELECT RAISE(ABORT,'experiment is immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS paper_experiments_no_delete BEFORE DELETE ON paper_experiments
       BEGIN SELECT RAISE(ABORT,'experiment is immutable'); END""",
)


def _validate(spec, now):
    keys = {
        "experiment_id",
        "account_id",
        "benchmark_id",
        "opening_cash",
        "currency",
        "start_date",
        "first_trade_date",
        "strategy",
        "risk",
        "data_sources",
        "benchmark",
    }
    if not isinstance(spec, dict) or set(spec) != keys:
        raise ValueError("experiment fields are missing or unknown")
    for key in ("experiment_id", "account_id", "benchmark_id"):
        _identifier(spec[key])
    if len(spec["experiment_id"]) > 118:
        raise ValueError("experiment id too long for generated order id")
    if spec["account_id"] == spec["benchmark_id"]:
        raise ValueError("strategy and benchmark require separate accounts")
    if spec["currency"] != "CNY" or _minor(spec["opening_cash"]) <= 0:
        raise ValueError("positive CNY opening capital required")
    start = _day(spec["start_date"])
    first = _day(spec["first_trade_date"])
    if start < now.date().isoformat() or first <= start:
        raise ValueError(
            "prospective start cannot predate registration; first trade follows start date"
        )
    if start == now.date().isoformat() and now.hour >= 15:
        raise ValueError("same-day startup must be registered before market close")
    strategy = spec["strategy"]
    if not isinstance(strategy, dict) or set(strategy) != {
        "kind",
        "market",
        "code",
        "exchange",
        "profile",
        "quantity",
    }:
        raise ValueError("strategy requires an explicit fixed instrument and quantity")
    if (
        strategy["kind"] != "buy_and_hold"
        or strategy["market"] != "etf"
        or strategy["profile"] != "cn_equity_etf"
    ):
        raise ValueError("only fixed-quantity ETF buy-and-hold is supported")
    if (
        strategy["exchange"] not in {"SSE", "SZSE"}
        or not isinstance(strategy["code"], str)
        or len(strategy["code"]) != 6
        or not strategy["code"].isdigit()
    ):
        raise ValueError("invalid ETF identity")
    qty = strategy["quantity"]
    if isinstance(qty, bool) or not isinstance(qty, int) or qty < 100 or qty % 100:
        raise ValueError("quantity must be a positive 100-share lot multiple")
    risk = spec["risk"]
    if not isinstance(risk, dict) or set(risk) != {
        "max_order_cash",
        "min_cash_reserve",
    }:
        raise ValueError("risk requires order spending and cash reserve limits")
    if _minor(risk["max_order_cash"]) <= 0 or _minor(risk["min_cash_reserve"]) < 0:
        raise ValueError("invalid risk limits")
    if _minor(risk["max_order_cash"]) + _minor(risk["min_cash_reserve"]) > _minor(
        spec["opening_cash"]
    ):
        raise ValueError("order cap and reserve exceed starting capital")
    sources = spec["data_sources"]
    if (
        not isinstance(sources, dict)
        or set(sources) != {"market", "calendar", "corporate_actions"}
        or any(not isinstance(x, str) or not x.strip() for x in sources.values())
    ):
        raise ValueError("data sources must be explicitly named")
    if spec["benchmark"] != {"kind": "cash", "cashflow_policy": "mirror"}:
        raise ValueError("benchmark must be same-capital cash with mirrored flows")


def register(book, spec, policy, *, now=None):
    now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    if now.tzinfo is None:
        raise ValueError("registration time must include timezone")
    now = now.astimezone(ZoneInfo("Asia/Shanghai"))
    intent_hash = _identity({"spec": spec, "policy_id": policy.policy_id})
    with paper_connection(book.path, write=True) as conn:
        for ddl in DDL:
            conn.execute(ddl)
        prior = conn.execute(
            "SELECT intent_hash,manifest_hash,manifest_json FROM paper_experiments WHERE experiment_id=?",
            (spec["experiment_id"],),
        ).fetchone()
        if prior:
            manifest = json.loads(prior["manifest_json"])
            if _identity(manifest) != prior["manifest_hash"]:
                raise LedgerCorruption("experiment manifest corrupted")
            if prior["intent_hash"] != intent_hash:
                raise LedgerConflict("experiment id conflicts with frozen intent")
            return {**manifest, "replayed": True}
        _validate(spec, now)
        ids = (spec["account_id"], spec["benchmark_id"])
        if conn.execute(
            "SELECT 1 FROM paper_accounts WHERE account_id IN (?,?) LIMIT 1", ids
        ).fetchone():
            raise LedgerConflict("experiment accounts must both be new")
        if policy.effective_from > spec["start_date"] or policy.profile("cn_equity_etf") is None:
            raise ValueError("policy does not cover the startup and ETF profile")
        for account_id in ids:
            book.create_account(
                account_id, spec["opening_cash"], spec["start_date"], policy, _conn=conn
            )
        manifest = {
            "spec": spec,
            "execution_policy_id": policy.policy_id,
            "execution_policy": json.loads(policy.definition_json),
            "code_sha256": code_fingerprints(),
            "registered_at": now.isoformat(),
            "python_version": sys.version,
            "status": "registered_not_yet_evaluated",
        }
        conn.execute(
            "INSERT INTO paper_experiments VALUES (?,?,?,?,?,?)",
            (
                spec["experiment_id"],
                *ids,
                intent_hash,
                _json(manifest),
                _identity(manifest),
            ),
        )
        return {**manifest, "replayed": False}


def start_experiment(path, spec, policy, *, now=None):
    target = _paper_path(path)
    # Validate before creating a candidate or touching an existing ledger.
    effective_now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    if effective_now.tzinfo is None:
        raise ValueError("registration time must include timezone")
    effective_now = effective_now.astimezone(ZoneInfo("Asia/Shanghai"))
    if target.exists():
        return register(PaperLedger(target), spec, policy, now=effective_now)
    _validate(spec, effective_now)
    with tempfile.TemporaryDirectory(prefix=".paper-experiment-", dir=target.parent) as staging:
        candidate = PaperLedger.create(Path(staging) / "ledger.db")
        created = register(candidate, spec, policy, now=effective_now)
        try:
            os.link(candidate.path, target)
            return created
        except FileExistsError:
            pass
    return register(PaperLedger(target), spec, policy, now=effective_now)


def experiment_for_account(conn, account_id):
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_experiments'"
    ).fetchone():
        return None
    row = conn.execute(
        "SELECT manifest_json,manifest_hash FROM paper_experiments WHERE account_id=? OR benchmark_id=?",
        (account_id, account_id),
    ).fetchone()
    if row is None:
        return None
    manifest = json.loads(row["manifest_json"])
    if row["manifest_hash"] != _identity(manifest):
        raise LedgerCorruption("experiment manifest corrupted")
    from quant_agent.application.paper.runtime import resolve_runtime

    return resolve_runtime(conn, manifest)


def verify_runtime(manifest, day=None, *, replay=False):
    runtime = manifest.get("runtime") or manifest
    if code_fingerprints() != runtime["code_sha256"]:
        raise LedgerConflict("frozen experiment code differs; settlement paused")
    if runtime["python_version"] != sys.version:
        raise LedgerConflict("frozen experiment Python differs; settlement paused")
    if (
        day is not None
        and not replay
        and day < runtime.get("effective_from", manifest["spec"]["start_date"])
    ):
        raise LedgerConflict("unsettled day precedes active runtime; settlement paused")


def check_daily_intent(manifest, account_id, request, *, replay=False):
    verify_runtime(manifest, request["day"], replay=replay)
    spec = manifest["spec"]
    if request["calendar"]["source"] != spec["data_sources"]["calendar"]:
        raise LedgerConflict("calendar source differs from frozen experiment")
    for price in request["closes"]:
        if price["source"] != spec["data_sources"]["market"]:
            raise LedgerConflict("closing price source differs from frozen experiment")
    strategy = spec["strategy"]
    if account_id == spec["account_id"]:
        first = spec["first_trade_date"]
        previous = request["previous_day"]
        if previous is not None and previous < first < request["day"]:
            raise LedgerConflict("first scheduled trade date cannot be skipped")
        orders = [item for item in request["events"] if "order" in item]
        if request["day"] == first and len(orders) != 1:
            raise LedgerConflict("first scheduled trade requires the frozen order")
    for entry in request["events"]:
        if "action" in entry:
            action = entry["action"]
            if action["kind"] in {"deposit", "withdrawal"}:
                continue
            if (
                account_id == spec["benchmark_id"]
                or action["source"] != spec["data_sources"]["corporate_actions"]
            ):
                raise LedgerConflict("action conflicts with frozen source or cash benchmark")
            if action["kind"] in {"split", "dividend_entitlement"} and (
                action["market"],
                action["code"],
            ) != (strategy["market"], strategy["code"]):
                raise LedgerConflict("corporate action is outside the frozen instrument")
        else:
            order, instrument, quote = (
                entry["order"],
                entry["instrument"],
                entry["quote"],
            )
            expected_id = spec["experiment_id"] + ":first-buy"
            if (
                account_id != spec["account_id"]
                or request["day"] != spec["first_trade_date"]
                or order["order_id"] != expected_id
                or order["signal_date"] != spec["start_date"]
                or order["side"] != "buy"
                or order.get("limit_price") is not None
                or order["quantity"] != strategy["quantity"]
                or any(
                    instrument[k] != strategy[k] for k in ("market", "code", "exchange", "profile")
                )
                or quote["source"] != spec["data_sources"]["market"]
            ):
                raise LedgerConflict("order conflicts with frozen buy-and-hold strategy")


def check_daily_result(manifest, account_id, results, snapshot):
    if account_id != manifest["spec"]["account_id"]:
        return
    risk = manifest["spec"]["risk"]
    if _minor(snapshot["cash"]) < _minor(risk["min_cash_reserve"]):
        raise LedgerConflict("frozen cash reserve breached")
    for result in results:
        if _minor(result["cash_after"]) < _minor(risk["min_cash_reserve"]):
            raise LedgerConflict("frozen cash reserve breached during session")
        if result.get("side") == "buy" and result.get("status") == "filled":
            if -_minor(result["cash_delta"]) > _minor(risk["max_order_cash"]):
                raise LedgerConflict("frozen order cap breached")


def compare(book, experiment_id):
    with paper_connection(book.path) as conn:
        row = conn.execute(
            "SELECT account_id,benchmark_id,manifest_hash,manifest_json FROM paper_experiments WHERE experiment_id=?",
            (experiment_id,),
        ).fetchone()
        if row is None:
            raise LedgerConflict("unknown experiment")
        manifest = json.loads(row["manifest_json"])
        if _identity(manifest) != row["manifest_hash"]:
            raise LedgerCorruption("experiment manifest corrupted")
        from quant_agent.application.paper.runtime import resolve_runtime, runtime_status

        manifest = resolve_runtime(conn, manifest)
        runtime = runtime_status(manifest)
        left, right = (
            _snapshot(_audit(conn, row["account_id"])),
            _snapshot(_audit(conn, row["benchmark_id"])),
        )
        if (left["opening_cash"], left["currency"], left["start_date"]) != (
            right["opening_cash"],
            right["currency"],
            right["start_date"],
        ):
            raise LedgerCorruption("benchmark origin differs")

        def flows(account):
            return [
                (a["date"], a["phase"], a["kind"], a["amount"])
                for a in (
                    json.loads(r["intent_json"])
                    for r in conn.execute(
                        "SELECT intent_json FROM paper_actions WHERE account_id=? ORDER BY action_date,sequence",
                        (account,),
                    )
                )
                if a["kind"] in {"deposit", "withdrawal"}
            ]

        lf, rf = flows(row["account_id"]), flows(row["benchmark_id"])

        def curve(account):
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_daily'"
            ).fetchone():
                return []
            values = []
            for record in conn.execute(
                "SELECT result_json,result_hash FROM paper_daily WHERE account_id=? ORDER BY day",
                (account,),
            ):
                value = json.loads(record["result_json"])
                if _identity(value) != record["result_hash"]:
                    raise LedgerCorruption("daily record evidence mismatch")
                values.append(value)
            return values

        left_curve = curve(row["account_id"])
        right_curve = curve(row["benchmark_id"])
    if lf != rf or [x["date"] for x in left_curve] != [x["date"] for x in right_curve]:
        raise LedgerConflict("benchmark dates or external cash flows differ")
    return {
        "experiment_id": experiment_id,
        "benchmark": "cash",
        "matching_cashflows": True,
        "runtime": runtime,
        "total_return_status": "corporate_actions_unavailable",
        "points": [
            {
                "date": a["date"],
                "strategy_net_assets": a["net_assets"],
                "benchmark_net_assets": b["net_assets"],
                "difference": f"{Decimal(a['net_assets']) - Decimal(b['net_assets']):.2f}",
                "strategy_unit_value": a["unit_value"],
                "benchmark_unit_value": b["unit_value"],
            }
            for a, b in zip(left_curve, right_curve)
        ],
    }


def settle_pair(
    book,
    experiment_id,
    day,
    previous_day,
    calendar,
    actions,
    closes,
    quote=None,
    *,
    now=None,
):
    """Atomically settle strategy and matching cash benchmark for one observed day."""
    from quant_agent.application.paper.daily import settle_day

    now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    if now.tzinfo is None:
        raise ValueError("current time needs timezone")
    now = now.astimezone(ZoneInfo("Asia/Shanghai"))
    _day(day)
    if day > now.date().isoformat() or (day == now.date().isoformat() and now.hour < 16):
        raise LedgerConflict("daily close not yet available for prospective account")
    if not isinstance(actions, list):
        raise ValueError("actions must be a list")
    with paper_connection(book.path, write=True) as conn:
        row = conn.execute(
            "SELECT account_id,benchmark_id,manifest_json,manifest_hash FROM paper_experiments WHERE experiment_id=?",
            (experiment_id,),
        ).fetchone()
        if row is None:
            raise LedgerConflict("unknown experiment")
        manifest = json.loads(row["manifest_json"])
        if row["manifest_hash"] != _identity(manifest):
            raise LedgerCorruption("experiment manifest corrupted")
        from quant_agent.application.paper.runtime import resolve_runtime

        manifest = resolve_runtime(conn, manifest)
        has_daily = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='paper_daily'"
        ).fetchone()
        sealed = (
            conn.execute(
                "SELECT account_id FROM paper_daily WHERE day=? AND account_id IN (?,?)",
                (day, row["account_id"], row["benchmark_id"]),
            ).fetchall()
            if has_daily
            else []
        )
        verify_runtime(manifest, day, replay=len(sealed) == 2)
        spec = manifest["spec"]
        before = []
        after = []
        mirrored_before = []
        mirrored_after = []
        for action in actions:
            if action["date"] != day or action["phase"] not in {"preopen", "close"}:
                raise ValueError("paired actions must use this day and preopen or close")
            target = before if action["phase"] == "preopen" else after
            target.append({"action": action})
            if action["kind"] in {"deposit", "withdrawal"}:
                (mirrored_before if action["phase"] == "preopen" else mirrored_after).append(
                    {"action": action}
                )
        trade_events = []
        if day == spec["first_trade_date"]:
            sessions = calendar.get("sessions", [])
            if spec["start_date"] not in sessions or [
                d for d in sessions if spec["start_date"] < d <= day
            ] != [day]:
                raise LedgerConflict("first trade must follow the declared start trading session")
            if quote is None:
                raise ValueError("first trade requires explicit opening quote and session facts")
            strategy = spec["strategy"]
            instrument = {key: strategy[key] for key in ("market", "code", "exchange", "profile")}
            order = {
                "order_id": spec["experiment_id"] + ":first-buy",
                "market": strategy["market"],
                "code": strategy["code"],
                "side": "buy",
                "quantity": strategy["quantity"],
                "signal_date": spec["start_date"],
                "execution_date": day,
                "limit_price": None,
            }
            trade_events = [{"order": order, "instrument": instrument, "quote": quote}]
        elif quote is not None:
            raise ValueError("opening quote supplied outside first trade date")
        common = {"day": day, "previous_day": previous_day, "calendar": calendar}
        left = settle_day(
            book,
            row["account_id"],
            {**common, "events": before + trade_events + after, "closes": closes},
            _conn=conn,
            _pair=True,
        )
        right = settle_day(
            book,
            row["benchmark_id"],
            {**common, "events": mirrored_before + mirrored_after, "closes": []},
            _conn=conn,
            _pair=True,
        )
        return {"experiment_id": experiment_id, "strategy": left, "benchmark": right}
