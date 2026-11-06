"""Small, deterministic research-only inputs; never read market caches.

@author cylovelife
"""

import hashlib
import json
from pathlib import Path


def make_fixture(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    policy = json.loads(
        (Path(__file__).resolve().parents[1] / "params/execution_v1.json").read_text()
    )
    instruments = [
        {"market": "cn", "code": code, "exchange": "SSE", "profile": "cn_mainboard"}
        for code in ("600001", "600002")
    ]
    artifacts = {}
    for phase, dates in (
        ("validation", ["2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06"]),
        ("test", ["2026-08-10", "2026-08-11", "2026-08-12", "2026-08-13"]),
    ):
        rows = []
        for index, day in enumerate(dates):
            for stock, instrument in enumerate(instruments):
                price = 10 + index * (1 if stock == 0 else -1) / 10
                rows.append(
                    {
                        "date": day,
                        "code": instrument["code"],
                        "adjust": "raw",
                        "open": f"{price:.2f}",
                        "close": f"{price + 0.05:.2f}",
                        "known_at": day + "@close",
                        "factors": {
                            "v_quality": 75 if stock == 0 else 25,
                            "v_valuation": 10 if stock == 0 else 95,
                            "v_growth": 50,
                            "v_cashflow": 50,
                            "v_balance": 50,
                        },
                        "facts": {
                            "date": day,
                            "source": "synthetic",
                            "known_at": day + "@open",
                            "trading_status": "trading",
                            "instrument_status": "regular",
                            "lower_limit": "5.00",
                            "upper_limit": "15.00",
                            "opening_capacity": 10000,
                            "capacity_kind": "assumed",
                            "previous_session": dates[max(index - 1, 0)],
                            "market": "cn",
                            "code": instrument["code"],
                        },
                    }
                )
        data = {
            "kind": "synthetic",
            "no_corporate_actions": True,
            "sessions": dates,
            "instruments": instruments,
            "rows": rows,
        }
        path = root / f"{phase}.json"
        path.write_text(json.dumps(data, sort_keys=True))
        artifacts[phase] = {
            "path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    manifest = {
        "format": "synthetic_long_weights_v1",
        **artifacts,
        "split": {
            "validation_start": "2026-08-03",
            "test_start": "2026-08-10",
            "test_end": "2026-08-13",
        },
        "candidates": ["default", "quality_first", "valuation_first"],
        "policy": policy,
        "initial_cash": "10000.00",
        "budget": {"max_candidates": 3, "max_attempts": 4, "max_attempt_seconds": 30},
    }
    path = root / "manifest.json"
    path.write_text(json.dumps(manifest, sort_keys=True))
    return path
