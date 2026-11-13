"""Frozen economic screening for synthetic accounts, not statistical confirmation.

@author cylovelife
"""

from dataclasses import asdict, dataclass
from decimal import Decimal


@dataclass(frozen=True)
class ScientificPolicy:
    version: str
    min_sessions: int
    min_filled_orders: int

    @classmethod
    def parse(cls, value: object) -> "ScientificPolicy":
        if not isinstance(value, dict) or set(value) != {
            "version",
            "min_sessions",
            "min_filled_orders",
        }:
            raise ValueError("explicit scientific policy fields required")
        if value["version"] != "synthetic_economic_v1":
            raise ValueError("unsupported scientific policy version")
        for name, minimum in (("min_sessions", 2), ("min_filled_orders", 1)):
            number = value[name]
            if isinstance(number, bool) or not isinstance(number, int) or number < minimum:
                raise ValueError(f"scientific policy {name} must be an integer >= {minimum}")
        return cls(**value)

    def definition(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class CandidateEvidence:
    candidate_id: str
    scheme: str
    sessions: int
    filled_orders: int
    net_assets: Decimal


def judge_candidate(
    evidence: CandidateEvidence,
    *,
    initial_cash: Decimal,
    default_net_assets: Decimal,
    policy: ScientificPolicy,
) -> dict:
    """Coverage first, then positive net profit and noninferiority to frozen default.

    Noninferiority here is an arithmetic comparison in a synthetic scenario, not
    a statistical noninferiority test. Session/order counts are coverage only.
    """
    if not all(x.is_finite() for x in (evidence.net_assets, initial_cash, default_net_assets)):
        raise ValueError("finite account evidence required for scientific decision")
    if initial_cash <= 0:
        raise ValueError("positive initial cash required")
    missing = []
    if evidence.sessions < policy.min_sessions:
        missing.append("insufficient_sessions")
    if evidence.filled_orders < policy.min_filled_orders:
        missing.append("insufficient_filled_orders")
    net_profit = evidence.net_assets - initial_cash
    excess = evidence.net_assets - default_net_assets
    rejected = []
    if net_profit <= 0:
        rejected.append("nonpositive_net_profit")
    if excess < 0:
        rejected.append("below_default_net_assets")
    verdict = (
        "insufficient_evidence"
        if missing
        else "rejected"
        if rejected
        else "eligible_for_locked_test"
    )
    return {
        "candidate_id": evidence.candidate_id,
        "scheme": evidence.scheme,
        "verdict": verdict,
        "reasons": missing or rejected,
        "metrics": {
            "sessions": evidence.sessions,
            "filled_orders": evidence.filled_orders,
            "net_profit": str(net_profit),
            "excess_net_assets_vs_default": str(excess),
        },
    }
