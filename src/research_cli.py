"""Local data/research CLI composition, keeping network synchronization explicit.

@author caiyanliangbj
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import time
from pathlib import Path

from daily_research import completed_date, research_daily, _atomic_json
from data_quality import assess_bars, capture_provenance
from data_snapshot import read_snapshot, sync_snapshot
from local_data import LocalData, iso_date


def add_arguments(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--daily",
        action="store_true",
        help="同步观察池→冻结数据→历史因子体检→研究台账（不改参数）",
    )
    group.add_argument(
        "--sync-data", action="store_true", help="只同步本地研究观察池行情"
    )
    group.add_argument(
        "--data-query", action="store_true", help="仅查询本地行情，输出 JSON，不联网"
    )
    group.add_argument("--verify-snapshot", metavar="PATH", help="校验冻结快照，不联网")
    group.add_argument(
        "--sync-snapshot", metavar="PATH", help="校验并镜像本地快照，不覆盖行情库"
    )
    parser.add_argument(
        "--offline", action="store_true", help="--daily 仅使用本地数据，不联网"
    )
    parser.add_argument(
        "--codes", help="研究/查询代码，逗号分隔；须同时指定单个 --market"
    )
    parser.add_argument("--start", help="本地查询起日 YYYY-MM-DD")
    parser.add_argument(
        "--end", help="本地查询/研究截止日 YYYY-MM-DD（每日研究自动排除盘中日线）"
    )
    parser.add_argument("--frequency", choices=["1d", "1w", "1M"], default="1d")
    parser.add_argument("--adjust", choices=["qfq", "raw", "nav"], default="qfq")
    parser.add_argument("--fields", help="本地查询投影字段，逗号分隔")
    parser.add_argument(
        "--bars", type=int, help="每标的保留最近 N 根（研究默认配置中的 bars）"
    )
    parser.add_argument(
        "--research-dir", help="研究输出/快照镜像目标目录，默认 state/research"
    )


def _universe(args: argparse.Namespace, cfg: dict) -> dict[str, list[str]]:
    if args.codes:
        if not args.market or "," in args.market:
            raise ValueError("--codes requires one --market")
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
        universe = {args.market: list(dict.fromkeys(codes))}
    else:
        universe = (cfg.get("local_research") or {}).get("universe", {})
        if args.market:
            markets = args.market.split(",")
            if any(m not in universe for m in markets):
                raise ValueError("requested market has no configured research universe")
            universe = {m: universe[m] for m in markets}
    if not universe or any(not codes for codes in universe.values()):
        raise ValueError("empty research/query universe")
    return universe


def synchronize(universe: dict[str, list[str]], bars: int, budget_sec: float) -> dict:
    """Refresh full qfq windows (historical prices can change), then verify writes.

    Existing fresh series are reused. The budget bounds admission of the next
    request; an in-flight fetch retains the existing adapter's timeout bounds.
    """
    import fetcher
    import store

    if bars < 1 or bars > 1000 or budget_sec <= 0:
        raise ValueError("sync requires 1..1000 bars and a positive time budget")
    if any(
        m not in {"cn", "etf"} or any(not re.fullmatch(r"\d{6}", c) for c in codes)
        for m, codes in universe.items()
    ):
        raise ValueError("synchronization supports six-digit CN/ETF symbols")
    if not store.enabled() or store.connect() is None:
        raise RuntimeError("SQLite storage is required for synchronization")
    started = time.monotonic()
    series = []
    for market, codes in universe.items():
        for code in codes:
            item = {"market": market, "code": code, "status": "failed", "written": 0}
            if store.kline_is_fresh(
                market, code, "qfq", min_rows=bars, ttl_hours=fetcher.KLINE_TTL_HOURS
            ):
                item["status"] = "reused"
            elif time.monotonic() - started >= budget_sec:
                item["reason"] = "sync_budget_exhausted"
            else:
                frame = fetcher.fetch_kline(code, market, limit=bars, use_db=False)
                if frame is None or frame.empty:
                    failure = frame.attrs.get("fetch_failure", {}) if frame is not None else {}
                    item["reason"] = failure.get("kind", "source_empty_or_unavailable")
                    if failure.get("reason"):
                        item["detail"] = failure["reason"]
                else:
                    # The adapter can return partial rows marked as rejected. Do not
                    # lose that evidence by persisting the surviving rows as healthy.
                    checked = assess_bars(frame, str(frame["date"].max())[:10], require_current=False)
                    try:
                        capture_provenance(frame, market, code, "qfq")
                    except ValueError:
                        checked["issues"].append("provenance_identity_mismatch")
                    if checked["issues"]:
                        item.update(reason="source_data_invalid", issues=checked["issues"])
                    else:
                        written = store.upsert_kline(
                            market,
                            code,
                            "qfq",
                            frame,
                            src=frame.attrs.get("source", "tencent_ifzq_qfq"),
                        )
                        item["written"] = written
                        if written == len(frame):
                            item["status"] = "updated"
                        else:
                            item["reason"] = "storage_write_failed_or_partial"
            series.append(item)
            print(
                f"  {market}:{code} {item['status']} rows={item['written']} "
                f"{item.get('reason', '')}",
                flush=True,
            )
            if item.get("detail") or item.get("issues"):
                print(f"    {item.get('detail') or ', '.join(item['issues'])}", flush=True)
    return {
        "series": series,
        "failed": sum(s["status"] == "failed" for s in series),
        "elapsed_sec": round(time.monotonic() - started, 2),
    }


def dispatch(
    args: argparse.Namespace, cfg: dict, params: dict, root: str
) -> int | None:
    """Return an exit code for research commands, None for the existing dual-track run."""
    active = any(
        (
            args.daily,
            args.sync_data,
            args.data_query,
            args.verify_snapshot,
            args.sync_snapshot,
        )
    )
    if not active:
        if (
            args.offline
            or args.codes
            or args.start
            or args.end
            or args.research_dir
            or args.bars is not None
        ):
            raise ValueError(
                "data/research options require --daily, --sync-data or --data-query"
            )
        return None
    rcfg = cfg.get("local_research") or {}
    output = Path(args.research_dir or rcfg.get("output_dir", "state/research"))
    if not output.is_absolute():
        output = Path(root) / output
    if args.verify_snapshot:
        frames = read_snapshot(args.verify_snapshot)
        print(
            json.dumps(
                {
                    "verified": True,
                    "series": len(frames),
                    "rows": sum(len(f) for f in frames.values()),
                }
            )
        )
        return 0
    if args.sync_snapshot:
        target = sync_snapshot(args.sync_snapshot, output / "snapshots")
        print(f"SNAPSHOT_PATH={target}")
        return 0
    universe = _universe(args, cfg)
    bars = args.bars if args.bars is not None else int(rcfg.get("bars", 300))
    import store

    if args.data_query:
        reader = LocalData(store.db_path())
        result = {}
        for market, codes in universe.items():
            frames = reader.get_data(
                market,
                codes,
                adjust=args.adjust,
                start=args.start,
                end=args.end,
                frequency=args.frequency,
                fields=args.fields.split(",") if args.fields else None,
                limit=bars,
            )
            for code, frame in frames.items():
                result[f"{market}:{code}"] = json.loads(
                    frame.to_json(orient="records", date_format="iso")
                )
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    if args.offline and args.sync_data:
        raise ValueError("--sync-data cannot be combined with --offline")
    if args.start or args.frequency != "1d" or args.adjust != "qfq" or args.fields:
        raise ValueError(
            "projection, frequency and adjustment options apply only to --data-query"
        )
    asof = iso_date(args.end or completed_date())
    if asof > completed_date():
        raise ValueError("daily cutoff must exclude today's unclosed/future bars")
    if any(m not in {"cn", "etf"} for m in universe):
        raise ValueError("daily research supports CN/ETF only")
    sync = None
    if not args.offline:
        sync = synchronize(universe, bars, float(rcfg.get("sync_budget_sec", 180)))
        _atomic_json(output / "last_sync.json", sync)
    if args.sync_data:
        return 2 if sync["failed"] else 0
    record = research_daily(
        store.db_path(),
        universe,
        params,
        output,
        asof=asof,
        bars=bars,
        max_age_days=int(rcfg.get("max_age_days", 7)),
        instrument_metadata=rcfg.get("instrument_metadata"),
    )
    summary = {
        k: record[k]
        for k in (
            "run_id",
            "data_date",
            "status",
            "change",
            "reused",
            "evidence_days",
            "promotion_allowed",
            "quality",
            "snapshot_path",
            "report_path",
            "daily_report_path",
            "daily_details_path",
        )
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"RESEARCH_REPORT={record['daily_report_path']}")
    print(f"RESEARCH_DETAILS={record['daily_details_path']}")
    bad = (
        record["status"] == "data_blocked"
        or any(
            record["quality"][k] for k in ("missing", "invalid", "stale", "regressed")
        )
        or (sync is not None and sync["failed"])
    )
    return 2 if bad else 0


def run_command(
    args: argparse.Namespace, cfg: dict, params: dict, root: str
) -> int | None:
    """CLI boundary: report concrete data/contract failures with a nonzero exit."""
    try:
        return dispatch(args, cfg, params, root)
    except (
        ValueError,
        KeyError,
        TypeError,
        OSError,
        RuntimeError,
        sqlite3.Error,
    ) as exc:
        print(f"本地研究失败: {type(exc).__name__}: {exc}", flush=True)
        return 2
