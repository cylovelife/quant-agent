"""Explicit file boundary for load_policy.

@author cylovelife
"""

import json
from pathlib import Path

from quant_agent.domain.trading.execution import ExecutionPolicy, policy_from_definition


def load_policy(path: str | Path) -> ExecutionPolicy:
    """Load explicit, fingerprinted assumptions; never infer an account tariff."""
    return policy_from_definition(json.loads(Path(path).read_text(encoding="utf-8")))
