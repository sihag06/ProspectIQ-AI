"""
The orchestrator: runs stages in order, saves after each one, and can
pick up from wherever a run stopped (paused for the rep, failed, or
interrupted). Stages never call each other; only this file decides
what runs next.
"""
from __future__ import annotations

from typing import Callable

from core.db import get_run, save_run
from core.events import emit, set_listener
from core.models import STAGE_ORDER, EventType, Run, RunEvent, RunStatus, Stage
from core.pipeline import Outcome, StageResult
from providers.base import ProviderError
from search.tavily_search import SearchError
from stages import s2_identity, s3_research, s4_extract, s5_fit, s6_draft, s7_verify

# Stages are added here as they're built
HANDLERS: dict[Stage, Callable[[Run], StageResult]] = {
    Stage.IDENTITY: s2_identity.run,
    Stage.RESEARCH: s3_research.run,
    Stage.EXTRACT: s4_extract.run,
    Stage.FIT: s5_fit.run,
    Stage.DRAFT: s6_draft.run,
    Stage.VERIFY: s7_verify.run,
}

ACTIVE = {RunStatus.CREATED, RunStatus.RUNNING}


def next_stage(stage: Stage) -> Stage | None:
    i = STAGE_ORDER.index(stage)
    return STAGE_ORDER[i + 1] if i + 1 < len(STAGE_ORDER) else None


def can_advance(run: Run) -> bool:
    return run.status in ACTIVE and run.current_stage in HANDLERS


def advance(run_id: str, on_event: Callable[[RunEvent], None] | None = None) -> Run:
    """Run stages until the run finishes, pauses, fails, or reaches a stage not built yet."""
    set_listener(on_event)
    try:
        run = get_run(run_id)
        while can_advance(run):
            stage = run.current_stage
            run.status = RunStatus.RUNNING
            save_run(run)
            emit(run.id, stage, EventType.STARTED, "Started")

            try:
                result = HANDLERS[stage](run)
            except (ProviderError, SearchError) as exc:
                return _fail(run, stage, str(exc))
            except Exception as exc:  # anything unexpected still ends cleanly
                return _fail(run, stage, f"Unexpected error: {type(exc).__name__}: {exc}")

            if result.outcome is Outcome.PAUSE:
                run.status = RunStatus.NEEDS_INPUT
                save_run(run)
                emit(run.id, stage, EventType.PAUSED, result.summary, result.data)
                return run

            emit(run.id, stage, EventType.COMPLETED, result.summary, result.data)
            run.current_stage = next_stage(stage) or stage
            run.status = result.status or RunStatus.RUNNING
            if run.current_stage not in HANDLERS and run.current_stage is Stage.REVIEW:
                emit(run.id, Stage.REVIEW, EventType.PAUSED, "Waiting for your review")
            save_run(run)
        return run
    finally:
        set_listener(None)


def _fail(run: Run, stage: Stage, message: str) -> Run:
    run.status = RunStatus.FAILED
    save_run(run)
    emit(run.id, stage, EventType.FAILED, message)
    return run


def retry(run_id: str) -> None:
    """Make a failed run ready to try its failed stage again."""
    run = get_run(run_id)
    if run.status is RunStatus.FAILED:
        run.status = RunStatus.CREATED
        save_run(run)
