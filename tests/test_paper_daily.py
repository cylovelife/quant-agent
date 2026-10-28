"""Atomic daily paper settlement using isolated ledgers and constructed observations.

@author cylovelife
"""

from dataclasses import asdict
from decimal import Decimal

import pytest
from test_paper_ledger import inputs
from test_paper_ledger import ledger as ledger
from test_paper_ledger import modules as modules
from test_paper_ledger import policy as policy

from quant_agent.application.paper.daily import daily_curve, settle_day

# Imported pytest fixtures intentionally share names with test parameters.
# ruff: noqa: F811


def day(day, previous=None, events=None, closes=None):
    return {
        "day": day,
        "previous_day": previous,
        "events": events or [],
        "closes": closes or [],
        "calendar": {"source": "fixture", "sessions": [day]},
    }


def close(day, price="10.00"):
    return {
        "market": "cn",
        "code": "600000",
        "date": day,
        "adjust": "raw",
        "close": price,
        "source": "fixture",
        "data_version": "v1",
    }


def order_event(
    execution,
    order_id="buy-1",
    side="buy",
    signal="2026-10-26",
    session="2026-10-27",
    price="10",
):
    order, instrument, quote = inputs(
        execution, order_id, side=side, signal=signal, session=session, price=price
    )
    return {
        "order": asdict(order),
        "instrument": asdict(instrument),
        "quote": asdict(quote),
    }


def action(kind, name, day, phase="preopen", **values):
    return {
        "action": {
            "kind": kind,
            "action_id": name,
            "date": day,
            "phase": phase,
            "source": "fixture",
            "known_at": day + "@" + phase,
            **values,
        }
    }


def test_equity_cost_flows_and_idempotent_curve(modules, ledger):
    execution, _ = modules
    initial = day("2026-10-26")
    start = settle_day(ledger, "fixture", initial)
    assert start["net_assets"] == "50000.00" and start["unit_value"] == "1"
    nxt = day(
        "2026-10-27",
        "2026-10-26",
        [
            action("deposit", "d", "2026-10-27", amount="1000.00"),
            order_event(execution),
        ],
        [close("2026-10-27", "11")],
    )
    result = settle_day(ledger, "fixture", nxt)
    assert result["cash"] == "49993.99" and result["market_value"] == "1100.00"
    assert result["net_assets"] == "51093.99" and result["profit_after_external_flows"] == "93.99"
    assert result["unrealized_trading_pnl"] == "93.99" and result["realized_trading_pnl"] == "0.00"
    assert result["total_return"] is None
    assert settle_day(ledger, "fixture", nxt)["replayed"]
    assert len(daily_curve(ledger, "fixture")) == 2
    with pytest.raises(modules[1].LedgerConflict):
        settle_day(ledger, "fixture", {**nxt, "closes": [close("2026-10-27", "12")]})
    with pytest.raises(modules[1].LedgerConflict):
        ledger.apply_action(
            "fixture",
            action("deposit", "late", "2026-10-27", phase="close", amount="1.00")["action"],
        )


def test_missing_close_rolls_back_order_and_retry_succeeds(modules, ledger):
    execution, ledger_module = modules
    settle_day(ledger, "fixture", day("2026-10-26"))
    event = order_event(execution)
    with pytest.raises(ledger_module.LedgerConflict):
        settle_day(ledger, "fixture", day("2026-10-27", "2026-10-26", [event]))
    assert ledger.snapshot("fixture")["cash"] == "50000.00"
    assert ledger.snapshot("fixture")["positions"] == []
    done = settle_day(
        ledger,
        "fixture",
        day("2026-10-27", "2026-10-26", [event], [close("2026-10-27")]),
    )
    assert done["cash"] == "48993.99"


