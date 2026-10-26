"""Persist original research conditions and audit them against subsequent sessions.

@author caiyanliangbj
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from data_snapshot import canonical_json, digest
from indicators import add_indicators

SIGNALS = {
    "price": "价格突破",
    "macd": "MACD转正",
    "trend": "动态MA60",
    "volume": "回踩量能",
}
STATUS_LABELS = {
    "pending": "待确认",
    "met": "已满足",
    "invalidated": "已失效",
    "unavailable": "暂不可判定",
}
HISTORY_COLUMNS = ["date", "open", "close", "high", "low", "volume", "amount"]


def ensure_tracking_schema(conn: sqlite3.Connection) -> None:
    """Additive DDL; the caller owns commit and the following IMMEDIATE transaction."""
    for sql in (
        """CREATE TABLE IF NOT EXISTS research_sessions (
            scope TEXT NOT NULL, data_date TEXT NOT NULL, run_id TEXT NOT NULL,
            payload TEXT NOT NULL, PRIMARY KEY(scope,data_date,run_id))""",
        """CREATE TABLE IF NOT EXISTS research_watches (
            watch_id TEXT PRIMARY KEY, scope TEXT NOT NULL, market TEXT NOT NULL,
            code TEXT NOT NULL, closed_date TEXT, payload TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS research_signal_checks (
            watch_id TEXT NOT NULL, data_date TEXT NOT NULL, run_id TEXT NOT NULL,
            payload TEXT NOT NULL, PRIMARY KEY(watch_id,data_date,run_id))""",
    ):
        conn.execute(sql)


def create_watch(record: dict, row: dict, frame: pd.DataFrame, scope: str) -> dict:
    """Freeze references and the original indicator warmup, without future rows."""
    history = frame.loc[frame["date"] <= row["date"], HISTORY_COLUMNS].copy()
    history["date"] = history["date"].dt.strftime("%Y-%m-%d")
    registered = datetime.fromisoformat(record["created_at"]).astimezone(
        ZoneInfo("Asia/Shanghai")
    )
    known_through = registered.date() - timedelta(days=int(registered.hour < 16))
    return {
        "watch_id": digest(
            canonical_json([scope, row["market"], row["code"], record["data_date"]])
        ),
        "format": 1,
        "market": row["market"],
        "code": row["code"],
        "name": row["name"],
        "origin_date": row["date"],
        "selected_on": record["data_date"],
        "origin_run_id": record["run_id"],
        "registered_at": record["created_at"],
        "eligible_after": max(row["date"], known_through.isoformat()),
        "high": row["high"],
        "low": row["low"],
        "parameters": record["parameters"],
        "history": history.to_dict("records"),
        "closed_date": None,
    }


def _new_signals() -> dict:
    return {
        key: {
            "status": "pending",
            "first_met_on": None,
            "invalidated_on": None,
            "checked_on": None,
            "evidence": "尚无登记后可核对的完整日线。",
        }
        for key in SIGNALS
    }


def _unavailable(watch: dict, day: str, note: str) -> dict:
    signals = json.loads(
        canonical_json(watch.get("evaluation", {}).get("signals") or _new_signals())
    )
    for value in signals.values():
        value.update(status="unavailable", evidence=note)
    return {"checked_on": day, "invalidated_on": None, "signals": signals, "note": note}


def _apply_signal(signals: dict, key: str, met: bool, day: str, evidence: str) -> None:
    current = signals[key]
    first = current["first_met_on"] or (day if met else None)
    invalidated = None
    if first and not met:
        invalidated = current["invalidated_on"] or day
    current.update(
        status="met" if met else ("invalidated" if first else "pending"),
        first_met_on=first,
        invalidated_on=invalidated,
        checked_on=day,
        evidence=evidence,
    )


def evaluate_watch(
    watch: dict,
    frame: pd.DataFrame | None,
    day: str,
    expected_sessions: list[str] | None = None,
) -> dict:
    """Replay only eligible closed sessions; support failure is terminal for this watch."""
    previous = watch.get("evaluation", {})
    if previous.get("invalidated_on"):
        return dict(
            previous,
            checked_on=day,
            note="原观察低点已失守，保留终止记录，不自动复活。",
        )
    if frame is None or frame.empty:
        return _unavailable(watch, day, "缺少该标的完整行情，暂不可判定。")
    current = frame.loc[frame["date"] <= day].copy()
    if current.empty or str(current["date"].max())[:10] != day:
        return _unavailable(watch, day, "该标的行情未推进到本次交易日，暂不可判定。")
    observed_dates = set(current["date"].dt.strftime("%Y-%m-%d"))
    missing = [
        dt
        for dt in (expected_sessions or [])
        if watch["eligible_after"] < dt <= day and dt not in observed_dates
    ]
    if missing:
        return _unavailable(
            watch,
            day,
            f"中间交易日缺行情（{', '.join(missing)}），不能跳过缺口判定确认或失效。",
        )
    history = pd.DataFrame(watch["history"])
    history["date"] = pd.to_datetime(history["date"])
    if not (current["date"] == watch["origin_date"]).any():
        return _unavailable(watch, day, "历史覆盖不足，无法核对原观察日复权口径。")
    overlap = history.merge(
        current[HISTORY_COLUMNS], on="date", suffixes=("_old", "_new")
    )
    for field in HISTORY_COLUMNS[1:]:
        if not np.allclose(
            overlap[field + "_old"], overlap[field + "_new"], rtol=1e-10, atol=1e-10
        ):
            return _unavailable(
                watch, day, "历史行情输入已修订，旧前复权条件暂不可比，须复核。"
            )
    combined = pd.concat(
        [history, current[current["date"] > watch["origin_date"]][HISTORY_COLUMNS]],
        ignore_index=True,
    )
    indicators = add_indicators(combined, watch["parameters"])
    signals = _new_signals()
    signals["volume"]["evidence"] = "尚无登记后的回踩日，等待适用行情。"
    invalidated_on = None
    for i, row in indicators.iterrows():
        dt = str(row["date"])[:10]
        if dt <= watch["eligible_after"]:
            continue
        if row["low"] < watch["low"]:
            invalidated_on = dt
            for value in signals.values():
                value.update(
                    status="invalidated",
                    invalidated_on=dt,
                    checked_on=dt,
                    evidence=f"最低价{row['low']:.3f}<原观察低点{watch['low']:.3f}，原研究前提失效。",
                )
            break
        _apply_signal(
            signals,
            "price",
            bool(row["close"] > watch["high"]),
            dt,
            f"收盘{row['close']:.3f}，原高点{watch['high']:.3f}；低点守住{watch['low']:.3f}。",
        )
        _apply_signal(
            signals,
            "macd",
            bool(row["macd_hist"] > 0),
            dt,
            f"MACD柱{row['macd_hist']:.6f}，要求>0。",
        )
        if pd.notna(row["ma60"]):
            _apply_signal(
                signals,
                "trend",
                bool(row["close"] > row["ma60"]),
                dt,
                f"收盘{row['close']:.3f}，当日MA60={row['ma60']:.3f}，要求收盘>MA60。",
            )
        else:
            signals["trend"].update(status="unavailable", evidence="MA60历史不足。")
        if i > 0 and row["close"] <= indicators.iloc[i - 1]["close"]:
            if np.isfinite(row["vr"]):
                _apply_signal(
                    signals,
                    "volume",
                    bool(0.4 <= row["vr"] <= 0.9),
                    dt,
                    f"回踩日量比{row['vr']:.3f}，要求[0.4,0.9]且原低点不失守。",
                )
            else:
                signals["volume"].update(
                    status="unavailable",
                    evidence="量比无法计算（历史不足或均量为零）。",
                )
    return {
        "checked_on": day,
        "invalidated_on": invalidated_on,
        "signals": signals,
        "note": "量能只核对回踩日，其余日沿用最近适用日状态；首次满足日期不随重跑增加。",
    }


def _ranks(record: dict) -> dict:
    return {
        (market, row["code"]): row
        for market, rows in record["research_summary"]["groups"].items()
        for row in rows
    }


def compare_priorities(
    record: dict, baseline: dict | None, expected_date: str | None
) -> dict:
    """Compare exactly the preceding observed market session, never silently bridge a gap."""
    if baseline is None or baseline["data_date"] != expected_date:
        return {
            "available": False,
            "previous_date": expected_date,
            "rows": [],
            "note": f"缺少上一交易日{expected_date or '（日期未知）'}的研究留档，无法比较；不回算虚构旧日排名。",
        }
    now, old = _ranks(record), _ranks(baseline)
    observations = {(r["market"], r["code"]): r for r in record["observations"]}
    prior_observations = {(r["market"], r["code"]): r for r in baseline["observations"]}
    unreliable_markets = {
        market for market, code in old if (market, code) not in observations
    }
    for source in (record, baseline):
        unreliable_markets.update(
            row["market"]
            for row in source["observations"]
            if row.get("date", source["data_date"]) != source["data_date"]
        )
        for kind in ("missing", "invalid", "stale"):
            unreliable_markets.update(
                value.split(":", 1)[0]
                for value in source.get("quality", {}).get(kind, [])
            )
    rows = []
    for key in sorted(now.keys() | old.keys()):
        current, prior = now.get(key), old.get(key)
        present = observations.get(key)
        if present is None or key[0] in unreliable_markets:
            status = "unavailable"
        elif prior is None:
            status = "entered"
        elif current is None:
            status = "exited"
        else:
            difference = prior["priority"] - current["priority"]
            status = (
                "up" if difference > 0 else ("down" if difference < 0 else "unchanged")
            )
        before = prior_observations.get(key, {}).get("score")
        after = present.get("score") if present else None
        rows.append(
            {
                "market": key[0],
                "code": key[1],
                "name": (present or prior)["name"],
                "status": status,
                "previous_priority": prior["priority"] if prior else None,
                "current_priority": current["priority"] if current else None,
                "previous_score": before,
                "current_score": after,
                "score_delta": after - before
                if after is not None and before is not None and status != "unavailable"
                else None,
            }
        )
    return {
        "available": True,
        "previous_date": expected_date,
        "rows": rows,
        "baseline_registered_at": baseline["created_at"],
        "method_changed": record.get("params_id") != baseline.get("params_id"),
        "note": "各市场分别比较优先清单；留档时间不等于行情交易日。",
    }


def track_research(
    conn: sqlite3.Connection, record: dict, frames: dict, scope: str
) -> dict:
    """Attach tracking within the caller's transaction; no file/network IO or commits."""
    day = record["data_date"]
    high_water = conn.execute(
        "SELECT MAX(data_date) FROM research_sessions WHERE scope=?", (scope,)
    ).fetchone()[0]
    if (
        not day
        or record["status"] == "data_blocked"
        or (high_water and day < high_water)
    ):
        return {
            "watches": [],
            "priority_changes": {
                "available": False,
                "previous_date": None,
                "rows": [],
                "note": "本次数据不可用或回退，不推进跟踪台账。",
            },
        }
    calendar = sorted(
        {
            str(dt)[:10]
            for frame in frames.values()
            for dt in frame["date"]
            if str(dt)[:10] < day
        }
    )
    previous_date = calendar[-1] if calendar else None
    baseline_row = conn.execute(
        "SELECT payload FROM research_sessions WHERE scope=? AND data_date=? ORDER BY rowid DESC LIMIT 1",
        (scope, previous_date),
    ).fetchone()
    changes = compare_priorities(
        record, json.loads(baseline_row[0]) if baseline_row else None, previous_date
    )
    active_rows = conn.execute(
        "SELECT payload FROM research_watches WHERE scope=? AND closed_date IS NULL",
        (scope,),
    ).fetchall()
    active = {}
    for (payload,) in active_rows:
        watch = json.loads(payload)
        active[(watch["market"], watch["code"])] = watch
    selected = _ranks(record)
    observations = {(r["market"], r["code"]): r for r in record["observations"]}
    for key in selected.keys() - active.keys():
        fresh = create_watch(record, observations[key], frames[key], scope)
        existing = conn.execute(
            "SELECT payload FROM research_watches WHERE watch_id=?",
            (fresh["watch_id"],),
        ).fetchone()
        active[key] = json.loads(existing[0]) if existing else fresh
    public = []
    for key, watch in sorted(active.items()):
        expected_sessions = sorted(
            {
                str(dt)[:10]
                for (market, _), frame in frames.items()
                if market == key[0]
                for dt in frame["date"]
            }
        )
        evaluation = evaluate_watch(watch, frames.get(key), day, expected_sessions)
        watch["evaluation"] = evaluation
        unknown = all(
            s["status"] == "unavailable" for s in evaluation["signals"].values()
        )
        if key not in selected and key in observations and not unknown:
            watch["closed_date"] = day
        conn.execute(
            "INSERT INTO research_watches (watch_id,scope,market,code,closed_date,payload) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(watch_id) DO UPDATE SET closed_date=excluded.closed_date,payload=excluded.payload",
            (
                watch["watch_id"],
                scope,
                watch["market"],
                watch["code"],
                watch["closed_date"],
                canonical_json(watch).decode(),
            ),
        )
        conn.execute(
            "INSERT OR IGNORE INTO research_signal_checks (watch_id,data_date,run_id,payload) VALUES (?,?,?,?)",
            (
                watch["watch_id"],
                day,
                record["run_id"],
                canonical_json(evaluation).decode(),
            ),
        )
        public.append(
            {
                k: watch[k]
                for k in (
                    "watch_id",
                    "market",
                    "code",
                    "name",
                    "origin_date",
                    "registered_at",
                    "eligible_after",
                    "closed_date",
                    "high",
                    "low",
                    "evaluation",
                )
            }
        )
        if key in selected:
            selected[key]["confirmation_origin_date"] = watch["origin_date"]
            selected[key]["confirmation_signals"][0] = (
                f"价格：守住原观察日{watch['origin_date']}低点{watch['low']:.3f}，后续收盘突破原高点{watch['high']:.3f}"
            )
            if evaluation["invalidated_on"]:
                selected[key]["risks"].insert(
                    0,
                    f"原观察低点已于{evaluation['invalidated_on']}失守，本轮研究前提失效。",
                )
                selected[key]["confirmation_signals"] = [
                    "本轮观察已失效，不自动重置；退出后重新入选才建立新观察轮次。"
                ]
    session = {
        k: record[k]
        for k in ("data_date", "created_at", "params_id", "research_summary")
    }
    session["observations"] = [
        {k: row[k] for k in ("market", "code", "name", "score", "date")}
        for row in record["observations"]
    ]
    session["quality"] = record["quality"]
    conn.execute(
        "INSERT OR IGNORE INTO research_sessions (scope,data_date,run_id,payload) VALUES (?,?,?,?)",
        (scope, day, record["run_id"], canonical_json(session).decode()),
    )
    return {"watches": public, "priority_changes": changes}
