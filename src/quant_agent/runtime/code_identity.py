"""Fingerprint the actual package and launcher without masking code drift.

@author cylovelife
"""

from quant_agent.domain.market.serialization import canonical_json, digest
from quant_agent.runtime.paths import PACKAGE_ROOT, SOURCE_ROOT


def code_fingerprints() -> dict[str, str]:
    files = sorted(PACKAGE_ROOT.rglob("*.py"))
    if not files:
        raise RuntimeError("runtime package source is missing")
    hashes = {
        "src/quant_agent/" + p.relative_to(PACKAGE_ROOT).as_posix(): digest(p.read_bytes())
        for p in files
    }
    for name in ("run.py", "pyproject.toml"):
        path = SOURCE_ROOT / name
        if path.is_file():
            hashes[name] = digest(path.read_bytes())
    return hashes


def code_signature() -> str:
    return digest(canonical_json(code_fingerprints()))
