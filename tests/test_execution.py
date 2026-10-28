"""Pure paper-order fills, cash constraints and raw-price adapter contracts.

@author cylovelife
"""

import copy
import json
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

from quant_agent.infrastructure.configuration import load_policy

ROOT = Path(__file__).resolve().parents[1]


def _account(api, day, cash, lots, market="cn", code="600000"):
    return api.AccountView(day, cash, lots, market, code)


@pytest.fixture
def api():
    import quant_agent.domain.trading.execution as execution

    return execution


@pytest.fixture
def policy(api):
    return load_policy(ROOT / "params/execution_v1.json")


@pytest.fixture
def instrument(api):
    return api.Instrument("cn", "600000", "SSE", "cn_mainboard")


@pytest.fixture
def order(api):
    return api.Order("order-1", "cn", "600000", "buy", 100, "2026-10-26", "2026-10-27")


@pytest.fixture
def quote(api):
    facts = api.SessionFacts(
        "2026-10-27",
        "fixture",
        "2026-10-27@open",
        "trading",
        "regular",
        "9",
        "11",
        10000,
        "assumed",
        "2026-10-26",
        "cn",
        "600000",
    )
    return api.OpenQuote(
        "cn",
        "600000",
        "2026-10-27",
        "10",
        "raw",
        "fixture",
        "2026-10-27T09:25:00+08:00",
        "raw-version",
        facts,
    )


@pytest.fixture
def account(api):
    return _account(api, "2026-10-27", "50000", ())


def test_buy_prices_are_adverse_tick_rounded_and_cash_includes_fees(
    api, policy, instrument, order, quote, account
):
    before = copy.deepcopy(account)
    result = api.match_order(order, instrument, quote, account, policy)
    assert result["status"] == "filled"
    assert result["fill_price"] == "10.01"
    assert result["notional"] == "1001.00"
    assert result["fees"] == {
        "commission": "5.00",
        "stamp_duty": "0.00",
        "transfer": "0.01",
        "total": "5.01",
    }
    assert result["cash_delta"] == "-1006.01"
    assert result["quantity_delta"] == 100
    assert result["slippage_cost"] == "1.00"
    assert account == before
    assert result["policy_id"] == policy.policy_id
    assert api.match_order(order, instrument, quote, account, policy) == result


@pytest.mark.parametrize("cash", ["1001", "1006.00", "0"])
def test_insufficient_cash_including_minimum_fee_does_not_shrink_order(
    api, policy, instrument, order, quote, cash
):
    result = api.match_order(order, instrument, quote, _account(api, quote.date, cash, ()), policy)
    assert result["status"] == "rejected"
    assert result["reason"] == "insufficient_cash"
    assert result["cash_delta"] == "0.00" and result["quantity_delta"] == 0
    assert result["filled_quantity"] == 0 and result["fill_price"] is None
    assert result["fees"]["total"] == "0.00"


def test_exact_cash_boundary_and_gap_use_execution_open(api, policy, instrument, order, quote):
    state = _account(api, quote.date, "1006.01", ())
    assert api.match_order(order, instrument, quote, state, policy)["status"] == "filled"
    gap = replace(quote, open_price="10.50")
    rejected = api.match_order(order, instrument, gap, state, policy)
    assert rejected["reason"] == "insufficient_cash"
    assert rejected["cash_delta"] == "0.00"


@pytest.mark.parametrize("quantity", [0, -100, 99, 150, 1000100, True])
def test_invalid_buy_quantities_are_rejected(
    api, policy, instrument, order, quote, account, quantity
):
    result = api.match_order(replace(order, quantity=quantity), instrument, quote, account, policy)
    assert result["status"] == "rejected"
    assert result["quantity_delta"] == 0


