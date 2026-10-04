# -*- coding: utf-8 -*-
"""虚拟持仓账本：把每日推荐登记为持仓，逐日结算，供离场模型决策。

口径（与 backtest 保持一致，便于离线复现）：
- **建仓**：信号日收盘后登记，`entry_price` = 信号日收盘价
  （实际成交在次日开盘，此处不建模滑点与手数，属研究口径）；
- **结算**：每个交易日用批量行情刷新最新价，`ret = last_price / entry_price - 1`；
- **平仓**：触发止损(-5%)、止盈(+8%)、或持有满 hold_days 个交易日 → 落盘 `state/trades.csv`；
- **持有天数**：按**交易日历**计（取 K 线日期集合），避免日历天在周末/假期上的误差；
- 同一标的在已有未平仓持仓时不重复建仓（避免金字塔式叠加）。

账本落盘：`state/positions.json`（全量，含已平仓记录）、`state/trades.csv`（平仓流水）。
"""
import csv
import json
import os
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(ROOT, "state")
BOOK_PATH = os.path.join(STATE, "positions.json")
TRADES_PATH = os.path.join(STATE, "trades.csv")

TRADE_COLS = ["exit_date", "code", "name", "market", "entry_date", "entry_price",
              "exit_price", "exit_ret", "days_held", "exit_reason", "entry_score"]


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------
def load_book(path: "str | None" = None) -> dict:
    path = path or BOOK_PATH
    if not os.path.exists(path):
        return {"created": datetime.now().isoformat(), "positions": []}
    try:
        with open(path, encoding="utf-8") as f:
            book = json.load(f)
        book.setdefault("positions", [])
        return book
    except Exception:
        return {"created": datetime.now().isoformat(), "positions": []}


def save_book(book: dict, path: "str | None" = None) -> None:
    path = path or BOOK_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    book["updated"] = datetime.now().isoformat()
    with open(path, "w", encoding="utf-8") as f:
        json.dump(book, f, ensure_ascii=False, indent=2)


def _append_trades(rows: list, path: "str | None" = None) -> None:
    path = path or TRADES_PATH
    if not rows:
        return
    exists = os.path.exists(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=TRADE_COLS, extrasaction="ignore")
        if not exists:
            w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in TRADE_COLS})


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
def settle_book(book: dict, quotes: dict, cal: dict, params: dict,
                today: str) -> list:
    """刷新所有未平仓持仓的最新价与持有天数，触发的直接平仓。返回本次平仓列表。"""
    bt = params.get("backtest", {})
    horizon = int(bt.get("hold_days", 5))
    stop = float(bt.get("stop_loss", -0.05))
    take = float(bt.get("take_profit", 0.08))
    closed = []
    for p in book.get("positions", []):
        if p.get("status") != "open":
            continue
        q = quotes.get((p.get("market"), str(p.get("code"))))
        if q and q.get("price"):
            p["last_price"] = float(q["price"])
            p["last_pct"] = float(q.get("pct") or 0.0)
            p["last_date"] = q.get("date") or today
        ep, lp = p.get("entry_price"), p.get("last_price")
        if ep and lp:
            try:
                p["ret"] = round(float(lp) / float(ep) - 1, 4)
            except (TypeError, ZeroDivisionError):
                pass
        p["days_held"] = trading_days_between(cal, p.get("market"), p["entry_date"], today)
        p["peak_ret"] = round(max(float(p.get("peak_ret") or 0.0), float(p.get("ret") or 0.0)), 4)

        r = p.get("ret")
        reason = None
        if r is not None and r <= stop:
            reason = "止损"
        elif r is not None and r >= take:
            reason = "止盈"
        elif p["days_held"] >= horizon:
            reason = "到期"
        if reason:
            p["status"] = "closed"
            p["exit_date"] = today
            p["exit_price"] = lp
            p["exit_ret"] = r
            p["exit_reason"] = reason
            closed.append(p)
    _append_trades(closed)
    return closed


def open_positions(book: dict) -> list:
    return [p for p in book.get("positions", []) if p.get("status") == "open"]


def latest_picks_file(before: "str | None" = None, state_dir: str = STATE) -> "str | None":
    """最近一份历史推荐文件（可限定早于某日）。"""
    import glob
    files = sorted(glob.glob(os.path.join(state_dir, "picks_*.json")))
    if before:
        files = [f for f in files
                 if os.path.basename(f).split("picks_")[-1][:10] < before]
    return files[-1] if files else None


