"""Strategy-child entry points for held-out B5-open program selection."""

from __future__ import annotations

from .b5_open import B5_OPEN_PROGRAMS, run_program


def run_balanced(surface) -> None:
    run_program(surface, B5_OPEN_PROGRAMS["balanced"])


def run_least_probed(surface) -> None:
    run_program(surface, B5_OPEN_PROGRAMS["least_probed"])


def run_unprobed(surface) -> None:
    run_program(surface, B5_OPEN_PROGRAMS["unprobed"])


def run_responsive(surface) -> None:
    run_program(surface, B5_OPEN_PROGRAMS["responsive"])


def run_least_then_unprobed(surface) -> None:
    run_program(surface, B5_OPEN_PROGRAMS["least_then_unprobed"])


def run_least_then_responsive(surface) -> None:
    run_program(surface, B5_OPEN_PROGRAMS["least_then_responsive"])


def run_unprobed_then_responsive(surface) -> None:
    run_program(surface, B5_OPEN_PROGRAMS["unprobed_then_responsive"])


ENTRY_POINTS = {
    "balanced": "src.baselines.b5_open_candidates:run_balanced",
    "least_probed": "src.baselines.b5_open_candidates:run_least_probed",
    "unprobed": "src.baselines.b5_open_candidates:run_unprobed",
    "responsive": "src.baselines.b5_open_candidates:run_responsive",
    "least_then_unprobed": "src.baselines.b5_open_candidates:run_least_then_unprobed",
    "least_then_responsive": "src.baselines.b5_open_candidates:run_least_then_responsive",
    "unprobed_then_responsive": "src.baselines.b5_open_candidates:run_unprobed_then_responsive",
}

__all__ = ["ENTRY_POINTS", *[f"run_{name}" for name in ENTRY_POINTS]]