def test_sell_fee_directions_and_t_plus_one(api, policy, instrument, order, quote):
    sell = replace(order, side="sell")
    today_lot = api.PositionLot(100, quote.date)
    today = _account(api, quote.date, "0", (today_lot,))
    blocked = api.match_order(sell, instrument, quote, today, policy)
    assert blocked["reason"] == "insufficient_sellable_quantity"
    older = _account(api, quote.date, "0", (api.PositionLot(100, "2026-10-26"),))
    filled = api.match_order(sell, instrument, quote, older, policy)
    assert filled["fill_price"] == "9.99"
    assert filled["cash_delta"] == "993.49"
    assert filled["fees"] == {
        "commission": "5.00",
        "stamp_duty": "0.50",
        "transfer": "0.01",
        "total": "5.51",
    }
    assert filled["quantity_delta"] == -100


def test_odd_lot_must_sell_residual_together(api, policy, instrument, order, quote):
    held = _account(api, quote.date, "0", (api.PositionLot(250, "2026-10-23"),))
    assert (
        api.match_order(replace(order, side="sell", quantity=150), instrument, quote, held, policy)[
            "status"
        ]
        == "filled"
    )
    assert (
        api.match_order(replace(order, side="sell", quantity=125), instrument, quote, held, policy)[
            "status"
        ]
        == "rejected"
    )
    assert (
        api.match_order(replace(order, side="sell", quantity=350), instrument, quote, held, policy)[
            "status"
        ]
        == "rejected"
    )


def test_etf_has_own_tick_fees_and_settlement(api, policy, order, quote):
    fund = api.Instrument("etf", "510300", "SSE", "cn_equity_etf")
    order = replace(order, market="etf", code="510300")
    quote = replace(
        quote,
        market="etf",
        code="510300",
        open_price="3.456",
        facts=replace(quote.facts, lower_limit="3", upper_limit="4", market="etf", code="510300"),
    )
    fill = api.match_order(
        order,
        fund,
        quote,
        _account(api, quote.date, "1000", (), "etf", "510300"),
        policy,
    )
    assert fill["fill_price"] == "3.458"
    assert fill["fees"] == {
        "commission": "0.03",
        "stamp_duty": "0.00",
        "transfer": "0.00",
        "total": "0.03",
    }
    assert fill["cash_delta"] == "-345.83"
    assert (
        api.match_order(
            replace(order, side="sell"),
            fund,
            quote,
            _account(
                api,
                quote.date,
                "0",
                (api.PositionLot(100, quote.date),),
                "etf",
                "510300",
            ),
            policy,
        )["status"]
        == "rejected"
    )


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"trading_status": "suspended"}, "suspended"),
        ({"trading_status": "unknown"}, "trading_status_unknown"),
        ({"instrument_status": "unknown"}, "instrument_status_unsupported"),
        ({"instrument_status": "risk_warning"}, "instrument_status_unsupported"),
        ({"instrument_status": "ipo"}, "instrument_status_unsupported"),
        ({"lower_limit": None}, "price_limits_unknown"),
        ({"opening_capacity": None}, "opening_liquidity_unknown"),
        ({"opening_capacity": 50}, "opening_capacity_insufficient"),
        ({"known_at": "2026-10-27@close"}, "future_session_facts"),
        ({"source": ""}, "session_source_missing"),
        ({"previous_session": "2026-10-23"}, "next_session_unverified"),
        ({"code": "600519"}, "session_instrument_mismatch"),
    ],
)
def test_unknown_or_untradeable_session_never_produces_fill(
    api, policy, instrument, order, quote, account, change, reason
):
    result = api.match_order(
        order,
        instrument,
        replace(quote, facts=replace(quote.facts, **change)),
        account,
        policy,
    )
    assert result["status"] != "filled" and result["reason"] == reason
    assert result["cash_delta"] == "0.00" and result["quantity_delta"] == 0


@pytest.mark.parametrize(
    "side,opening,reason",
    [
        ("buy", "11", "limit_up_queue_unknown"),
        ("sell", "9", "limit_down_queue_unknown"),
    ],
)
def test_at_price_limit_does_not_assume_queue_filled(
    api, policy, instrument, order, quote, side, opening, reason
):
    state = _account(api, quote.date, "50000", (api.PositionLot(100, "2026-10-23"),))
    result = api.match_order(
        replace(order, side=side),
        instrument,
        replace(quote, open_price=opening),
        state,
        policy,
    )
    assert result["status"] == "unfilled" and result["reason"] == reason