# ---------------------------------------------------------------------------
# 建仓
# ---------------------------------------------------------------------------
def add_positions(book: dict, picks_by_market: dict, params: dict,
                  today: str, max_per_market: int = 5) -> list:
    """把当日推荐登记为持仓（同标的已有未平仓则跳过）。

    幂等性：每市场上限按「**当日已建仓数** + 本次新增」计，而不是只算本次新增。
    否则同一天重复运行（手动补跑、自动化重试）时，已被持有的高排名标的会
    让出名额给排名更靠后的候选，导致账本被静默加仓。
    """
    held = {(p.get("market"), str(p.get("code")))
            for p in book.get("positions", []) if p.get("status") == "open"}
    cnt = {}
    for p in book.get("positions", []):
        if p.get("entry_date") == today:
            cnt[p.get("market")] = cnt.get(p.get("market"), 0) + 1
    added = []
    for mkt, lst in (picks_by_market or {}).items():
        for pk in lst:
            if cnt.get(mkt, 0) >= max_per_market:
                break
            key = (mkt, str(pk.get("code")))
            if key in held:
                continue
            pos = {
                "id": f"{mkt}-{pk.get('code')}-{today}",
                "code": str(pk.get("code")), "name": pk.get("name"), "market": mkt,
                "mkt_id": pk.get("mkt_id"),
                "entry_date": today,
                "entry_price": float(pk.get("entry_price") or pk.get("price") or 0.0),
                "entry_score": float(pk["score"]) if pk.get("score") is not None else None,
                "status": "open", "days_held": 0, "ret": 0.0,
                "last_price": float(pk.get("entry_price") or pk.get("price") or 0.0),
                "last_date": today,
            }
            for f in ("f_boll", "f_rsi", "f_volume", "f_trend", "f_macd", "f_drawdown"):
                if pk.get(f) is not None:
                    pos[f] = pk.get(f)
            book.setdefault("positions", []).append(pos)
            added.append(pos)
            cnt[mkt] = cnt.get(mkt, 0) + 1
    return added


# ---------------------------------------------------------------------------
# 首次回填：把最近一次历史推荐补齐（含已结束交易），让账本立刻可用
# ---------------------------------------------------------------------------
def backfill_from_picks(book: dict, picks_path: str, kline_fn, params: dict,
                        today: str, cal: dict, max_per_market: "int | None" = None) -> dict:
    """用最近一份历史推荐 + K 线，重建该批标的的持仓轨迹。

    - 已触发止损/止盈/到期的，直接落盘为已平仓交易；
    - 仍在持有窗口内的，登记为 open 持仓；
    - `max_per_market` 不为 None 时，按评分降序每市场只取前 N 只，
      与 `add_positions` 的持仓上限保持一致（否则首日账本会超出上限）。
    返回统计 {"opened": n, "closed": n, "skipped": n}。
    """
    with open(picks_path, encoding="utf-8") as f:
        rec = json.load(f)
    entry_date = rec.get("date")
    picks = list(rec.get("picks", []))
    if max_per_market is not None:
        by_mkt = {}
        for pk in picks:
            by_mkt.setdefault(pk.get("market"), []).append(pk)
        picks = []
        for mkt, lst in by_mkt.items():
            lst.sort(key=lambda x: float(x.get("score") or 0.0), reverse=True)
            picks.extend(lst[:max_per_market])
    bt = params.get("backtest", {})
    horizon = int(bt.get("hold_days", 5))
    stop = float(bt.get("stop_loss", -0.05))
    take = float(bt.get("take_profit", 0.08))

    n_open = n_closed = n_skip = 0
    for pk in picks:
        code, market = str(pk["code"]), pk["market"]
        entry = pk.get("entry_price")
        if not entry:
            n_skip += 1
            continue
        try:
            k = kline_fn(code, market, pk.get("mkt_id"))
        except Exception:
            k = None
        if k is None or len(k) == 0:
            n_skip += 1
            continue
        kk = k.copy()
        kk["d"] = kk["date"].astype(str).str.slice(0, 10)
        seg = kk[(kk["d"] > entry_date) & (kk["d"] <= today)].reset_index(drop=True)
        if seg.empty:
            n_skip += 1
            continue

        base = {
            "id": f"{market}-{code}-{entry_date}", "code": code, "name": pk.get("name"),
            "market": market, "mkt_id": pk.get("mkt_id"), "entry_date": entry_date,
            "entry_price": float(entry),
            "entry_score": float(pk["score"]) if pk.get("score") is not None else None,
        }
        for f in ("f_boll", "f_rsi", "f_volume", "f_trend", "f_macd", "f_drawdown"):
            if pk.get(f) is not None:
                base[f] = pk.get(f)

        exit_row = None
        for i, r in seg.iterrows():
            c = float(r["close"])
            ret = c / float(entry) - 1
            d = int(i) + 1
            reason = ("止损" if ret <= stop else "止盈" if ret >= take
                      else "到期" if d >= horizon else None)
            if reason:
                exit_row = {**base, "exit_date": r["d"], "exit_price": c,
                            "exit_ret": round(ret, 4), "days_held": d,
                            "exit_reason": reason, "status": "closed"}
                break
        if exit_row:
            book.setdefault("positions", []).append(exit_row)
            _append_trades([exit_row])
            n_closed += 1
        else:
            last = seg.iloc[-1]
            lp = float(last["close"])
            book.setdefault("positions", []).append({
                **base, "status": "open",
                "days_held": trading_days_between(cal, market, entry_date, today),
                "ret": round(lp / float(entry) - 1, 4),
                "last_price": lp, "last_date": last["d"],
                "peak_ret": round(lp / float(entry) - 1, 4),
            })
            n_open += 1
    return {"opened": n_open, "closed": n_closed, "skipped": n_skip}
