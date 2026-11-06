"""One finite synthetic research entry: validate, lock, then single-use test.

@author cylovelife
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from quant_agent.application.research.experiments import (
    _CONTROLLED_REGISTRATION,
    CONTROLLED_FORMAT,
    ExperimentRegistry,
)
from quant_agent.application.research.loop_data import check_phase, read_artifact, snapshot_frames
from quant_agent.application.research.model_selection import FACTORS_CORE, LONG_SCHEMES
from quant_agent.domain.market.serialization import canonical_json, digest
from quant_agent.domain.research.validation import configured_boundaries
from quant_agent.domain.trading.execution import policy_from_definition
from quant_agent.infrastructure.persistence.snapshots import publish_snapshot
from quant_agent.runtime.paths import SOURCE_ROOT

FORMAT = CONTROLLED_FORMAT


def _check_protocol(manifest: dict) -> None:
    """Validate the frozen contract without opening either phase artifact."""
    if set(manifest) != {
        "format",
        "validation",
        "test",
        "split",
        "candidates",
        "policy",
        "initial_cash",
        "budget",
    }:
        raise ValueError("unknown or missing research manifest fields")
    if (
        manifest["format"] != FORMAT
        or configured_boundaries({"temporal_validation": manifest["split"]}) is None
    ):
        raise ValueError("synthetic protocol with completed frozen dates required")
    names = manifest["candidates"]
    if (
        not isinstance(names, list)
        or not names
        or len(set(names)) != len(names)
        or "default" not in names
    ):
        raise ValueError("unique finite candidates including default required")
    if any(name not in LONG_SCHEMES for name in names):
        raise ValueError("only built-in long weight schemes supported")
    cash = Decimal(manifest["initial_cash"])
    if not cash.is_finite() or not 0 < cash <= Decimal("1e12") or cash % Decimal("0.01"):
        raise ValueError("positive cent-aligned initial cash required")
    policy = policy_from_definition(manifest["policy"])
    if policy.effective_from > manifest["split"]["validation_start"]:
        raise ValueError("policy must be effective before validation")
    for phase in ("validation", "test"):
        artifact = manifest[phase]
        if set(artifact) != {"path", "sha256"} or not re.fullmatch(
            r"[0-9a-f]{64}", artifact["sha256"]
        ):
            raise ValueError("explicit artifact path and SHA-256 required")
        if not isinstance(artifact["path"], str) or not artifact["path"]:
            raise ValueError("explicit phase path required")
    if manifest["validation"]["path"] == manifest["test"]["path"]:
        raise ValueError("validation and test artifacts must be separate")
    budget = manifest["budget"]
    if set(budget) != {"max_candidates", "max_attempts", "max_attempt_seconds"} or any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0
        for value in budget.values()
    ):
        raise ValueError("positive integer budget required")
    if budget["max_candidates"] != len(names) or budget["max_attempts"] != len(names) + 1:
        raise ValueError("budget must cover exactly the declared candidates and one test")


def _spec(manifest: dict, instruments: list[dict]) -> dict:
    policy = policy_from_definition(manifest["policy"])
    return {
        "universe": sorted([[item["market"], item["code"], "raw"] for item in instruments]),
        "parameters": {
            "protocol": manifest,
            "definitions": [
                {"scheme": name, "weights": LONG_SCHEMES[name]} for name in manifest["candidates"]
            ],
            "instruments": instruments,
            "selection": "max_validation_net_assets_then_declared_order",
            "top_k": 1,
            "quantity": 100,
        },
        "features": FACTORS_CORE,
        "label": {
            "name": "synthetic_net_assets",
            "decision": "previous_close_to_next_open",
            "fitted_model": False,
        },
        "cost": {"policy_id": policy.policy_id, "policy": manifest["policy"]},
        "split": manifest["split"],
        "seed": 0,
        "budget": manifest["budget"],
    }


def register(registry_path: Path, manifest_path: Path) -> dict:
    """Freeze finite candidates and an opaque test digest without reading the test."""
    manifest_path = Path(manifest_path).resolve()
    manifest = json.loads(manifest_path.read_text())
    _check_protocol(manifest)
    for phase in ("validation", "test"):
        artifact = manifest[phase]
        artifact["path"] = str((manifest_path.parent / artifact["path"]).resolve())
    _check_protocol(manifest)
    data = read_artifact(manifest["validation"])
    check_phase(data, "validation", manifest["split"])
    frames = snapshot_frames(data)
    registry_path = Path(registry_path).resolve()
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot = publish_snapshot(frames, registry_path.parent / "research-loop-snapshots")
    spec = _spec(manifest, data["instruments"])
    definitions = spec["parameters"]["definitions"]
    # Publish a complete new registry atomically; retries reuse the immutable one.
    if not registry_path.exists():
        with TemporaryDirectory(
            prefix=".loop-registration-", dir=registry_path.parent
        ) as directory:
            staged = ExperimentRegistry.create(Path(directory) / "registry.db")
            result = staged.register(snapshot, spec, _registration_token=_CONTROLLED_REGISTRATION)
            for definition in definitions:
                staged.add_candidate(result["experiment_id"], definition)
            try:
                os.link(staged.path, registry_path)
            except FileExistsError:
                pass
    registry = ExperimentRegistry(registry_path)
    result = registry.register(snapshot, spec, _registration_token=_CONTROLLED_REGISTRATION)
    for definition in definitions:
        registry.add_candidate(result["experiment_id"], definition)
    return result


class ResearchLoop:
    """Only built-in, frozen candidate execution; no code, account or data adapter hooks."""

    def __init__(self, registry_path: Path):
        self.registry = ExperimentRegistry(registry_path)

    @staticmethod
    def attempt_id(eid: str, cid: str, phase: str) -> str:
        return digest(canonical_json([FORMAT, eid, cid, phase]))

    def _context(self, eid: str) -> tuple[dict, dict, list[dict]]:
        self.registry.require_controlled_registration(eid)
        state = self.registry.inspect(eid)
        parameters = state["identity"]["parameters"]
        protocol = parameters.get("protocol", {})
        if protocol.get("format") != FORMAT:
            raise ValueError("not a synthetic controlled research registration")
        _check_protocol(protocol)
        expected_spec = _spec(protocol, parameters["instruments"])
        if any(state["identity"][key] != value for key, value in expected_spec.items()):
            raise ValueError("frozen identity differs from controlled protocol")
        if any(not Path(protocol[phase]["path"]).is_absolute() for phase in ("validation", "test")):
            raise ValueError("frozen phase paths must be absolute")
        definitions = parameters["definitions"]
        expected = {digest(canonical_json(item)): item for item in definitions}
        actual = {
            item["candidate_id"]: json.loads(item["definition_json"])
            for item in state["candidates"]
        }
        if actual != expected:
            raise ValueError("candidate universe differs from frozen protocol")
        return state, protocol, definitions

    def _evaluate(self, eid: str, phase: str, definitions: list[dict]) -> dict:
        _, protocol, _ = self._context(eid)
        started = time.monotonic()
        parameters = self.registry.inspect(eid)["identity"]["parameters"]
        payload = {
            "artifact": protocol[phase],
            "phase": phase,
            "split": protocol["split"],
            "instruments": parameters["instruments"],
            "candidates": definitions,
            "policy": protocol["policy"],
            "initial_cash": protocol["initial_cash"],
        }
        timeout = protocol["budget"]["max_attempt_seconds"] - (time.monotonic() - started)
        if timeout <= 0:
            raise TimeoutError("phase preparation exceeded budget")
        # Explicit source path and empty workspace: no existing runtime state/config.
        with TemporaryDirectory(prefix="synthetic-research-worker-") as directory:
            env = {
                key: value
                for key, value in os.environ.items()
                if key in {"PATH", "SYSTEMROOT", "LANG"}
            }
            env.update(
                PYTHONPATH=str(SOURCE_ROOT / "src"),
                QUANT_AGENT_HOME=directory,
                TMPDIR=directory,
                TMP=directory,
                TEMP=directory,
                OPENBLAS_NUM_THREADS="1",
                OMP_NUM_THREADS="1",
            )
            completed = subprocess.run(
                [sys.executable, "-m", "quant_agent.entrypoints.research_loop", "--worker"],
                input=canonical_json(payload).decode(),
                text=True,
                capture_output=True,
                timeout=timeout,
                check=False,
                cwd=directory,
                env=env,
            )
        result = json.loads(completed.stdout)
        if completed.returncode:
            raise ValueError("phase worker failed: " + result.get("error", "unknown error"))
        return {
            **result,
            "protocol": FORMAT,
            "artifact_sha256": protocol[phase]["sha256"],
            "phase": phase,
            "promotion_allowed": False,
            "independent_new_samples": 0,
        }

    def validate(self, eid: str) -> dict:
        state, _, definitions = self._context(eid)
        if state["locked_candidate"] is not None:
            raise ValueError("validation search is closed after lock")
        results = []
        for definition in definitions:
            cid = digest(canonical_json(definition))
            result = self.registry.run_attempt(
                eid,
                cid,
                self.attempt_id(eid, cid, "validation"),
                "validation",
                lambda definition=definition: self._evaluate(eid, "validation", [definition]),
            )
            results.append(result)
            if result["outcome"] != "succeeded":
                raise ValueError("previous validation failed; no automatic retry")
            self.registry.managed_result(
                eid, cid, self.attempt_id(eid, cid, "validation"), "validation"
            )
        return {"validation": results, "promotion_allowed": False}

    def _winner(self, eid: str) -> tuple[str, dict]:
        _, protocol, definitions = self._context(eid)
        scored = []
        for definition in definitions:
            cid = digest(canonical_json(definition))
            result = self.registry.managed_result(
                eid, cid, self.attempt_id(eid, cid, "validation"), "validation"
            )
            if (result.get("protocol"), result.get("phase"), result.get("artifact_sha256")) != (
                FORMAT,
                "validation",
                protocol["validation"]["sha256"],
            ) or result.get("candidates") != [definition]:
                raise ValueError("managed validation differs from frozen intent")
            score = Decimal(result["accounts"][0]["net_assets"])
            if not score.is_finite():
                raise ValueError("nonfinite validation score")
            scored.append((score, cid, definition))
        _, cid, definition = max(scored, key=lambda item: item[0])
        return cid, definition

    def lock(self, eid: str) -> dict:
        cid, definition = self._winner(eid)
        return {
            **self.registry.lock_candidate(eid, cid),
            "scheme": definition["scheme"],
            "promotion_allowed": False,
        }

    def test(self, eid: str) -> dict:
        state, protocol, definitions = self._context(eid)
        if state["locked_candidate"] is None:
            raise ValueError("test requires candidate lock")
        cid, definition = self._winner(eid)
        if cid != state["locked_candidate"]:
            raise ValueError("lock does not match managed validation winner")
        baseline = next(item for item in definitions if item["scheme"] == "default")

        def operation() -> dict:
            candidates = [definition] if definition == baseline else [definition, baseline]
            result = self._evaluate(eid, "test", candidates)
            accounts = result.pop("accounts")
            return {
                **result,
                "selected": accounts[0],
                "default_baseline": accounts[-1],
                "cash_baseline": {
                    "net_assets": protocol["initial_cash"],
                    "simulated_account_return": "0",
                },
            }

        attempt_id = self.attempt_id(eid, cid, "test")
        result = self.registry.run_attempt(eid, cid, attempt_id, "test", operation)
        if result["outcome"] == "succeeded":
            # Admission/runtime checks also consume wall time. A worker that
            # finished before its timeout must still pass the full attempt gate,
            # including when an over-budget result is replayed later.
            self.registry.managed_result(eid, cid, attempt_id, "test")
        return result