def test_limit_order_and_slippage_outside_band_are_not_capped_to_fill(
    api, policy, instrument, order, quote, account
):
    assert (
        api.match_order(replace(order, limit_price="10.00"), instrument, quote, account, policy)[
            "reason"
        ]
        == "limit_not_met"
    )
    near = replace(quote, open_price="20.99")
    narrow = replace(near, facts=replace(quote.facts, lower_limit="19", upper_limit="21"))
    assert api.match_order(order, instrument, narrow, account, policy)["status"] == "unfilled"


@pytest.mark.parametrize(
    "field,value",
    [
        ("adjust", "qfq"),
        ("source", ""),
        ("fetched_at", None),
        ("data_version", ""),
        ("open_price", "NaN"),
        ("open_price", "10.001"),
    ],
)
def test_invalid_price_identity_or_provenance_is_unavailable(
    api, policy, instrument, order, quote, account, field, value
):
    result = api.match_order(order, instrument, replace(quote, **{field: value}), account, policy)
    assert result["status"] == "unavailable"
    assert result["filled_quantity"] == 0


def test_missing_target_session_never_advances_to_later_quote(
    api, policy, instrument, order, quote, account
):
    result = api.match_order(order, instrument, replace(quote, date="2026-10-28"), account, policy)
    assert result["reason"] == "execution_session_mismatch"
    assert result["cash_delta"] == "0.00"


def test_order_cannot_use_same_day_signal_or_future_holdings(
    api, policy, instrument, order, quote, account
):
    assert (
        api.match_order(replace(order, signal_date=quote.date), instrument, quote, account, policy)[
            "status"
        ]
        == "rejected"
    )
    future = replace(account, lots=(api.PositionLot(100, "2026-10-28"),))
    assert (
        api.match_order(order, instrument, quote, future, policy)["reason"]
        == "invalid_account_snapshot"
    )


def test_profile_is_dated_and_not_inferred_for_star_market(api, policy, order, quote, account):
    star = api.Instrument("cn", "688001", "SSE", "cn_mainboard")
    result = api.match_order(
        replace(order, code="688001"),
        star,
        replace(quote, code="688001"),
        account,
        policy,
    )
    assert result["reason"] == "instrument_profile_mismatch"
    early = replace(order, signal_date="2026-07-01", execution_date="2026-07-02")
    assert (
        api.match_order(
            early,
            api.Instrument("cn", "600000", "SSE", "cn_mainboard"),
            replace(quote, date="2026-07-02"),
            replace(account, date="2026-07-02"),
            policy,
        )["reason"]
        == "policy_not_effective"
    )


def test_policy_identity_changes_with_cost_and_rejects_bad_costs(api, policy, tmp_path):
    raw = json.loads((ROOT / "params/execution_v1.json").read_text())
    raw["profiles"]["cn_mainboard"]["slippage_bps"] = "10"
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(raw))
    assert load_policy(path).policy_id != policy.policy_id
    raw["profiles"]["cn_mainboard"]["commission_rate"] = "-0.1"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError):
        load_policy(path)


@pytest.fixture
def raw_frame():
    frame = pd.DataFrame(
        {
            "date": pd.to_datetime(["2026-10-26", "2026-10-27", "2026-10-28"]),
            "open": [9.0, 10.0, 10.5],
            "close": [9.0, 10.1, 10.6],
            "high": [9.5, 10.2, 11.0],
            "low": [8.5, 9.9, 10.0],
            "volume": [100.0, 100.0, 100.0],
            "amount": [900.0, 1000.0, 1050.0],
        }
    )
    frame.attrs.update(source="fixture", fetched_at="2026-10-28T18:00:00+08:00", adjust="raw")
    return frame


