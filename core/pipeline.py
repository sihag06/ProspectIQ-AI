"""Shared types for pipeline stages."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from core.models import Run, RunStatus
from providers.base import LLMResult


class Outcome(StrEnum):
    CONTINUE = "continue"   # stage done, move to the next one
    PAUSE = "pause"         # wait for the rep (e.g. pick the right person)


@dataclass
class StageResult:
    outcome: Outcome
    summary: str                      # one line shown on the stage block
    data: dict = field(default_factory=dict)
    status: RunStatus | None = None   # set by the last automatic stage (e.g. READY)


def track(run: Run, result: LLMResult) -> None:
    """Add an LLM call's tokens and cost to the run's totals."""
    run.tokens_in += result.tokens_in
    run.tokens_out += result.tokens_out
    if result.cost_usd:
        run.cost_usd = round(run.cost_usd + result.cost_usd, 6)
