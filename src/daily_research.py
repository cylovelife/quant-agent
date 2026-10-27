"""Frozen-data daily research and an idempotent evidence ledger, observation only.

@author caiyanliangbj
"""

from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from data_snapshot import (
    atomic_json as _atomic_json,
    Frames,
    canonical_json,
    digest,
    publish_snapshot,
    read_snapshot,
    price_dataset_id,
)
from data_quality import assess_bars, build_data_contract
from factor_eval import rolling_factor_ic
from local_data import LocalData, iso_date
from research_report import (
    build_observations,
    build_research_summary,
    render_details,
    render_report,
)
from research_tracking import ensure_tracking_schema, track_research

ROOT = Path(__file__).resolve().parents[1]
SHANGHAI = ZoneInfo("Asia/Shanghai")


def completed_date(now: datetime | None = None) -> str:
    """CN/ETF research waits until 16:00 Shanghai before using today's daily bar."""
    local = (now or datetime.now(SHANGHAI)).astimezone(SHANGHAI)
    return (local.date() - timedelta(days=int(local.hour < 16))).isoformat()


def code_signature() -> str:
    """Include the evaluator and its dependencies, not a potentially dirty Git HEAD."""
    hashes = {
        str(path.relative_to(ROOT)): digest(path.read_bytes())
        for path in sorted((ROOT / "src").rglob("*.py"))
    }
    hashes["run.py"] = digest((ROOT / "run.py").read_bytes())
    return digest(canonical_json(hashes))


def _quality(frames: Frames, asof: str, max_age_days: int) -> tuple[dict, dict]:
    quality = {
        "missing": [],
        "invalid": [],
        "stale": [],
        "lagging": [],
        "regressed": [],
        "last_dates": {},
    }
    valid = {}
    for (market, code, _adjust), bars in frames.items():
        key = f"{market}:{code}"
        if bars.empty:
            quality["missing"].append(key)
            continue
        checked = assess_bars(bars, asof, max_age_days, require_current=False)
        bad = any(issue != "stale" for issue in checked["issues"])
        last = bars["date"].max().date()
        quality["last_dates"][key] = last.isoformat()
        if bad:
            quality["invalid"].append(key)
        elif (datetime.fromisoformat(asof).date() - last).days > max_age_days:
            quality["stale"].append(key)
        else:
            valid[(market, code)] = bars
    # Relative lag is observable without inventing an exchange holiday calendar.
    # Treat it as a warning: suspensions also cause unequal series end dates.
    for market in {m for m, _ in valid}:
        latest = max(quality["last_dates"][f"{m}:{c}"] for m, c in valid if m == market)
        quality["lagging"].extend(
            f"{m}:{c}"
            for m, c in valid
            if m == market and quality["last_dates"][f"{m}:{c}"] < latest
        )
    return quality, valid