def test_dividend_sale_and_split_reconcile(modules, ledger):
    execution, _ = modules
    settle_day(ledger, "fixture", day("2026-10-26"))
    events = [
        order_event(execution),
        action(
            "dividend_entitlement",
            "div",
            "2026-10-27",
            "close",
            market="cn",
            code="600000",
            net_per_share="0.10",
        ),
    ]
    settle_day(
        ledger,
        "fixture",
        day("2026-10-27", "2026-10-26", events, [close("2026-10-27")]),
    )
    events = [
        action("dividend_accrual", "accrue", "2026-10-28", entitlement_id="div"),
        action(
            "split",
            "split",
            "2026-10-28",
            market="cn",
            code="600000",
            numerator=2,
            denominator=1,
        ),
    ]
    after = settle_day(
        ledger,
        "fixture",
        day("2026-10-28", "2026-10-27", events, [close("2026-10-28", "5")]),
    )
    assert after["dividend_receivable"] == "10.00" and after["net_assets"] == "50003.99"
    assert (
        after["positions"][0]["quantity"] == 200
        and after["positions"][0]["cost_basis"] == "1006.01"
    )
    sell = order_event(execution, "sell", "sell", "2026-10-28", "2026-10-29", "5")
    sell["quote"]["facts"]["lower_limit"] = "4"
    sell["quote"]["facts"]["upper_limit"] = "6"
    sold = settle_day(
        ledger,
        "fixture",
        day("2026-10-29", "2026-10-28", [sell], [close("2026-10-29", "5")]),
    )
    assert sold["positions"][0]["quantity"] == 100 and sold["realized_trading_pnl"] == "-9.26"


def test_declared_previous_day_and_bad_raw_price_fail_without_mutation(modules, ledger):
    settle_day(ledger, "fixture", day("2026-10-26"))
    with pytest.raises(modules[1].LedgerConflict):
        settle_day(ledger, "fixture", day("2026-10-28", "2026-10-25"))
    bad = {**close("2026-10-27"), "adjust": "qfq"}
    with pytest.raises(ValueError):
        settle_day(ledger, "fixture", day("2026-10-27", "2026-10-26", closes=[bad]))
    assert len(daily_curve(ledger, "fixture")) == 1


def test_declared_skipped_session_is_rejected(modules, ledger):
    settle_day(ledger, "fixture", day("2026-10-26"))
    req = day("2026-10-28", "2026-10-26")
    req["calendar"]["sessions"] = ["2026-10-27", "2026-10-28"]
    with pytest.raises(modules[1].LedgerConflict, match="skipped"):
        settle_day(ledger, "fixture", req)
    assert len(daily_curve(ledger, "fixture")) == 1


def test_close_cash_flow_does_not_change_unit_value(modules, ledger):
    settle_day(ledger, "fixture", day("2026-10-26"))
    result = settle_day(
        ledger,
        "fixture",
        day(
            "2026-10-27",
            "2026-10-26",
            [action("deposit", "deposit-close", "2026-10-27", "close", amount="100.00")],
        ),
    )
    assert result["net_assets"] == "50100.00"
    assert result["profit_after_external_flows"] == "0.00"
    assert Decimal(result["unit_value"]) == 1 and Decimal(result["units"]) == 50100


def test_out_of_band_event_cannot_be_silently_absorbed(modules, ledger):
    settle_day(ledger, "fixture", day("2026-10-26"))
    with pytest.raises(modules[1].LedgerConflict, match="daily settlement"):
        ledger.apply_action(
            "fixture",
            action("deposit", "outside", "2026-10-27", amount="100.00")["action"],
        )
    assert len(daily_curve(ledger, "fixture")) == 1


