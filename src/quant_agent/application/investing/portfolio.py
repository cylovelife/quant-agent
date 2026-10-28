# -*- coding: utf-8 -*-
"""虚拟持仓账本：把每日推荐登记为持仓，逐日结算，供离场模型决策。

新信号先登记pending，次根完整日线开盘入场；入场日为持有第1日。
使用冻结的止盈／止损／持有期逐根回放，只以真实收盘标签结束，不用最新报价替代。
旧持仓保留原状态及数值，标为legacy_unverified，不与v2成交混算。
新成交另存trades_v2.csv；positions.json保留版本及已观察价格，重复结算不重复平仓。
独立raw现金账本只经record_paper_order显式记账；原v2结算不触碰现金事件。
"""

import csv
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from quant_agent.application.paper.ledger import PaperLedger
from quant_agent.domain.trading.execution import (
    AccountView,
    ExecutionPolicy,
    Instrument,
    OpenQuote,
    Order,
    match_order,
)
from quant_agent.domain.trading.labels import (
    LABEL_VERSION,
    SUPPORTED_MARKETS,
    LabelSpec,
    positive_price,
    resolve_trade,
)
from quant_agent.runtime.paths import WORKSPACE_ROOT


def preview_paper_order(
    order: Order,
    instrument: Instrument,
    quote: OpenQuote,
    account: AccountView,
    policy: ExecutionPolicy,
) -> dict:
    """Return a proposed fill/deltas without writing positions, trades or cash events."""
    return match_order(order, instrument, quote, account, policy)


def record_paper_order(
    ledger: PaperLedger, account_id: str, order: Order, instrument: Instrument, quote: OpenQuote
) -> dict:
    """Book a simulated order using ledger-owned cash/lots, isolated from v2 research positions."""
    return ledger.execute(account_id, order, instrument, quote)


ROOT = str(WORKSPACE_ROOT)
STATE = os.path.join(ROOT, "state")
BOOK_PATH = os.path.join(STATE, "positions.json")
TRADES_PATH = os.path.join(STATE, "trades.csv")

TRADE_COLS = [
    "exit_date",
    "code",
    "name",
    "market",
    "entry_date",
    "entry_price",
    "exit_price",
    "exit_ret",
    "days_held",
    "exit_reason",
    "entry_score",
]


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------
def load_book(path: "str | None" = None) -> dict:
    path = path or BOOK_PATH
    try:
        with open(path, encoding="utf-8") as f:
            book = json.load(f)
    except FileNotFoundError:
        return {"created": datetime.now().isoformat(), "positions": []}
    _validate_book(book)
    return book


def _validate_book(book: dict) -> None:
    if (
        not isinstance(book, dict)
        or not isinstance(book.get("positions"), list)
        or any(not isinstance(pos, dict) for pos in book["positions"])
    ):
        raise ValueError("invalid positions book; existing records require recovery")


