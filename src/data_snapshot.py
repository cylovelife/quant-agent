"""Content-addressed, integrity-checked local research snapshots.

@author caiyanliangbj
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
from pathlib import Path

import pandas as pd

from local_data import BAR_FIELDS

Frames = dict[tuple[str, str, str], pd.DataFrame]
FORMAT = 1


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _payload(frames: Frames) -> dict:
    series = []
    for (market, code, adjust), frame in sorted(frames.items()):
        bars = frame.reindex(columns=BAR_FIELDS).copy()
        if not bars.empty:
            bars["date"] = pd.to_datetime(bars["date"]).dt.strftime("%Y-%m-%d")
            bars = bars.sort_values("date").reset_index(drop=True)
        # Preserve Python float round-trip precision: to_json's decimal rounding
        # could otherwise hide a historical revision from the content hash.
        records = [
            {
                field: (
                    None
                    if pd.isna(value)
                    or (isinstance(value, float) and not math.isfinite(value))
                    else value
                )
                for field, value in row.items()
            }
            for row in bars.to_dict("records")
        ]
        series.append(
            {"market": market, "code": code, "adjust": adjust, "bars": records}
        )
    return {"format": FORMAT, "series": series}


def _publish(data: bytes, root: Path) -> Path:
    snapshot_id = digest(data)
    root.mkdir(parents=True, exist_ok=True)
    target = root / snapshot_id
    if target.exists():
        read_snapshot(target)
        return target
    staging = Path(tempfile.mkdtemp(prefix=".snapshot-part-", dir=root))
    try:
        manifest = {
            "format": FORMAT,
            "snapshot_id": snapshot_id,
            "files": [{"path": "bars.json", "size": len(data), "sha256": snapshot_id}],
        }
        (staging / "bars.json").write_bytes(data)
        (staging / "manifest.json").write_bytes(canonical_json(manifest))
        # Verify before making any snapshot visible.
        read_snapshot(staging, expected_id=snapshot_id)
        try:
            os.rename(staging, target)
        except OSError:
            if not target.exists():
                raise
            read_snapshot(target)
        return target
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def publish_snapshot(frames: Frames, root: str | Path) -> Path:
    """Same content reuses a snapshot; historical revisions create a new one."""
    return _publish(canonical_json(_payload(frames)), Path(root))


def read_snapshot(path: str | Path, *, expected_id: str | None = None) -> Frames:
    """Reject damaged/unknown formats and manifest paths before reading data."""
    path = Path(path)
    manifest = json.loads((path / "manifest.json").read_bytes())
    entries = manifest.get("files")
    if (
        manifest.get("format") != FORMAT
        or not isinstance(entries, list)
        or len(entries) != 1
    ):
        raise ValueError("unsupported snapshot manifest")
    entry = entries[0]
    if entry.get("path") != "bars.json" or (path / "bars.json").is_symlink():
        raise ValueError("unsafe snapshot path")
    data = (path / "bars.json").read_bytes()
    snapshot_id = digest(data)
    if (
        entry.get("sha256") != snapshot_id
        or entry.get("size") != len(data)
        or manifest.get("snapshot_id") != snapshot_id
        or snapshot_id != (expected_id or path.name)
    ):
        raise ValueError("snapshot integrity check failed")
    payload = json.loads(data)
    if payload.get("format") != FORMAT or not isinstance(payload.get("series"), list):
        raise ValueError("unsupported snapshot payload")
    frames = {}
    for series in payload["series"]:
        key = (series["market"], series["code"], series["adjust"])
        if key in frames:
            raise ValueError("duplicate snapshot series")
        frame = pd.DataFrame(series["bars"], columns=BAR_FIELDS)
        frame["date"] = pd.to_datetime(frame["date"], errors="raise")
        if (
            frame["date"].duplicated().any()
            or not frame["date"].is_monotonic_increasing
        ):
            raise ValueError("snapshot dates must be sorted and unique")
        frames[key] = frame
    return frames


def sync_snapshot(source: str | Path, destination: str | Path) -> Path:
    """Local directory mirror, verified before publish; no overwrite of live DBs."""
    source = Path(source)
    read_snapshot(source)
    data = (source / "bars.json").read_bytes()
    if digest(data) != source.name:
        raise ValueError("snapshot integrity changed during synchronization")
    return _publish(data, Path(destination))
