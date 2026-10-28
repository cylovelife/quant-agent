"""Explicit file boundary for freeze_data_contract.

@author cylovelife
"""

from quant_agent.domain.market.quality import build_data_contract


def freeze_data_contract(
    frames: dict,
    universe: dict[str, list[str]],
    asof: str,
    output: str,
    max_age_days: int = 7,
    reference_key: tuple[str, str, str] | None = None,
    universe_observation: dict | None = None,
    freshness_asof: str | None = None,
) -> dict:
    """Persist a locally observed version and its admission decision; no network or live-book writes."""
    from datetime import datetime, timezone
    from pathlib import Path

    from quant_agent.domain.market.serialization import canonical_json, digest
    from quant_agent.infrastructure.persistence.snapshots import (
        atomic_json,
        price_dataset_id,
        publish_snapshot,
    )

    contract = build_data_contract(
        frames,
        universe,
        asof,
        max_age_days,
        reference_key=reference_key,
        freshness_asof=freshness_asof,
    )
    contract["universe_observation"] = universe_observation or {"kind": "caller_configured"}
    structural = {
        "invalid_dates",
        "duplicate_dates",
        "unordered_dates",
        "provenance_identity_mismatch",
    }
    included = {
        key: frame
        for key, frame in frames.items()
        if frame is not None
        and not structural.intersection(contract["series"][f"{key[0]}:{key[1]}"]["issues"])
    }
    snapshot = publish_snapshot(included, Path(output) / "snapshots")
    contract.update(
        snapshot_path=str(snapshot),
        snapshot_id=snapshot.name,
        price_dataset_id=price_dataset_id(included),
        captured_at=datetime.now(timezone.utc).isoformat(),
        excluded_from_snapshot=[f"{key[0]}:{key[1]}" for key in frames if key not in included],
    )
    identity = digest(canonical_json(contract))
    contract["record_id"] = identity
    atomic_json(Path(output) / f"check_{identity}.json", contract)
    atomic_json(Path(output) / "latest.json", contract)
    return contract
