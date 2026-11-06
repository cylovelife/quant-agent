"""Historical synthetic accounts using the existing raw execution ledger.

@author cylovelife
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

from quant_agent.application.paper.ledger import PaperLedger
from quant_agent.application.research.model_selection import long_weight_score
from quant_agent.domain.trading.execution import (
    Instrument,
    OpenQuote,
    Order,
    SessionFacts,
    policy_from_definition,
)


def evaluate_account(
    data: dict, candidate: dict, policy_definition: dict, initial_cash: str
) -> dict:
    """Top one, 100 shares, next supplied open; no automatic portfolio promotion."""
    instruments = {item["code"]: Instrument(**item) for item in data["instruments"]}
    rows = {(row["date"], row["code"]): row for row in data["rows"]}
    dates = data["sessions"]
    policy = policy_from_definition(policy_definition)
    orders, curve = [], []
    peak = Decimal(initial_cash)
    drawdown = Decimal(0)
    turnover_notional = Decimal(0)
    slippage = Decimal(0)
    with TemporaryDirectory(prefix="synthetic-research-account-") as directory:
        ledger = PaperLedger.create(Path(directory) / "account.db")
        ledger.create_account("research", initial_cash, dates[0], policy)
        for index, day in enumerate(dates):
            if index:
                previous = dates[index - 1]
                panel = pd.DataFrame(
                    [
                        {"code": code, **rows[(previous, code)]["factors"]}
                        for code in sorted(instruments)
                    ]
                )
                panel["score"] = long_weight_score(panel, candidate["weights"])
                target = panel.sort_values(["score", "code"], ascending=[False, True]).iloc[0][
                    "code"
                ]
                state = ledger.snapshot("research")
                holdings = {
                    position["code"]: position["quantity"] for position in state["positions"]
                }
                intents = [
                    (code, "sell", quantity)
                    for code, quantity in sorted(holdings.items())
                    if code != target
                ]
                if target not in holdings:
                    intents.append((target, "buy", 100))
                for code, side, quantity in intents:
                    # A suspended/limited exit must not turn a one-position policy
                    # into two positions. The actual ledger, not planned sales, wins.
                    if side == "buy" and ledger.snapshot("research")["positions"]:
                        continue
                    row = rows[(day, code)]
                    quote = OpenQuote(
                        market="cn",
                        code=code,
                        date=day,
                        open_price=row["open"],
                        adjust="raw",
                        source="synthetic",
                        fetched_at=day + "T09:30:00+08:00",
                        data_version="synthetic_phase_v1",
                        facts=SessionFacts(**row["facts"]),
                    )
                    order = Order(f"{day}:{side}:{code}", "cn", code, side, quantity, previous, day)
                    result = ledger.execute("research", order, instruments[code], quote)
                    if result["status"] == "unavailable":
                        raise ValueError("account execution unavailable: " + result["reason"])
                    orders.append(result)
                    turnover_notional += Decimal(result["notional"])
                    slippage += Decimal(result["slippage_cost"])
            state = ledger.snapshot("research")
            market_value = sum(
                (
                    Decimal(rows[(day, item["code"])]["close"]) * item["quantity"]
                    for item in state["positions"]
                ),
                Decimal(0),
            )
            net_assets = Decimal(state["cash"]) + market_value
            peak = max(peak, net_assets)
            drawdown = max(drawdown, 1 - net_assets / peak)
            curve.append(
                {
                    "date": day,
                    "cash": state["cash"],
                    "market_value": f"{market_value:.2f}",
                    "net_assets": f"{net_assets:.2f}",
                    "positions": state["positions"],
                    "cumulative_fees": state["fees"]["total"],
                }
            )
    return {
        "scheme": candidate["scheme"],
        "net_assets": curve[-1]["net_assets"],
        "simulated_account_return": str(
            Decimal(curve[-1]["net_assets"]) / Decimal(initial_cash) - 1
        ),
        "max_drawdown": str(drawdown),
        "turnover_over_initial_cash": str(turnover_notional / Decimal(initial_cash)),
        "fees": state["fees"]["total"],
        "slippage_cost": str(slippage),
        "orders": orders,
        "curve": curve,
        "reconciled": state["reconciled"],
        "total_return": None,
        "corporate_action_coverage": "unverified",
        "end_valuation": "raw_close_mark_to_market_no_liquidation",
        "evidence_kind": "synthetic",
        "promotion_allowed": False,
        "independent_new_samples": 0,
    }
