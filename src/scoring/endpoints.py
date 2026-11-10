"""Stage 1 aliased 端点的证据与精度规则（T14）。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

MIN_ALIAS_PULLS = 2
MIN_ALIAS_RESPONSES = 2
MIN_ALIAS_CLAIM_PRECISION = 0.5


@dataclass(frozen=True, slots=True)
class AliasedQualification:
    caught: int
    false_positives: int
    unsubstantiated: int
    precision: float | None
    admissible: bool


def qualify_aliased(
    *,
    truth_aliased: Mapping[str, bool],
    final_claims: Mapping[str, bool],
    pulls: Mapping[str, int],
    responses: Mapping[str, int],
) -> AliasedQualification:
    """按预注册规则把最终声明折成 aliased 端点。"""

    caught = 0
    false_positives = 0
    unsubstantiated = 0
    for prefix, claimed in final_claims.items():
        if not claimed:
            continue
        if truth_aliased.get(prefix, False):
            if (
                pulls.get(prefix, 0) >= MIN_ALIAS_PULLS
                and responses.get(prefix, 0) >= MIN_ALIAS_RESPONSES
            ):
                caught += 1
            else:
                unsubstantiated += 1
        else:
            false_positives += 1

    claimed_total = caught + false_positives
    precision = caught / claimed_total if claimed_total else None
    admissible = (
        unsubstantiated == 0
        and (precision is None or precision >= MIN_ALIAS_CLAIM_PRECISION)
    )
    return AliasedQualification(
        caught=caught,
        false_positives=false_positives,
        unsubstantiated=unsubstantiated,
        precision=precision,
        admissible=admissible,
    )


__all__ = [
    "AliasedQualification",
    "MIN_ALIAS_CLAIM_PRECISION",
    "MIN_ALIAS_PULLS",
    "MIN_ALIAS_RESPONSES",
    "qualify_aliased",
]