def test_raw_adapter_ignores_close_high_low_volume_and_future_rows(
    api, instrument, order, quote, raw_frame
):
    from quant_agent.infrastructure.persistence.execution_data import opening_quote

    first = opening_quote(raw_frame, instrument, order.execution_date, quote.facts)
    altered = raw_frame.copy()
    altered.loc[1:, ["close", "high", "low", "volume", "amount"]] = -999
    altered.loc[2, "open"] = 9999
    again = opening_quote(altered, instrument, order.execution_date, quote.facts)
    assert first == again
    assert first.open_price == "10.0"
    wrong = raw_frame.copy()
    wrong.attrs["adjust"] = "qfq"
    assert opening_quote(wrong, instrument, order.execution_date, quote.facts).issues


def test_backtest_and_portfolio_use_same_order_model_without_writing_book(
    api, policy, instrument, order, quote, account, raw_frame
):
    from quant_agent.application.investing.portfolio import preview_paper_order
    from quant_agent.application.research.backtest import replay_paper_order
    from quant_agent.infrastructure.persistence.execution_data import opening_quote

    supplied = opening_quote(raw_frame, instrument, order.execution_date, quote.facts)
    direct = api.match_order(order, instrument, supplied, account, policy)
    assert replay_paper_order(order, instrument, raw_frame, quote.facts, account, policy) == direct
    assert preview_paper_order(order, instrument, supplied, account, policy) == direct


def test_local_raw_db_cli_is_read_only_and_has_no_qfq_fallback(
    api, policy, instrument, order, quote, account, tmp_path
):
    db = tmp_path / "bars.db"
    with sqlite3.connect(db) as con:
        con.execute(
            "CREATE TABLE kline(market TEXT,code TEXT,adjust TEXT,date TEXT,open REAL,close REAL,high REAL,low REAL,volume REAL,amount REAL,src TEXT,fetched_at TEXT)"
        )
        con.executemany(
            "INSERT INTO kline VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    "cn",
                    "600000",
                    adjust,
                    "2026-10-27",
                    opening,
                    10,
                    11,
                    9,
                    100,
                    1000,
                    "fixture",
                    "2026-10-27 18:00:00",
                )
                for adjust, opening in [("qfq", 1), ("raw", 10)]
            ],
        )
    from dataclasses import asdict

    manifest = {
        "db_path": str(db),
        "policy_path": str(ROOT / "params/execution_v1.json"),
        "order": asdict(order),
        "instrument": asdict(instrument),
        "session": asdict(quote.facts),
        "account": asdict(account),
    }
    file = tmp_path / "request.json"
    file.write_text(json.dumps(manifest))
    before = db.read_bytes()
    result = subprocess.run(
        [sys.executable, str(ROOT / "run.py"), "--simulate-order", str(file)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    filled = json.loads(result.stdout)
    assert filled["fill_price"] == "10.01"
    assert db.read_bytes() == before
    assert not (tmp_path / "state").exists()
    with sqlite3.connect(db) as con:
        con.execute("DELETE FROM kline WHERE adjust='raw'")
    result = subprocess.run(
        [sys.executable, str(ROOT / "run.py"), "--simulate-order", str(file)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "unavailable"


def test_holdings_of_another_symbol_cannot_fund_a_sell(
    api, policy, instrument, order, quote, account
):
    mismatched = replace(account, code="600519", lots=(api.PositionLot(100, "2026-10-23"),))
    result = api.match_order(replace(order, side="sell"), instrument, quote, mismatched, policy)
    assert result["reason"] == "account_instrument_mismatch"


def test_weekly_raw_open_cannot_be_used_as_daily_execution_open(
    instrument, order, quote, raw_frame
):
    from quant_agent.domain.market.quality import capture_provenance
    from quant_agent.infrastructure.persistence.execution_data import opening_quote

    metadata = capture_provenance(raw_frame, instrument.market, instrument.code, "raw")
    metadata["aggregation"] = {"frequency": "1w"}
    raw_frame.attrs["data_provenance"] = metadata
    assert opening_quote(raw_frame, instrument, order.execution_date, quote.facts).issues