def test_cli_reads_raw_market_and_outputs_curve(modules, ledger, tmp_path):
    import json
    import sqlite3
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    db = tmp_path / "market.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE kline (market TEXT, code TEXT, adjust TEXT, date TEXT, open REAL, close REAL, high REAL, low REAL, volume REAL, amount REAL, src TEXT, fetched_at TEXT)"
        )
        conn.execute(
            "INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "cn",
                "600000",
                "raw",
                "2026-10-27",
                10,
                11,
                11,
                10,
                10000,
                100000,
                "fixture",
                "2026-10-27T16:00:00+08:00",
            ),
        )
    execution, _ = modules
    order, instrument, quote = inputs(execution)
    payload = {
        "ledger_path": str(ledger.path),
        "db_path": str(db),
        "account_id": "fixture",
        "calendar": {"source": "fixture", "sessions": ["2026-10-27"]},
        "days": [
            {
                "day": "2026-10-26",
                "previous_day": None,
                "events": [],
                "close_symbols": [],
            },
            {
                "day": "2026-10-27",
                "previous_day": "2026-10-26",
                "events": [
                    {
                        "order": asdict(order),
                        "instrument": asdict(instrument),
                        "session": asdict(quote.facts),
                    }
                ],
                "close_symbols": [{"market": "cn", "code": "600000"}],
            },
        ],
    }
    request = tmp_path / "daily.json"
    request.write_text(json.dumps(payload))
    cmd = [sys.executable, str(root / "run.py"), "--settle-paper-days", str(request)]
    run = subprocess.run(cmd, cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout)["days"][-1]["net_assets"] == "50093.99"
    repeated = subprocess.run(cmd, cwd=tmp_path, capture_output=True, text=True, timeout=30)
    assert repeated.returncode == 0 and all(
        d["replayed"] for d in json.loads(repeated.stdout)["days"]
    )
    status = tmp_path / "curve.json"
    status.write_text(json.dumps({"ledger_path": str(ledger.path), "account_id": "fixture"}))
    curve = subprocess.run(
        [sys.executable, str(root / "run.py"), "--paper-curve", str(status)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert curve.returncode == 0 and len(json.loads(curve.stdout)["days"]) == 2


def test_concurrent_day_settlement_is_exactly_once(modules, ledger):
    from concurrent.futures import ThreadPoolExecutor

    execution, _ = modules
    settle_day(ledger, "fixture", day("2026-10-26"))
    request = day("2026-10-27", "2026-10-26", [order_event(execution)], [close("2026-10-27")])
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: settle_day(ledger, "fixture", request), range(2)))
    assert sorted(item["replayed"] for item in results) == [False, True]
    assert ledger.snapshot("fixture")["cash"] == "48993.99"
    assert len(daily_curve(ledger, "fixture")) == 2


def test_dividend_payment_and_withdrawal_reconcile(modules, ledger):
    execution, _ = modules
    settle_day(ledger, "fixture", day("2026-10-26"))
    settle_day(
        ledger,
        "fixture",
        day(
            "2026-10-27",
            "2026-10-26",
            [
                order_event(execution),
                action(
                    "dividend_entitlement",
                    "ent",
                    "2026-10-27",
                    "close",
                    market="cn",
                    code="600000",
                    net_per_share="0.10",
                ),
            ],
            [close("2026-10-27")],
        ),
    )
    accrued = settle_day(
        ledger,
        "fixture",
        day(
            "2026-10-28",
            "2026-10-27",
            [action("dividend_accrual", "accrue", "2026-10-28", entitlement_id="ent")],
            [close("2026-10-28")],
        ),
    )
    paid = settle_day(
        ledger,
        "fixture",
        day(
            "2026-10-29",
            "2026-10-28",
            [
                action("dividend_payment", "payment", "2026-10-29", accrual_id="accrue"),
                action("withdrawal", "withdraw", "2026-10-29", "close", amount="100.00"),
            ],
            [close("2026-10-29")],
        ),
    )
    assert accrued["dividend_receivable"] == "10.00"
    assert paid["dividend_receivable"] == "0.00"
    assert Decimal(paid["net_assets"]) == Decimal(accrued["net_assets"]) - Decimal("100")
    assert paid["net_external_flow"] == "-100.00"
    assert Decimal(paid["unit_value"]) == Decimal(accrued["unit_value"])
