"""
Stage 1: intake.

Turns what the rep typed into a validated, saved run. Nothing here
calls an LLM: intake should be instant and never fail for reasons the
rep can't see.
"""
from __future__ import annotations

from core.db import get_seller_profile, save_run
from core.events import emit
from core.models import (
    EventType,
    ProspectInput,
    Provider,
    Run,
    SearchProvider,
    RunRequest,
    RunStatus,
    Stage,
)
from core.config import model_for
from providers.base import PROVIDER_LABELS, available_providers
from search.tavily_search import SEARCH_LABELS, available_search_providers


class IntakeError(Exception):
    """A problem the rep can fix in the form."""


def normalise_website(raw: str) -> str | None:
    """'acme.com' -> 'https://acme.com'. Empty stays empty."""
    raw = raw.strip()
    if not raw:
        return None
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    return raw


def create_run(prospect: ProspectInput, seller_profile_id: str, provider: Provider,
               search_provider: SearchProvider = SearchProvider.TAVILY) -> Run:
    seller = get_seller_profile(seller_profile_id)
    if seller is None:
        raise IntakeError("That seller profile no longer exists. Pick another one.")
    if not seller.capabilities or not seller.problems_solved:
        raise IntakeError(
            f"The '{seller.name}' profile needs at least one capability and one "
            "problem solved, or fit analysis has nothing to match against. "
            "Add them in Settings."
        )
    if provider not in available_providers():
        raise IntakeError(
            f"{PROVIDER_LABELS[provider]} has no API key. Add it to .env or pick another provider."
        )

    if search_provider not in available_search_providers():
        raise IntakeError(
            f"{SEARCH_LABELS[search_provider]} has no API key. Add it to .env or pick another search provider."
        )

    run = Run(
        request=RunRequest(
            prospect=prospect,
            seller_profile_id=seller.id,
            provider=provider,
            model=model_for(provider, Stage.DRAFT),  # the writing model, shown on the dashboard
            search_provider=search_provider,
        ),
        status=RunStatus.CREATED,
        current_stage=Stage.IDENTITY,  # intake is done; identity is next
    )
    save_run(run)

    p = prospect
    who = f"{p.name}, {p.title} at {p.company}" if p.title else f"{p.name} at {p.company}"
    emit(run.id, Stage.INTAKE, EventType.STARTED, "Request received")
    emit(run.id, Stage.INTAKE, EventType.COMPLETED,
         f"Researching {who} for {seller.name}",
         {"seller": seller.name, "provider": provider.value,
          "search": search_provider.value, "tone": p.tone.value})
    return run