def _replace_file(path: str, write) -> None:
    """Publish a complete file; failures retain the previous durable record."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".portfolio-", dir=target.parent)
    try:
        with os.fdopen(handle, "w", newline="", encoding="utf-8") as stream:
            write(stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_book(book: dict, path: "str | None" = None) -> None:
    path = path or BOOK_PATH
    _validate_book(book)
    updated = datetime.now().isoformat()
    payload = {**book, "updated": updated}
    _replace_file(path, lambda stream: json.dump(payload, stream, ensure_ascii=False, indent=2))
    book["updated"] = updated


def _append_trades(rows: list, path: "str | None" = None) -> None:
    path = path or TRADES_PATH
    if not rows:
        return
    columns = TRADE_COLS
    if all(row.get("label_version") == LABEL_VERSION for row in rows):
        path = str(Path(path).with_name("trades_v2.csv"))
        columns = TRADE_COLS + [
            "signal_date",
            "signal_time",
            "entry_time",
            "label_end_date",
            "label_end_time",
            "label_version",
            "label_id",
        ]
        prior = []
        if os.path.exists(path):
            with open(path, newline="", encoding="utf-8") as stream:
                reader = csv.DictReader(stream)
                if reader.fieldnames != columns:
                    raise ValueError("invalid v2 trades header; existing history requires recovery")
                prior = list(reader)
        identities = {}
        output = list(prior)
        incoming = [
            {key: str(row.get(key) if row.get(key) is not None else "") for key in columns}
            for row in rows
        ]
        for index, row in enumerate(prior + incoming):
            identity = tuple(row.get(key) for key in ("market", "code", "signal_date", "label_id"))
            if (
                None in row
                or any(row.get(key) is None for key in columns)
                or row.get("label_version") != LABEL_VERSION
                or not all(identity)
            ):
                raise ValueError("invalid v2 trade; existing history requires recovery")
            if identity in identities and identities[identity] != row:
                raise ValueError("completed v2 trade conflicts with recorded history")
            if index >= len(prior) and identity not in identities:
                output.append(row)
            identities[identity] = row
        if len(output) == len(prior):
            return

        def write_trades(stream):
            writer = csv.DictWriter(stream, fieldnames=columns)
            writer.writeheader()
            writer.writerows(output)

        _replace_file(path, write_trades)
        return
    exists = os.path.exists(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        if not exists:
            w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in columns})


# ---------------------------------------------------------------------------
# 交易日历
# ---------------------------------------------------------------------------
def trading_calendar(kline_map: dict) -> dict:
    """从 K 线缓存提取各市场交易日历 {market: [YYYY-MM-DD, ...]}，另附 all。"""
    cal = {}
    for key, k in (kline_map or {}).items():
        if k is None or len(k) == 0 or "date" not in getattr(k, "columns", []):
            continue
        market = key[0] if isinstance(key, tuple) else "all"
        ds = set(k["date"].astype(str).str.slice(0, 10).tolist())
        cal.setdefault(market, set()).update(ds)
    out = {m: sorted(ds) for m, ds in cal.items()}
    all_days = set()
    for ds in out.values():
        all_days.update(ds)
    out["all"] = sorted(all_days)
    return out


def trading_days_between(cal: dict, market: str, start: str, end: str) -> int:
    """(start, end] 之间的交易日数。取不到该市场日历则回退到全市场并集。"""
    ds = cal.get(market) or cal.get("all") or []
    return sum(1 for d in ds if start < d <= end)


# ---------------------------------------------------------------------------
# 结算
# ---------------------------------------------------------------------------
def settle_book(
    book: dict,
    quotes: dict,
    cal: dict,
    params: dict,
    today: str,
    kline_fn=None,
    data_context: dict | None = None,
) -> list:
    """Replay saved v2 policies from daily bars; quotes cannot backfill closes."""
    closed = []
    for pos in book.get("positions", []):
        if pos.get("status") not in ("pending", "open"):
            continue
        if pos.get("label_version") != LABEL_VERSION:
            pos["verification_status"] = "legacy_unverified"
            quote = quotes.get((pos.get("market"), str(pos.get("code")))) or {}
            if positive_price(quote.get("price")) and positive_price(pos.get("entry_price")):
                ref_ret = float(quote["price"]) / float(pos["entry_price"]) - 1
                bt = params.get("backtest", {})
                stop, take = bt.get("stop_loss"), bt.get("take_profit")
                held = trading_days_between(cal, pos.get("market"), pos["entry_date"], today)
                trigger = (
                    "止损复核"
                    if stop is not None and ref_ret <= stop
                    else "止盈复核"
                    if take is not None and ref_ret >= take
                    else "到期复核"
                    if held >= bt.get("hold_days", 5)
                    else None
                )
                pos["legacy_risk_review"] = {
                    "date": quote.get("date") or today,
                    "reference_ret": ref_ret,
                    "trigger": trigger,
                }
            continue
        if kline_fn is None:
            pos["verification_status"] = "unavailable"
            continue
        if data_context is not None and not data_context.get("series", {}).get(
            f"{pos['market']}:{pos['code']}", {}
        ).get("data_usable", False):
            pos["verification_status"] = "unavailable"
            continue
        frame = kline_fn(pos["code"], pos["market"], pos.get("mkt_id"))
        spec = LabelSpec.from_dict(pos["label_spec"])
        trade = resolve_trade(
            frame, pos["signal_date"], spec, asof=today, calendar=cal.get(pos["market"])
        )
        if trade["status"] == "unavailable":
            pos["verification_status"] = "unavailable"
            continue
        # Never silently replace an already observed fill or close history after a revision.
        history = []
        if trade["last_date"]:
            for row in frame.itertuples():
                day = str(row.date)[:10]
                if pos["signal_date"] < day <= trade["last_date"]:
                    history.append([day, float(row.close)])
        old_history = pos.get("observed_prices", [])
        if (
            pos.get("entry_price") is not None and pos["entry_price"] != trade["entry_price"]
        ) or history[: len(old_history)] != old_history:
            pos["verification_status"] = "revised_history"
            continue
        pos.update(trade)
        pos["observed_prices"] = history
        pos["verification_status"] = "verified"
        if trade["status"] == "mature":
            pos.update(
                status="closed",
                exit_date=trade["label_end_date"],
                exit_ret=trade["ret"],
                exit_reason=trade["reason"],
            )
            closed.append(pos)
        elif trade["last_price"] is not None:
            pos["ret"] = trade["last_price"] / trade["entry_price"] - 1
    _append_trades(closed)
    return closed


def open_positions(book: dict) -> list:
    return [p for p in book.get("positions", []) if p.get("status") == "open"]


def latest_picks_file(before: "str | None" = None, state_dir: str = STATE) -> "str | None":
    """最近一份历史推荐文件（可限定早于某日）。"""
    import glob

    files = sorted(glob.glob(os.path.join(state_dir, "picks_v2_*.json")))
    if before:
        files = [f for f in files if os.path.basename(f).split("picks_v2_")[-1][:10] < before]
    return files[-1] if files else None


# ---------------------------------------------------------------------------
# 建仓
# ---------------------------------------------------------------------------
def add_positions(
    book: dict, picks_by_market: dict, params: dict, today: str, max_per_market: int = 5
) -> list:
    """Register pending signals; reference prices never become simulated fills."""
    positions = book.setdefault("positions", [])
    held = {
        (p.get("market"), str(p.get("code")))
        for p in positions
        if p.get("status") in ("open", "pending")
    }
    ids = {p.get("id") for p in positions}
    counts = {}
    for pos in positions:
        if pos.get("signal_date", pos.get("entry_date")) == today:
            counts[pos.get("market")] = counts.get(pos.get("market"), 0) + 1
    added = []
    for market, picks in (picks_by_market or {}).items():
        if market not in SUPPORTED_MARKETS:
            continue
        for pick in picks:
            if counts.get(market, 0) >= max_per_market:
                break
            signal_date = pick.get("signal_date", today)
            key = (market, str(pick["code"]))
            identity = f"{market}-{pick['code']}-{signal_date}"
            if key in held or identity in ids:
                continue
            spec = (
                LabelSpec.from_dict(pick["label_spec"])
                if "label_spec" in pick
                else LabelSpec.from_params(params)
            )
            pos = {
                **pick,
                "id": identity,
                "market": market,
                "code": str(pick["code"]),
                "signal_date": signal_date,
                "signal_time": signal_date + "@close",
                "label_spec": spec.to_dict(),
                "label_version": LABEL_VERSION,
                "label_id": spec.label_id,
                "entry_date": None,
                "entry_time": None,
                "entry_price": None,
                "entry_score": pick.get("score"),
                "status": "pending",
                "days_held": 0,
                "ret": None,
                "last_price": None,
                "last_date": None,
                "label_end_date": None,
                "label_end_time": None,
            }
            positions.append(pos)
            added.append(pos)
            held.add(key)
            ids.add(identity)
            counts[market] = counts.get(market, 0) + 1
    return added


# ---------------------------------------------------------------------------
# First-run replay only accepts archives with explicit signal and label semantics.
# ---------------------------------------------------------------------------
def backfill_from_picks(
    book: dict,
    picks_path: str,
    kline_fn,
    params: dict,
    today: str,
    cal: dict,
    max_per_market: int | None = None,
    data_context: dict | None = None,
) -> dict:
    """Replay a frozen v2 archive without inventing fills for legacy picks."""
    with open(picks_path, encoding="utf-8") as f:
        rec = json.load(f)
    picks = rec.get("picks", [])
    if rec.get("label_version") != LABEL_VERSION:
        return {"opened": 0, "closed": 0, "skipped": len(picks)}
    by_market = {}
    for pick in picks:
        if data_context is not None and not data_context.get("series", {}).get(
            f"{pick['market']}:{pick['code']}", {}
        ).get("data_usable", False):
            continue
        by_market.setdefault(pick["market"], []).append(pick)
    for items in by_market.values():
        items.sort(key=lambda p: float(p.get("score") or 0), reverse=True)
    added = add_positions(
        book,
        by_market,
        params,
        rec["date"],
        max_per_market if max_per_market is not None else len(picks),
    )
    closed = settle_book(book, {}, cal, params, today, kline_fn=kline_fn, data_context=data_context)
    return {
        "opened": sum(p["status"] == "open" for p in added),
        "closed": len(closed),
        "skipped": len(picks) - len(added),
        "pending": sum(p["status"] == "pending" for p in added),
    }