def research_daily(
    db_path: str | Path,
    universe: dict[str, list[str]],
    params: dict,
    output: str | Path,
    *,
    asof: str | None = None,
    bars: int = 300,
    max_age_days: int = 7,
    instrument_metadata: dict | None = None,
) -> dict:
    """Read a cutoff-bounded dataset, freeze it, evaluate the freeze, record evidence.

    Deliberately has no fetcher and no parameter writer. The daily CLI composes
    synchronization separately. Database and snapshot failures propagate.
    """
    asof = iso_date(asof or completed_date())
    instrument_metadata = instrument_metadata or {}
    if not universe or any(
        m not in {"cn", "etf"} or not codes for m, codes in universe.items()
    ):
        raise ValueError("daily research requires a nonempty CN/ETF universe")
    if bars < 1 or max_age_days < 0:
        raise ValueError("invalid bars or max_age_days")
    output = Path(output).resolve()
    reader = LocalData(db_path)
    frames = {}
    for market, codes in sorted(universe.items()):
        for code, data in reader.get_data(market, codes, end=asof, limit=bars).items():
            frames[(market, code, "qfq")] = data
    snapshot = publish_snapshot(frames, output / "snapshots")
    frozen = read_snapshot(snapshot)
    quality, valid = _quality(frozen, asof, max_age_days)
    dataset_id = price_dataset_id(frozen)
    data_contract = build_data_contract(frozen, universe, asof, max_age_days)
    data_contract.update(snapshot_id=snapshot.name, snapshot_path=str(snapshot), price_dataset_id=dataset_id)
    data_contract["universe_observation"] = {"kind": "caller_configured"}
    params_id = digest(canonical_json(params))
    code_id = code_signature()
    universe_id = digest(
        canonical_json({m: sorted(set(c)) for m, c in universe.items()})
    )
    runtime = {
        "python": sys.version.split()[0],
        "numpy": np.__version__,
        "pandas": pd.__version__,
    }
    # Include evaluator settings and quality exclusions: never reuse a different evaluation.
    run_id = digest(
        canonical_json(
            [
                snapshot.name,
                params_id,
                code_id,
                max_age_days,
                runtime,
                instrument_metadata,
                sorted(f"{m}:{c}" for m, c in valid),
            ]
        )
    )
    data_date = max((str(k["date"].max())[:10] for k in valid.values()), default=None)
    created = datetime.now(SHANGHAI).isoformat()
    report_path = output / f"research_{run_id}.md"
    details_path = output / f"research_{run_id}.details.md"
    with closing(sqlite3.connect(output / "ledger.db", timeout=30)) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS research_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT UNIQUE NOT NULL,
            universe_id TEXT NOT NULL, dataset_id TEXT NOT NULL,
            params_id TEXT NOT NULL, code_id TEXT NOT NULL, data_date TEXT,
            created_at TEXT NOT NULL, payload TEXT NOT NULL)""")
        ensure_tracking_schema(conn)
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT payload FROM research_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if existing:
            record = json.loads(existing[0])
            record["reused"] = True
            quality["regressed"] = record["quality"].get("regressed", [])
            status = record["status"]
        else:
            metrics = rolling_factor_ic(valid, params) if valid else None
            status = (
                ("observed" if metrics else "insufficient_evidence")
                if valid
                else "data_blocked"
            )
            previous = conn.execute(
                "SELECT dataset_id,params_id,code_id,data_date,payload FROM research_runs "
                "WHERE universe_id=? ORDER BY id DESC LIMIT 1",
                (universe_id,),
            ).fetchone()
            dates = {
                row[0]
                for row in conn.execute(
                    "SELECT data_date,payload FROM research_runs WHERE universe_id=? AND data_date IS NOT NULL",
                    (universe_id,),
                )
                if json.loads(row[1]).get("change") != "data_regression"
            }
            high_water = max(dates, default=None)
            if data_date and (high_water is None or data_date > high_water):
                dates.add(data_date)
            change = "initial_observation"
            if previous:
                if data_date and (high_water is None or data_date > high_water):
                    change = "new_trading_data"
                elif data_date and high_water and data_date < high_water:
                    change = "data_regression"
                    quality["regressed"] = [data_date]
                    status = "data_blocked"
                elif dataset_id != previous[0]:
                    change = "historical_revision" if json.loads(previous[4]).get("snapshot_id") else "data_contract_upgrade"
                elif snapshot.name != json.loads(previous[4]).get("snapshot_id"):
                    change = "provenance_update"
                else:
                    change = "evaluation_change"
            record = {
                "format": 6,
                "run_id": run_id,
                "dataset_id": dataset_id,
                "snapshot_id": snapshot.name,
                "data_contract": data_contract,
                "params_id": params_id,
                "parameters": params,
                "instrument_metadata": instrument_metadata,
                "universe": {m: sorted(set(c)) for m, c in universe.items()},
                "settings": {"bars": bars, "max_age_days": max_age_days},
                "runtime": runtime,
                "code_id": code_id,
                "created_at": created,
                "asof": asof,
                "data_date": data_date,
                "status": status,
                "change": change,
                "evidence_days": len(dates),
                "evaluation_kind": "historical_descriptive",
                "promotion_allowed": False,
                "reused": False,
                "quality": quality,
                "metrics": metrics,
                "observations": build_observations(valid, params, instrument_metadata),
                "snapshot_path": str(snapshot),
                "report_path": str(report_path),
                "details_path": str(details_path),
            }
            record["research_summary"] = build_research_summary(record)
            record["tracking"] = track_research(conn, record, valid, universe_id)
            conn.execute(
                "INSERT INTO research_runs "
                "(run_id,universe_id,dataset_id,params_id,code_id,data_date,created_at,payload) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    universe_id,
                    dataset_id,
                    params_id,
                    code_id,
                    data_date,
                    created,
                    canonical_json(record).decode("utf-8"),
                ),
            )
        conn.commit()
    # Regenerate missing reports on retry after an interrupted file write.
    report_path.write_text(render_report(record), encoding="utf-8")
    details_path.write_text(render_details(record), encoding="utf-8")
    day_report = output / f"research_{created[:10]}.md"
    day_details = output / f"research_{created[:10]}.details.md"
    latest = dict(
        record,
        checked_at=created,
        checked_asof=asof,
        quality=quality,
        data_contract=data_contract,
        status=status,
        daily_report_path=str(day_report),
        daily_details_path=str(day_details),
    )
    day_report.write_text(render_report(latest), encoding="utf-8")
    (output / "latest.md").write_text(render_report(latest), encoding="utf-8")
    day_details.write_text(render_details(latest), encoding="utf-8")
    (output / "latest.details.md").write_text(render_details(latest), encoding="utf-8")
    _atomic_json(output / "latest.json", latest)
    return latest
